"""Select only explicit public profiles from the existing collector inventory."""
import json
from pathlib import Path


def select_public_accounts(followees: Path, profiles: Path, login: str):
    selected = []
    seen = set()
    counts = {"public": 0, "private": 0, "unknown": 0}
    for line in followees.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        userid = int(row["userid"])
        if userid in seen or row["username"] == login:
            continue
        seen.add(userid)
        username = row["username"]
        if Path(username).name != username:
            raise ValueError("Invalid account username")
        path = profiles / username / "profile.json"
        doc = json.loads(path.read_text()) if path.exists() else {}
        doc = doc.get("node", doc)
        identity = doc.get("userid", doc.get("id", userid))
        privacy = doc.get("is_private", row.get("is_private"))
        if int(identity) != userid:
            privacy = None
        category = "public" if privacy is False else "private" if privacy is True else "unknown"
        counts[category] += 1
        if privacy is False:
            selected.append({**row, "is_private": False})
    return selected, counts


def write_public_accounts(followees: Path, profiles: Path, output: Path, login: str):
    rows, counts = select_public_accounts(followees, profiles, login)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    temporary.replace(output)
    return counts
