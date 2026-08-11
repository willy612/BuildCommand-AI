# Construction AI — Unified Role-Aware Platform v1.7

v1.7 unifies the separate company, project, lookahead, approval, and notification concepts
into one role-aware shell.

## Role-aware homes

### Superintendent / Assistant Superintendent / Foreman
- READY / HOLD / AT RISK
- priority actions
- 6-week lookahead
- project team
- project readiness graph
- approvals and inbox

### PM / Project Engineer
- project health
- approvals
- inbox
- project team
- setup status
- schedule/lookahead context

### Operations / Project Executive / Company Admin
- company portfolio
- project risk
- blocking constraints
- subcontractor delay counts
- pending commitments
- cross-project oversight

## Shared approval queue
Designed to centralize approval of:
- AI-proposed field-state changes
- subcontractor delay interpretation
- outbound communication drafts
- critical project-state changes
- future change-event/RFI recommendations

## Shared inbox
Company/project/user notifications can be surfaced with:
- critical
- warning
- informational severity
- unread/read tracking

## Project setup wizard
Tracks readiness of:
- company/project information
- team assignments
- jurisdiction
- documents
- schedule
- subcontractors
- source-backed requirements

## Project switching
Users only see projects they can access under the company/project tenancy rules.

## Subcontractor portal
The data model now includes subcontractor portal users. The next increment should provide
a dedicated limited-access portal for:
- lookahead confirmations
- delay reporting
- manpower/material readiness
- requested documents/photos
- action-item acknowledgement

## Run
    pip install -r requirements.txt
    python production_bootstrap.py
    streamlit run unified_app.py
