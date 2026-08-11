import json
import streamlit as st
from db.session import init_db,SessionLocal
from db.models import Company,User,Project,ChangeEvent,ScheduleActivity,CostChangeEvent,ScheduleImpactEvent,OutboundNotification
from platform.tenancy import accessible_projects
from schedule_impact.engine import create_schedule_impact
from cost.change_service import create_cost_change
from delivery.notification_service import queue_sub_notification,approve_notification

st.set_page_config(page_title="Construction AI v2",page_icon="🏗️",layout="wide")
init_db(); db=SessionLocal()
st.title("🏗️ Construction AI — Production Integration v2.0")

companies=db.query(Company).all()
if not companies: st.stop()
company={c.name:c for c in companies}[st.sidebar.selectbox("Company",[c.name for c in companies])]
users=db.query(User).filter(User.company_id==company.id,User.active==True).all()
user={f"{u.display_name or u.email} — {u.role}":u for u in users}[st.sidebar.selectbox("User",[f"{u.display_name or u.email} — {u.role}" for u in users])]
projects=accessible_projects(db,user)
if not projects: st.stop()
project={f"{p.project_number} — {p.name}":p for p in projects}[st.sidebar.selectbox("Project",[f"{p.project_number} — {p.name}" for p in projects])]

tabs=st.tabs(["Change Integration","Schedule Exposure","Cost Events","Outbound Notifications","API Status"])

with tabs[0]:
    st.markdown("### Change Events")
    events=db.query(ChangeEvent).filter(ChangeEvent.project_id==project.id).order_by(ChangeEvent.created_at.desc()).all()
    for e in events:
        with st.expander(f"#{e.id} — {e.title} — {e.status}"):
            st.write(e.summary)
            st.write("Affected subcontractors:",json.loads(e.affected_subcontractors_json or "[]"))
            st.write("Affected schedule:",json.loads(e.affected_schedule_json or "[]"))
            if e.possible_schedule_change:
                st.warning("Schedule exposure should be reviewed.")
            if e.possible_cost_change:
                st.warning("Potential cost exposure should be reviewed.")

with tabs[1]:
    st.markdown("### Schedule Exposure")
    events=db.query(ChangeEvent).filter(ChangeEvent.project_id==project.id).all()
    acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
    if events and acts:
        emap={f"#{e.id} — {e.title}":e for e in events}
        amap={f"{a.external_id} — {a.name}":a for a in acts}
        e=emap[st.selectbox("Change event",list(emap.keys()),key="impact_event")]
        a=amap[st.selectbox("Source schedule activity",list(amap.keys()),key="impact_activity")]
        days=st.number_input("Estimated exposure days",0.0,365.0,0.0)
        if st.button("Calculate downstream exposure"):
            row,downstream=create_schedule_impact(db,project.id,e.id,a.id,days)
            st.success(f"Schedule Impact #{row.id} created for review.")
            for x in downstream[:30]:
                st.write(f"- {x['external_id']} — {x['name']} (depth {x['depth']})")

with tabs[2]:
    st.markdown("### Cost / Change Events")
    title=st.text_input("Change title")
    desc=st.text_area("Description")
    party=st.text_input("Responsible party")
    est=st.number_input("Estimated cost",min_value=0.0,value=0.0,step=100.0)
    if st.button("Create potential cost event"):
        row=create_cost_change(db,project.id,title,desc,None,party,est)
        st.success(f"Cost Event #{row.id} created.")
    for c in db.query(CostChangeEvent).filter(CostChangeEvent.project_id==project.id).all():
        st.write(f"**#{c.id} — {c.title} — {c.status}**")
        st.caption(f"Estimate ${c.estimated_cost:,.2f} | Approved ${c.approved_cost:,.2f}")

with tabs[3]:
    st.markdown("### Outbound Notifications")
    st.caption("External sending remains approval-gated. This screen queues and approves messages; actual SMTP/provider delivery is the next deployment integration.")
    rows=db.query(OutboundNotification).filter(OutboundNotification.project_id==project.id).all()
    for n in rows:
        with st.expander(f"#{n.id} — {n.subject} — {n.status}"):
            st.write(n.body)
            st.caption(f"{n.channel} → {n.recipient or 'recipient not resolved'}")
            if n.status=="DRAFT" and st.button("Approve to send",key=f"approve_notice_{n.id}"):
                approve_notification(db,n.id)
                st.success("Approved to send.")
                st.rerun()

with tabs[4]:
    st.markdown("### API Backend")
    st.code("uvicorn api.main:app --reload --port 8000")
    st.write("Health endpoint: `/health`")
    st.write("Portfolio endpoint: `/companies/{company_id}/portfolio`")
    st.write("Project endpoint: `/projects/{project_id}`")
    st.write("Cost changes: `POST /cost-changes`")
    st.write("Schedule impacts: `POST /schedule-impacts`")

db.close()
