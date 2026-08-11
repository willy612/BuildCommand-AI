# Construction AI — Operations Intelligence v3.8

## Added

### Daily Superintendent Command Brief
Every morning, the system can summarize:
- 6-week READY / HOLD / AT RISK
- overdue make-ready actions
- near-term HOLD activities
- near-term AT RISK activities

### Weekly Make-Ready Meeting
One review list for:
- activity
- unresolved gate
- action
- reason
- required-by date
- priority
- escalation level

### Constraint Aging
Shows how long make-ready actions have been overdue.

### Configurable Lead Times
Company-level lead times can now override defaults for:
- predecessors
- inspection / quality
- permits
- subcontractor commitments
- submittals / procurement
- site conditions

### Role Routing Foundation
Gate types now have preferred internal roles:
- permits → PM / PE
- procurement → PE / PM
- inspections / quality → superintendent / PE
- site conditions → superintendent
- predecessor work → superintendent / PM

### Subcontractor Reliability
A transparent project-management signal based on:
- commitments
- confirmations
- delay reports
- unanswered commitments
- open quality items

It must not be treated as a blame metric. Root cause matters.

## Next
- attach downstream schedule exposure to every make-ready action
- predicted date impact
- milestone risk
- planned-vs-actual constraint clearance
- crew/manpower history
- daily production quantities
- superintendent productivity trends
- executive portfolio forecasting
