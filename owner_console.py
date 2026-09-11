"""BuildCommand AI — Professional Owner Console 8.5.1.

Drop-in companion for full_app.py 8.5.0. Uses the existing database and
session. Account changes are local controls; this module never calls Stripe.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

OWNER_CONSOLE_VERSION = "8.5.1"
OWNER_EMAIL = "buildcommandai@gmail.com"
RELEASE_NAME = "Professional Owner Console"
LOG = logging.getLogger("buildcommand.owner")
CSRF_COOKIE = "bc_owner_csrf"
STATES = ("PENDING", "ACTIVE", "TRIAL", "PAST_DUE", "SUSPENDED", "CANCELED", "LEGACY")
DEMO_ENROLLMENT_STATES = {"NO_SUBSCRIPTION", "PENDING", "PENDING_PAYMENT", "TRIAL", "TRIAL_EXPIRED", "TRIALING"}
LABELS = {
    "NO_SUBSCRIPTION": "No subscription", "PENDING": "Pending", "ACTIVE": "Active",
    "TRIAL": "Trial", "TRIALING": "Trial", "TRIAL_EXPIRED": "Trial expired",
    "PAST_DUE": "Past due", "SUSPENDED": "Suspended", "CANCELED": "Canceled",
    "CANCELLED": "Canceled", "LEGACY": "Legacy", "UNPAID": "Unpaid",
    "PENDING_APPROVAL": "Pending review", "DENIED": "Ended / declined",
    "EXPIRED": "Expired", "UPGRADED": "Upgraded", "APPROVED": "Approved",
}
ACTION_LABELS = {
    "approve": "Approve access", "revoke": "Revoke approval", "suspend": "Suspend access",
    "reactivate": "Reactivate account", "cancel": "Cancel local subscription",
    "subscription": "Save local subscription", "plan": "Change local plan",
    "status": "Change local status", "note": "Add account note",
}
MESSAGES = {
    "saved": "Changes saved. The account history has been updated.",
    "note": "Account note added.", "demo": "Demo decision saved.",
    "deleted": "The reviewed empty account records were deleted.",
}


def esc(value):
    return escape(str(value if value is not None else ""), quote=True)


def utcnow():
    # The customer app compares naive UTC ISO timestamps.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def date_label(value):
    value = parse_date(value)
    return value.strftime("%b %d, %Y · %H:%M UTC") if value else "—"


def label(value):
    return LABELS.get(str(value or "").upper(), str(value or "Not recorded").replace("_", " ").title())


def money(cents, currency="USD"):
    amount = int(cents or 0) / 100
    code = str(currency or "USD").upper()
    return f"${amount:,.2f}" if code == "USD" else f"{code} {amount:,.2f}"


def badge(text, kind="neutral"):
    return f'<span class="badge {esc(kind)}">{esc(text)}</span>'


def state_badge(value):
    kind = "good" if value in {"ACTIVE", "LEGACY", "APPROVED"} else "bad" if value in {"SUSPENDED", "PAST_DUE", "UNPAID"} else "neutral"
    if value in {"PENDING_APPROVAL", "PENDING", "TRIAL"}:
        kind = "warn"
    return badge(label(value), kind)


class ConsoleProblem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class OwnerConsole:
    def __init__(self, app, runtime, owner_email):
        self.app, self.runtime = app, runtime
        self.owner_email = str(owner_email or OWNER_EMAIL).strip().lower()
        self.postgres = str(getattr(runtime, "DATABASE_KIND", "")).lower() == "postgres"
        self.schema_ready = False
        self.initialize()

    @contextmanager
    def connection(self, write=False):
        connection = self.runtime.db()
        try:
            if write and not self.postgres:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        try:
            with self.connection(True) as c:
                pk = "BIGSERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
                c.execute("""CREATE TABLE IF NOT EXISTS company_access_approvals(
                    company_id BIGINT PRIMARY KEY, approved INTEGER DEFAULT 0,
                    approved_by_user_id BIGINT, approved_at TEXT,
                    revoked_by_user_id BIGINT, revoked_at TEXT, note TEXT,
                    created TEXT, updated TEXT)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS company_access_approval_events(
                    id {pk}, company_id BIGINT NOT NULL, actor_user_id BIGINT,
                    action TEXT NOT NULL, detail TEXT, created TEXT)""")
                c.execute(f"""CREATE TABLE IF NOT EXISTS bc_owner_console_events(
                    id {pk}, actor_user_id BIGINT NOT NULL, actor_email TEXT NOT NULL,
                    company_id BIGINT, company_name TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL, created TEXT NOT NULL)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_bc_owner_events_company ON bc_owner_console_events(company_id,id)")
            self.schema_ready = True
        except Exception:
            LOG.exception("Owner Console schema initialization failed")

    def owner_emails(self):
        configured = os.environ.get("PLATFORM_OWNER_EMAILS", os.environ.get("PLATFORM_OWNER_EMAIL", ""))
        emails = {x.strip().lower() for x in configured.split(",") if x.strip()}
        return emails or {self.owner_email}

    def actor(self, c):
        session_user = self.runtime.current_user()
        if not session_user:
            raise ConsoleProblem("Sign in with your platform owner account to open this console.", 401)
        uid = dict(session_user).get("id")
        row = c.execute("SELECT id,email,display_name,role,company_id FROM users WHERE id=?", (uid,)).fetchone()
        user = dict(row) if row else {}
        if str(user.get("role") or "").upper() not in {"OWNER", "PLATFORM_OWNER"} or str(user.get("email") or "").strip().lower() not in self.owner_emails():
            raise ConsoleProblem("This console is reserved for the BuildCommand platform owner.", 403)
        return user

    def current_actor(self):
        with self.connection() as c:
            return self.actor(c)

    def protected_companies(self, c, user):
        # Protect every configured owner, plus the original master identity.
        emails = sorted(self.owner_emails() | {self.owner_email})
        marks = ",".join("?" for _ in emails)
        rows = c.execute(f"SELECT company_id FROM users WHERE LOWER(email) IN ({marks})", tuple(emails)).fetchall()
        return {int(r["company_id"]) for r in rows if r["company_id"] is not None} | {int(user["company_id"])}

    def table_exists(self, c, table):
        if self.postgres:
            return bool(c.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=?", (table,)).fetchone())
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def lock_account(self, c, cid):
        suffix = " FOR UPDATE" if self.postgres else ""
        row = c.execute("SELECT id,name FROM companies WHERE id=?" + suffix, (cid,)).fetchone()
        if not row:
            raise ConsoleProblem("This customer account could not be found.", 404)
        if self.postgres:
            for table in ("company_subscriptions", "company_access_approvals", "company_demo_access"):
                if self.table_exists(c, table):
                    c.execute(f"SELECT company_id FROM {table} WHERE company_id=? FOR UPDATE", (cid,)).fetchall()

    def dataset(self, c, user):
        protected = self.protected_companies(c, user)
        companies = c.execute("""SELECT co.id,co.name,co.created,
            (SELECT COUNT(*) FROM users u WHERE u.company_id=co.id) AS user_count,
            (SELECT COUNT(*) FROM projects p WHERE p.company_id=co.id) AS project_count,
            (SELECT u.email FROM users u WHERE u.company_id=co.id ORDER BY u.id LIMIT 1) AS contact
            FROM companies co ORDER BY LOWER(co.name),co.id""").fetchall()
        subs = {int(r["company_id"]): dict(r) for r in c.execute("""SELECT s.* FROM company_subscriptions s
            WHERE s.id=(SELECT MAX(s2.id) FROM company_subscriptions s2 WHERE s2.company_id=s.company_id)""").fetchall()}
        approvals = {int(r["company_id"]): dict(r) for r in c.execute("SELECT * FROM company_access_approvals").fetchall()}
        demos = {int(r["company_id"]): dict(r) for r in c.execute("SELECT * FROM company_demo_access").fetchall()} if self.table_exists(c, "company_demo_access") else {}
        plans = [dict(r) for r in c.execute("SELECT * FROM platform_plans ORDER BY monthly_price_cents,code").fetchall()]
        rows = []
        for source in companies:
            row = dict(source)
            cid = int(row["id"])
            if cid in protected:
                continue
            sub, approval, demo = subs.get(cid, {}), approvals.get(cid, {}), demos.get(cid, {})
            status = str(sub.get("status") or "NO_SUBSCRIPTION").upper()
            if status == "CANCELLED":
                status = "CANCELED"
            end = parse_date(sub.get("trial_ends_at"))
            if status == "TRIAL" and end and end <= datetime.now(timezone.utc):
                status = "TRIAL_EXPIRED"
            demo_state = str(demo.get("status") or "").upper()
            expiry = parse_date(demo.get("expires_at"))
            if demo_state == "ACTIVE" and (not expiry or expiry <= datetime.now(timezone.utc)):
                demo_state = "EXPIRED"
            approved = bool(int(approval.get("approved") or 0))
            eligible = status == "ACTIVE"
            # Display the same separate paid/demo paths as the 8.5 customer gate.
            access = "Demo access" if demo_state == "ACTIVE" else "Enabled" if eligible and approved else "Awaiting approval" if eligible else "Restricted"
            row.update(sub=sub, approval=approval, demo=demo, status=status,
                       demo_status=demo_state, approved=approved, access=access,
                       eligible=eligible, plan_code=sub.get("plan_code") or "",
                       stripe_payment_status=sub.get("stripe_payment_status") or "Not recorded")
            rows.append(row)
        return rows, plans

    def account(self, c, user, cid):
        if cid in self.protected_companies(c, user):
            raise ConsoleProblem("The platform owner's company is protected from customer account controls.", 409)
        rows, plans = self.dataset(c, user)
        row = next((r for r in rows if int(r["id"]) == cid), None)
        if not row:
            raise ConsoleProblem("This customer account could not be found.", 404)
        return row, plans

    def digest(self, row):
        selected = {key: row.get(key) for key in ("id", "name", "user_count", "project_count", "sub", "approval", "demo")}
        return hashlib.sha256(json.dumps(selected, sort_keys=True, default=str).encode()).hexdigest()

    def token(self, request, purpose, value):
        session = request.cookies.get("bc_session", "")
        payload = json.dumps({"purpose": purpose, "value": value, "time": int(time.time())}, sort_keys=True, separators=(",", ":"))
        # A signed session-bound value, independent of worker-local memory.
        import base64
        body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        signature = hmac.new(session.encode(), body.encode(), hashlib.sha256).hexdigest()
        return body + "." + signature

    def verify_token(self, request, token, purpose, max_age=900):
        import base64
        session = request.cookies.get("bc_session", "")
        try:
            if not session or len(token) > 16000:
                raise ValueError()
            body, signature = token.split(".", 1)
            expected = hmac.new(session.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            age = time.time() - int(payload["time"])
            if payload["purpose"] != purpose or not -30 <= age <= max_age:
                raise ValueError()
            return payload["value"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ConsoleProblem("This form has expired or changed. Refresh the page and try again.", 409)

    def form_security(self, request, form):
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                same = (parsed.scheme.lower(), parsed.netloc.lower()) == (request.url.scheme.lower(), request.url.netloc.lower())
            except ValueError:
                same = False
            if not same:
                raise ConsoleProblem("Submit this form from the Owner Console.", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ConsoleProblem("Submit this form from the Owner Console.", 403)
        cookie, submitted = request.cookies.get(CSRF_COOKIE, ""), str(form.get("csrf_token") or "")
        if not cookie or not submitted or not hmac.compare_digest(cookie.encode(), submitted.encode()):
            raise ConsoleProblem("Refresh this page before submitting the form.", 403)
        self.verify_token(request, cookie, "csrf", 43200)

    def hidden(self, request, csrf, row=None):
        fields = f'<input type="hidden" name="csrf_token" value="{esc(csrf)}">'
        if row:
            snapshot = self.token(request, "account", {"id": row["id"], "digest": self.digest(row)})
            fields += f'<input type="hidden" name="snapshot" value="{esc(snapshot)}">'
        return fields

    def check_snapshot(self, request, form, row):
        value = self.verify_token(request, str(form.get("snapshot") or ""), "account")
        if value != {"id": row["id"], "digest": self.digest(row)}:
            raise ConsoleProblem("This account changed after you opened the form. Refresh it to review the latest information.", 409)

    def insert(self, c, table, columns, values, pk="id"):
        # Explicit RETURNING prevents PgCompatConnection's rollback-and-retry
        # behavior on tables keyed by company_id rather than id.
        marks = ",".join("?" for _ in values)
        return c.execute(f"INSERT INTO {table}({columns}) VALUES({marks}) RETURNING {pk}", tuple(values)).fetchone()

    def audit(self, c, user, row, action, detail):
        self.insert(c, "bc_owner_console_events", "actor_user_id,actor_email,company_id,company_name,action,detail,created",
                    (int(user["id"]), user["email"], int(row["id"]), row["name"], action, detail, utcnow().isoformat()))

    def set_approval(self, c, user, row, enabled, note):
        ts = utcnow().isoformat()
        if not row["approval"]:
            self.insert(c, "company_access_approvals", "company_id,approved,created,updated", (row["id"], 0, ts, ts), "company_id")
        if enabled:
            c.execute("""UPDATE company_access_approvals SET approved=1,approved_by_user_id=?,approved_at=?,
                revoked_by_user_id=NULL,revoked_at=NULL,note=?,updated=? WHERE company_id=?""",
                      (user["id"], ts, note, ts, row["id"]))
        else:
            c.execute("""UPDATE company_access_approvals SET approved=0,revoked_by_user_id=?,revoked_at=?,
                note=?,updated=? WHERE company_id=?""", (user["id"], ts, note, ts, row["id"]))

    def set_subscription(self, c, row, plan, state):
        ts = utcnow().isoformat()
        if row["sub"]:
            c.execute("UPDATE company_subscriptions SET plan_code=?,status=?,updated=? WHERE id=?",
                      (plan, state, ts, row["sub"]["id"]))
        else:
            self.insert(c, "company_subscriptions", "company_id,plan_code,status,created,updated", (row["id"], plan, state, ts, ts))

    def mutate(self, request, form, cid, action):
        with self.connection(True) as c:
            user = self.actor(c)
            self.lock_account(c, cid)
            row, plans = self.account(c, user, cid)
            self.check_snapshot(request, form, row)
            note = str(form.get("note") or "").strip()
            if len(note) > 2000:
                raise ConsoleProblem("Keep the account note under 2,000 characters.")
            if action in {"suspend", "reactivate", "cancel", "revoke"} and form.get("confirmed") != "yes":
                raise ConsoleProblem("Review and confirm this account change first.", 409)
            if action == "note":
                if not note:
                    raise ConsoleProblem("Enter an account note.")
            elif action == "approve":
                self.set_approval(c, user, row, True, note or "Approved by platform owner")
            elif action in {"revoke", "suspend", "cancel"}:
                self.set_approval(c, user, row, False, note or ACTION_LABELS[action])
                if action != "revoke" and row["sub"]:
                    self.set_subscription(c, row, row["plan_code"], "SUSPENDED" if action == "suspend" else "CANCELED")
                # A running demo independently permits entry in 8.5.
                if row["demo"]:
                    c.execute("UPDATE company_demo_access SET status='DENIED' WHERE company_id=?", (cid,))
            elif action == "reactivate":
                if not row["sub"]:
                    raise ConsoleProblem("Save a local subscription before reactivating this account.", 409)
                self.set_subscription(c, row, row["plan_code"], "ACTIVE")
                self.set_approval(c, user, row, True, note or "Manually reactivated by platform owner")
            elif action in {"subscription", "plan", "status"}:
                active_plans = {str(p["code"]): p for p in plans if int(p.get("active") if p.get("active") is not None else 1)}
                plan = str(form.get("plan_code") or "").strip() if action != "status" else row["plan_code"]
                state = str(form.get("status") or "").upper().strip() if action != "plan" else str(row["sub"].get("status") or "PENDING").upper()
                if plan not in active_plans and not (row["sub"] and plan == row["plan_code"]):
                    raise ConsoleProblem("Choose a plan from the current plan catalog.")
                if not plan or state not in STATES:
                    raise ConsoleProblem("Choose a valid plan and subscription status.")
                if not note:
                    raise ConsoleProblem("Add a brief reason for this local subscription change.")
                self.set_subscription(c, row, plan, state)
                if state in {"SUSPENDED", "CANCELED"}:
                    self.set_approval(c, user, row, False, note)
                    if row["demo"]:
                        c.execute("UPDATE company_demo_access SET status='DENIED' WHERE company_id=?", (cid,))
                note = f"Plan: {row['plan_code'] or 'none'} → {plan}; local status: {row['sub'].get('status') or 'none'} → {state}. {note}"
            else:
                raise ConsoleProblem("That account action is not available.", 404)
            self.audit(c, user, row, action.upper(), note or ACTION_LABELS[action])
        return RedirectResponse(f"/owner/customers/{cid}?notice={'note' if action == 'note' else 'saved'}", status_code=303)

    def decide_demo(self, request, form, cid, action):
        with self.connection(True) as c:
            user = self.actor(c)
            self.lock_account(c, cid)
            row, plans = self.account(c, user, cid)
            self.check_snapshot(request, form, row)
            if not row["demo"]:
                raise ConsoleProblem("No demo request exists for this account.", 404)
            if action == "approve":
                if row["demo_status"] != "PENDING_APPROVAL":
                    raise ConsoleProblem("Only a pending demo can be approved. Refresh the account to see its current state.", 409)
                if row['status'] not in DEMO_ENROLLMENT_STATES | {'ACTIVE', 'LEGACY'}:
                    raise ConsoleProblem('Resolve this account’s local subscription status before approving a demo. Its billing or suspension status will not be overwritten by demo approval.', 409)
                start = utcnow()
                expires = (start + timedelta(days=7)).isoformat()
                if row['status'] in DEMO_ENROLLMENT_STATES:
                    plan = row['plan_code'] or next((str(p['code']) for p in plans if int(p.get('active') if p.get('active') is not None else 1)), '')
                    if not plan:
                        raise ConsoleProblem('Configure an active plan before approving this new account’s demo.', 409)
                    self.set_subscription(c, row, plan, 'TRIAL')
                    # 8.5 still has a legacy subscription gate in addition to
                    # company_demo_access. Both clocks must start at approval.
                    c.execute('''UPDATE company_subscriptions SET trial_ends_at=?
                        WHERE id=(SELECT id FROM company_subscriptions WHERE company_id=? ORDER BY id DESC LIMIT 1)''', (expires, cid))
                c.execute("UPDATE company_demo_access SET status='ACTIVE',started_at=?,expires_at=?,upgraded_at=NULL WHERE company_id=?",
                          (start.isoformat(), expires, cid))
                detail = "Approved a seven-day demo. The clock starts at approval."
            elif action == "deny":
                if form.get("confirmed") != "yes":
                    raise ConsoleProblem("Review the demo decision before confirming.", 409)
                if row["demo_status"] not in {"PENDING_APPROVAL", "ACTIVE"}:
                    raise ConsoleProblem("This demo is no longer awaiting a decision or running.", 409)
                c.execute("UPDATE company_demo_access SET status='DENIED' WHERE company_id=?", (cid,))
                detail = "Declined or ended the demo. Paid account approval was not changed."
            else:
                raise ConsoleProblem("That demo action is not available.", 404)
            note = str(form.get('note') or '').strip()
            if len(note) > 2000:
                raise ConsoleProblem('Keep the decision note under 2,000 characters.')
            if note:
                detail += ' ' + note
            self.audit(c, user, row, "DEMO_" + action.upper(), detail)
        return RedirectResponse("/owner/demos?notice=demo", status_code=303)

    def history(self, c, cid=None):
        clause, params = (" WHERE company_id=?", (cid,)) if cid is not None else ("", ())
        events = [dict(r) for r in c.execute("SELECT * FROM bc_owner_console_events" + clause + " ORDER BY id DESC LIMIT 30", params).fetchall()]
        for table in ("owner_subscription_events", "company_access_approval_events", "owner_cleanup_events"):
            if not self.table_exists(c, table) or (cid is not None and table == "owner_cleanup_events"):
                continue
            old = c.execute(f"SELECT * FROM {table}" + (clause if table != "owner_cleanup_events" else "") + " ORDER BY created DESC LIMIT 15", params if table != "owner_cleanup_events" else ()).fetchall()
            for source in old:
                row = dict(source)
                row.setdefault("actor_email", "Earlier console")
                row.setdefault("company_name", "Company #" + str(row.get("company_id") or "—"))
                events.append(row)
        return sorted(events, key=lambda row: parse_date(row.get("created")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:30]

    def readiness(self):
        mode = (os.environ.get("STRIPE_MODE") or "LIVE").strip().upper()
        if mode not in {"LIVE", "TEST"}:
            mode = "LIVE"
        prefix = "STRIPE_TEST_" if mode == "TEST" else "STRIPE_"
        base = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
        try:
            parsed = urlsplit(base)
            valid_base = bool(parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except ValueError:
            valid_base = False
        return {"mode": mode, "secret_key_configured": bool(os.environ.get(prefix + "SECRET_KEY", "").strip()),
                "webhook_secret_configured": bool(os.environ.get(prefix + "WEBHOOK_SECRET", "").strip()),
                "app_base_url_configured": valid_base,
                "webhook_url": base + "/billing/stripe-webhook" if valid_base else None,
                "scope": "Configuration presence only; no live payment connection was tested."}

    def summary(self, rows, plans):
        customers = [r for r in rows if r["user_count"]]
        prices = {str(p["code"]): int(p.get("monthly_price_cents") or 0) for p in plans}
        estimated = sum(prices.get(r["plan_code"], 0) for r in customers if r["status"] == "ACTIVE" and not int(r["sub"].get("grandfathered") or 0) and r["demo_status"] != "ACTIVE")
        return {"customers": len(customers), "mrr_cents": estimated, "arr_cents": estimated * 12,
                "active": sum(r["status"] in {"ACTIVE", "LEGACY"} for r in customers),
                "trials": sum(r["status"] == "TRIAL" for r in customers),
                "past_due": sum(r["status"] in {"PAST_DUE", "UNPAID"} for r in customers),
                "canceled": sum(r["status"] == "CANCELED" for r in customers),
                "awaiting_approval": sum(r["eligible"] and not r["approved"] and r["demo_status"] != "ACTIVE" for r in customers),
                "pending_demos": sum(r["demo_status"] == "PENDING_APPROVAL" for r in customers),
                "active_demos": sum(r["demo_status"] == "ACTIVE" for r in customers),
                "revenue_basis": "Estimate from local active plan prices; not collected revenue."}

    def shell(self, request, user, title, description, body, active="overview", action=""):
        tabs = [("overview", "Overview", "/owner", "grid"),
                ("customers", "Customers", "/owner/customers", "building"),
                ("demos", "Demo requests", "/owner/demos", "clock"),
                ("billing", "Billing", "/owner/billing", "card"),
                ("activity", "Activity", "/owner/activity", "activity"),
                ("maintenance", "Maintenance", "/owner/cleanup", "settings")]
        nav = "".join(f'<a href="{href}" class="nav-link {"active" if key == active else ""}" {"aria-current=page" if key == active else ""}>{icon(symbol)}<span>{text}</span></a>' for key, text, href, symbol in tabs)
        name = str(user.get("display_name") or "Platform owner")
        initials = "".join(word[0] for word in name.split()[:2]).upper()
        notice = MESSAGES.get(request.query_params.get("notice", ""), "")
        notice_html = f'<div class="notice success" role="status">{icon("check")}<span>{esc(notice)}</span></div>' if notice else ""
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>{esc(title)} · BuildCommand Owner</title>
<style>{STYLES}</style></head><body class="oc">
<a class="skip" href="#main">Skip to content</a>
<aside class="sidebar" aria-label="Owner console navigation">
  <a class="brand" href="/owner"><span class="brand-mark">{icon("building")}</span><span>BuildCommand <b>AI</b><small>OWNER CONSOLE</small></span></a>
  <div class="nav-label">BUSINESS</div><nav>{nav}</nav>
  <div class="sidebar-bottom"><a class="nav-link" href="/workspace">{icon("arrow")}<span>Open construction app</span></a>
  <div class="owner-profile"><span class="avatar">{esc(initials)}</span><span>{esc(name)}<small>Platform owner</small></span></div></div>
</aside>
<div class="main-wrap"><header class="topbar"><span>Business workspace <span class="divider">/</span> <b>{esc(title)}</b></span><span class="top-date">{utcnow().strftime('%b %d, %Y')}</span></header>
<main id="main" tabindex="-1"><div class="page-heading"><div><div class="eyebrow">OWNER CONSOLE</div><h1>{esc(title)}</h1><p>{esc(description)}</p></div>{action}</div>
{notice_html}{body}</main>
<footer>BuildCommand AI <span>Owner Console {OWNER_CONSOLE_VERSION} · Built by Willy LaHood © 2026</span></footer></div>
</body></html>'''

    def metric(self, title, value, description, href=None):
        content = f'<span class="metric-label">{esc(title)}</span><strong>{esc(value)}</strong><span class="metric-note">{esc(description)}</span>'
        return f'<a class="metric" href="{esc(href)}">{content}{icon("arrow")}</a>' if href else f'<div class="metric">{content}</div>'

    def empty(self, title, detail):
        return f'<div class="empty">{icon("inbox")}<h3>{esc(title)}</h3><p>{esc(detail)}</p></div>'

    def table(self, headings, rows, caption):
        return f'<div class="table-scroll" role="region" aria-label="{esc(caption)}" tabindex="0"><table><caption class="sr-only">{esc(caption)}</caption><thead><tr>' + "".join(f'<th scope="col">{esc(h)}</th>' for h in headings) + '</tr></thead><tbody>' + rows + '</tbody></table></div>'

    def customer_table(self, rows, billing=False):
        if not rows:
            return self.empty("No matching accounts", "Try another search or clear the filters.")
        lines = []
        for row in rows:
            cid = int(row["id"])
            account = f'<a class="account-link" href="/owner/customers/{cid}">{esc(row["name"])}</a><small>{esc(row.get("contact") or "No users yet")}</small>'
            access = badge(row["access"], "good" if row["access"] == "Enabled" else "info" if row["access"] == "Demo access" else "warn" if row["access"] == "Awaiting approval" else "neutral")
            fields = [account, esc(row["plan_code"] or "—"), state_badge(row["status"])]
            if billing:
                fields += [esc(label(row["stripe_payment_status"])), badge("Approved", "good") if row["approved"] else badge("Pending"), access]
            else:
                fields += [access, f'<span class="counts">{int(row["user_count"])} people <span>·</span> {int(row["project_count"])} projects</span>']
            fields += [f'<a class="text-link" href="/owner/customers/{cid}" aria-label="Review {esc(row["name"])}">Review {icon("arrow")}</a>']
            lines.append('<tr>' + ''.join(f'<td>{field}</td>' for field in fields) + '</tr>')
        headers = ["Customer", "Plan", "Local status"] + (["Recorded Stripe status", "Owner approval", "App access"] if billing else ["App access", "Team & projects"]) + ["Action"]
        return self.table(headers, ''.join(lines), "Customer accounts")

    def filtered(self, request, rows, demo=False):
        q = str(request.query_params.get("q") or "").strip()[:200]
        status = str(request.query_params.get("status") or "all").upper()
        access = str(request.query_params.get("access") or "all").lower()
        selected = []
        for row in rows:
            if q and q.casefold() not in f"{row['name']} {row.get('contact') or ''} {row['id']}".casefold():
                continue
            if status != "ALL" and (row["demo_status"] if demo else row["status"]) != status:
                continue
            if access == "pending" and (row["approved"] or not row["eligible"] or row["demo_status"] == "ACTIVE"):
                continue
            if access == "enabled" and row["access"] not in {"Enabled", "Demo access"}:
                continue
            if access == "restricted" and row["access"] in {"Enabled", "Demo access"}:
                continue
            selected.append(row)
        try:
            page = max(1, min(int(request.query_params.get("page", 1)), 1000000))
        except ValueError:
            page = 1
        page = min(page, max(1, (len(selected) + 24) // 25))
        return selected[(page - 1) * 25: page * 25], len(selected), page

    def filters(self, request, total, demo=False):
        q = request.query_params.get("q", "")[:200]
        states = ("PENDING_APPROVAL", "ACTIVE", "EXPIRED", "DENIED", "UPGRADED") if demo else STATES + ("TRIAL_EXPIRED", "NO_SUBSCRIPTION", "UNPAID")
        selected_state = str(request.query_params.get("status", "all")).upper()
        options = '<option value="all">All statuses</option>' + ''.join(f'<option value="{state}" {"selected" if selected_state == state else ""}>{esc(label(state))}</option>' for state in states)
        access_value = request.query_params.get("access", "all")
        access = '' if demo else '<label><span>App access</span><select name="access">' + ''.join(f'<option value="{key}" {"selected" if access_value == key else ""}>{text}</option>' for key, text in (("all", "All access"), ("pending", "Awaiting approval"), ("enabled", "Enabled or demo"), ("restricted", "Restricted"))) + '</select></label>'
        return f'''<form class="filters" method="get" action="{esc(request.url.path)}" role="search">
<label class="search-label"><span>Search accounts</span><input type="search" name="q" value="{esc(q)}" maxlength="200" placeholder="Company, contact email, or ID"></label>
<label><span>Status</span><select name="status">{options}</select></label>{access}
<button class="btn secondary" type="submit">Apply filters</button><a class="clear-link" href="{esc(request.url.path)}">Clear</a></form>
<div class="result-count">{total} matching {'demo requests' if demo else 'accounts'}</div>'''

    def pager(self, request, total, page):
        pages = max(1, (total + 24) // 25)
        if pages == 1:
            return ''
        def link(number, text):
            query = dict(request.query_params)
            query["page"] = number
            return f'<a class="btn secondary" href="{esc(request.url.path + "?" + urlencode(query))}">{text}</a>'
        return f'<nav class="pager" aria-label="Results pages"><span>Page {page} of {pages}</span><div class="actions">{link(page - 1, "Previous") if page > 1 else ""}{link(page + 1, "Next") if page < pages else ""}</div></nav>'

    def timeline(self, events, account=False):
        if not events:
            return self.empty("No account activity yet", "Owner decisions and notes will appear here as they happen.")
        items = []
        for row in events:
            action = ACTION_LABELS.get(str(row.get("action") or "").lower(), label(row.get("action")))
            company = '' if account else f'<span class="event-company">{esc(row.get("company_name") or "Earlier account event")}</span>'
            items.append(f'''<li><span class="event-dot"></span><div><div class="event-title"><b>{esc(action)}</b>{company}<time>{esc(date_label(row.get("created")))}</time></div>
<p class="prewrap">{esc(row.get("detail") or "No additional note.")}</p><small>{esc(row.get("actor_email") or "Earlier console")}</small></div></li>''')
        return '<ol class="timeline">' + ''.join(items) + '</ol>'

    def dashboard(self, c, request, user, rows, plans):
        metrics = self.summary(rows, plans)
        cards = ''.join([
            self.metric("Customer accounts", metrics["customers"], "Companies with registered users", "/owner/customers"),
            self.metric("Awaiting approval", metrics["awaiting_approval"], "Active accounts ready for review", "/owner/customers?access=pending"),
            self.metric("Demo requests", metrics["pending_demos"], "Waiting for your decision", "/owner/demos?status=PENDING_APPROVAL"),
            self.metric("Estimated monthly recurring", money(metrics["mrr_cents"]), "Local plan estimate · not collections"),
        ])
        queue = []
        for row in rows:
            if not row["user_count"]:
                continue
            if row["demo_status"] == "PENDING_APPROVAL":
                reason, style, destination = "Demo request", "info", "/owner/demos?status=PENDING_APPROVAL"
            elif row["access"] == "Awaiting approval":
                reason, style, destination = "Access approval", "warn", f'/owner/customers/{row["id"]}'
            elif row["status"] in {"PAST_DUE", "UNPAID"}:
                reason, style, destination = "Billing attention", "bad", f'/owner/customers/{row["id"]}'
            else:
                continue
            queue.append(f'<a class="queue-item" href="{destination}"><span><strong>{esc(row["name"])}</strong><small>{esc(row.get("contact") or "No contact recorded")}</small></span>{badge(reason, style)}{icon("arrow")}</a>')
        queue_html = ''.join(queue[:6]) if queue else self.empty("You're up to date", "No pending demos, access approvals, or past-due accounts.")
        events = self.history(c)[:4]
        body = f'''<section class="metrics" aria-label="Business overview">{cards}</section>
<div class="split"><section class="panel"><div class="panel-heading"><div><h2>Needs your attention</h2><p>Decisions that keep customer accounts moving.</p></div>{badge(str(len(queue)) + " items")}</div>{queue_html}</section>
<section class="panel quick-panel"><div class="eyebrow">ACCOUNT ACCESS</div><h2>A clear view of every customer.</h2><p>Review the local subscription, your approval, and any active demo together.</p><a class="btn primary" href="/owner/customers">Review customers {icon("arrow")}</a>
<div class="quick-stats"><span><b>{metrics['active']}</b>Active / legacy accounts</span><span><b>{metrics['active_demos']}</b>Running demos</span><span><b>{metrics['past_due']}</b>Billing issues</span></div></section></div>
<section class="panel"><div class="panel-heading"><div><h2>Customer accounts</h2><p>Your first ten customer accounts, listed alphabetically.</p></div><a class="text-link" href="/owner/customers">View all {icon('arrow')}</a></div>{self.customer_table([r for r in rows if r['user_count']][:10])}</section>
<section class="panel"><div class="panel-heading"><div><h2>Recent owner activity</h2><p>Decisions and notes across the business.</p></div><a class="text-link" href="/owner/activity">View activity {icon('arrow')}</a></div>{self.timeline(events)}</section>'''
        return self.shell(request, user, "Overview", "Your customers, account decisions, and business activity in one place.", body)

    def customers(self, request, user, rows):
        selected, total, page = self.filtered(request, rows)
        body = '<section class="panel">' + self.filters(request, total) + self.customer_table(selected) + self.pager(request, total, page) + '</section>'
        return self.shell(request, user, "Customers", "Find an account and review its subscription, people, projects, and access.", body, "customers")

    def customer_detail(self, c, request, user, csrf, row, plans):
        cid = int(row["id"])
        tab = request.query_params.get("tab", "account")
        if tab not in {"account", "people", "projects", "activity"}:
            tab = "account"
        tabs = '<nav class="tabs" aria-label="Customer sections">' + ''.join(f'<a href="/owner/customers/{cid}?tab={key}" class="{"active" if key == tab else ""}" {"aria-current=page" if key == tab else ""}>{text}</a>' for key, text in (("account", "Account"), ("people", "People"), ("projects", "Projects"), ("activity", "Activity"))) + '</nav>'
        summary = f'''<a class="back-link" href="/owner/customers">← All customers</a><section class="account-summary"><div class="company-monogram">{esc(str(row['name'])[:2].upper())}</div>
<div><h2>{esc(row['name'])}</h2><p>Company #{cid} <span>·</span> {esc(row.get('contact') or 'No users yet')}</p></div><div class="summary-badges">{state_badge(row['status'])}{badge(row['access'], 'good' if row['access'] == 'Enabled' else 'info' if row['access'] == 'Demo access' else 'neutral')}</div></section>'''
        hidden = self.hidden(request, csrf, row)
        if tab == "people":
            offset, _ = self.offset(request, row['user_count'])
            people = c.execute("SELECT id,email,display_name,role,created FROM users WHERE company_id=? ORDER BY id LIMIT 25 OFFSET ?", (cid, offset)).fetchall()
            lines = ''.join(f'<tr><td><b>{esc(p["display_name"] or "—")}</b><small>{esc(p["email"])}</small></td><td>{esc(label(p["role"]))}</td><td>{esc(date_label(p["created"]))}</td></tr>' for p in people)
            content = '<section class="panel"><div class="panel-heading"><div><h2>People</h2><p>Company administrators manage roles and project assignments in their company workspace.</p></div></div>' + (self.table(["Person", "Company role", "Joined"], lines, "Company people") if lines else self.empty("No people on this account", "Registered users will appear here.")) + self.pager(request, row['user_count'], offset // 25 + 1) + '</section>'
        elif tab == "projects":
            offset, _ = self.offset(request, row['project_count'])
            projects = c.execute("SELECT id,name FROM projects WHERE company_id=? ORDER BY id DESC LIMIT 25 OFFSET ?", (cid, offset)).fetchall()
            lines = ''.join(f'<tr><td><b>{esc(p["name"])}</b></td><td>#{int(p["id"])}</td></tr>' for p in projects)
            content = '<section class="panel"><div class="panel-heading"><div><h2>Projects</h2><p>Project records remain managed by the customer’s appointed team.</p></div></div>' + (self.table(["Project", "ID"], lines, "Customer projects") if lines else self.empty("No projects yet", "Projects created by this customer will appear here.")) + self.pager(request, row['project_count'], offset // 25 + 1) + '</section>'
        elif tab == "activity":
            content = '<section class="panel"><div class="panel-heading"><div><h2>Account history</h2><p>Most recent 30 owner events, including available history from the earlier console.</p></div></div>' + self.timeline(self.history(c, cid), True) + '</section>'
        else:
            plan_choices = []
            codes = set()
            for plan in plans:
                code = str(plan["code"])
                if not int(plan.get("active") if plan.get("active") is not None else 1) and code != row["plan_code"]:
                    continue
                codes.add(code)
                plan_choices.append(f'<option value="{esc(code)}" {"selected" if code == row["plan_code"] else ""}>{esc(plan.get("name") or code)} · {esc(money(plan.get("monthly_price_cents")))}/month</option>')
            if row["plan_code"] and row["plan_code"] not in codes:
                plan_choices.insert(0, f'<option value="{esc(row["plan_code"])}" selected>{esc(row["plan_code"])} · retained plan</option>')
            if not row["sub"]:
                plan_choices.insert(0, '<option value="" selected disabled>Choose a plan</option>')
            raw_status = str(row["sub"].get("status") or "PENDING").upper()
            state_choices = ''.join(f'<option value="{state}" {"selected" if state == raw_status else ""}>{label(state)}</option>' for state in STATES)
            if raw_status not in STATES:
                state_choices = f'<option value="" selected disabled>{esc(label(raw_status))} · choose a new status to change</option>' + state_choices
            approve = '' if row['approved'] else f'<form method="post" action="/owner/customers/{cid}/approve">{hidden}<button class="btn primary" type="submit">Approve access</button></form>'
            revoke = f'<a class="btn secondary" href="/owner/customers/{cid}/review?action=revoke">Revoke approval</a>' if row['approved'] else ''
            demo_note = f'<div class="notice info">{icon("clock")}<span>Demo: {esc(label(row["demo_status"]))}. {"Ends " + esc(date_label(row["demo"].get("expires_at"))) if row["demo_status"] == "ACTIVE" else "Manage demo decisions on Demo requests."}</span></div>' if row['demo'] else ''
            billing = self.billing_activity(c, cid)
            content = f'''<div class="split account-split"><section class="panel padded"><div class="section-label">ACCESS</div><h2>Customer access</h2>
<dl class="facts"><div><dt>Local subscription</dt><dd>{state_badge(row['status'])}</dd></div><div><dt>Owner approval</dt><dd>{badge('Approved', 'good') if row['approved'] else badge('Pending', 'warn')}</dd></div><div><dt>App access</dt><dd>{esc(row['access'])}</dd></div></dl>
<p class="helper">Paid account access requires an active subscription and your approval. A running, approved demo provides temporary access.</p>{demo_note}<div class="actions">{approve}{revoke}</div>
<div class="section-divider"></div><h3>Account actions</h3><p class="helper">Review the effect of each action before confirming.</p><div class="actions"><a class="btn secondary" href="/owner/customers/{cid}/review?action=suspend">Suspend access</a><a class="btn secondary" href="/owner/customers/{cid}/review?action=reactivate">Reactivate account</a></div>
</section><section class="panel padded"><div class="section-label">SUBSCRIPTION</div><h2>Local plan & status</h2><p class="helper">These controls update BuildCommand access. They do not charge a card or change a Stripe subscription.</p>
<form class="stack-form" method="post" action="/owner/customers/{cid}/subscription">{hidden}<label>Plan<select name="plan_code" required>{''.join(plan_choices)}</select></label><label>Local subscription status<select name="status" required>{state_choices}</select></label>
<label>Reason for change<textarea name="note" maxlength="2000" rows="2" required placeholder="Briefly explain this account adjustment"></textarea></label><button class="btn primary" type="submit">Save local subscription</button></form>
<a class="danger-link" href="/owner/customers/{cid}/review?action=cancel">Review local cancellation</a></section></div>
<div class="split"><section class="panel padded"><div class="section-label">ACCOUNT SNAPSHOT</div><h2>Team & billing</h2><dl class="facts"><div><dt>People</dt><dd><a href="/owner/customers/{cid}?tab=people">{int(row['user_count'])} people</a></dd></div><div><dt>Projects</dt><dd><a href="/owner/customers/{cid}?tab=projects">{int(row['project_count'])} projects</a></dd></div><div><dt>Recorded Stripe status</dt><dd>{esc(label(row['stripe_payment_status']))}</dd></div><div><dt>Last Stripe update</dt><dd>{esc(date_label(row['sub'].get('stripe_updated_at')))}</dd></div></dl></section>
<section class="panel padded"><div class="section-label">INTERNAL NOTE</div><h2>Add account context</h2><p class="helper">Visible to platform owners in this account’s history.</p><form class="stack-form" method="post" action="/owner/customers/{cid}/note">{hidden}<label>Account note<textarea name="note" rows="3" maxlength="2000" required placeholder="Capture the decision or follow-up"></textarea></label><button class="btn secondary" type="submit">Add note</button></form></section></div>
<section class="panel"><div class="panel-heading"><div><h2>Recent billing events</h2><p>Recorded events from the existing billing system.</p></div></div>{billing}</section>'''
        return self.shell(request, user, "Customer account", "Review account access and keep a clear record of your decisions.", summary + tabs + content, "customers")

    def offset(self, request, total):
        try:
            page = max(1, min(int(request.query_params.get("page", 1)), 1000000))
        except ValueError:
            page = 1
        page = min(page, max(1, (int(total) + 24) // 25))
        return (page - 1) * 25, page

    def billing_activity(self, c, cid):
        if not self.table_exists(c, "billing_events"):
            return self.empty("Billing history unavailable", "The billing history table is not available in this installation.")
        bills = c.execute("SELECT * FROM billing_events WHERE company_id=? ORDER BY id DESC LIMIT 20", (cid,)).fetchall()
        if not bills:
            return self.empty("No billing events recorded", "Payment events will appear here when the billing system records them.")
        rows = ''
        for bill in bills:
            b = dict(bill)
            rows += f'<tr><td>{esc(label(b.get("event_type")))}</td><td>{esc(label(b.get("status")))}</td><td class="numeric">{esc(money(b.get("amount_cents"), b.get("currency")))}</td><td>{esc(date_label(b.get("created")))}</td></tr>'
        return self.table(["Event", "Status", "Recorded amount", "Date"], rows, "Recent billing events")

    def review(self, request, user, csrf, row):
        action = request.query_params.get("action", "")
        explanations = {
            "suspend": ("Suspend customer access", "This revokes owner approval, suspends the local subscription if one exists, and ends any demo. People, projects, and files remain saved. Stripe billing continues until changed in Stripe."),
            "revoke": ("Revoke owner approval", "This removes owner approval and ends any demo. The local subscription and Stripe billing remain as recorded."),
            "reactivate": ("Reactivate customer account", "This sets the local subscription to Active and grants owner approval. It allows BuildCommand access without verifying a new payment. Stripe billing is not changed."),
            "cancel": ("Cancel local subscription", "This cancels the local subscription, revokes access, and ends any demo. Customer data remains saved. You must manage any Stripe billing cancellation separately."),
            "demo_deny": ("Decline or end demo", "This ends demo access. An account that also has an active subscription and owner approval can still use its paid access."),
        }
        if action not in explanations:
            raise ConsoleProblem("Choose an account action from the customer page.")
        title, explanation = explanations[action]
        cid = int(row['id'])
        destination = f"/owner/demos/{cid}/deny" if action == "demo_deny" else f"/owner/customers/{cid}/{action}"
        primary = "Confirm reactivation" if action == "reactivate" else "Confirm decision"
        body = f'''<section class="panel padded review-panel"><div class="section-label">REVIEW ACCOUNT CHANGE</div><h2>{esc(row['name'])}</h2><p class="helper">Company #{cid}</p><p class="review-description">{esc(explanation)}</p><dl class="facts"><div><dt>Current status</dt><dd>{state_badge(row['status'])}</dd></div><div><dt>Current app access</dt><dd>{esc(row['access'])}</dd></div></dl>
<form class="stack-form" method="post" action="{destination}">{self.hidden(request, csrf, row)}<label>Decision note <span class="optional">(optional)</span><textarea name="note" rows="3" maxlength="2000" placeholder="Add context for the account history"></textarea></label>
<label class="check-label"><input type="checkbox" name="confirmed" value="yes" required><span>I have reviewed the effect on this customer account.</span></label><div class="actions"><button class="btn {'primary' if action == 'reactivate' else 'danger'}" type="submit">{primary}</button><a class="btn secondary" href="/owner/customers/{cid}">Go back</a></div></form></section>'''
        return self.shell(request, user, title, "Confirm the intended change before applying it.", body, "customers")

    def demos(self, request, user, csrf, rows):
        demos = [row for row in rows if row['demo']]
        selected, total, page = self.filtered(request, demos, True)
        metrics = ''.join([self.metric("Pending review", sum(r['demo_status'] == 'PENDING_APPROVAL' for r in demos), "Waiting for your decision"), self.metric("Running demos", sum(r['demo_status'] == 'ACTIVE' for r in demos), "Approved and within seven days"), self.metric("Demo duration", "7 days", "Starts when you approve the request")])
        lines = ''
        for row in selected:
            cid, state = int(row['id']), row['demo_status']
            actions = f'<a class="text-link" href="/owner/customers/{cid}">Review account</a>'
            if state == 'PENDING_APPROVAL' and row['status'] in DEMO_ENROLLMENT_STATES | {'ACTIVE', 'LEGACY'}:
                actions += f'<form method="post" action="/owner/demos/{cid}/approve">{self.hidden(request, csrf, row)}<button class="btn primary small-btn" type="submit">Approve 7-day demo</button></form>'
            elif state == 'PENDING_APPROVAL':
                actions += '<span class="helper">Review subscription first.</span>'
            if state in {'PENDING_APPROVAL', 'ACTIVE'}:
                actions += f'<a class="text-link danger-link" href="/owner/customers/{cid}/review?action=demo_deny">{"Decline" if state == "PENDING_APPROVAL" else "End demo"}</a>'
            start_label = "Requested" if state == 'PENDING_APPROVAL' else "Started"
            expiry = "Starts after approval" if state == 'PENDING_APPROVAL' else date_label(row['demo'].get('expires_at'))
            lines += f'<tr><td><a class="account-link" href="/owner/customers/{cid}">{esc(row["name"])}</a><small>{esc(row.get("contact") or "No users")}</small></td><td>{state_badge(state)}</td><td><small>{start_label}</small>{esc(date_label(row["demo"].get("started_at")))}</td><td>{esc(expiry)}</td><td><div class="actions">{actions}</div></td></tr>'
        table = self.table(["Customer", "Demo status", "Request / start", "Expires", "Decision"], lines, "Demo requests") if lines else self.empty("No matching demo requests", "New demo requests appear here for your approval.")
        body = '<section class="metrics three">' + metrics + '</section><section class="panel">' + self.filters(request, total, True) + table + self.pager(request, total, page) + '</section>'
        return self.shell(request, user, "Demo requests", "Approve seven days of temporary access. No card or Stripe payment is required.", body, "demos")

    def billing(self, request, user, rows, plans):
        metrics, ready = self.summary(rows, plans), self.readiness()
        cards = ''.join([self.metric("Estimated monthly recurring", money(metrics['mrr_cents']), "Local active plan prices · not collections"), self.metric("Billing attention", metrics['past_due'], "Past-due or unpaid local accounts"), self.metric("Awaiting approval", metrics['awaiting_approval'], "Active accounts needing an owner decision")])
        checks = [('Payment credentials', ready['secret_key_configured']), ('Webhook credentials', ready['webhook_secret_configured']), ('Application address', ready['app_base_url_configured'])]
        connection = ''.join(f'<div><dt>{text}</dt><dd>{badge("Configured", "good") if present else badge("Missing", "warn")}</dd></div>' for text, present in checks)
        webhook = f'<p class="helper wrap-text">Webhook address: <code>{esc(ready["webhook_url"])}</code></p>' if ready['webhook_url'] else ''
        selected, total, page = self.filtered(request, rows)
        body = f'''<section class="metrics three">{cards}</section><section class="panel padded"><div class="panel-heading flush"><div><h2>Payment configuration</h2><p>Settings for the selected Stripe mode.</p></div>{badge(ready['mode'] + ' mode', 'warn' if ready['mode'] == 'TEST' else 'info')}</div><dl class="facts horizontal">{connection}</dl><p class="helper">Configured means credentials are present. It does not confirm a successful payment or a working connection.</p>{webhook}</section>
<section class="panel"><div class="panel-heading"><div><h2>Billing & account access</h2><p>Recorded payment state, local subscription status, and owner approval are shown separately.</p></div></div>{self.filters(request, total)}{self.customer_table(selected, True)}{self.pager(request, total, page)}</section>'''
        return self.shell(request, user, "Billing", "Understand account billing state without confusing access approval with payment received.", body, "billing")

    def company_tables(self, c):
        if self.postgres:
            rows = c.execute("""SELECT table_name FROM information_schema.columns
                WHERE table_schema='public' AND column_name='company_id' ORDER BY table_name""").fetchall()
            tables = [str(row['table_name']) for row in rows]
        else:
            tables = []
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                name = str(row['name'])
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                    columns = c.execute(f'PRAGMA table_info("{name}")').fetchall()
                    if any(column['name'] == 'company_id' for column in columns):
                        tables.append(name)
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table) for table in tables):
            raise ConsoleProblem("This database needs a manual account-cleanup review.", 409)
        return tables

    def cleanup_eligible(self, row):
        return not row['user_count'] and not row['project_count'] and row['status'] not in {'ACTIVE', 'LEGACY'} and row['demo_status'] != 'ACTIVE'

    def cleanup_inventory(self, c, row, tables):
        if not self.cleanup_eligible(row):
            raise ConsoleProblem("Maintenance deletion is limited to empty, inactive accounts. Suspend an account with people or projects instead.", 409)
        removable = {'company_subscriptions', 'company_access_approvals', 'company_demo_access'}
        retained_audits = {'bc_owner_console_events', 'company_access_approval_events', 'owner_subscription_events', 'owner_subscription_control_events', 'platform_admin_audit'}
        for table in tables:
            if table in removable | retained_audits:
                continue
            count = c.execute(f'SELECT COUNT(*) AS n FROM "{table}" WHERE company_id=?', (row['id'],)).fetchone()['n']
            if int(count or 0):
                raise ConsoleProblem(f"{row['name']} still has related records. Keep the account or suspend it from Customers.", 409)

    def selection(self, form):
        raw = form.getlist('company_ids')
        try:
            values = sorted({int(value) for value in raw})
            if not values or len(values) > 10 or min(values) < 1:
                raise ValueError()
            return values
        except (ValueError, TypeError):
            raise ConsoleProblem("Select between one and ten empty accounts to review.")

    def cleanup_page(self, request, user, csrf, rows):
        selected, total, page = self.filtered(request, rows)
        lines = ''
        for row in selected:
            cid = int(row['id'])
            eligible = self.cleanup_eligible(row)
            control = f'<input type="checkbox" name="company_ids" value="{cid}" aria-label="Select {esc(row["name"])}" form="cleanup-review">' if eligible else '<span class="muted">—</span>'
            note = badge('Review available', 'info') if eligible else badge('Keep / suspend')
            lines += f'<tr><td>{control}</td><td><a class="account-link" href="/owner/customers/{cid}">{esc(row["name"])}</a><small>Company #{cid}</small></td><td>{state_badge(row["status"])}</td><td>{int(row["user_count"])}</td><td>{int(row["project_count"])}</td><td>{note}</td></tr>'
        table = self.table(['Select', 'Account', 'Status', 'People', 'Projects', 'Maintenance'], lines, 'Account maintenance') if lines else self.empty('No matching accounts', 'Try another search or clear the filters.')
        body = f'''<section class="panel padded"><div class="section-label">ACCOUNT MAINTENANCE</div><h2>Keep useful records. Review empty accounts.</h2><p class="helper">Only inactive accounts with no people, projects, billing history, or other related records can be deleted here. Use Suspend access to close a populated account while keeping its records.</p><p class="helper">Platform owner companies are protected. Deletion requires a separate review and confirmation.</p></section>
<section class="panel">{self.filters(request, total)}{table}{self.pager(request, total, page)}<form id="cleanup-review" class="panel-actions" method="post" action="/owner/cleanup/review">{self.hidden(request, csrf)}<span>Select up to 10 empty accounts.</span><button class="btn secondary" type="submit">Review selected accounts {icon('arrow')}</button></form></section>'''
        return self.shell(request, user, "Maintenance", "Review unused account records with customer data protection built into the workflow.", body, "maintenance")

    def cleanup_review(self, request, form, csrf):
        selected = self.selection(form)
        with self.connection() as c:
            user = self.actor(c)
            tables = self.company_tables(c)
            rows = []
            for cid in selected:
                row, _ = self.account(c, user, cid)
                self.cleanup_inventory(c, row, tables)
                rows.append(row)
        token = self.token(request, 'cleanup', [{'id': r['id'], 'digest': self.digest(r)} for r in rows])
        fields = ''.join(f'<input type="hidden" name="company_ids" value="{cid}">' for cid in selected)
        list_html = ''.join(f'<li><b>{esc(row["name"])}</b><span>Company #{int(row["id"])} · 0 people · 0 projects</span></li>' for row in rows)
        body = f'''<section class="panel padded review-panel"><div class="section-label">REVIEW PERMANENT DELETION</div><h2>{len(rows)} empty account{'s' if len(rows) != 1 else ''}</h2><p class="review-description">The listed company records and their local subscription, approval, and demo records will be permanently removed. Historical owner audit entries are retained.</p><ul class="review-list">{list_html}</ul>
<form class="stack-form" method="post" action="/owner/cleanup/delete-selected">{self.hidden(request, csrf)}{fields}<input type="hidden" name="review_token" value="{esc(token)}"><label>Type DELETE SELECTED COMPANIES to confirm<input name="confirmation" autocomplete="off" required></label><div class="actions"><button class="btn danger" type="submit">Delete these empty accounts</button><a class="btn secondary" href="/owner/cleanup">Keep accounts</a></div></form></section>'''
        return self.shell(request, user, "Review empty accounts", "Check the exact accounts below before confirming permanent removal.", body, "maintenance")

    def cleanup_delete(self, request, form):
        selected = self.selection(form)
        if str(form.get('confirmation') or '').strip() != 'DELETE SELECTED COMPANIES':
            raise ConsoleProblem("The confirmation text did not match. No accounts were deleted.")
        expected = self.verify_token(request, str(form.get('review_token') or ''), 'cleanup')
        with self.connection(True) as c:
            user = self.actor(c)
            tables = self.company_tables(c)
            if self.postgres:
                # Older app tables lack foreign keys. Prevent concurrent writes
                # from turning an empty account into a populated one mid-delete.
                # Only this rare, explicit maintenance transaction takes these
                # short, bounded locks; ordinary console work uses row locks.
                c.execute("SET LOCAL lock_timeout = '1500ms'")
                c.execute("SET LOCAL statement_timeout = '8000ms'")
                names = ','.join('"' + name + '"' for name in sorted(set(tables) | {'companies', 'users', 'projects'}))
                c.execute(f'LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE')
            rows = []
            for cid in selected:
                self.lock_account(c, cid)
                row, _ = self.account(c, user, cid)
                self.cleanup_inventory(c, row, tables)
                rows.append(row)
            if expected != [{'id': r['id'], 'digest': self.digest(r)} for r in rows]:
                raise ConsoleProblem("The selected accounts changed. Review the selection again. No accounts were deleted.", 409)
            for row in rows:
                for table in ('company_demo_access', 'company_access_approvals', 'company_subscriptions'):
                    if self.table_exists(c, table):
                        c.execute(f'DELETE FROM {table} WHERE company_id=?', (row['id'],))
                c.execute('DELETE FROM companies WHERE id=?', (row['id'],))
                self.audit(c, user, row, 'DELETE_EMPTY_ACCOUNT', 'Deleted an explicitly reviewed empty account. No people, projects, or billing history were present.')
        return RedirectResponse('/owner/cleanup?notice=deleted', status_code=303)

    def public_customer(self, row):
        return {'company_id': int(row['id']), 'company_name': row['name'], 'plan': row['plan_code'],
                'subscription_status': row['status'], 'payment_active': row['eligible'],
                'payment_active_basis': 'Local subscription eligibility, not proof of payment.',
                'stripe_payment_status': row['stripe_payment_status'], 'owner_approved': row['approved'],
                'app_access': row['access'], 'demo_status': row['demo_status'],
                'users': int(row['user_count']), 'projects': int(row['project_count'])}

    def handle(self, request, form, csrf, user):
        path = request.url.path.rstrip('/') or '/'
        if not self.schema_ready:
            raise ConsoleProblem("The Owner Console is temporarily unavailable. Check the server log for the database setup error.", 503)
        if request.method == 'POST':
            self.form_security(request, form)
            if path == '/owner/cleanup/review':
                return HTMLResponse(self.cleanup_review(request, form, csrf))
            if path == '/owner/cleanup/delete-selected':
                return self.cleanup_delete(request, form)
            cid = self.company_id(request)
            action = path.rsplit('/', 1)[-1]
            if path.startswith('/owner/demos/'):
                return self.decide_demo(request, form, cid, action)
            return self.mutate(request, form, cid, action)
        aliases = {'/owner/subscriptions': '/owner/customers', '/owner/access-approvals': '/owner/customers?access=pending', '/owner/cleanup/preview': '/owner/cleanup', '/owner/financial': '/owner/billing'}
        if path in aliases:
            return RedirectResponse(aliases[path], status_code=303)
        with self.connection() as c:
            rows, plans = self.dataset(c, user)
            if path == '/owner/api/summary':
                return JSONResponse({'status': 'ok', 'version': OWNER_CONSOLE_VERSION, **self.summary(rows, plans)})
            if path == '/owner/api/customers':
                selected, total, page = self.filtered(request, rows)
                return JSONResponse({'status': 'ok', 'version': OWNER_CONSOLE_VERSION, 'total': total, 'page': page, 'page_size': 25, 'customers': [self.public_customer(row) for row in selected]})
            if path == '/owner/api/billing-readiness':
                return JSONResponse({'status': 'ok', 'version': OWNER_CONSOLE_VERSION, 'stripe': self.readiness(), **self.summary(rows, plans)})
            if path == '/owner/api/cleanup-preview':
                selected, total, page = self.filtered(request, rows)
                return JSONResponse({'status': 'ok', 'version': OWNER_CONSOLE_VERSION, 'total': total, 'page': page,
                                     'automatic_delete': False, 'scope': 'Empty inactive accounts only; related records checked during review.',
                                     'companies': [{**self.public_customer(row), 'can_review_cleanup': self.cleanup_eligible(row)} for row in selected]})
            if path == '/owner':
                html = self.dashboard(c, request, user, rows, plans)
            elif path == '/owner/customers':
                html = self.customers(request, user, rows)
            elif path == '/owner/demos':
                html = self.demos(request, user, csrf, rows)
            elif path == '/owner/billing':
                html = self.billing(request, user, rows, plans)
            elif path == '/owner/activity':
                body = '<section class="panel"><div class="panel-heading"><div><h2>Owner decisions & notes</h2><p>Most recent 30 events. Each customer also has an individual account history.</p></div></div>' + self.timeline(self.history(c)) + '</section>'
                html = self.shell(request, user, 'Activity', 'A clear record of decisions across customer accounts.', body, 'activity')
            elif path == '/owner/cleanup':
                html = self.cleanup_page(request, user, csrf, rows)
            elif path.startswith('/owner/customers/'):
                row, plans = self.account(c, user, self.company_id(request))
                html = self.review(request, user, csrf, row) if path.endswith('/review') else self.customer_detail(c, request, user, csrf, row, plans)
            else:
                raise ConsoleProblem('This console page could not be found.', 404)
        return HTMLResponse(html)

    def company_id(self, request):
        try:
            cid = int(request.path_params.get('company_id'))
            if cid < 1:
                raise ValueError()
            return cid
        except (TypeError, ValueError):
            raise ConsoleProblem('Choose a valid customer account.', 404)

    def health(self):
        routes = {(r.path, method) for r in self.app.routes if hasattr(r, 'path') for method in (getattr(r, 'methods', None) or ())}
        checks = {'schema_initialized': self.schema_ready}
        try:
            with self.connection() as c:
                c.execute('SELECT actor_user_id,company_id,company_name,action,detail,created FROM bc_owner_console_events LIMIT 0')
                c.execute('SELECT company_id,approved,approved_by_user_id,revoked_by_user_id,note,updated FROM company_access_approvals LIMIT 0')
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        for path, method in (('/owner', 'GET'), ('/owner/customers', 'GET'), ('/owner/demos', 'GET'), ('/owner/billing', 'GET'), ('/owner/activity', 'GET'), ('/owner/customers/{company_id}/subscription', 'POST'), ('/owner/cleanup/review', 'POST'), ('/owner/cleanup/delete-selected', 'POST')):
            checks[method + ' ' + path] = (path, method) in routes
        return {'app': 'BuildCommand AI', 'version': OWNER_CONSOLE_VERSION, 'release': RELEASE_NAME,
                'status': 'ok' if all(checks.values()) else 'degraded', 'checks': checks,
                'passed': sum(checks.values()), 'total': len(checks), 'data_reset': False,
                'scope': 'Schema and route checks only; verify account actions and owner access on staging.'}


def icon(name):
    paths = {
        'grid': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
        'building': '<path d="M4 21V6l8-3v18M12 9h8v12M2 21h20M7 8v1m0 3v1m0 3v1m9-4h1m-1 4h1"/>',
        'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        'card': '<rect x="2" y="5" width="20" height="14" rx="3"/><path d="M2 10h20M6 15h4"/>',
        'activity': '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
        'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="currentColor"/><circle cx="15" cy="17" r="3" fill="currentColor"/>',
        'arrow': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        'check': '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
        'inbox': '<path d="m4 5-2 10v5h20v-5L20 5ZM2 15h6l2 3h4l2-3h6"/>',
    }
    return f'<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths.get(name, paths["grid"])}</svg>'


STYLES = r'''
:root{--navy:#112032;--navy-soft:#203146;--ink:#172b3e;--muted:#596a7c;--line:#e1e6ec;--canvas:#f5f7f9;--amber:#edb44b;--blue:#225a86;--red:#a32c39;--green:#247150}
*{box-sizing:border-box}body.oc{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}button,input,select,textarea{font:inherit}button,a,input,select,textarea{touch-action:manipulation}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.55}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,[tabindex]:focus-visible{outline:3px solid #a77813;outline-offset:3px}.icon{width:19px;height:19px;flex-shrink:0;vertical-align:middle}.skip{position:fixed;top:-100px;left:16px;background:white;padding:12px;z-index:100}.skip:focus{top:12px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.sidebar{position:fixed;inset:0 auto 0 0;width:238px;background:var(--navy);color:#d6e0ea;padding:29px 17px 17px;display:flex;flex-direction:column;z-index:20;overflow-y:auto}.brand{display:flex;align-items:center;gap:11px;color:#fff;font-weight:650;letter-spacing:-.35px;font-size:16px;margin:0 7px 37px;line-height:1.5}.brand:hover{text-decoration:none}.brand b{color:var(--amber);font-weight:650}.brand small{font-size:9px;letter-spacing:2.1px;color:#a8b8c8;font-weight:500;margin-top:3px}.brand-mark{display:flex;align-items:center;justify-content:center;background:var(--amber);color:var(--navy);width:34px;height:39px;border-radius:8px}.brand-mark .icon{width:24px;height:24px}.nav-label{font-size:10px;letter-spacing:1.7px;margin:0 15px 11px;color:#93a7bc}.nav-link{display:flex;align-items:center;gap:12px;color:#bfccda;border-radius:8px;min-height:46px;padding:11px 14px;margin-bottom:5px;font-size:13px;font-weight:550}.nav-link:hover{background:#1a2e43;color:white;text-decoration:none}.nav-link.active{background:#2c3945;color:#f6c667;box-shadow:inset 3px 0 var(--amber)}.sidebar-bottom{margin-top:auto;padding-top:40px}.sidebar-bottom .nav-link{font-size:12px;gap:9px}.owner-profile{display:flex;align-items:center;gap:10px;border-top:1px solid #2c3b4b;padding:18px 8px 2px;margin-top:14px;color:#eef3f8;font-size:12px}.owner-profile small{font-size:11px;color:#9dadbe}.avatar{display:inline-flex;align-items:center;justify-content:center;width:33px;height:33px;border-radius:50%;background:#31465b;color:#eed29a;font-size:10px;font-weight:700;flex-shrink:0}.main-wrap{margin-left:238px;min-width:0}.topbar{height:66px;display:flex;align-items:center;justify-content:space-between;background:white;border-bottom:1px solid var(--line);padding:0 36px;font-size:12px;color:var(--muted);gap:20px}.topbar b{font-weight:550;color:var(--ink)}.divider{padding:0 13px;color:#a9b4be}.top-date{white-space:nowrap;color:var(--muted)}main{max-width:1510px;margin:auto;padding:32px 36px 20px;outline:none}.page-heading{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:26px}.eyebrow,.section-label{font-size:10px;font-weight:700;letter-spacing:1.5px;color:#876017;margin-bottom:8px}h1{font-size:30px;line-height:1.25;font-weight:650;letter-spacing:-.8px;margin:0 0 9px}h2{font-size:17px;letter-spacing:-.25px;font-weight:650;margin:0 0 6px;line-height:1.4}h3{font-size:14px;margin:0 0 6px}.page-heading p,.panel-heading p{color:var(--muted);margin:0;font-size:13px}.panel-heading p{font-size:12px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:24px}.metrics.three{grid-template-columns:repeat(3,minmax(0,1fr))}.metric{background:white;border:1px solid var(--line);border-radius:11px;padding:20px;position:relative;display:flex;flex-direction:column;min-height:136px;color:var(--ink)}a.metric:hover{border-color:#afbdcb;box-shadow:0 3px 12px #20324908;text-decoration:none}.metric-label{font-size:11px;color:var(--muted);font-weight:600;padding-right:15px}.metric strong{font-size:29px;letter-spacing:-.9px;font-weight:650;line-height:1.25;margin:10px 0 7px;font-variant-numeric:tabular-nums}.metric-note{font-size:10px;color:var(--muted)}.metric>.icon{position:absolute;right:15px;top:18px;width:14px;color:#718194}.panel{background:white;border:1px solid var(--line);border-radius:11px;margin-bottom:23px;min-width:0;overflow:hidden}.padded{padding:24px}.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px 24px 18px}.panel-heading.flush{padding:0 0 12px}.split{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:23px}.account-split{grid-template-columns:1fr 1fr}.split>.panel{margin-bottom:23px}.quick-panel{padding:26px;background:#f9fafb}.quick-panel h2{font-size:22px;max-width:290px;letter-spacing:-.55px;margin-top:16px}.quick-panel>p{font-size:12px;color:var(--muted);max-width:360px;margin:10px 0 18px}.quick-stats{display:flex;border-top:1px solid var(--line);gap:20px;justify-content:space-between;margin-top:24px;padding-top:18px}.quick-stats span{display:flex;flex-direction:column;font-size:10px;color:var(--muted)}.quick-stats b{font-size:20px;color:var(--ink);font-weight:650;margin-bottom:4px}.queue-item{display:flex;align-items:center;gap:14px;padding:16px 24px;border-top:1px solid #edf0f3;color:var(--ink)}.queue-item:hover{background:#f9fafc;text-decoration:none}.queue-item>span:first-child{flex:1;min-width:0}.queue-item strong{font-size:12px;display:block;overflow-wrap:anywhere}.queue-item small{color:var(--muted);font-size:10px;overflow-wrap:anywhere}.queue-item>.icon{width:15px;color:#728298}small{display:block}.badge{display:inline-flex;align-items:center;padding:4px 8px;border-radius:5px;background:#f0f2f5;color:#566478;font-size:10px;line-height:1.45;font-weight:600;white-space:nowrap}.badge.good{background:#e9f4ed;color:#216044}.badge.warn{background:#fbf2dd;color:#785514}.badge.bad{background:#faeaeb;color:#963340}.badge.info{background:#eaf1f8;color:#315f87}.btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;border:1px solid transparent;border-radius:7px;min-height:42px;padding:10px 15px;font-weight:600;font-size:12px;line-height:1.4;white-space:normal}.btn:hover{text-decoration:none;filter:brightness(.975)}.btn.primary{background:var(--navy);color:white;border-color:var(--navy)}.btn.secondary{background:white;color:var(--ink);border-color:#ccd5df}.btn.danger{background:#a3303d;color:white;border-color:#a3303d}.small-btn{font-size:10px;padding:8px 11px;min-height:36px}.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.actions form{margin:0}.text-link{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;min-height:34px}.text-link .icon{width:14px}.danger-link{color:var(--red);display:inline-block;font-size:12px;margin-top:14px}.actions .danger-link{margin-top:0}.clear-link{font-size:12px;padding:12px 0}.table-scroll{overflow-x:auto;overscroll-behavior-x:contain}table{width:100%;border-collapse:collapse;font-size:12px}th{background:#fafbfc;font-size:10px;letter-spacing:.35px;font-weight:600;color:var(--muted);border-top:1px solid var(--line);white-space:nowrap}th,td{padding:14px 22px;text-align:left;border-bottom:1px solid #edf0f3;vertical-align:middle}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#fcfdfe}td{overflow-wrap:anywhere}td small{font-size:10px;color:var(--muted);margin-top:4px}.account-link{font-size:12px;color:var(--ink);font-weight:650;display:inline-block;min-width:120px;max-width:300px;overflow-wrap:anywhere}.counts{white-space:nowrap;color:var(--muted);font-size:11px}.counts span{color:#adb8c2;margin:0 5px}.numeric{font-variant-numeric:tabular-nums;white-space:nowrap}.empty{text-align:center;padding:35px 20px;color:var(--muted)}.empty>.icon{height:30px;width:30px;color:#8090a2;margin-bottom:12px}.empty h3{color:var(--ink);font-size:14px}.empty p{margin:5px auto 0;font-size:12px;max-width:380px}.filters{display:flex;align-items:flex-end;gap:12px;padding:22px 24px 15px;flex-wrap:wrap}.filters label{display:flex;flex-direction:column;gap:6px;font-size:11px;font-weight:600;color:var(--muted)}.search-label{flex:1;min-width:200px}.filters input,.filters select{height:42px}.filters select{min-width:144px}input,select,textarea{background:white;border:1px solid #cbd5df;border-radius:6px;padding:9px 11px;color:var(--ink);min-width:0;width:100%;font-size:12px}input::placeholder,textarea::placeholder{color:#758595}input[type=checkbox]{width:18px;height:18px;accent-color:var(--navy);flex-shrink:0}textarea{resize:vertical;min-height:70px}.result-count{padding:0 24px 15px;color:var(--muted);font-size:10px}.pager{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-top:1px solid var(--line);gap:14px;font-size:11px;color:var(--muted)}.timeline{list-style:none;margin:0;padding:0 24px 8px}.timeline li{display:grid;grid-template-columns:10px minmax(0,1fr);gap:12px;padding:16px 0;border-top:1px solid #edf0f3}.event-dot{width:6px;height:6px;background:#ab7d2b;border-radius:50%;margin-top:7px}.event-title{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:12px}.event-title time{margin-left:auto;font-size:10px;color:var(--muted)}.event-company{color:#4d657d;font-size:11px}.timeline p{font-size:12px;margin:7px 0 4px;color:#43576b;overflow-wrap:anywhere}.timeline small{font-size:10px;color:var(--muted)}.prewrap{white-space:pre-wrap}.notice{display:flex;gap:10px;align-items:flex-start;padding:13px 15px;border-radius:7px;font-size:12px;margin:0 0 22px;border:1px solid #dbe6ef}.notice.success{color:#235d43;background:#ecf6ef;border-color:#cde3d5}.notice.info{color:#345a7b;background:#f0f5fa;margin:15px 0}.notice>.icon{width:17px;height:17px;margin-top:1px}.back-link{display:inline-block;font-size:12px;margin:0 0 15px}.account-summary{display:flex;gap:16px;align-items:center;background:white;border:1px solid var(--line);border-radius:11px;padding:23px 24px}.company-monogram{width:46px;height:46px;display:flex;align-items:center;justify-content:center;flex-shrink:0;border-radius:9px;background:#eef1f5;color:#536b83;font-size:15px;font-weight:650}.account-summary h2{font-size:21px;overflow-wrap:anywhere}.account-summary p{font-size:11px;color:var(--muted);margin:0;overflow-wrap:anywhere}.account-summary p span{margin:0 8px}.summary-badges{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}.tabs{display:flex;gap:26px;border-bottom:1px solid #d7dfe7;margin:20px 0 24px;overflow-x:auto}.tabs a{padding:11px 2px 14px;font-size:12px;color:var(--muted);font-weight:600;border-bottom:2px solid transparent;white-space:nowrap}.tabs .active{color:var(--ink);border-color:#a77923}.facts{margin:19px 0 15px}.facts>div{display:flex;justify-content:space-between;align-items:center;gap:15px;border-bottom:1px solid #edf0f3;padding:11px 0}.facts>div:last-child{border-bottom:0}.facts dt{font-size:12px;color:var(--muted)}.facts dd{margin:0;font-size:12px;text-align:right;overflow-wrap:anywhere}.facts.horizontal{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin-top:3px}.facts.horizontal>div{border:0;align-items:flex-start;flex-direction:column;gap:7px}.helper{font-size:12px;line-height:1.7;color:var(--muted);margin:9px 0 17px}.stack-form{display:flex;flex-direction:column;gap:15px}.stack-form>label{display:flex;flex-direction:column;gap:7px;font-size:12px;font-weight:600}.stack-form>.btn{align-self:flex-start}.stack-form .optional{font-weight:400;color:var(--muted)}.stack-form .check-label{flex-direction:row;gap:10px;align-items:flex-start;font-weight:400}.section-divider{height:1px;background:var(--line);margin:25px 0}.review-panel{max-width:740px}.review-description{font-size:14px;line-height:1.8;color:#40546a}.review-list{list-style:none;padding:0;margin:24px 0}.review-list li{display:flex;flex-direction:column;gap:3px;padding:14px 0;border-bottom:1px solid var(--line)}.review-list span{font-size:11px;color:var(--muted)}.panel-actions{padding:18px 24px;display:flex;align-items:center;justify-content:space-between;gap:15px;border-top:1px solid var(--line)}.panel-actions>span{font-size:12px;color:var(--muted)}.wrap-text{overflow-wrap:anywhere}code{font-size:11px}.muted{color:var(--muted)}footer{display:flex;justify-content:space-between;gap:15px;font-size:10px;color:#697b8d;max-width:1510px;margin:0 auto;padding:12px 36px 25px;border-top:1px solid var(--line)}
@media(min-width:1600px){.metric strong{font-size:33px}main{padding-top:39px}.page-heading{margin-bottom:30px}}
@media(max-width:1200px){.sidebar{width:214px;padding-left:12px;padding-right:12px}.main-wrap{margin-left:214px}.brand{font-size:14px;gap:8px}.topbar{padding:0 24px}main{padding:28px 24px 18px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.split{grid-template-columns:1fr}.quick-panel{display:none}.account-split{grid-template-columns:1fr 1fr}.metric{min-height:120px}.split>.panel{margin-bottom:0}.split{margin-bottom:23px}.metrics.three{grid-template-columns:repeat(3,minmax(0,1fr))}footer{padding-left:24px;padding-right:24px}}
@media(max-width:900px){.sidebar{position:static;width:100%;padding:16px 20px 10px;overflow:visible}.brand{margin:0 0 14px;font-size:16px}.brand small{display:none}.brand-mark{height:31px;width:31px}.sidebar nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px}.nav-link{white-space:nowrap;min-height:42px;margin:0;font-size:12px;padding:9px 12px}.nav-label,.owner-profile{display:none}.sidebar-bottom{padding:0;margin:0;position:absolute;top:13px;right:14px}.sidebar-bottom .nav-link{font-size:11px;padding:8px}.main-wrap{margin:0}.topbar{height:48px}.account-split{grid-template-columns:1fr 1fr}.summary-badges{margin-left:0}.account-summary{flex-wrap:wrap}.metric strong{font-size:28px}}
@media(max-width:650px){main{padding:24px 15px 16px}.topbar{padding:0 15px;font-size:10px}.top-date{display:none}.divider{padding:0 8px}h1{font-size:26px}.page-heading{margin-bottom:20px}.page-heading p{font-size:12px}.metrics,.metrics.three{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.metrics.three .metric:last-child{grid-column:1/-1}.metric{padding:15px;min-height:132px}.metric-label{font-size:10px}.metric-note{font-size:10px}.metric strong{font-size:26px;overflow-wrap:anywhere}.panel-heading,.padded{padding:18px}.panel-heading{align-items:flex-start}.panel-heading .text-link{white-space:nowrap}.queue-item{padding:15px 18px;gap:8px}.queue-item>.badge{font-size:9px}.queue-item>.icon{display:none}.account-split{grid-template-columns:1fr}.account-summary{padding:18px;gap:12px}.account-summary h2{font-size:18px}.account-summary>div:nth-child(2){max-width:calc(100% - 60px)}.summary-badges{width:100%;margin-top:2px}.filters{padding:18px;gap:11px}.filters label{flex:1 1 130px}.filters .search-label{flex-basis:100%}.filters .btn{flex:1 1 auto}.filters .clear-link{padding:10px 8px}.filters select{min-width:0}.result-count{padding-left:18px}.facts.horizontal{grid-template-columns:1fr;gap:0}.facts.horizontal>div{flex-direction:row;border-bottom:1px solid #edf0f3;align-items:center}.timeline{padding:0 18px 5px}.event-title time{width:100%;margin:0}.timeline li{padding:15px 0}.panel-actions{align-items:flex-start;flex-direction:column;padding:18px}.pager{padding:15px 18px}.sidebar{padding:14px 15px 9px}.sidebar-bottom .nav-link span{display:none}.sidebar-bottom .icon{width:21px}.brand{font-size:15px}.nav-link{font-size:11px;padding:9px 11px}.nav-link .icon{width:16px}.tabs{gap:23px}th,td{padding:13px 17px}.review-description{font-size:13px}footer{padding:16px 15px 24px;flex-direction:column;gap:4px}.actions .btn{min-height:44px}.review-panel .actions{align-items:stretch}.review-panel .actions .btn{flex:1 1 auto}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
'''


OWNER_ROUTES = {
    'GET': ('/owner', '/owner/customers', '/owner/customers/{company_id}', '/owner/customers/{company_id}/review',
            '/owner/demos', '/owner/billing', '/owner/activity', '/owner/cleanup', '/owner/cleanup/preview',
            '/owner/subscriptions', '/owner/access-approvals', '/owner/financial',
            '/owner/api/summary', '/owner/api/customers', '/owner/api/billing-readiness', '/owner/api/cleanup-preview'),
    'POST': tuple('/owner/customers/{company_id}/' + action for action in ACTION_LABELS)
            + ('/owner/access-approvals/{company_id}/approve', '/owner/access-approvals/{company_id}/revoke',
               '/owner/demos/{company_id}/approve', '/owner/demos/{company_id}/deny',
               '/owner/cleanup/review', '/owner/cleanup/delete-selected'),
}
HEALTH_ROUTES = ('/health/owner-console-8-5-1', '/health/owner-console-1-8-18-97',
                 '/health/owner-console-7-2-2', '/health/owner-console-7-2-3', '/health/owner-console-7-2-5',
                 '/health/customer-subscription-control-7-3-0', '/health/owner-billing-access-7-4-0',
                 '/health/owner-console-navigation-7-4-1', '/health/owner-navigation-runtime-fix-7-4-2',
                 '/health/owner-stripe-mode-7-4-3', '/health/owner-manual-approval-7-4-6', '/health/owner-demo-control-7-4-11')


def register_owner_console(app, runtime, owner_email=OWNER_EMAIL):
    console = OwnerConsole(app, runtime, owner_email)
    app.state.owner_console = console

    async def endpoint(request: Request):
        from starlette.datastructures import FormData
        from urllib.parse import parse_qsl
        csrf = request.cookies.get(CSRF_COOKIE, '')
        try:
            console.verify_token(request, csrf, 'csrf', 43200)
        except ConsoleProblem:
            csrf = console.token(request, 'csrf', secrets.token_urlsafe(24))
        try:
            user = await run_in_threadpool(console.current_actor)
            form = FormData()
            if request.method == 'POST':
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
                    raise ConsoleProblem('Submit the form on the Owner Console page.', 415)
                parts, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 32768:
                        raise ConsoleProblem('This form is too large. Shorten the account note.', 413)
                    parts.append(chunk)
                try:
                    form = FormData(parse_qsl(b''.join(parts).decode('utf-8'), keep_blank_values=True, max_num_fields=40))
                except (ValueError, UnicodeError):
                    raise ConsoleProblem('The form could not be read. Refresh the page and try again.')
                for key in form:
                    if key != 'company_ids' and len(form.getlist(key)) != 1:
                        raise ConsoleProblem('The form contains a repeated field. Refresh it and try again.')
            response = await run_in_threadpool(console.handle, request, form, csrf, user)
        except ConsoleProblem as exc:
            if exc.status == 401 and request.method == 'GET' and '/api/' not in request.url.path:
                response = RedirectResponse('/login', status_code=303)
            elif '/api/' in request.url.path:
                response = JSONResponse({'detail': exc.message}, status_code=exc.status)
            else:
                response = error_page(exc.message, exc.status)
        except Exception:
            LOG.exception('Owner Console request failed method=%s path=%s', request.method, request.url.path)
            response = JSONResponse({'detail': 'The console could not complete this request. Try again shortly.'}, status_code=503) if '/api/' in request.url.path else error_page('The console could not complete this request. Try again shortly. Account changes are saved only when the full operation succeeds.', 503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        if isinstance(response, HTMLResponse) and response.status_code == 200:
            response.set_cookie(CSRF_COOKIE, csrf, max_age=43200, path='/owner', secure=request.url.scheme == 'https', httponly=True, samesite='strict')
        return response

    def health():
        return JSONResponse(console.health(), headers={'Cache-Control': 'no-store'})

    replacements = {(path, method) for method, paths in OWNER_ROUTES.items() for path in paths}
    replacements |= {(path, 'GET') for path in HEALTH_ROUTES}
    # Retire the earlier unreviewed bulk trial deletion route.
    replacements.add(('/owner/cleanup/delete-trials', 'POST'))
    for route in list(app.router.routes):
        methods = set(getattr(route, 'methods', None) or ())
        retained = {method for method in methods if (getattr(route, 'path', ''), method) not in replacements}
        if methods != retained:
            if retained:
                route.methods = retained
            else:
                app.router.routes.remove(route)
    for method, paths in OWNER_ROUTES.items():
        for path in paths:
            name = 'owner_851_' + method.lower() + '_' + re.sub(r'[^a-z0-9]+', '_', path.lower()).strip('_')
            app.add_api_route(path, endpoint, methods=[method], name=name)
    for path in HEALTH_ROUTES:
        app.add_api_route(path, health, methods=['GET'], name='owner_health_' + path.rsplit('/', 1)[-1])
        public = getattr(runtime, 'PUBLIC_PATHS', None)
        if isinstance(public, set):
            public.add(path)
    app.openapi_schema = None
    return app


def error_page(message, status):
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Owner Console · Check your request</title><style>{STYLES}</style></head><body class="oc"><main style="max-width:700px;margin:8vh auto"><section class="panel padded"><div class="eyebrow">OWNER CONSOLE</div><h1>We couldn't complete that request</h1><p class="review-description" role="alert">{esc(message)}</p><div class="actions"><a class="btn primary" href="/owner">Open Owner Console</a><a class="btn secondary" href="/login">Sign in</a></div></section></main></body></html>''', status_code=status)
