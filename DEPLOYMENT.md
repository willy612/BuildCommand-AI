# Construction AI deployment guide

## Development
Use an isolated virtual environment and install dependencies from requirements.txt.
Run the Streamlit application using app_v3.py.

## Production architecture target
- managed PostgreSQL
- application service behind TLS
- SSO / OIDC identity provider
- managed secrets
- Redis or durable queue for background work
- transactional email/SMS providers
- object storage for documents/imports
- centralized logs, metrics and tracing
- automated backups + tested restore
- staging environment mirroring production
- CI pipeline with unit, integration, tenant-isolation and end-to-end tests

## Mandatory gates before external pilot
- tenant isolation tests pass
- backup/restore tested
- SSO configured
- external communications require explicit approval
- P6/MS Project integration validated against representative schedules
- schedule calculations checked by a qualified scheduler
- security review complete
- privacy/retention policy implemented
- subcontractor analytics reviewed for legal/fairness concerns
- field mobile workflow tested with actual superintendents
