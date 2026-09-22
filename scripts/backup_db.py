#!/usr/bin/env python3
"""Database backup — wraps pg_dump around settings.database_url so a
backup never needs its own separately-maintained connection string.

Requires the Postgres client tools (pg_dump) on PATH — the same major
version as the server is safest, but pg_dump is generally
forward-compatible with newer server versions.

Usage:
    python scripts/backup_db.py                       # backups/campusio_2026-08-29T140501Z.dump
    python scripts/backup_db.py --out /path/to/file.dump
    python scripts/backup_db.py --format plain         # .sql instead of pg_dump's custom format

Example crontab entry (nightly at 04:00 UTC, outside every other job's window):
    0 4 * * * cd /path/to/campusio_backend && venv/bin/python scripts/backup_db.py >> logs/backup.log 2>&1

Restoring: see scripts/restore_db.py and docs/BACKUP_RESTORE.md.
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_settings

BACKUP_DIR = Path(__file__).parent.parent / "backups"


def backup(out_path: Path, fmt: str) -> None:
    settings = get_settings()
    BACKUP_DIR.mkdir(exist_ok=True)

    args = ["pg_dump", settings.database_url, "-f", str(out_path)]
    if fmt == "custom":
        args += ["-F", "c"]  # pg_dump's own compressed format — required for pg_restore/--jobs

    print(f"Backing up database to {out_path} (format={fmt})...")
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        print("pg_dump not found on PATH. Install the PostgreSQL client tools (e.g. `apt install postgresql-client`, `brew install libpq`, or the Windows installer) and try again.", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(f"pg_dump failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Backup complete: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back up the Campusio database")
    parser.add_argument("--out", type=str, default=None, help="Output file path (default: backups/campusio_<timestamp>.dump)")
    parser.add_argument("--format", choices=["custom", "plain"], default="custom", help="pg_dump format — 'custom' (default, restorable with pg_restore) or 'plain' (.sql, restorable with psql)")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    ext = "dump" if args.format == "custom" else "sql"
    out_path = Path(args.out) if args.out else BACKUP_DIR / f"campusio_{timestamp}.{ext}"

    backup(out_path, args.format)
