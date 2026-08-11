import json
import streamlit as st
from db.session import init_db,SessionLocal
from db.models import Company,User,Project,Document,DocumentPage,ScheduleActivity,ChangeEvent
from platform.tenancy import accessible_projects
from brain.master_brain import load_tasks
from change_intelligence.engine import revision_comparison,task_relevance,schedule_relevance,infer_change_flags,summarize_change
from change_events.service import create_change_event,approve_change_event

st.set_page_config(page_title="Change Intelligence",page_icon="🧠",layout="wide")
init_db(); db=SessionLocal()
st.title("🧠 Change Intelligence v1.9")

companies=db.query(Company).all()
if not companies: st.stop()
company={c.name:c for c in companies}[st.sidebar.selectbox("Company",[c.name for c in companies])]
users=db.query(User).filter(User.company_id==company.id,User.active==True).all()
user={f"{u.display_name or u.email} — {u.role}":u for u in users}[st.sidebar.selectbox("User",[f"{u.display_name or u.email} — {u.role}" for u in users])]
projects=accessible_projects(db,user)
if not projects: st.stop()
project={f"{p.project_number} — {p.name}":p for p in projects}[st.sidebar.selectbox("Project",[f"{p.project_number} — {p.name}" for p in projects])]

tabs=st.tabs(["Compare Revisions","Pending Change Events","Impact Review"])

with tabs[0]:
    docs=db.query(Document).filter(Document.project_id==project.id).all()
    if len(docs)<2:
        st.info("At least two project documents are needed for comparison.")
    else:
        dmap={f"#{d.id} {d.filename} — Rev {d.revision or '—'} — {d.status}":d for d in docs}
        old=dmap[st.selectbox("Previous document",list(dmap.keys()),key="old_doc")]
        new=dmap[st.selectbox("New document",list(dmap.keys()),key="new_doc")]
        if st.button("Compare revisions",type="primary"):
            old_pages=[{
                "sheet_number":p.sheet_number,"text_excerpt":p.text_excerpt,
                "page_number":p.page_number,"source_ref":p.source_ref
            } for p in db.query(DocumentPage).filter(DocumentPage.document_id==old.id).all()]
            new_pages=[{
                "sheet_number":p.sheet_number,"text_excerpt":p.text_excerpt,
                "page_number":p.page_number,"source_ref":p.source_ref
            } for p in db.query(DocumentPage).filter(DocumentPage.document_id==new.id).all()]
            results=revision_comparison(old_pages,new_pages)
            st.session_state["revision_results"]=results
            st.session_state["revision_docs"]=(old.id,new.id)

        results=st.session_state.get("revision_results",[])
        if results:
            tasks=load_tasks()
            schedule=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            for i,ch in enumerate(results):
                if ch["status"]=="UNCHANGED":
                    continue
                with st.expander(f"{ch['sheet_number']} — {ch['status']}",expanded=True):
                    st.write(summarize_change(ch))
                    task_hits=task_relevance(ch,tasks)
                    sched_hits=schedule_relevance(ch,schedule)
                    flags=infer_change_flags(ch)
                    st.write("**Potential construction tasks**")
                    for t in task_hits: st.write(f"- {t['name']} ({t['score']})")
                    st.write("**Potential schedule activities**")
                    for a in sched_hits: st.write(f"- {a['external_id']} — {a['name']} ({a['score']})")
                    st.write("**Possible implications**")
                    st.json(flags)
                    if st.button("Create reviewable change event",key=f"event_{i}"):
                        old_id,new_id=st.session_state["revision_docs"]
                        event=create_change_event(
                            db,project.id,f"Revision impact — {ch['sheet_number']}",
                            summarize_change(ch),source_document_id=new_id,prior_document_id=old_id,
                            affected_tasks=task_hits,affected_schedule=sched_hits,flags=flags
                        )
                        st.success(f"Change Event #{event.id} created for review.")

with tabs[1]:
    rows=db.query(ChangeEvent).filter(ChangeEvent.project_id==project.id,ChangeEvent.status=="PENDING_REVIEW").all()
    if not rows: st.success("No pending change events.")
    for e in rows:
        with st.expander(f"Change Event #{e.id} — {e.title}"):
            st.write(e.summary)
            st.write(f"Possible RFI: **{e.possible_rfi}**")
            st.write(f"Possible cost change: **{e.possible_cost_change}**")
            st.write(f"Possible schedule change: **{e.possible_schedule_change}**")
            st.write("Affected tasks:",json.loads(e.affected_tasks_json or "[]"))
            st.write("Affected schedule:",json.loads(e.affected_schedule_json or "[]"))
            st.write("Affected subcontractors:",json.loads(e.affected_subcontractors_json or "[]"))
            st.warning("This is an AI-assisted impact proposal, not an approved change directive.")
            if st.button("Approve change event for project tracking",key=f"approve_{e.id}"):
                approve_change_event(db,e.id)
                st.success("Approved for project tracking.")
                st.rerun()

with tabs[2]:
    rows=db.query(ChangeEvent).filter(ChangeEvent.project_id==project.id).order_by(ChangeEvent.created_at.desc()).all()
    for e in rows:
        st.write(f"**#{e.id} — {e.title} — {e.status}**")
        st.caption(
            f"RFI {e.possible_rfi} | Cost {e.possible_cost_change} | Schedule {e.possible_schedule_change}"
        )

db.close()
