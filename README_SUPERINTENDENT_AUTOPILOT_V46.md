# Construction AI — Superintendent Autopilot Foundation v4.6

## Daily Superintendent Command
The app now converts operating signals into a ranked action queue.

Initial inputs:
- make-ready constraints
- overdue clear dates
- escalation levels
- field-vs-CPM drift alerts

Each command action includes:
- severity
- priority score
- title
- why the superintendent is seeing it
- recommended next action
- owner role
- due date

## Explainability
Every recommendation includes a visible "Why am I seeing this?" trail.
This is a permanent product principle: no unexplained black-box field direction.

## Prepared communications
A command action can create a draft communication for review.
External communication remains human-approved.

## Recovery scenario screening
For exposed schedule activities the system can propose:
- crew increase
- targeted overtime / additional shift
- resequencing
- constraint clearance first

Recovery days are screening estimates, not promises.
The superintendent/PM/scheduler must validate safety, constructability, cost,
labor availability, contracts, and authoritative CPM impact.

## Product principle
Complex brain. Simple field experience.

The superintendent should not operate ten modules.
The system should convert those modules into:
WHAT NEEDS ATTENTION
→ WHY
→ WHO OWNS IT
→ WHEN
→ WHAT HAPPENS IF WE WAIT
→ WHAT SHOULD WE DO

## Next
- downstream impact directly on each command action
- owner routing to actual project users
- subcontractor contact routing
- response tracking
- action completion/feedback
- recovery scenario CPM simulation
- command action learning from outcomes
- voice-ready command API
