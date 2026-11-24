#!/usr/bin/env python3
"""Turn a session clone's SMBIOS serial into /etc/openinspect/bootstrap; see README.md.

The control plane sets the clone's ``smbios1`` serial to ``oi1:`` followed by
the base64 of ``{"url": ..., "token": ...}``. That is the Proxmox counterpart
of EC2 user data: readable by whoever may read the VM's configuration, never
holding the session environment itself. The EC2 launcher then redeems it
unchanged. A clone without such a serial (the template's verify clone, or any
VM booted from the template by hand) is left alone.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path

PREFIX = "oi1:"
SERIAL = Path("/sys/class/dmi/id/product_serial")
ETC = Path("/etc/openinspect")


def parse_serial(serial: str) -> tuple[str, str] | None:
    """Return (url, token) from a serial, or None when it is not an Open-Inspect bootstrap."""
    serial = serial.strip()
    if not serial.startswith(PREFIX):
        return None
    try:
        payload = json.loads(base64.b64decode(serial[len(PREFIX) :], validate=True))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    url, token = payload.get("url"), payload.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        return None
    if not url.startswith("https://") or not token or any(c.isspace() for c in url + token):
        return None
    return url, token


def render(url: str, token: str) -> str:
    return f"OPENINSPECT_BOOTSTRAP_URL={url}\nOPENINSPECT_BOOTSTRAP_TOKEN={token}\n"


def seed(serial_path: Path, etc: Path) -> str:
    """Write the bootstrap file when this boot needs one; return what happened, for the log."""
    if (etc / "session.json").exists():
        return "session environment already kept; nothing to seed"
    if (etc / "bootstrap").exists():
        return "bootstrap already present"
    try:
        serial = serial_path.read_text()
    except OSError:
        return "no SMBIOS serial; not a session clone"
    parsed = parse_serial(serial)
    if parsed is None:
        return "SMBIOS serial carries no bootstrap; not a session clone"
    etc.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = etc / ".bootstrap.seed"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render(*parsed))
    temporary.replace(etc / "bootstrap")
    return "bootstrap seeded from SMBIOS serial"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", type=Path, default=SERIAL)
    parser.add_argument("--etc", type=Path, default=ETC)
    args = parser.parse_args(argv)
    # Never echo the serial: it holds a bearer token until redeemed.
    print(f"openinspect-seed: {seed(args.serial, args.etc)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
