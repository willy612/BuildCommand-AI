import tempfile
from pathlib import Path
import streamlit as st

from ai.assistant import ask_construction_ai
from brain.master_brain import load_tasks
from brain.project_state import new_project_state, set_permit, add_test, add_inspection, mark_complete
from brain.schedule_brain import evaluate_activity, critical_actions
from brain.document_intake import intake_metadata, find_revision_conflicts, mark_current
from brain.project_setup import startup_readiness
from brain.plan_reader import parse_pdf
from brain.visual_plan_reader import render_pdf_page, analyze_page_image, build_visual_record, visual_record_to_chunks
from brain.knowledge_graph import build_graph
from brain.readiness_engine import evaluate_readiness, format_readiness
from brain.submittal_procurement import calculate_required_dates, assess_item, portfolio_risks
from brain.command_center import command_summary, priority_actions, activity_health, inspection_board, testing_board, procurement_board, rfi_board, safety_board, swppp_board
from brain.field_intelligence import parse_field_update, new_pending_update, approve_pending_update, reject_pending_update, daily_report
from brain.readiness_graph import build_readiness_graph, evaluate_task_from_graph, downstream_impact, graph_metrics

st.set_page_config(page_title="Construction AI", page_icon="🏗️", layout="wide")

if "project_state" not in st.session_state:
    st.session_state.project_state = new_project_state()
state = st.session_state.project_state

st.title("🏗️ Construction AI")
st.subheader("Project-Aware Construction Intelligence — Visual Plan Brain v0.8")

tabs = st.tabs([
    "Project Setup","Document Intake","Text Plan Reading","Visual Plan Analysis",
    "Command Center","Readiness Graph","Field Intelligence","Ask AI","Readiness Engine","Submittals & Procurement","Task Gate","Schedule & Planning","Knowledge Graph","Master Sequence"
])


with tabs[0]:
    st.markdown("### Daily Superintendent Command Center")
    st.caption("One screen for what is ready, blocked, at risk, due, and worth your attention first.")

    tasks = load_tasks()
    summary = command_summary(state, tasks)

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("READY activities", summary.get("READY",0))
    c2.metric("HOLD activities", summary.get("HOLD",0))
    c3.metric("AT RISK activities", summary.get("AT_RISK",0))
    c4.metric("Critical procurement", summary.get("PROCUREMENT_CRITICAL",0))

    c5,c6,c7,c8 = st.columns(4)
    c5.metric("Open RFIs", summary.get("OPEN_RFIS",0))
    c6.metric("Upcoming inspections", summary.get("INSPECTIONS",0))
    c7.metric("Upcoming tests", summary.get("TESTS",0))
    sw = swppp_board(state)
    c8.metric("SWPPP", sw.get("status","NOT_CHECKED"))

    st.markdown("### What should I handle first?")
    actions = priority_actions(state, tasks)
    if not actions:
        st.success("No immediate project blockers are recorded.")
    else:
        for i,a in enumerate(actions[:12],1):
            label = "🔴" if a["priority"] == 1 else ("🟠" if a["priority"] == 2 else "🟡")
            st.markdown(f"{label} **{i}. {a['category']} — {a['title']}**")
            st.write(a["reason"])

    st.markdown("### Activity Readiness")
    for row in activity_health(state, tasks):
        if row["status"] in ("HOLD","AT RISK","READY","IN_PROGRESS"):
            st.write(f"**{row['name']}** — {row['status']}")
            for reason in row["reasons"][:3]:
                st.caption(f"• {reason}")

    left,right = st.columns(2)

    with left:
        st.markdown("### Inspections")
        inspections = inspection_board(state)
        if not inspections:
            st.info("No upcoming/undated inspection records.")
        for r in inspections[:10]:
            st.write(f"**{r.get('inspection_type','Inspection')}** — {r.get('result','PENDING')}")
            st.caption(f"Task: {r.get('task_id','')} | Date: {r.get('scheduled_date','Not set')}")

        st.markdown("### Testing")
        tests = testing_board(state)
        if not tests:
            st.info("No upcoming/undated test records.")
        for r in tests[:10]:
            st.write(f"**{r.get('test_type','Test')}** — {r.get('result','PENDING')}")
            st.caption(f"Task: {r.get('task_id','')} | Date: {r.get('scheduled_date') or r.get('date','Not set')}")

        st.markdown("### Safety")
        safety = safety_board(state, tasks)
        if not safety:
            st.info("No task safety-readiness records.")
        for s in safety[:10]:
            st.write(f"**{s['name']}** — {s['status']}")

    with right:
        st.markdown("### Procurement")
        proc = procurement_board(state)
        if not proc:
            st.info("No procurement items.")
        for p in proc[:10]:
            st.write(f"**{p['name']}** — {p['risk']}")
            st.caption(p["reason"])

        st.markdown("### Open RFIs / Constraints")
        rfis = rfi_board(state)
        if not rfis:
            st.info("No open RFIs are recorded.")
        for r in rfis[:10]:
            st.write(f"**{r.get('subject') or r.get('id','RFI')}**")
            st.caption("BLOCKING" if r.get("blocking") else "Open")



with tabs[1]:
    st.markdown("### Construction Readiness Graph")
    st.caption("The central nervous system: every construction activity is connected to the evidence, approvals, inspections, tests, safety, materials, constraints, RFIs, and schedule signals that affect release.")

    tasks = load_tasks()
    state["readiness_graph"] = build_readiness_graph(state, tasks)
    graph = state["readiness_graph"]
    metrics = graph_metrics(graph)

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Graph nodes", metrics["nodes"])
    c2.metric("Connections", metrics["edges"])
    c3.metric("HOLD nodes", metrics["holds"])
    c4.metric("AT RISK nodes", metrics["at_risk"])
    c5.metric("Not verified", metrics["not_verified"])

    labels = {f"{t['phase']} → {t['name']}": t for t in tasks}
    selected_label = st.selectbox("Evaluate construction activity", list(labels.keys()), key="graph_task")
    selected_task = labels[selected_label]
    result = evaluate_task_from_graph(graph, selected_task["id"])

    if result["status"] == "READY":
        st.success(f"{selected_task['name']} — READY")
    elif result["status"] == "HOLD":
        st.error(f"{selected_task['name']} — HOLD")
    elif result["status"] == "AT RISK":
        st.warning(f"{selected_task['name']} — AT RISK")
    else:
        st.info(f"{selected_task['name']} — NOT VERIFIED")

    st.write(f"Connected dependencies: **{result['dependency_count']}**")

    left,right = st.columns(2)
    with left:
        st.markdown("#### What is stopping us?")
        if not result["blockers"]:
            st.write("No blocking dependency is currently identified.")
        for b in result["blockers"]:
            st.write(f"**{b['label']}** — {b['status']}")
            if b.get("reason"): st.caption(b["reason"])

    with right:
        st.markdown("#### What is likely to stop us next?")
        if not result["risks"]:
            st.write("No near-term graph risk is currently identified.")
        for r in result["risks"]:
            st.write(f"**{r['label']}** — {r['status']}")
            if r.get("reason"): st.caption(r["reason"])

    st.markdown("#### Verified dependencies")
    if not result["verified"]:
        st.caption("No verified dependencies are currently connected.")
    for v in result["verified"][:20]:
        st.write(f"✓ {v['label']} — {v['status']}")

    st.markdown("### Graph Explorer")
    node_types = sorted(set(n.get("type","unknown") for n in graph.get("nodes",[])))
    selected_type = st.selectbox("Node type", node_types, key="graph_node_type")
    filtered = [n for n in graph.get("nodes",[]) if n.get("type")==selected_type]
    for n in filtered[:50]:
        st.write(f"**{n.get('label')}** — {n.get('status')}")

    st.markdown("### Why this matters")
    st.write("This graph is designed to answer three field questions continuously: **Can we do it? What is stopping us? What is likely to stop us next?**")

with tabs[2]:
    st.markdown("### Field Intelligence")
    st.caption("Capture what happened in the field. The AI proposes structured project updates; nothing is committed until a superintendent reviews and approves it.")

    field_text=st.text_area(
        "Field update",
        placeholder="Example: Underground plumbing passed inspection. Gas pressure test is tomorrow. Transformer pad is delayed because embeds have not arrived.",
        height=150,
        key="field_update_text"
    )

    if st.button("Interpret field update",type="primary",key="field_interpret"):
        if not field_text.strip():
            st.warning("Enter or dictate a field update first.")
        else:
            task_catalog=[{"id":t["id"],"name":t["name"],"phase":t["phase"]} for t in load_tasks()]
            parsed=parse_field_update(field_text,task_catalog)
            update=new_pending_update(field_text,parsed)
            state.setdefault("pending_field_updates",[]).append(update)
            st.success("Field update interpreted. Review the proposed changes below.")

    st.markdown("### Pending Superintendent Review")
    pending=[u for u in state.get("pending_field_updates",[]) if u.get("status")=="PENDING_APPROVAL"]
    if not pending:
        st.info("No pending field updates.")
    for u in reversed(pending):
        with st.expander(f"{u['id']} — {u.get('parsed',{}).get('summary','Field update')}",expanded=True):
            st.write("**Original update**")
            st.write(u.get("raw_text",""))

            parsed=u.get("parsed",{})
            st.write("**AI observations**")
            for obs in parsed.get("observations",[]):
                st.write(f"- {obs.get('category','other')}: {obs.get('statement','')} ({obs.get('confidence','')})")

            changes=parsed.get("proposed_changes",[])
            selected=[]
            if changes:
                st.write("**Proposed project-state changes**")
                for i,ch in enumerate(changes):
                    checked=st.checkbox(
                        f"Approve: {ch.get('change_type')} — {ch.get('reason','')}",
                        value=True,
                        key=f"field_change_{u['id']}_{i}"
                    )
                    if checked:
                        selected.append(i)
                    st.json(ch)
            else:
                st.info("No structured state changes were proposed. The update can still be approved as a daily-report note.")

            c1,c2=st.columns(2)
            with c1:
                if st.button("Approve selected changes",key=f"approve_{u['id']}"):
                    approve_pending_update(state,u["id"],selected)
                    st.success("Approved changes committed to project state.")
                    st.rerun()
            with c2:
                if st.button("Reject update",key=f"reject_{u['id']}"):
                    reject_pending_update(state,u["id"])
                    st.warning("Field update rejected.")
                    st.rerun()

    st.markdown("### Daily Report Draft")
    st.markdown(daily_report(state,state.get("project_name","")))

    st.markdown("### Field Photo Intelligence")
    st.info("Photo capture is staged for the next increment. v0.12 establishes the approval workflow first so future photo observations cannot silently alter critical project status.")

with tabs[3]:
    c1,c2=st.columns(2)
    with c1:
        state["project_name"]=st.text_input("Project name",state.get("project_name",""))
        state["project_number"]=st.text_input("Project number",state.get("project_number",""))
        state["project_type"]=st.text_input("Project type",state.get("project_type",""))
        state["address_or_site"]=st.text_input("Address / site",state.get("address_or_site",""))
        state["jurisdiction"]=st.text_input("Jurisdiction / city",state.get("jurisdiction",""))
    with c2:
        state["owner"]=st.text_input("Owner",state.get("owner",""))
        state["general_contractor"]=st.text_input("General contractor",state.get("general_contractor",""))
        state["architect"]=st.text_input("Architect",state.get("architect",""))
        state["structural_engineer"]=st.text_input("Structural engineer",state.get("structural_engineer",""))
        state["civil_engineer"]=st.text_input("Civil engineer",state.get("civil_engineer",""))
    state["requires_geotechnical"]=st.checkbox("Geotechnical / soils report expected",value=state.get("requires_geotechnical",True))
    state["requires_swppp"]=st.checkbox("SWPPP expected",value=state.get("requires_swppp",False))
    state["requires_special_inspections"]=st.checkbox("Special inspection document expected",value=state.get("requires_special_inspections",False))
    state["schedule_expected"]=st.checkbox("Project schedule expected",value=state.get("schedule_expected",True))
    ready=startup_readiness(state)
    st.write("Startup status:",ready["status"])
    for item in ready["missing"]: st.write(f"- {item}")

with tabs[4]:
    uploaded=st.file_uploader("Upload project PDFs",accept_multiple_files=True,type=["pdf"],key="doc_upload")
    if uploaded:
        known={d.get("filename") for d in state.get("documents",[])}
        for f in uploaded:
            if f.name not in known:
                state.setdefault("documents",[]).append(intake_metadata(f.name,f.size))

    type_options=["civil","architectural","structural","plumbing","mechanical","electrical",
                  "fire_protection","landscape","specifications","geotechnical","permit","swppp",
                  "special_inspection","addendum","rfi","submittal","shop_drawing","schedule",
                  "unknown_pdf","other"]
    for i,doc in enumerate(state.get("documents",[])):
        with st.expander(f"{doc['filename']} — {doc.get('status','UPLOADED')}"):
            cur=doc.get("document_type","other")
            doc["document_type"]=st.selectbox("Document type",type_options,index=type_options.index(cur) if cur in type_options else len(type_options)-1,key=f"type_{i}")
            doc["discipline"]=st.text_input("Discipline",doc.get("discipline",""),key=f"disc_{i}")
            doc["sheet_or_section"]=st.text_input("Sheet / section",doc.get("sheet_or_section",""),key=f"sheet_{i}")
            doc["title"]=st.text_input("Title",doc.get("title",""),key=f"title_{i}")
            doc["revision"]=st.text_input("Revision",doc.get("revision",""),key=f"rev_{i}")
            statuses=["UPLOADED","CURRENT","SUPERSEDED","REVIEW_REQUIRED","APPROVED"]
            doc["status"]=st.selectbox("Status",statuses,index=statuses.index(doc.get("status","UPLOADED")),key=f"status_{i}")
            if st.button("Mark current",key=f"current_{i}"):
                mark_current(state["documents"],doc["document_id"]); st.success("Marked current.")
    for c in find_revision_conflicts(state.get("documents",[])):
        st.warning(f"{c['discipline']} {c['sheet_or_section']}: multiple revisions — {', '.join(c['documents'])}")

with tabs[5]:
    st.markdown("### Text Plan Reading")
    files=st.file_uploader("Choose searchable PDFs to parse",accept_multiple_files=True,type=["pdf"],key="text_parse")
    if st.button("Parse text PDFs"):
        docs_by_name={d["filename"]:d for d in state.get("documents",[])}
        for f in files or []:
            if f.name not in docs_by_name:
                meta=intake_metadata(f.name,f.size); meta["status"]="CURRENT"
                state.setdefault("documents",[]).append(meta); docs_by_name[f.name]=meta
            meta=docs_by_name[f.name]
            with tempfile.NamedTemporaryFile(delete=False,suffix=".pdf") as tmp:
                tmp.write(f.getbuffer()); temp_path=Path(tmp.name)
            pages,chunks=parse_pdf(temp_path,meta)
            state["plan_pages"]=[p for p in state.get("plan_pages",[]) if p.get("document_id")!=meta["document_id"]]+pages
            state["knowledge_chunks"]=[c for c in state.get("knowledge_chunks",[]) if c.get("document_id")!=meta["document_id"]]+chunks
            temp_path.unlink(missing_ok=True)
        state["graph"]=build_graph(state)
        st.success("Text parsed and indexed.")
    st.metric("Indexed text pages",len(state.get("plan_pages",[])))
    st.metric("Knowledge chunks",len(state.get("knowledge_chunks",[])))

with tabs[6]:
    st.markdown("### Visual Plan Analysis")
    st.caption(
        "This renders a PDF page as an image and asks a vision-capable OpenAI model to identify "
        "visible sheet information, callouts, dimensions, notes, and major construction elements."
    )

    vf=st.file_uploader("Choose a plan PDF for visual analysis",type=["pdf"],key="visual_pdf")
    page_number=st.number_input("Page number (1-based)",min_value=1,value=1,step=1)

    if st.button("Analyze drawing page visually",type="primary"):
        if not vf:
            st.warning("Choose a PDF first.")
        else:
            docs_by_name={d["filename"]:d for d in state.get("documents",[])}
            if vf.name not in docs_by_name:
                meta=intake_metadata(vf.name,vf.size); meta["status"]="CURRENT"
                state.setdefault("documents",[]).append(meta); docs_by_name[vf.name]=meta
            meta=docs_by_name[vf.name]

            with tempfile.NamedTemporaryFile(delete=False,suffix=".pdf") as tmp:
                tmp.write(vf.getbuffer()); pdf_path=Path(tmp.name)

            render_dir=Path(tempfile.gettempdir())/"construction_ai_plan_renders"
            image_path=render_pdf_page(pdf_path,int(page_number),render_dir,zoom=2.0)
            st.image(str(image_path),caption=f"{vf.name} — page {int(page_number)}",use_container_width=True)

            try:
                analysis=analyze_page_image(
                    image_path,
                    source_ref=f"{vf.name} | page {int(page_number)}"
                )
                record=build_visual_record(meta,int(page_number),image_path,analysis)

                state["visual_pages"]=[
                    p for p in state.get("visual_pages",[])
                    if not (p.get("document_id")==meta["document_id"] and p.get("page_number")==int(page_number))
                ]+[record]

                visual_chunks=visual_record_to_chunks(record)
                state["knowledge_chunks"]=[
                    c for c in state.get("knowledge_chunks",[])
                    if not (c.get("source_type")=="visual" and c.get("document_id")==meta["document_id"] and c.get("page_number")==int(page_number))
                ]+visual_chunks

                state.setdefault("visual_analysis_log",[]).append({
                    "document_id":meta["document_id"],
                    "filename":vf.name,
                    "page_number":int(page_number),
                    "source_ref":record["source_ref"]
                })

                state["graph"]=build_graph(state)

                st.success("Visual analysis added to the project brain.")
                st.json(analysis)

            except Exception as e:
                st.error(str(e))
            finally:
                pdf_path.unlink(missing_ok=True)

    st.markdown("### Indexed Visual Pages")
    for p in state.get("visual_pages",[]):
        with st.expander(f"{p.get('sheet_number') or 'Page '+str(p['page_number'])} — {p.get('sheet_title','')}"):
            st.write("Source:",p.get("source_ref"))
            st.write("Summary:",p.get("visual_summary"))
            st.write("Callouts:",p.get("callouts"))
            st.write("Dimensions:",p.get("dimensions"))
            st.write("Notes:",p.get("notes"))
            st.write("Coordination questions:",p.get("potential_conflicts_or_questions"))

with tabs[7]:
    q=st.text_area("Ask about the project",placeholder="Example: What does the footing detail show, and which sheet does it reference?",height=120)
    if st.button("Ask Construction AI",type="primary",use_container_width=True,key="ask_ai"):
        if not q.strip():
            st.warning("Ask a project or construction question.")
        else:
            st.markdown(ask_construction_ai(
                q,
                {"project_name":state.get("project_name"),"jurisdiction":state.get("jurisdiction"),
                 "project_type":state.get("project_type"),"documents":state.get("documents",[])},
                state
            ))


with tabs[8]:
    st.markdown("### Construction Readiness Engine")
    st.caption("Checks whether an activity is READY, HOLD, or AT RISK using the project evidence currently registered.")
    tasks=load_tasks()
    labels={f"{t['phase']} → {t['name']}":t for t in tasks}
    selected=st.selectbox("Activity to evaluate",list(labels.keys()),key="readiness_task")
    task=labels[selected]

    c1,c2=st.columns(2)
    with c1:
        prereq_ok=st.checkbox("Project-specific prerequisites verified",key="ready_prereq")
        state.setdefault("task_prerequisites",{})[task["id"]]={"Field prerequisites verified":prereq_ok}
    with c2:
        safety_ok=st.checkbox("JHA / pre-task safety readiness verified",key="ready_safety")
        state.setdefault("safety_readiness",{})[task["id"]]=safety_ok

    result=evaluate_readiness(state,task)
    if result["status"]=="READY":
        st.success(f"{task['name']} — READY")
    elif result["status"]=="HOLD":
        st.error(f"{task['name']} — HOLD")
    else:
        st.warning(f"{task['name']} — AT RISK")
    st.markdown(format_readiness(result))


with tabs[9]:
    st.markdown("### Submittal & Procurement Brain")
    st.caption("Work backward from the required-on-site date to expose approval, release, fabrication, shipping, and delivery risk.")

    tasks = load_tasks()
    task_labels = {f"{t['phase']} → {t['name']}": t["id"] for t in tasks}

    with st.expander("Add procurement item", expanded=True):
        c1,c2 = st.columns(2)
        with c1:
            item_name = st.text_input("Item / submittal name", key="sp_name")
            spec_section = st.text_input("Spec section", key="sp_spec")
            trade = st.text_input("Trade", key="sp_trade")
            responsible = st.text_input("Responsible subcontractor / vendor", key="sp_resp")
            linked_task_label = st.selectbox("Linked construction activity", list(task_labels.keys()), key="sp_task")
            required = st.checkbox("Required for linked activity", value=True, key="sp_required")
        with c2:
            required_on_site = st.date_input("Required on site", key="sp_ros")
            gc_days = st.number_input("GC review days", min_value=0, value=5, step=1, key="sp_gc")
            design_days = st.number_input("Design-team review days", min_value=0, value=10, step=1, key="sp_design")
            resubmit_days = st.number_input("Resubmit buffer days", min_value=0, value=7, step=1, key="sp_resub")
            fabrication_days = st.number_input("Fabrication / lead-time days", min_value=0, value=30, step=1, key="sp_fab")
            shipping_days = st.number_input("Shipping days", min_value=0, value=5, step=1, key="sp_ship")

        if st.button("Add item", type="primary", key="sp_add"):
            if not item_name.strip():
                st.warning("Enter an item name.")
            else:
                item = {
                    "id": f"proc-{len(state.get('procurement_items',[]))+1}",
                    "name": item_name.strip(),
                    "spec_section": spec_section.strip(),
                    "trade": trade.strip(),
                    "responsible_company": responsible.strip(),
                    "task_ids": [task_labels[linked_task_label]],
                    "required": required,
                    "submittal_status": "NOT_STARTED",
                    "procurement_status": "NOT_RELEASED",
                    "required_on_site_date": required_on_site.isoformat(),
                    "gc_review_days": int(gc_days),
                    "design_review_days": int(design_days),
                    "resubmit_buffer_days": int(resubmit_days),
                    "fabrication_days": int(fabrication_days),
                    "shipping_days": int(shipping_days),
                    "release_buffer_days": 0,
                    "notes": ""
                }
                state.setdefault("procurement_items", []).append(item)
                st.success("Submittal / procurement item added.")

    st.markdown("### Procurement Register")
    sub_statuses = ["NOT_STARTED","PREPARING","GC_REVIEW","DESIGN_REVIEW","REVISION_REQUIRED","APPROVED","APPROVED_AS_NOTED","REJECTED"]
    proc_statuses = ["NOT_RELEASED","RELEASED","FABRICATION","SHIPPED","DELIVERED","INSTALLED","ON_HOLD"]

    for idx,item in enumerate(state.get("procurement_items",[])):
        assessment = assess_item(item)
        label = f"{item.get('name','Unnamed')} — {assessment['risk']}"
        with st.expander(label):
            item["submittal_status"] = st.selectbox(
                "Submittal status", sub_statuses,
                index=sub_statuses.index(item.get("submittal_status","NOT_STARTED")),
                key=f"substat_{idx}"
            )
            item["procurement_status"] = st.selectbox(
                "Procurement status", proc_statuses,
                index=proc_statuses.index(item.get("procurement_status","NOT_RELEASED")),
                key=f"procstat_{idx}"
            )
            item["notes"] = st.text_area("Notes", item.get("notes",""), key=f"procnotes_{idx}")

            assessment = assess_item(item)
            dates = assessment["dates"]
            st.write(f"**Risk:** {assessment['risk']}")
            st.write(assessment["reason"])
            if dates:
                st.write(f"Submit by: **{dates['submit_by_date']}**")
                st.write(f"Approved by: **{dates['approved_by_date']}**")
                st.write(f"Release by: **{dates['release_by_date']}**")
                st.write(f"Ship by: **{dates['ship_by_date']}**")
                st.write(f"Required on site: **{dates['required_on_site_date']}**")

    st.markdown("### Procurement Risk Board")
    risks = portfolio_risks(state.get("procurement_items",[]))
    if not risks:
        st.info("No procurement items registered yet.")
    else:
        for row in risks:
            item=row["item"]; a=row["assessment"]
            st.write(f"**{a['risk']} — {item.get('name')}**: {a['reason']}")

with tabs[10]:
    tasks=load_tasks()
    labels={f"{t['phase']} → {t['name']}":t["id"] for t in tasks}
    task_id=labels[st.selectbox("Task",list(labels.keys()))]
    permit_status=st.selectbox("Permit / approval status",["NOT_CHECKED","VERIFIED","APPROVED","NOT_REQUIRED"])
    if st.button("Save permit status"):
        set_permit(state,task_id,permit_status); st.success("Saved.")
    inspection_result=st.selectbox("Inspection result",["PENDING","PASSED","FAILED","NOT_REQUIRED"])
    if st.button("Add inspection record"):
        add_inspection(state,{"task_id":task_id,"inspection_type":"Project inspection","authority_or_agency":"",
                              "required":inspection_result!="NOT_REQUIRED","result":inspection_result}); st.success("Added.")
    test_type=st.text_input("Test type","Compaction / density test")
    test_result=st.selectbox("Test result",["PENDING","PASSED","FAILED","NOT_REQUIRED"])
    if st.button("Add test record"):
        add_test(state,{"task_id":task_id,"test_type":test_type,"testing_agency":"",
                        "required":test_result!="NOT_REQUIRED","result":test_result}); st.success("Added.")
    if st.button("Mark task complete"):
        mark_complete(state,task_id); st.success("Task complete.")

with tabs[11]:
    for a in state.get("schedule_activities",[]):
        result=evaluate_activity(a,state)
        st.markdown(f"**{a['activity_id']} — {a['name']}**")
        st.write(f"Status: **{result['status']}**")
        for reason in result["reasons"]: st.caption(f"• {reason}")
        st.divider()
    st.subheader("Current Constraints")
    for item in critical_actions(state.get("schedule_activities",[]),state):
        st.markdown(f"**{item['activity']}**")
        for reason in item["reasons"]: st.write(f"- {reason}")

with tabs[12]:
    state["graph"]=build_graph(state)
    graph=state["graph"]
    st.metric("Graph nodes",len(graph.get("nodes",[])))
    st.metric("Graph edges",len(graph.get("edges",[])))
    st.caption("The graph now includes documents, plan pages, visual elements, schedule activities, and some cross-sheet references.")
    for e in graph.get("edges",[])[:100]:
        st.write(f"{e['relation']}: {e['from']} → {e['to']}")

with tabs[13]:
    for i,t in enumerate(load_tasks(),1):
        st.write(f"**{i}. {t['phase']} → {t['name']}**")
        st.caption(t["purpose"])
