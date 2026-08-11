from db.session import init_db, SessionLocal
from db.models import Company, User, Project
from services.audit_service import log_action

def seed():
    init_db()
    db = SessionLocal()
    try:
        if db.query(Company).count() == 0:
            company = Company(name="Demo Construction Company")
            db.add(company)
            db.commit()
            db.refresh(company)

            user = User(
                company_id=company.id,
                email="superintendent@example.com",
                display_name="Demo Superintendent",
                role="superintendent"
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            project = Project(
                company_id=company.id,
                name="Demo Project",
                project_number="DEMO-001",
                project_type="Commercial",
                jurisdiction="Example City",
                general_contractor=company.name
            )
            db.add(project)
            db.commit()
            db.refresh(project)

            log_action(
                db,
                "SEED_PROJECT",
                project_id=project.id,
                user_id=user.id,
                entity_type="project",
                entity_id=project.id
            )
            print("Demo company/user/project created.")
        else:
            print("Database already initialized.")
    finally:
        db.close()

if __name__ == "__main__":
    seed()
