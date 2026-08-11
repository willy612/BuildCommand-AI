# Construction AI — Integrated v1.0 Prototype

This build keeps advancing the platform instead of stopping at one feature.

## Added beyond v0.13

### v0.14 — Automatic Dependency Generation
The app can now generate expected task dependencies from construction-task templates:
- typical required documents
- project prerequisites
- expected permit / inspection / testing / safety gates
- suggested submittal / procurement requirements

### v0.15 — Field Photo Intelligence
The code now supports image-based field analysis with a vision-capable OpenAI model.
It is designed to identify visible progress, possible quality/safety/coordination issues,
and proposed follow-up actions while preserving human approval before project-state changes.

### v0.16 — Closeout & Warranty Brain
Added closeout and warranty logic:
- as-builts / record drawings
- O&M manuals
- warranties
- training
- testing / inspection reports
- commissioning / TAB
- punch
- occupancy documentation
- warranty tracking

### v0.17 — End-of-Project Construction Workflow
Added master tasks for:
- finish installation
- MEP trim-out
- equipment startup
- TAB / functional testing
- commissioning
- final inspections
- pre-punch / punch
- occupancy readiness
- owner turnover
- warranty period

## Current product architecture

1. Master Construction Brain
2. Permit / Jurisdiction Brain
3. Inspection & Testing Brain
4. Job Safety Brain
5. Blueprint / Document Brain
6. Project State Brain
7. Schedule & Planning Brain
8. Submittal & Procurement Brain
9. Field Intelligence
10. Construction Readiness Engine
11. Construction Readiness Graph
12. Automatic Dependency Generator
13. Photo Intelligence
14. Closeout & Warranty Brain
15. Daily Superintendent Command Center

## Core product loop

Project documents + jurisdiction + schedule + submittals + field state
→ automatic dependency generation
→ Construction Readiness Graph
→ READY / HOLD / AT RISK decisions
→ Daily Superintendent Command Center
→ field voice/photo updates
→ human approval
→ project state updates
→ risk/readiness recalculation

## Next steps toward a real product

The prototype now needs engineering hardening more than more isolated features:

1. Persistent database (PostgreSQL)
2. Authentication / company + project accounts
3. Cloud file storage
4. Real document ingestion pipeline
5. Embeddings / semantic retrieval
6. Better plan-sheet recognition and visual linking
7. Jurisdiction data service
8. OSHA / safety source library
9. Real schedule import (P6 / MS Project / CSV)
10. Submittal/RFI import and integrations
11. Mobile-first field interface
12. Voice transcription
13. Photo storage + area/task tagging
14. Notifications / assignments
15. Audit trail and permissions
16. Testing / security / backups
17. Stripe billing and SaaS administration

## Run

1. `pip install -r requirements.txt`
2. Set `OPENAI_API_KEY`
3. `streamlit run app.py`

This remains a prototype and decision-support system. Approved project documents,
responsible professionals, contractors, inspectors, AHJs, safety requirements, and actual
field conditions remain controlling.
