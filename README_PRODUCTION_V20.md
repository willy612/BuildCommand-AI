# Construction AI — Production Integration v2.0

v2.0 begins separating the intelligence/backend from the prototype UI.

## Added

### FastAPI backend
Initial API:
- health
- company portfolio
- project lookup
- create potential cost change
- create schedule-impact event

This is the beginning of a backend that can power:
- web app
- mobile app
- superintendent app
- subcontractor portal
- future integrations

### Schedule exposure
A change event can now be tied to a source schedule activity and traced through
successor relationships to identify downstream activities potentially exposed.

### Cost/change tracking
Potential cost events now have:
- title / description
- responsible party
- estimated cost
- approved cost
- status
- optional link to a Change Event

### Outbound notification queue
Messages to users/subcontractors can be queued in the database and approved for send.
Actual provider delivery remains intentionally separate from approval.

### Production split
`api.main` is the API backend.
`production_v2_app.py` is the current web control surface.
The architecture can now evolve toward independent web/mobile clients.

## Run API
    uvicorn api.main:app --reload --port 8000

## Run web
    streamlit run production_v2_app.py

## Docker
    docker compose -f docker-compose.v2.yml up --build

## Next production work

The next phase should focus on hardening instead of adding many more brains:
1. Alembic migrations
2. real authentication/OIDC
3. API-level tenant authorization
4. S3/R2/Azure object storage
5. job queue for document/vision processing
6. pgvector
7. email provider integration
8. push notifications
9. web frontend
10. mobile frontend
11. automated integration tests
12. secrets management
13. observability/logging
14. backups/disaster recovery
15. billing/subscriptions
16. external construction-platform integrations
