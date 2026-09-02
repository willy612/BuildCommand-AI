
"""
BuildCommand AI — Owner Business Console
Version 1.8.18.97

Separate business-control module for BuildCommand AI.
Uses the same FastAPI application and PostgreSQL database as full_app.py.
"""

from datetime import datetime
from html import escape
from fastapi import Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

OWNER_CONSOLE_VERSION = "1.8.18.100"
OWNER_EMAIL = "buildcommandai@gmail.com"


def register_owner_console(app, runtime, owner_email=OWNER_EMAIL):
    owner_email = (owner_email or OWNER_EMAIL).strip().lower()

    def db():
        return runtime.db()

    def now():
        return datetime.utcnow().isoformat()

    def owner_user():
        u = runtime.current_user()
        if not u:
            return None
        try:
            if runtime._bc174_is_platform_owner(u):
                return u
        except Exception:
            pass
        return u if str(u["email"] or "").strip().lower() == owner_email else None

    def require_owner():
        return owner_user()

    def remove_route(path, methods=None):
        methods = {m.upper() for m in (methods or [])}
        kept = []
        for r in app.router.routes:
            if getattr(r, "path", None) != path:
                kept.append(r)
                continue
            route_methods = {m.upper() for m in (getattr(r, "methods", None) or set())}
            if methods and not (route_methods & methods):
                kept.append(r)
        app.router.routes[:] = kept

    # Replace only the business-console surfaces. Customer construction routes stay untouched.
    for p, methods in (
        ("/owner", {"GET"}),
        ("/owner/customers", {"GET"}),
        ("/owner/customers/{company_id}", {"GET"}),
        ("/owner/customers/{company_id}/plan", {"POST"}),
        ("/owner/customers/{company_id}/status", {"POST"}),
        ("/owner/access-approvals", {"GET"}),
        ("/owner/access-approvals/{company_id}/approve", {"POST"}),
        ("/owner/access-approvals/{company_id}/revoke", {"POST"}),
        ("/owner/api/summary", {"GET"}),
        ("/owner/api/customers", {"GET"}),
    ):
        remove_route(p, methods)

    # PostgreSQL-safe schema protection. These may already exist from 1.8.18.94+.
    c = db()
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_access_approvals(
            company_id BIGINT PRIMARY KEY,
            approved INTEGER DEFAULT 0,
            approved_by_user_id BIGINT,
            approved_at TEXT,
            revoked_by_user_id BIGINT,
            revoked_at TEXT,
            note TEXT,
            created TEXT,
            updated TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_access_approval_events(
            id BIGSERIAL PRIMARY KEY,
            company_id BIGINT NOT NULL,
            actor_user_id BIGINT,
            action TEXT NOT NULL,
            detail TEXT,
            created TEXT
        )
    """)
    c.commit()
    c.close()

    def owner_company_id():
        c = db()
        row = c.execute(
            "SELECT company_id FROM users WHERE LOWER(email)=LOWER(?) LIMIT 1",
            (owner_email,)
        ).fetchone()
        c.close()
        return int(row["company_id"]) if row else None

    def plan_rows():
        c = db()
        rows = c.execute(
            "SELECT * FROM platform_plans WHERE COALESCE(active,1)=1 ORDER BY monthly_price_cents,code"
        ).fetchall()
        c.close()
        return rows

    def subscription(company_id):
        c = db()
        r = c.execute(
            "SELECT * FROM company_subscriptions WHERE company_id=? ORDER BY id DESC LIMIT 1",
            (int(company_id),)
        ).fetchone()
        c.close()
        return r

    def effective_status(sub):
        if not sub:
            return "NO_SUBSCRIPTION"
        try:
            return str(runtime._bc174_effective_status(sub) or "NO_SUBSCRIPTION").upper()
        except Exception:
            return str(sub["status"] or "NO_SUBSCRIPTION").upper()

    def approval(company_id, create=True):
        c = db()
        r = c.execute(
            "SELECT * FROM company_access_approvals WHERE company_id=?",
            (int(company_id),)
        ).fetchone()
        if not r and create:
            ts = now()
            c.execute(
                """INSERT INTO company_access_approvals
                   (company_id,approved,note,created,updated)
                   VALUES(?,?,?,?,?)""",
                (int(company_id), 0, "Awaiting platform owner approval", ts, ts)
            )
            c.commit()
            r = c.execute(
                "SELECT * FROM company_access_approvals WHERE company_id=?",
                (int(company_id),)
            ).fetchone()
        c.close()
        return r

    def approved(company_id):
        r = approval(company_id, create=True)
        try:
            return int(r["approved"] or 0) == 1
        except Exception:
            return False

    def paid(company_id):
        # Matches the front-door payment gate in the main app.
        return effective_status(subscription(company_id)) in {"ACTIVE", "LEGACY"}

    def customer_rows():
        oid = owner_company_id()
        c = db()
        rows = c.execute(
            """SELECT co.id,co.name,
                      cs.plan_code,cs.status,cs.grandfathered,
                      ca.approved,ca.approved_at,
                      (SELECT COUNT(*) FROM users u WHERE u.company_id=co.id) user_count,
                      (SELECT COUNT(*) FROM projects p WHERE p.company_id=co.id) project_count,
                      (SELECT MAX(u.created) FROM users u WHERE u.company_id=co.id) newest_user
               FROM companies co
               LEFT JOIN company_subscriptions cs ON cs.company_id=co.id
               LEFT JOIN company_access_approvals ca ON ca.company_id=co.id
               WHERE EXISTS (SELECT 1 FROM users ux WHERE ux.company_id=co.id)
               ORDER BY co.name"""
        ).fetchall()
        c.close()
        return [r for r in rows if oid is None or int(r["id"]) != oid]

    def billing_count(company_id, failed_only=False):
        c = db()
        if failed_only:
            r = c.execute(
                """SELECT COUNT(*) n FROM billing_events
                   WHERE company_id=?
                     AND (LOWER(COALESCE(status,'')) IN ('failed','past_due','unpaid')
                          OR LOWER(COALESCE(event_type,''))='payment_failed')""",
                (int(company_id),)
            ).fetchone()
        else:
            r = c.execute(
                "SELECT COUNT(*) n FROM billing_events WHERE company_id=?",
                (int(company_id),)
            ).fetchone()
        c.close()
        return int(r["n"] or 0)

    def metrics():
        plans = {str(r["code"]): r for r in plan_rows()}
        rows = customer_rows()
        mrr = active = trials = past_due = canceled = awaiting = 0
        for r in rows:
            cid = int(r["id"])
            st = effective_status(subscription(cid))
            if st in {"ACTIVE", "LEGACY"}:
                active += 1
                if st == "ACTIVE":
                    p = plans.get(str(r["plan_code"] or ""))
                    if p:
                        mrr += int(p["monthly_price_cents"] or 0)
                if not approved(cid):
                    awaiting += 1
            elif st == "TRIAL":
                trials += 1
            elif st == "PAST_DUE":
                past_due += 1
            elif st == "CANCELED":
                canceled += 1
        return {
            "customers": len(rows),
            "mrr_cents": mrr,
            "arr_cents": mrr * 12,
            "active": active,
            "trials": trials,
            "past_due": past_due,
            "canceled": canceled,
            "awaiting_approval": awaiting,
        }

    def fmt_money(cents):
        return f"${(int(cents or 0)/100):,.2f}"

    def badge(text, kind="neutral"):
        return f'<span class="badge {kind}">{escape(str(text))}</span>'

    def shell(title, body):
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · BuildCommand AI</title>
<style>
:root{{--bg:#081018;--panel:#0e1823;--panel2:#111f2d;--line:#203247;--text:#edf4fb;--muted:#8fa5bb;--gold:#f0b44d;--green:#55d68b;--red:#ff7070;--blue:#78b7ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}}
.wrap{{max-width:1420px;margin:auto;padding:26px}}
.top{{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:22px}}
.brand{{font-size:22px;font-weight:900}} .brand span{{color:var(--gold)}}
.nav a{{color:#c9d6e3;text-decoration:none;margin-left:18px;font-weight:700}}
.hero{{background:linear-gradient(135deg,#111f2d,#0b151f);border:1px solid var(--line);border-radius:18px;padding:24px;margin-bottom:18px}}
.eyebrow{{color:var(--gold);font-size:12px;letter-spacing:.13em;font-weight:900;text-transform:uppercase}}
h1{{margin:7px 0 8px;font-size:34px}} h2{{margin:0 0 13px}}
.muted,.small{{color:var(--muted)}} .small{{font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:16px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:17px}}
.kpi{{font-size:27px;font-weight:900;margin-top:4px}} .label{{font-size:12px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.06em}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:13px 10px;text-align:left;border-bottom:1px solid #1c2d40;vertical-align:middle}}
th{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}}
tr:hover td{{background:#101c28}}
.badge{{display:inline-block;padding:5px 9px;border-radius:999px;background:#1a2938;font-size:11px;font-weight:900}}
.badge.good{{background:#123322;color:#7ae2a3}} .badge.warn{{background:#362a10;color:#f3ca6b}} .badge.bad{{background:#3b171b;color:#ff9696}} .badge.info{{background:#122b45;color:#93c9ff}}
.btn,button{{display:inline-block;border:0;border-radius:9px;background:var(--gold);color:#071018;padding:9px 12px;font-weight:900;text-decoration:none;cursor:pointer}}
.btn.secondary,button.secondary{{background:#1b2b3b;color:#dce7f1;border:1px solid #30465d}}
.btn.danger,button.danger{{background:#432027;color:#ffb2b2}}
.actions{{display:flex;gap:8px;flex-wrap:wrap}}
.two{{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}}
select,input{{background:#0a141e;color:#edf4fb;border:1px solid #2a4056;border-radius:9px;padding:9px}}
form.inline{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.notice{{padding:12px;border-radius:10px;border:1px solid #29415a;background:#0a1621;margin:10px 0}}
.footer{{text-align:center;color:#667d92;font-size:12px;margin:28px 0}}
@media(max-width:1050px){{.grid{{grid-template-columns:repeat(3,1fr)}}.two{{grid-template-columns:1fr}}}}
@media(max-width:650px){{.grid{{grid-template-columns:repeat(2,1fr)}}.wrap{{padding:14px}}table{{font-size:12px}}}}
</style>
</head>
<body>
<div class="wrap">
<div class="top">
  <div class="brand">BuildCommand <span>AI</span> · Owner</div>
  <div class="nav"><a href="/owner">Dashboard</a><a href="/owner/customers">Customers</a><a href="/app">Construction App</a></div>
</div>
{body}
<div class="footer">Built By Willy LaHood © 2026 · Owner Console {OWNER_CONSOLE_VERSION}</div>
</div>
</body></html>"""

    def log_access(company_id, actor_id, action, detail):
        c = db()
        c.execute(
            """INSERT INTO company_access_approval_events
               (company_id,actor_user_id,action,detail,created)
               VALUES(?,?,?,?,?)""",
            (int(company_id), actor_id, action, detail, now())
        )
        c.commit()
        c.close()

    def company_detail(company_id):
        oid = owner_company_id()
        if oid is not None and int(company_id) == oid:
            return None
        c = db()
        co = c.execute("SELECT * FROM companies WHERE id=?", (int(company_id),)).fetchone()
        if not co:
            c.close()
            return None
        users = c.execute(
            "SELECT id,email,role,created FROM users WHERE company_id=? ORDER BY created,email",
            (int(company_id),)
        ).fetchall()
        projects = c.execute(
            "SELECT id,name FROM projects WHERE company_id=? ORDER BY id DESC LIMIT 25",
            (int(company_id),)
        ).fetchall()
        bills = c.execute(
            """SELECT * FROM billing_events WHERE company_id=?
               ORDER BY id DESC LIMIT 15""",
            (int(company_id),)
        ).fetchall()
        c.close()
        return co, users, projects, bills

    @app.get("/owner", response_class=HTMLResponse)
    def owner_dashboard():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)
        m = metrics()
        rows = customer_rows()
        customer_html = ""
        for r in rows[:20]:
            cid = int(r["id"])
            st = effective_status(subscription(cid))
            is_paid = paid(cid)
            is_approved = approved(cid)
            if st in {"ACTIVE","LEGACY"}:
                sb = badge(st, "good")
            elif st in {"PAST_DUE","CANCELED","SUSPENDED"}:
                sb = badge(st, "bad")
            elif st == "TRIAL":
                sb = badge(st, "info")
            else:
                sb = badge(st, "neutral")
            ab = badge("APPROVED","good") if is_approved else badge("AWAITING","warn")
            customer_html += f"""
            <tr>
              <td><b>{escape(str(r["name"]))}</b><div class="small">Company #{cid}</div></td>
              <td>{escape(str(r["plan_code"] or "—"))}</td>
              <td>{sb}</td>
              <td>{badge("PAID","good") if is_paid else badge("NOT ACTIVE","warn")}</td>
              <td>{ab}</td>
              <td>{int(r["user_count"] or 0)}</td>
              <td>{int(r["project_count"] or 0)}</td>
              <td><a class="btn secondary" href="/owner/customers/{cid}">Manage</a></td>
            </tr>"""
        if not customer_html:
            customer_html = '<tr><td colspan="8" class="muted">No outside customer accounts yet. New registrations will appear here automatically.</td></tr>'

        body = f"""
        <div class="hero">
          <div class="eyebrow">BuildCommand Business</div>
          <h1>Owner Business Console</h1>
          <div class="muted">Live control over customers, subscriptions, payment state and BuildCommand access.</div>
        </div>
        <div class="grid">
          <div class="card"><div class="label">MRR</div><div class="kpi">{fmt_money(m["mrr_cents"])}</div></div>
          <div class="card"><div class="label">Active Customers</div><div class="kpi">{m["active"]}</div></div>
          <div class="card"><div class="label">Awaiting Approval</div><div class="kpi">{m["awaiting_approval"]}</div></div>
          <div class="card"><div class="label">Trials</div><div class="kpi">{m["trials"]}</div></div>
          <div class="card"><div class="label">Past Due</div><div class="kpi">{m["past_due"]}</div></div>
          <div class="card"><div class="label">Total Customers</div><div class="kpi">{m["customers"]}</div></div>
        </div>
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:15px">
            <div><h2>Customers</h2><div class="muted">Only real outside accounts with a current user are counted.</div></div>
            <a class="btn" href="/owner/customers">View All Customers</a>
          </div>
          <div style="overflow:auto;margin-top:10px">
          <table>
            <tr><th>Company</th><th>Plan</th><th>Subscription</th><th>Payment</th><th>Access</th><th>Users</th><th>Projects</th><th></th></tr>
            {customer_html}
          </table>
          </div>
        </div>"""
        return shell("Owner Business Console", body)

    @app.get("/owner/customers", response_class=HTMLResponse)
    def owner_customers():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)
        rows = customer_rows()
        tr = ""
        for r in rows:
            cid = int(r["id"])
            st = effective_status(subscription(cid))
            tr += f"""
            <tr>
              <td><b>{escape(str(r["name"]))}</b><div class="small">#{cid}</div></td>
              <td>{escape(str(r["plan_code"] or "—"))}</td>
              <td>{badge(st, "good" if st in {"ACTIVE","LEGACY"} else ("bad" if st in {"PAST_DUE","CANCELED","SUSPENDED"} else "info"))}</td>
              <td>{badge("YES","good") if paid(cid) else badge("NO","warn")}</td>
              <td>{badge("APPROVED","good") if approved(cid) else badge("LOCKED","warn")}</td>
              <td>{int(r["user_count"] or 0)}</td>
              <td>{int(r["project_count"] or 0)}</td>
              <td><a class="btn secondary" href="/owner/customers/{cid}">Open Account</a></td>
            </tr>"""
        if not tr:
            tr = '<tr><td colspan="8" class="muted">No customer accounts yet.</td></tr>'
        body = f"""
        <div class="hero"><div class="eyebrow">Customer Command</div><h1>Customer Accounts</h1>
        <div class="muted">Manage every BuildCommand subscriber from one place.</div></div>
        <div class="card"><div style="overflow:auto"><table>
          <tr><th>Company</th><th>Plan</th><th>Status</th><th>Paid/Active</th><th>Access</th><th>Users</th><th>Projects</th><th></th></tr>
          {tr}
        </table></div></div>"""
        return shell("Customers", body)

    @app.get("/owner/customers/{company_id}", response_class=HTMLResponse)
    def owner_customer(company_id: int):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        detail = company_detail(company_id)
        if not detail:
            return HTMLResponse("Customer company not found.", status_code=404)
        co, users, projects, bills = detail
        sub = subscription(company_id)
        st = effective_status(sub)
        apr = approved(company_id)
        pay = paid(company_id)
        plans = plan_rows()

        plan_options = "".join(
            f'<option value="{escape(str(p["code"]))}" {"selected" if sub and str(sub["plan_code"] or "")==str(p["code"]) else ""}>{escape(str(p["name"] or p["code"]))} · {fmt_money(p["monthly_price_cents"])}/mo</option>'
            for p in plans
        )
        if not plan_options:
            plan_options = '<option value="starter">starter</option>'

        user_rows = "".join(
            f'<tr><td>{escape(str(x["email"]))}</td><td>{escape(str(x["role"] or "user"))}</td><td>{escape(str(x["created"] or "—"))}</td></tr>'
            for x in users
        ) or '<tr><td colspan="3" class="muted">No users.</td></tr>'
        project_rows = "".join(
            f'<tr><td>{escape(str(x["name"]))}</td><td>#{int(x["id"])}</td></tr>'
            for x in projects
        ) or '<tr><td colspan="2" class="muted">No projects.</td></tr>'
        bill_rows = "".join(
            f'<tr><td>{escape(str(x["event_type"] or "event"))}</td><td>{escape(str(x["status"] or "—"))}</td><td>{escape(str(x["amount_cents"] if "amount_cents" in x.keys() else "—"))}</td><td>{escape(str(x["created"] or "—"))}</td></tr>'
            for x in bills
        ) or '<tr><td colspan="4" class="muted">No billing events recorded yet.</td></tr>'

        approval_action = (
            f'<form method="post" action="/owner/access-approvals/{company_id}/revoke"><button class="danger" type="submit">Revoke Access</button></form>'
            if apr else
            f'<form method="post" action="/owner/access-approvals/{company_id}/approve"><button type="submit">Approve Access</button></form>'
        )

        body = f"""
        <div class="hero">
          <div class="eyebrow">Customer Account #{company_id}</div>
          <h1>{escape(str(co["name"]))}</h1>
          <div class="actions">
            {badge(st, "good" if st in {"ACTIVE","LEGACY"} else "warn")}
            {badge("PAYMENT ACTIVE","good") if pay else badge("PAYMENT NOT ACTIVE","warn")}
            {badge("ACCESS APPROVED","good") if apr else badge("ACCESS LOCKED","bad")}
          </div>
        </div>
        <div class="two">
          <div>
            <div class="card">
              <h2>Subscription Control</h2>
              <form class="inline" method="post" action="/owner/customers/{company_id}/plan">
                <select name="plan_code">{plan_options}</select>
                <button type="submit">Change Plan</button>
              </form>
              <div style="height:12px"></div>
              <form class="inline" method="post" action="/owner/customers/{company_id}/status">
                <select name="status">
                  {''.join(f'<option value="{s}" {"selected" if st==s else ""}>{s}</option>' for s in ["ACTIVE","TRIAL","PAST_DUE","SUSPENDED","CANCELED"])}
                </select>
                <button type="submit">Update Subscription</button>
              </form>
              <div class="notice small">ACTIVE is treated as payment-active by the front-door gate. Access still requires your separate approval.</div>
            </div>
            <div class="card" style="margin-top:14px">
              <h2>Access Control</h2>
              <div class="actions">{approval_action}<a class="btn secondary" href="/owner">Back to Dashboard</a></div>
            </div>
          </div>
          <div class="card">
            <h2>Account Snapshot</h2>
            <p><b>Users:</b> {len(users)}</p>
            <p><b>Projects:</b> {len(projects)}</p>
            <p><b>Billing events:</b> {billing_count(company_id)}</p>
            <p><b>Failed billing events:</b> {billing_count(company_id, True)}</p>
            <p><b>Access rule:</b> PAYMENT ACTIVE + OWNER APPROVED</p>
          </div>
        </div>
        <div class="two" style="margin-top:14px">
          <div class="card"><h2>Users</h2><table><tr><th>Email</th><th>Role</th><th>Created</th></tr>{user_rows}</table></div>
          <div class="card"><h2>Projects</h2><table><tr><th>Project</th><th>Number</th><th>ID</th></tr>{project_rows}</table></div>
        </div>
        <div class="card" style="margin-top:14px"><h2>Recent Billing Activity</h2>
          <div style="overflow:auto"><table><tr><th>Event</th><th>Status</th><th>Amount (cents)</th><th>Created</th></tr>{bill_rows}</table></div>
        </div>"""
        return shell(str(co["name"]), body)

    @app.post("/owner/customers/{company_id}/plan")
    def owner_set_plan(company_id: int, plan_code: str = Form(...)):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        allowed = {str(r["code"]) for r in plan_rows()}
        if plan_code not in allowed:
            return HTMLResponse("Invalid plan.", status_code=400)
        c = db()
        sub = c.execute(
            "SELECT id FROM company_subscriptions WHERE company_id=? ORDER BY id DESC LIMIT 1",
            (company_id,)
        ).fetchone()
        ts = now()
        if sub:
            c.execute(
                "UPDATE company_subscriptions SET plan_code=?,updated=? WHERE id=?",
                (plan_code, ts, sub["id"])
            )
        else:
            c.execute(
                """INSERT INTO company_subscriptions(company_id,plan_code,status,created,updated)
                   VALUES(?,?,?,?,?)""",
                (company_id, plan_code, "TRIAL", ts, ts)
            )
        c.commit()
        c.close()
        return RedirectResponse(f"/owner/customers/{company_id}", status_code=303)

    @app.post("/owner/customers/{company_id}/status")
    def owner_set_status(company_id: int, status: str = Form(...)):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        status = str(status or "").strip().upper()
        if status not in {"ACTIVE","TRIAL","PAST_DUE","SUSPENDED","CANCELED"}:
            return HTMLResponse("Invalid subscription status.", status_code=400)
        c = db()
        sub = c.execute(
            "SELECT id FROM company_subscriptions WHERE company_id=? ORDER BY id DESC LIMIT 1",
            (company_id,)
        ).fetchone()
        ts = now()
        if sub:
            c.execute(
                "UPDATE company_subscriptions SET status=?,updated=? WHERE id=?",
                (status, ts, sub["id"])
            )
        else:
            plans = plan_rows()
            code = str(plans[0]["code"]) if plans else "starter"
            c.execute(
                """INSERT INTO company_subscriptions(company_id,plan_code,status,created,updated)
                   VALUES(?,?,?,?,?)""",
                (company_id, code, status, ts, ts)
            )
        c.commit()
        c.close()
        return RedirectResponse(f"/owner/customers/{company_id}", status_code=303)

    @app.get("/owner/access-approvals", response_class=HTMLResponse)
    def access_approvals():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)
        return RedirectResponse("/owner/customers", status_code=303)

    @app.post("/owner/access-approvals/{company_id}/approve")
    def approve_access(company_id: int):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        if not paid(company_id):
            return HTMLResponse(
                "This customer does not have an ACTIVE subscription. Activate payment/subscription first, then approve access.",
                status_code=409
            )
        approval(company_id, create=True)
        c = db()
        ts = now()
        c.execute(
            """UPDATE company_access_approvals
               SET approved=1,approved_by_user_id=?,approved_at=?,
                   revoked_by_user_id=NULL,revoked_at=NULL,
                   note=?,updated=?
               WHERE company_id=?""",
            (u["id"], ts, "Approved by platform owner", ts, company_id)
        )
        c.commit()
        c.close()
        log_access(company_id, u["id"], "APPROVED", "Owner approved customer access")
        return RedirectResponse(f"/owner/customers/{company_id}", status_code=303)

    @app.post("/owner/access-approvals/{company_id}/revoke")
    def revoke_access(company_id: int):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        approval(company_id, create=True)
        c = db()
        ts = now()
        c.execute(
            """UPDATE company_access_approvals
               SET approved=0,revoked_by_user_id=?,revoked_at=?,
                   note=?,updated=?
               WHERE company_id=?""",
            (u["id"], ts, "Access revoked by platform owner", ts, company_id)
        )
        c.commit()
        c.close()
        log_access(company_id, u["id"], "REVOKED", "Owner revoked customer access")
        return RedirectResponse(f"/owner/customers/{company_id}", status_code=303)

    @app.get("/owner/api/summary")
    def owner_api_summary():
        if not require_owner():
            return JSONResponse({"detail": "Platform owner access required."}, status_code=403)
        return {"status": "ok", "version": OWNER_CONSOLE_VERSION, **metrics()}

    @app.get("/owner/api/customers")
    def owner_api_customers():
        if not require_owner():
            return JSONResponse({"detail": "Platform owner access required."}, status_code=403)
        out = []
        for r in customer_rows():
            cid = int(r["id"])
            out.append({
                "company_id": cid,
                "company_name": r["name"],
                "plan": r["plan_code"],
                "subscription_status": effective_status(subscription(cid)),
                "payment_active": paid(cid),
                "owner_approved": approved(cid),
                "users": int(r["user_count"] or 0),
                "projects": int(r["project_count"] or 0),
            })
        return {"status": "ok", "customers": out}

    @app.get("/health/owner-console-1-8-18-97")
    def owner_console_health():
        paths = {getattr(r, "path", "") for r in app.routes}
        checks = {
            "owner_dashboard": "/owner" in paths,
            "customers": "/owner/customers" in paths,
            "customer_detail": "/owner/customers/{company_id}" in paths,
            "plan_control": "/owner/customers/{company_id}/plan" in paths,
            "status_control": "/owner/customers/{company_id}/status" in paths,
            "approval_control": "/owner/access-approvals/{company_id}/approve" in paths,
            "revocation_control": "/owner/access-approvals/{company_id}/revoke" in paths,
            "owner_api": "/owner/api/summary" in paths,
            "customers_api": "/owner/api/customers" in paths,
            "same_database": callable(getattr(runtime, "db", None)),
            "owner_only": owner_email == OWNER_EMAIL,
        }
        passed = sum(1 for v in checks.values() if v)
        return {
            "status": "ok" if passed == len(checks) else "degraded",
            "version": OWNER_CONSOLE_VERSION,
            "release": "Separate Real Owner Business Console",
            "passed": passed,
            "total": len(checks),
            "checks": checks,
        }

    return app
