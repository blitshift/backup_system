#!/usr/bin/env python3
"""Dead-man's-switch failure notifier for the backup system.

Triggered by systemd `OnFailure=` when a backup unit fails. Pushes a DOWN
status to the matching Uptime Kuma push monitor so a failure is visible
immediately instead of silently.

Deliberately STDLIB-ONLY and meant to run with the SYSTEM python
(/usr/bin/python3), NOT the backup conda env. The whole point is that this
still fires when the backup env itself is broken -- which is exactly the
failure mode that left backups silently dead for ~12 days in June 2026 (an
Arch python 3.13 -> 3.14 upgrade invalidated the venv; the crash happened at
import time, before any Kuma ping could be sent).

Best effort: never raises, always exits 0, so it can't fail or loop.
"""

import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import tomllib  # stdlib >= 3.11
except ModuleNotFoundError:  # pragma: no cover - system python is always >= 3.11 here
    tomllib = None

MACHINE_CONFIG = Path("/etc/backup/machine.toml")

# systemd unit name -> key under [kuma] in machine.toml
UNIT_TO_KEY = {
    "backup.service": "backup",
    "backup-verify.service": "verify",
}


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else ""
    key = UNIT_TO_KEY.get(unit)
    if key is None or tomllib is None:
        return 0

    try:
        config = tomllib.loads(MACHINE_CONFIG.read_text())
    except (OSError, ValueError):
        return 0

    url = (config.get("kuma") or {}).get(key)
    if not url:
        return 0

    # Strip any existing query params (the configured URL is the success URL),
    # then rebuild it as a DOWN ping.
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlunparse(parsed._replace(query=""))
    query = urllib.parse.urlencode({"status": "down", "msg": f"{unit} FAILED"})
    target = f"{base}?{query}"

    try:
        urllib.request.urlopen(target, timeout=20)
    except Exception:
        pass  # best effort -- never fail the handler

    return 0


if __name__ == "__main__":
    sys.exit(main())
