# Construction AI — Location + Quality Close Loop v3.4

## Added

### Project location hierarchy
Supports:
- building
- level
- area
- grid
- room
- zone

Field photos can now be tagged to a project location.

### Dedicated punch / quality workflow
Issues carry:
- project
- location
- activity
- responsible subcontractor
- source photo
- priority
- due date
- status
- correction / verification photo

### Close-the-loop model
OPEN
→ subcontractor correction
→ verification photo
→ READY_FOR_VERIFICATION
→ superintendent verifies
→ CLOSED

### Inspection / test records
Track:
- inspection/test type
- authority/inspector
- location
- schedule activity
- planned date
- actual date
- result
- notes
- source reference

## Why this matters
The system can now organize field intelligence by WHERE the work is happening,
WHO owns it, WHAT scheduled work it belongs to, and WHETHER required inspection/
quality loops have actually been closed.

## Next
- expose assigned punch items directly in subcontractor portal
- sub uploads correction photo
- inspection readiness gates schedule activities
- location-based photo timeline
- room/area dashboard
- QR codes for field locations
- turnover/closeout by room/system
