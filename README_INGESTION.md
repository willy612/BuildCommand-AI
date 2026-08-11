# Construction AI — Persistent Ingestion v1.2

v1.2 makes uploaded project PDFs durable project knowledge.

## Flow
Upload → project storage → ingestion job → PDF parse → page records → knowledge chunks →
SQL persistence → project search → readiness/graph reuse after restart.

## Added
- DocumentPage database model
- KnowledgeChunk database model
- IngestionJob database model
- persistent ingestion pipeline
- SQL-backed project knowledge search
- persistent production upload interface
- ingestion job history
- project-state loading of document/page/chunk records

## Current search
Keyword scoring over persisted chunks.

## Next
v1.3:
- embeddings
- pgvector
- hybrid semantic + keyword retrieval
- revision-aware ranking
- automatic relevant-page selection for visual analysis
- source-grounded AI answers from persistent project knowledge

## Run
    pip install -r requirements.txt
    python production_bootstrap.py
    streamlit run app_production.py
