# Construction AI — Schedule Quality + P6 Structure v4.3

## Added

### Project calendar foundation
- project calendars
- workweek JSON
- hours/day
- holiday table
- workday helpers

### WBS + activity code foundation
- WBS nodes
- activity code assignments

### XER section reader
The app can inspect raw Primavera P6 XER sections.

This is the foundation for mapping:
- TASK
- TASKPRED
- PROJWBS
- CALENDAR
- ACTVCODE
- ACTVTYPE

### Schedule quality checks
Explainable checks include:
- open ends
- long remaining durations
- unusually high float
- negative float
- high lag
- relationship density

The quality score is a management signal, not a formal certification.

### Longest-path candidate
A graph-based longest dependency chain is surfaced for review.

## Important note
The current quality checks are inspired by common scheduling review practices,
but they are not presented as official DCMA 14-point compliance.

A future compliance module should implement the exact published definitions,
thresholds, calendars, and data rules for the standard being claimed.

## Next
- map XER TASK/TASKPRED into persistent activities/relationships
- map PROJWBS
- map calendars/holidays
- activity codes
- richer baseline history
- exact schedule-quality standard modules
- compare superintendent lookahead vs CPM logic
