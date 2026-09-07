# Construction AI — Schedule Versions + Drift Alerts v4.5

## Live lookahead alignment
The existing 6-week lookahead now feeds directly into the field-vs-CPM alignment engine.

## Automatic drift alerts
Latest alignment records generate alerts when field and CPM starts differ.

Default thresholds:
- warning: 3+ days
- critical: 7+ days

These should become company/project configurable.

## Schedule versions
The app can snapshot the current schedule and preserve:
- activity ID/name
- planned start/finish
- percent complete
- total float
- remaining duration

Two versions can be compared to identify:
- added activities
- removed activities
- changed dates
- changed progress
- changed float
- changed remaining duration

## Fuzzy activity matching
When field terminology does not match the P6 ID/name exactly, the app can suggest likely
schedule activities using explainable text similarity.

Matches are saved for review rather than silently accepted.

## Operating loop
Master schedule version
→ superintendent lookahead
→ field/CPM alignment
→ drift alert
→ schedule/field coordination
→ next schedule version
→ trend

## Next
- auto-match unmatched lookahead rows
- approved match reuse
- drift history by activity
- baseline/version trend charts
- alert routing to superintendent/PM
- company thresholds
- schedule narrative generator
- recovery-plan recommendations
