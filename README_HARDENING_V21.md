# Construction AI — Hardened Foundation v2.1

Yes, new features can be added later. v2.1 is intentionally designed to make that easier.

## What changed

### Security boundary
- AuthContext
- project/company tenant guard
- project access checks separated from UI
- future OIDC/JWT integration point

### Storage abstraction
The app no longer has to assume local disk forever.
`get_storage_backend()` can later return:
- S3
- Cloudflare R2
- Azure Blob
- Google Cloud Storage

### Background job abstraction
Long-running work such as:
- blueprint parsing
- visual analysis
- embeddings
- revision comparison
- notifications
can later move from inline execution to a real queue without rewriting every caller.

Future choices:
- Celery
- RQ
- Arq
- Dramatiq
- managed cloud queues

### Alembic migration scaffold
Database changes can become versioned instead of relying on `create_all()`.

### Observability
Structured event logging + database health check foundation.

### Integration contracts
Schedule, document, RFI and submittal integrations now have adapter interfaces.
This keeps future Procore/Autodesk/P6/etc. connectors from becoming hard-wired into the core brain.

## Why this matters

The product should remain modular:

Core platform
→ project data
→ source-backed construction knowledge
→ readiness graph
→ role workflows
→ optional integrations/features

That allows later additions such as:
- estimating
- cost controls
- BIM
- drones
- equipment tracking
- owner portals
- QA/QC
- commissioning
- warranty
- AI agents
without rebuilding the core architecture.

## Immediate next hardening tasks
1. Generate first Alembic migration from a clean database.
2. Replace demo auth with a real OIDC provider.
3. Enforce tenant guard on every API route.
4. Move document storage behind S3/R2-compatible storage.
5. Move ingestion to a queue worker.
6. Replace JSON embeddings with pgvector.
7. Add integration tests against PostgreSQL.
8. Add backup/restore procedures.
9. Add secrets management.
10. Add error monitoring and metrics.
11. Add rate limiting and request IDs.
12. Add real email/push provider behind the outbound notification queue.

## Product rule

Features can be added later as modules as long as they depend on stable domain objects:
Company, User, Project, Activity, Document, Subcontractor, Constraint, Change, Inspection,
Test, Procurement Item, and Readiness dependency.
