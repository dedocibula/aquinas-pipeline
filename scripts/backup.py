#!/usr/bin/env python3
"""Dump the production database and upload it to S3.

Runs as a Railway cron service on the private network (DATABASE_URL is the
internal reference — no public exposure). Three phases, each fails loudly:
dump (pg_dump subprocess), upload (S3 via a dedicated IAM user scoped to one
bucket), then retention pruning. Pruning only runs after a successful
upload, so a run of failures never deletes existing good backups.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

PREFIX = "backups/"
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


def _upload(s3, dump_path: Path, bucket: str) -> str:
    key = f"{PREFIX}{dump_path.name}"
    try:
        s3.upload_file(str(dump_path), bucket, key)
    except ClientError as exc:
        print(f"backup: FAILED on upload: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"backup: uploaded s3://{bucket}/{key}")
    return key


def _prune(s3, bucket: str) -> None:
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=PREFIX)
    except ClientError as exc:
        print(f"backup: prune FAILED to list objects: {exc}", file=sys.stderr)
        return

    objects = sorted(resp.get("Contents", []), key=lambda o: o["LastModified"], reverse=True)
    for stale in objects[RETAIN:]:
        try:
            s3.delete_object(Bucket=bucket, Key=stale["Key"])
        except ClientError as exc:
            print(f"backup: prune FAILED to delete {stale['Key']}: {exc}", file=sys.stderr)
        else:
            print(f"backup: pruned {stale['Key']}")


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    bucket = os.environ.get("S3_BACKUP_BUCKET")
    if not bucket:
        print("ERROR: S3_BACKUP_BUCKET not set", file=sys.stderr)
        sys.exit(1)
    region = os.environ.get("AWS_REGION")
    if not region:
        print("ERROR: AWS_REGION not set", file=sys.stderr)
        sys.exit(1)

    dump_path = _dump(db_url)
    s3 = boto3.client("s3", region_name=region)
    _upload(s3, dump_path, bucket)
    dump_path.unlink()

    _prune(s3, bucket)
    print("backup: done")


if __name__ == "__main__":
    main()
