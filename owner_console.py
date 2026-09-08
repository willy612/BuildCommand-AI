
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

OWNER_CONSOLE_VERSION = "7.2.5"
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
            "SELECT id,name,project_number FROM projects WHERE company_id=? ORDER BY id DESC LIMIT 25",
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
            f'<tr><td>{escape(str(x["name"]))}</td><td>{escape(str(x["project_number"] or "—"))}</td><td>#{int(x["id"])}</td></tr>'
            for x in projects
        ) or '<tr><td colspan="3" class="muted">No projects.</td></tr>'
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


    # ========================================================
    # BuildCommand AI 7.2.2 — Owner Subscription + Cleanup Hub
    # ========================================================

    # These business routes intentionally live here, not in full_app.py.
    for p, methods in (
        ("/owner/subscriptions", {"GET"}),
        ("/owner/cleanup", {"GET"}),
        ("/owner/cleanup/preview", {"GET"}),
        ("/owner/cleanup/delete-trials", {"POST"}),
        ("/owner/api/cleanup-preview", {"GET"}),
    ):
        remove_route(p, methods)

    def master_company_id():
        return owner_company_id()

    def is_master_company(company_id):
        oid = master_company_id()
        return oid is not None and int(company_id) == int(oid)

    def trial_cleanup_candidates():
        """Only non-master companies whose latest subscription is TRIAL.
        Companies with no subscription are not silently deleted."""
        oid = master_company_id()
        c = db()
        try:
            rows = c.execute(
                """SELECT co.id,co.name,
                          cs.status,cs.plan_code,
                          (SELECT COUNT(*) FROM users u WHERE u.company_id=co.id) user_count,
                          (SELECT COUNT(*) FROM projects p WHERE p.company_id=co.id) project_count
                   FROM companies co
                   JOIN company_subscriptions cs
                     ON cs.id=(
                        SELECT s2.id FROM company_subscriptions s2
                        WHERE s2.company_id=co.id
                        ORDER BY s2.id DESC LIMIT 1
                     )
                   WHERE UPPER(COALESCE(cs.status,''))='TRIAL'
                     AND (? IS NULL OR co.id<>?)
                   ORDER BY co.name""",
                (oid, oid)
            ).fetchall()
            return rows
        finally:
            c.close()

    def _table_exists(c, table):
        try:
            c.execute("SELECT 1 FROM " + table + " LIMIT 1")
            return True
        except Exception:
            try: c.rollback()
            except Exception: pass
            return False

    def _delete_company_business_records(c, company_id):
        """Delete known owner/business records first.
        Project/customer domain data is handled separately and conservatively."""
        for table in (
            "owner_subscription_control_events",
            "company_access_approval_events",
            "billing_events",
            "usage_events",
            "company_notes",
            "company_control_events",
            "subscription_requests",
            "company_access_approvals",
            "company_subscriptions",
        ):
            try:
                c.execute(f"DELETE FROM {table} WHERE company_id=?", (int(company_id),))
            except Exception:
                # A table may not exist in older installations.
                try: c.rollback()
                except Exception: pass

    def _project_child_tables(c):
        """PostgreSQL FK metadata for tables directly referencing projects(id)."""
        try:
            rows = c.execute("""
                SELECT tc.table_name AS child_table,
                       kcu.column_name AS child_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name=kcu.constraint_name
                 AND tc.table_schema=kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name=tc.constraint_name
                 AND ccu.table_schema=tc.table_schema
                WHERE tc.constraint_type='FOREIGN KEY'
                  AND ccu.table_name='projects'
                  AND ccu.column_name='id'
                  AND tc.table_schema='public'
            """).fetchall()
            return [(str(r["child_table"]), str(r["child_column"])) for r in rows]
        except Exception:
            try: c.rollback()
            except Exception: pass
            return []

    def delete_trial_company(company_id, actor_user_id):
        """Destructive action with hard master protection and transaction rollback."""
        if is_master_company(company_id):
            raise RuntimeError("The BuildCommand master company is permanently protected.")

        # Re-check TRIAL status at execution time.
        c = db()
        try:
            row = c.execute(
                """SELECT co.id,co.name,cs.status
                   FROM companies co
                   JOIN company_subscriptions cs
                     ON cs.id=(SELECT s2.id FROM company_subscriptions s2
                               WHERE s2.company_id=co.id ORDER BY s2.id DESC LIMIT 1)
                   WHERE co.id=?""",
                (int(company_id),)
            ).fetchone()
            if not row or str(row["status"] or "").upper() != "TRIAL":
                raise RuntimeError("Company is no longer a TRIAL account. Nothing was deleted.")

            # Project-linked data: use actual PostgreSQL FK metadata instead of guessing table names.
            project_rows = c.execute(
                "SELECT id FROM projects WHERE company_id=?",
                (int(company_id),)
            ).fetchall()
            project_ids = [int(r["id"]) for r in project_rows]

            if project_ids:
                for child_table, child_column in _project_child_tables(c):
                    # Strict identifier validation before dynamic SQL.
                    if not child_table.replace("_","").isalnum() or not child_column.replace("_","").isalnum():
                        continue
                    for pid in project_ids:
                        c.execute(
                            f'DELETE FROM "{child_table}" WHERE "{child_column}"=?',
                            (pid,)
                        )
                c.execute("DELETE FROM projects WHERE company_id=?", (int(company_id),))

            # Known business/account records.
            # Execute individually but do not swallow FK failures during the destructive transaction.
            for table in (
                "owner_subscription_control_events",
                "company_access_approval_events",
                "billing_events",
                "usage_events",
                "company_notes",
                "company_control_events",
                "subscription_requests",
                "company_access_approvals",
                "company_subscriptions",
            ):
                try:
                    c.execute(f"DELETE FROM {table} WHERE company_id=?", (int(company_id),))
                except Exception as exc:
                    # Undefined table is acceptable; other SQL errors must abort.
                    if "does not exist" not in str(exc).lower():
                        raise

            c.execute("DELETE FROM users WHERE company_id=?", (int(company_id),))
            c.execute("DELETE FROM companies WHERE id=?", (int(company_id),))
            c.commit()
            return str(row["name"])
        except Exception:
            try: c.rollback()
            except Exception: pass
            raise
        finally:
            c.close()

    @app.get("/owner/subscriptions", response_class=HTMLResponse)
    def owner_subscriptions_hub():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)
        rows = customer_rows()
        tr = ""
        oid = master_company_id()
        for r in rows:
            cid = int(r["id"])
            master = oid is not None and cid == int(oid)
            status = effective_status(subscription(cid))
            is_approved = True if master else approved(cid)
            allowed = master or (status == "ACTIVE" and is_approved)
            tr += f"""<tr>
              <td><b>{escape(str(r["name"]))}</b>{" <span class='pill good'>MASTER</span>" if master else ""}</td>
              <td>{escape(str(r["plan_code"] or "—"))}</td>
              <td><span class="pill {'good' if status=='ACTIVE' else 'warn'}">{escape(status)}</span></td>
              <td>{"YES" if is_approved else "NO"}</td>
              <td><span class="pill {'good' if allowed else 'bad'}">{"ALLOWED" if allowed else "LOCKED"}</span></td>
              <td><a class="btn secondary" href="/owner/customers/{cid}">Manage</a></td>
            </tr>"""
        body = f"""
        <div class="card">
          <div class="eyebrow">OWNER BUSINESS CONTROL</div>
          <h1>Subscriptions & Access</h1>
          <p class="muted">Customer rule: ACTIVE subscription + owner approval. The BuildCommand master company is always protected.</p>
          <div style="overflow:auto"><table>
            <tr><th>Company</th><th>Plan</th><th>Subscription</th><th>Approved</th><th>App Access</th><th></th></tr>
            {tr or '<tr><td colspan="6">No customer companies.</td></tr>'}
          </table></div>
          <div style="margin-top:16px"><a class="btn secondary" href="/owner/cleanup">Test / Trial Cleanup</a></div>
        </div>"""
        return shell("Subscriptions & Access", body)

    @app.get("/owner/cleanup", response_class=HTMLResponse)
    @app.get("/owner/cleanup/preview", response_class=HTMLResponse)
    def owner_cleanup_preview():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)
        rows = trial_cleanup_candidates()
        cards = ""
        total_users = 0
        total_projects = 0
        for r in rows:
            total_users += int(r["user_count"] or 0)
            total_projects += int(r["project_count"] or 0)
            cards += f"""<tr>
              <td><b>{escape(str(r["name"]))}</b><br><span class="muted">Company #{int(r["id"])}</span></td>
              <td>{escape(str(r["plan_code"] or "—"))}</td>
              <td><span class="pill warn">TRIAL</span></td>
              <td>{int(r["user_count"] or 0)}</td>
              <td>{int(r["project_count"] or 0)}</td>
            </tr>"""

        body = f"""
        <div class="card">
          <div class="eyebrow">OWNER ONLY · DESTRUCTIVE CONTROL</div>
          <h1>Delete Trial Companies</h1>
          <p><b>Master protection:</b> the company containing {escape(owner_email)} is excluded in code and cannot be deleted by this tool.</p>
          <p class="muted">This cleanup targets only companies whose latest subscription status is exactly TRIAL. ACTIVE, PAST_DUE, SUSPENDED, CANCELED, and companies with no subscription are not selected.</p>
          <div class="grid">
            <div class="stat"><span>Trial Companies</span><b>{len(rows)}</b></div>
            <div class="stat"><span>Users Removed</span><b>{total_users}</b></div>
            <div class="stat"><span>Test Projects Removed</span><b>{total_projects}</b></div>
          </div>
          <div style="overflow:auto;margin-top:16px"><table>
            <tr><th>Company</th><th>Plan</th><th>Status</th><th>Users</th><th>Projects</th></tr>
            {cards or '<tr><td colspan="5"><b>No non-master TRIAL companies found.</b></td></tr>'}
          </table></div>
        </div>
        <div class="card">
          <h2>Permanent deletion</h2>
          <p>This cannot be undone. Type <b>DELETE TRIAL COMPANIES</b> exactly to continue.</p>
          <form method="post" action="/owner/cleanup/delete-trials">
            <input name="confirmation" autocomplete="off" placeholder="DELETE TRIAL COMPANIES" style="width:100%;max-width:420px;padding:12px;border-radius:8px">
            <button class="btn" type="submit" style="margin-top:12px" {"disabled" if not rows else ""}>Delete Listed Trial Companies</button>
          </form>
        </div>"""
        return shell("Trial Company Cleanup", body)

    @app.post("/owner/cleanup/delete-trials")
    def owner_cleanup_delete_trials(confirmation: str = Form(...)):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)
        if str(confirmation or "").strip() != "DELETE TRIAL COMPANIES":
            return HTMLResponse("Confirmation text did not match. Nothing was deleted.", status_code=400)

        candidates = list(trial_cleanup_candidates())
        deleted = []
        failed = []
        for r in candidates:
            cid = int(r["id"])
            try:
                name = delete_trial_company(cid, int(u["id"]))
                deleted.append({"company_id": cid, "name": name})
            except Exception as exc:
                failed.append({"company_id": cid, "name": str(r["name"]), "error": str(exc)})

        # Audit the cleanup in an owner-level table that is not company-owned.
        c = db()
        try:
            c.execute("""
                CREATE TABLE IF NOT EXISTS owner_cleanup_events(
                    id BIGSERIAL PRIMARY KEY,
                    actor_user_id BIGINT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created TEXT NOT NULL
                )
            """)
            c.execute(
                """INSERT INTO owner_cleanup_events(actor_user_id,action,detail,created)
                   VALUES(?,?,?,?)""",
                (int(u["id"]), "DELETE_TRIAL_COMPANIES",
                 f"deleted={len(deleted)} failed={len(failed)}", now())
            )
            c.commit()
        finally:
            c.close()

        status = 200 if not failed else 409
        body = f"""
        <div class="card">
          <div class="eyebrow">CLEANUP RESULT</div>
          <h1>{"Cleanup Complete" if not failed else "Cleanup Partially Completed"}</h1>
          <p><b>{len(deleted)}</b> trial companies deleted. <b>{len(failed)}</b> failed safely and were rolled back individually.</p>
          <p class="muted">The BuildCommand master company was never a deletion candidate.</p>
          {"<pre>"+escape(str(failed))+"</pre>" if failed else ""}
          <a class="btn secondary" href="/owner/customers">Return to Customers</a>
        </div>"""
        return HTMLResponse(shell("Cleanup Result", body).body, status_code=status)

    @app.get("/owner/api/cleanup-preview")
    def owner_cleanup_preview_api():
        if not require_owner():
            return JSONResponse({"detail":"Platform owner access required."},status_code=403)
        oid = master_company_id()
        rows = trial_cleanup_candidates()
        return {
            "status":"ok",
            "version":"7.2.2",
            "master_company_id":oid,
            "master_email":owner_email,
            "master_protected":True,
            "delete_requires_exact_confirmation":"DELETE TRIAL COMPANIES",
            "candidates":[
                {
                    "company_id":int(r["id"]),
                    "company_name":r["name"],
                    "status":r["status"],
                    "plan":r["plan_code"],
                    "users":int(r["user_count"] or 0),
                    "projects":int(r["project_count"] or 0),
                } for r in rows
            ]
        }

    @app.get("/health/owner-console-7-2-2")
    def owner_console_722_health():
        paths = {getattr(r,"path","") for r in app.routes}
        checks = {
            "owner_dashboard":"/owner" in paths,
            "customers":"/owner/customers" in paths,
            "subscriptions":"/owner/subscriptions" in paths,
            "cleanup_preview":"/owner/cleanup" in paths,
            "cleanup_delete":"/owner/cleanup/delete-trials" in paths,
            "cleanup_api":"/owner/api/cleanup-preview" in paths,
            "master_owner_hard_protection":owner_email == "buildcommandai@gmail.com",
            "same_database":callable(getattr(runtime,"db",None)),
        }
        passed=sum(1 for v in checks.values() if v)
        return {
            "status":"ok" if passed==len(checks) else "degraded",
            "app":"BuildCommand AI",
            "version":"7.2.2",
            "release":"Owner Console Control Center + Trial Cleanup",
            "passed":passed,
            "total":len(checks),
            "failed":len(checks)-passed,
            "master_protected":True,
            "automatic_deletion_on_deploy":False,
            "checks":checks,
        }


    # ========================================================
    # BuildCommand AI 7.2.3 — Company Cleanup Center
    # Replaces trial-only cleanup with selectable non-master cleanup.
    # ========================================================

    # Remove the 7.2.2 cleanup routes before registering replacements.
    for p, methods in (
        ("/owner/cleanup", {"GET"}),
        ("/owner/cleanup/preview", {"GET"}),
        ("/owner/cleanup/delete-trials", {"POST"}),
        ("/owner/api/cleanup-preview", {"GET"}),
    ):
        remove_route(p, methods)

    def all_non_master_companies():
        oid = owner_company_id()
        c = db()
        try:
            rows = c.execute(
                """SELECT co.id,co.name,
                          COALESCE(
                            (SELECT s.status FROM company_subscriptions s
                             WHERE s.company_id=co.id ORDER BY s.id DESC LIMIT 1),
                            'NO_SUBSCRIPTION'
                          ) AS status,
                          COALESCE(
                            (SELECT s.plan_code FROM company_subscriptions s
                             WHERE s.company_id=co.id ORDER BY s.id DESC LIMIT 1),
                            ''
                          ) AS plan_code,
                          COALESCE(
                            (SELECT a.approved FROM company_access_approvals a
                             WHERE a.company_id=co.id LIMIT 1),
                            0
                          ) AS approved,
                          (SELECT COUNT(*) FROM users u WHERE u.company_id=co.id) AS user_count,
                          (SELECT COUNT(*) FROM projects p WHERE p.company_id=co.id) AS project_count
                   FROM companies co
                   WHERE (? IS NULL OR co.id<>?)
                   ORDER BY co.name""",
                (oid, oid)
            ).fetchall()
            return rows
        finally:
            c.close()

    def _safe_ident(name):
        s = str(name or "")
        return bool(s) and s.replace("_","").isalnum()

    def _tables_with_column(c, column_name):
        try:
            rows = c.execute(
                """SELECT table_name FROM information_schema.columns
                   WHERE table_schema='public' AND column_name=?
                   ORDER BY table_name""",
                (str(column_name),)
            ).fetchall()
            return [str(r["table_name"]) for r in rows if _safe_ident(r["table_name"])]
        except Exception:
            try: c.rollback()
            except Exception: pass
            return []

    def delete_non_master_company(company_id, actor_user_id):
        """Delete one explicitly selected company.
        Hard-protects the master company and uses one transaction."""
        cid = int(company_id)
        oid = owner_company_id()
        if oid is not None and cid == int(oid):
            raise RuntimeError("The BuildCommand master company cannot be deleted.")

        c = db()
        try:
            row = c.execute(
                "SELECT id,name FROM companies WHERE id=? LIMIT 1",
                (cid,)
            ).fetchone()
            if not row:
                raise RuntimeError("Company no longer exists.")

            # Re-check master protection by actual master email as a second guard.
            master_user = c.execute(
                "SELECT id FROM users WHERE company_id=? AND LOWER(email)=LOWER(?) LIMIT 1",
                (cid, owner_email)
            ).fetchone()
            if master_user:
                raise RuntimeError("The company containing the BuildCommand master email cannot be deleted.")

            project_rows = c.execute(
                "SELECT id FROM projects WHERE company_id=?",
                (cid,)
            ).fetchall()
            project_ids = [int(r["id"]) for r in project_rows]

            user_rows = c.execute(
                "SELECT id FROM users WHERE company_id=?",
                (cid,)
            ).fetchall()
            user_ids = [int(r["id"]) for r in user_rows]

            # 1) Delete rows explicitly scoped by company_id from all public tables.
            # Skip parent tables until the end.
            for table in _tables_with_column(c, "company_id"):
                if table in {"companies","projects","users"}:
                    continue
                c.execute(f'DELETE FROM "{table}" WHERE company_id=?', (cid,))

            # 2) Delete rows scoped by project_id for this company's projects.
            if project_ids:
                for table in _tables_with_column(c, "project_id"):
                    if table == "projects":
                        continue
                    for pid in project_ids:
                        c.execute(f'DELETE FROM "{table}" WHERE project_id=?', (pid,))

            # 3) Delete rows scoped by user_id / actor_user_id where present.
            # Never delete the audit record we create after successful cleanup.
            if user_ids:
                for col in ("user_id","actor_user_id","approved_by_user_id","revoked_by_user_id"):
                    for table in _tables_with_column(c, col):
                        if table in {"users","owner_cleanup_events"}:
                            continue
                        for uid in user_ids:
                            c.execute(f'DELETE FROM "{table}" WHERE "{col}"=?', (uid,))

            # 4) Parents last.
            c.execute("DELETE FROM projects WHERE company_id=?", (cid,))
            c.execute("DELETE FROM users WHERE company_id=?", (cid,))
            c.execute("DELETE FROM companies WHERE id=?", (cid,))
            c.commit()
            return str(row["name"])
        except Exception:
            try: c.rollback()
            except Exception: pass
            raise
        finally:
            c.close()

    @app.get("/owner/cleanup", response_class=HTMLResponse)
    @app.get("/owner/cleanup/preview", response_class=HTMLResponse)
    def owner_company_cleanup_center():
        if not require_owner():
            return HTMLResponse("Platform owner access required.", status_code=403)

        rows = all_non_master_companies()
        total_users = sum(int(r["user_count"] or 0) for r in rows)
        total_projects = sum(int(r["project_count"] or 0) for r in rows)

        table_rows = ""
        for r in rows:
            cid = int(r["id"])
            status = str(r["status"] or "NO_SUBSCRIPTION").upper()
            approved_value = bool(int(r["approved"] or 0))
            table_rows += f"""<tr>
              <td style="width:42px">
                <input class="company-check" type="checkbox" name="company_ids" value="{cid}" form="cleanup-form">
              </td>
              <td>
                <b>{escape(str(r["name"]))}</b><br>
                <span class="muted">Company #{cid}</span>
              </td>
              <td>{escape(str(r["plan_code"] or "—"))}</td>
              <td><span class="pill {'good' if status=='ACTIVE' else 'warn'}">{escape(status)}</span></td>
              <td>{"YES" if approved_value else "NO"}</td>
              <td>{int(r["user_count"] or 0)}</td>
              <td>{int(r["project_count"] or 0)}</td>
            </tr>"""

        body = f"""
        <div class="card">
          <div class="eyebrow">OWNER ONLY · COMPANY CLEANUP CENTER</div>
          <h1>Reset Customer Companies</h1>
          <p><b>Master protection:</b> the company containing {escape(owner_email)} is excluded from this list and blocked again inside the delete function.</p>
          <p class="muted">This page shows every non-master company, regardless of subscription status. Select only the companies you intend to permanently remove.</p>

          <div class="grid">
            <div class="stat"><span>Non-Master Companies</span><b>{len(rows)}</b></div>
            <div class="stat"><span>Users Across Them</span><b>{total_users}</b></div>
            <div class="stat"><span>Projects Across Them</span><b>{total_projects}</b></div>
          </div>

          <div style="display:flex;gap:10px;flex-wrap:wrap;margin:16px 0">
            <button type="button" class="btn secondary" onclick="setAllCompanies(true)">Select All Non-Master</button>
            <button type="button" class="btn secondary" onclick="setAllCompanies(false)">Clear Selection</button>
          </div>

          <div style="overflow:auto">
            <table>
              <tr>
                <th>Select</th><th>Company</th><th>Plan</th><th>Status</th>
                <th>Approved</th><th>Users</th><th>Projects</th>
              </tr>
              {table_rows or '<tr><td colspan="7"><b>No non-master companies found. Customer database is already clean.</b></td></tr>'}
            </table>
          </div>
        </div>

        <div class="card">
          <div class="eyebrow">PERMANENT DELETION</div>
          <h2>Delete Selected Companies</h2>
          <p>This cannot be undone. Type <b>DELETE SELECTED COMPANIES</b> exactly.</p>
          <form id="cleanup-form" method="post" action="/owner/cleanup/delete-selected">
            <input name="confirmation" autocomplete="off"
                   placeholder="DELETE SELECTED COMPANIES"
                   style="width:100%;max-width:430px;padding:12px;border-radius:8px">
            <div style="margin-top:12px">
              <button class="btn" type="submit" {"disabled" if not rows else ""}>Delete Selected Companies</button>
            </div>
          </form>
        </div>

        <script>
        function setAllCompanies(value) {{
          document.querySelectorAll('.company-check').forEach(function(cb) {{
            cb.checked = value;
          }});
        }}
        </script>
        """
        return shell("Company Cleanup Center", body)

    @app.post("/owner/cleanup/delete-selected")
    def owner_cleanup_delete_selected(
        confirmation: str = Form(...),
        company_ids: list[int] = Form(default=[])
    ):
        u = require_owner()
        if not u:
            return HTMLResponse("Platform owner access required.", status_code=403)

        if str(confirmation or "").strip() != "DELETE SELECTED COMPANIES":
            return HTMLResponse(
                "Confirmation text did not match. Nothing was deleted.",
                status_code=400
            )

        selected = []
        seen = set()
        for raw in company_ids or []:
            cid = int(raw)
            if cid not in seen:
                selected.append(cid)
                seen.add(cid)

        if not selected:
            return HTMLResponse("No companies were selected. Nothing was deleted.", status_code=400)

        oid = owner_company_id()
        if oid is not None and int(oid) in selected:
            return HTMLResponse(
                "Master company protection blocked this request. Nothing was deleted.",
                status_code=409
            )

        deleted = []
        failed = []
        for cid in selected:
            try:
                name = delete_non_master_company(cid, int(u["id"]))
                deleted.append({"company_id":cid,"name":name})
            except Exception as exc:
                failed.append({"company_id":cid,"error":str(exc)})

        # Audit after deletions, in a master-level table.
        c = db()
        try:
            c.execute("""
                CREATE TABLE IF NOT EXISTS owner_cleanup_events(
                    id BIGSERIAL PRIMARY KEY,
                    actor_user_id BIGINT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created TEXT NOT NULL
                )
            """)
            c.execute(
                """INSERT INTO owner_cleanup_events(actor_user_id,action,detail,created)
                   VALUES(?,?,?,?)""",
                (
                    int(u["id"]),
                    "DELETE_SELECTED_COMPANIES",
                    f"selected={selected}; deleted={deleted}; failed={failed}",
                    now()
                )
            )
            c.commit()
        finally:
            c.close()

        status_code = 200 if not failed else 409
        fail_html = ""
        if failed:
            fail_html = "<h3>Not deleted</h3><pre>" + escape(str(failed)) + "</pre>"

        body = f"""
        <div class="card">
          <div class="eyebrow">CLEANUP RESULT</div>
          <h1>{"Customer Reset Complete" if not failed else "Customer Reset Partially Completed"}</h1>
          <p><b>{len(deleted)}</b> companies deleted. <b>{len(failed)}</b> companies failed safely and remained in the database.</p>
          <p class="muted">The BuildCommand master company was never eligible for deletion.</p>
          {fail_html}
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <a class="btn secondary" href="/owner/cleanup">Back to Cleanup</a>
            <a class="btn secondary" href="/owner/customers">Customer List</a>
          </div>
        </div>
        """
        rendered = shell("Cleanup Result", body)
        # shell() returns HTMLResponse in this module.
        if isinstance(rendered, HTMLResponse):
            rendered.status_code = status_code
            return rendered
        return HTMLResponse(str(rendered), status_code=status_code)

    @app.get("/owner/api/cleanup-preview")
    def owner_company_cleanup_preview_api():
        if not require_owner():
            return JSONResponse({"detail":"Platform owner access required."},status_code=403)
        rows = all_non_master_companies()
        return {
            "status":"ok",
            "version":"7.2.3",
            "master_company_id":owner_company_id(),
            "master_email":owner_email,
            "master_protected":True,
            "automatic_delete":False,
            "confirmation_required":"DELETE SELECTED COMPANIES",
            "companies":[
                {
                    "company_id":int(r["id"]),
                    "company_name":r["name"],
                    "plan":r["plan_code"],
                    "status":r["status"],
                    "approved":bool(int(r["approved"] or 0)),
                    "users":int(r["user_count"] or 0),
                    "projects":int(r["project_count"] or 0),
                }
                for r in rows
            ]
        }

    @app.get("/health/owner-console-7-2-3")
    def owner_console_723_health():
        paths = {getattr(r,"path","") for r in app.routes}
        checks = {
            "owner_dashboard":"/owner" in paths,
            "customers":"/owner/customers" in paths,
            "subscriptions":"/owner/subscriptions" in paths,
            "cleanup_center":"/owner/cleanup" in paths,
            "selective_delete":"/owner/cleanup/delete-selected" in paths,
            "cleanup_preview_api":"/owner/api/cleanup-preview" in paths,
            "master_email_protected":owner_email == "buildcommandai@gmail.com",
            "same_database":callable(getattr(runtime,"db",None)),
            "old_trial_delete_removed":"/owner/cleanup/delete-trials" not in paths,
        }
        passed = sum(1 for v in checks.values() if v)
        return {
            "status":"ok" if passed==len(checks) else "degraded",
            "app":"BuildCommand AI",
            "version":"7.2.3",
            "release":"Company Cleanup Center",
            "passed":passed,
            "total":len(checks),
            "failed":len(checks)-passed,
            "master_protected":True,
            "automatic_deletion_on_deploy":False,
            "requires_selection":True,
            "checks":checks,
        }


    @app.get("/health/owner-console-7-2-5")
    def owner_console_725_health():
        paths = {getattr(r, "path", "") for r in app.routes}
        checks = {
            "owner_dashboard": "/owner" in paths,
            "customers": "/owner/customers" in paths,
            "subscriptions": "/owner/subscriptions" in paths,
            "cleanup_center_owned_here": "/owner/cleanup" in paths,
            "selective_delete_owned_here": "/owner/cleanup/delete-selected" in paths,
            "cleanup_preview_api_owned_here": "/owner/api/cleanup-preview" in paths,
            "master_email_protected": owner_email == "buildcommandai@gmail.com",
            "same_database": callable(getattr(runtime, "db", None)),
            "automatic_deletion_disabled": True,
        }
        passed = sum(1 for v in checks.values() if v)
        return {
            "status": "ok" if passed == len(checks) else "degraded",
            "app": "BuildCommand AI",
            "version": "7.2.5",
            "release": "Health Check Cleanup",
            "passed": passed,
            "total": len(checks),
            "failed": len(checks) - passed,
            "owner_business_ui": "owner_console.py",
            "customer_app": "full_app.py",
            "master_protected": True,
            "automatic_deletion_on_deploy": False,
            "checks": checks,
        }

    return app
