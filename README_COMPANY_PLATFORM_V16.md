# Construction AI — Company Platform v1.6

v1.6 changes the product architecture from a single-project application into a
multi-project contractor platform.

## Platform hierarchy

Company
→ Users
→ Projects
→ Project Team / Roles
→ Project Documents / Schedule / Subs / RFIs / Permits / Inspections / Testing
→ Project-specific AI knowledge and Readiness Graph

## Company roles
- owner_admin
- operations_manager
- project_executive
- project_manager
- superintendent
- assistant_superintendent
- project_engineer
- foreman
- viewer

## Project roles
A user can have a different role on each project and can be marked as a primary assignment.

Examples:
- Superintendent A runs Project 101.
- Superintendent B runs Project 102.
- A project executive can oversee both.
- A PE can support several projects.
- Company leadership can see portfolio risk.

## Company Portfolio
The portfolio service aggregates:
- active projects
- blocking constraints
- subcontractor delay reports
- pending subcontractor commitments
- failed document ingestion
- schedule activity counts
- project-level risk scores

## Tenant separation
Project access is checked against the user's company and project membership/role.
Company-level leadership roles can access company projects; project users only see
projects assigned to them.

This is still a prototype authorization layer. Production must enforce tenancy at the
API/database-query layer on every request, not only in the UI.

## Run

    pip install -r requirements.txt
    python production_bootstrap.py
    streamlit run company_platform_app.py

Use:
- `company_platform_app.py` for portfolio/team administration
- `app_production.py` for project documents/search
- `lookahead_app.py` for lookahead/subcontractor workflows

## Next build

v1.7 should unify these separate prototype apps into one role-aware navigation shell and add:
- superintendent home screen
- PM/project executive home screen
- company operations dashboard
- subcontractor portal
- persistent schedule import/mapping UI
- company-wide subcontractor directory
- project switching
- notifications/inbox
- approval queue
- project setup wizard
