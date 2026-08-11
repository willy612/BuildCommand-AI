# Construction AI — Field Vision v3.2

## Added
- FieldVisionProposal domain object
- field-photo-to-review workflow
- categories for progress, quality, safety, coordination, punch, delivery and housekeeping
- suggested action
- optional proposed percent complete
- confidence
- approve/reject workflow
- conservative vision rules

## Critical product rule
A photo never silently changes official project status.

Image analysis creates proposals. A superintendent or authorized project user reviews them.

The vision layer must not claim:
- concealed conditions
- code compliance
- structural adequacy
- inspection approval
- installation quality beyond visible evidence

## Provider integration
The core is provider-neutral. `FIELD_VISION_PROVIDER` is the future adapter switch.
Until a multimodal provider is configured, the workflow creates a reviewable proposal
from the superintendent's photo observation/caption instead of pretending image analysis occurred.

## Next
- real multimodal provider adapter
- photo-to-punch item
- photo-to-constraint
- photo-to-progress proposal
- photo timeline by activity/location
- compare field photo against drawing/detail context
