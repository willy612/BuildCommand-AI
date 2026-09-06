"""
BuildCommand AI — Shared Core
Version 6.6.5 Phase 2A

Small shared foundation for BuildCommand services:
- PostgreSQL/SQLite database compatibility
- session authentication
- current-user runtime context
- platform-owner authorization
- subscription status helper

This module is intentionally independent of full_app.py.
"""

from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import hmac
import os
import re
import secrets
import sqlite3

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_KIND = "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"
SQLITE_DB = os.environ.get("BUILDCOMMAND_SQLITE_DB", "construction_ai_web.db")

_current_user_id = ContextVar("buildcommand_user_id", default=None)
_current_company_id = ContextVar("buildcommand_company_id", default=None)


class PgCompatConnection:
    """Keep existing BuildCommand ?-placeholder SQL working on PostgreSQL."""

    def __init__(self, conn):
        self.conn = conn
        self.last_insert_id = None

    def _sql(self, sql):
        if sql.strip().lower().startswith("select last_insert_rowid()"):
            return None

        translated = sql.replace("?", "%s")

        translated = re.sub(
            r"INSERT OR REPLACE INTO user_state\s*"
            r"\(user_id,selected_project_id\)\s*VALUES\(%s,%s\)",
            "INSERT INTO user_state(user_id,selected_project_id) VALUES(%s,%s) "
            "ON CONFLICT(user_id) DO UPDATE SET selected_project_id=EXCLUDED.selected_project_id",
            translated,
            flags=re.I | re.S,
        )
        translated = re.sub(
            r"INSERT OR REPLACE INTO app_state\s*"
            r"\(id,selected_project_id\)\s*VALUES\(%s,%s\)",
            "INSERT INTO app_state(id,selected_project_id) VALUES(%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET selected_project_id=EXCLUDED.selected_project_id",
            translated,
            flags=re.I | re.S,
        )
        return translated

    def execute(self, sql, params=()):
        translated = self._sql(sql)

        if translated is None:
            value = self.last_insert_id

            class OneRow:
                def fetchone(self):
                    return {"id": value}
                def fetchall(self):
                    return [{"id": value}]
                def __iter__(self):
                    return iter([{"id": value}])

            return OneRow()

        cur = self.conn.cursor()
        match = re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", translated, re.I)

        if match and "RETURNING" not in translated.upper() and match.group(1).lower() not in {"user_state"}:
            try:
                cur.execute(translated.rstrip().rstrip(";") + " RETURNING id", params)
                row = cur.fetchone()
                if row and "id" in row:
                    self.last_insert_id = row["id"]
                return cur
            except Exception:
                self.conn.rollback()
                cur = self.conn.cursor()

        cur.execute(translated, params)
        return cur

    def executescript(self, script):
        ddl = re.sub(r"\bid INTEGER PRIMARY KEY\b", "id BIGSERIAL PRIMARY KEY", script, flags=re.I)
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            self.execute(stmt)
        return self

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def db():
    if DATABASE_KIND == "postgres":
        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL DATABASE_URL is set but psycopg is not installed. "
                "Keep psycopg in requirements.txt."
            )
        return PgCompatConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row))

    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password):
    salt = secrets.token_bytes(16)
    rounds = 210000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        ).hex()
        return hmac.compare_digest(calc, digest_hex)
    except Exception:
        return False


def create_session(user_id):
    raw = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()

    c = db()
    c.execute(
        "INSERT INTO sessions(user_id,token_hash,expires,created) VALUES(?,?,?,?)",
        (user_id, token_hash, expires, datetime.utcnow().isoformat()),
    )
    c.commit()
    c.close()
    return raw


def user_from_session(raw_token):
    if not raw_token:
        return None

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    c = db()
    row = c.execute(
        """
        SELECT u.* FROM sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND (s.expires IS NULL OR s.expires>?)
        """,
        (token_hash, datetime.utcnow().isoformat()),
    ).fetchone()
    c.close()
    return row


def set_request_user(user):
    token_user = _current_user_id.set(user["id"] if user else None)
    token_company = _current_company_id.set(user["company_id"] if user else None)
    return token_user, token_company


def reset_request_user(tokens):
    token_user, token_company = tokens
    _current_user_id.reset(token_user)
    _current_company_id.reset(token_company)


def current_user_id():
    return _current_user_id.get()


def current_company_id():
    return _current_company_id.get()


def current_user():
    uid = current_user_id()
    if not uid:
        return None

    c = db()
    row = c.execute(
        """
        SELECT u.*, c.name AS company_name, c.logo_url
        FROM users u
        JOIN companies c ON c.id=u.company_id
        WHERE u.id=?
        """,
        (uid,),
    ).fetchone()
    c.close()
    return row


def _bc174_owner_emails():
    raw = os.environ.get(
        "PLATFORM_OWNER_EMAILS",
        os.environ.get("PLATFORM_OWNER_EMAIL", ""),
    )
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _bc174_is_platform_owner(user=None):
    user = user or current_user()
    if not user:
        return False
    return str(user["email"] or "").strip().lower() in _bc174_owner_emails()


def _bc174_effective_status(sub):
    if not sub:
        return "NO_SUBSCRIPTION"

    status = str(sub["status"] or "").upper()

    try:
        trial_ends_at = sub["trial_ends_at"]
    except Exception:
        trial_ends_at = None

    if status == "TRIAL" and trial_ends_at:
        try:
            trial_end = datetime.fromisoformat(str(trial_ends_at).replace("Z", "+00:00"))
            trial_end = trial_end.replace(tzinfo=None)
            if trial_end < datetime.utcnow():
                return "TRIAL_EXPIRED"
        except Exception:
            pass

    return status


SHARED_CORE_VERSION = "6.6.5-phase-2A"
