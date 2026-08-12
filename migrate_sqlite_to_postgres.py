"""
BuildCommand AI - one-time SQLite -> PostgreSQL migration

Usage on a machine/container that has:
  - the existing construction_ai_web.db file
  - DATABASE_URL set to the target PostgreSQL database
  - psycopg[binary] installed

Run:
    export MIGRATION_CONFIRM=REPLACE_TARGET
    python migrate_sqlite_to_postgres.py

The script:
  1. Imports full_app.py so PostgreSQL tables are initialized.
  2. Copies SQLite rows table-by-table.
  3. Preserves primary-key IDs where possible.
  4. Resets PostgreSQL identity sequences after import.
"""

import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQLITE_PATH = os.environ.get("SQLITE_PATH", "construction_ai_web.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MIGRATION_CONFIRM = os.environ.get("MIGRATION_CONFIRM", "").strip()

if not DATABASE_URL.startswith(("postgres://", "postgresql://")):
    raise SystemExit("DATABASE_URL must be set to the target PostgreSQL URL.")

if not Path(SQLITE_PATH).exists():
    raise SystemExit(f"SQLite database not found: {SQLITE_PATH}")

# Tables are ordered so parent records are copied before related child records.
TABLES = [
    "companies",
    "projects",
    "users",
    "sessions",
    "user_state",
    "app_state",
    "activities",
    "subs",
    "subcontractor_updates",
    "daily_reports",
    "daily_report_analysis",
    "risks",
    "make_ready",
    "field_updates",
    "memory",
    "production",
    "recovery",
    "action_items",
    "activity_readiness",
    "procurement",
    "project_issues",
    "punch_items",
    "inspections_tracker",
    "submittals",
    "safety_items",
    "change_events",
    "meeting_notes",
    "attachments",
    "notifications",
    "morning_briefs",
    "beta_feedback",
    "invitations",
    "company_settings",
]

sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_conn.row_factory = sqlite3.Row

pg = psycopg.connect(DATABASE_URL, row_factory=dict_row)

if MIGRATION_CONFIRM != "REPLACE_TARGET":
    raise SystemExit(
        "Safety stop: set MIGRATION_CONFIRM=REPLACE_TARGET only when DATABASE_URL "
        "points to the new BuildCommand PostgreSQL database you intend to replace "
        "with the current SQLite data."
    )

def sqlite_table_exists(name):
    return sqlite_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone() is not None

def pg_table_exists(name):
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
            """,
            (name,)
        )
        return cur.fetchone() is not None

def common_columns(table):
    sqlite_cols = [
        r["name"]
        for r in sqlite_conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]

    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table,)
        )
        pg_cols = [r["column_name"] for r in cur.fetchall()]

    return [c for c in sqlite_cols if c in pg_cols]

def copy_table(table):
    if not sqlite_table_exists(table):
        print(f"SKIP {table}: not present in SQLite")
        return

    if not pg_table_exists(table):
        print(f"SKIP {table}: not present in PostgreSQL")
        return

    cols = common_columns(table)
    if not cols:
        print(f"SKIP {table}: no common columns")
        return

    rows = sqlite_conn.execute(
        f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)} FROM "{table}"'
    ).fetchall()

    if not rows:
        print(f"OK   {table}: 0 rows")
        return

    placeholders = ", ".join(["%s"] * len(cols))
    quoted_cols = ", ".join(f'"{c}"' for c in cols)

    # Idempotent-ish import: preserve existing PostgreSQL rows with the same PK.
    conflict = ""
    if "id" in cols:
        conflict = ' ON CONFLICT ("id") DO NOTHING'
    elif table == "user_state" and "user_id" in cols:
        conflict = ' ON CONFLICT ("user_id") DO NOTHING'
    elif table == "company_settings" and "company_id" in cols:
        conflict = ' ON CONFLICT ("company_id") DO NOTHING'

    sql = (
        f'INSERT INTO "{table}" ({quoted_cols}) '
        f'VALUES ({placeholders}){conflict}'
    )

    values = [tuple(row[c] for c in cols) for row in rows]

    with pg.cursor() as cur:
        cur.executemany(sql, values)

    pg.commit()
    print(f"OK   {table}: {len(rows)} rows")

def reset_sequence(table):
    if not pg_table_exists(table):
        return

    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name=%s
              AND column_name='id'
            """,
            (table,)
        )
        row = cur.fetchone()

        if not row or not row["column_default"]:
            return

        cur.execute(
            """
            SELECT setval(
                pg_get_serial_sequence(%s, 'id'),
                COALESCE((SELECT MAX(id) FROM """ + f'"{table}"' + """), 1),
                true
            )
            """,
            (table,)
        )

    pg.commit()

print("Starting BuildCommand SQLite -> PostgreSQL migration...")
print("Clearing target BuildCommand tables first (confirmed by MIGRATION_CONFIRM)...")

existing_target_tables = [t for t in TABLES if pg_table_exists(t)]
if existing_target_tables:
    with pg.cursor() as cur:
        quoted = ", ".join(f'"{t}"' for t in existing_target_tables)
        cur.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
    pg.commit()

for table in TABLES:
    copy_table(table)

for table in TABLES:
    reset_sequence(table)

sqlite_conn.close()
pg.close()

print("Migration complete.")
print("Verify the app against PostgreSQL before removing the old SQLite database.")
