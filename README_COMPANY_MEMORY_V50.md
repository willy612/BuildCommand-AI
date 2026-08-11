# Construction AI — Company Construction Memory v5.0

## The checkpoint
v5.0 changes the platform from project-by-project intelligence into a company learning system.

## Evidence layer
CompanyLearningSignal stores observations from completed work without pretending that
one observation is a universal rule.

Initial evidence sources:
- recovery plan outcomes
- field-vs-CPM alignment observations

Future sources:
- production rates
- crew sizes
- constraint clearance duration
- subcontractor commitments
- procurement lead times
- submittal turnaround
- inspection outcomes
- weather impacts
- safety/quality effects

## Knowledge patterns
Repeated signals are aggregated into CompanyKnowledgePattern records with:
- sample count
- average observed value
- success rate
- confidence
- evidence-backed recommendation

Confidence deliberately grows with sample size and is capped below certainty.

## Company playbooks
When a pattern has enough evidence, the system can generate a reusable company playbook candidate.

A playbook is evidence-backed guidance, not an automatic command.

## Superintendent Command integration
Daily Superintendent Command can now show:
"What your company history says"

This means recommendations can eventually be based on how THIS contractor actually performs,
not only generic construction heuristics.

## Data boundary
Company memory is isolated by company_id.
Cross-company learning must never expose another contractor's identifiable project,
subcontractor, pricing, performance, or operational data.

## Product moat
PROJECT OUTCOME
→ EVIDENCE
→ COMPANY PATTERN
→ PLAYBOOK
→ NEXT PROJECT DECISION
→ NEW OUTCOME
→ BETTER COMPANY PATTERN

## Next checkpoint: v5.1 Predictive Risk Engine
- risk features from schedule + make-ready + production + commitments
- explainable probability bands
- milestone-delay risk
- early-warning trend
- historical company patterns as features
- calibration against actual outcomes
- no black-box risk score without evidence
