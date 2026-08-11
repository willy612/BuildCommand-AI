# Construction AI — Recovery Sandbox v4.8

## What changed
Recovery ideas can now be applied to a cloned in-memory schedule network and the
network is forward-scheduled again before the scenario is stored.

Scenarios:
- add crew
- overtime
- work Saturday
- clear a constraint
- resequence / overlap

The result shows:
- original project completion
- simulated project completion
- modeled project days recovered
- selected activity days recovered
- downstream activities whose dates changed
- original vs simulated dates
- screening confidence
- explicit assumptions

## Why this matters
Accelerating a non-driving activity may recover zero project days.
The sandbox makes that visible before a superintendent commits money or labor.

## Guardrails
This is decision support, not an authoritative schedule update.

Crew and overtime assumptions deliberately include diminishing productivity.
Construction acceleration can reduce labor efficiency, so adding hours or people
should never be modeled as perfectly linear.

Before field execution, validate:
- safety
- constructability
- available workspace
- manpower availability
- material/equipment availability
- subcontract terms
- cost
- quality/rework exposure
- authoritative scheduler/P6 result

## Next
- project-specific work calendars and holidays in the sandbox
- true logic-edge resequencing rather than duration approximation
- scenario cost inputs
- safety/constructability flags
- milestone-specific recovery benefit
- compare several scenarios side by side
- save approved recovery plan
- measure actual outcome against the selected simulation
