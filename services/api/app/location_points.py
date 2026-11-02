"""Source location payloads as labelled map points and per-place summaries."""



def _location_label(location: object) -> str:
    if not isinstance(location, dict):
        return "未命名地点"
    for key in ("name", "poiName", "poi_name", "poi", "address", "city", "province", "country"):
        value = location.get(key)
        if value not in (None, "", "0"):
            return str(value)
    lat = location.get("latitude") or location.get("lat")
    lon = location.get("longitude") or location.get("lon") or location.get("lng")
    return f"{lat}, {lon}" if lat not in (None, "0", 0) and lon not in (None, "0", 0) else "未命名地点"


def _location_point(location: object, coordinate_format: str | None = None) -> tuple[float, float] | None:
    if not isinstance(location, dict):
        return None
    try:
        lat = float(location.get("latitude", location.get("lat")))
        lon = float(location.get("longitude", location.get("lon", location.get("lng"))))
    except (TypeError, ValueError):
        return None
    # Chatlog SNS XML labels its longitude slot "latitude" and its latitude
    # slot "longitude". Both values can fit latitude bounds in Europe/Africa.
    # Reverse only this explicit raw source format, preserving original data.
    if coordinate_format == 'chatlog_sns_xml':
        lat, lon = lon, lat
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _build_location_summary(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, float, float], dict[str, object]] = {}
    for row in rows:
        location = row.get("location")
        point = _location_point(location, row.get("coordinate_format"))
        if not point:
            continue
        lat, lon = point
        label = _location_label(location)
        key = (label, round(lat, 5), round(lon, 5))
        entry = grouped.setdefault(
            key,
            {
                "label": label,
                "latitude": lat,
                "longitude": lon,
                "count": 0,
                "first_seen": row.get("occurred_at"),
                "last_seen": row.get("occurred_at"),
                "sources": set(),
                "events": [],
            },
        )
        entry["count"] = int(entry["count"]) + int(row.get("count", 1))
        if row.get("source_kind"):
            entry["source_kind"] = row["source_kind"]
        if row.get("occurred_at") and (
            not entry.get("last_seen") or row["occurred_at"] > entry["last_seen"]
        ):
            entry["last_seen"] = row["occurred_at"]
        if row.get("occurred_at") and (
            not entry.get("first_seen") or row["occurred_at"] < entry["first_seen"]
        ):
            entry["first_seen"] = row["occurred_at"]
        if row.get("events"):
            entry["events"].extend(row["events"])
        if row.get("first_seen"):
            entry["first_seen"] = row["first_seen"]
        if row.get("last_seen"):
            entry["last_seen"] = row["last_seen"]
        if row.get('occurred_at'):
            entry['events'].append({'at':row['occurred_at'].isoformat(), 'text':str(row.get('summary') or '')[:120],
                'source':str(row.get('provider') or 'wechat'),
                'url':f"/profiles/{row['profile_id']}?tab=timeline&event={row['id']}" if row.get('id') and row.get('profile_id') else None})
        sources = entry["sources"]
        if isinstance(sources, set):
            sources.add(str(row.get("provider") or "wechat"))
    result = []
    for entry in grouped.values():
        entry["sources"] = sorted(entry["sources"])
        entry["map_url"] = (
            f"https://www.openstreetmap.org/?mlat={entry['latitude']}&mlon={entry['longitude']}#map=14/{entry['latitude']}/{entry['longitude']}"
        )
        result.append(entry)
    return sorted(result, key=lambda item: (-int(item["count"]), str(item["label"])))
