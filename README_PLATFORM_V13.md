# Construction AI — Platform Expansion v1.3

This build continues the production path instead of adding isolated prototype features.

## Added in v1.3

### Hybrid semantic retrieval
- OpenAI embedding support when configured
- embedding persistence in `KnowledgeChunk.embedding_json`
- cosine similarity
- hybrid semantic + keyword ranking
- current/approved document ranking bonus

The embedding model defaults to `text-embedding-3-small` and can be changed with
`OPENAI_EMBEDDING_MODEL`.

### Schedule import
CSV parser designed for exports from:
- Primavera P6
- Microsoft Project
- spreadsheets / other scheduling systems

It recognizes common columns for:
- activity ID
- activity name
- start / finish
- percent complete
- predecessors
- trade
- phase / WBS

### Jurisdiction source registry
Dedicated database/source structure for:
- city / AHJ requirements
- inspection procedures
- adopted code cycle
- permit requirements
- local amendments
- source URL
- effective date
- verification date

### Safety source registry
Dedicated structure for:
- OSHA / State Plan / project safety sources
- standard/reference
- topic
- applicable tasks
- verified source URL/date

### Notification rules
Project risks can now be converted into notification records/messages:
- critical readiness blockers
- procurement risk
- ingestion failures
- project warnings

## Product principle
Jurisdiction, inspection, testing, and safety requirements should be source-backed,
versioned, and date-verified. The AI should not substitute memory for controlling sources.

## Next major engineering steps
1. Persist imported schedules in SQL.
2. Automatic task-to-schedule mapping.
3. PostgreSQL pgvector instead of JSON embeddings.
4. Revision-aware document supersession service.
5. Automatic visual page selection for drawing questions.
6. Source-backed jurisdiction ingestion.
7. Source-backed OSHA/state safety knowledge.
8. Push/in-app notification delivery.
9. Mobile-first superintendent UI.
10. Voice transcription + photo capture in the production shell.
11. Integration adapters for common construction platforms.
12. Replace Streamlit with API + web/mobile clients when product validation justifies it.
