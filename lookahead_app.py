import streamlit as st
from db.session import init_db, SessionLocal
from db.models import Project, ScheduleActivity, Subcontractor, LookaheadCommitment
from brain.master_brain import load_tasks
from lookahead.engine import lookahead_activities, activity_subcontractors, ensure_commitment, commitment_health
from subcontractors.service import (
    create_subcontractor, add_contact, add_to_project, assign_activity,
    project_subcontractors, primary_email
)
from communications.lookahead import draft_lookahead_email, save_draft, parse_sub_response, save_response

st.set_page_config(page_title="Construction AI Lookahead",page_icon="🏗️",layout="wide")
init_db()
db=SessionLocal()

st.title("🏗️ Lookahead + Subcontractor Intelligence v1.5")

projects=db.query(Project).all()
if not projects:
    st.info("Create a project in app_production.py first.")
    st.stop()

project_labels={f"{p.project_number} — {p.name}":p for p in projects}
project=project_labels[st.selectbox("Project",list(project_labels.keys()))]

tabs=st.tabs(["6-Week Lookahead","Subcontractor Directory","Commitments","Responses"])

with tabs[0]:
    st.markdown("### 6-Week Lookahead")
    activities=lookahead_activities(db,project.id,weeks=6)
    if not activities:
        st.info("No scheduled activities fall within the next six weeks.")
    for a in activities:
        st.markdown(f"**{a.external_id} — {a.name}**")
        st.caption(f"{a.planned_start} → {a.planned_finish} | {a.trade or 'Trade not assigned'}")
        subs=activity_subcontractors(db,project.id,a.id)
        if not subs:
            st.warning("No subcontractor assigned.")
        for sub in subs:
            c=ensure_commitment(db,project.id,a,sub.id)
            st.write(f"{sub.name} — commitment: **{commitment_health(c)}**")
            if st.button(f"Create notice draft — {a.id}-{sub.id}",key=f"draft_{a.id}_{sub.id}"):
                email=primary_email(db,sub.id)
                draft=draft_lookahead_email(
                    project.name,sub.name,a.name,a.planned_start,a.planned_finish,c.response_due,email
                )
                save_draft(db,project.id,sub.id,a.id,draft)
                st.success("Lookahead notice draft created for project-team review.")

with tabs[1]:
    st.markdown("### Subcontractor Directory")
    c1,c2=st.columns(2)
    with c1:
        name=st.text_input("Company name")
        trade=st.text_input("Trade")
        scope=st.text_area("Scope")
        if st.button("Add subcontractor"):
            if name.strip():
                sub=create_subcontractor(db,name.strip(),trade.strip(),scope.strip())
                add_to_project(db,project.id,sub.id,trade.strip(),scope.strip())
                st.success("Subcontractor added.")
                st.rerun()
    with c2:
        subs=project_subcontractors(db,project.id)
        if subs:
            selected={f"{s.name} — {s.trade}":s for s in subs}
            sub=selected[st.selectbox("Add contact to",list(selected.keys()))]
            cname=st.text_input("Contact name")
            role=st.text_input("Contact role")
            email=st.text_input("Email")
            phone=st.text_input("Phone")
            primary=st.checkbox("Primary project contact",value=True)
            if st.button("Save contact"):
                add_contact(db,sub.id,cname,email,phone,role,primary)
                st.success("Contact saved.")

    st.markdown("### Assign Subs to Schedule Activities")
    subs=project_subcontractors(db,project.id)
    activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
    if subs and activities:
        submap={f"{s.name} — {s.trade}":s for s in subs}
        actmap={f"{a.external_id} — {a.name}":a for a in activities}
        sub=submap[st.selectbox("Subcontractor",list(submap.keys()),key="assign_sub")]
        act=actmap[st.selectbox("Schedule activity",list(actmap.keys()),key="assign_act")]
        if st.button("Assign to activity"):
            assign_activity(db,project.id,act.id,sub.id)
            st.success("Assigned.")

with tabs[2]:
    st.markdown("### Commitment Board")
    rows=db.query(LookaheadCommitment).filter(LookaheadCommitment.project_id==project.id).all()
    for c in rows:
        sub=db.query(Subcontractor).filter(Subcontractor.id==c.subcontractor_id).first()
        act=db.query(ScheduleActivity).filter(ScheduleActivity.id==c.schedule_activity_id).first()
        st.write(f"**{sub.name if sub else c.subcontractor_id} — {act.name if act else c.schedule_activity_id}**")
        st.caption(f"Start {c.requested_start} | Response due {c.response_due} | Status {c.status}")

with tabs[3]:
    st.markdown("### Record / Interpret Subcontractor Response")
    subs=project_subcontractors(db,project.id)
    activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
    if subs and activities:
        submap={f"{s.name} — {s.trade}":s for s in subs}
        actmap={f"{a.external_id} — {a.name}":a for a in activities}
        sub=submap[st.selectbox("Responding subcontractor",list(submap.keys()),key="resp_sub")]
        act=actmap[st.selectbox("Related activity",list(actmap.keys()),key="resp_act")]
        text=st.text_area("Response text",placeholder="We can meet the date, but material will arrive two days late...")
        if st.button("Interpret response"):
            parsed=parse_sub_response(text)
            save_response(db,project.id,sub.id,act.id,parsed)
            st.json(parsed)
            st.info("Response saved as PENDING_REVIEW. The superintendent must approve any resulting project-state change.")

db.close()
