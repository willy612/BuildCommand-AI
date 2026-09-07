# Construction AI — Production Foundation v1.1

This build begins converting the prototype into a real multi-project SaaS foundation.

## Added in v1.1

### Persistent database
SQLAlchemy models for:
- companies
- users
- projects
- project members
- documents
- task states
- permits
- inspections
- tests
- procurement items
- RFIs
- constraints
- audit logs

Local development defaults to SQLite.
Docker Compose includes PostgreSQL for a production-style local stack.

### Accounts and permissions
Role scaffold:
- owner_admin
- project_manager
- superintendent
- project_engineer
- foreman
- viewer

### Project file storage
A document storage service copies uploaded files into project-specific storage folders
and calculates SHA-256 hashes.

### Audit trail
Project changes can now be logged with:
- user
- project
- action
- entity type/id
- serialized payload
- timestamp

### Persistent project state adapter
The existing readiness, schedule, procurement and command-center brains can now consume
project data loaded from SQL records instead of only Streamlit session state.

### Notifications scaffold
Priority 1 project actions become critical notifications.
Priority 2 actions become warning notifications.

### Deployment foundation
- Dockerfile
- docker-compose.yml
- PostgreSQL service
- environment configuration
- separate `app_production.py`

## Run locally with SQLite

    pip install -r requirements.txt
    python production_bootstrap.py
    streamlit run app_production.py

## Run with Docker/PostgreSQL

    docker compose up --build

Then open the Streamlit app on port 8501.

## What should come next

The strongest production sequence is:

1. Replace demo auth with Clerk/Auth0/OIDC
2. Add Alembic database migrations
3. Add persistent document upload UI
4. Build background-safe ingestion jobs or explicit queued processing
5. Add object/cloud storage (S3/R2/Azure Blob)
6. Add vector search / semantic retrieval
7. Add P6/MS Project/CSV schedule import
8. Add RFI/submittal CSV/API import
9. Build jurisdiction data tables + source verification dates
10. Build OSHA/safety source library with source citations
11. Add mobile-first UI
12. Add microphone transcription workflow
13. Add field photo storage/area tagging
14. Add email/push/in-app notifications
15. Add company/project audit screens
16. Add billing/Stripe
17. Add automated tests, security hardening, backups and observability
18. Split backend into FastAPI service when ready to leave Streamlit prototype UI

## Important

This software remains construction decision support.
Approved plans/specifications, responsible design professionals, contractors, inspectors,
AHJs, safety requirements and real field conditions control the work.
