# Construction AI — Early-Warning Forecasting v3.9

## Production tracking

Field teams can record by schedule activity:
- work date
- subcontractor
- crew size
- regular hours
- overtime hours
- installed quantity
- unit
- planned quantity
- notes

The engine calculates:
- reported installed quantity
- reported planned quantity
- labor hours
- observed productivity
- production pace ratio

## Production drift

A simple explainable first-pass signal:
- NORMAL
- WATCH
- HIGH

This is deliberately not a black-box schedule prediction.
It surfaces measurable drift for superintendent review.

## Downstream exposure

Every open make-ready action can now trace successor relationships through the schedule.

The engine records:
- source activity
- exposed downstream activity
- relationship depth
- modeled risk days

This lets the team see that a constraint on today's activity can affect work several steps later.

## Milestone exposure

Downstream relationships that reach milestones are surfaced separately.

## Constraint clearance history

When a make-ready action closes, the system records:
- planned clear date
- actual clear date
- variance days
- gate type

This creates the history needed to learn how early different constraints really need to be managed.

## Important limitation

Modeled risk days are an early-warning heuristic, not a CPM schedule calculation.
A production deployment should integrate float, calendars, relationship types,
lags, constraints, and schedule-update logic from the authoritative schedule system.

## Next
- CPM-aware schedule impact
- remaining-duration forecasting
- production trend curves
- crew/manpower trend by trade
- milestone confidence
- forecast completion date
- portfolio-level forecast
- superintendent trend alerts
