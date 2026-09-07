# Construction AI — Predictive Risk Engine v5.1

## Goal
Answer:
"What is most likely to hurt this job before the superintendent can see it coming?"

## Explainable activity risk
Every schedule activity receives a transparent risk score built from evidence buckets:
- field-vs-CPM drift
- open make-ready constraints
- subcontractor commitment hook
- production/progress hook
- downstream schedule exposure
- company-history context

The score is deliberately explainable. Each point source is visible.

## Risk bands
- LOW
- WATCH
- HIGH
- CRITICAL

These are screening bands, not statistical promises.

## Daily Superintendent Command
The command screen now includes:
"What may hurt the job next"

This surfaces the highest emerging activity risks before the normal action queue.

## Calibration
Risk predictions can be paired with actual outcomes:
- did a delay occur?
- how many days?
- notes

This creates the foundation for measuring whether the model is useful instead of
assuming a risk score is accurate.

## Important limitation
v5.1 is an explainable rules/evidence engine.
It does NOT claim that a score of 75 means a literal 75% chance of delay.

Probability calibration should only be introduced after enough verified historical
outcomes exist.

## Next checkpoint: v5.2 Subcontractor Intelligence
- commitment reliability
- response speed
- manpower reliability
- start-date reliability
- production performance
- delay-warning behavior
- trade/company context
- project-specific context
- privacy-safe company memory
