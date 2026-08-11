import tempfile
from pathlib import Path
import streamlit as st

from db.session import init_db, SessionLocal
from services.project_service import list_projects, create_project
from services.state_service import load_project_state
from services.document_service import store_document, list_documents
from ingestion.pipeline import create_ingestion_job, ingest_document, list_jobs
from search.project_search import search_project_chunks, format_search_results
from search.semantic_search import hybrid_search, backfill_embeddings
from imports.schedule_import import parse_schedule_csv
from brain.master_brain import load_tasks
from brain.command_center import command_summary, priority_actions
from brain.readiness_graph import build_readiness_graph, graph_metrics

st.set_page_config(page_title="Construction AI", page_icon="🏗️", layout="wide")
init_db()
db = SessionLocal()

st.title("🏗️ Construction AI")
st.subheader("Persistent Ingestion v1.2")

projects = list_projects(db)

with st.sidebar:
    st.header("Projects")
    if projects:
        labels={f"{p.project_number} — {p.name}":p.id for p in projects}
        selected=st.selectbox("Active project",list(labels.keys()))
        project_id=labels[selected]
    else:
        project_id=None
        st.info("No projects yet.")

    st.divider()
    st.markdown("### New project")
    name=st.text_input("Name")
    number=st.text_input("Project number")
    jurisdiction=st.text_input("Jurisdiction")
    if st.button("Create project"):
        if name.strip():
            p=create_project(db,name=name.strip(),project_number=number.strip(),jurisdiction=jurisdiction.strip())
            st.success(f"Created project #{p.id}")
            st.rerun()

if project_id:
    tabs=st.tabs(["Command Center","Documents","Project Search","Schedule Import","Sources","Ingestion Jobs","System"])

    with tabs[0]:
        state=load_project_state(db,project_id)
        tasks=load_tasks()
        graph=build_readiness_graph(state,tasks)
        metrics=graph_metrics(graph)
        summary=command_summary(state,tasks)
        actions=priority_actions(state,tasks)

        c1,c2,c3,c4=st.columns(4)
        c1.metric("READY",summary.get("READY",0))
        c2.metric("HOLD",summary.get("HOLD",0))
        c3.metric("AT RISK",summary.get("AT_RISK",0))
        c4.metric("Graph nodes",metrics.get("nodes",0))

        st.markdown("### Priority Actions")
        if not actions: st.success("No critical actions recorded.")
        for a in actions[:12]:
            st.write(f"**P{a['priority']} — {a['category']} — {a['title']}**")
            st.caption(a["reason"])

    with tabs[1]:
        st.markdown("### Persistent Project Documents")
        uploaded=st.file_uploader("Upload PDF",type=["pdf"],key="prod_doc_upload")
        dtype=st.selectbox("Document type",[
            "civil","architectural","structural","plumbing","mechanical","electrical",
            "fire_protection","specifications","geotechnical","permit","swppp",
            "special_inspection","addendum","rfi","submittal","shop_drawing","schedule","other"
        ])
        discipline=st.text_input("Discipline",value=dtype if dtype not in ("other","specifications") else "")
        revision=st.text_input("Revision")

        if st.button("Store and ingest document",type="primary"):
            if not uploaded:
                st.warning("Choose a PDF.")
            else:
                with tempfile.NamedTemporaryFile(delete=False,suffix=".pdf") as tmp:
                    tmp.write(uploaded.getbuffer())
                    temp_path=Path(tmp.name)
                doc=store_document(
                    db,project_id,temp_path,uploaded.name,
                    document_type=dtype,discipline=discipline,revision=revision,status="CURRENT"
                )
                temp_path.unlink(missing_ok=True)
                job=create_ingestion_job(db,project_id,doc.id)
                try:
                    ingest_document(db,job.id)
                    st.success(f"Stored and ingested {doc.filename}.")
                except Exception as e:
                    st.error(f"Stored document, but ingestion failed: {e}")
                st.rerun()

        for d in list_documents(db,project_id):
            st.write(f"**{d.filename}** — {d.document_type} — {d.status}")
            st.caption(f"Revision: {d.revision or '—'} | SHA-256: {d.source_hash[:12]}…")

    with tabs[2]:
        st.markdown("### Search Project Knowledge")
        st.caption("Hybrid retrieval can combine keyword matching with embeddings when OPENAI_API_KEY is configured.")
        q=st.text_input("Search plans/specs",placeholder="footing reinforcement, roof drain, vapor barrier...")
        mode=st.radio("Search mode",["Keyword","Hybrid semantic"],horizontal=True)
        if st.button("Build missing embeddings"):
            count=backfill_embeddings(db,project_id,limit=200)
            st.success(f"Embedded {count} knowledge chunk(s).")
        if q.strip():
            rows=search_project_chunks(db,project_id,q,top_k=10) if mode=="Keyword" else hybrid_search(db,project_id,q,top_k=10)
            if not rows: st.info("No indexed project content matched.")
            for r in format_search_results(rows):
                st.markdown(f"**{r['source_ref']}**")
                st.write(r["content"][:1200])
                st.divider()


with tabs[3]:
    st.markdown("### Schedule Import")
    st.caption("Import a CSV exported from P6, MS Project, or another scheduling tool.")
    sched = st.file_uploader("Schedule CSV", type=["csv"], key="schedule_csv")
    if sched:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False,suffix=".csv") as tmp:
            tmp.write(sched.getbuffer())
            path=Path(tmp.name)
        rows=parse_schedule_csv(path)
        path.unlink(missing_ok=True)
        st.write(f"Parsed activities: **{len(rows)}**")
        for a in rows[:20]:
            st.write(f"**{a['activity_id']} — {a['name']}**")
            st.caption(f"{a['planned_start']} → {a['planned_finish']} | Pred: {', '.join(a['predecessors']) or '—'}")
        st.info("v1.3 parses schedule data for review. The next persistence step will store schedule activities in SQL and map them automatically to construction tasks.")

with tabs[4]:
    st.markdown("### Source Registries")
    st.write("Jurisdiction and safety requirements now have dedicated source-registry models.")
    st.write("The goal is to keep every city/AHJ or safety requirement tied to a source, code cycle, effective date, and verification date rather than relying on AI memory.")
    st.info("No jurisdiction requirement is considered verified until an official source has been entered and reviewed.")

    with tabs[5]:
        st.markdown("### Ingestion Jobs")
        jobs=list_jobs(db,project_id)
        if not jobs: st.info("No ingestion jobs yet.")
        for j in jobs:
            st.write(f"**Job #{j.id}** — {j.status}")
            st.caption(f"Stage: {j.stage} | Progress: {j.progress}% | Document ID: {j.document_id}")
            if j.error_message: st.error(j.error_message)

    with tabs[6]:
        st.markdown("### v1.2 ingestion foundation")
        st.write("✓ Persistent document storage")
        st.write("✓ Persistent ingestion job records")
        st.write("✓ PDF parsing into SQL page/chunk records")
        st.write("✓ Project-wide persistent search")
        st.write("✓ Persistent project-state adapter for documents/pages/chunks")
        st.write("✓ Readiness graph can consume persistent document records")

db.close()
