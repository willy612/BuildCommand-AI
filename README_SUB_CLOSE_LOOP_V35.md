# Construction AI — Subcontractor Close Loop + Readiness Gates v3.5

## Subcontractor quality workflow

Assigned quality/punch items now appear in the subcontractor portal.

Subcontractor:
1. sees assigned issue
2. sees due date / description
3. uploads correction photo
4. adds correction note
5. submits correction

System:
6. changes issue to READY_FOR_VERIFICATION

Superintendent:
7. reviews correction
8. verifies and closes issue

System:
9. closes associated readiness gate
10. alerts project team that the issue was verified/released

## Readiness gates

Schedule activities can now be blocked by open:
- quality issues
- required inspections/tests

Examples:
- footing concrete cannot be treated as ready while footing rebar inspection is pending/failed
- drywall close-in should not be ready while an assigned rough-in quality issue remains open

A passed inspection closes its gate.
A superintendent-verified quality correction closes its gate.

## Product behavior

This moves the platform from passive tracking toward active field control:

Schedule says what is next.
Readiness gates determine whether it should proceed.
Subcontractors see what they must resolve.
Superintendent verifies closure.
The project team gets the release signal.

## Next
- map gate types to construction task dependencies automatically
- inspection prerequisites generated from task brain
- submittal/procurement gates
- permit/authority gates
- predecessor completion gates
- weather/site-condition gates
- one combined READY / HOLD decision per activity
