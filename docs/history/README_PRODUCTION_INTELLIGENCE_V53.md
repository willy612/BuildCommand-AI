# Construction AI — Production Intelligence v5.3

## Goal
Detect schedule risk from actual field production before the next formal schedule update.

## Activity production intelligence
For production records tied to schedule activities, the engine calculates:
- quantity per observation day
- quantity per labor-hour
- average crew
- planned quantity per day
- actual/planned pace ratio
- projected remaining duration from observed rate
- recent production trend: improving, stable, fading, or insufficient data

## Predictive Risk integration
Production pace is now a real evidence bucket in the v5.1 risk engine.

An activity running below recorded planned pace contributes risk points and an explanation.

## Company production benchmarks
Across the company's projects, comparable production snapshots can create benchmarks by:
- trade
- activity type
- quantity unit

Benchmarks store:
- sample count
- average quantity/day
- average quantity/labor-hour
- average crew
- confidence

## Company memory
Production observations can be captured as CompanyLearningSignal records.

## Important limitations
Production comparisons are only meaningful when:
- quantities are measured consistently
- units match
- activity scope is comparable
- labor hours are recorded consistently
- unusual conditions are understood

The system should never tell a superintendent that a crew is "bad" solely because a raw rate
is below a benchmark.

## Next checkpoint: v5.4 Company Playbooks
- convert repeated evidence into operational playbooks
- trigger conditions
- recommended actions
- required lead times
- escalation paths
- company-specific checklists
- superintendent feedback on whether a playbook helped
