#!/usr/bin/env python3
"""Database restore — the counterpart to scripts/backup_db.py.

Restoring is destructive by nature (it overwrites live data), so this
script refuses to run without --confirm, and always prints which database
(host + name, never the password) it's about to overwrite before doing
anything.

Usage:
    python scripts/restore_db.py backups/campusio_2026-08-29T140501Z.dump --confirm
    python scripts/restore_db.py backups/campusio_2026-08-29T140501Z.sql --format plain --confirm

See docs/BACKUP_RESTORE.md for the full runbook (when to restore, how to
verify afterward, how this fits disaster recovery).
"""
import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_settings


def restore(backup_file: Path, fmt: str) -> None:
    settings = get_settings()
    parsed = urlparse(settings.database_url)
    target_description = f"{parsed.hostname}:{parsed.port or 5432}{parsed.path}"

    print(f"About to restore {backup_file} into: {target_description}")
    print("This OVERWRITES existing data in that database. There is no undo.")

    if fmt == "custom":
        args = ["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", settings.database_url, str(backup_file)]
    else:
        args = ["psql", settings.database_url, "-f", str(backup_file)]

    print(f"Restoring (format={fmt})...")
    tool = args[0]
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"{tool} not found on PATH. Install the PostgreSQL client tools and try again.", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(f"Restore reported errors:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print("Restore complete. Run alembic upgrade head afterward if the backup predates a since-applied migration.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore the Campusio database from a backup_db.py dump")
    parser.add_argument("backup_file", type=str, help="Path to the .dump or .sql file to restore")
    parser.add_argument("--format", choices=["custom", "plain"], default="custom", help="Must match the format the backup was taken in")
    parser.add_argument("--confirm", action="store_true", help="Required — restoring overwrites the target database")
    args = parser.parse_args()

    backup_file = Path(args.backup_file)
    if not backup_file.exists():
        print(f"File not found: {backup_file}", file=sys.stderr)
        sys.exit(1)

    if not args.confirm:
        print("Refusing to restore without --confirm (this overwrites the target database).", file=sys.stderr)
        sys.exit(1)

    restore(backup_file, args.format)
