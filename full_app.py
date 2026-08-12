from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import sqlite3
import os
from datetime import date
from openai import OpenAI

app=FastAPI(title="BuildCommand AI",version="9.0")
DB="construction_ai_web.db"

def db():
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    return conn

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS daily_reports(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        report_date TEXT,
        weather TEXT,
        manpower INTEGER DEFAULT 0,
        work_completed TEXT,
        delays TEXT,
        deliveries TEXT,
        inspections TEXT,
        safety TEXT,
        tomorrow_plan TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS daily_report_analysis(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        report_id INTEGER,
        analysis_text TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS action_items(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        title TEXT,
        owner TEXT,
        priority TEXT,
        due TEXT,
        status TEXT DEFAULT 'OPEN',
        notes TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS app_state(
        id INTEGER PRIMARY KEY,
        selected_project_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT,number TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY,project_id INTEGER,external_id TEXT,name TEXT,trade TEXT,start TEXT,finish TEXT,pct REAL DEFAULT 0,status TEXT DEFAULT 'NOT_STARTED');
    CREATE TABLE IF NOT EXISTS make_ready(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,title TEXT,reason TEXT,due TEXT,priority TEXT,status TEXT DEFAULT 'OPEN');
    CREATE TABLE IF NOT EXISTS risks(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,score REAL,band TEXT,explanation TEXT);
    CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,trade TEXT);
    CREATE TABLE IF NOT EXISTS production(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,work_date TEXT,crew INTEGER,qty REAL,planned_qty REAL,unit TEXT);
    CREATE TABLE IF NOT EXISTS field_updates(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,update_type TEXT,text TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY,project_id INTEGER,category TEXT,insight TEXT,confidence REAL);
    CREATE TABLE IF NOT EXISTS recovery(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,scenario TEXT,days_recovered REAL,est_cost REAL,status TEXT);
    """)

    if c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"] == 0:
        c.execute(
            "INSERT INTO projects(name,number,status) VALUES(?,?,?)",
            ("Canyon Medical Office", "CMO-024", "ACTIVE")
        )
        pid = c.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]

        acts = [
            ("A100", "Footings & Foundations", "Concrete", "2026-08-10", "2026-08-18", 80, "IN_PROGRESS"),
            ("A200", "Structural Steel / Deck", "Structural", "2026-08-19", "2026-09-04", 15, "NOT_STARTED"),
            ("A300", "MEP Underground / Rough", "MEP", "2026-08-24", "2026-09-11", 5, "NOT_STARTED"),
            ("A400", "Interior Framing", "Framing", "2026-09-08", "2026-09-25", 0, "NOT_STARTED"),
            ("A500", "Drywall Close-In", "Drywall", "2026-09-22", "2026-10-09", 0, "NOT_STARTED"),
        ]
        for x in acts:
            c.execute(
                "INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,?,?)",
                (pid, *x)
            )

        ids = {
            r["external_id"]: r["id"]
            for r in c.execute(
                "SELECT id,external_id FROM activities WHERE project_id=?",
                (pid,)
            )
        }

        c.execute(
            "INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",
            (pid, ids["A300"], "Clear MEP rough-in access", "Material laydown is blocking east-side access.", "2026-08-12", "CRITICAL")
        )
        c.execute(
            "INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",
            (pid, ids["A200"], "Confirm steel delivery", "Fabricator delivery confirmation has not been received.", "2026-08-13", "HIGH")
        )

        for aid, score, band, why in [
            (ids["A300"], 82, "CRITICAL", "MEP rough-in is behind the field plan and access remains unresolved."),
            (ids["A200"], 68, "HIGH", "Steel delivery confirmation is unresolved; downstream starts are exposed."),
            (ids["A400"], 44, "WATCH", "Framing depends on MEP rough-in and inspection clearance."),
        ]:
            c.execute(
                "INSERT INTO risks(project_id,activity_id,score,band,explanation) VALUES(?,?,?,?,?)",
                (pid, aid, score, band, why)
            )

        for name, trade in [
            ("Apex Concrete", "Concrete"),
            ("Metro Steel", "Structural"),
            ("Summit MEP", "MEP"),
        ]:
            c.execute(
                "INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)",
                (pid, name, trade)
            )

        c.execute(
            "INSERT INTO memory(project_id,category,insight,confidence) VALUES(?,?,?,?)",
            (pid, "Company Memory", "Early access constraints on MEP rough-in should be cleared before manpower is increased.", .72)
        )

    if c.execute("SELECT COUNT(*) n FROM app_state").fetchone()["n"] == 0:
        first_project = c.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if first_project:
            c.execute(
                "INSERT INTO app_state(id, selected_project_id) VALUES(1, ?)",
                (first_project["id"],)
            )

    c.commit()
    c.close()


init()
@app.get("/projects/new", response_class=HTMLResponse)
def new_project_form():
    return """
    <html>
    <head>
        <title>Add Project</title>
        <style>
            body {
                background:#0a1017;
                color:white;
                font-family:Arial,sans-serif;
                padding:40px;
            }
            .box {
                max-width:500px;
                margin:auto;
                background:#111923;
                padding:30px;
                border-radius:15px;
            }
            input, select {
                width:100%;
                padding:12px;
                margin:8px 0 18px;
                box-sizing:border-box;
                border-radius:8px;
                border:1px solid #213042;
                background:#0d1620;
                color:white;
            }
            button {
                background:#f0b44d;
                border:none;
                padding:12px 20px;
                border-radius:8px;
                font-weight:bold;
                cursor:pointer;
            }
            a {
                color:#f0b44d;
            }
        </style>
    </head>

    <body>
        <div class="box">
            <h1>Add New Project</h1>

            <form method="post" action="/projects/new">

                <label>Project Name</label>
                <input
                    type="text"
                    name="name"
                    placeholder="Example: Phoenix Medical Center"
                    required
                >

                <label>Project Number</label>
                <input
                    type="text"
                    name="number"
                    placeholder="Example: PMC-001"
                    required
                >

                <label>Status</label>
                <select name="status">
                    <option value="ACTIVE">Active</option>
                    <option value="PLANNING">Planning</option>
                    <option value="ON_HOLD">On Hold</option>
                    <option value="COMPLETE">Complete</option>
                </select>

                <button type="submit">
                    Save Project
                </button>

            </form>

            <p>
                <a href="/">← Back to Dashboard</a>
            </p>
        </div>
    </body>
    </html>
    """


@app.post("/projects/new")
def create_project(
    name: str = Form(...),
    number: str = Form(...),
    status: str = Form(...)
):
    c = db()

    c.execute(
        """
        INSERT INTO projects(name, number, status)
        VALUES (?, ?, ?)
        """,
        (name, number, status)
    )

    c.commit()
    c.close()

    return RedirectResponse(
        url="/",
        status_code=303
    )
CSS="""
:root{--bg:#0a1017;--panel:#111923;--line:#213042;--text:#eef4fb;--muted:#8fa2b5;--gold:#f0b44d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.side{background:#0c141d;border-right:1px solid var(--line);padding:22px 16px}.brand{font-size:20px;font-weight:800}.company{font-size:12px;color:var(--muted);margin:5px 0 20px}.nav a{display:block;color:#cbd7e3;text-decoration:none;padding:10px;border-radius:9px;margin:2px 0}.nav a:hover{background:#162333}.creator-footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:11px;line-height:1.6}.main{padding:26px;max-width:1400px}
.hero,.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:12px}.hero h1{margin:4px 0}.eyebrow{color:var(--gold);font-size:11px;text-transform:uppercase;letter-spacing:.13em}.muted,.small{color:var(--muted)}.small{font-size:12px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kpi{font-size:28px;font-weight:800}.label{font-size:11px;color:var(--muted);text-transform:uppercase}.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-weight:800;font-size:10px}.CRITICAL,.HOLD{background:#492324;color:#ff9b9b}.HIGH,.WATCH{background:#43381b;color:#ffd779}.READY,.LOW,.COMPLETE{background:#18392c;color:#82e4b5}.OPEN{background:#1d2e44;color:#99c9ff}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}input,textarea,select{width:100%;background:#0d1620;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px}textarea{min-height:90px}button{background:var(--gold);border:0;border-radius:9px;padding:10px 14px;font-weight:800}.action{padding:12px 0;border-bottom:1px solid var(--line)}
@media(max-width:850px){.app{grid-template-columns:1fr}.grid4,.grid3,.grid2{grid-template-columns:1fr}.main{padding:14px}}
"""

NAV=[("Daily Command","/"),("Action Center","/actions"),("AI Assistant","/assistant"),("AI Analysis","/ai-analysis"),("Daily Report","/daily-report"),("Schedule","/schedule"),("Make Ready","/make-ready"),("Field","/field"),("Subcontractors","/subcontractors"),("Production","/production"),("Predictive Risk","/risk"),("Recovery","/recovery"),("Company Memory","/memory"),("Playbooks","/playbooks"),("Portfolio","/portfolio")]

def esc(x):
    return str(x or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def shell(title, body):
    current_pid = project_id()

    c = db()

    projects = c.execute(
        "SELECT id,name,number,status FROM projects ORDER BY name"
    ).fetchall()

    current = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (current_pid,)
    ).fetchone()

    c.close()

    nav = "".join(
        f'<a href="{u}">{n}</a>'
        for n, u in NAV
    )

    project_options = "".join(
        f'''
        <option value="{p["id"]}"
            {"selected" if p["id"] == current_pid else ""}>
            {esc(p["number"])} - {esc(p["name"])}
        </option>
        '''
        for p in projects
    )

    current_name = (
        esc(current["name"])
        if current
        else "No Project Selected"
    )

    selector = f'''
    <div style="margin-bottom:20px;">

        <div class="small" style="margin-bottom:6px;">
            CURRENT PROJECT
        </div>

        <form method="post" action="/projects/select">

            <select name="project_id"
                    style="margin-bottom:8px;">
                {project_options}
            </select>

            <button type="submit"
                    style="width:100%;">
                Switch Project
            </button>

        </form>

        <div style="margin-top:10px;">
            <a href="/projects/new"
               style="color:#f0b44d;
                      text-decoration:none;
                      font-weight:700;">
                + Add Project
            </a>
        </div>

    </div>
    '''

    return f'''
    <!doctype html>
    <html>

    <head>
        <meta name="viewport"
              content="width=device-width,initial-scale=1">

        <title>{esc(title)}</title>

        <style>
            {CSS}
        </style>
    </head>

    <body>

        <div class="app">

            <aside class="side">

                <div class="brand">
                    BuildCommand AI
                </div>

                <div class="company">
                    Demo Construction Company<br>
                    {current_name}
                </div>

                {selector}

                <nav class="nav">
                    {nav}
                </nav>

                <div class="creator-footer">
                    Built by Wilson LaHood<br>
                    © 2026 Wilson LaHood
                </div>

            </aside>

            <main class="main">
                {body}
            </main>

        </div>

    </body>
    </html>
    '''

def project_id():
    c = db()

    r = c.execute(
        "SELECT selected_project_id FROM app_state WHERE id=1"
    ).fetchone()

    if r and r["selected_project_id"]:
        pid = r["selected_project_id"]
    else:
        first = c.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1"
        ).fetchone()
        pid = first["id"]

    c.close()
    return pid


@app.post("/projects/select")
def select_project(project_id: int = Form(...)):
    c = db()

    exists = c.execute(
        "SELECT id FROM projects WHERE id=?",
        (project_id,)
    ).fetchone()

    if exists:
        c.execute(
            """
            INSERT INTO app_state(id, selected_project_id)
            VALUES(1, ?)
            ON CONFLICT(id)
            DO UPDATE SET selected_project_id=excluded.selected_project_id
            """,
            (project_id,)
        )
        c.commit()

    c.close()

    return RedirectResponse("/", status_code=303)
@app.get("/",response_class=HTMLResponse)
def home():
    pid=project_id(); c=db()
    actions=c.execute("SELECT m.*,a.name activity FROM make_ready m JOIN activities a ON a.id=m.activity_id WHERE m.project_id=? AND m.status='OPEN' ORDER BY due",(pid,)).fetchall()
    risks=c.execute("SELECT r.*,a.name activity FROM risks r JOIN activities a ON a.id=r.activity_id WHERE r.project_id=? ORDER BY score DESC",(pid,)).fetchall()
    act_count=c.execute("SELECT COUNT(*) n FROM activities WHERE project_id=?",(pid,)).fetchone()["n"]; c.close()
    crit=sum(r["band"]=="CRITICAL" for r in risks); high=sum(r["band"]=="HIGH" for r in risks)
    ah="".join(f'<div class="action"><span class="badge {r["priority"]}">{r["priority"]}</span> <b>{esc(r["title"])}</b><div>{esc(r["reason"])}</div><div class="small">Due {r["due"]}</div></div>' for r in actions)
    rh="".join(f'<div class="action"><span class="badge {r["band"]}">{r["band"]}</span> <b>{esc(r["activity"])}</b> · {r["score"]:.0f}/100<div class="small">{esc(r["explanation"])}</div></div>' for r in risks)
    body=f'''
<div class="hero">
    <div class="eyebrow">Daily Superintendent Command</div>

    <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
        <div>
            <h1>What needs attention today?</h1>
            <div class="muted">
                Risk, constraints, ownership and next action in one view.
            </div>
        </div>

        <a href="/projects/new"
           style="background:#f0b44d;
                  color:#0a1017;
                  text-decoration:none;
                  padding:12px 18px;
                  border-radius:9px;
                  font-weight:800;">
            + Add Project
        </a>
    </div>
</div>

<div class="grid4">
    <div class="card">
        <div class="label">Activities</div>
        <div class="kpi">{act_count}</div>
    </div>

    <div class="card">
        <div class="label">Critical risk</div>
        <div class="kpi">{crit}</div>
    </div>

    <div class="card">
        <div class="label">High risk</div>
        <div class="kpi">{high}</div>
    </div>

    <div class="card">
        <div class="label">Open make-ready</div>
        <div class="kpi">{len(actions)}</div>
    </div>
</div>

<div class="grid2">
    <div class="card">
        <h2>Handle first</h2>
        {ah}
    </div>

    <div class="card">
        <h2>What may hurt next</h2>
        {rh}
    </div>
</div>
'''
    return shell("Daily Command",body)

@app.get("/schedule",response_class=HTMLResponse)
def schedule():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); c.close()
    tr="".join(
        f'<tr>'
        f'<td>{esc(r["external_id"])}</td>'
        f'<td><b>{esc(r["name"])}</b></td>'
        f'<td>{esc(r["trade"])}</td>'
        f'<td>{r["start"]}</td>'
        f'<td>{r["finish"]}</td>'
        f'<td>{r["pct"]:.0f}%</td>'
        f'<td>{esc(r["status"])}</td>'
        f'<td><a href="/activities/{r["id"]}/edit" style="color:#f0b44d;text-decoration:none;font-weight:700;">Edit</a></td>'
        f'</tr>'
        for r in rows
    )
    return shell("Schedule",f'<div class="hero"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;"><div><div class="eyebrow">Schedule + Lookahead</div><h1>Field execution plan</h1></div><a href="/activities/new" style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">+ Add Activity</a></div></div><div class="card"><table><tr><th>ID</th><th>Activity</th><th>Trade</th><th>Start</th><th>Finish</th><th>%</th><th>Status</th><th>Action</th></tr>{tr}</table></div>')

@app.get("/make-ready",response_class=HTMLResponse)
def make_ready():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY
            CASE m.priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                ELSE 4
            END,
            m.due
        """,
        (pid,)
    ).fetchall()

    closed = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='COMPLETE'
        ORDER BY m.id DESC
        LIMIT 10
        """,
        (pid,)
    ).fetchall()

    c.close()

    open_html = "".join(
        f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {r["priority"]}">{r["priority"]}</span>
                    <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
                </div>

                <form method="post" action="/make-ready/{r["id"]}/close">
                    <button type="submit">Mark Cleared</button>
                </form>
            </div>

            <h3>{esc(r["title"])}</h3>
            <p>{esc(r["reason"])}</p>
            <div class="small">Clear by {esc(r["due"])}</div>
        </div>
        """
        for r in rows
    ) or '<div class="card"><div class="muted">No open make-ready items. Nice work.</div></div>'

    closed_html = "".join(
        f"""
        <div class="action">
            <span class="badge COMPLETE">CLEARED</span>
            <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
            <div>{esc(r["title"])}</div>
        </div>
        """
        for r in closed
    ) or '<div class="muted">No recently cleared items.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Predictive Make-Ready</div>
                <h1>Clear tomorrow's blockers before they hit the field.</h1>
            </div>

            <a href="/make-ready/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Make-Ready
            </a>
        </div>
    </div>

    <div class="grid2">
        <div>
            <h2>Open Blockers</h2>
            {open_html}
        </div>

        <div class="card">
            <h2>Recently Cleared</h2>
            {closed_html}
        </div>
    </div>
    """

    return shell("Make Ready", body)

@app.get("/field",response_class=HTMLResponse)
def field():
    pid=project_id(); c=db(); acts=c.execute("SELECT id,external_id,name FROM activities WHERE project_id=?",(pid,)).fetchall(); updates=c.execute("SELECT f.*,a.name activity FROM field_updates f JOIN activities a ON a.id=f.activity_id WHERE f.project_id=? ORDER BY f.id DESC LIMIT 15",(pid,)).fetchall(); c.close()
    opts="".join(f'<option value="{a["id"]}">{a["external_id"]} - {esc(a["name"])}</option>' for a in acts)
    recent="".join(f'<div class="action"><b>{u["update_type"]} - {esc(u["activity"])}</b><div>{esc(u["text"])}</div><div class="small">{u["created"]}</div></div>' for u in updates) or '<div class="muted">No updates yet.</div>'
    body=f'<div class="hero"><div class="eyebrow">Field Execution</div><h1>Capture what changed today.</h1></div><div class="grid2"><div class="card"><form method="post" action="/field/add"><select name="activity_id">{opts}</select><br><br><select name="update_type"><option>PROGRESS</option><option>BLOCKER</option><option>QUALITY</option><option>INSPECTION</option><option>DELIVERY</option></select><br><br><textarea name="text" placeholder="What changed in the field?"></textarea><br><br><button>Save field update</button></form></div><div class="card"><h2>Recent field updates</h2>{recent}</div></div>'
    return shell("Field",body)

@app.post("/field/add")
def add_field(activity_id:int=Form(...),update_type:str=Form(...),text:str=Form(...)):
    pid=project_id(); c=db(); c.execute("INSERT INTO field_updates(project_id,activity_id,update_type,text,created) VALUES(?,?,?,?,?)",(pid,activity_id,update_type,text,date.today().isoformat())); c.commit(); c.close()
    return RedirectResponse("/field",303)

@app.get("/subcontractors",response_class=HTMLResponse)
def subcontractors():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    updates = c.execute(
        """
        SELECT u.*, s.name sub_name, s.trade
        FROM subcontractor_updates u
        JOIN subs s ON s.id=u.sub_id
        WHERE u.project_id=?
        ORDER BY u.update_date DESC, u.id DESC
        LIMIT 25
        """,
        (pid,)
    ).fetchall()

    c.close()

    latest_by_sub = {}
    for u in updates:
        if u["sub_id"] not in latest_by_sub:
            latest_by_sub[u["sub_id"]] = u

    cards = ""

    for r in rows:
        latest = latest_by_sub.get(r["id"])

        if latest:
            status = latest["status"] or "WATCH"
            status_badge = status if status in ["CRITICAL","HIGH","WATCH","READY","LOW"] else "OPEN"

            detail = f"""
            <div class="small">Latest Update: {esc(latest["update_date"])}</div>
            <p><b>Manpower:</b> {latest["manpower"] or 0}</p>
            <p><b>Commitment:</b> {esc(latest["commitment"]) or "—"}</p>
            <p><b>Issue:</b> {esc(latest["issue"]) or "—"}</p>
            <span class="badge {status_badge}">{esc(status)}</span>
            """
        else:
            detail = '<div class="muted">No field update recorded yet.</div>'

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    <h3 style="margin-top:0;">{esc(r["name"])}</h3>
                    <div class="muted">{esc(r["trade"])}</div>
                </div>

                <a href="/subcontractors/{r["id"]}/update"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Log Update
                </a>
            </div>

            <div style="margin-top:14px;">
                {detail}
            </div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No subcontractors added yet.</div></div>'

    recent_html = "".join(
        f"""
        <div class="action">
            <span class="badge {u["status"] if u["status"] in ["CRITICAL","HIGH","WATCH","READY","LOW"] else "OPEN"}">
                {esc(u["status"])}
            </span>
            <b>{esc(u["sub_name"])}</b> · {esc(u["trade"])}
            <div class="small">{esc(u["update_date"])} · Manpower {u["manpower"] or 0}</div>
            <div>{esc(u["commitment"]) or "No commitment entered."}</div>
            <div class="small">{esc(u["issue"]) or "No issue entered."}</div>
        </div>
        """
        for u in updates[:12]
    ) or '<div class="muted">No subcontractor updates yet.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Subcontractor Intelligence</div>
                <h1>Know who is committed, who is staffed, and who needs attention.</h1>
            </div>

            <a href="/subcontractors/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Subcontractor
            </a>
        </div>
    </div>

    <div class="grid3">
        {cards}
    </div>

    <div class="card">
        <h2>Recent Trade Partner Updates</h2>
        {recent_html}
    </div>
    """

    return shell("Subcontractors", body)


@app.get("/subcontractors/{sub_id}/update", response_class=HTMLResponse)
def subcontractor_update_form(sub_id: int):
    pid = project_id()
    c = db()

    sub = c.execute(
        """
        SELECT *
        FROM subs
        WHERE id=? AND project_id=?
        """,
        (sub_id, pid)
    ).fetchone()

    c.close()

    if not sub:
        return RedirectResponse(url="/subcontractors", status_code=303)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Subcontractor Intelligence</div>
        <h1>Log Trade Partner Update</h1>
        <div class="muted">{esc(sub["name"])} · {esc(sub["trade"])}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/subcontractors/{sub_id}/update">

            <label>Update Date</label>
            <input type="date" name="update_date" value="{date.today().isoformat()}" required>

            <label>Manpower</label>
            <input type="number" name="manpower" min="0" value="0">

            <label>Commitment</label>
            <textarea
                name="commitment"
                placeholder="Example: Complete level 2 overhead rough-in by Friday."
            ></textarea>

            <label>Issue / Constraint</label>
            <textarea
                name="issue"
                placeholder="Example: Waiting on sleeves and access above ceiling."
            ></textarea>

            <label>Status</label>
            <select name="status">
                <option value="READY">Ready / On Track</option>
                <option value="WATCH">Watch</option>
                <option value="HIGH">High Concern</option>
                <option value="CRITICAL">Critical</option>
                <option value="LOW">Low Concern</option>
            </select>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Update</button>

                <a href="/subcontractors"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Subcontractor Update", body)


@app.post("/subcontractors/{sub_id}/update")
def save_subcontractor_update(
    sub_id: int,
    update_date: str = Form(...),
    manpower: int = Form(0),
    commitment: str = Form(""),
    issue: str = Form(""),
    status: str = Form("WATCH")
):
    pid = project_id()
    c = db()

    sub = c.execute(
        "SELECT id FROM subs WHERE id=? AND project_id=?",
        (sub_id, pid)
    ).fetchone()

    if sub:
        c.execute(
            """
            INSERT INTO subcontractor_updates(
                project_id,
                sub_id,
                update_date,
                manpower,
                commitment,
                issue,
                status,
                created
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                pid,
                sub_id,
                update_date,
                manpower,
                commitment.strip(),
                issue.strip(),
                status,
                date.today().isoformat()
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/subcontractors", status_code=303)


@app.get("/production",response_class=HTMLResponse)
def production():
    pid = project_id()
    c = db()

    acts = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    rows = c.execute(
        """
        SELECT p.*,a.external_id,a.name activity
        FROM production p
        JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY p.work_date DESC,p.id DESC
        LIMIT 30
        """,
        (pid,)
    ).fetchall()

    c.close()

    opts = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in acts
    )

    total_qty = sum((r["qty"] or 0) for r in rows)
    total_plan = sum((r["planned_qty"] or 0) for r in rows)

    overall_pct = 0
    if total_plan > 0:
        overall_pct = (total_qty / total_plan) * 100

    behind_count = 0
    onplan_count = 0

    tr = ""

    for r in rows:
        qty = r["qty"] or 0
        plan = r["planned_qty"] or 0

        if plan > 0:
            pct = (qty / plan) * 100
            variance = qty - plan

            if pct < 90:
                status = "BEHIND"
                badge = "CRITICAL"
                behind_count += 1
            elif pct < 100:
                status = "WATCH"
                badge = "WATCH"
            else:
                status = "ON PLAN"
                badge = "READY"
                onplan_count += 1
        else:
            pct = 0
            variance = qty
            status = "NO PLAN"
            badge = "OPEN"

        tr += f"""
        <tr>
            <td>{esc(r["work_date"])}</td>
            <td><b>{esc(r["external_id"])} - {esc(r["activity"])}</b></td>
            <td>{r["crew"] or 0}</td>
            <td>{qty:g}</td>
            <td>{plan:g}</td>
            <td>{esc(r["unit"])}</td>
            <td>{pct:.0f}%</td>
            <td>{variance:+g}</td>
            <td><span class="badge {badge}">{status}</span></td>
        </tr>
        """

    if not tr:
        tr = '<tr><td colspan="9" class="muted">No production records yet.</td></tr>'

    if not opts:
        form_html = """
        <div class="muted">
            Add a schedule activity before recording production.
        </div>
        <div style="margin-top:12px;">
            <a href="/activities/new"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                + Add Activity
            </a>
        </div>
        """
    else:
        form_html = f"""
        <form method="post" action="/production/add">
            <label>Activity</label>
            <select name="activity_id" required>{opts}</select>

            <label>Crew Size</label>
            <input name="crew" type="number" min="0" placeholder="Example: 6">

            <label>Installed Quantity</label>
            <input name="qty" type="number" step="0.1" placeholder="Example: 420">

            <label>Planned Quantity</label>
            <input name="planned_qty" type="number" step="0.1" placeholder="Example: 500">

            <label>Unit</label>
            <input name="unit" placeholder="LF / SF / CY / EA">

            <button type="submit">Record Production</button>
        </form>
        """

    body = f"""
    <div class="hero">
        <div class="eyebrow">Production Intelligence</div>
        <h1>Measure the job before the schedule update does.</h1>
        <div class="muted">
            Track actual production against plan and flag underperformance early.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Production Records</div>
            <div class="kpi">{len(rows)}</div>
        </div>

        <div class="card">
            <div class="label">Overall To Plan</div>
            <div class="kpi">{overall_pct:.0f}%</div>
        </div>

        <div class="card">
            <div class="label">Behind Plan</div>
            <div class="kpi">{behind_count}</div>
        </div>

        <div class="card">
            <div class="label">On / Above Plan</div>
            <div class="kpi">{onplan_count}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Record Production</h2>
            {form_html}
        </div>

        <div class="card">
            <h2>How BuildCommand Reads It</h2>
            <p>
                Below 90% of planned production is flagged as <b>Behind</b>.
                Between 90% and 99% is flagged as <b>Watch</b>.
                At or above plan is shown as <b>On Plan</b>.
            </p>
            <div class="small">
                These records can be used by the AI Assistant when evaluating field performance and schedule risk.
            </div>
        </div>
    </div>

    <div class="card">
        <h2>Production History</h2>

        <table>
            <tr>
                <th>Date</th>
                <th>Activity</th>
                <th>Crew</th>
                <th>Actual</th>
                <th>Plan</th>
                <th>Unit</th>
                <th>% Plan</th>
                <th>Variance</th>
                <th>Status</th>
            </tr>

            {tr}
        </table>
    </div>
    """

    return shell("Production", body)


@app.post("/production/add")
def add_prod(activity_id:int=Form(...),crew:int=Form(0),qty:float=Form(0),planned_qty:float=Form(0),unit:str=Form("")):
    pid=project_id(); c=db(); c.execute("INSERT INTO production(project_id,activity_id,work_date,crew,qty,planned_qty,unit) VALUES(?,?,?,?,?,?,?)",(pid,activity_id,date.today().isoformat(),crew,qty,planned_qty,unit)); c.commit(); c.close()
    return RedirectResponse("/production",303)

@app.get("/risk",response_class=HTMLResponse)
def risk():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    html = "".join(
        f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {r["band"]}">{r["band"]}</span>
                    <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
                </div>

                <a href="/risk/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <h3>{r["score"]:.0f}/100</h3>
            <p>{esc(r["explanation"])}</p>
        </div>
        """
        for r in rows
    ) or '<div class="card"><div class="muted">No risks entered for this project yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Predictive Risk</div>
                <h1>What is most likely to hurt the job next?</h1>
                <div class="muted">
                    Track field and schedule risk with an explainable score.
                </div>
            </div>

            <a href="/risk/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Risk
            </a>
        </div>
    </div>

    {html}
    """

    return shell("Predictive Risk", body)


@app.get("/risk/new", response_class=HTMLResponse)
def new_risk_form():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    options = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    if not options:
        body = """
        <div class="hero">
            <div class="eyebrow">Predictive Risk</div>
            <h1>Add Risk</h1>
        </div>

        <div class="card">
            <p>This project has no schedule activities yet. Add an activity first.</p>
            <a href="/activities/new"
               style="color:#f0b44d;font-weight:700;text-decoration:none;">
                + Add Activity
            </a>
        </div>
        """
        return shell("Add Risk", body)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Predictive Risk</div>
        <h1>Add Risk</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/risk/new">

            <label>Activity</label>
            <select name="activity_id" required>
                {options}
            </select>

            <div class="grid2">
                <div>
                    <label>Risk Score</label>
                    <input
                        type="number"
                        name="score"
                        min="0"
                        max="100"
                        step="1"
                        value="50"
                        required
                    >
                </div>

                <div>
                    <label>Risk Band</label>
                    <select name="band">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>
            </div>

            <label>Why Is This a Risk?</label>
            <textarea
                name="explanation"
                placeholder="Example: Material release is late and threatens the rough-in start."
                required
            ></textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Risk</button>

                <a href="/risk"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Risk", body)


@app.post("/risk/new")
def create_risk(
    activity_id: int = Form(...),
    score: float = Form(...),
    band: str = Form(...),
    explanation: str = Form(...)
):
    pid = project_id()
    score = max(0.0, min(100.0, score))

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO risks(
                project_id,
                activity_id,
                score,
                band,
                explanation
            )
            VALUES(?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                score,
                band,
                explanation.strip()
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.get("/risk/{risk_id}/edit", response_class=HTMLResponse)
def edit_risk_form(risk_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.id=? AND r.project_id=?
        """,
        (risk_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/risk", status_code=303)

    bands = ["CRITICAL", "HIGH", "WATCH", "LOW"]
    band_options = "".join(
        f'<option value="{b}" {"selected" if item["band"] == b else ""}>{b.title()}</option>'
        for b in bands
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Predictive Risk</div>
        <h1>Edit Risk</h1>
        <div class="muted">
            {esc(item["external_id"])} - {esc(item["activity"])}
        </div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/risk/{risk_id}/edit">

            <div class="grid2">
                <div>
                    <label>Risk Score</label>
                    <input
                        type="number"
                        name="score"
                        min="0"
                        max="100"
                        step="1"
                        value="{item["score"]:.0f}"
                        required
                    >
                </div>

                <div>
                    <label>Risk Band</label>
                    <select name="band">
                        {band_options}
                    </select>
                </div>
            </div>

            <label>Explanation</label>
            <textarea name="explanation" required>{esc(item["explanation"])}</textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>

                <a href="/risk"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>

        <form method="post"
              action="/risk/{risk_id}/delete"
              style="margin-top:22px;padding-top:18px;border-top:1px solid #213042;">
            <button type="submit"
                    style="background:#492324;color:#ffb0b0;">
                Delete Risk
            </button>
        </form>
    </div>
    """

    return shell("Edit Risk", body)


@app.post("/risk/{risk_id}/edit")
def edit_risk(
    risk_id: int,
    score: float = Form(...),
    band: str = Form(...),
    explanation: str = Form(...)
):
    pid = project_id()
    score = max(0.0, min(100.0, score))

    c = db()
    c.execute(
        """
        UPDATE risks
        SET score=?, band=?, explanation=?
        WHERE id=? AND project_id=?
        """,
        (
            score,
            band,
            explanation.strip(),
            risk_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.post("/risk/{risk_id}/delete")
def delete_risk(risk_id: int):
    pid = project_id()

    c = db()
    c.execute(
        "DELETE FROM risks WHERE id=? AND project_id=?",
        (risk_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.get("/recovery",response_class=HTMLResponse)
def recovery():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM recovery r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY
            CASE r.status
                WHEN 'APPROVED' THEN 1
                WHEN 'PROPOSED' THEN 2
                WHEN 'REJECTED' THEN 3
                ELSE 4
            END,
            r.days_recovered DESC,
            r.est_cost ASC
        """,
        (pid,)
    ).fetchall()

    acts = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    opts = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in acts
    )

    total_scenarios = len(rows)
    approved = sum(1 for r in rows if r["status"] == "APPROVED")
    proposed = sum(1 for r in rows if r["status"] == "PROPOSED")

    best_value = None
    best_value_score = None

    scenario_cards = ""

    for r in rows:
        days = r["days_recovered"] or 0
        cost = r["est_cost"] or 0

        if days > 0:
            cost_per_day = cost / days
            value_text = f'${cost_per_day:,.0f} / recovered day'

            if best_value_score is None or cost_per_day < best_value_score:
                best_value_score = cost_per_day
                best_value = r["id"]
        else:
            cost_per_day = None
            value_text = "No recovered days entered"

        status_badge = (
            "READY" if r["status"] == "APPROVED"
            else "HIGH" if r["status"] == "REJECTED"
            else "OPEN"
        )

        best_badge = (
            '<span class="badge READY">BEST VALUE</span>'
            if best_value == r["id"]
            else ""
        )

        scenario_cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {status_badge}">{esc(r["status"])}</span>
                    {best_badge}
                    <h3 style="margin:10px 0 4px;">{esc(r["scenario"]).replace("_"," ").title()}</h3>
                    <div class="muted">{esc(r["external_id"])} - {esc(r["activity"])}</div>
                </div>

                <form method="post" action="/recovery/{r["id"]}/delete">
                    <button type="submit" style="background:#492324;color:#ffb0b0;">
                        Delete
                    </button>
                </form>
            </div>

            <div class="grid3" style="margin-top:14px;">
                <div>
                    <div class="label">Days Recovered</div>
                    <div class="kpi">{days:.1f}</div>
                </div>

                <div>
                    <div class="label">Estimated Cost</div>
                    <div class="kpi">${cost:,.0f}</div>
                </div>

                <div>
                    <div class="label">Cost / Day</div>
                    <div style="font-size:20px;font-weight:800;">{value_text}</div>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;">
                <form method="post" action="/recovery/{r["id"]}/approve">
                    <button type="submit">Approve</button>
                </form>

                <form method="post" action="/recovery/{r["id"]}/reject">
                    <button type="submit" style="background:#43381b;color:#ffd779;">
                        Reject
                    </button>
                </form>
            </div>
        </div>
        """

    if not scenario_cards:
        scenario_cards = '<div class="card"><div class="muted">No recovery scenarios saved yet.</div></div>'

    if opts:
        form = f"""
        <div class="card">
            <h2>Test Recovery Idea</h2>

            <form method="post" action="/recovery/add">
                <label>Activity</label>
                <select name="activity_id" required>
                    {opts}
                </select>

                <label>Scenario</label>
                <select name="scenario">
                    <option value="ADD_CREW">Add Crew</option>
                    <option value="OVERTIME">Overtime</option>
                    <option value="WORK_SATURDAY">Work Saturday</option>
                    <option value="RESEQUENCE">Resequence Work</option>
                    <option value="CLEAR_CONSTRAINT">Clear Constraint</option>
                    <option value="SECOND_SHIFT">Second Shift</option>
                </select>

                <label>Modeled Days Recovered</label>
                <input
                    name="days_recovered"
                    type="number"
                    step="0.1"
                    min="0"
                    placeholder="Example: 2.5"
                >

                <label>Estimated Cost</label>
                <input
                    name="est_cost"
                    type="number"
                    step="100"
                    min="0"
                    placeholder="Example: 7500"
                >

                <button type="submit">Save Recovery Scenario</button>
            </form>
        </div>
        """
    else:
        form = """
        <div class="card">
            <h2>Test Recovery Idea</h2>
            <p>Add a schedule activity before creating a recovery scenario.</p>
            <a href="/activities/new"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                + Add Activity
            </a>
        </div>
        """

    body = f"""
    <div class="hero">
        <div class="eyebrow">Recovery Intelligence</div>
        <h1>Compare recovery before spending money.</h1>
        <div class="muted">
            Model time recovered, cost, and decision status before committing resources.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Scenarios</div>
            <div class="kpi">{total_scenarios}</div>
        </div>

        <div class="card">
            <div class="label">Proposed</div>
            <div class="kpi">{proposed}</div>
        </div>

        <div class="card">
            <div class="label">Approved</div>
            <div class="kpi">{approved}</div>
        </div>

        <div class="card">
            <div class="label">Best Cost / Day</div>
            <div class="kpi">
                {"$"+format(best_value_score,",.0f") if best_value_score is not None else "—"}
            </div>
        </div>
    </div>

    <div class="grid2">
        {form}

        <div class="card">
            <h2>How To Use Recovery Intelligence</h2>
            <p>
                Compare options by how many schedule days they recover and what each recovered day costs.
                Approve the option you intend to execute and reject scenarios you no longer want considered.
            </p>
            <div class="small">
                BuildCommand AI can use these recovery scenarios when evaluating project risk and next actions.
            </div>
        </div>
    </div>

    <div>
        <h2>Recovery Scenarios</h2>
        {scenario_cards}
    </div>
    """

    return shell("Recovery", body)


@app.post("/recovery/add")
def add_recovery(
    activity_id: int = Form(...),
    scenario: str = Form(...),
    days_recovered: float = Form(0),
    est_cost: float = Form(0)
):
    pid = project_id()

    days_recovered = max(0.0, days_recovered)
    est_cost = max(0.0, est_cost)

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO recovery(
                project_id,
                activity_id,
                scenario,
                days_recovered,
                est_cost,
                status
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                scenario,
                days_recovered,
                est_cost,
                "PROPOSED"
            )
        )
        c.commit()

    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/approve")
def approve_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE recovery
        SET status='APPROVED'
        WHERE id=? AND project_id=?
        """,
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/reject")
def reject_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE recovery
        SET status='REJECTED'
        WHERE id=? AND project_id=?
        """,
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/delete")
def delete_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        "DELETE FROM recovery WHERE id=? AND project_id=?",
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.get("/memory",response_class=HTMLResponse)
def memory():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM memory WHERE project_id=? ORDER BY confidence DESC",(pid,)).fetchall(); c.close()
    html="".join(f'<div class="card"><h3>{esc(r["category"])}</h3><p>{esc(r["insight"])}</p><div class="small">Confidence {r["confidence"]:.0%}</div></div>' for r in rows)
    return shell("Company Memory",'<div class="hero"><div class="eyebrow">Company Construction Memory</div><h1>Every project makes the next one smarter.</h1></div>'+html)

@app.get("/playbooks",response_class=HTMLResponse)
def playbooks():
    body='<div class="hero"><div class="eyebrow">Company Playbooks</div><h1>Turn repeated evidence into how your company runs work.</h1></div><div class="card"><h3>MEP Start Readiness</h3><p>Confirm manpower, material, predecessor completion, access, and inspection prerequisites. Escalate unresolved items before start.</p><div class="small">Evidence-backed company operating routine.</div></div><div class="card"><h3>Schedule Drift Response</h3><p>Verify field status, identify the controlling constraint, compare the field plan with the master schedule, test recovery, and document the agreed plan.</p></div>'
    return shell("Playbooks",body)

@app.get("/portfolio",response_class=HTMLResponse)
def portfolio():
    current_pid = project_id()
    c = db()

    projects = c.execute(
        """
        SELECT *
        FROM projects
        ORDER BY
            CASE status
                WHEN 'ACTIVE' THEN 1
                WHEN 'PLANNING' THEN 2
                WHEN 'ON_HOLD' THEN 3
                WHEN 'COMPLETE' THEN 4
                ELSE 5
            END,
            name
        """
    ).fetchall()

    cards = []
    portfolio_scores = []

    for p in projects:
        pid = p["id"]

        risks = c.execute(
            "SELECT * FROM risks WHERE project_id=?",
            (pid,)
        ).fetchall()

        mr = c.execute(
            """
            SELECT COUNT(*) n
            FROM make_ready
            WHERE project_id=? AND status='OPEN'
            """,
            (pid,)
        ).fetchone()["n"]

        activities = c.execute(
            """
            SELECT COUNT(*) n
            FROM activities
            WHERE project_id=?
            """,
            (pid,)
        ).fetchone()["n"]

        active_activities = c.execute(
            """
            SELECT COUNT(*) n
            FROM activities
            WHERE project_id=? AND status='IN_PROGRESS'
            """,
            (pid,)
        ).fetchone()["n"]

        report = c.execute(
            """
            SELECT report_date, manpower, delays
            FROM daily_reports
            WHERE project_id=?
            ORDER BY report_date DESC, id DESC
            LIMIT 1
            """,
            (pid,)
        ).fetchone()

        crit = sum(r["band"] == "CRITICAL" for r in risks)
        high = sum(r["band"] == "HIGH" for r in risks)
        watch = sum(r["band"] == "WATCH" for r in risks)

        score = min(
            100,
            crit * 30 +
            high * 15 +
            watch * 5 +
            mr * 5
        )

        portfolio_scores.append(score)

        if score >= 70:
            attention_band = "CRITICAL"
            attention_text = "Immediate Attention"
        elif score >= 40:
            attention_band = "HIGH"
            attention_text = "Needs Attention"
        elif score >= 15:
            attention_band = "WATCH"
            attention_text = "Watch"
        else:
            attention_band = "READY"
            attention_text = "Stable"

        selected_badge = (
            '<span class="badge READY">CURRENT</span>'
            if pid == current_pid
            else ""
        )

        latest_report = esc(report["report_date"]) if report else "No report"
        latest_manpower = report["manpower"] if report else 0
        latest_delay = esc(report["delays"]) if report and report["delays"] else "No delay noted"

        card = f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <div class="eyebrow">{esc(p["number"])}</div>
                    <h2 style="margin:5px 0;">{esc(p["name"])}</h2>
                    <div class="muted">{esc(p["status"]).replace("_"," ").title()}</div>
                </div>

                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    {selected_badge}
                    <span class="badge {attention_band}">{attention_text}</span>
                </div>
            </div>

            <div class="grid4" style="margin-top:16px;">
                <div>
                    <div class="label">Attention</div>
                    <div class="kpi">{score}</div>
                </div>
                <div>
                    <div class="label">Critical</div>
                    <div class="kpi">{crit}</div>
                </div>
                <div>
                    <div class="label">High</div>
                    <div class="kpi">{high}</div>
                </div>
                <div>
                    <div class="label">Make Ready</div>
                    <div class="kpi">{mr}</div>
                </div>
            </div>

            <div style="margin-top:16px;">
                <div class="small">
                    Activities: {activities} · Active: {active_activities}
                </div>
                <div class="small">
                    Latest report: {latest_report} · Manpower: {latest_manpower or 0}
                </div>
                <div class="small">
                    Latest delay signal: {latest_delay}
                </div>
            </div>

            <form method="post" action="/projects/select" style="margin-top:16px;">
                <input type="hidden" name="project_id" value="{pid}">
                <button type="submit">Open Project</button>
            </form>
        </div>
        """

        cards.append((score, card))

    c.close()

    cards.sort(key=lambda x: x[0], reverse=True)
    cards_html = "".join(card for _, card in cards)

    total_projects = len(projects)
    active_projects = sum(p["status"] == "ACTIVE" for p in projects)
    high_attention = sum(score >= 40 for score in portfolio_scores)
    avg_score = (
        sum(portfolio_scores) / len(portfolio_scores)
        if portfolio_scores
        else 0
    )

    if not cards_html:
        cards_html = '<div class="card"><div class="muted">No projects created yet.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Executive Intelligence</div>
        <h1>Which projects need intervention?</h1>
        <div class="muted">
            Portfolio-wide risk, make-ready, field reporting, and schedule attention in one view.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Projects</div>
            <div class="kpi">{total_projects}</div>
        </div>

        <div class="card">
            <div class="label">Active Projects</div>
            <div class="kpi">{active_projects}</div>
        </div>

        <div class="card">
            <div class="label">Need Attention</div>
            <div class="kpi">{high_attention}</div>
        </div>

        <div class="card">
            <div class="label">Avg Attention</div>
            <div class="kpi">{avg_score:.0f}</div>
        </div>
    </div>

    <div class="grid2">
        {cards_html}
    </div>
    """

    return shell("Portfolio", body)

@app.get("/activities/new", response_class=HTMLResponse)
def new_activity_form():
    pid = project_id()
    c = db()
    project = c.execute("SELECT name,number FROM projects WHERE id=?", (pid,)).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Schedule</div>
        <h1>Add Activity</h1>
        <div class="muted">Add a schedule activity to {project_label}.</div>
    </div>
    <div class="card" style="max-width:760px;">
        <form method="post" action="/activities/new">
            <div class="grid2">
                <div><label>Activity ID</label><input type="text" name="external_id" placeholder="Example: A600" required></div>
                <div><label>Trade</label><input type="text" name="trade" placeholder="Example: Electrical" required></div>
            </div>
            <label>Activity Name</label>
            <input type="text" name="name" placeholder="Example: Electrical Rough-In" required>
            <div class="grid2">
                <div><label>Start Date</label><input type="date" name="start" required></div>
                <div><label>Finish Date</label><input type="date" name="finish" required></div>
            </div>
            <div class="grid2">
                <div><label>Percent Complete</label><input type="number" name="pct" min="0" max="100" step="1" value="0" required></div>
                <div>
                    <label>Status</label>
                    <select name="status">
                        <option value="NOT_STARTED">Not Started</option>
                        <option value="IN_PROGRESS">In Progress</option>
                        <option value="COMPLETE">Complete</option>
                        <option value="HOLD">Hold</option>
                    </select>
                </div>
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Activity</button>
                <a href="/schedule" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>
    </div>
    """
    return shell("Add Activity", body)


@app.post("/activities/new")
def create_activity(
    external_id: str = Form(...),
    name: str = Form(...),
    trade: str = Form(...),
    start: str = Form(...),
    finish: str = Form(...),
    pct: float = Form(0),
    status: str = Form("NOT_STARTED")
):
    pid = project_id()
    pct = max(0.0, min(100.0, pct))
    c = db()
    c.execute(
        """
        INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (pid, external_id.strip(), name.strip(), trade.strip(), start, finish, pct, status)
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.get("/activities/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity_form(activity_id: int):
    pid = project_id()
    c = db()
    activity = c.execute(
        "SELECT * FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()
    c.close()

    if not activity:
        return RedirectResponse(url="/schedule", status_code=303)

    statuses = ["NOT_STARTED", "IN_PROGRESS", "COMPLETE", "HOLD"]
    options = "".join(
        f'<option value="{s}" {"selected" if activity["status"] == s else ""}>{s.replace("_"," ").title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Schedule</div>
        <h1>Edit Activity</h1>
        <div class="muted">Update schedule information for this activity.</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/activities/{activity_id}/edit">
            <div class="grid2">
                <div>
                    <label>Activity ID</label>
                    <input type="text" name="external_id" value="{esc(activity["external_id"])}" required>
                </div>
                <div>
                    <label>Trade</label>
                    <input type="text" name="trade" value="{esc(activity["trade"])}" required>
                </div>
            </div>

            <label>Activity Name</label>
            <input type="text" name="name" value="{esc(activity["name"])}" required>

            <div class="grid2">
                <div>
                    <label>Start Date</label>
                    <input type="date" name="start" value="{activity["start"]}" required>
                </div>
                <div>
                    <label>Finish Date</label>
                    <input type="date" name="finish" value="{activity["finish"]}" required>
                </div>
            </div>

            <div class="grid2">
                <div>
                    <label>Percent Complete</label>
                    <input type="number" name="pct" min="0" max="100" step="1" value="{activity["pct"]:.0f}" required>
                </div>
                <div>
                    <label>Status</label>
                    <select name="status">{options}</select>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>
                <a href="/schedule" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>

        <form method="post" action="/activities/{activity_id}/delete" style="margin-top:22px;padding-top:18px;border-top:1px solid #213042;">
            <button type="submit" style="background:#492324;color:#ffb0b0;">Delete Activity</button>
        </form>
    </div>
    """
    return shell("Edit Activity", body)


@app.post("/activities/{activity_id}/edit")
def edit_activity(
    activity_id: int,
    external_id: str = Form(...),
    name: str = Form(...),
    trade: str = Form(...),
    start: str = Form(...),
    finish: str = Form(...),
    pct: float = Form(0),
    status: str = Form("NOT_STARTED")
):
    pid = project_id()
    pct = max(0.0, min(100.0, pct))
    c = db()
    c.execute(
        """
        UPDATE activities
        SET external_id=?, name=?, trade=?, start=?, finish=?, pct=?, status=?
        WHERE id=? AND project_id=?
        """,
        (external_id.strip(), name.strip(), trade.strip(), start, finish, pct, status, activity_id, pid)
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.post("/activities/{activity_id}/delete")
def delete_activity(activity_id: int):
    pid = project_id()
    c = db()

    c.execute("DELETE FROM field_updates WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM production WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM make_ready WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM risks WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM recovery WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM activities WHERE id=? AND project_id=?", (activity_id, pid))

    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.get("/subcontractors/new", response_class=HTMLResponse)
def new_subcontractor_form():
    pid = project_id()
    c = db()
    project = c.execute("SELECT name,number FROM projects WHERE id=?", (pid,)).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Subcontractors</div>
        <h1>Add Subcontractor</h1>
        <div class="muted">Add a trade partner to {project_label}.</div>
    </div>

    <div class="card" style="max-width:680px;">
        <form method="post" action="/subcontractors/new">
            <label>Company Name</label>
            <input type="text" name="name" placeholder="Example: Valley Electric" required>

            <label>Trade</label>
            <input type="text" name="trade" placeholder="Example: Electrical" required>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Subcontractor</button>
                <a href="/subcontractors" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>
    </div>
    """
    return shell("Add Subcontractor", body)


@app.post("/subcontractors/new")
def create_subcontractor(
    name: str = Form(...),
    trade: str = Form(...)
):
    pid = project_id()
    c = db()
    c.execute(
        "INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)",
        (pid, name.strip(), trade.strip())
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/subcontractors", status_code=303)


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    reports = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 10
        """,
        (pid,)
    ).fetchall()

    analyses = c.execute(
        """
        SELECT *
        FROM daily_report_analysis
        WHERE project_id=?
        ORDER BY id DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    analysis_by_report = {}
    for a in analyses:
        if a["report_id"] not in analysis_by_report:
            analysis_by_report[a["report_id"]] = a

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    report_cards = ""

    for r in reports:
        analysis = analysis_by_report.get(r["id"])

        if analysis:
            analysis_html = (
                '<div style="margin-top:16px;padding-top:14px;border-top:1px solid #213042;">'
                '<div class="small">BUILDCOMMAND ANALYSIS</div>'
                f'<div style="margin-top:8px;line-height:1.6;">{esc(analysis["analysis_text"]).replace(chr(10), "<br>")}</div>'
                '</div>'
            )
            analyze_button = (
                f'<form method="post" action="/daily-report/{r["id"]}/analyze">'
                '<button type="submit">Analyze Again</button>'
                '</form>'
            )
        else:
            analysis_html = ""
            analyze_button = (
                f'<form method="post" action="/daily-report/{r["id"]}/analyze">'
                '<button type="submit">Analyze Report</button>'
                '</form>'
            )

        report_cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    <div class="eyebrow">Daily Report</div>
                    <h3 style="margin:6px 0;">{esc(r["report_date"])}</h3>
                    <div class="badge OPEN">{r["manpower"] or 0} workers</div>
                </div>
                <div>{analyze_button}</div>
            </div>

            <div class="small" style="margin-top:14px;">Weather</div>
            <p>{esc(r["weather"]) or "—"}</p>

            <div class="small">Work Completed</div>
            <p>{esc(r["work_completed"]) or "—"}</p>

            <div class="small">Delays / Constraints</div>
            <p>{esc(r["delays"]) or "—"}</p>

            <div class="small">Deliveries</div>
            <p>{esc(r["deliveries"]) or "—"}</p>

            <div class="small">Inspections</div>
            <p>{esc(r["inspections"]) or "—"}</p>

            <div class="small">Safety</div>
            <p>{esc(r["safety"]) or "—"}</p>

            <div class="small">Tomorrow's Plan</div>
            <p>{esc(r["tomorrow_plan"]) or "—"}</p>

            {analysis_html}
        </div>
        """

    if not report_cards:
        report_cards = '<div class="card"><div class="muted">No daily reports yet.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Daily Superintendent Report</div>
        <h1>Capture the job today so BuildCommand can analyze tomorrow.</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>New Daily Report</h2>

            <form method="post" action="/daily-report">
                <label>Report Date</label>
                <input type="date" name="report_date" value="{date.today().isoformat()}" required>

                <label>Weather</label>
                <input type="text" name="weather" placeholder="Example: Clear, 96°F">

                <label>Total Manpower</label>
                <input type="number" name="manpower" min="0" value="0">

                <label>Work Completed</label>
                <textarea name="work_completed" placeholder="What got completed today?"></textarea>

                <label>Delays / Constraints</label>
                <textarea name="delays" placeholder="Access issues, missing material, RFIs, manpower problems, etc."></textarea>

                <label>Deliveries</label>
                <textarea name="deliveries" placeholder="What arrived or failed to arrive?"></textarea>

                <label>Inspections</label>
                <textarea name="inspections" placeholder="Passed, failed, scheduled, or pending inspections."></textarea>

                <label>Safety Notes</label>
                <textarea name="safety" placeholder="Incidents, observations, toolbox talks, corrective actions."></textarea>

                <label>Tomorrow's Plan</label>
                <textarea name="tomorrow_plan" placeholder="What needs to happen tomorrow?"></textarea>

                <button type="submit">Save Daily Report</button>
            </form>
        </div>

        <div>
            <h2 style="margin-top:0;">Recent Reports</h2>
            {report_cards}
        </div>
    </div>
    """

    return shell("Daily Report", body)


@app.post("/daily-report")
def save_daily_report(
    report_date: str = Form(...),
    weather: str = Form(""),
    manpower: int = Form(0),
    work_completed: str = Form(""),
    delays: str = Form(""),
    deliveries: str = Form(""),
    inspections: str = Form(""),
    safety: str = Form(""),
    tomorrow_plan: str = Form("")
):
    pid = project_id()
    c = db()
    c.execute(
        """
        INSERT INTO daily_reports(
            project_id, report_date, weather, manpower, work_completed,
            delays, deliveries, inspections, safety, tomorrow_plan, created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            report_date,
            weather.strip(),
            manpower,
            work_completed.strip(),
            delays.strip(),
            deliveries.strip(),
            inspections.strip(),
            safety.strip(),
            tomorrow_plan.strip(),
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/daily-report", status_code=303)


@app.get("/ai-analysis", response_class=HTMLResponse)
def ai_analysis():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    risks = c.execute(
        """
        SELECT r.*, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    make_ready_items = c.execute(
        """
        SELECT m.*, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY due
        """,
        (pid,)
    ).fetchall()

    report = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 1
        """,
        (pid,)
    ).fetchone()

    c.close()

    project_name = esc(project["name"]) if project else "Current Project"

    critical = [r for r in risks if r["band"] == "CRITICAL"]
    high = [r for r in risks if r["band"] == "HIGH"]
    in_progress = [a for a in activities if a["status"] == "IN_PROGRESS"]

    top_actions = []

    for r in critical[:3]:
        top_actions.append(
            f'CRITICAL: {esc(r["activity"])} — {esc(r["explanation"])}'
        )

    for item in make_ready_items[:3]:
        top_actions.append(
            f'MAKE READY: {esc(item["title"])} — due {esc(item["due"])}'
        )

    if report:
        delays = (report["delays"] or "").strip()
        inspections = (report["inspections"] or "").strip()
        tomorrow = (report["tomorrow_plan"] or "").strip()

        if delays:
            top_actions.append(f'FIELD DELAY: {esc(delays)}')

        if inspections:
            top_actions.append(f'INSPECTION: {esc(inspections)}')

        if tomorrow:
            top_actions.append(f'TOMORROW: {esc(tomorrow)}')

    if not top_actions:
        top_actions.append(
            "No immediate high-priority issues were found in the current project data."
        )

    action_html = "".join(
        f'<div class="action">{item}</div>'
        for item in top_actions[:7]
    )

    if report:
        report_summary = (
            f'<div class="small">Latest Report: {esc(report["report_date"])}</div>'
            f'<p><b>Manpower:</b> {report["manpower"] or 0}</p>'
            f'<p><b>Work Completed:</b> {esc(report["work_completed"]) or "—"}</p>'
            f'<p><b>Delays:</b> {esc(report["delays"]) or "—"}</p>'
            f'<p><b>Tomorrow:</b> {esc(report["tomorrow_plan"]) or "—"}</p>'
        )
    else:
        report_summary = '<div class="muted">No daily report has been submitted yet.</div>'

    risk_html = "".join(
        (
            f'<div class="action">'
            f'<span class="badge {r["band"]}">{r["band"]}</span> '
            f'<b>{esc(r["activity"])}</b> · {r["score"]:.0f}/100'
            f'<div class="small">{esc(r["explanation"])}</div>'
            f'</div>'
        )
        for r in risks[:6]
    ) or '<div class="muted">No risk records yet.</div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand Intelligence</div>
        <h1>What needs attention now?</h1>
        <div class="muted">
            Current project analysis for {project_name}.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Critical Risks</div>
            <div class="kpi">{len(critical)}</div>
        </div>
        <div class="card">
            <div class="label">High Risks</div>
            <div class="kpi">{len(high)}</div>
        </div>
        <div class="card">
            <div class="label">Open Make-Ready</div>
            <div class="kpi">{len(make_ready_items)}</div>
        </div>
        <div class="card">
            <div class="label">Active Activities</div>
            <div class="kpi">{len(in_progress)}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Handle First</h2>
            {action_html}
        </div>

        <div class="card">
            <h2>Latest Field Signal</h2>
            {report_summary}
        </div>
    </div>

    <div class="card">
        <h2>Risk Intelligence</h2>
        {risk_html}
    </div>

    <div class="card">
        <h2>BuildCommand Recommendation</h2>
        <p>
            Focus first on critical risks and open make-ready items, then verify field delays,
            inspections, and tomorrow's plan before committing additional manpower or recovery cost.
        </p>
        <div class="small">
            This is the first intelligence layer. The next version can connect a live AI model
            so BuildCommand can answer natural-language questions about the project.
        </div>
    </div>
    """

    return shell("AI Analysis", body)


def build_project_context(pid):
    c = db()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    risks = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    make_ready_items = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY due
        """,
        (pid,)
    ).fetchall()

    reports = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 5
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        "SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",
        (pid,)
    ).fetchall()

    production_rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM production p
        JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY p.work_date DESC, p.id DESC
        LIMIT 12
        """,
        (pid,)
    ).fetchall()

    sub_updates = c.execute(
        """
        SELECT u.*, s.name sub_name, s.trade
        FROM subcontractor_updates u
        JOIN subs s ON s.id=u.sub_id
        WHERE u.project_id=?
        ORDER BY u.update_date DESC, u.id DESC
        LIMIT 15
        """,
        (pid,)
    ).fetchall()

    recovery_rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM recovery r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.id DESC
        LIMIT 12
        """,
        (pid,)
    ).fetchall()

    action_rows = c.execute(
        """
        SELECT *
        FROM action_items
        WHERE project_id=? AND status='OPEN'
        ORDER BY due, id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    c.close()

    lines = []

    if project:
        lines.append(
            f'PROJECT: {project["number"]} - {project["name"]} | Status: {project["status"]}'
        )

    lines.append("\nSCHEDULE ACTIVITIES:")
    for a in activities:
        lines.append(
            f'- {a["external_id"]}: {a["name"]} | Trade: {a["trade"]} | '
            f'{a["start"]} to {a["finish"]} | {a["pct"]:.0f}% | {a["status"]}'
        )

    lines.append("\nRISKS:")
    for r in risks:
        lines.append(
            f'- {r["band"]} {r["score"]:.0f}/100 | {r["external_id"]} {r["activity"]}: '
            f'{r["explanation"]}'
        )

    lines.append("\nOPEN MAKE-READY ITEMS:")
    for m in make_ready_items:
        lines.append(
            f'- {m["priority"]} | {m["external_id"]} {m["activity"]} | '
            f'{m["title"]} | Reason: {m["reason"]} | Due: {m["due"]}'
        )

    lines.append("\nSUBCONTRACTORS:")
    for s in subs:
        lines.append(f'- {s["name"]} | Trade: {s["trade"]}')

    lines.append("\nRECENT PRODUCTION:")
    for p in production_rows:
        qty = p["qty"] or 0
        plan = p["planned_qty"] or 0
        pct = (qty / plan * 100) if plan > 0 else 0
        lines.append(
            f'- {p["work_date"]} | {p["external_id"]} {p["activity"]} | '
            f'Crew {p["crew"] or 0} | Actual {qty:g} {p["unit"] or ""} | '
            f'Plan {plan:g} | {pct:.0f}% of plan'
        )

    lines.append("\nRECENT SUBCONTRACTOR UPDATES:")
    for u in sub_updates:
        lines.append(
            f'- {u["update_date"]} | {u["sub_name"]} | Trade: {u["trade"]} | '
            f'Manpower: {u["manpower"] or 0} | Status: {u["status"] or "N/A"} | '
            f'Commitment: {u["commitment"] or "N/A"} | Issue: {u["issue"] or "N/A"}'
        )

    lines.append("\nRECOVERY SCENARIOS:")
    for r in recovery_rows:
        days = r["days_recovered"] or 0
        cost = r["est_cost"] or 0
        cpd = (cost / days) if days > 0 else 0
        lines.append(
            f'- {r["external_id"]} {r["activity"]} | '
            f'{r["scenario"]} | Days recovered: {days:.1f} | '
            f'Cost: ${cost:,.0f} | Cost/day: ${cpd:,.0f} | Status: {r["status"]}'
        )

    lines.append("\nOPEN ACTION ITEMS:")
    for a in action_rows:
        lines.append(
            f'- {a["priority"]} | {a["title"]} | Owner: {a["owner"] or "Unassigned"} | '
            f'Due: {a["due"]} | Notes: {a["notes"] or "N/A"}'
        )

    lines.append("\nRECENT DAILY REPORTS:")
    for r in reports:
        lines.append(
            f'- Date: {r["report_date"]} | Weather: {r["weather"] or "N/A"} | '
            f'Manpower: {r["manpower"] or 0}\n'
            f'  Work completed: {r["work_completed"] or "N/A"}\n'
            f'  Delays: {r["delays"] or "N/A"}\n'
            f'  Deliveries: {r["deliveries"] or "N/A"}\n'
            f'  Inspections: {r["inspections"] or "N/A"}\n'
            f'  Safety: {r["safety"] or "N/A"}\n'
            f'  Tomorrow plan: {r["tomorrow_plan"] or "N/A"}'
        )

    return "\n".join(lines)


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page():
    pid = project_id()
    c = db()
    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    api_ready = bool(os.environ.get("OPENAI_API_KEY"))

    status_html = (
        '<span class="badge READY">AI CONNECTED</span>'
        if api_ready
        else '<span class="badge HIGH">API KEY NEEDED</span>'
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand AI Copilot</div>
        <h1>Ask the project.</h1>
        <div class="muted">{project_label}</div>
        <div style="margin-top:10px;">{status_html}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Ask BuildCommand</h2>

            <form method="post" action="/assistant">
                <label>Your Question</label>
                <textarea
                    name="question"
                    placeholder="Example: What should I worry about today?"
                    required
                ></textarea>

                <button type="submit">Analyze Project</button>
            </form>

            <div class="small" style="margin-top:14px;">
                Try: What should I handle first? Which activity could delay us?
                What should I ask the MEP subcontractor? What should tomorrow's plan include?
            </div>
        </div>

        <div class="card">
            <h2>How it works</h2>
            <p>
                BuildCommand sends the selected project's schedule, risks, open make-ready items,
                subcontractors, and recent daily reports to the AI with your question.
            </p>
            <p class="small">
                The AI is instructed to stay grounded in project data and clearly identify when
                information is missing instead of inventing jobsite facts.
            </p>
        </div>
    </div>
    """

    return shell("AI Assistant", body)


@app.post("/assistant", response_class=HTMLResponse)
def assistant_answer(question: str = Form(...)):
    pid = project_id()

    c = db()
    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        answer = """
        OPENAI_API_KEY is not configured yet.

        Add the key in Render under your service's Environment settings,
        then redeploy the service. Do not paste the API key into full_app.py.
        """
    else:
        context = build_project_context(pid)

        instructions = """
You are BuildCommand AI, a construction superintendent and project-execution copilot.

Use only the project information supplied in the prompt as job-specific facts.
Do not invent schedule status, subcontractor commitments, inspections, deliveries,
manpower, safety conditions, or field progress.

Prioritize:
1. immediate field risk,
2. schedule impact,
3. make-ready constraints,
4. inspections and deliveries,
5. subcontractor follow-up,
6. practical next actions.

When useful, structure the answer as:
- Handle first
- Why it matters
- Who to follow up with
- Next action

Keep answers concise, practical, and written for a working superintendent.
If the project data is insufficient, clearly say what information is missing.
"""

        user_input = f"""
PROJECT DATA
------------
{context}

SUPERINTENDENT QUESTION
-----------------------
{question}
"""

        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model="gpt-5.6",
                instructions=instructions,
                input=user_input
            )
            answer = response.output_text
        except Exception as exc:
            answer = (
                "BuildCommand could not reach the AI service. "
                f"Error: {str(exc)}"
            )

    answer_html = esc(answer).replace("\n", "<br>")

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand AI Copilot</div>
        <h1>Project Answer</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <div class="small">YOUR QUESTION</div>
            <h3>{esc(question)}</h3>

            <a href="/assistant"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                ← Ask another question
            </a>
        </div>

        <div class="card">
            <div class="small">BUILDCOMMAND AI</div>
            <div style="margin-top:12px;line-height:1.65;">
                {answer_html}
            </div>
        </div>
    </div>
    """

    return shell("AI Assistant", body)


@app.get("/make-ready/new", response_class=HTMLResponse)
def new_make_ready_form():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    options = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    if not options:
        body = """
        <div class="hero">
            <div class="eyebrow">Make Ready</div>
            <h1>Add Make-Ready Item</h1>
        </div>
        <div class="card">
            <p>This project has no activities yet. Add a schedule activity first.</p>
            <a href="/activities/new" style="color:#f0b44d;font-weight:700;text-decoration:none;">
                + Add Activity
            </a>
        </div>
        """
        return shell("Add Make-Ready", body)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Make Ready</div>
        <h1>Add Make-Ready Item</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/make-ready/new">

            <label>Activity</label>
            <select name="activity_id" required>
                {options}
            </select>

            <label>Blocker / Action Title</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Confirm electrical gear delivery"
                required
            >

            <label>Why It Matters</label>
            <textarea
                name="reason"
                placeholder="Describe the constraint, dependency, or field impact."
                required
            ></textarea>

            <div class="grid2">
                <div>
                    <label>Clear By</label>
                    <input type="date" name="due" required>
                </div>

                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Make-Ready Item</button>

                <a href="/make-ready"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Make-Ready", body)


@app.post("/make-ready/new")
def create_make_ready(
    activity_id: int = Form(...),
    title: str = Form(...),
    reason: str = Form(...),
    due: str = Form(...),
    priority: str = Form("HIGH")
):
    pid = project_id()

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO make_ready(
                project_id,
                activity_id,
                title,
                reason,
                due,
                priority,
                status
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                title.strip(),
                reason.strip(),
                due,
                priority,
                "OPEN"
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/make-ready", status_code=303)


@app.post("/make-ready/{item_id}/close")
def close_make_ready(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE make_ready
        SET status='COMPLETE'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/make-ready", status_code=303)


@app.post("/daily-report/{report_id}/analyze")
def analyze_daily_report(report_id: int):
    pid = project_id()
    c = db()

    report = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE id=? AND project_id=?
        """,
        (report_id, pid)
    ).fetchone()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    c.close()

    if not report:
        return RedirectResponse(url="/daily-report", status_code=303)

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        analysis_text = (
            "OPENAI_API_KEY is not configured. Add the key in Render Environment settings "
            "to enable automatic report analysis."
        )
    else:
        schedule_lines = []
        for a in activities:
            schedule_lines.append(
                f'{a["external_id"]} | {a["name"]} | {a["trade"]} | '
                f'{a["start"]} to {a["finish"]} | {a["pct"]:.0f}% | {a["status"]}'
            )

        schedule_text = "\n".join(schedule_lines) or "No schedule activities entered."

        prompt = f"""
PROJECT
{project["number"] if project else ""} - {project["name"] if project else ""}

SCHEDULE
{schedule_text}

DAILY REPORT
Date: {report["report_date"]}
Weather: {report["weather"] or "N/A"}
Manpower: {report["manpower"] or 0}
Work completed: {report["work_completed"] or "N/A"}
Delays / constraints: {report["delays"] or "N/A"}
Deliveries: {report["deliveries"] or "N/A"}
Inspections: {report["inspections"] or "N/A"}
Safety: {report["safety"] or "N/A"}
Tomorrow plan: {report["tomorrow_plan"] or "N/A"}
"""

        instructions = """
You are BuildCommand AI, a construction superintendent field-intelligence copilot.

Analyze the daily report against the schedule information provided.
Do not invent facts that are not present.

Return a concise superintendent review with these headings:
HANDLE FIRST
SCHEDULE IMPACT
SUBCONTRACTOR / FOLLOW-UP
TOMORROW READINESS
MISSING INFORMATION

Call out specific delays, inspection issues, delivery risks, manpower concerns,
or tomorrow-plan gaps when supported by the report.
"""

        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model="gpt-5.6",
                instructions=instructions,
                input=prompt
            )
            analysis_text = response.output_text
        except Exception as exc:
            analysis_text = f"AI analysis failed: {str(exc)}"

    c = db()
    c.execute(
        "DELETE FROM daily_report_analysis WHERE project_id=? AND report_id=?",
        (pid, report_id)
    )
    c.execute(
        """
        INSERT INTO daily_report_analysis(
            project_id,
            report_id,
            analysis_text,
            created
        )
        VALUES(?,?,?,?)
        """,
        (
            pid,
            report_id,
            analysis_text,
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/daily-report", status_code=303)


@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM action_items
        WHERE project_id=?
        ORDER BY
            CASE status
                WHEN 'OPEN' THEN 1
                WHEN 'COMPLETE' THEN 2
                ELSE 3
            END,
            CASE priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                WHEN 'LOW' THEN 4
                ELSE 5
            END,
            due
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()

    open_items = [r for r in rows if r["status"] == "OPEN"]
    complete_items = [r for r in rows if r["status"] == "COMPLETE"]
    overdue_items = [
        r for r in open_items
        if r["due"] and r["due"] < today
    ]

    open_html = ""

    for r in open_items:
        overdue = bool(r["due"] and r["due"] < today)
        due_text = f'Due {esc(r["due"])}'

        if overdue:
            due_text += " · OVERDUE"

        badge = r["priority"] if r["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "OPEN"

        open_html += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["priority"])}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">Owner: {esc(r["owner"]) or "Unassigned"}</div>
                </div>

                <form method="post" action="/actions/{r["id"]}/complete">
                    <button type="submit">Mark Complete</button>
                </form>
            </div>

            <p>{esc(r["notes"]) or "No notes entered."}</p>
            <div class="small">{due_text}</div>
        </div>
        """

    if not open_html:
        open_html = '<div class="card"><div class="muted">No open action items.</div></div>'

    complete_html = "".join(
        f"""
        <div class="action">
            <span class="badge COMPLETE">COMPLETE</span>
            <b>{esc(r["title"])}</b>
            <div class="small">
                Owner: {esc(r["owner"]) or "Unassigned"} · Due {esc(r["due"]) or "—"}
            </div>
        </div>
        """
        for r in complete_items[:12]
    ) or '<div class="muted">No completed actions yet.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Action Center</div>
                <h1>Turn project intelligence into accountable action.</h1>
                <div class="muted">
                    Assign ownership, due dates, and priority to the work that cannot fall through the cracks.
                </div>
            </div>

            <a href="/actions/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Action
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open Actions</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue_items)}</div>
        </div>

        <div class="card">
            <div class="label">Critical</div>
            <div class="kpi">{sum(r["priority"] == "CRITICAL" for r in open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Completed</div>
            <div class="kpi">{len(complete_items)}</div>
        </div>
    </div>

    <div class="grid2">
        <div>
            <h2>Open Actions</h2>
            {open_html}
        </div>

        <div class="card">
            <h2>Recently Completed</h2>
            {complete_html}
        </div>
    </div>
    """

    return shell("Action Center", body)


@app.get("/actions/new", response_class=HTMLResponse)
def new_action_form():
    pid = project_id()
    c = db()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    c.close()

    owner_options = '<option value="">Unassigned</option>'
    owner_options += '<option value="Superintendent">Superintendent</option>'
    owner_options += '<option value="Project Manager">Project Manager</option>'

    for s in subs:
        owner_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Action Center</div>
        <h1>Add Action Item</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/actions/new">

            <label>Action</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Confirm switchgear delivery date"
                required
            >

            <label>Owner</label>
            <select name="owner">
                {owner_options}
            </select>

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" required>
                </div>
            </div>

            <label>Notes</label>
            <textarea
                name="notes"
                placeholder="What specifically needs to happen?"
            ></textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Action</button>

                <a href="/actions"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Action", body)


@app.post("/actions/new")
def create_action(
    title: str = Form(...),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    notes: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        INSERT INTO action_items(
            project_id,
            title,
            owner,
            priority,
            due,
            status,
            notes,
            created
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            title.strip(),
            owner.strip(),
            priority,
            due,
            "OPEN",
            notes.strip(),
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/actions", status_code=303)


@app.post("/actions/{action_id}/complete")
def complete_action(action_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE action_items
        SET status='COMPLETE'
        WHERE id=? AND project_id=?
        """,
        (action_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/actions", status_code=303)
