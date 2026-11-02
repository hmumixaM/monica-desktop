"""Read Monica targets before collection, using the existing queue and budgets."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Target
from .storage import Store

MAX_FEED_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Collector target feed must not redirect")


def slug_of(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
        or not re.fullmatch(r"(?:(?:www|[a-z]{2,3})\.)?linkedin\.com", host)
        or len(parts) != 2
        or parts[0] != "in"
    ):
        raise ValueError("A LinkedIn personal profile URL is required")
    slug = unquote(parts[1]).casefold()
    if not re.fullmatch(r"[\w.~\-]{1,255}", slug) or not any(c.isalnum() for c in slug):
        raise ValueError("Invalid LinkedIn account")
    return slug


def fetch_targets(url, token_file):
    if urlsplit(url).scheme != "https":
        raise ValueError("The collector target feed requires HTTPS")
    token = token_file.read_text().strip()
    if not token:
        raise ValueError("Collector target token is empty")
    request = Request(url, headers={"X-LinkedIn-Targets-Token": token})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(MAX_FEED_BYTES + 1)
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("Collector target feed is too large")
    return json.loads(raw)


def apply_targets(store, document):
    # Fully validate before changing any queue state. A failed download/invalid
    # response must never be mistaken for an empty (stop-all) target list.
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("targets"), list)
    ):
        raise ValueError("Unsupported collector target feed")
    targets = {}
    for value in document["targets"]:
        target = Target.model_validate(value)
        slug = slug_of(target.url)
        if (
            target.external_id != slug
            or target.metadata.get("provider_user_id") != slug
        ):
            raise ValueError("Collector target identifiers disagree")
        targets[slug] = target
    with store.connect() as connection:
        existing = connection.execute(
            "SELECT * FROM targets ORDER BY active DESC,id"
        ).fetchall()
    by_slug = {}
    for row in existing:
        try:
            by_slug.setdefault(slug_of(row["profile_url"]), row)
        except ValueError:
            continue
    queued = []
    for slug in targets:
        previous = by_slug.get(slug)
        # Reuse an existing URL spelling so connections and manually added
        # regional/trailing-slash URLs share the same job and snapshot history.
        url = (
            previous["profile_url"]
            if previous
            else "https://www.linkedin.com/in/" + quote(slug, safe=".~-")
        )
        queued.append(
            Target(
                profile_url=url,
                external_id=previous["external_id"] if previous else slug,
                metadata={"provider_user_id": slug, "crm_manual": True},
            )
        )
    store.enqueue(queued, reconcile_metadata_key="crm_manual")
    return len(queued)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    document = fetch_targets(config["url"], Path(config["token_file"]))
    store = Store(args.db)
    store.initialize()
    print(json.dumps({"crm_manual_targets": apply_targets(store, document)}))


if __name__ == "__main__":
    main()
