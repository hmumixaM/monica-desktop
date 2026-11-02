"""linkedin-cli — drive LinkedIn interactions inside a persistent browser session.

Verbs auto-start a per-session worker that owns one persistent browser profile
and reuses it across commands. One session = one account; pick it with
``--session <name>`` (or ``$LINKEDIN_CLI_SESSION``). ``session open`` remains
available as a legacy/debug bound-browser launcher.

Output contract — design decisions, kept here so they travel with the package:

* **Every verb produces a dict** — its canonical result. That one dict is both
  the ``--json`` payload and the source the human renderer summarises, so the
  two views can never drift.
* **Human-readable by default; ``--json`` on every verb for the full dict.**
  Per clig.dev ("humans first", "keep it brief, err toward less output"), the
  default is a short, scannable per-verb summary (``status`` → ``Connected``,
  ``profile`` → a few lines); ``--json`` emits the whole dict for machines.
* **No ``--out``/file flag — print to stdout, let the caller redirect.** To save
  a result: ``linkedin-cli profile alice --json > alice.json``. This matches the
  composability convention (clig.dev; ``kubectl -o``, ``aws --output``,
  ``gh --json``) and keeps the tool free of file-lifecycle concerns.
* **stdout carries only the result; logs and errors go to stderr.** Errors are an
  ``error: <type>: <message>`` line + non-zero exit (``type`` mirrors
  ``exceptions.py``). A verb that ran is exit 0 — ``message`` reports send success
  in its dict (``sent``), not via the exit code.

This module is the composition root: it owns policy (e.g. interaction pacing)
and injects it into the session — the session/action layers read no config.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

REACTION_CHOICES = ["like", "celebrate", "support", "love", "insightful", "funny"]

from linkedin_cli.enums import ProfileState
from linkedin_cli.exceptions import (
    AuthenticationError,
    CheckpointChallengeError,
    ProfileInaccessibleError,
    ReachedConnectionLimit,
    SkipProfile,
)
from linkedin_cli.session import PlaywrightCliSession, clear_session, linkedin_cli_home, read_session, session_lock
from linkedin_cli.url_utils import public_id_to_url, url_to_public_id

logger = logging.getLogger("linkedin_cli")

# Pacing policy lives here (the composition root), injected into the session.
DEFAULT_MIN_PACE_S = 5.0
DEFAULT_MAX_PACE_S = 8.0

# Exception → contract error `type`, in match order.
_ERROR_TYPES = [
    (CheckpointChallengeError, "checkpoint_challenge"),
    (AuthenticationError, "authentication"),
    (ProfileInaccessibleError, "profile_inaccessible"),
    (SkipProfile, "skip_profile"),
    (ReachedConnectionLimit, "connection_limit"),
]


# ── output helpers ─────────────────────────────────────────────────

def _out(text: str) -> None:
    """Print a result line to stdout (the only thing that touches stdout)."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    """Print a log/error line to stderr."""
    print(text, file=sys.stderr)


def _error_type(exc: Exception) -> str | None:
    for cls, name in _ERROR_TYPES:
        if isinstance(exc, cls):
            return name
    return None


def _self_block(profile: dict) -> dict:
    return {
        "public_identifier": profile.get("public_identifier"),
        "urn": profile.get("urn"),
        "full_name": profile.get("full_name"),
    }


# ── human-readable rendering (the non-`--json` default) ─────────────
#
# clig.dev: "keep it brief", "err toward less output". Each verb gets a short,
# scannable summary of its result dict; `--json` always emits the full dict.

def _human_identity(result: dict) -> str:
    member = result.get("self", result)
    return f"{member.get('full_name')} ({member.get('public_identifier')})"


def _human_state(result: dict) -> str:
    return result.get("state", "")


def _human_sent(result: dict) -> str:
    return "sent" if result.get("sent") else "not sent"


def _human_profile(result: dict) -> str:
    industry = result.get("industry") or {}
    subtitle = " · ".join(x for x in (
        result.get("location_name"),
        industry.get("name") if isinstance(industry, dict) else None,
    ) if x)
    lines = [" — ".join(x for x in (result.get("full_name"), result.get("headline")) if x)]
    if subtitle:
        lines.append(subtitle)
    lines.append(f"{len(result.get('positions') or [])} positions · "
                 f"{len(result.get('educations') or [])} schools")
    lines.append("(--json for the full record)")
    return "\n".join(lines)


def _human_thread(result: dict) -> str:
    messages = result.get("messages")
    if not messages:
        return "(no conversation)"
    return "\n".join(
        f"{m.get('timestamp', '')}  {m.get('sender', '')}: {m.get('text', '')}"
        for m in messages
    )


def _human_inbox(result: dict) -> str:
    conversations = result.get("conversations") or []
    if not conversations:
        return "(no conversations)"
    lines = [f"{len(conversations)} conversation(s):"]
    for conversation in conversations:
        participants = ", ".join(
            p.get("public_identifier") or p.get("name") or p.get("urn") or "unknown"
            for p in conversation.get("participants") or []
        )
        preview = " ".join((conversation.get("last_message") or "").split())
        lines.append("  " + " — ".join(x for x in (
            conversation.get("thread_id"),
            participants,
            conversation.get("last_activity_at"),
            preview,
        ) if x)[:220])
    return "\n".join(lines)


def _human_search(result: dict) -> str:
    profiles = result.get("profiles") or []
    if not profiles:
        return "(no results)"
    header = f"{len(profiles)} result(s) on page {result.get('page', 1)}:"
    return "\n".join(
        [header] + [
            "  " + " — ".join(x for x in (
                p.get("public_identifier"),
                p.get("full_name"),
                p.get("headline"),
            ) if x)
            for p in profiles
        ]
    )


def _human_jobs_search(result: dict) -> str:
    jobs = result.get("jobs") or []
    if not jobs:
        return "(no results)"
    header = f"{len(jobs)} job(s) on page {result.get('page', 1)}:"
    return "\n".join(
        [header] + [
            "  " + " — ".join(x for x in (j.get("job_id"), j.get("title"), j.get("company")) if x)
            for j in jobs
        ]
    )


def _human_jobs_saved(result: dict) -> str:
    jobs = result.get("jobs") or []
    card_type = result.get("type", "saved")
    if not jobs:
        return f"(no {card_type} jobs)"
    header = f"{len(jobs)} {card_type} job(s):"
    return "\n".join(
        [header] + [
            "  " + " — ".join(x for x in (j.get("job_id"), j.get("title"), j.get("company")) if x)
            for j in jobs
        ]
    )


def _human_jobs_show(result: dict) -> str:
    lines = [" — ".join(x for x in (result.get("title"), result.get("company")) if x)]
    subtitle = " · ".join(x for x in (
        result.get("location"),
        result.get("workplace"),
        result.get("employment_type"),
        "saved" if result.get("saved") else None,
        result.get("apply_method"),
    ) if x)
    if subtitle:
        lines.append(subtitle)
    if result.get("apply_url"):
        lines.append(f"apply: {result['apply_url']}")
    lines.append("(--json for the full record)")
    return "\n".join(line for line in lines if line)


def _human_jobs_save(result: dict) -> str:
    if "jobs" in result:
        jobs = result.get("jobs") or []
        changed = sum(1 for job in jobs if job.get("changed"))
        return f"unsaved {changed}/{len(jobs)}"
    return "saved" if result.get("saved") else "not saved"


def _human_jobs_apply(result: dict) -> str:
    if result.get("submitted"):
        return "submitted"
    if result.get("apply_url"):
        return f"external apply: {result['apply_url']}"
    if result.get("method") == "easy_apply":
        return f"easy apply opened: {result.get('next_step', 'review_modal')}"
    return "manual apply required"


def _human_posts(result: dict) -> str:
    posts = result.get("posts") or result.get("scheduled_posts") or []
    if not posts:
        return "(no posts)"
    header = f"{len(posts)} post(s) on page {result.get('page', 1)}:" if result.get("page") else f"{len(posts)} scheduled post(s):"
    return "\n".join(
        [header] + [
            "  " + " — ".join(x for x in (
                str(p.get("index")) if p.get("index") is not None else None,
                p.get("activity_id"),
                p.get("scheduled_at"),
                p.get("author"),
                (p.get("content") or "").splitlines()[0][:80] if p.get("content") else None,
            ) if x)
            for p in posts
        ]
    )


def _human_post(result: dict) -> str:
    engagement = result.get("engagement") or {}
    lines = [" — ".join(x for x in (result.get("activity_id"), result.get("author")) if x)]
    if result.get("content"):
        lines.append(result["content"])
    lines.append(
        " · ".join(
            f"{engagement.get(key) or 0} {key}"
            for key in ("reactions", "comments", "reposts")
        )
    )
    lines.append("(--json for the full record)")
    return "\n".join(line for line in lines if line)


def _human_comments(result: dict) -> str:
    comments = result.get("comments") or []
    if not comments:
        return "(no comments)"
    lines = [f"{len(comments)} comment(s):"]
    for comment in comments:
        lines.append("  " + " — ".join(x for x in (comment.get("comment_id"), comment.get("author"), comment.get("text")) if x)[:180])
    return "\n".join(lines)


def _human_post_write(result: dict) -> str:
    if result.get("posted"):
        return "posted"
    if result.get("drafted"):
        return "draft saved"
    if result.get("scheduled"):
        return f"scheduled for {result.get('scheduled_at')}"
    if result.get("updated"):
        if result.get("index") is not None:
            return f"updated scheduled post {result.get('index')} to {result.get('scheduled_at')}"
        return "updated"
    if result.get("cancelled"):
        return f"cancelled scheduled post {result.get('index')}"
    if result.get("deleted"):
        return "deleted"
    if result.get("replied"):
        return "replied"
    if result.get("reacted"):
        return f"reacted: {result.get('reaction')}"
    return "not completed"


def _human_notifications(result: dict) -> str:
    notifications = result.get("notifications") or []
    if not notifications:
        return "(no notifications)"
    lines = [f"{len(notifications)} notification(s):"]
    for item in notifications:
        marker = "unread" if item.get("unread") else "read"
        text = item.get("text") or ""
        lines.append(f"  [{marker}] {text[:160]}")
    return "\n".join(lines)


def _human_page_posts(result: dict) -> str:
    posts = result.get("posts") or result.get("scheduled_posts") or []
    if not posts:
        return "(no page posts)"
    tab = result.get("tab") or ("scheduled" if result.get("scheduled_posts") is not None else "published")
    header = f"{len(posts)} page post(s) in {tab}:"
    return "\n".join(
        [header] + [
            "  " + " — ".join(x for x in (
                str(p.get("index")) if p.get("index") is not None else None,
                p.get("activity_id"),
                p.get("scheduled_at"),
                (p.get("content") or "").splitlines()[0][:100] if p.get("content") else None,
            ) if x)
            for p in posts
        ]
    )


def _human_page_list(result: dict) -> str:
    pages = result.get("pages") or []
    if not pages:
        return "(no admin pages found)"
    return "\n".join([f"{len(pages)} admin page(s):"] + [
        "  " + " — ".join(x for x in (p.get("company_id"), p.get("name"), p.get("admin_url")) if x)
        for p in pages
    ])


def _human_page_post_write(result: dict) -> str:
    if result.get("posted"):
        return "posted"
    if result.get("scheduled"):
        return f"scheduled for {result.get('scheduled_at')}"
    if result.get("updated"):
        if result.get("index") is not None:
            return f"updated scheduled page post {result.get('index')} to {result.get('scheduled_at')}"
        return "updated"
    if result.get("cancelled"):
        return f"cancelled scheduled page post {result.get('index')}"
    if result.get("deleted"):
        return "deleted"
    return "not completed"


def _human_page_inbox(result: dict) -> str:
    threads = result.get("threads") or []
    if not threads:
        return "(no inbox threads)"
    return "\n".join([f"{len(threads)} thread(s):"] + [
        "  " + " — ".join(x for x in (t.get("thread_id"), t.get("summary")) if x)[:180]
        for t in threads
    ])


def _human_page_thread(result: dict) -> str:
    messages = result.get("messages") or []
    if not messages:
        return "(no messages)"
    return "\n".join(
        f"{m.get('sender', '')}: {m.get('text', '')}"
        for m in messages
    )


def _human_closed(result: dict) -> str:
    return f"closed {result.get('name')}"


_HUMAN = {
    "login": _human_identity,
    "whoami": _human_identity,
    "status": _human_state,
    "connect": _human_state,
    "message": _human_sent,
    "profile": _human_profile,
    "inbox": _human_inbox,
    "thread": _human_thread,
    "search": _human_search,
    "jobs-search": _human_jobs_search,
    "jobs-saved": _human_jobs_saved,
    "jobs-show": _human_jobs_show,
    "jobs-save": _human_jobs_save,
    "jobs-unsave": _human_jobs_save,
    "jobs-apply": _human_jobs_apply,
    "posts-profile": _human_posts,
    "posts-search": _human_posts,
    "posts-show": _human_post,
    "posts-engagement": _human_post,
    "posts-comments": _human_comments,
    "posts-create": _human_post_write,
    "posts-edit": _human_post_write,
    "posts-draft": _human_post_write,
    "posts-schedule": _human_post_write,
    "posts-update-schedule": _human_post_write,
    "posts-scheduled": _human_posts,
    "posts-cancel": _human_post_write,
    "posts-delete": _human_post_write,
    "posts-comment-reply": _human_post_write,
    "posts-react": _human_post_write,
    "posts-comment-react": _human_post_write,
    "notifications": _human_notifications,
    "notifications-reply": _human_post_write,
    "notifications-react": _human_post_write,
    "page-list": _human_page_list,
    "page-posts": _human_page_posts,
    "page-post": _human_post,
    "page-post-create": _human_page_post_write,
    "page-post-edit": _human_page_post_write,
    "page-post-schedule": _human_page_post_write,
    "page-post-update-schedule": _human_page_post_write,
    "page-post-scheduled": _human_page_posts,
    "page-post-cancel": _human_page_post_write,
    "page-post-delete": _human_page_post_write,
    "page-inbox": _human_page_inbox,
    "page-thread": _human_page_thread,
    "page-reply": _human_sent,
    "session-close": _human_closed,
}


def _render(command: str, result: dict, as_json: bool) -> None:
    """Print *result*: the full dict as JSON if ``--json``, else a brief summary."""
    if as_json:
        _out(json.dumps(result, ensure_ascii=False, default=str))
        return
    renderer = _HUMAN.get(command)
    _out(renderer(result) if renderer
         else "\n".join(f"{k}: {v}" for k, v in result.items()))


def _handle_to_profile(handle: str) -> dict:
    """Build a minimal ``{public_identifier, url}`` from a <url|id> handle."""
    public_id = url_to_public_id(handle) if "/" in handle else handle
    if not public_id:
        raise ValueError(f"Could not resolve a public identifier from {handle!r}")
    return {"public_identifier": public_id, "url": public_id_to_url(public_id)}


def _scrape(session, handle: str) -> dict:
    """Scrape the target so urn-dependent verbs (message/thread) have its ``urn``."""
    from linkedin_cli.actions.profile import scrape_profile

    profile, _data = scrape_profile(session, _handle_to_profile(handle))
    if not profile:
        raise ProfileInaccessibleError(handle)
    return profile


# ── verbs ──────────────────────────────────────────────────────────

def _verb_login(session, args) -> dict:
    from linkedin_cli.auth import authenticate

    authenticate(session)
    return {"account": args.name, "self": _self_block(session.self_profile)}


def _verb_whoami(session, args) -> dict:
    return {"self": _self_block(session.self_profile)}


def _verb_profile(session, args) -> dict:
    from linkedin_cli.actions.profile import scrape_profile

    profile, data = scrape_profile(session, _handle_to_profile(args.handle))
    if not profile:
        raise ProfileInaccessibleError(args.handle)
    out = dict(profile)
    if args.raw:
        out["_raw"] = data
    return out


def _verb_status(session, args) -> dict:
    from linkedin_cli.actions.status import get_connection_status

    profile = _handle_to_profile(args.handle)
    state = get_connection_status(session, profile)
    return {"public_identifier": profile["public_identifier"], "state": state.value}


def _verb_connect(session, args) -> dict:
    from linkedin_cli.actions.connect import send_connection_request
    from linkedin_cli.actions.status import get_connection_status

    profile = _handle_to_profile(args.handle)
    state = get_connection_status(session, profile)
    if state not in (ProfileState.CONNECTED, ProfileState.PENDING):
        state = send_connection_request(session, profile)
    return {"public_identifier": profile["public_identifier"], "state": state.value}


def _verb_message(session, args) -> dict:
    from linkedin_cli.actions.message import send_raw_message

    profile = _scrape(session, args.handle)
    sent = send_raw_message(session, profile, args.text, attachments=args.attachment)
    return {"public_identifier": profile.get("public_identifier"), "sent": sent, "attachments": args.attachment}


def _verb_thread(session, args) -> dict:
    from linkedin_cli.actions.conversations import get_conversation, get_conversation_by_thread_id

    if args.thread_id:
        messages = get_conversation_by_thread_id(session, args.thread_id, session.self_profile["urn"], limit=args.limit)
        return {"thread_id": args.thread_id, "messages": messages}
    if not args.handle:
        raise ValueError("thread requires a profile handle/URL or --thread-id")

    profile = _scrape(session, args.handle)
    messages = get_conversation(session, profile.get("urn"), session.self_profile["urn"], limit=args.limit)
    return {"public_identifier": profile.get("public_identifier"), "messages": messages}


def _verb_inbox(session, args) -> dict:
    from linkedin_cli.actions.conversations import list_conversations

    return list_conversations(session, limit=args.limit)


def _verb_search(session, args) -> dict:
    from linkedin_cli.actions.search import NETWORK_CODES, search_people

    codes = [NETWORK_CODES[n] for n in (args.network or [])]
    return search_people(
        session,
        args.keywords,
        page=args.page,
        network=codes or None,
        limit=args.limit,
        geo_urn=args.geo_urn,
        current_company=args.current_company,
        is_verified=args.verified,
        connection_of=args.connection_of,
        follower_of=args.follower_of,
        past_company=args.past_company,
        school=args.school,
        industry=args.industry,
        profile_language=args.profile_language,
        open_to_volunteer=args.open_to_volunteer,
        service_category=args.service_category,
    )


def _verb_jobs_search(session, args) -> dict:
    from linkedin_cli.actions.jobs import search_jobs

    return search_jobs(
        session,
        args.keywords,
        location=args.location,
        page=args.page,
        easy_apply=args.easy_apply,
        remote=args.remote,
        date_posted=args.date_posted,
        job_type=args.job_type,
    )


def _verb_jobs_saved(session, args) -> dict:
    from linkedin_cli.actions.jobs import saved_jobs

    return saved_jobs(session, page=args.page, card_type=args.type, limit=args.limit)


def _verb_jobs_show(session, args) -> dict:
    from linkedin_cli.actions.jobs import show_job

    return show_job(session, args.job)


def _verb_jobs_save(session, args) -> dict:
    from linkedin_cli.actions.jobs import save_job

    return save_job(session, args.job)


def _verb_jobs_unsave(session, args) -> dict:
    from linkedin_cli.actions.jobs import unsave_job

    return unsave_job(session, args.jobs)


def _verb_jobs_apply(session, args) -> dict:
    from linkedin_cli.actions.jobs import apply_job

    return apply_job(session, args.job, submit=args.submit)


def _verb_posts_profile(session, args) -> dict:
    from linkedin_cli.actions.posts import profile_posts

    return profile_posts(session, args.handle, page=args.page, limit=args.limit)


def _verb_posts_search(session, args) -> dict:
    from linkedin_cli.actions.posts import search_posts

    return search_posts(
        session,
        args.keywords,
        page=args.page,
        limit=args.limit,
        sort=args.sort,
        date_posted=args.date_posted,
        content_type=args.content_type,
        from_member=args.from_member,
        posted_by=args.posted_by,
        author_company=args.author_company,
        author_job_title=args.author_job_title,
    )


def _verb_posts_show(session, args) -> dict:
    from linkedin_cli.actions.posts import show_post

    return show_post(session, args.post)


def _verb_posts_engagement(session, args) -> dict:
    from linkedin_cli.actions.posts import post_engagement

    return post_engagement(session, args.post)


def _verb_posts_comments(session, args) -> dict:
    from linkedin_cli.actions.posts import list_post_comments

    return list_post_comments(session, args.post, limit=args.limit)


def _verb_posts_create(session, args) -> dict:
    from linkedin_cli.actions.posts import create_post

    return create_post(
        session,
        args.text,
        images=args.image,
        documents=args.document,
        poll_question=args.poll_question,
        poll_options=args.poll_option,
    )


def _verb_posts_edit(session, args) -> dict:
    from linkedin_cli.actions.posts import edit_post

    return edit_post(session, args.post, args.text)


def _verb_posts_draft(session, args) -> dict:
    from linkedin_cli.actions.posts import draft_post

    return draft_post(session, args.text)


def _verb_posts_schedule(session, args) -> dict:
    from linkedin_cli.actions.posts import schedule_post

    return schedule_post(session, args.text, args.at)


def _verb_posts_update_schedule(session, args) -> dict:
    from linkedin_cli.actions.posts import update_scheduled_post_time

    return update_scheduled_post_time(session, args.index, args.at, text=args.text)


def _verb_posts_scheduled(session, args) -> dict:
    from linkedin_cli.actions.posts import list_scheduled_posts

    return list_scheduled_posts(session)


def _verb_posts_cancel(session, args) -> dict:
    from linkedin_cli.actions.posts import cancel_scheduled_post

    return cancel_scheduled_post(session, args.index)


def _verb_posts_delete(session, args) -> dict:
    from linkedin_cli.actions.posts import delete_post

    return delete_post(session, args.post)


def _verb_posts_comment_reply(session, args) -> dict:
    from linkedin_cli.actions.posts import reply_to_comment

    return reply_to_comment(session, args.post, comment_id=args.comment_id, author=args.author, text=args.text)


def _verb_posts_react(session, args) -> dict:
    from linkedin_cli.actions.posts import react_to_post

    return react_to_post(session, args.post, reaction=args.reaction)


def _verb_posts_comment_react(session, args) -> dict:
    from linkedin_cli.actions.posts import react_to_comment

    return react_to_comment(session, args.post, comment_id=args.comment_id, author=args.author, reaction=args.reaction)


def _verb_notifications(session, args) -> dict:
    from linkedin_cli.actions.notifications import list_notifications

    return list_notifications(session, limit=args.limit)


def _verb_notifications_reply(session, args) -> dict:
    from linkedin_cli.actions.notifications import reply_to_notification

    return reply_to_notification(session, index=args.index, text=args.text)


def _verb_notifications_react(session, args) -> dict:
    from linkedin_cli.actions.notifications import react_to_notification

    return react_to_notification(session, index=args.index, reaction=args.reaction)


def _verb_page_posts(session, args) -> dict:
    from linkedin_cli.actions.page_admin import list_page_posts

    return list_page_posts(session, args.company, limit=args.limit)


def _verb_page_list(session, args) -> dict:
    from linkedin_cli.actions.page_admin import list_admin_pages

    return list_admin_pages(session)


def _verb_page_post(session, args) -> dict:
    from linkedin_cli.actions.posts import show_post

    return show_post(session, args.post)


def _verb_page_post_create(session, args) -> dict:
    from linkedin_cli.actions.page_admin import create_page_post

    return create_page_post(
        session,
        args.company,
        args.text,
        images=args.image,
        documents=args.document,
        poll_question=args.poll_question,
        poll_options=args.poll_option,
    )


def _verb_page_post_edit(session, args) -> dict:
    from linkedin_cli.actions.page_admin import edit_page_post

    return edit_page_post(session, args.company, args.post, args.text)


def _verb_page_post_schedule(session, args) -> dict:
    from linkedin_cli.actions.page_admin import schedule_page_post

    return schedule_page_post(session, args.company, args.text, args.at)


def _verb_page_post_update_schedule(session, args) -> dict:
    from linkedin_cli.actions.page_admin import update_page_scheduled_post_time

    return update_page_scheduled_post_time(session, args.company, args.index, args.at, text=args.text)


def _verb_page_post_scheduled(session, args) -> dict:
    from linkedin_cli.actions.page_admin import list_page_scheduled_posts

    return list_page_scheduled_posts(session, args.company)


def _verb_page_post_cancel(session, args) -> dict:
    from linkedin_cli.actions.page_admin import cancel_page_scheduled_post

    return cancel_page_scheduled_post(session, args.company, args.index)


def _verb_page_post_delete(session, args) -> dict:
    from linkedin_cli.actions.page_admin import delete_page_post

    return delete_page_post(session, args.company, args.post)


def _verb_page_inbox(session, args) -> dict:
    from linkedin_cli.actions.page_admin import list_page_inbox

    return list_page_inbox(session, args.company, limit=args.limit)


def _verb_page_thread(session, args) -> dict:
    from linkedin_cli.actions.page_admin import page_inbox_thread

    return page_inbox_thread(session, args.company, args.thread, limit=args.limit)


def _verb_page_reply(session, args) -> dict:
    from linkedin_cli.actions.page_admin import reply_page_inbox_thread

    return reply_page_inbox_thread(session, args.company, args.thread, args.text, attachments=args.attachment)


_VERBS = {
    "login": _verb_login,
    "whoami": _verb_whoami,
    "profile": _verb_profile,
    "status": _verb_status,
    "connect": _verb_connect,
    "message": _verb_message,
    "inbox": _verb_inbox,
    "thread": _verb_thread,
    "search": _verb_search,
    "jobs-search": _verb_jobs_search,
    "jobs-saved": _verb_jobs_saved,
    "jobs-show": _verb_jobs_show,
    "jobs-save": _verb_jobs_save,
    "jobs-unsave": _verb_jobs_unsave,
    "jobs-apply": _verb_jobs_apply,
    "posts-profile": _verb_posts_profile,
    "posts-search": _verb_posts_search,
    "posts-show": _verb_posts_show,
    "posts-engagement": _verb_posts_engagement,
    "posts-comments": _verb_posts_comments,
    "posts-create": _verb_posts_create,
    "posts-edit": _verb_posts_edit,
    "posts-draft": _verb_posts_draft,
    "posts-schedule": _verb_posts_schedule,
    "posts-update-schedule": _verb_posts_update_schedule,
    "posts-scheduled": _verb_posts_scheduled,
    "posts-cancel": _verb_posts_cancel,
    "posts-delete": _verb_posts_delete,
    "posts-comment-reply": _verb_posts_comment_reply,
    "posts-react": _verb_posts_react,
    "posts-comment-react": _verb_posts_comment_react,
    "notifications": _verb_notifications,
    "notifications-reply": _verb_notifications_reply,
    "notifications-react": _verb_notifications_react,
    "page-list": _verb_page_list,
    "page-posts": _verb_page_posts,
    "page-post": _verb_page_post,
    "page-post-create": _verb_page_post_create,
    "page-post-edit": _verb_page_post_edit,
    "page-post-schedule": _verb_page_post_schedule,
    "page-post-update-schedule": _verb_page_post_update_schedule,
    "page-post-scheduled": _verb_page_post_scheduled,
    "page-post-cancel": _verb_page_post_cancel,
    "page-post-delete": _verb_page_post_delete,
    "page-inbox": _verb_page_inbox,
    "page-thread": _verb_page_thread,
    "page-reply": _verb_page_reply,
}


# ── session lifecycle commands ─────────────────────────────────────

def _cmd_session_open(args) -> int:
    from linkedin_cli.launcher import open_bound_session

    profile_dir = str(linkedin_cli_home() / "profiles" / args.name)
    open_bound_session(args.name, profile_dir=profile_dir)
    return 0


def _cmd_session_close(args) -> int:
    from linkedin_cli.worker import stop_worker

    stop_worker(args.name)
    with session_lock(args.name):
        record = read_session(args.name)
        if not record:
            _render("session-close", {"name": args.name, "closed": True}, args.json)
            return 0
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        clear_session(args.name)
        _render("session-close", {"name": args.name, "closed": True}, args.json)
        return 0


# ── verb runner ────────────────────────────────────────────────────

def _execute_verb(args, session) -> int:
    try:
        _render(args.verb, _VERBS[args.verb](session, args), args.json)
        return 0
    except Exception as exc:  # noqa: BLE001 — map known errors, re-raise the rest
        error_type = _error_type(exc)
        if error_type is None:
            raise
        _err(f"error: {error_type}: {exc}")
        return 1


def _run_verb_bound(args) -> int:
    """Run a verb against an explicitly opened legacy bound session."""
    record = read_session(args.name)
    if not record:
        _err(f"error: usage: no open session named {args.name!r} — run "
             f"`linkedin-cli session open --session {args.name}`")
        return 2

    session = PlaywrightCliSession(
        record["endpoint"],
        min_pace=DEFAULT_MIN_PACE_S,
        max_pace=DEFAULT_MAX_PACE_S,
        username=os.environ.get("LINKEDIN_USERNAME"),
        password=os.environ.get("LINKEDIN_PASSWORD"),
        name=args.name,
    )
    try:
        session.ensure_browser()
        return _execute_verb(args, session)
    finally:
        session.close()


def _run_verb(args, argv: list[str]) -> int:
    if os.environ.get("LINKEDIN_CLI_WORKER") == "1":
        return _run_verb_bound(args)
    if os.environ.get("LINKEDIN_CLI_BOUND") == "1":
        with session_lock(args.name):
            return _run_verb_bound(args)

    from linkedin_cli.worker import run_via_worker

    return run_via_worker(args.name, argv)


# ── parser ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--session", "--name", dest="name",
        default=os.environ.get("LINKEDIN_CLI_SESSION", "default"),
        help="Bound session name (default: $LINKEDIN_CLI_SESSION or 'default')",
    )
    common.add_argument(
        "--json", action="store_true",
        help="Emit the full result as JSON instead of a human-readable summary",
    )

    parser = argparse.ArgumentParser(prog="linkedin-cli", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # session open / close
    session_cmd = sub.add_parser("session", help="Manage the bound browser session")
    session_sub = session_cmd.add_subparsers(dest="subcmd", required=True)
    session_sub.add_parser("open", parents=[common], help="Launch + bind a legacy debug browser, then block")
    session_sub.add_parser("close", parents=[common], help="Stop the worker or legacy session launcher")

    # verbs
    handle_help = "Profile URL or public identifier (e.g. alice-smith)"

    sub.add_parser("login", parents=[common],
                   help="Log the session in (fill the form, clear a checkpoint) and report the logged-in member")
    sub.add_parser("whoami", parents=[common],
                   help="Report who the session is logged in as — no login, no checkpoint")

    p_profile = sub.add_parser("profile", parents=[common],
                               help="Scrape a member's full profile: headline, positions, education, location")
    p_profile.add_argument("handle", help=handle_help)
    p_profile.add_argument("--raw", action="store_true", help="Also emit the untouched Voyager blob under _raw")

    sub.add_parser("status", parents=[common],
                   help="Report the connection state with the member: Connected, Pending, or Qualified"
                   ).add_argument("handle", help=handle_help)
    sub.add_parser("connect", parents=[common],
                   help="Send a connection request (no note); no-op if already Connected or Pending"
                   ).add_argument("handle", help=handle_help)
    p_inbox = sub.add_parser("inbox", parents=[common],
                             help="List recent personal messaging conversations")
    p_inbox.add_argument("--limit", type=int, default=20, help="Maximum conversations to return (default: 20)")
    p_thread = sub.add_parser("thread", parents=[common],
                                help="Dump the conversation with the member as a list of messages (newest last)")
    p_thread.add_argument("handle", nargs="?", help=handle_help)
    p_thread.add_argument("--thread-id", help="Personal messaging thread_id returned by inbox")
    p_thread.add_argument("--limit", type=int, default=50, help="Maximum messages to return (default: 50)")

    p_message = sub.add_parser("message", parents=[common],
                               help="Send a direct message to the member")
    p_message.add_argument("handle", help=handle_help)
    p_message.add_argument("--text", required=True, help="Message body to send")
    p_message.add_argument("--attachment", action="append", default=[], help="File to attach (repeatable)")

    p_search = sub.add_parser("search", parents=[common],
                              help="Search People by keyword; list matching profile handles")
    p_search.add_argument("keywords", help="Search keywords, e.g. 'San Francisco'")
    p_search.add_argument("--network", action="append", choices=["first", "second", "third"],
                           help="Filter by connection degree (repeatable): first / second / third")
    p_search.add_argument("--page", type=int, default=1, help="Result page (default: 1)")
    p_search.add_argument("--limit", type=int, default=10, help="Maximum visible profiles to return (default: 10)")
    p_search.add_argument("--geo-urn", action="append", default=[], help="LinkedIn geoUrn id filter (repeatable)")
    p_search.add_argument("--current-company", action="append", default=[], help="Current company id filter (repeatable)")
    p_search.add_argument("--verified", action="store_true", help="Only show verified profiles")
    p_search.add_argument("--connection-of", action="append", default=[], help="Member URN id whose connections should match (repeatable)")
    p_search.add_argument("--follower-of", action="append", default=[], help="Member URN id whose followers should match (repeatable)")
    p_search.add_argument("--past-company", action="append", default=[], help="Past company id filter (repeatable)")
    p_search.add_argument("--school", action="append", default=[], help="School id filter (repeatable)")
    p_search.add_argument("--industry", action="append", default=[], help="Industry id filter (repeatable)")
    p_search.add_argument("--profile-language", action="append", default=[], help="Profile language code filter, e.g. en or pt (repeatable)")
    p_search.add_argument("--open-to-volunteer", action="store_true", help="Only show profiles open to volunteering")
    p_search.add_argument("--service-category", action="append", default=[], help="Service category id filter (repeatable)")

    jobs_cmd = sub.add_parser("jobs", help="Search, save, and apply to LinkedIn jobs")
    jobs_sub = jobs_cmd.add_subparsers(dest="jobs_cmd", required=True)

    p_jobs_search = jobs_sub.add_parser("search", parents=[common], help="Search LinkedIn Jobs by keyword")
    p_jobs_search.add_argument("keywords", help="Search keywords, e.g. 'software engineer'")
    p_jobs_search.add_argument("--location", help="Optional location, e.g. 'United States'")
    p_jobs_search.add_argument("--page", type=int, default=1, help="Result page (default: 1)")
    p_jobs_search.add_argument("--easy-apply", action="store_true", help="Only show Easy Apply jobs")
    p_jobs_search.add_argument("--remote", action="store_true", help="Only show remote jobs")
    p_jobs_search.add_argument("--date-posted", choices=["past-24h", "past-week", "past-month"], help="Filter by when the job was posted")
    p_jobs_search.add_argument("--job-type", choices=["full-time", "part-time", "contract", "temporary", "internship"], help="Filter by job type")

    p_jobs_saved = jobs_sub.add_parser("saved", parents=[common], help="List saved LinkedIn jobs")
    p_jobs_saved.add_argument("--page", type=int, default=1, help="Result page (default: 1)")
    p_jobs_saved.add_argument("--limit", type=int, default=10, help="Maximum visible jobs to return (default: 10)")
    p_jobs_saved.add_argument(
        "--type",
        choices=["saved", "in-progress", "applied", "archived"],
        default="saved",
        help="Saved jobs card type to list (default: saved)",
    )

    jobs_handle_help = "LinkedIn job id or URL"
    jobs_sub.add_parser("show", parents=[common], help="Show structured details for a LinkedIn job").add_argument("job", help=jobs_handle_help)
    jobs_sub.add_parser("save", parents=[common], help="Save a LinkedIn job").add_argument("job", help=jobs_handle_help)
    jobs_sub.add_parser("unsave", parents=[common], help="Unsave one or more LinkedIn jobs").add_argument("jobs", nargs="+", help=jobs_handle_help)

    p_jobs_apply = jobs_sub.add_parser("apply", parents=[common], help="Start or submit an Easy Apply job application")
    p_jobs_apply.add_argument("job", help=jobs_handle_help)
    p_jobs_apply.add_argument("--submit", action="store_true", help="Submit only if the first Easy Apply dialog is immediately ready")

    posts_cmd = sub.add_parser("posts", help="Read, create, edit, schedule, and engage with LinkedIn posts")
    posts_sub = posts_cmd.add_subparsers(dest="posts_cmd", required=True)

    p_posts_profile = posts_sub.add_parser("profile", parents=[common], help="List visible recent posts for a member profile")
    p_posts_profile.add_argument("handle", help=handle_help)
    p_posts_profile.add_argument("--page", type=int, default=1, help="Result page (default: 1)")
    p_posts_profile.add_argument("--limit", type=int, default=10, help="Maximum visible posts to return (default: 10)")

    p_posts_search = posts_sub.add_parser("search", parents=[common], help="Search LinkedIn content/posts by keyword")
    p_posts_search.add_argument("keywords", help="Search keywords, e.g. 'AI Developer Melbourne'")
    p_posts_search.add_argument("--page", type=int, default=1, help="Result page (default: 1)")
    p_posts_search.add_argument("--limit", type=int, default=10, help="Maximum visible posts to return (default: 10)")
    p_posts_search.add_argument("--sort", choices=["latest", "top-match"], help="Sort results by latest or top match")
    p_posts_search.add_argument("--date-posted", choices=["past-24h", "past-week", "past-month"], help="Filter by when the post was published")
    p_posts_search.add_argument("--content-type", choices=["videos", "photos", "jobs", "liveVideos", "documents"], help="Filter by content type")
    p_posts_search.add_argument("--from-member", action="append", default=[], help="Author member URN id to filter by (repeatable)")
    p_posts_search.add_argument("--posted-by", action="append", choices=["me", "first", "following"], default=[], help="Filter by poster relationship (repeatable): me / first / following")
    p_posts_search.add_argument("--author-company", action="append", default=[], help="Author company id to filter by (repeatable)")
    p_posts_search.add_argument("--author-job-title", help="Author job title keyword filter")

    posts_handle_help = "LinkedIn activity id, activity/share URN, or /feed/update/... URL"
    posts_sub.add_parser("show", parents=[common], help="Show visible post content and aggregate engagement").add_argument("post", help=posts_handle_help)
    posts_sub.add_parser("engagement", parents=[common], help="Show aggregate engagement and visible comments for a post").add_argument("post", help=posts_handle_help)

    p_posts_comments = posts_sub.add_parser("comments", parents=[common], help="List visible comments for a post")
    p_posts_comments.add_argument("post", help=posts_handle_help)
    p_posts_comments.add_argument("--limit", type=int, default=20, help="Maximum visible comments to return (default: 20)")

    p_posts_create = posts_sub.add_parser("create", parents=[common], help="Create a LinkedIn post with optional images, documents, or a poll")
    p_posts_create.add_argument("--text", required=True, help="Post body text")
    p_posts_create.add_argument("--image", action="append", default=[], help="Image file to attach (repeatable)")
    p_posts_create.add_argument("--document", action="append", default=[], help="Document file to attach (repeatable)")
    p_posts_create.add_argument("--poll-question", help="Poll question; defaults to the post text when LinkedIn requires one")
    p_posts_create.add_argument("--poll-option", action="append", default=[], help="Poll option (repeat at least twice to create a poll)")

    p_posts_edit = posts_sub.add_parser("edit", parents=[common], help="Edit the text of an already-published LinkedIn post")
    p_posts_edit.add_argument("post", help=posts_handle_help)
    p_posts_edit.add_argument("--text", required=True, help="Replacement post body text")

    p_posts_draft = posts_sub.add_parser("draft", parents=[common], help="Write a post, close the composer, and save LinkedIn's draft prompt")
    p_posts_draft.add_argument("--text", required=True, help="Draft body text")

    p_posts_schedule = posts_sub.add_parser("schedule", parents=[common], help="Schedule a text post for a specific local datetime")
    p_posts_schedule.add_argument("--text", required=True, help="Post body text")
    p_posts_schedule.add_argument("--at", required=True, help="Local ISO datetime, e.g. 2026-06-10T09:30")

    p_posts_update_schedule = posts_sub.add_parser("update-schedule", parents=[common], help="Update the scheduled datetime and/or text for a scheduled post by 1-based index")
    p_posts_update_schedule.add_argument("index", type=int, help="1-based index from the scheduled posts list")
    p_posts_update_schedule.add_argument("--at", help="Local ISO datetime, e.g. 2026-06-10T09:30")
    p_posts_update_schedule.add_argument("--text", help="Replacement post body text")

    posts_sub.add_parser("scheduled", parents=[common], help="List scheduled posts")

    p_posts_cancel = posts_sub.add_parser("cancel", parents=[common], help="Cancel a scheduled post by 1-based index")
    p_posts_cancel.add_argument("index", type=int, help="1-based index from the scheduled posts list")

    posts_sub.add_parser("delete", parents=[common], help="Delete a LinkedIn post by id, URN, or URL").add_argument("post", help=posts_handle_help)

    p_posts_comment_reply = posts_sub.add_parser("comment-reply", parents=[common], help="Reply to a visible comment on a post")
    p_posts_comment_reply.add_argument("post", help="Post id/URL or full comment URL")
    p_posts_comment_reply.add_argument("--comment-id", help="LinkedIn comment id when post is not a full comment URL")
    p_posts_comment_reply.add_argument("--author", help="Visible comment author to target, e.g. 'Yanca Ranzone'")
    p_posts_comment_reply.add_argument("--text", required=True, help="Reply body text")

    p_posts_react = posts_sub.add_parser("react", parents=[common], help="React to a post")
    p_posts_react.add_argument("post", help=posts_handle_help)
    p_posts_react.add_argument("--reaction", default="like", choices=REACTION_CHOICES, help="Reaction type (default: like)")

    p_posts_comment_react = posts_sub.add_parser("comment-react", parents=[common], help="React to a visible comment on a post")
    p_posts_comment_react.add_argument("post", help="Post id/URL or full comment URL")
    p_posts_comment_react.add_argument("--comment-id", help="LinkedIn comment id when post is not a full comment URL")
    p_posts_comment_react.add_argument("--author", help="Visible comment author to target, e.g. 'Yanca Ranzone'")
    p_posts_comment_react.add_argument("--reaction", default="like", choices=REACTION_CHOICES, help="Reaction type (default: like)")

    p_notifications = sub.add_parser("notifications", parents=[common], help="List visible LinkedIn notifications")
    p_notifications.add_argument("--limit", type=int, default=20, help="Maximum visible notifications to return (default: 20)")
    notifications_sub = p_notifications.add_subparsers(dest="notifications_cmd")

    p_notifications_reply = notifications_sub.add_parser("reply", parents=[common], help="Reply to the comment referenced by a visible notification")
    p_notifications_reply.add_argument("--index", type=int, required=True, help="1-based notification index from the visible notifications list")
    p_notifications_reply.add_argument("--text", required=True, help="Reply body text")

    p_notifications_react = notifications_sub.add_parser("react", parents=[common], help="React to the post or comment referenced by a visible notification")
    p_notifications_react.add_argument("--index", type=int, required=True, help="1-based notification index from the visible notifications list")
    p_notifications_react.add_argument("--reaction", default="like", choices=REACTION_CHOICES, help="Reaction type (default: like)")

    page_cmd = sub.add_parser("page", help="Manage a LinkedIn company page as an admin")
    page_sub = page_cmd.add_subparsers(dest="page_cmd", required=True)
    company_help = "LinkedIn company numeric id, e.g. 112454418"

    page_sub.add_parser("list", parents=[common], help="List company pages this session can administer")

    p_page_posts = page_sub.add_parser("posts", parents=[common], help="List company page admin posts")
    p_page_posts.add_argument("company", help=company_help)
    p_page_posts.add_argument("--limit", type=int, default=10, help="Maximum visible posts to return (default: 10)")

    p_page_post = page_sub.add_parser("post", parents=[common], help="Show one company page post by activity id or URL")
    p_page_post.add_argument("company", help=company_help)
    p_page_post.add_argument("post", help=posts_handle_help)

    p_page_post_create = page_sub.add_parser("post-create", parents=[common], help="Create a text post as a company page admin")
    p_page_post_create.add_argument("company", help=company_help)
    p_page_post_create.add_argument("--text", required=True, help="Post body text")
    p_page_post_create.add_argument("--image", action="append", default=[], help="Image file to attach (repeatable)")
    p_page_post_create.add_argument("--document", action="append", default=[], help="Document file to attach (repeatable)")
    p_page_post_create.add_argument("--poll-question", help="Poll question; defaults to the post text when LinkedIn requires one")
    p_page_post_create.add_argument("--poll-option", action="append", default=[], help="Poll option (repeat at least twice to create a poll)")

    p_page_post_edit = page_sub.add_parser("post-edit", parents=[common], help="Edit the text of an already-published company page post")
    p_page_post_edit.add_argument("company", help=company_help)
    p_page_post_edit.add_argument("post", help=posts_handle_help)
    p_page_post_edit.add_argument("--text", required=True, help="Replacement post body text")

    p_page_post_schedule = page_sub.add_parser("post-schedule", parents=[common], help="Schedule a company page text post for a specific local datetime")
    p_page_post_schedule.add_argument("company", help=company_help)
    p_page_post_schedule.add_argument("--text", required=True, help="Post body text")
    p_page_post_schedule.add_argument("--at", required=True, help="Local ISO datetime, e.g. 2026-06-10T09:30")

    p_page_post_update_schedule = page_sub.add_parser("post-update-schedule", parents=[common], help="Update the scheduled datetime and/or text for a company page post by 1-based index")
    p_page_post_update_schedule.add_argument("company", help=company_help)
    p_page_post_update_schedule.add_argument("index", type=int, help="1-based index from the scheduled page posts list")
    p_page_post_update_schedule.add_argument("--at", help="Local ISO datetime, e.g. 2026-06-10T09:30")
    p_page_post_update_schedule.add_argument("--text", help="Replacement post body text")

    p_page_post_scheduled = page_sub.add_parser("post-scheduled", parents=[common], help="List scheduled company page posts")
    p_page_post_scheduled.add_argument("company", help=company_help)

    p_page_post_cancel = page_sub.add_parser("post-cancel", parents=[common], help="Cancel a scheduled company page post by 1-based index")
    p_page_post_cancel.add_argument("company", help=company_help)
    p_page_post_cancel.add_argument("index", type=int, help="1-based index from the scheduled page posts list")

    p_page_post_delete = page_sub.add_parser("post-delete", parents=[common], help="Delete a company page post by activity id or URL")
    p_page_post_delete.add_argument("company", help=company_help)
    p_page_post_delete.add_argument("post", help=posts_handle_help)

    p_page_inbox = page_sub.add_parser("inbox", parents=[common], help="List visible company page inbox threads")
    p_page_inbox.add_argument("company", help=company_help)
    p_page_inbox.add_argument("--limit", type=int, default=20, help="Maximum visible threads to return (default: 20)")

    p_page_thread = page_sub.add_parser("thread", parents=[common], help="Show visible messages in a company page inbox thread")
    p_page_thread.add_argument("company", help=company_help)
    p_page_thread.add_argument("thread", help="Page inbox thread id or URL")
    p_page_thread.add_argument("--limit", type=int, default=50, help="Maximum visible messages to return (default: 50)")

    p_page_reply = page_sub.add_parser("reply", parents=[common], help="Reply to a company page inbox thread")
    p_page_reply.add_argument("company", help=company_help)
    p_page_reply.add_argument("thread", help="Page inbox thread id or URL")
    p_page_reply.add_argument("--text", required=True, help="Reply body text")
    p_page_reply.add_argument("--attachment", action="append", default=[], help="File to attach (repeatable)")
    return parser


def _configure_logging(*, json_mode: bool = False) -> None:
    default_level = "WARNING" if json_mode else "INFO"
    level = os.environ.get("LINKEDIN_CLI_LOG", default_level).upper()
    logging.basicConfig(level=level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _parse_args(argv: list[str]):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "thread" and not (args.handle or args.thread_id):
        parser.error("thread requires a profile handle/URL or --thread-id")
    if args.cmd == "posts" and args.posts_cmd == "update-schedule" and not (args.at or args.text):
        parser.error("posts update-schedule requires --at, --text, or both")
    if args.cmd == "page" and args.page_cmd == "post-update-schedule" and not (args.at or args.text):
        parser.error("page post-update-schedule requires --at, --text, or both")
    _configure_logging(json_mode=getattr(args, "json", False))

    if args.cmd == "session":
        return args
    if args.cmd == "jobs":
        args.verb = f"jobs-{args.jobs_cmd}"
    elif args.cmd == "posts":
        args.verb = f"posts-{args.posts_cmd}"
    elif args.cmd == "page":
        args.verb = f"page-{args.page_cmd}"
    elif args.cmd == "notifications" and args.notifications_cmd:
        args.verb = f"notifications-{args.notifications_cmd}"
    else:
        args.verb = args.cmd
    return args


def main(argv=None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = _parse_args(raw_argv)
    if args.cmd == "session":
        return _cmd_session_open(args) if args.subcmd == "open" else _cmd_session_close(args)
    return _run_verb(args, raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
