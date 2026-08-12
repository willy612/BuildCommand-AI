from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import sqlite3
from datetime import date

app=FastAPI(title="Construction AI",version="8.3")
DB="construction_ai_web.db"

def db():
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    return conn

def init():
    c=db()
    c.executescript("""
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
    if c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]==0:
        c.execute("INSERT INTO projects(name,number,status) VALUES(?,?,?)",("Canyon Medical Office","CMO-024","ACTIVE"))
        pid=c.execute("SELECT id FROM projects").fetchone()["id"]
        acts=[
            ("A100","Footings & Foundations","Concrete","2026-08-10","2026-08-18",80,"IN_PROGRESS"),
            ("A200","Structural Steel / Deck","Structural","2026-08-19","2026-09-04",15,"NOT_STARTED"),
            ("A300","MEP Underground / Rough","MEP","2026-08-24","2026-09-11",5,"NOT_STARTED"),
            ("A400","Interior Framing","Framing","2026-09-08","2026-09-25",0,"NOT_STARTED"),
            ("A500","Drywall Close-In","Drywall","2026-09-22","2026-10-09",0,"NOT_STARTED"),
        ]
        for x in acts:
            c.execute("INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,?,?)",(pid,*x))
        ids={r["external_id"]:r["id"] for r in c.execute("SELECT id,external_id FROM activities WHERE project_id=?",(pid,))}
        c.execute("INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",(pid,ids["A300"],"Clear MEP rough-in access","Material laydown is blocking east-side access.","2026-08-12","CRITICAL"))
        c.execute("INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",(pid,ids["A200"],"Confirm steel delivery","Fabricator delivery confirmation has not been received.","2026-08-13","HIGH"))
        for aid,score,band,why in [
            (ids["A300"],82,"CRITICAL","MEP rough-in is behind the field plan and access remains unresolved."),
            (ids["A200"],68,"HIGH","Steel delivery confirmation is unresolved; downstream starts are exposed."),
            (ids["A400"],44,"WATCH","Framing depends on MEP rough-in and inspection clearance.")
        ]:
            c.execute("INSERT INTO risks(project_id,activity_id,score,band,explanation) VALUES(?,?,?,?,?)",(pid,aid,score,band,why))
        for name,trade in [("Apex Concrete","Concrete"),("Metro Steel","Structural"),("Summit MEP","MEP")]:
            c.execute("INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)",(pid,name,trade))
        c.execute("INSERT INTO memory(project_id,category,insight,confidence) VALUES(?,?,?,?)",(pid,"Company Memory","Early access constraints on MEP rough-in should be cleared before manpower is increased.",.72))
c.commit()

if c.execute("SELECT COUNT(*) n FROM app_state").fetchone()["n"] == 0:
    first_project = c.execute(
        "SELECT id FROM projects ORDER BY id LIMIT 1"
    ).fetchone()

    if first_project:
        c.execute(
            "INSERT INTO app_state(id, selected_project_id) VALUES(1, ?)",
            (first_project["id"],)
        )

c.commit()

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
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.side{background:#0c141d;border-right:1px solid var(--line);padding:22px 16px}.brand{font-size:20px;font-weight:800}.company{font-size:12px;color:var(--muted);margin:5px 0 20px}.nav a{display:block;color:#cbd7e3;text-decoration:none;padding:10px;border-radius:9px;margin:2px 0}.nav a:hover{background:#162333}.main{padding:26px;max-width:1400px}
.hero,.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:12px}.hero h1{margin:4px 0}.eyebrow{color:var(--gold);font-size:11px;text-transform:uppercase;letter-spacing:.13em}.muted,.small{color:var(--muted)}.small{font-size:12px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kpi{font-size:28px;font-weight:800}.label{font-size:11px;color:var(--muted);text-transform:uppercase}.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-weight:800;font-size:10px}.CRITICAL,.HOLD{background:#492324;color:#ff9b9b}.HIGH,.WATCH{background:#43381b;color:#ffd779}.READY,.LOW,.COMPLETE{background:#18392c;color:#82e4b5}.OPEN{background:#1d2e44;color:#99c9ff}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}input,textarea,select{width:100%;background:#0d1620;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px}textarea{min-height:90px}button{background:var(--gold);border:0;border-radius:9px;padding:10px 14px;font-weight:800}.action{padding:12px 0;border-bottom:1px solid var(--line)}
@media(max-width:850px){.app{grid-template-columns:1fr}.grid4,.grid3,.grid2{grid-template-columns:1fr}.main{padding:14px}}
"""

NAV=[("Daily Command","/"),("Schedule","/schedule"),("Make Ready","/make-ready"),("Field","/field"),("Subcontractors","/subcontractors"),("Production","/production"),("Predictive Risk","/risk"),("Recovery","/recovery"),("Company Memory","/memory"),("Playbooks","/playbooks"),("Portfolio","/portfolio")]

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
                    Construction AI
                </div>

                <div class="company">
                    Demo Construction Company<br>
                    {current_name}
                </div>

                {selector}

                <nav class="nav">
                    {nav}
                </nav>

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
    tr="".join(f'<tr><td>{r["external_id"]}</td><td><b>{esc(r["name"])}</b></td><td>{r["trade"]}</td><td>{r["start"]}</td><td>{r["finish"]}</td><td>{r["pct"]:.0f}%</td><td>{r["status"]}</td></tr>' for r in rows)
    return shell("Schedule",f'<div class="hero"><div class="eyebrow">Schedule + Lookahead</div><h1>Field execution plan</h1></div><div class="card"><table><tr><th>ID</th><th>Activity</th><th>Trade</th><th>Start</th><th>Finish</th><th>%</th><th>Status</th></tr>{tr}</table></div>')

@app.get("/make-ready",response_class=HTMLResponse)
def make_ready():
    pid=project_id(); c=db(); rows=c.execute("SELECT m.*,a.name activity FROM make_ready m JOIN activities a ON a.id=m.activity_id WHERE m.project_id=? ORDER BY due",(pid,)).fetchall(); c.close()
    html="".join(f'<div class="card"><span class="badge {r["priority"]}">{r["priority"]}</span> <b>{esc(r["activity"])}</b><h3>{esc(r["title"])}</h3><p>{esc(r["reason"])}</p><div class="small">Clear by {r["due"]}</div></div>' for r in rows)
    return shell("Make Ready",'<div class="hero"><div class="eyebrow">Predictive Make-Ready</div><h1>Clear tomorrow blockers before they hit the field.</h1></div>'+html)

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
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM subs WHERE project_id=?",(pid,)).fetchall(); c.close()
    html="".join(f'<div class="card"><h3>{esc(r["name"])}</h3><div class="muted">{esc(r["trade"])}</div><p>Commitments, manpower, production, constraints and early-warning behavior feed the operating loop.</p></div>' for r in rows)
    return shell("Subcontractors",'<div class="hero"><div class="eyebrow">Subcontractor Intelligence</div><h1>Trade partners in the execution loop.</h1></div><div class="grid3">'+html+'</div>')

@app.get("/production",response_class=HTMLResponse)
def production():
    pid=project_id(); c=db(); acts=c.execute("SELECT id,external_id,name FROM activities WHERE project_id=?",(pid,)).fetchall(); rows=c.execute("SELECT p.*,a.name activity FROM production p JOIN activities a ON a.id=p.activity_id WHERE p.project_id=? ORDER BY p.id DESC LIMIT 20",(pid,)).fetchall(); c.close()
    opts="".join(f'<option value="{a["id"]}">{a["external_id"]} - {esc(a["name"])}</option>' for a in acts)
    tr="".join(f'<tr><td>{r["work_date"]}</td><td>{esc(r["activity"])}</td><td>{r["crew"]}</td><td>{r["qty"]}</td><td>{r["unit"]}</td><td>{r["planned_qty"]}</td></tr>' for r in rows)
    body=f'<div class="hero"><div class="eyebrow">Production Intelligence</div><h1>Measure the job before the schedule update does.</h1></div><div class="grid2"><div class="card"><form method="post" action="/production/add"><select name="activity_id">{opts}</select><br><br><input name="crew" type="number" placeholder="Crew size"><br><br><input name="qty" type="number" step="0.1" placeholder="Installed quantity"><br><br><input name="planned_qty" type="number" step="0.1" placeholder="Planned quantity"><br><br><input name="unit" placeholder="LF / SF / CY / EA"><br><br><button>Record production</button></form></div><div class="card"><table><tr><th>Date</th><th>Activity</th><th>Crew</th><th>Qty</th><th>Unit</th><th>Plan</th></tr>{tr}</table></div></div>'
    return shell("Production",body)

@app.post("/production/add")
def add_prod(activity_id:int=Form(...),crew:int=Form(0),qty:float=Form(0),planned_qty:float=Form(0),unit:str=Form("")):
    pid=project_id(); c=db(); c.execute("INSERT INTO production(project_id,activity_id,work_date,crew,qty,planned_qty,unit) VALUES(?,?,?,?,?,?,?)",(pid,activity_id,date.today().isoformat(),crew,qty,planned_qty,unit)); c.commit(); c.close()
    return RedirectResponse("/production",303)

@app.get("/risk",response_class=HTMLResponse)
def risk():
    pid=project_id(); c=db(); rows=c.execute("SELECT r.*,a.name activity FROM risks r JOIN activities a ON a.id=r.activity_id WHERE r.project_id=? ORDER BY score DESC",(pid,)).fetchall(); c.close()
    html="".join(f'<div class="card"><span class="badge {r["band"]}">{r["band"]}</span><h3>{esc(r["activity"])} - {r["score"]:.0f}/100</h3><p>{esc(r["explanation"])}</p></div>' for r in rows)
    return shell("Predictive Risk",'<div class="hero"><div class="eyebrow">Predictive Risk</div><h1>What is most likely to hurt the job next?</h1><div class="muted">Explainable risk screening, not a fake probability promise.</div></div>'+html)

@app.get("/recovery",response_class=HTMLResponse)
def recovery():
    pid=project_id(); c=db(); rows=c.execute("SELECT r.*,a.name activity FROM recovery r JOIN activities a ON a.id=r.activity_id WHERE r.project_id=? ORDER BY r.id DESC",(pid,)).fetchall(); acts=c.execute("SELECT id,external_id,name FROM activities WHERE project_id=?",(pid,)).fetchall(); c.close()
    opts="".join(f'<option value="{a["id"]}">{a["external_id"]} - {esc(a["name"])}</option>' for a in acts)
    html="".join(f'<div class="card"><b>{esc(r["activity"])}</b><h3>{esc(r["scenario"])}</h3><div>Modeled recovery: {r["days_recovered"]:.1f} day(s)</div><div>Screening cost: ${r["est_cost"]:,.0f}</div><div class="small">{r["status"]}</div></div>' for r in rows)
    form=f'<div class="card"><h2>Test recovery idea</h2><form method="post" action="/recovery/add"><select name="activity_id">{opts}</select><br><br><select name="scenario"><option>ADD_CREW</option><option>OVERTIME</option><option>WORK_SATURDAY</option><option>RESEQUENCE</option><option>CLEAR_CONSTRAINT</option></select><br><br><input name="days_recovered" type="number" step="0.1" placeholder="Modeled days recovered"><br><br><input name="est_cost" type="number" step="100" placeholder="Estimated cost"><br><br><button>Save screening scenario</button></form></div>'
    return shell("Recovery",'<div class="hero"><div class="eyebrow">Recovery Intelligence</div><h1>Test recovery before spending money.</h1></div><div class="grid2">'+form+'<div>'+html+'</div></div>')

@app.post("/recovery/add")
def add_recovery(activity_id:int=Form(...),scenario:str=Form(...),days_recovered:float=Form(0),est_cost:float=Form(0)):
    pid=project_id(); c=db(); c.execute("INSERT INTO recovery(project_id,activity_id,scenario,days_recovered,est_cost,status) VALUES(?,?,?,?,?,?)",(pid,activity_id,scenario,days_recovered,est_cost,"PROPOSED")); c.commit(); c.close()
    return RedirectResponse("/recovery",303)

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
    pid=project_id(); c=db(); p=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); risks=c.execute("SELECT * FROM risks WHERE project_id=?",(pid,)).fetchall(); mr=c.execute("SELECT COUNT(*) n FROM make_ready WHERE project_id=? AND status='OPEN'",(pid,)).fetchone()["n"]; c.close()
    crit=sum(r["band"]=="CRITICAL" for r in risks); high=sum(r["band"]=="HIGH" for r in risks); score=min(100,crit*30+high*15+mr*5)
    body=f'<div class="hero"><div class="eyebrow">Executive Intelligence</div><h1>Which projects need intervention?</h1></div><div class="card"><h2>{esc(p["number"])} - {esc(p["name"])}</h2><div class="grid4"><div><div class="label">Attention</div><div class="kpi">{score}</div></div><div><div class="label">Critical</div><div class="kpi">{crit}</div></div><div><div class="label">High</div><div class="kpi">{high}</div></div><div><div class="label">Make Ready</div><div class="kpi">{mr}</div></div></div><p>The executive view rolls field risk upward without hiding the reasons.</p></div>'
    return shell("Portfolio",body)
