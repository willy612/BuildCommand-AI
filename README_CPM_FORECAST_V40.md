# Construction AI — CPM-Aware Forecasting v4.0

## Remaining duration forecasting
Uses:
- current percent complete
- planned activity duration
- observed production pace
- field production history

to estimate remaining duration.

## Relationship-aware forecast
The forecast walks schedule predecessor/successor relationships and moves successor
forecast starts when predecessor forecast finishes move.

## Project completion forecast
Produces:
- planned completion
- forecast completion
- forecast variance days
- confidence
- critical-candidate activity count

## Activity forecast snapshots
Stores history for:
- planned finish
- forecast finish
- remaining duration
- variance
- confidence
- critical-candidate flag

## Manpower trends
Crew counts are aggregated by trade and work date.

## Production trends
Shows:
- installed quantity
- planned quantity
- labor hours
- productivity
- pace ratio

## Important scheduling limitation
This is now relationship-aware, but it is not yet a full commercial CPM engine.

A production-grade CPM integration still needs:
- schedule calendars
- FS / SS / FF / SF relationship semantics
- relationship lag
- total/free float
- constraints
- out-of-sequence progress rules
- retained logic / progress override behavior
- authoritative data-date handling
- baseline comparison

The app should eventually ingest these directly from Primavera P6 / Microsoft Project
or another authoritative schedule source rather than recreating every scheduling rule.

## Next
- P6/XER or richer schedule import
- relationship type + lag
- float calculations
- data-date logic
- baseline variance
- executive forecast across projects
- confidence trend over time
