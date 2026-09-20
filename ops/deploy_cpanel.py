"""Publish the built panel through cPanel. Set CPANEL_TOKEN_FILE privately."""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
HOST = os.environ.get("CPANEL_HOST", "https://your-cpanel-host:2083")
USER = os.environ.get("CPANEL_USER", "your-cpanel-user")
REMOTE = os.environ.get("CPANEL_PANEL_ROOT", f"/home/{USER}/public_html/panel")


def upload(path: Path, directory: str, token: str) -> None:
    with path.open("rb") as source:
        response = requests.post(
            HOST + "/execute/Fileman/upload_files",
            headers={"Authorization": f"cpanel {USER}:{token}"},
            data={"dir": directory, "overwrite": "1"},
            files={"file-1": (path.name, source, mimetypes.guess_type(path.name)[0] or "application/octet-stream")},
            timeout=120,
        )
    response.raise_for_status()
    result = response.json()
    status = result.get("status", result.get("result", {}).get("status"))
    errors = result.get("errors") or result.get("result", {}).get("errors")
    if status in (0, False) or errors:
        raise RuntimeError(f"cPanel rejected {path.name}: {errors or 'status=0'}")
    print("uploaded", path.relative_to(PUBLIC))


def main() -> None:
    token_file = os.environ.get("CPANEL_TOKEN_FILE")
    if not token_file:
        raise SystemExit("CPANEL_TOKEN_FILE must point to the private API token")
    token = Path(token_file).read_text().strip()
    if not token:
        raise SystemExit("cPanel API token file is empty")
    index = PUBLIC / "index.html"
    if not index.is_file():
        raise SystemExit("Build frontend first: cd frontend && npm ci && npm run build")
    assets = sorted(set(re.findall(r"/assets/[A-Za-z0-9_.-]+", index.read_text())))
    if not assets:
        raise SystemExit("index.html has no built asset references")
    for asset in assets:
        path = PUBLIC / asset.lstrip("/")
        if not path.is_file():
            raise SystemExit(f"Missing built asset: {path}")
        upload(path, REMOTE + "/assets", token)
    for path in (PUBLIC / ".htaccess", PUBLIC / "api" / ".htaccess", PUBLIC / "api" / "index.php", PUBLIC / "api" / "platform.php"):
        if path.is_file():
            upload(path, REMOTE + ("/api" if path.parent.name == "api" else ""), token)
    upload(index, REMOTE, token)


if __name__ == "__main__":
    main()
