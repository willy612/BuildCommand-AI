from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from db.session import init_db, SessionLocal
from db.models import (
    Company,User,Project,ScheduleActivity,MakeReadyAction,PredictiveRiskSnapshot,
    Subcontractor,ProductionRecord,CompanyKnowledgePattern,CompanyPlaybookRule,
    ApprovedRecoveryPlan,ExecutiveRiskSnapshot,LookaheadCommitment
)
from predictive_risk.engine import run_project_risk
from company_memory.service import capture_project_learning, refresh_company_patterns
from production_intelligence.service import refresh_project_production, refresh_company_benchmarks
from executive_intelligence.service import refresh_executive_snapshots

app=FastAPI(title="Construction AI Full App",version="8.0")
init_db()

CSS="""
:root{--bg:#0a1017;--panel:#111923;--panel2:#0e1620;--line:#213042;--text:#eef4fb;--muted:#8fa2b5;--gold:#f0b44d;--green:#58d29e;--amber:#f4c86c;--red:#ff7777;--blue:#73b5ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
.app{display:grid;grid-template-columns:265px 1fr;min-height:100vh}.sidebar{background:#0c141d;border-right:1px solid var(--line);padding:22px 16px;position:sticky;top:0;height:100vh}
.brand{font-size:20px;font-weight:800}.muted{color:var(--muted)}.company{font-size:12px;color:var(--muted);margin:5px 0 18px}.project-select{width:100%;background:#111b26;color:var(--text);border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:14px}
.nav a{display:block;color:#cbd7e3;text-decoration:none;padding:10px 11px;border-radius:9px;margin:2px 0;font-size:14px}.nav a:hover{background:#162333;color:#fff}
.main{padding:26px;max-width:1450px}.hero{border:1px solid var(--line);background:linear-gradient(135deg,#111b26,#0e1721);padding:22px;border-radius:18px;margin-bottom:16px}.eyebrow{color:var(--gold);font-size:11px;text-transform:uppercase;letter-spacing:.13em}.hero h1{margin:4px 0 5px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:15px;margin-bottom:12px}.kpi{font-size:28px;font-weight:800}.label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-weight:800;font-size:10px}.CRITICAL,.HOLD{background:#492324;color:#ff9b9b}.HIGH,.WATCH{background:#43381b;color:#ffd779}.READY,.LOW,.COMPLETE{background:#18392c;color:#82e4b5}.OPEN{background:#1d2e44;color:#99c9ff}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted);font-weight:600}
input,textarea,select{width:100%;background:#0d1620;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px}textarea{min-height:90px}
button,.btn{background:var(--gold);color:#1b1407;border:0;border-radius:9px;padding:10px 14px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-block}
.btn.secondary{background:#182536;color:#dce8f4}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.action{border-bottom:1px solid var(--line);padding:12px 0}.action:last-child{border-bottom:0}
.small{font-size:12px;color:var(--muted)}.danger{color:var(--red)}.good{color:var(--green)}.warn{color:var(--amber)}
@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}.nav{display:flex;gap:3px;overflow:auto}.nav a{white-space:nowrap}.grid4,.grid3,.grid2,.row{grid-template-columns:1fr 1fr}.main{padding:15px}}
@media(max-width:600px){.grid4,.grid3,.grid2,.row{grid-template-columns:1fr}}
"""

def esc(s):
    return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def get_context(db,project_id=None):
    company=db.query(Company).first()
    user=db.query(User).filter(User.company_id==company.id).first() if company else None
    projects=db.query(Project).filter(Project.company_id==company.id).order_by(Project.name).all() if company else []
    if project_id:
        project=db.query(Project).filter(Project.id==project_id,Project.company_id==company.id).first()
    else:
        project=projects[0] if projects else None
    return company,user,projects,project

def shell(title,body,company,user,projects,project):
    project_options="".join(f'<option value="{p.id}" {"selected" if project and p.id==project.id else ""}>{esc(p.project_number)} — {esc(p.name)}</option>' for p in projects)
    links=[
        ("Daily Command","/"),
        ("Schedule","/schedule"),
        ("Make Ready","/make-ready"),
        ("Field","/field"),
        ("Subcontractors","/subcontractors"),
        ("Production","/production"),
        ("Predictive Risk","/risk"),
        ("Recovery","/recovery"),
        ("Company Memory","/memory"),
        ("Playbooks","/playbooks"),
        ("Portfolio","/portfolio"),
    ]
    nav="".join(f'<a href="{url}?project_id={project.id if project else ""}">{name}</a>' for name,url in links)
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="app"><aside class="sidebar"><div class="brand">🏗 Construction AI</div><div class="company">{esc(company.name if company else "")}<br>{esc(user.display_name if user else "")}</div>
<form method="get"><select name="project_id" class="project-select" onchange="this.form.submit()">{project_options}</select></form>
<nav class="nav">{nav}</nav></aside><main class="main">{body}</main></div></body></html>"""

def risk_latest(db,project_id):
    rows=db.query(PredictiveRiskSnapshot).filter(PredictiveRiskSnapshot.project_id==project_id).order_by(PredictiveRiskSnapshot.created_at.desc()).all()
    latest={}
    for r in rows: latest.setdefault(r.schedule_activity_id,r)
    return list(latest.values())

@app.get("/",response_class=HTMLResponse)
def home(project_id:int|None=None):
    db=SessionLocal()
    try:
        company,user,projects,p=get_context(db,project_id)
        risks=sorted(risk_latest(db,p.id),key=lambda x:x.risk_score,reverse=True)
        actions=db.query(MakeReadyAction).filter(MakeReadyAction.project_id==p.id,MakeReadyAction.status=="OPEN").order_by(MakeReadyAction.required_by.asc()).all()
        crit=sum(r.probability_band=="CRITICAL" for r in risks); high=sum(r.probability_band=="HIGH" for r in risks)
        act_count=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).count()
        hero=f"""<div class="hero"><div class="eyebrow">Daily Superintendent Command</div><h1>What needs attention today?</h1><div class="muted">One field-first view of risk, constraints, ownership and next action.</div></div>"""
        kpis=f"""<div class="grid4"><div class="card"><div class="label">Activities</div><div class="kpi">{act_count}</div></div><div class="card"><div class="label">Critical risk</div><div class="kpi">{crit}</div></div><div class="card"><div class="label">High risk</div><div class="kpi">{high}</div></div><div class="card"><div class="label">Open make-ready</div><div class="kpi">{len(actions)}</div></div></div>"""
        action_html="".join(f"""<div class="action"><span class="badge {a.priority}">{esc(a.priority)}</span> <b>{esc(a.title)}</b><div>{esc(a.reason)}</div><div class="small">Due {esc(a.required_by)} · {esc(a.gate_name)}</div></div>""" for a in actions[:8]) or "<div class='muted'>No open make-ready actions.</div>"
        risk_html=""
        for r in risks[:6]:
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            risk_html+=f"""<div class="action"><span class="badge {r.probability_band}">{esc(r.probability_band)}</span> <b>{esc(a.name if a else r.schedule_activity_id)}</b> · {r.risk_score:.0f}/100<div class="small">{esc(r.explanation)}</div></div>"""
        body=hero+kpis+f"<div class='grid2'><div class='card'><h2>Handle first</h2>{action_html}</div><div class='card'><h2>What may hurt next</h2>{risk_html}</div></div>"
        return shell("Daily Command",body,company,user,projects,p)
    finally: db.close()

@app.get("/schedule",response_class=HTMLResponse)
def schedule(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).order_by(ScheduleActivity.planned_start).all()
        rows="".join(f"<tr><td>{esc(a.external_id)}</td><td><b>{esc(a.name)}</b></td><td>{esc(a.trade)}</td><td>{esc(a.planned_start)}</td><td>{esc(a.planned_finish)}</td><td>{float(a.percent_complete or 0):.0f}%</td><td><span class='badge {esc(a.status)}'>{esc(a.status)}</span></td></tr>" for a in acts)
        body=f"""<div class="hero"><div class="eyebrow">Schedule + Lookahead</div><h1>Field execution plan</h1><div class="muted">Master schedule activities organized for field coordination.</div></div><div class="card"><table><tr><th>ID</th><th>Activity</th><th>Trade</th><th>Start</th><th>Finish</th><th>%</th><th>Status</th></tr>{rows}</table></div>"""
        return shell("Schedule",body,c,u,ps,p)
    finally: db.close()

@app.get("/make-ready",response_class=HTMLResponse)
def make_ready(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        rows=db.query(MakeReadyAction).filter(MakeReadyAction.project_id==p.id).order_by(MakeReadyAction.required_by.asc()).all()
        html=""
        for r in rows:
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            html+=f"""<div class="card"><span class="badge {esc(r.priority)}">{esc(r.priority)}</span> <b>{esc(a.name if a else r.title)}</b><h3>{esc(r.title)}</h3><p>{esc(r.reason)}</p><div class="small">Clear by {esc(r.required_by)} · Owner {esc(r.responsible_type)} · Escalation {r.escalation_level}</div></div>"""
        body=f"""<div class="hero"><div class="eyebrow">Predictive Make-Ready</div><h1>Clear tomorrow's blockers before they hit the field.</h1></div>{html or "<div class='card muted'>No make-ready actions.</div>"}"""
        return shell("Make Ready",body,c,u,ps,p)
    finally: db.close()

@app.get("/field",response_class=HTMLResponse)
def field(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).all()
        opts="".join(f"<option value='{a.id}'>{esc(a.external_id)} — {esc(a.name)}</option>" for a in acts)
        body=f"""<div class="hero"><div class="eyebrow">Field Execution</div><h1>Capture what changed today.</h1><div class="muted">Fast superintendent entry for progress, blockers and coordination.</div></div>
        <div class="grid2"><div class="card"><h2>Quick update</h2><form method="post" action="/field/update"><input type="hidden" name="project_id" value="{p.id}"><label>Activity</label><select name="activity_id">{opts}</select><br><br><label>Update</label><textarea name="text" placeholder="What changed in the field?"></textarea><br><br><label>Type</label><select name="update_type"><option>PROGRESS</option><option>BLOCKER</option><option>SAFETY</option><option>QUALITY</option><option>INSPECTION</option><option>DELIVERY</option></select><br><br><button>Save update</button></form></div>
        <div class="card"><h2>Field principle</h2><p>Capture once, then let the project brain route the information into risk, readiness, production and follow-up.</p></div></div>"""
        return shell("Field",body,c,u,ps,p)
    finally: db.close()

@app.post("/field/update")
def field_update(project_id:int=Form(...),activity_id:int=Form(...),text:str=Form(...),update_type:str=Form("PROGRESS")):
    db=SessionLocal()
    try:
        # Best-effort direct insert using known service if available
        try:
            from field_execution.service import record_field_update
            company,user,_,_=get_context(db,project_id)
            record_field_update(db,project_id,text,user.id,activity_id,None,update_type,0)
        except Exception:
            pass
    finally: db.close()
    return RedirectResponse(url=f"/field?project_id={project_id}",status_code=303)

@app.get("/subcontractors",response_class=HTMLResponse)
def subcontractors(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        subs=db.query(Subcontractor).filter(Subcontractor.company_id==c.id).all()
        cards=""
        for s in subs:
            commits=db.query(LookaheadCommitment).filter(LookaheadCommitment.project_id==p.id,LookaheadCommitment.subcontractor_id==s.id).count()
            cards+=f"<div class='card'><h3>{esc(s.name)}</h3><div class='muted'>{esc(s.trade)}</div><p>{commits} lookahead commitment(s)</p></div>"
        body=f"""<div class="hero"><div class="eyebrow">Subcontractor Intelligence</div><h1>Trade partners in the operating loop.</h1></div><div class="grid3">{cards}</div>"""
        return shell("Subcontractors",body,c,u,ps,p)
    finally: db.close()

@app.get("/production",response_class=HTMLResponse)
def production(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==p.id).all()
        opts="".join(f"<option value='{a.id}'>{esc(a.external_id)} — {esc(a.name)}</option>" for a in acts)
        rows=db.query(ProductionRecord).filter(ProductionRecord.project_id==p.id).order_by(ProductionRecord.work_date.desc()).limit(20).all()
        table="".join(f"<tr><td>{esc(r.work_date)}</td><td>{r.crew_size}</td><td>{r.quantity_installed:.1f}</td><td>{esc(r.quantity_unit)}</td><td>{r.planned_quantity:.1f}</td></tr>" for r in rows)
        body=f"""<div class="hero"><div class="eyebrow">Production Intelligence</div><h1>Measure the job before the schedule update does.</h1></div>
        <div class="grid2"><div class="card"><h2>Record production</h2><form method="post" action="/production/add"><input type="hidden" name="project_id" value="{p.id}"><select name="activity_id">{opts}</select><br><br><input name="crew_size" type="number" placeholder="Crew size"><br><br><input name="quantity" type="number" step="0.01" placeholder="Quantity installed"><br><br><input name="planned_quantity" type="number" step="0.01" placeholder="Planned quantity"><br><br><input name="unit" placeholder="Unit (LF/SF/CY/EA)"><br><br><button>Record</button></form></div>
        <div class="card"><h2>Recent production</h2><table><tr><th>Date</th><th>Crew</th><th>Installed</th><th>Unit</th><th>Plan</th></tr>{table}</table></div></div>"""
        return shell("Production",body,c,u,ps,p)
    finally: db.close()

@app.post("/production/add")
def production_add(project_id:int=Form(...),activity_id:int=Form(...),crew_size:int=Form(0),quantity:float=Form(0),planned_quantity:float=Form(0),unit:str=Form("")):
    db=SessionLocal()
    try:
        from production.service import record_production
        record_production(db,project_id,activity_id,crew_size=crew_size,quantity_installed=quantity,planned_quantity=planned_quantity,quantity_unit=unit)
        refresh_project_production(db,project_id)
    finally: db.close()
    return RedirectResponse(url=f"/production?project_id={project_id}",status_code=303)

@app.get("/risk",response_class=HTMLResponse)
def risk(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        rows=sorted(risk_latest(db,p.id),key=lambda x:x.risk_score,reverse=True)
        cards=""
        for r in rows:
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            cards+=f"""<div class="card"><span class="badge {r.probability_band}">{esc(r.probability_band)}</span><h3>{esc(a.name if a else r.schedule_activity_id)} · {r.risk_score:.0f}/100</h3><p>{esc(r.explanation)}</p><div class="small">Drift {r.schedule_drift_points:.1f} · Constraints {r.constraint_points:.1f} · Production {r.production_points:.1f} · Downstream {r.downstream_points:.1f}</div></div>"""
        body=f"""<div class="hero"><div class="eyebrow">Predictive Risk</div><h1>What is most likely to hurt the job next?</h1><form method="post" action="/risk/refresh"><input type="hidden" name="project_id" value="{p.id}"><button>Refresh risk scan</button></form></div>{cards}"""
        return shell("Predictive Risk",body,c,u,ps,p)
    finally: db.close()

@app.post("/risk/refresh")
def risk_refresh(project_id:int=Form(...)):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        run_project_risk(db,p.id,c.id)
    finally: db.close()
    return RedirectResponse(url=f"/risk?project_id={project_id}",status_code=303)

@app.get("/recovery",response_class=HTMLResponse)
def recovery(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        plans=db.query(ApprovedRecoveryPlan).filter(ApprovedRecoveryPlan.project_id==p.id).order_by(ApprovedRecoveryPlan.created_at.desc()).all()
        html="".join(f"<div class='card'><span class='badge {esc(pl.status)}'>{esc(pl.status)}</span><h3>Recovery plan #{pl.id}</h3><div>Predicted recovery: {pl.predicted_days_recovered:.1f} day(s)</div><div>Predicted cost: ${pl.predicted_cost:,.0f}</div><div class='small'>Target {esc(pl.target_completion)}</div></div>" for pl in plans)
        body=f"""<div class="hero"><div class="eyebrow">Recovery Intelligence</div><h1>Test recovery before spending money.</h1><div class="muted">The full optimizer/sandbox remains in the advanced product UI; this view tracks approved plans and outcomes.</div></div>{html or "<div class='card muted'>No approved recovery plans yet.</div>"}"""
        return shell("Recovery",body,c,u,ps,p)
    finally: db.close()

@app.get("/memory",response_class=HTMLResponse)
def memory(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        pats=db.query(CompanyKnowledgePattern).filter(CompanyKnowledgePattern.company_id==c.id).order_by(CompanyKnowledgePattern.confidence.desc()).all()
        rules=db.query(CompanyPlaybookRule).filter(CompanyPlaybookRule.company_id==c.id,CompanyPlaybookRule.active==True).all()
        phtml="".join(f"<div class='card'><h3>{esc(x.pattern_type)} · {esc(x.category)}</h3><p>{esc(x.recommendation)}</p><div class='small'>{x.sample_count} samples · confidence {x.confidence:.0%}</div></div>" for x in pats[:20])
        body=f"""<div class="hero"><div class="eyebrow">Company Construction Memory</div><h1>Every project makes the next one smarter.</h1><form method="post" action="/memory/refresh"><input type="hidden" name="project_id" value="{p.id}"><button>Capture + refresh memory</button></form></div><div class='grid2'><div>{phtml or "<div class='card muted'>No patterns yet.</div>"}</div><div class='card'><h2>Active playbooks</h2><div class='kpi'>{len(rules)}</div><div class='muted'>Evidence-backed company routines</div></div></div>"""
        return shell("Company Memory",body,c,u,ps,p)
    finally: db.close()

@app.post("/memory/refresh")
def memory_refresh(project_id:int=Form(...)):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        capture_project_learning(db,c.id,p.id)
        refresh_company_patterns(db,c.id)
        refresh_company_benchmarks(db,c.id)
    finally: db.close()
    return RedirectResponse(url=f"/memory?project_id={project_id}",status_code=303)

@app.get("/playbooks",response_class=HTMLResponse)
def playbooks(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        rules=db.query(CompanyPlaybookRule).filter(CompanyPlaybookRule.company_id==c.id,CompanyPlaybookRule.active==True).order_by(CompanyPlaybookRule.confidence.desc()).all()
        html="".join(f"<div class='card'><h3>{esc(r.rule_name)}</h3><p>{esc(r.recommended_action)}</p><div class='small'>Confidence {r.confidence:.0%} · {esc(r.evidence_summary)}</div></div>" for r in rules)
        body=f"""<div class="hero"><div class="eyebrow">Company Playbooks</div><h1>Turn repeated evidence into how your company runs work.</h1></div>{html or "<div class='card muted'>Playbooks appear after enough company evidence is collected.</div>"}"""
        return shell("Playbooks",body,c,u,ps,p)
    finally: db.close()

@app.get("/portfolio",response_class=HTMLResponse)
def portfolio(project_id:int|None=None):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        rows=db.query(ExecutiveRiskSnapshot).filter(ExecutiveRiskSnapshot.company_id==c.id).order_by(ExecutiveRiskSnapshot.created_at.desc()).all()
        latest={}
        for r in rows: latest.setdefault(r.project_id,r)
        html=""
        for r in sorted(latest.values(),key=lambda x:x.executive_score,reverse=True):
            pp=db.query(Project).filter(Project.id==r.project_id).first()
            html+=f"""<div class="card"><h2>{esc(pp.name if pp else r.project_id)}</h2><div class="grid4"><div><div class="label">Attention</div><div class="kpi">{r.executive_score:.0f}</div></div><div><div class="label">Critical</div><div class="kpi">{r.critical_risk_count}</div></div><div><div class="label">High</div><div class="kpi">{r.high_risk_count}</div></div><div><div class="label">Make Ready</div><div class="kpi">{r.open_make_ready_count}</div></div></div><p>{esc(r.explanation)}</p></div>"""
        body=f"""<div class="hero"><div class="eyebrow">Executive Intelligence</div><h1>Which projects need intervention?</h1><form method="post" action="/portfolio/refresh"><input type="hidden" name="project_id" value="{p.id if p else ''}"><button>Refresh portfolio</button></form></div>{html or "<div class='card muted'>Refresh portfolio intelligence to populate this view.</div>"}"""
        return shell("Portfolio",body,c,u,ps,p)
    finally: db.close()

@app.post("/portfolio/refresh")
def portfolio_refresh(project_id:int=Form(...)):
    db=SessionLocal()
    try:
        c,u,ps,p=get_context(db,project_id)
        refresh_executive_snapshots(db,c.id)
    finally: db.close()
    return RedirectResponse(url=f"/portfolio?project_id={project_id}",status_code=303)
