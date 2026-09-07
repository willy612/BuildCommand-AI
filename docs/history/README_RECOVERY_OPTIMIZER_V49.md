# Construction AI — Recovery Optimizer v4.9

## What v4.9 adds

### Side-by-side recovery option ranking
The optimizer runs multiple recovery scenarios against the selected activity and ranks them using:
- modeled project days recovered
- modeled activity days recovered
- confidence
- estimated cost
- constructability risk
- milestone-protection signal

### Recovery cost screening
Each scenario receives a transparent placeholder screening cost.
These are NOT bid/contract values and must be replaced by project/company cost logic.

### Safety + constructability guardrails
Every option explicitly requires:
- safety review
- constructability validation
- subcontractor coordination
- cost validation
- authoritative CPM review

### Approved recovery plan
A selected option becomes a tracked recovery plan with:
- approving user
- approval date
- target completion
- predicted days recovered
- predicted cost
- notes
- status

### Predicted vs actual
When the recovery plan is complete, the team records:
- actual completion
- actual days recovered
- actual cost
- whether target was met
- outcome rating
- lessons learned

### Recovery learning
The app aggregates outcomes by intervention type:
- plan count
- predicted days saved
- actual days saved
- actual cost
- strong-outcome rate

## Why this matters
The platform can now begin learning:
"Which recovery interventions actually work for this contractor?"

That is more valuable than a generic recommendation engine because the evidence comes
from the contractor's own projects and outcomes.

## Next
- company-level recovery playbooks
- project-specific cost rates
- milestone protection weighting
- safety/quality impact scoring
- compare options in one visual matrix
- automatic recommendation using historical outcomes
- portfolio intervention learning
- executive recovery-risk dashboard
