# Construction AI — XER Ingestion + Field/CPM Alignment v4.4

## True XER persistence foundation

The P6 XER reader can now map core sections into the application:

- TASK → schedule activities
- TASKPRED → predecessor/successor relationships
- PROJWBS → WBS nodes
- CALENDAR → project calendar records

It also maps:
- actual start / finish
- baseline/target dates
- remaining duration
- total float
- relationship type
- relationship lag

This is intentionally a core-field mapper rather than pretending every P6 field
has already been implemented.

## Field vs CPM alignment engine

A new alignment model compares superintendent lookahead start dates to the master
CPM activity start.

Initial statuses:
- ALIGNED
- FIELD_EARLIER
- FIELD_LATER

A variance greater than two days is surfaced for review.

This creates an important operating loop:

Master CPM schedule
→ superintendent lookahead
→ actual field plan
→ alignment variance
→ coordination discussion
→ schedule / field correction

## Next
- wire the existing lookahead records directly into alignment
- match unmatched field activities intelligently
- calendar exception mapping from XER
- WBS hierarchy browser
- activity code mapping
- baseline update comparisons
- schedule revision/version identity
- field-vs-CPM drift alerts
