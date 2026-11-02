"""Read-only cross-platform match queue and explicit confirm/reject actions."""
from __future__ import annotations

import json
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import text

from .account_links import set_link
from .aggregate_cache import cached_record
from .faces.signals import avatar_matches
from .link_matching import (
    EMAIL_PATTERN,
    PAIR_LABELS,
    PHONE_PATTERN,
    PROVIDERS,
    AccountSignals,
    candidate_keys,
    compact,
    confidence,
    field_texts,
    pair_key,
    score_pair,
)
from .link_ownership import link_blockers
from .profile_facts import GROUP_NAME_SEPARATOR
from .schemas import PlatformLinkBatch, PlatformLinkDecision

IDENTITY_FIELDS = (
    "wechat.alias",
    "wechat.remark",
    "wechat.nickname",
    "wechat.group_nicknames",
    "wechat.historical_nicknames",
    "instagram.username",
    "instagram.full_name",
    "linkedin.name",
    "linkedin.headline",
    "linkedin.profile_url",
    "email",
    "phone",
    "mobile",
)
# Identity fields shown next to an account so a person can judge a suggestion.
SIGNAL_LABELS = {
    "wechat.remark": "备注",
    "wechat.alias": "微信号",
    "wechat.group_nicknames": "群内名",
    "wechat.nickname": "昵称",
    "wechat.historical_nicknames": "曾用名",
    "instagram.full_name": "Instagram 全名",
    "linkedin.name": "LinkedIn 姓名",
    "linkedin.headline": "LinkedIn 简介",
    "email": "邮箱",
    "phone": "电话",
    "mobile": "电话",
}
SIGNAL_VALUES = 4
LINK_KIND_LABELS = {
    "existing-exact-identity": "旧 Monica 匹配",
    "platform-link-review": "账号关联页确认",
    "manual-confirmation": "手动关联",
    "user-confirmed-account": "手动确认",
    "user-confirmed-profile-link": "手动确认",
    "user-requested-profile-creation": "导入时新建",
    "user-requested-separate-instagram-profile": "导入时新建",
    "user-requested-separate-linkedin-profile": "导入时新建",
}
MAX_KEY_ACCOUNTS = 200
# Background imports advance "media" every few seconds, longer than the queue takes to
# compute, so a queue keyed on it would never be stored. Blocked-account flags may lag
# media and tag changes; set_link re-checks ownership live before any link is saved.
QUEUE_DOMAINS = ("links", "profiles")
PROVIDER_LABELS = {"wechat": "微信", "instagram": "Instagram", "linkedin": "LinkedIn"}


def _texts(value) -> list[str]:
    return [item.strip() for item in field_texts(value) if item and str(item).strip()]


def _group_entries(value) -> list[tuple[str, str]]:
    """(display name, group name) pairs from "group｜name" lines."""
    entries = []
    for line in "\n".join(_texts(value)).splitlines():
        parts = GROUP_NAME_SEPARATOR.split(line.strip(), maxsplit=1)
        if parts[-1].strip():
            entries.append((parts[-1].strip(), parts[0].strip() if len(parts) == 2 else ""))
    return entries


def profile_signals(db, profile_ids=None) -> dict[str, list[dict]]:
    """Labelled identity values per Profile, e.g. remark, WeChat ID and group display names."""
    found: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in db.execute(
        text("""
          SELECT c.profile_id, c.field_key, co.payload_json AS value, p.display_name
          FROM profile_field_current c
          JOIN profiles p ON p.id=c.profile_id
          JOIN profile_field_revisions r ON r.id=c.current_revision_id
          JOIN content_objects co ON co.id=r.content_object_id
          WHERE c.field_key = ANY(:keys)
            AND (CAST(:ids AS uuid[]) IS NULL OR c.profile_id = ANY(CAST(:ids AS uuid[])))
        """),
        {"keys": list(SIGNAL_LABELS), "ids": None if profile_ids is None else list(profile_ids)},
    ).mappings():
        if row["field_key"] == "wechat.group_nicknames":
            values = [f"{name}（{group}）" if group else name for name, group in _group_entries(row["value"])]
        else:
            values = _texts(row["value"])
        shown = found[str(row["profile_id"])][SIGNAL_LABELS[row["field_key"]]]
        name = (row["display_name"] or "").casefold()
        for value in values:
            if value.casefold() != name and value not in shown:
                shown.append(value)
    order = list(dict.fromkeys(SIGNAL_LABELS.values()))
    return {
        profile_id: [
            {"label": label, "values": labels[label][:SIGNAL_VALUES], "more": max(0, len(labels[label]) - SIGNAL_VALUES)}
            for label in order if labels.get(label)
        ]
        for profile_id, labels in found.items()
    }


def _load(db) -> tuple[list[AccountSignals], dict[str, set[str]], dict[tuple[str, str], str], dict[str, str]]:
    blockers = link_blockers(db)
    accounts = [
        AccountSignals(
            account_id=str(row["id"]),
            provider=row["provider"],
            external_id=row["external_id"],
            display_name=row["display_name"] or "",
            username=row["username"] or "",
            profile_url=row["profile_url"] or "",
            profile_id=str(row["profile_id"]) if row["profile_id"] else None,
            profile_name=row["profile_name"] or "",
            ready=str(row["id"]) not in blockers,
            avatar_media_id=str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
        )
        for row in db.execute(
            text("""
              SELECT a.id,a.provider,a.external_id,a.display_name,a.username,a.profile_url,
                     l.profile_id,p.display_name AS profile_name,
                     COALESCE(av.media_id, p.avatar_media_id) AS avatar_media_id
              FROM source_accounts a
              LEFT JOIN source_account_links l ON l.account_id=a.id
              LEFT JOIN profiles p ON p.id=l.profile_id
              LEFT JOIN (
                SELECT DISTINCT ON (account_id) account_id, media_id FROM source_account_media
                WHERE role IN ('wechat_avatar_small','wechat_avatar','instagram_avatar','linkedin_avatar')
                ORDER BY account_id, role='wechat_avatar_small' DESC
              ) av ON av.account_id=a.id
              WHERE a.object_kind='person'
                AND (p.id IS NULL OR (p.profile_type='person' AND p.archived_at IS NULL))
            """)
        ).mappings()
    ]
    by_profile: dict[str, list[AccountSignals]] = defaultdict(list)
    for account in accounts:
        if account.profile_id:
            by_profile[account.profile_id].append(account)
    for row in db.execute(
        text("""
          SELECT l.profile_id,c.field_key,co.payload_json AS value
          FROM source_account_links l
          JOIN profile_field_current c ON c.profile_id=l.profile_id
          JOIN profile_field_revisions r ON r.id=c.current_revision_id
          JOIN content_objects co ON co.id=r.content_object_id
          WHERE c.field_key = ANY(:keys)
        """),
        {"keys": list(IDENTITY_FIELDS)},
    ).mappings():
        if row["field_key"] == "wechat.group_nicknames":
            texts = [name for name, _group in _group_entries(row["value"])]
        else:
            texts = _texts(row["value"])
        for account in by_profile.get(str(row["profile_id"]), []):
            account.aliases.extend(texts)
            if row["field_key"].endswith("username") or row["field_key"].endswith("alias") or row["field_key"].endswith("profile_url"):
                account.handles.update(item.lower() for item in texts)
            if "email" in row["field_key"]:
                account.emails.update(item.lower() for item in texts)
            if "phone" in row["field_key"] or row["field_key"] == "mobile":
                account.phones.update(item for item in texts)
            for item in texts:
                account.emails.update(email.lower() for email in EMAIL_PATTERN.findall(item))
                account.phones.update(PHONE_PATTERN.findall(item))
    for account in accounts:
        account.handles.add((account.username or account.external_id or "").lower())
        account.aliases = list(dict.fromkeys(account.aliases))
        for item in account.identity_values():
            account.emails.update(email.lower() for email in EMAIL_PATTERN.findall(item))
            account.phones.update(PHONE_PATTERN.findall(item))
    providers = {profile: {item.provider for item in items} for profile, items in by_profile.items()}
    decisions = {
        (str(row["source_account_id"]), str(row["target_profile_id"])): row["status"]
        for row in db.execute(text("SELECT source_account_id,target_profile_id,status FROM platform_link_decisions")).mappings()
    }
    return accounts, providers, decisions, blockers


def linked_people(db, *, query: str = "", providers: str = ""):
    """Every person Profile that carries accounts from two or more platforms."""
    required = {item for item in providers.split(",") if item in PROVIDERS}
    needle = compact(query)
    people = []
    for row in db.execute(
        text("""
          SELECT p.id, p.display_name,
                 COALESCE((
                   SELECT small.media_id FROM source_account_media big
                   JOIN source_account_media small ON small.account_id=big.account_id AND small.role='wechat_avatar_small'
                   WHERE big.media_id=p.avatar_media_id AND big.role='wechat_avatar' LIMIT 1
                 ), p.avatar_media_id) AS avatar_media_id,
                 json_agg(json_build_object(
                   'account_id', a.id,
                   'provider', a.provider,
                   'display_name', a.display_name,
                   'username', COALESCE(a.username, a.external_id),
                   'profile_url', a.profile_url,
                   'avatar_media_id', (
                     SELECT m.media_id FROM source_account_media m
                     WHERE m.account_id=a.id
                       AND m.role IN ('wechat_avatar_small','wechat_avatar','instagram_avatar','linkedin_avatar')
                     ORDER BY m.role='wechat_avatar_small' DESC LIMIT 1
                   ),
                   'link_kind', l.evidence->>'kind',
                   'link_reasons', l.evidence->'reasons'
                 ) ORDER BY a.provider, a.display_name) AS accounts
          FROM profiles p
          JOIN source_account_links l ON l.profile_id=p.id
          JOIN source_accounts a ON a.id=l.account_id
          WHERE p.profile_type='person' AND p.archived_at IS NULL AND a.object_kind='person'
          GROUP BY p.id
          HAVING count(DISTINCT a.provider) >= 2
          ORDER BY p.display_name
        """)
    ).mappings():
        accounts = row["accounts"]
        present = sorted({item["provider"] for item in accounts})
        if not required <= set(present):
            continue
        for item in accounts:
            item["link_label"] = LINK_KIND_LABELS.get(item.pop("link_kind") or "", "")
        people.append({
            "profile_id": str(row["id"]),
            "display_name": row["display_name"],
            "avatar_media_id": str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
            "providers": present,
            "accounts": accounts,
        })
    signals = profile_signals(db, [person["profile_id"] for person in people]) if people else {}
    for person in people:
        person["signals"] = signals.get(person["profile_id"], [])
    if needle:
        people = [person for person in people if needle in compact(" ".join([
            person["display_name"] or "",
            *[f"{item['display_name'] or ''} {item['username'] or ''}" for item in person["accounts"]],
            *[value for signal in person["signals"] for value in signal["values"]],
        ]))]
    return people


def _queue(db) -> list[dict]:
    """Every account with candidate Profiles on another platform, in any review status."""
    accounts, providers, decisions, blockers = _load(db)
    keys = {account.account_id: candidate_keys(account) for account in accounts}
    # Score only pairs that share an index key; a key held by very many accounts
    # (a bare surname, a school) cannot tell people apart and is not used.
    index: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_profile: dict[str, list[int]] = defaultdict(list)
    for position, account in enumerate(accounts):
        if account.profile_id:
            by_profile[account.profile_id].append(position)
            for key in keys[account.account_id]:
                index[(account.provider, key)].append(position)
    decided: dict[str, set[str]] = defaultdict(set)
    rejected_accounts = set()
    for (account_id, profile_id), status in decisions.items():
        decided[account_id].add(profile_id)
        if status == "rejected":
            rejected_accounts.add(account_id)

    queue = []
    claimed: dict[str, list[str]] = defaultdict(list)
    moves: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source in accounts:
        already = providers.get(source.profile_id, set()) if source.profile_id else set()
        missing = [provider for provider in PROVIDERS if provider != source.provider and provider not in already]
        if not missing:
            continue
        nearby = {
            position
            for provider in missing
            for key in keys[source.account_id]
            if len(bucket := index.get((provider, key), ())) <= MAX_KEY_ACCOUNTS
            for position in bucket
        }
        nearby.update(
            position
            for profile_id in decided.get(source.account_id, ())
            for position in by_profile.get(profile_id, ())
            if accounts[position].provider in missing
        )
        seen: set[str] = set()
        candidates = []
        for position in sorted(nearby):
            other = accounts[position]
            if other.account_id == source.account_id or not other.profile_id:
                continue
            if other.profile_id == source.profile_id or other.profile_id in seen:
                continue
            if source.provider in providers.get(other.profile_id, set()):
                continue
            seen.add(other.profile_id)
            status = decisions.get((source.account_id, other.profile_id))
            scored = score_pair(source, other)
            if scored.score < 55 and status != "accepted":
                continue
            pair_name = pair_key(source.provider, providers.get(other.profile_id, set()))
            candidates.append({
                "profile_id": other.profile_id,
                "display_name": other.profile_name or other.display_name,
                "providers": sorted(providers.get(other.profile_id, set())),
                "username": other.username or other.external_id,
                "score": scored.score,
                "confidence": confidence(scored.score),
                "reasons": scored.reasons,
                "pair": pair_name,
                "pair_label": PAIR_LABELS.get(pair_name, pair_name),
                "ready": source.ready,
                "status": status or "pending",
                "expected_profile_id": source.profile_id,
            })
        candidates.sort(key=lambda item: (-item["score"], item["display_name"]))
        selected = (
            next((item for item in candidates if item["status"] == "accepted"), None)
            or next((item for item in candidates if item["status"] == "pending"), None)
            or next((item for item in candidates if item["status"] == "skipped"), None)
            or (candidates[0] if candidates else None)
        )
        if selected and selected["status"] != "rejected":
            claimed[selected["profile_id"]].append(source.account_id)
        rejected = source.account_id in rejected_accounts
        if selected and selected["status"] == "accepted":
            decision_status = "matched"
        elif (selected and selected["status"] == "rejected") or (not candidates and rejected):
            decision_status = "no_match"
        elif selected and selected["status"] == "skipped":
            decision_status = "skipped"
        else:
            decision_status = "unresolved"
        if not candidates and decision_status == "unresolved":
            continue
        row_pair = selected["pair"] if selected else pair_key(source.provider, missing)
        if selected and decision_status == "unresolved" and source.profile_id:
            moves[(source.profile_id, selected["profile_id"])].append(source.account_id)
        queue.append({
            "account_id": source.account_id,
            "provider": source.provider,
            "provider_label": PROVIDER_LABELS[source.provider],
            "display_name": source.display_name,
            "username": source.username or source.external_id,
            "profile_id": source.profile_id,
            "profile_name": source.profile_name,
            "ready": source.ready,
            "blocked_reason": blockers.get(source.account_id),
            "avatar_media_id": source.avatar_media_id,
            "pair": row_pair,
            "pair_label": PAIR_LABELS.get(row_pair, row_pair),
            "score": selected["score"] if selected else 0,
            "confidence": selected["confidence"] if selected else "weak",
            "status": decision_status,
            "candidates": candidates[:8],
        })

    # Two profiles suggested for each other would swap accounts if both rows were
    # confirmed. Offer only the move onto the profile that keeps WeChat or more accounts.
    def keeps(profile_id: str):
        present = providers.get(profile_id, set())
        return ("wechat" in present, len(present), profile_id)

    reverse_rows = {
        account_id
        for (origin, target), account_ids in moves.items()
        if (target, origin) in moves and keeps(origin) > keeps(target)
        for account_id in account_ids
    }
    queue = [row for row in queue if row["account_id"] not in reverse_rows]

    signals = profile_signals(db)
    for row in queue:
        top = row["candidates"][0]["profile_id"] if row["candidates"] else None
        row["conflict"] = bool(top and len({account_id for account_id in claimed.get(top, []) if account_id != row["account_id"]}))
        row["signals"] = signals.get(row["profile_id"] or "", [])
        for candidate in row["candidates"]:
            candidate["signals"] = signals.get(candidate["profile_id"], [])
    return queue


def review(db, *, pair: str = "", status_filter: str = "unresolved", query: str = "", sort: str = "confidence",
           page: int = 1, page_size: int = 50):
    """One page of the cached queue; filtering and paging never recompute candidates."""
    queue = cached_record(db, "link-review-queue", QUEUE_DOMAINS, [], _queue)["value"]
    needle = compact(query)
    filtered = []
    for row in queue:
        if pair and row["pair"] != pair and not row["pair"].startswith(pair):
            continue
        if status_filter == "unresolved" and row["status"] != "unresolved":
            continue
        if status_filter == "recommended" and not (row["status"] == "unresolved" and row["score"] >= 70):
            continue
        if status_filter in {"matched", "no_match", "skipped"} and row["status"] != status_filter:
            continue
        haystack = compact(" ".join([
            row["display_name"], row["username"], row["profile_name"],
            *[item["display_name"] for item in row["candidates"]],
            *[value for signal in row["signals"] for value in signal["values"]],
        ]))
        if needle and needle not in haystack:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: (-row["score"], row["username"]) if sort == "confidence" else (row["username"], -row["score"]))
    page = max(page, 1)
    start = (page - 1) * page_size
    return {
        "pairs": [{"id": key, "label": label} for key, label in PAIR_LABELS.items()],
        "total": len(filtered),
        "unresolved": sum(1 for row in queue if row["status"] == "unresolved"),
        "page": page,
        "page_size": page_size,
        "queue": _with_faces(db, filtered[start:start + page_size]),
    }


def _with_faces(db, rows: list[dict]) -> list[dict]:
    """Face similarity changes nightly and is cheap per page, so it is added outside the cached queue."""
    matches = avatar_matches(db, {row["avatar_media_id"] for row in rows if row["avatar_media_id"]})
    return [{**row, "candidates": [
        {**candidate, "face_score": matches.get((row["avatar_media_id"], candidate["profile_id"]))}
        for candidate in row["candidates"]]} for row in rows]


def _record(db, payload: PlatformLinkDecision, user_id, status: str, pair: str, score: int | None, evidence: dict):
    db.execute(
        text("""
          INSERT INTO platform_link_decisions(source_account_id,target_profile_id,status,score,pair,evidence,reviewed_by)
          VALUES(:account,:profile,:status,:score,:pair,CAST(:evidence AS jsonb),:user)
          ON CONFLICT (source_account_id,target_profile_id)
          DO UPDATE SET status=EXCLUDED.status,score=EXCLUDED.score,pair=EXCLUDED.pair,
                        evidence=EXCLUDED.evidence,reviewed_by=EXCLUDED.reviewed_by,reviewed_at=now()
        """),
        {
            "account": payload.account_id,
            "profile": payload.profile_id,
            "status": status,
            "score": score,
            "pair": pair,
            "evidence": json.dumps(evidence, ensure_ascii=False),
            "user": user_id,
        },
    )


def confirm(db, payload: PlatformLinkDecision, user):
    account = db.execute(
        text("""
          SELECT a.id,a.provider,l.profile_id
          FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id
          WHERE a.id=:id
        """),
        {"id": payload.account_id},
    ).mappings().one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Source account not found")
    target_providers = {
        row[0]
        for row in db.execute(
            text("""
              SELECT a.provider FROM source_account_links l
              JOIN source_accounts a ON a.id=l.account_id WHERE l.profile_id=:id
            """),
            {"id": payload.profile_id},
        )
    }
    if account["provider"] in target_providers:
        raise HTTPException(status_code=409, detail="Target profile already has this provider")
    pair = pair_key(account["provider"], target_providers)
    result = set_link(
        db,
        payload.account_id,
        payload.profile_id,
        actor=user["id"],
        expected_profile_id=payload.expected_profile_id,
        evidence={"kind": "platform-link-review", "pair": pair, "reasons": payload.reasons},
    )
    _record(db, payload, user["id"], "accepted", pair, payload.score, {"reasons": payload.reasons})
    return {**result, "pair": pair}


def decide(db, payload: PlatformLinkDecision, user, status: str):
    account = db.execute(
        text("SELECT provider FROM source_accounts WHERE id=:id"), {"id": payload.account_id}
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Source account not found")
    _record(db, payload, user["id"], status, f"{account}-review", payload.score, {"reasons": payload.reasons})
    return {"status": status}


def apply_batch(db, payload: PlatformLinkBatch, user):
    """Apply staged decisions; each item commits or rolls back on its own savepoint."""
    results = []
    for item in payload.items:
        savepoint = db.begin_nested()
        try:
            if item.action == "confirm":
                confirm(db, item, user)
            else:
                decide(db, item, user, "rejected" if item.action == "reject" else "skipped")
        except HTTPException as failure:
            savepoint.rollback()
            results.append({"account_id": str(item.account_id), "ok": False, "error": failure.detail})
            continue
        savepoint.commit()
        results.append({"account_id": str(item.account_id), "ok": True, "error": None})
    return {"results": results}


def search_contacts(db, *, query: str, provider: str = ""):
    needle = query.strip()
    if not needle:
        return []
    rows = []
    for row in db.execute(
        text("""
          SELECT p.id AS profile_id, p.display_name, p.avatar_media_id,
                 json_agg(json_build_object(
                   'provider', a.provider,
                   'display_name', a.display_name,
                   'username', COALESCE(a.username, a.external_id)
                 ) ORDER BY a.provider)
          FROM profiles p
          JOIN source_account_links l ON l.profile_id=p.id
          JOIN source_accounts a ON a.id=l.account_id
          WHERE p.profile_type='person' AND p.archived_at IS NULL
            AND (p.display_name ILIKE :query OR a.display_name ILIKE :query
                 OR COALESCE(a.username,'') ILIKE :query OR a.external_id ILIKE :query)
            AND (:provider='' OR NOT EXISTS (
              SELECT 1 FROM source_account_links x
              JOIN source_accounts s ON s.id=x.account_id
              WHERE x.profile_id=p.id AND s.provider=:provider
            ))
          GROUP BY p.id
          ORDER BY p.display_name
          LIMIT 30
        """),
        {"query": f"%{needle}%", "provider": provider or ""},
    ):
        rows.append({
            "profile_id": str(row[0]),
            "display_name": row[1],
            "avatar_media_id": str(row[2]) if row[2] else None,
            "accounts": row[3] or [],
        })
    return rows
