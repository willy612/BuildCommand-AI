# Construction AI — Schedule Intelligence + Portfolio Forecasting v4.1

## Rich CPM import
Adds support for fields commonly exported from P6/MS Project:
- baseline start / finish
- data date
- total float
- free float
- actual start / finish
- remaining duration
- calendar name
- constraint type / date
- relationship types
- lag

Relationship types supported in storage:
- FS
- SS
- FF
- SF

## Schedule health
Surfaces:
- critical activities (TF <= 0)
- near-critical activities
- baseline finish variance
- data date

## Portfolio forecasting
Company leadership can refresh a portfolio forecast that rolls up:
- latest project completion forecast
- forecast variance
- forecast confidence
- HOLD count
- AT RISK count
- intervention score

The intervention score is explainable and is intended to prioritize attention,
not replace management judgment.

## Next
- apply relationship-type semantics in forecast math
- business calendars / non-work days
- schedule constraints
- true forward/backward pass
- total/free float recomputation
- baseline comparison charts
- portfolio trend history
- intervention recommendations by project
