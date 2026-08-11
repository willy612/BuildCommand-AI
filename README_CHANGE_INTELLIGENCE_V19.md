# Construction AI — Change Intelligence v1.9

## Goal
Do not stop at "this sheet changed."

Ask:
- What changed?
- Which construction activities may be affected?
- Which scheduled activities may be affected?
- Which subcontractors are assigned to that work?
- Could this require an RFI?
- Could this create cost exposure?
- Could this create schedule exposure?

## New
- extracted-text revision comparison
- added/removed sheet detection
- changed sheet identification
- changed-term extraction
- task relevance suggestions
- schedule relevance suggestions
- subcontractor impact lookup through schedule assignments
- reviewable Change Events
- Change Impact Items
- potential RFI/cost/schedule flags
- human approval before change events become tracked project items

## Important limitations
Text comparison alone does not reliably capture all graphical drawing changes.
Future versions should combine:
- visual page comparison
- revision clouds
- detail/callout changes
- dimension changes
- note changes
- schedule and submittal context

## Next
v2.0 Production Integration:
- visual revision comparison
- RFI/change-event workflow
- notifications to affected PM/superintendent/subs
- schedule exposure calculation
- cost/change-order integration
- one production API/backend
- mobile/web clients
