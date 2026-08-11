import streamlit as st
from db.session import init_db,SessionLocal
from db.models import Company,User,Project
from services.project_service import create_project
from platform.team_service import create_user,assign_project_role,project_team
from platform.tenancy import accessible_projects
from portfolio.dashboard import company_portfolio

st.set_page_config(page_title="Construction AI Company Platform",page_icon="🏗️",layout="wide")
init_db(); db=SessionLocal()
st.title("🏗️ Construction AI — Company Platform v1.6")

companies=db.query(Company).all()
if not companies:
    st.warning("Run production_bootstrap.py first.")
    st.stop()

company_map={c.name:c for c in companies}
company=company_map[st.sidebar.selectbox("Company",list(company_map.keys()))]
users=db.query(User).filter(User.company_id==company.id,User.active==True).all()

if not users:
    st.info("No active users.")
    st.stop()

user_map={f"{u.display_name or u.email} — {u.role}":u for u in users}
user=user_map[st.sidebar.selectbox("Signed-in user (prototype)",list(user_map.keys()))]

tabs=st.tabs(["My Projects","Company Portfolio","Project Teams","Create Project","Company Users"])

with tabs[0]:
    st.markdown("### My Projects")
    projects=accessible_projects(db,user)
    if not projects: st.info("No projects assigned.")
    for p in projects:
        st.markdown(f"**{p.project_number or '—'} — {p.name}**")
        st.caption(f"{p.jurisdiction or 'Jurisdiction not set'} | {p.status}")

with tabs[1]:
    st.markdown("### Company Portfolio")
    rows=company_portfolio(db,company.id)
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Active projects",sum(1 for r in rows if r["status"]=="ACTIVE"))
    c2.metric("Blocking constraints",sum(r["blocking_constraints"] for r in rows))
    c3.metric("Reported sub delays",sum(r["sub_delays"] for r in rows))
    c4.metric("Pending commitments",sum(r["pending_commitments"] for r in rows))
    for r in rows:
        st.markdown(f"**{r['project_number'] or '—'} — {r['name']}**")
        st.caption(
            f"Risk score {r['portfolio_risk_score']} | blockers {r['blocking_constraints']} | "
            f"sub delays {r['sub_delays']} | pending commitments {r['pending_commitments']}"
        )

with tabs[2]:
    st.markdown("### Project Teams")
    projects=db.query(Project).filter(Project.company_id==company.id).all()
    if projects:
        pmap={f"{p.project_number} — {p.name}":p for p in projects}
        p=pmap[st.selectbox("Project",list(pmap.keys()),key="team_project")]
        st.markdown("#### Current Team")
        for member,assignment in project_team(db,p.id):
            st.write(f"**{member.display_name or member.email}** — {assignment.role}" + (" — Primary" if assignment.primary_assignment else ""))
        st.markdown("#### Assign User")
        assign_user=user_map[st.selectbox("User",list(user_map.keys()),key="assign_user")]
        role=st.selectbox("Project role",[
            "project_executive","project_manager","superintendent","assistant_superintendent",
            "project_engineer","foreman","viewer"
        ])
        primary=st.checkbox("Primary assignment")
        if st.button("Assign to project"):
            assign_project_role(db,p.id,assign_user.id,role,primary)
            st.success("Project role assigned.")
            st.rerun()

with tabs[3]:
    st.markdown("### Create Project")
    name=st.text_input("Project name",key="new_project_name")
    number=st.text_input("Project number",key="new_project_number")
    ptype=st.text_input("Project type")
    jurisdiction=st.text_input("Jurisdiction",key="new_project_jurisdiction")
    owner=st.text_input("Owner")
    architect=st.text_input("Architect")
    if st.button("Create company project"):
        if name.strip():
            p=create_project(
                db,company_id=company.id,name=name.strip(),project_number=number.strip(),
                project_type=ptype.strip(),jurisdiction=jurisdiction.strip(),
                owner=owner.strip(),architect=architect.strip(),general_contractor=company.name
            )
            st.success(f"Created {p.name}.")
            st.rerun()

with tabs[4]:
    st.markdown("### Company Users")
    for u in users:
        st.write(f"**{u.display_name or u.email}** — {u.role} — {u.email}")
    st.markdown("#### Add User")
    email=st.text_input("Email",key="new_user_email")
    display=st.text_input("Name",key="new_user_name")
    role=st.selectbox("Company role",[
        "owner_admin","operations_manager","project_executive","project_manager",
        "superintendent","assistant_superintendent","project_engineer","foreman","viewer"
    ],key="new_user_role")
    if st.button("Create user"):
        if email.strip():
            create_user(db,company.id,email.strip(),display.strip(),role)
            st.success("User created.")
            st.rerun()

db.close()
