# Construction AI — App-Ready Foundation v2.5

This build is the point where the architecture is ready to move from iterative prototype
development into application implementation and deployment work.

## What is ready

### Multi-company SaaS model
- companies
- users
- projects
- role assignments
- project/team access
- company portfolio

### Core construction intelligence
- construction sequence brain
- plans/spec ingestion
- visual plan analysis
- readiness engine
- readiness graph
- schedule
- submittals/procurement
- lookahead
- subcontractor commitments
- field intelligence
- daily reports
- constraints
- change intelligence
- cost/change events
- closeout/warranty

### Platform infrastructure
- FastAPI backend foundation
- PostgreSQL-ready SQLAlchemy models
- Alembic scaffold
- tenant security guard
- auth-provider abstraction
- object-storage abstraction
- queue abstraction
- vector-search abstraction
- structured logging/health
- integration adapters
- Docker stack

## Still required before real customer launch

These are launch-hardening tasks, not product-architecture blockers:

1. Pick a real auth provider and implement OIDC/JWT verification.
2. Generate and test initial Alembic migrations on clean Postgres.
3. Move file storage to S3/R2/Azure and test upload/download lifecycle.
4. Replace inline queue with a worker queue.
5. Move embeddings to pgvector.
6. Add API tenant tests for every route.
7. Add real email/push provider behind approval queue.
8. Add secrets management.
9. Add error monitoring/metrics.
10. Add backups and restore drills.
11. Add rate limiting and request IDs.
12. Build production web/mobile UI from the role workflows.
13. Run pilot projects with real superintendents and subs.
14. Add billing only after workflow/product validation.

## Recommended next move

Stop expanding the backend feature list.

Begin the actual application UX:
- login
- company/project selector
- superintendent home
- project setup wizard
- plan/document upload
- schedule import
- lookahead board
- subcontractor portal
- field update/photo capture
- approvals/inbox
- change review
- portfolio dashboard

The architecture is intentionally modular so new features can be added later without
rebuilding the platform core.
