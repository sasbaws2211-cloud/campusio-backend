# Backup & Restore

## Backing up

```bash
python scripts/backup_db.py
```

Writes a `pg_dump` custom-format file to `backups/campusio_<timestamp>.dump`. Requires the Postgres client tools (`pg_dump`) on `PATH` — the same major version as the server is safest, but `pg_dump` is generally forward-compatible with newer server versions.

Schedule it outside every other nightly job's window (see `services/scheduler.py` for what else runs when):

```cron
0 4 * * * cd /path/to/campusio_backend && venv/bin/python scripts/backup_db.py >> logs/backup.log 2>&1
```

Store the resulting files somewhere that isn't the same disk/host as the database itself — a backup that lives next to what it's backing up doesn't survive the failure it exists for.

## Restoring

```bash
python scripts/restore_db.py backups/campusio_2026-08-29T140501Z.dump --confirm
```

This **overwrites the target database** — it prints the host and database name it's about to act on before running, and refuses to run at all without `--confirm`. After restoring, run `alembic upgrade head` if the backup predates a migration that's since been applied.

## When to restore

- **Data corruption or a bad manual change** — restore into a *new*, empty database first, verify the data looks right, then point the app at it (or restore in place only once you're sure).
- **Full database loss** — provision a fresh Postgres instance, restore the most recent backup, run `alembic upgrade head`, update `DATABASE_URL`.

## What this doesn't cover

This is backup/restore tooling, not a full disaster-recovery plan — it doesn't include a secondary region, automated failover, or an RTO/RPO commitment. Those are infrastructure decisions (where the database is hosted, whether it has point-in-time recovery already, what backup retention the hosting provider offers) that belong to whoever operates the production environment, not something a script can decide. If the production database is hosted on a managed service (RDS, Render, Supabase, etc.), check whether it already provides automated backups and point-in-time recovery before relying on this script as the only line of defense — it's meant as a portable, provider-independent fallback, not a replacement for what a managed Postgres instance already gives you.
