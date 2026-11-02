"""Avatar extraction from the target's existing Voyager response; no extra request."""

from urllib.parse import urlsplit


def _vectors(value):
    if isinstance(value, dict):
        if value.get("rootUrl") and isinstance(value.get("artifacts"), list):
            for artifact in value["artifacts"]:
                suffix = artifact.get("fileIdentifyingUrlPathSegment")
                if suffix:
                    yield (int(artifact.get("width") or 0), value["rootUrl"] + suffix)
        for child in value.values():
            yield from _vectors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _vectors(child)


def profile_avatar(raw, public_identifier):
    for entity in raw.get("included", []):
        if str(entity.get("publicIdentifier", "")).lower() != public_identifier.lower():
            continue
        # Only the requested person's portrait, never a background or another person's photo.
        for key in ("profilePicture", "picture", "displayImageReference"):
            candidates = list(_vectors(entity.get(key)))
            for _, url in sorted(candidates, reverse=True):
                host = (urlsplit(url).hostname or "").lower()
                if url.startswith("https://") and host.endswith(".licdn.com"):
                    return url
    return None
