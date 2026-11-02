"""Submit a prepared Import API batch from a collector host.

The collector never needs database credentials. It signs the exact JSON bytes
that are sent over Tailscale/HTTPS and can safely retry with the same
idempotency key after a network interruption.
"""

import argparse
import hashlib
import hmac
import json
import secrets
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch", type=Path)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--token")
    args = parser.parse_args()
    body = json.dumps(
        json.loads(args.batch.read_text(encoding="utf-8")),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    message = b".".join([timestamp.encode(), nonce.encode(), body])
    signature = hmac.new(args.secret.encode(), message, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Import-Timestamp": timestamp,
        "X-Import-Nonce": nonce,
        "X-Import-Signature": signature,
    }
    if args.token:
        headers["X-Import-Token"] = args.token
    request = urllib.request.Request(
        f"{args.api.rstrip('/')}/api/v1/imports/batches",
        data=body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
