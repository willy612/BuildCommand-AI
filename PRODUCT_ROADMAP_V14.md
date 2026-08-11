# Construction AI — v1.4 Field Operations Backbone

## Added
- SQL persistence for imported schedule activities
- SQL persistence for predecessor/successor relationships
- automatic schedule-to-master-task mapping suggestions
- human-approved task mapping
- document revision activation/supersession service
- revision audit events
- mobile superintendent digest model
- quick field actions model

## Why this matters
The schedule should not be a detached file. It should become another live input into the
Construction Readiness Graph. Schedule activities can now be mapped to the construction
brain so RFIs, inspections, procurement, documents, field constraints and safety gates can
affect the activities the superintendent is actually planning.

## Next build sequence
v1.5 — Lookahead Intelligence
- 3/6 week lookahead generated from the master schedule
- readiness overlay on every lookahead activity
- constraint log generated automatically
- commitments / responsible parties / due dates
- missed commitment tracking
- daily field updates feed percent complete proposals

v1.6 — Change Intelligence
- compare drawing revisions
- identify potentially affected activities/trades
- create proposed RFIs / change events
- connect changes to schedule exposure
- preserve human approval

v1.7 — Inspection / Testing Command
- jurisdiction-driven inspection matrix
- special inspection matrix
- testing matrix
- inspection request workflow
- failed inspection corrective-action loop
- report/document attachment tracking

v1.8 — Safety Command
- activity-specific JHA templates
- OSHA/state-plan source references
- pre-task planning
- high-risk work gates
- competent-person requirements
- safety observations and corrective actions

v1.9 — Turnover / Warranty
- closeout matrix
- O&M/warranty collection
- training tracking
- commissioning/TAB/final testing
- punch closure
- warranty issue routing

v2.0 — Production Product
- API backend
- web + mobile clients
- real authentication
- object storage
- pgvector
- background job queue
- push/email notifications
- audit/security/backup/observability
- construction-platform integrations
