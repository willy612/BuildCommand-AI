# v6.0 Release / Validation Checklist

## Functional
- [ ] Create company and multiple projects
- [ ] Assign different superintendents/project users
- [ ] Import representative schedule
- [ ] Build lookahead and make-ready actions
- [ ] Create subcontractor directory and commitments
- [ ] Record production
- [ ] Run predictive risk
- [ ] Run recovery sandbox
- [ ] Run recovery optimizer
- [ ] Approve and close recovery plan
- [ ] Capture company memory
- [ ] Generate company playbook
- [ ] Execute playbook and record feedback
- [ ] Generate field brief
- [ ] Refresh executive portfolio intelligence

## Data isolation
- [ ] User from Company A cannot read Company B projects
- [ ] Company A memory cannot include Company B signals
- [ ] Subcontractor analytics do not cross tenant boundary
- [ ] Audit events are tenant-scoped

## Safety / decision support
- [ ] No recovery recommendation auto-updates authoritative schedule
- [ ] No subcontractor context auto-awards/rejects a vendor
- [ ] Safety and constructability reviews remain explicit
- [ ] Predictive scores are not presented as calibrated probabilities
- [ ] Human approval is required before external communications/actions

## Production readiness
See README_PLATFORM_V60.md.
