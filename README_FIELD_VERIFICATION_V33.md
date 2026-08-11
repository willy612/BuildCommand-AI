# Construction AI — Field Verification v3.3

This release connects field photos to the project brain.

## Workflow

Field photo
→ superintendent observation
→ candidate source-backed requirements
→ human field/document comparison
→ approved action

Approved actions can create:
- progress record
- punch / quality item
- constraint proposal
- no action

## Source hierarchy

The engine searches existing project knowledge chunks for candidate requirements.
The review shows:
- field observation
- requirement text
- source document
- source page
- source reference
- confidence

## Safety / accuracy rule

The AI does NOT declare that installed work complies with plans, specifications,
code, approved submittals, structural requirements, or inspection requirements.

It proposes candidate comparisons. A qualified project user verifies the condition.

## Why this matters

The system is beginning to connect:

WHAT SHOULD BE BUILT
(plans/specs/submittals/RFIs)

with

WHAT IS HAPPENING
(field photos/observations/progress)

and then

WHAT SHOULD WE DO
(progress / punch / constraint / follow-up).

## Next
- location tagging (building/level/area/grid/room)
- photo timelines by activity/location
- approved submittal/shop-drawing comparison
- inspection/test linkage
- dedicated punch workflow
- responsible subcontractor assignment
- close-the-loop verification photos
