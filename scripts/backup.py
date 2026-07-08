#!/usr/bin/env python3
"""Dump the production database and upload it to Google Drive.

Runs as a Railway cron service on the private network (DATABASE_URL is the
internal reference — no public exposure). Two phases, each fails loudly:
dump (pg_dump subprocess) then upload (Drive API via a dedicated service
account). Retention pruning only runs after a successful upload, so a run
of failures never deletes existing good backups.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
RETAIN = 14


def _dump(db_url: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dump_path = Path(f"/tmp/aquinas_backup_{stamp}.dump")
    result = subprocess.run(
        ["pg_dump", "-Fc", db_url, "-f", str(dump_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"backup: FAILED on pg_dump: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"backup: dumped {dump_path.name} ({dump_path.stat().st_size} bytes)")
    return dump_path


def _drive_credentials() -> Credentials:
    sa_json = os.environ.get("GDRIVE_SA_JSON")
    if not sa_json:
        print("ERROR: GDRIVE_SA_JSON not set", file=sys.stderr)
        sys.exit(1)
    creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=DRIVE_SCOPES)
    creds.refresh(Request())
    return creds


def _upload(dump_path: Path, creds: Credentials, folder_id: str) -> None:
    metadata = {"name": dump_path.name, "parents": [folder_id]}
    files = {
        "metadata": ("metadata", json.dumps(metadata), "application/json"),
        "file": (dump_path.name, dump_path.read_bytes(), "application/octet-stream"),
    }
    resp = requests.post(
        DRIVE_UPLOAD_URL,
        headers={"Authorization": f"Bearer {creds.token}"},
        files=files,
    )
    if resp.status_code >= 300:
        print(f"backup: FAILED on upload: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)
    print(f"backup: uploaded {dump_path.name}")


def _prune(creds: Credentials, folder_id: str) -> None:
    resp = requests.get(
        DRIVE_FILES_URL,
        headers={"Authorization": f"Bearer {creds.token}"},
        params={
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "files(id,name,createdTime)",
            "orderBy": "createdTime desc",
            "pageSize": 1000,
        },
    )
    if resp.status_code >= 300:
        print(f"backup: prune FAILED to list files: {resp.status_code} {resp.text}", file=sys.stderr)
        return

    files = resp.json().get("files", [])
    for stale in files[RETAIN:]:
        del_resp = requests.delete(
            f"{DRIVE_FILES_URL}/{stale['id']}",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        if del_resp.status_code >= 300:
            print(f"backup: prune FAILED to delete {stale['name']}: {del_resp.status_code}", file=sys.stderr)
        else:
            print(f"backup: pruned {stale['name']}")


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    folder_id = os.environ.get("DRIVE_BACKUP_FOLDER_ID")
    if not folder_id:
        print("ERROR: DRIVE_BACKUP_FOLDER_ID not set", file=sys.stderr)
        sys.exit(1)

    dump_path = _dump(db_url)
    creds = _drive_credentials()
    _upload(dump_path, creds, folder_id)
    dump_path.unlink()

    _prune(creds, folder_id)
    print("backup: done")


if __name__ == "__main__":
    main()
