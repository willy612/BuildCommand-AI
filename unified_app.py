import streamlit as st

from db.session import init_db,SessionLocal
from db.models import Company,User,Project,Subcontractor,ScheduleActivity
from platform.tenancy import accessible_projects,user_can_access_project
from platform.team_service import project_team
from portfolio.dashboard import company_portfolio
from approvals.service import pending_approvals,decide_approval
from inbox.service import user_inbox,mark_read
from setup_wizard.service import refresh_progress,percent_complete
from services.state_service import load_project_state
from brain.master_brain import load_tasks
from brain.command_center import command_summary,priority_actions
from brain.readiness_graph import build_readiness_graph,graph_metrics
from lookahead.engine import lookahead_activities

st.set_page_config(page_title="Construction AI",page_icon="🏗️",layout="wide")
init_db(); db=SessionLocal()

st.title("🏗️ Construction AI — Unified Platform v1.7")

companies=db.query(Company).all()
if not companies:
    st.warning("Run production_bootstrap.py first.")
    st.stop()

company_map={c.name:c for c in companies}
company=company_map[st.sidebar.selectbox("Company",list(company_map.keys()))]

users=db.query(User).filter(User.company_id==company.id,User.active==True).all()
if not users:
    st.stop()
user_map={f"{u.display_name or u.email} — {u.role}":u for u in users}
user=user_map[st.sidebar.selectbox("User",list(user_map.keys()))]

projects=accessible_projects(db,user)
project=None
if projects:
    pmap={f"{p.project_number} — {p.name}":p for p in projects}
    project=pmap[st.sidebar.selectbox("Project",list(pmap.keys()))]

st.sidebar.divider()
unread=user_inbox(db,company.id,user.id)
st.sidebar.metric("Unread inbox",len(unread))
st.sidebar.metric("Pending approvals",len(pending_approvals(db,project.id if project else None)))

role=user.role
if role in ("owner_admin","operations_manager","project_executive"):
    home_label="Operations / Executive Home"
elif role in ("project_manager","project_engineer"):
    home_label="PM / PE Home"
elif role in ("superintendent","assistant_superintendent","foreman"):
    home_label="Superintendent Home"
else:
    home_label="Project Home"

tabs=st.tabs([home_label,"Project","Approvals","Inbox","Setup Wizard","Portfolio"])

with tabs[0]:
    st.markdown(f"### {home_label}")
    if role in ("owner_admin","operations_manager","project_executive"):
        rows=company_portfolio(db,company.id)
        c1,c2,c3,c4=st.columns(4)
        c1.metric("Projects",len(rows))
        c2.metric("Blocking constraints",sum(r["blocking_constraints"] for r in rows))
        c3.metric("Sub delays",sum(r["sub_delays"] for r in rows))
        c4.metric("Pending commitments",sum(r["pending_commitments"] for r in rows))
        for r in rows[:10]:
            st.write(f"**{r['project_number']} — {r['name']}** — risk {r['portfolio_risk_score']}")
    elif project:
        state=load_project_state(db,project.id)
        tasks=load_tasks()
        summary=command_summary(state,tasks)
        actions=priority_actions(state,tasks)
        c1,c2,c3=st.columns(3)
        c1.metric("READY",summary.get("READY",0))
        c2.metric("HOLD",summary.get("HOLD",0))
        c3.metric("AT RISK",summary.get("AT_RISK",0))
        st.markdown("#### What should I handle first?")
        if not actions: st.success("No critical actions recorded.")
        for a in actions[:8]:
            st.write(f"**P{a['priority']} — {a['category']} — {a['title']}**")
            st.caption(a["reason"])
        st.markdown("#### 6-week lookahead")
        for a in lookahead_activities(db,project.id,weeks=6)[:12]:
            st.write(f"**{a.external_id} — {a.name}**")
            st.caption(f"{a.planned_start} → {a.planned_finish}")
    else:
        st.info("No project assigned.")

with tabs[1]:
    if not project:
        st.info("Choose a project.")
    else:
        st.markdown(f"### {project.name}")
        st.caption(f"{project.project_number} | {project.jurisdiction} | {project.status}")
        st.markdown("#### Project Team")
        for member,assignment in project_team(db,project.id):
            st.write(f"**{member.display_name or member.email}** — {assignment.role}")

        state=load_project_state(db,project.id)
        tasks=load_tasks()
        graph=build_readiness_graph(state,tasks)
        metrics=graph_metrics(graph)
        c1,c2,c3=st.columns(3)
        c1.metric("Graph nodes",metrics["nodes"])
        c2.metric("HOLD nodes",metrics["holds"])
        c3.metric("AT RISK nodes",metrics["at_risk"])

with tabs[2]:
    st.markdown("### Approval Queue")
    rows=pending_approvals(db,project.id if project else None)
    if not rows: st.success("No pending approvals.")
    for r in rows:
        with st.expander(f"{r.approval_type} — {r.title}"):
            st.write(r.summary)
            note=st.text_area("Decision note",key=f"note_{r.id}")
            c1,c2=st.columns(2)
            with c1:
                if st.button("Approve",key=f"approve_{r.id}"):
                    decide_approval(db,r.id,user.id,True,note)
                    st.success("Approved."); st.rerun()
            with c2:
                if st.button("Reject",key=f"reject_{r.id}"):
                    decide_approval(db,r.id,user.id,False,note)
                    st.warning("Rejected."); st.rerun()

with tabs[3]:
    st.markdown("### Inbox / Notifications")
    rows=user_inbox(db,company.id,user.id,include_read=True)
    if not rows: st.info("Inbox is empty.")
    for r in rows[:50]:
        prefix="🔴" if r.severity=="critical" else "🟠" if r.severity=="warning" else "🔵"
        st.write(f"{prefix} **{r.title}** — {r.status}")
        st.caption(r.message)
        if r.status=="UNREAD" and st.button("Mark read",key=f"read_{r.id}"):
            mark_read(db,r.id); st.rerun()

with tabs[4]:
    if not project:
        st.info("Choose a project.")
    else:
        st.markdown("### Guided Project Setup")
        p=refresh_progress(db,project)
        pct=percent_complete(p)
        st.progress(pct/100)
        st.write(f"Setup complete: **{pct}%**")
        steps=[
            ("Project/company information",p.company_info_complete),
            ("Project team",p.team_complete),
            ("Jurisdiction",p.jurisdiction_complete),
            ("Project documents",p.documents_complete),
            ("Schedule",p.schedule_complete),
            ("Subcontractors",p.subcontractors_complete),
            ("Requirements/source setup",p.requirements_complete),
        ]
        for label,done in steps:
            st.write(("✓ " if done else "○ ")+label)

with tabs[5]:
    st.markdown("### Company Portfolio")
    rows=company_portfolio(db,company.id)
    for r in rows:
        st.write(f"**{r['project_number']} — {r['name']}**")
        st.caption(
            f"Risk {r['portfolio_risk_score']} | blockers {r['blocking_constraints']} | "
            f"sub delays {r['sub_delays']} | pending commitments {r['pending_commitments']}"
        )

db.close()
