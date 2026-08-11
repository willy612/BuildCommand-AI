import streamlit as st
from field_media.service import save_field_photo
from quality.service import submit_verification_photo
from db.session import init_db,SessionLocal
from db.models import QualityIssue,FieldPhotoRecord,ProjectLocation
from db.models import SubcontractorPortalUser,Subcontractor,Project,ScheduleActivity
from sub_portal.service import portal_projects,portal_commitments,submit_commitment_response
from field_execution.constraint_intelligence import propose_constraint

st.set_page_config(page_title="Subcontractor Portal",page_icon="🦺",layout="wide")
init_db(); db=SessionLocal()
st.title("🦺 Subcontractor Portal")
st.caption("Confirm work, report constraints, and keep the project team ahead of delays.")

users=db.query(SubcontractorPortalUser).filter(SubcontractorPortalUser.active==True).all()
if not users:
    st.info("No subcontractor portal users configured.")
    st.stop()

umap={f"{u.display_name or u.email} — {u.email}":u for u in users}
user=umap[st.sidebar.selectbox("Portal user (prototype)",list(umap.keys()))]
sub=db.query(Subcontractor).filter(Subcontractor.id==user.subcontractor_id).first()
st.sidebar.write(f"**{sub.name if sub else 'Subcontractor'}**")

assignments=portal_projects(db,user)
projects=[]
for a in assignments:
    p=db.query(Project).filter(Project.id==a.project_id).first()
    if p: projects.append(p)
if not projects:
    st.info("No active projects assigned.")
    st.stop()

pmap={f"{p.project_number} — {p.name}":p for p in projects}
project=pmap[st.selectbox("Project",list(pmap.keys()))]

st.markdown("### Your upcoming work")
rows=portal_commitments(db,user,project.id)
if not rows: st.info("No lookahead commitments currently assigned.")

for c in rows:
    act=db.query(ScheduleActivity).filter(ScheduleActivity.id==c.schedule_activity_id).first()
    with st.expander(f"{act.name if act else 'Activity'} — {c.requested_start}"):
        st.write(f"Planned start: **{c.requested_start or 'TBD'}**")
        st.write(f"Planned finish: **{c.requested_finish or 'TBD'}**")
        status=st.selectbox("Can you meet this commitment?",[
            "CONFIRMED","DELAY_REPORTED","NEED_INFORMATION","MATERIAL_ISSUE",
            "MANPOWER_ISSUE","ACCESS_ISSUE","CANNOT_MEET_DATE"
        ],key=f"status_{c.id}")
        manpower=st.text_input("Manpower / crew commitment",key=f"mp_{c.id}")
        material=st.text_input("Material readiness",key=f"mat_{c.id}")
        equipment=st.text_input("Equipment readiness",key=f"eq_{c.id}")
        explanation=st.text_area("Issue / response",key=f"exp_{c.id}")
        if st.button("Submit response",key=f"submit_{c.id}"):
            submit_commitment_response(db,c,status,explanation,manpower,material,equipment,explanation)
            if status!="CONFIRMED":
                propose_constraint(
                    db,project.id,explanation or f"{status} reported for {act.name if act else 'activity'}",
                    schedule_activity_id=c.schedule_activity_id,
                    subcontractor_id=c.subcontractor_id,
                    source_type="SUBCONTRACTOR_RESPONSE",source_id=c.id,
                    required_by=c.requested_start
                )
            st.success("Response submitted for superintendent review.")
            st.rerun()

db.close()
