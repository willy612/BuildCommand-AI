import streamlit as st
from datetime import date
from db.session import init_db,SessionLocal
from db.models import Company,User,Project,ScheduleActivity,ConstraintProposal
from platform.tenancy import accessible_projects
from field_execution.service import record_field_update,get_or_create_daily_report,update_daily_report
from field_execution.constraint_intelligence import propose_constraint,approve_constraint

st.set_page_config(page_title="Field Execution",page_icon="🏗️",layout="wide")
init_db(); db=SessionLocal()
st.title("🏗️ Field Execution Command v1.8")

companies=db.query(Company).all()
if not companies: st.stop()
company={c.name:c for c in companies}[st.sidebar.selectbox("Company",[c.name for c in companies])]
users=db.query(User).filter(User.company_id==company.id,User.active==True).all()
user={f"{u.display_name or u.email} — {u.role}":u for u in users}[st.sidebar.selectbox("User",[f"{u.display_name or u.email} — {u.role}" for u in users])]
projects=accessible_projects(db,user)
if not projects:
    st.info("No assigned projects."); st.stop()
project={f"{p.project_number} — {p.name}":p for p in projects}[st.sidebar.selectbox("Project",[f"{p.project_number} — {p.name}" for p in projects])]

tabs=st.tabs(["Morning Command","Field Update","Constraint Review","Daily Report","Tomorrow Readiness"])

with tabs[0]:
    st.markdown("### Morning Command")
    st.write("Review today's planned work, subcontractor commitments, open constraints, inspections/tests, safety readiness, and tomorrow's exposed work before crews mobilize.")

with tabs[1]:
    st.markdown("### Record Field Update")
    activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
    amap={"General / project-wide":None}
    amap.update({f"{a.external_id} — {a.name}":a for a in activities})
    chosen=amap[st.selectbox("Activity",list(amap.keys()))]
    utype=st.selectbox("Update type",["PROGRESS","BLOCKER","SAFETY","QUALITY","INSPECTION","DELIVERY","COORDINATION"])
    text=st.text_area("What changed in the field?")
    pct=st.number_input("Proposed percent complete",0.0,100.0,0.0)
    if st.button("Record update"):
        row=record_field_update(db,project.id,text,user.id,chosen.id if chosen else None,None,utype,pct)
        proposal=propose_constraint(
            db,project.id,text,schedule_activity_id=chosen.id if chosen else None,
            source_type="FIELD_UPDATE",source_id=row.id
        )
        st.success("Field update recorded.")
        if proposal: st.warning("A potential blocking constraint was detected and sent for review.")

with tabs[2]:
    st.markdown("### Proposed Constraints")
    rows=db.query(ConstraintProposal).filter(
        ConstraintProposal.project_id==project.id,
        ConstraintProposal.status=="PENDING_REVIEW"
    ).all()
    if not rows: st.success("No pending constraint proposals.")
    for p in rows:
        with st.expander(f"{p.category} — {p.description[:80]}"):
            st.write(p.description)
            st.caption(f"Confidence {p.confidence:.0%} | {p.schedule_exposure}")
            p.responsible_party=st.text_input("Responsible party",value=p.responsible_party,key=f"owner_{p.id}")
            p.required_by=st.text_input("Required by",value=p.required_by,key=f"due_{p.id}")
            db.commit()
            if st.button("Approve into constraint log",key=f"approve_constraint_{p.id}"):
                approve_constraint(db,p.id)
                st.success("Constraint approved and added to project constraint log.")
                st.rerun()

with tabs[3]:
    st.markdown("### Daily Superintendent Report")
    report=get_or_create_daily_report(db,project.id,date.today().isoformat(),user.id)
    weather=st.text_input("Weather",value=report.weather)
    site=st.text_area("Site conditions",value=report.site_conditions)
    completed=st.text_area("Work completed",value=report.work_completed)
    delays=st.text_area("Delays / impacts",value=report.delays)
    safety=st.text_area("Safety",value=report.safety_notes)
    inspections=st.text_area("Inspections / testing",value=report.inspection_notes)
    tomorrow=st.text_area("Tomorrow plan",value=report.tomorrow_plan)
    if st.button("Save daily report"):
        update_daily_report(db,report,weather=weather,site_conditions=site,work_completed=completed,
                            delays=delays,safety_notes=safety,inspection_notes=inspections,
                            tomorrow_plan=tomorrow)
        st.success("Daily report saved.")

with tabs[4]:
    st.markdown("### Tomorrow Readiness")
    st.write("This workspace is reserved for the next readiness pass: tomorrow's scheduled work, prerequisites, sub commitments, inspections, materials, safety gates, and unresolved constraints.")

db.close()
