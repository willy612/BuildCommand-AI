# Construction AI — Unified App UX v3.0

This is the first product-facing application shell built on top of the hardened backend.

## One app

Run:

    streamlit run app_v3.py

The app includes:

### Login / company context
Prototype sign-in flow using the existing company/user model.

### Role-aware navigation
Superintendents, PM/PE users, and operations/executive users land in different contexts.

### Project switching
Users only see projects they can access.

### Superintendent Home
- READY / HOLD / AT RISK
- priority actions
- 6-week lookahead
- assigned subcontractors

### Company Portfolio
- portfolio risk
- blocking constraints
- subcontractor delays
- unanswered commitments

### Project Setup
- guided completeness
- project identity
- setup checklist

### Documents
- persistent PDF upload
- ingestion
- document register

### Schedule & Lookahead
- CSV schedule import
- persistent activities
- 6-week window
- sub commitments

### Field
- activity-linked field updates
- blocker detection
- daily superintendent report

### Readiness
- readiness graph metrics
- priority blockers / risks

### Approval Queue
- approve/reject sensitive AI/workflow changes

### Inbox
- critical/warning/info project messages

## Product direction

This is now the UX layer we should refine with real superintendent feedback.

Next improvements should be usability, not a flood of new backend features:
- mobile-friendly layouts
- faster field entry
- photo capture
- voice input
- subcontractor portal UX
- visual schedule/lookahead board
- activity cards
- cleaner project setup
- branded design system
- real login provider
