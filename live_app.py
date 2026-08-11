from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from db.session import init_db,SessionLocal
from db.models import (
    Company,Project,ScheduleActivity,PredictiveRiskSnapshot,
    MakeReadyAction,Subcontractor
)

app=FastAPI(title="Construction AI Live Prototype")
init_db()

CSS="""
:root{--bg:#0b1118;--panel:#111a24;--line:#223142;--text:#edf4fb;--muted:#8ca0b3;--accent:#f0b44d;--ok:#57d39b;--warn:#f5c96a;--bad:#ff7676}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
.shell{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{border-right:1px solid var(--line);padding:24px 18px;background:#0d151e;position:sticky;top:0;height:100vh}
.brand{font-size:19px;font-weight:800;margin-bottom:4px}.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.nav a{display:block;padding:11px 12px;color:#c9d7e5;text-decoration:none;border-radius:10px;margin:3px 0}.nav a:hover{background:#142131;color:white}
.main{padding:28px;max-width:1350px}.hero{padding:22px;border:1px solid var(--line);background:linear-gradient(135deg,#111b26,#0f1822);border-radius:18px;margin-bottom:18px}
.hero h1{margin:4px 0 6px;font-size:28px}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent)}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.card{border:1px solid var(--line);background:var(--panel);border-radius:15px;padding:16px;margin-bottom:12px}
.k{font-size:28px;font-weight:800}.label{font-size:12px;color:var(--muted)}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:800}.HIGH,.WATCH{background:#403619;color:#ffd778}.CRITICAL,.HOLD{background:#482323;color:#ff9b9b}.READY,.LOW{background:#17392b;color:#7ce3b2}
.action{padding:14px 0;border-bottom:1px solid var(--line)}.action:last-child{border-bottom:0}.small{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted);font-weight:600}
@media(max-width:850px){.shell{grid-template-columns:1fr}.side{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}.nav{display:flex;gap:4px;overflow:auto}.nav a{white-space:nowrap}.grid{grid-template-columns:1fr 1fr}.row{grid-template-columns:1fr}.main{padding:16px}}
"""

def shell(title,content,project_name="Demo Project"):
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{CSS}</style></head>
<body><div class="shell"><aside class="side"><div class="brand">🏗 Construction AI</div><div class="sub">{project_name}</div>
<nav class="nav"><a href="/">Daily Command</a><a href="/schedule">Schedule</a><a href="/risk">Predictive Risk</a><a href="/subs">Subcontractors</a><a href="/portfolio">Portfolio</a></nav>
</aside><main class="main">{content}</main></div></body></html>"""

def context(db):
    company=db.query(Company).first()
    project=db.query(Project).filter(Project.company_id==company.id).first() if company else None
    return company,project

@app.get("/",response_class=HTMLResponse)
def home():
    db=SessionLocal()
    try:
        company,p=context(db)
        risks=db.query(PredictiveRiskSnapshot).filter(PredictiveRiskSnapshot.project_id==p.id).order_by(PredictiveRiskSnapshot.risk_score.desc()).all()
        latest={}
        for r in risks: latest.setdefault(r.schedule_activity_id,r)
        ranked=list(latest.values())
        actions=db.query(MakeReadyAction).filter(MakeReadyAction.project_id==p.id,MakeReadyAction.status=="OPEN").order_by(MakeReadyAction.required_by.asc()).all()
        critical=sum(1 for r in ranked if r.probability_band=="CRITICAL")
        high=sum(1 for r in ranked if r.probability_band=="HIGH")
        ready=max(0,db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).count()-critical-high)
        cards=f"""<div class="grid"><div class="card"><div class="label">READY / CLEAR</div><div class="k">{ready}</div></div>
        <div class="card"><div class="label">CRITICAL RISK</div><div class="k">{critical}</div></div>
        <div class="card"><div class="label">HIGH RISK</div><div class="k">{high}</div></div>
        <div class="card"><div class="label">OPEN MAKE-READY</div><div class="k">{len(actions)}</div></div></div>"""
        action_html="".join(
            f"""<div class="action"><span class="badge {a.priority}">{a.priority}</span> <b>{a.title}</b>
            <div class="small">Due {a.required_by or '—'} · {a.gate_name}</div><div>{a.reason}</div></div>""" for a in actions
        ) or "<div class='muted'>No open actions.</div>"
        risk_html=""
        for r in ranked[:4]:
            act=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            risk_html+=f"""<div class="action"><span class="badge {r.probability_band}">{r.probability_band}</span>
            <b>{act.name if act else r.schedule_activity_id}</b> · {r.risk_score:.0f}/100
            <div class="small">{r.explanation}</div></div>"""
        content=f"""<section class="hero"><div class="eyebrow">Daily Superintendent Command</div><h1>Good morning. Here’s what can hurt the job.</h1>
        <div class="muted">Prioritized from project evidence — schedule, make-ready, field risk and downstream exposure.</div></section>
        {cards}<div class="row"><section class="card"><h2>Handle first</h2>{action_html}</section>
        <section class="card"><h2>What may hurt next</h2>{risk_html}</section></div>"""
        return shell("Construction AI — Daily Command",content,p.name)
    finally: db.close()

@app.get("/schedule",response_class=HTMLResponse)
def schedule():
    db=SessionLocal()
    try:
        _,p=context(db)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).order_by(ScheduleActivity.planned_start.asc()).all()
        rows="".join(f"<tr><td>{a.external_id}</td><td><b>{a.name}</b></td><td>{a.trade}</td><td>{a.planned_start}</td><td>{a.planned_finish}</td><td>{a.percent_complete:.0f}%</td></tr>" for a in acts)
        content=f"""<section class="hero"><div class="eyebrow">Schedule + Lookahead</div><h1>Field execution plan</h1><div class="muted">The master schedule becomes a make-ready operating plan.</div></section>
        <div class="card"><table><thead><tr><th>ID</th><th>Activity</th><th>Trade</th><th>Start</th><th>Finish</th><th>Complete</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        return shell("Schedule",content,p.name)
    finally: db.close()

@app.get("/risk",response_class=HTMLResponse)
def risk():
    db=SessionLocal()
    try:
        _,p=context(db)
        risks=db.query(PredictiveRiskSnapshot).filter(PredictiveRiskSnapshot.project_id==p.id).order_by(PredictiveRiskSnapshot.risk_score.desc()).all()
        latest={}
        for r in risks: latest.setdefault(r.schedule_activity_id,r)
        cards=""
        for r in latest.values():
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            cards+=f"""<div class="card"><span class="badge {r.probability_band}">{r.probability_band}</span>
            <h3>{a.name if a else r.schedule_activity_id} · {r.risk_score:.0f}/100</h3><div>{r.explanation}</div>
            <div class="small">Drift {r.schedule_drift_points:.0f} · Constraints {r.constraint_points:.0f} · Downstream {r.downstream_points:.0f}</div></div>"""
        content=f"""<section class="hero"><div class="eyebrow">Predictive Risk</div><h1>What may hurt the job before it’s obvious?</h1>
        <div class="muted">Explainable screening signals — not a fake probability promise.</div></section>{cards}"""
        return shell("Predictive Risk",content,p.name)
    finally: db.close()

@app.get("/subs",response_class=HTMLResponse)
def subs():
    db=SessionLocal()
    try:
        company,p=context(db)
        subs=db.query(Subcontractor).filter(Subcontractor.company_id==company.id).all()
        cards="".join(f"<div class='card'><h3>{s.name}</h3><div class='muted'>{s.trade}</div><p>{s.scope or 'Project trade partner'}</p></div>" for s in subs)
        content=f"""<section class="hero"><div class="eyebrow">Subcontractor Intelligence</div><h1>Trade partners in the execution loop</h1>
        <div class="muted">Commitments, manpower, production, constraints and response behavior feed make-ready decisions.</div></section>
        <div class="grid">{cards}</div>"""
        return shell("Subcontractors",content,p.name)
    finally: db.close()

@app.get("/portfolio",response_class=HTMLResponse)
def portfolio():
    db=SessionLocal()
    try:
        company,p=context(db)
        content=f"""<section class="hero"><div class="eyebrow">Executive Intelligence</div><h1>{company.name}</h1>
        <div class="muted">Portfolio oversight rolls field risk upward without hiding why the job needs attention.</div></section>
        <div class="card"><h2>{p.project_number} — {p.name}</h2><p>Status: <b>{p.status}</b></p>
        <p>Current prototype demonstrates the field-to-executive workflow using the same project database.</p></div>"""
        return shell("Portfolio",content,p.name)
    finally: db.close()
