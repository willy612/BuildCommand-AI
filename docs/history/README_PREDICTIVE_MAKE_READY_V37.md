# Construction AI — Predictive Make-Ready v3.7

The system now looks ahead instead of waiting for a scheduled activity to become blocked.

## 6-week make-ready scan

For each upcoming activity:
1. calculate READY / HOLD / AT RISK
2. inspect every unresolved gate
3. calculate when the gate should be cleared
4. create a make-ready action
5. assign a responsible party
6. prioritize overdue/blocking items

## Default make-ready lead times

- predecessor completion: 3 days before activity
- inspection / quality: 2 days
- permit / authorization: 10 days
- subcontractor commitment: 14 days
- submittal / procurement: 21 days
- site conditions: 2 days

These are defaults and should become company/project configurable.

## Escalation

When a required-by date is missed, the system prepares an approval-gated follow-up.

Escalation:
- Level 1: 0–2 days overdue
- Level 2: 3–7 days overdue
- Level 3: more than 7 days overdue

Subcontractor-owned actions resolve to the subcontractor contact when available.
Project-team actions remain in-app until role routing is configured.

## Automatic closure

When the underlying gate clears, the associated make-ready action closes.

## Why this matters

The operating model is now:

Schedule
→ future activity
→ readiness gates
→ required clear dates
→ responsible party
→ follow-up
→ escalation
→ resolution
→ READY

That is much closer to how a strong superintendent actually manages the job.

## Next
- downstream schedule exposure attached to each make-ready action
- company-configurable lead times
- automatic role routing to PM/PE/superintendent
- weekly make-ready meeting view
- constraint aging analytics
- subcontractor reliability score
- planned-vs-actual readiness history
- superintendent morning command brief
