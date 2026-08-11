from db.session import init_db, SessionLocal
from db.models import (
    Company,User,Project,ScheduleActivity,PredictiveRiskSnapshot,
    MakeReadyAction,Subcontractor
)

def seed():
    init_db()
    db=SessionLocal()
    try:
        company=db.query(Company).first()
        if not company:
            company=Company(name="Summit Builders")
            db.add(company);db.commit();db.refresh(company)

        user=db.query(User).filter(User.company_id==company.id).first()
        if not user:
            user=User(company_id=company.id,email="superintendent@example.com",
                      display_name="Demo Superintendent",role="superintendent")
            db.add(user);db.commit();db.refresh(user)

        project=db.query(Project).filter(Project.company_id==company.id).first()
        if not project:
            project=Project(company_id=company.id,name="Canyon Medical Office",
                            project_number="CMO-024",project_type="Commercial",
                            jurisdiction="Demo City",general_contractor=company.name,status="ACTIVE")
            db.add(project);db.commit();db.refresh(project)

        if db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).count()==0:
            acts=[
                ("A100","Footings & Foundations","2026-08-10","2026-08-18",80,"Concrete","IN_PROGRESS"),
                ("A200","Structural Steel / Deck","2026-08-19","2026-09-04",15,"Structural","NOT_STARTED"),
                ("A300","MEP Underground / Rough","2026-08-24","2026-09-11",5,"MEP","NOT_STARTED"),
                ("A400","Interior Framing","2026-09-08","2026-09-25",0,"Framing","NOT_STARTED"),
                ("A500","Drywall Close-In","2026-09-22","2026-10-09",0,"Drywall","NOT_STARTED"),
            ]
            rows=[]
            for ext,name,s,f,pct,trade,status in acts:
                a=ScheduleActivity(project_id=project.id,external_id=ext,name=name,
                                   planned_start=s,planned_finish=f,percent_complete=pct,
                                   trade=trade,status=status)
                db.add(a);db.commit();db.refresh(a);rows.append(a)
            risks=[
                (rows[1],68,"HIGH","Steel delivery confirmation is unresolved; two downstream starts are exposed."),
                (rows[2],82,"CRITICAL","MEP rough-in is 5 days behind field plan and access remains unresolved."),
                (rows[3],44,"WATCH","Framing is dependent on MEP rough-in and inspection clearance."),
            ]
            for a,score,band,why in risks:
                db.add(PredictiveRiskSnapshot(project_id=project.id,schedule_activity_id=a.id,
                    risk_score=score,probability_band=band,constraint_points=20,
                    downstream_points=12,schedule_drift_points=20,explanation=why))
            db.add(MakeReadyAction(project_id=project.id,schedule_activity_id=rows[2].id,
                gate_name="Site conditions",title="Clear MEP rough-in access",
                reason="Material laydown is blocking east-side access.",
                responsible_type="PROJECT_TEAM",required_by="2026-08-12",
                status="OPEN",priority="CRITICAL",escalation_level=1))
            db.add(MakeReadyAction(project_id=project.id,schedule_activity_id=rows[1].id,
                gate_name="Submittals / procurement",title="Confirm steel delivery",
                reason="Fabricator delivery confirmation has not been received.",
                responsible_type="SUBCONTRACTOR",required_by="2026-08-13",
                status="OPEN",priority="HIGH",escalation_level=0))
            db.commit()

        if db.query(Subcontractor).filter(Subcontractor.company_id==company.id).count()==0:
            db.add_all([
                Subcontractor(company_id=company.id,name="Apex Concrete",trade="Concrete"),
                Subcontractor(company_id=company.id,name="Metro Steel",trade="Structural"),
                Subcontractor(company_id=company.id,name="Summit MEP",trade="MEP"),
            ])
            db.commit()

        print(f"Seeded live demo: {company.name} / {project.name}")
    finally:
        db.close()

if __name__=="__main__":
    seed()
