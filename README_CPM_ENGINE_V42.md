# Construction AI — CPM Engine v4.2

## Added

### Relationship semantics
The CPM calculation now interprets:
- Finish-to-Start (FS)
- Start-to-Start (SS)
- Finish-to-Finish (FF)
- Start-to-Finish (SF)
- relationship lag

### Data date
Uses the latest imported schedule data date as the calculation anchor.

### Remaining duration
Uses imported remaining duration where available.

### Work calendar
v4.2 includes a default Monday-Friday working-day calendar.

Named activity calendars are stored but project-specific holidays and multiple
calendar definitions are not yet modeled.

### Forward pass
Calculates:
- Early Start
- Early Finish

### Backward pass
Calculates:
- Late Start
- Late Finish
- Total Float
- critical flag

### Constraints
Initial support for start-no-earlier-than style constraints:
- START ON OR AFTER
- SNET
- START NO EARLIER THAN

### Schedule update history
Every CPM calculation stores:
- data date
- planned completion
- calculated completion
- critical activity count
- negative float count

This allows schedule health to be trended across updates.

## Important
This is now substantially closer to CPM logic, but it still should not be treated
as a replacement for Primavera P6's scheduling engine.

Remaining production-grade items include:
- multiple calendars and holidays
- resource calendars
- all constraint semantics
- progress override / retained logic
- out-of-sequence progress
- longest-path calculation
- free float recomputation
- WBS / activity codes
- XER-native import

## Next
- project calendars + holidays
- longest path
- baseline trend charts
- XER parsing
- P6 activity codes / WBS
- schedule quality checks
- DCMA-style schedule health metrics
