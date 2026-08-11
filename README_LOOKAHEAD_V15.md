# Construction AI — Lookahead + Subcontractor Intelligence v1.5

## What v1.5 adds

### Subcontractor Directory
- company
- trade
- project scope
- PM/foreman/other contacts
- email/phone
- primary project contact

### Schedule assignment
Subcontractors can be assigned directly to imported/persistent schedule activities.

### 6-week lookahead commitments
For each scheduled activity:
- assigned subcontractor
- requested start/finish
- response due date
- commitment status
- manpower commitment
- material status
- equipment status
- delay reason
- superintendent review status

### Automatic lookahead communication drafts
The system can create a ready-to-review email draft asking the subcontractor to confirm:
- schedule dates
- manpower
- materials
- equipment
- submittals/procurement
- RFIs
- access
- predecessor work
- inspections/testing
- any expected delay

Actual external sending remains intentionally gated behind project-team approval.

### Subcontractor response intelligence
Responses can be classified as:
- CONFIRMED
- DELAY_REPORTED
- NEED_INFORMATION
- MATERIAL_ISSUE
- MANPOWER_ISSUE
- ACCESS_ISSUE
- OTHER

Responses remain PENDING_REVIEW until the superintendent approves resulting project-state changes.

## Core principle

Do not merely report that a subcontractor is late.
The system should identify whether the root cause is:
- subcontractor commitment/manpower;
- procurement/material;
- design/RFI;
- owner/GC decision;
- predecessor work;
- inspection/testing;
- access/logistics;
- another upstream project constraint.

This distinction should feed the Construction Readiness Graph and schedule-risk model.

## Run the v1.5 workflow

    pip install -r requirements.txt
    python production_bootstrap.py
    streamlit run lookahead_app.py

Use `app_production.py` for the core project/document interface.
