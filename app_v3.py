from datetime import datetime
import tempfile
from pathlib import Path
from datetime import date
import streamlit as st

from db.session import init_db, SessionLocal
from db.models import (
    Company, User, Project, ScheduleActivity, ConstraintRecord,
    ApprovalQueueItem, InboxItem, Subcontractor, FieldVisionProposal, FieldVerificationProposal, PunchQualityItem, ProjectLocation, QualityIssue, InspectionTestRecord, ReadinessGate, SiteConditionRecord, MakeReadyAction
)
from platform.tenancy import accessible_projects
from services.project_service import create_project
from services.document_service import store_document, list_documents
from services.state_service import load_project_state
from ingestion.pipeline import create_ingestion_job, ingest_document
from imports.schedule_import import parse_schedule_csv
from schedule.service import persist_schedule
from subcontractors.service import project_subcontractors
from lookahead.engine import lookahead_activities, activity_subcontractors, ensure_commitment, commitment_health
from field_execution.service import record_field_update, get_or_create_daily_report, update_daily_report
from field_execution.constraint_intelligence import propose_constraint
from approvals.service import pending_approvals, decide_approval
from inbox.service import user_inbox, mark_read
from setup_wizard.service import refresh_progress, percent_complete
from brain.master_brain import load_tasks
from brain.command_center import command_summary, priority_actions
from brain.readiness_graph import build_readiness_graph, graph_metrics
from portfolio.dashboard import company_portfolio
from ui.components.theme import apply_theme, hero, kpi
from ui.components.activity_cards import activity_card
from ui.components.lookahead_board import lookahead_board
from field_media.service import save_field_photo
from voice.service import split_quick_update
from field_vision.analyzer import analyze_photo
from field_vision.service import approve_proposal, reject_proposal
from field_verification.engine import propose_document_checks
from field_verification.service import approve_verification
from locations.service import create_location, locations, link_photo
from quality.service import create_issue, submit_verification_photo, close_issue
from inspections.service import create_inspection, record_result
from readiness_gates.service import sync_quality_gates, sync_inspection_gates, activity_gate_status
from notifications.quality_notifications import notify_project_team
from activity_release.engine import evaluate_activity_release
from site_conditions.service import record_site_condition
from make_ready.engine import sync_make_ready_actions, close_resolved_actions
from escalations.service import prepare_followups
from briefs.superintendent import morning_brief
from analytics.operations import constraint_aging, subcontractor_reliability
from operations.meeting import weekly_make_ready_rows
from operations.settings import lead_times, set_lead_time
from production.service import record_production, activity_production_metrics
from forecasting.engine import calculate_exposure, activity_drift_signal, milestone_exposure
from cpm.forecast import forecast_project
from schedule_advanced.importer import parse_rich_schedule_csv
from schedule_advanced.service import persist_rich_schedule
from schedule_advanced.analysis import schedule_health
from cpm_engine.engine import calculate_cpm
from schedule_history.service import update_history, critical_path_rows
from schedule_quality.service import evaluate_schedule_quality, schedule_quality_findings
from schedule_quality.longest_path import longest_path_candidates
from xer.parser import parse_xer_sections
from xer.mapper import import_xer_sections
from schedule_versions.service import snapshot_current_schedule, compare_versions
from alignment.lookahead_bridge import align_current_lookahead
from drift_alerts.service import sync_drift_alerts
from matching.activity_matcher import suggest_matches, save_match
from autopilot.engine import build_daily_actions, prepare_action_message
from recovery.service import propose_recovery
from interventions.impact import downstream_impact
from interventions.recovery_sim import simulate_recovery
from routing.service import suggest_routes, save_route
from learning.interventions import record_outcome, intervention_scorecard
from recovery_sandbox.engine import run_sandbox
from recovery_optimizer.service import evaluate_options
from recovery_plans.service import approve_option, record_outcome as record_recovery_outcome, learned_recovery_summary
from company_memory.service import capture_project_learning, refresh_company_patterns, memory_for_action, generate_playbook_candidates
from company_memory.dashboard import company_memory_stats
from predictive_risk.engine import run_project_risk
from predictive_risk.calibration import calibration_summary
from sub_intelligence.service import refresh_project_subcontractor_intelligence, subcontractor_context_for_activity
from sub_intelligence.company_memory import capture_subcontractor_learning
from production_intelligence.service import refresh_project_production, refresh_company_benchmarks, capture_production_learning
from playbook_engine.service import trigger_playbooks, complete_playbook
from field_assistant.service import project_brief
from executive_intelligence.service import refresh_executive_snapshots
from governance.audit import audit
from ops.health import health_report
from core.config import Settings
from portfolio_forecast.service import refresh_company_portfolio_forecast
from trends.production import manpower_by_trade, production_trends

st.set_page_config(
    page_title="Construction Intelligence",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded"
)
apply_theme()
init_db()
db = SessionLocal()

# --------------------------
# prototype login
# --------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

companies = db.query(Company).all()

if not st.session_state.logged_in:
    st.markdown("<br><br>", unsafe_allow_html=True)
    c1,c2,c3 = st.columns([1,1.2,1])
    with c2:
        hero(
            "Construction Intelligence",
            "Run projects with plans, schedule, subcontractors, field updates, readiness, and AI in one operating system.",
            "Welcome"
        )
        if not companies:
            st.warning("No company exists yet. Run production_bootstrap.py first.")
            st.stop()

        company_map = {c.name:c for c in companies}
        company = company_map[st.selectbox("Company", list(company_map.keys()))]
        users = db.query(User).filter(User.company_id==company.id, User.active==True).all()
        if not users:
            st.warning("This company has no active users.")
            st.stop()
        user_map = {f"{u.display_name or u.email} — {u.role}":u for u in users}
        user = user_map[st.selectbox("User", list(user_map.keys()))]

        if st.button("Enter platform", type="primary", use_container_width=True):
            st.session_state.logged_in = True
            st.session_state.company_id = company.id
            st.session_state.user_id = user.id
            st.rerun()
    st.stop()

company = db.query(Company).filter(Company.id==st.session_state.company_id).first()
user = db.query(User).filter(User.id==st.session_state.user_id).first()

# --------------------------
# sidebar navigation
# --------------------------
st.sidebar.markdown("## 🏗️ Construction Intelligence")
st.sidebar.caption(f"{company.name}")
st.sidebar.write(f"**{user.display_name or user.email}**")
st.sidebar.caption(user.role)

projects = accessible_projects(db, user)
project = None
if projects:
    project_map = {f"{p.project_number or '—'} — {p.name}":p for p in projects}
    selected_project_label = st.sidebar.selectbox("Project", list(project_map.keys()))
    project = project_map[selected_project_label]

role = user.role

if project:
    sync_quality_gates(db,project.id)
    sync_inspection_gates(db,project.id)

base_pages = [
    "Home",
    "Project Setup",
    "Documents",
    "Schedule & Lookahead",
    "Schedule Alignment",
    "Make Ready",
    "Operations Intelligence",
    "Forecasting",
    "Field",
    "Quality & Inspections",
    "Readiness",
    "Approvals",
    "Inbox",
]
if role in ("owner_admin","operations_manager","project_executive"):
    base_pages.insert(1, "Portfolio")

page = st.sidebar.radio("Navigate", base_pages)

st.sidebar.divider()
if st.sidebar.button("Sign out"):
    for key in ["logged_in","company_id","user_id"]:
        st.session_state.pop(key, None)
    st.rerun()

# --------------------------
# HOME
# --------------------------
if page == "Home":
    title = "Operations Home" if role in ("owner_admin","operations_manager","project_executive") else "Superintendent Home"
    subtitle = "See what is ready, blocked, at risk, and worth your attention first."
    hero(title, subtitle, project.name if project else company.name)

    if not project:
        st.info("No project is assigned to this user.")
    else:
        state = load_project_state(db, project.id)
        tasks = load_tasks()
        summary = command_summary(state, tasks)
        actions = priority_actions(state, tasks)
        graph = build_readiness_graph(state, tasks)
        metrics = graph_metrics(graph)

        cols = st.columns(5)
        with cols[0]: kpi("READY", summary.get("READY",0), "Activities clear to proceed")
        with cols[1]: kpi("HOLD", summary.get("HOLD",0), "Blocking conditions")
        with cols[2]: kpi("AT RISK", summary.get("AT_RISK",0), "Near-term exposure")
        with cols[3]: kpi("Open RFIs", summary.get("OPEN_RFIS",0), "Needs project follow-up")
        with cols[4]: kpi("Graph nodes", metrics.get("nodes",0), "Connected project intelligence")

        brief=morning_brief(db,project.id)
        st.markdown("### Morning command brief")
        b1,b2,b3=st.columns(3)
        with b1: kpi("6-week READY",brief["snapshot"]["ready"],"Current make-ready scan")
        with b2: kpi("6-week HOLD",brief["snapshot"]["hold"],"Blocking activities")
        with b3: kpi("6-week AT RISK",brief["snapshot"]["at_risk"],"Needs action")

        c1,c2 = st.columns([1.2,1])
        with c1:
            st.markdown("### What should I handle first?")
            if not actions:
                st.success("No critical project actions are currently recorded.")
            for i,a in enumerate(actions[:10],1):
                icon = "🔴" if a["priority"]==1 else "🟠" if a["priority"]==2 else "🟡"
                st.markdown(f"{icon} **{i}. {a['category']} — {a['title']}**")
                st.caption(a["reason"])

        with c2:
            st.markdown("### 6-week lookahead")
            rows = lookahead_activities(db, project.id, weeks=6)
            if not rows:
                st.info("No activities are currently in the six-week window.")
            for a in rows[:12]:
                subs = activity_subcontractors(db, project.id, a.id)
                sub_label = ", ".join(s.name for s in subs) if subs else "No sub assigned"
                activity_card(
                    f"{a.external_id} — {a.name}",
                    status=a.status or "NOT VERIFIED",
                    meta=f"{a.planned_start} → {a.planned_finish}",
                    owner=sub_label
                )

# --------------------------
# SUPERINTENDENT COMMAND
# --------------------------
elif page == "Superintendent Command":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Daily Superintendent Command","The job tells you what needs attention, why it matters, and what to do next.",project.name)
        from db.models import PredictiveRiskSnapshot
        latest_risk={}
        for rr in db.query(PredictiveRiskSnapshot).filter(
            PredictiveRiskSnapshot.project_id==project.id
        ).order_by(PredictiveRiskSnapshot.created_at.desc()).all():
            latest_risk.setdefault(rr.schedule_activity_id,rr)
        top_risks=sorted(latest_risk.values(),key=lambda x:x.risk_score,reverse=True)[:3]
        if top_risks:
            st.markdown("### What may hurt the job next")
            for rr in top_risks:
                if rr.risk_score<30: continue
                aa=db.query(ScheduleActivity).filter(ScheduleActivity.id==rr.schedule_activity_id).first()
                st.write(f"**{rr.probability_band} · {aa.name if aa else rr.schedule_activity_id} · risk {rr.risk_score:.0f}/100**")
                st.caption(rr.explanation)
        actions=build_daily_actions(db,project.id)
        if not actions:
            st.success("No open command actions. Keep the job moving.")
        else:
            st.caption("Ranked from highest operational exposure to lowest.")
        for i,action in enumerate(actions[:15],1):
            with st.expander(f"{i}. {action.severity} · {action.title}",expanded=i<=5):
                st.write(f"**Why am I seeing this?** {action.why}")
                st.write(f"**Recommended action:** {action.recommended_action}")
                memory=memory_for_action(db,user.company_id,action)
                if memory:
                    st.write("**What your company history says**")
                    for p in memory:
                        st.caption(f"{p.recommendation} · confidence {p.confidence:.0%}")
                impact=downstream_impact(db,project.id,action.schedule_activity_id) if action.schedule_activity_id else []
                if impact:
                    st.write(f"**Downstream exposure:** {len(impact)} activity(ies) within 4 logic hops.")
                    st.caption(" → ".join(x["name"] for x in impact[:5]))
                st.caption(f"Owner role: {action.owner_role} · Due: {action.due_date or 'Today / coordinate'} · Priority score: {action.score:.0f}")
                routes=suggest_routes(db,action)
                if action.schedule_activity_id:
                    aa=db.query(ScheduleActivity).filter(ScheduleActivity.id==action.schedule_activity_id).first()
                    if aa:
                        from db.models import ActivityAssignment
                        assignment=db.query(ActivityAssignment).filter(
                            ActivityAssignment.project_id==project.id,
                            ActivityAssignment.schedule_activity_id==aa.id
                        ).first()
                        if assignment:
                            subctx=subcontractor_context_for_activity(db,project.id,assignment.subcontractor_id)
                            if subctx:
                                st.write("**Subcontractor context**")
                                st.caption(subctx["explanation"])
                if routes:
                    st.write("**Suggested routing**")
                    for r in routes[:4]:
                        st.caption(f"{r['name']} · {r['email'] or 'no email'} · {r['reason']}")
                c1,c2=st.columns(2)
                with c1:
                    if st.button("Prepare communication",key=f"prep_action_{action.id}"):
                        prepare_action_message(db,action)
                        st.success("Draft communication prepared for review.")
                with c2:
                    if action.schedule_activity_id and st.button("Show recovery options",key=f"recovery_{action.id}"):
                        a=db.query(ScheduleActivity).filter(ScheduleActivity.id==action.schedule_activity_id).first()
                        if a:
                            scenarios=propose_recovery(db,project.id,a)
                            for s in scenarios:
                                sim=simulate_recovery(db,project.id,a.id,s.scenario_type)
                                st.write(f"**{s.scenario_type}** — {s.description}")
                                st.caption(f"Screening recovery: {sim.modeled_days_recovered:.1f} day(s) on remaining duration · confidence {sim.confidence:.0%}. {sim.assumptions}")

        st.markdown("### Intervention learning")
        scorecard=intervention_scorecard(db,project.id)
        if not scorecard:
            st.caption("Outcome learning begins after recovery actions are completed and rated.")
        for typ,g in scorecard.items():
            st.write(f"**{typ}** · {g['count']} outcome(s) · on-time {g['on_time_rate']:.0%} · actual days saved {g['actual']:.1f}")

# --------------------------
# PREDICTIVE RISK
# --------------------------
elif page == "Predictive Risk":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Predictive Risk Engine","Surface emerging execution risk before it becomes a visible schedule miss.",project.name)
        st.caption("Explainable risk screening — not a guaranteed probability of delay.")
        if st.button("Run predictive risk scan",type="primary"):
            rows=run_project_risk(db,project.id,user.company_id)
            st.success(f"Scored {len(rows)} schedule activity(ies).")
            st.rerun()

        from db.models import PredictiveRiskSnapshot
        rows=db.query(PredictiveRiskSnapshot).filter(
            PredictiveRiskSnapshot.project_id==project.id
        ).order_by(PredictiveRiskSnapshot.created_at.desc()).all()
        latest={}
        for r in rows:latest.setdefault(r.schedule_activity_id,r)
        ranked=sorted(latest.values(),key=lambda x:x.risk_score,reverse=True)

        if ranked:
            c1,c2,c3=st.columns(3)
            with c1:kpi("Critical risk",sum(1 for r in ranked if r.probability_band=="CRITICAL"),"Activities")
            with c2:kpi("High risk",sum(1 for r in ranked if r.probability_band=="HIGH"),"Activities")
            with c3:kpi("Watch",sum(1 for r in ranked if r.probability_band=="WATCH"),"Activities")

        for r in ranked[:30]:
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            with st.expander(f"{r.probability_band} · {r.risk_score:.0f}/100 · {a.external_id if a else ''} — {a.name if a else r.schedule_activity_id}",expanded=r.risk_score>=55):
                st.write(r.explanation)
                st.write("**Risk evidence**")
                st.caption(
                    f"Schedule drift {r.schedule_drift_points:.1f} · "
                    f"constraints {r.constraint_points:.1f} · "
                    f"commitments {r.commitment_points:.1f} · "
                    f"production {r.production_points:.1f} · "
                    f"downstream {r.downstream_points:.1f} · "
                    f"company history {r.company_history_points:.1f}"
                )

        cal=calibration_summary(db,project.id)
        st.markdown("### Model calibration")
        if cal["count"]:
            st.write(f"{cal['count']} verified outcome(s) · directional hit rate {cal['hit_rate']:.0%}")
        else:
            st.caption("Calibration begins when predicted risks are later compared with actual project outcomes.")

# --------------------------
# SUBCONTRACTOR INTELLIGENCE
# --------------------------
elif page == "Subcontractor Intelligence":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Subcontractor Intelligence","Understand operational behavior in context without reducing a trade partner to a black-box score.",project.name)
        st.caption("Use this for coordination and earlier intervention — not blame or automatic vendor decisions.")
        c1,c2=st.columns(2)
        with c1:
            if st.button("Refresh subcontractor intelligence",type="primary"):
                rows=refresh_project_subcontractor_intelligence(db,project.id)
                st.success(f"Refreshed {len(rows)} subcontractor snapshot(s).")
                st.rerun()
        with c2:
            if st.button("Capture into company memory"):
                n=capture_subcontractor_learning(db,user.company_id,project.id)
                st.success(f"Captured {n} subcontractor learning signal(s).")
                st.rerun()

        from db.models import SubcontractorPerformanceSnapshot
        rows=db.query(SubcontractorPerformanceSnapshot).filter(
            SubcontractorPerformanceSnapshot.project_id==project.id
        ).order_by(SubcontractorPerformanceSnapshot.created_at.desc()).all()
        latest={}
        for r in rows:latest.setdefault(r.subcontractor_id,r)
        ranked=sorted(latest.values(),key=lambda x:x.context_score)

        for r in ranked:
            sub=db.query(Subcontractor).filter(Subcontractor.id==r.subcontractor_id).first()
            with st.expander(f"{sub.name if sub else r.subcontractor_id} · {r.trade or 'Trade'} · context {r.context_score:.0f}/100"):
                c1,c2,c3,c4=st.columns(4)
                with c1:kpi("Response reliability",f"{r.response_reliability:.0%}","Confirmed commitments")
                with c2:kpi("Start reliability",f"{r.start_reliability:.0%}","Commitment follow-through proxy")
                with c3:kpi("Manpower reliability",f"{r.manpower_reliability:.0%}","Reported manpower readiness")
                with c4:kpi("Production reliability",f"{r.production_reliability:.0%}","Installed vs reported plan")
                st.write(r.explanation)
                st.caption(
                    f"Early-warning behavior {r.early_warning_rate:.0%} · "
                    f"Open quality {r.quality_open_count} · Closed quality {r.quality_closed_count}"
                )

# --------------------------
# PRODUCTION INTELLIGENCE
# --------------------------
elif page == "Production Intelligence":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Production Intelligence","Turn daily installed quantities and crew effort into early schedule signals and company production knowledge.",project.name)
        c1,c2,c3=st.columns(3)
        with c1:
            if st.button("Refresh production intelligence",type="primary"):
                rows=refresh_project_production(db,project.id)
                st.success(f"Refreshed {len(rows)} activity production snapshot(s).")
                st.rerun()
        with c2:
            if st.button("Update company benchmarks"):
                rows=refresh_company_benchmarks(db,user.company_id)
                st.success(f"Updated {len(rows)} company production benchmark(s).")
                st.rerun()
        with c3:
            if st.button("Capture production learning"):
                n=capture_production_learning(db,user.company_id,project.id)
                st.success(f"Captured {n} production learning signal(s).")
                st.rerun()

        from db.models import ProductionIntelligenceSnapshot,CompanyProductionBenchmark
        rows=db.query(ProductionIntelligenceSnapshot).filter(
            ProductionIntelligenceSnapshot.project_id==project.id
        ).order_by(ProductionIntelligenceSnapshot.created_at.desc()).all()
        latest={}
        for r in rows:latest.setdefault(r.schedule_activity_id,r)
        ranked=sorted(latest.values(),key=lambda x:(x.pace_ratio if x.planned_quantity_per_day else 999))

        st.markdown("### Current production signals")
        if not ranked:
            st.caption("Production intelligence appears after daily production records are tied to schedule activities.")
        for r in ranked:
            a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
            label="AT RISK" if r.planned_quantity_per_day and r.pace_ratio<0.85 else "WATCH" if r.trend=="FADING" else "ON PACE"
            with st.expander(f"{label} · {a.external_id if a else ''} — {a.name if a else r.schedule_activity_id}"):
                c1,c2,c3,c4=st.columns(4)
                with c1:kpi("Qty/day",f"{r.quantity_per_day:.1f}",r.quantity_unit or "units")
                with c2:kpi("Qty/labor hr",f"{r.quantity_per_labor_hour:.3f}","Productivity")
                with c3:kpi("Avg crew",f"{r.average_crew:.1f}","Observed")
                with c4:kpi("Pace",f"{r.pace_ratio:.0%}" if r.planned_quantity_per_day else "—","Vs daily plan")
                st.write(r.explanation)
                if r.projected_remaining_days:
                    st.caption(f"Production-rate projected remaining duration: {r.projected_remaining_days:.1f} workday-equivalent observation days.")

        st.markdown("### Company production benchmarks")
        benches=db.query(CompanyProductionBenchmark).filter(
            CompanyProductionBenchmark.company_id==user.company_id
        ).order_by(CompanyProductionBenchmark.confidence.desc()).all()
        if not benches:
            st.caption("Benchmarks strengthen as more projects contribute comparable production observations.")
        for b in benches[:50]:
            st.write(f"**{b.trade or 'General'} · {b.activity_type}**")
            st.caption(
                f"{b.sample_count} sample(s) · {b.average_quantity_per_day:.1f} {b.quantity_unit}/day · "
                f"{b.average_quantity_per_labor_hour:.3f} {b.quantity_unit}/labor-hour · "
                f"crew {b.average_crew:.1f} · confidence {b.confidence:.0%}"
            )

# --------------------------
# COMPANY PLAYBOOKS
# --------------------------
elif page == "Company Playbooks":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Company Playbooks","Turn repeated company evidence into executable field routines with owners, due dates, escalation, and feedback.",project.name)
        if st.button("Trigger applicable playbooks",type="primary"):
            rows=trigger_playbooks(db,user.company_id,project.id,user.id)
            audit(db,user.company_id,"PLAYBOOK_TRIGGER",project.id,user.id,decision=f"{len(rows)} created")
            st.success(f"Created {len(rows)} new playbook execution(s).")
            st.rerun()

        from db.models import PlaybookExecution,PlaybookChecklistItem,CompanyPlaybookRule
        executions=db.query(PlaybookExecution).filter(
            PlaybookExecution.project_id==project.id
        ).order_by(PlaybookExecution.created_at.desc()).all()
        if not executions:
            st.caption("Generate evidence-backed company playbooks in Company Memory, then trigger them here.")
        for ex in executions:
            rule=db.query(CompanyPlaybookRule).filter(CompanyPlaybookRule.id==ex.company_playbook_rule_id).first()
            with st.expander(f"{ex.status} · {rule.rule_name if rule else 'Playbook'} · due {ex.due_date}"):
                st.write(ex.triggered_reason)
                items=db.query(PlaybookChecklistItem).filter(
                    PlaybookChecklistItem.playbook_execution_id==ex.id
                ).order_by(PlaybookChecklistItem.item_order.asc()).all()
                for item in items:
                    checked=st.checkbox(item.item_text,value=item.status=="COMPLETE",key=f"pb_item_{item.id}")
                    if checked and item.status!="COMPLETE":
                        item.status="COMPLETE";item.completed_at=datetime.utcnow();db.commit()
                if ex.status=="OPEN":
                    helped=st.selectbox("Did this playbook help?",["Not decided","Yes","No"],key=f"pb_help_{ex.id}")
                    feedback=st.text_area("Feedback / lesson",key=f"pb_feedback_{ex.id}")
                    if helped!="Not decided" and st.button("Complete playbook",key=f"pb_complete_{ex.id}"):
                        complete_playbook(db,ex.id,helped=="Yes",feedback)
                        audit(db,user.company_id,"PLAYBOOK_COMPLETE",project.id,user.id,"PlaybookExecution",ex.id,helped,feedback)
                        st.success("Playbook outcome recorded.")
                        st.rerun()

# --------------------------
# FIELD ASSISTANT
# --------------------------
elif page == "Field Assistant":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Field Assistant","A grounded superintendent briefing built from the project's own risk, recovery, production, and company-memory evidence.",project.name)
        question=st.text_input("Ask the project",value="What needs my attention today?")
        if st.button("Build field brief",type="primary"):
            q=project_brief(db,user.company_id,project.id,user.id,question)
            audit(db,user.company_id,"FIELD_BRIEF",project.id,user.id,"FieldAssistantQuery",q.id,"generated",q.evidence)
            st.session_state["field_query_id"]=q.id
            st.rerun()
        from db.models import FieldAssistantQuery
        qid=st.session_state.get("field_query_id")
        q=db.query(FieldAssistantQuery).filter(FieldAssistantQuery.id==qid).first() if qid else None
        if q:
            st.markdown("### Superintendent brief")
            st.write(q.answer)
            st.caption(f"Evidence trace: {q.evidence or 'No linked evidence IDs'}")
            st.info("This assistant summarizes project evidence. It does not replace superintendent judgment, safety review, contract review, or the authoritative schedule.")

# --------------------------
# RECOVERY SANDBOX
# --------------------------
elif page == "Recovery Sandbox":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Recovery Sandbox","Test recovery ideas against a cloned schedule network before changing the field plan.",project.name)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).order_by(ScheduleActivity.planned_start.asc()).all()
        if not acts:
            st.info("Import a schedule first.")
        else:
            labels={f"{a.external_id} — {a.name}":a for a in acts}
            selected=st.selectbox("Activity",list(labels.keys()))
            a=labels[selected]
            scenario=st.selectbox("Recovery idea",[
                "ADD_CREW","OVERTIME","WORK_SATURDAY","CLEAR_CONSTRAINT","RESEQUENCE"
            ])
            help_text={
                "ADD_CREW":"Percent crew increase (screening assumption)",
                "OVERTIME":"Percent overtime increase",
                "WORK_SATURDAY":"Value is ignored",
                "CLEAR_CONSTRAINT":"Estimated days removed from remaining duration",
                "RESEQUENCE":"Percent overlap / compression assumption"
            }[scenario]
            value=st.number_input(help_text,min_value=0.0,max_value=60.0,value=20.0,step=1.0)
            if st.button("Run recovery simulation",type="primary"):
                run,results=run_sandbox(db,project.id,a.id,scenario,value)
                if run:
                    st.session_state["sandbox_run_id"]=run.id
                    st.success("Recovery scenario simulated.")
                    st.rerun()
            from db.models import RecoverySandboxRun,RecoverySandboxActivityResult
            run_id=st.session_state.get("sandbox_run_id")
            run=db.query(RecoverySandboxRun).filter(RecoverySandboxRun.id==run_id).first() if run_id else None
            if run:
                c1,c2,c3,c4=st.columns(4)
                with c1:kpi("Original finish",run.original_completion,"Current cloned network")
                with c2:kpi("Simulated finish",run.simulated_completion,run.scenario_type)
                with c3:kpi("Project days recovered",f"{run.project_days_recovered:.0f}","Modeled")
                with c4:kpi("Confidence",f"{run.confidence:.0%}","Screening confidence")
                st.caption(run.assumptions)
                st.markdown("### Activities affected")
                rows=db.query(RecoverySandboxActivityResult).filter(
                    RecoverySandboxActivityResult.recovery_sandbox_run_id==run.id
                ).all()
                if not rows:st.info("This scenario did not move downstream dates in the cloned network.")
                for r in rows[:50]:
                    aa=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
                    st.write(f"**{aa.external_id if aa else ''} — {aa.name if aa else r.schedule_activity_id}**")
                    st.caption(
                        f"{r.original_start} → {r.simulated_start} · "
                        f"{r.original_finish} → {r.simulated_finish} · "
                        f"finish shift {r.finish_shift_days:+d} workday(s)"
                    )

# --------------------------
# RECOVERY OPTIMIZER
# --------------------------
elif page == "Recovery Optimizer":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Recovery Optimizer","Compare recovery options by schedule benefit, cost, confidence, and constructability before approving a plan.",project.name)
        acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).order_by(ScheduleActivity.planned_start.asc()).all()
        if not acts:
            st.info("Import a schedule first.")
        else:
            amap={f"{a.external_id} — {a.name}":a for a in acts}
            a=amap[st.selectbox("Driving / exposed activity",list(amap.keys()),key="optimizer_activity")]
            if st.button("Evaluate recovery options",type="primary"):
                options=evaluate_options(db,project.id,a.id)
                st.session_state["optimizer_activity_id"]=a.id
                st.success(f"Evaluated {len(options)} recovery option(s).")
                st.rerun()

            from db.models import RecoveryOptionEvaluation,ApprovedRecoveryPlan,RecoveryOutcomeReview
            options=db.query(RecoveryOptionEvaluation).filter(
                RecoveryOptionEvaluation.project_id==project.id,
                RecoveryOptionEvaluation.schedule_activity_id==a.id,
                RecoveryOptionEvaluation.status.in_(["PROPOSED","APPROVED"])
            ).order_by(RecoveryOptionEvaluation.optimizer_score.desc()).all()

            if options:
                st.markdown("### Ranked recovery options")
                for i,o in enumerate(options[:10],1):
                    with st.expander(f"{i}. {o.scenario_type} · score {o.optimizer_score:.1f}",expanded=i<=3):
                        c1,c2,c3,c4=st.columns(4)
                        with c1:kpi("Project days",f"{o.modeled_project_days_recovered:.1f}","Modeled recovered")
                        with c2:kpi("Est. cost",f"${o.estimated_cost:,.0f}","Screening estimate")
                        with c3:kpi("Confidence",f"{o.confidence:.0%}","Model confidence")
                        with c4:kpi("Risk",o.constructability_risk,"Constructability")
                        st.write(o.assumptions)
                        st.warning("Safety review, subcontractor coordination, cost validation, and authoritative CPM review are required before execution.")
                        target=st.text_input("Target completion",key=f"target_{o.id}",placeholder="YYYY-MM-DD")
                        note=st.text_input("Approval note",key=f"plan_note_{o.id}")
                        if o.status=="PROPOSED" and st.button("Approve recovery option",key=f"approve_opt_{o.id}"):
                            approve_option(db,o.id,user.id,target,note)
                            st.success("Recovery plan approved for tracking.")
                            st.rerun()

            st.markdown("### Active / completed recovery plans")
            plans=db.query(ApprovedRecoveryPlan).filter(
                ApprovedRecoveryPlan.project_id==project.id
            ).order_by(ApprovedRecoveryPlan.created_at.desc()).all()
            for p in plans:
                opt=db.query(RecoveryOptionEvaluation).filter(
                    RecoveryOptionEvaluation.id==p.recovery_option_evaluation_id
                ).first()
                with st.expander(f"Plan #{p.id} · {opt.scenario_type if opt else 'Recovery'} · {p.status}"):
                    st.caption(f"Predicted recovery {p.predicted_days_recovered:.1f} days · predicted cost ${p.predicted_cost:,.0f}")
                    if p.status=="ACTIVE":
                        actual_finish=st.text_input("Actual completion",key=f"actual_finish_{p.id}",placeholder="YYYY-MM-DD")
                        actual_days=st.number_input("Actual days recovered",0.0,365.0,0.0,key=f"actual_days_{p.id}")
                        actual_cost=st.number_input("Actual recovery cost",0.0,100000000.0,0.0,key=f"actual_cost_{p.id}")
                        lessons=st.text_area("Lessons learned",key=f"lessons_{p.id}")
                        if st.button("Close plan + record outcome",key=f"close_plan_{p.id}"):
                            record_recovery_outcome(db,p.id,actual_finish,actual_days,actual_cost,lessons)
                            st.success("Outcome recorded. Recovery learning updated.")
                            st.rerun()
                    else:
                        out=db.query(RecoveryOutcomeReview).filter(
                            RecoveryOutcomeReview.approved_recovery_plan_id==p.id
                        ).first()
                        if out:
                            st.write(f"Outcome: **{out.outcome_rating}** · actual recovery {out.actual_days_recovered:.1f} days · actual cost ${out.actual_cost:,.0f}")
                            st.caption(out.lessons)

            st.markdown("### Recovery learning")
            learned=learned_recovery_summary(db,project.id)
            if not learned:
                st.caption("Learning begins after approved recovery plans are completed.")
            for typ,g in learned.items():
                st.write(f"**{typ}** · {g['count']} completed plan(s) · strong outcomes {g['strong_rate']:.0%}")
                st.caption(f"Predicted {g['predicted']:.1f} days · actual {g['actual']:.1f} days · actual cost ${g['cost']:,.0f}")

# --------------------------
# PORTFOLIO
# --------------------------
elif page == "Portfolio":
    hero("Company Portfolio", "See risk across every active project.", company.name)
    rows = company_portfolio(db, company.id)
    cols=st.columns(4)
    with cols[0]: kpi("Projects",len(rows),"Company portfolio")
    with cols[1]: kpi("Blocking constraints",sum(r["blocking_constraints"] for r in rows),"Across all jobs")
    with cols[2]: kpi("Sub delays",sum(r["sub_delays"] for r in rows),"Reported commitments")
    with cols[3]: kpi("Pending commitments",sum(r["pending_commitments"] for r in rows),"Awaiting response")
    if st.button("Refresh portfolio forecast",type="primary"):
        refresh_company_portfolio_forecast(db,company.id)
        st.rerun()
    from db.models import PortfolioForecastSnapshot
    pf=db.query(PortfolioForecastSnapshot).filter(
        PortfolioForecastSnapshot.company_id==company.id
    ).order_by(PortfolioForecastSnapshot.created_at.desc()).all()
    latest_by_project={}
    for x in pf:
        latest_by_project.setdefault(x.project_id,x)

    for r in rows:
        snap=latest_by_project.get(r["project_id"])
        label=f"{r['project_number'] or '—'} — {r['name']} · Risk {r['portfolio_risk_score']}"
        if snap:
            label+=f" · Intervention {snap.intervention_score:.1f}"
        with st.expander(label):
            st.write(f"Jurisdiction: **{r['jurisdiction'] or 'Not set'}**")
            st.write(f"Blocking constraints: **{r['blocking_constraints']}**")
            st.write(f"Sub delays: **{r['sub_delays']}**")
            st.write(f"Pending commitments: **{r['pending_commitments']}**")
            if snap:
                st.write(f"Forecast variance: **{snap.variance_days:+d} days**")
                st.write(f"Forecast confidence: **{snap.confidence:.0%}**")
                st.write(f"Intervention score: **{snap.intervention_score:.1f}**")

# --------------------------
# PROJECT SETUP
# --------------------------
elif page == "Project Setup":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Project Setup", "Get the project brain ready before field execution begins.", project.name)
        p = refresh_progress(db, project)
        pct = percent_complete(p)
        st.progress(pct/100)
        st.write(f"Setup complete: **{pct}%**")

        left,right = st.columns([1.1,1])
        with left:
            st.markdown("### Project identity")
            st.write(f"Project number: **{project.project_number or '—'}**")
            st.write(f"Jurisdiction: **{project.jurisdiction or 'Not set'}**")
            st.write(f"Owner: **{project.owner or '—'}**")
            st.write(f"Architect: **{project.architect or '—'}**")

        with right:
            st.markdown("### Project locations")
            lname=st.text_input("Add location",placeholder="Level 2 - East Wing")
            ltype=st.selectbox("Location type",["BUILDING","LEVEL","AREA","GRID","ROOM","ZONE"])
            lcode=st.text_input("Location code",placeholder="L2-E")
            if st.button("Add project location") and lname:
                create_location(db,project.id,lname,ltype,lcode)
                st.rerun()
            for loc in locations(db,project.id):
                st.caption(f"{loc.location_type} · {loc.code or '—'} · {loc.name}")

            st.markdown("### Setup checklist")
            steps=[
                ("Project/company information",p.company_info_complete),
                ("Project team",p.team_complete),
                ("Jurisdiction",p.jurisdiction_complete),
                ("Project documents",p.documents_complete),
                ("Schedule",p.schedule_complete),
                ("Subcontractors",p.subcontractors_complete),
                ("Requirements/source setup",p.requirements_complete),
            ]
            for label,done in steps:
                st.write(("✅" if done else "⬜")+" "+label)

# --------------------------
# DOCUMENTS
# --------------------------
elif page == "Documents":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Project Documents", "Upload plans, specs, geotech, permits, schedules, submittals, and other project sources.", project.name)
        uploaded = st.file_uploader("Upload PDF", type=["pdf"])
        dtype = st.selectbox("Document type",[
            "civil","architectural","structural","plumbing","mechanical","electrical",
            "fire_protection","specifications","geotechnical","permit","swppp",
            "special_inspection","addendum","rfi","submittal","shop_drawing","schedule","other"
        ])
        discipline = st.text_input("Discipline", value=dtype if dtype not in ("other","specifications") else "")
        revision = st.text_input("Revision")
        if st.button("Store + ingest", type="primary"):
            if not uploaded:
                st.warning("Choose a PDF.")
            else:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(uploaded.getbuffer())
                    temp_path = Path(tmp.name)
                doc = store_document(
                    db, project.id, temp_path, uploaded.name,
                    document_type=dtype, discipline=discipline,
                    revision=revision, status="CURRENT"
                )
                temp_path.unlink(missing_ok=True)
                job = create_ingestion_job(db, project.id, doc.id)
                try:
                    ingest_document(db, job.id)
                    st.success("Document stored and indexed.")
                except Exception as e:
                    st.error(f"Stored, but ingestion failed: {e}")
                st.rerun()

        st.markdown("### Document register")
        for d in list_documents(db, project.id):
            st.write(f"**{d.filename}** — {d.document_type} — {d.status}")
            st.caption(f"Revision {d.revision or '—'}")

# --------------------------
# SCHEDULE & LOOKAHEAD
# --------------------------
elif page == "Schedule & Lookahead":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Schedule & Lookahead", "Turn the master schedule into the field plan and subcontractor commitments.", project.name)

        st.markdown("### Import schedule")
        mode=st.radio("Import type",["Standard CSV","Rich CPM CSV"],horizontal=True)
        sched = st.file_uploader("Schedule CSV", type=["csv"], key="v3_schedule")
        if sched and st.button("Import schedule", type="primary"):
            with tempfile.NamedTemporaryFile(delete=False,suffix=".csv") as tmp:
                tmp.write(sched.getbuffer())
                path=Path(tmp.name)
            if mode=="Standard CSV":
                rows=parse_schedule_csv(path)
                persist_schedule(db, project.id, rows)
                count=len(rows)
            else:
                activities,relationships=parse_rich_schedule_csv(path)
                persist_rich_schedule(db,project.id,activities,relationships)
                count=len(activities)
            path.unlink(missing_ok=True)
            st.success(f"Imported {count} schedule activities.")
            st.rerun()

        st.markdown("### P6 / XER structure")
        xer=st.file_uploader("Optional XER file",type=["xer"],key="xer_upload")
        if xer:
            x1,x2=st.columns(2)
            with x1:
                if st.button("Inspect XER"):
                    with tempfile.NamedTemporaryFile(delete=False,suffix=".xer") as tmp:
                        tmp.write(xer.getbuffer());xp=Path(tmp.name)
                    sections=parse_xer_sections(xp);xp.unlink(missing_ok=True)
                    st.write("Detected XER sections:",", ".join(sorted(sections.keys())[:20]))
            with x2:
                if st.button("Import XER",type="primary"):
                    with tempfile.NamedTemporaryFile(delete=False,suffix=".xer") as tmp:
                        tmp.write(xer.getbuffer());xp=Path(tmp.name)
                    sections=parse_xer_sections(xp);xp.unlink(missing_ok=True)
                    counts=import_xer_sections(db,project.id,sections)
                    st.success(
                        f"XER imported: {counts['activities']} activities, "
                        f"{counts['relationships']} relationships, {counts['wbs']} WBS nodes, "
                        f"{counts['calendars']} calendars."
                    )
                    st.rerun()

        st.markdown("### Schedule health")
        for h in schedule_health(db,project.id)[:30]:
            tag="CRITICAL" if h["critical"] else "NEAR CRITICAL" if h["near_critical"] else "FLOAT"
            st.write(f"**{h['external_id']} — {h['name']}** · {tag} · TF {h['total_float']:.1f}")
            st.caption(f"Baseline variance {h['baseline_variance_days']:+d} days · Data date {h['data_date'] or '—'}")
        st.markdown("### CPM calculation")
        st.caption("Uses imported relationship types/lags, remaining durations, data date, a default Monday-Friday work calendar, and supported start constraints.")
        if st.button("Run CPM calculation",type="primary"):
            update,snaps=calculate_cpm(db,project.id)
            if update:
                st.success(f"CPM calculated through {update.calculated_completion}.")
                st.rerun()
        history=update_history(db,project.id)
        if history:
            latest=history[-1]
            c1,c2,c3=st.columns(3)
            with c1:kpi("Calculated completion",latest["calculated_completion"],"Latest CPM pass")
            with c2:kpi("Critical activities",latest["critical_count"],"TF ≤ 0")
            with c3:kpi("Negative float",latest["negative_float_count"],"Needs schedule review")
            st.markdown("### Calculated critical path")
            for r in critical_path_rows(db,project.id,latest["data_date"])[:30]:
                st.write(f"**{r['external_id']} — {r['activity']}**")
                st.caption(f"ES {r['early_start']} · EF {r['early_finish']} · LS {r['late_start']} · LF {r['late_finish']} · TF {r['total_float']:.1f}")
            st.markdown("### Schedule update history")
            for h in history:
                st.write(f"**{h['data_date']}** · completion {h['calculated_completion']} · critical {h['critical_count']} · negative float {h['negative_float_count']}")

            st.markdown("### Schedule quality check")
            if st.button("Run schedule quality checks"):
                qs=evaluate_schedule_quality(db,project.id)
                st.session_state["latest_schedule_quality"]=qs.id
                st.rerun()
            from db.models import ScheduleQualitySnapshot
            qs=db.query(ScheduleQualitySnapshot).filter(
                ScheduleQualitySnapshot.project_id==project.id
            ).order_by(ScheduleQualitySnapshot.created_at.desc()).first()
            if qs:
                q1,q2,q3=st.columns(3)
                with q1:kpi("Quality score",f"{qs.quality_score:.0f}/100","Explainable schedule checks")
                with q2:kpi("Open ends",qs.open_ends,"Potential incomplete logic")
                with q3:kpi("Logic density",qs.relationship_density,"Relationships per activity")
                for finding in schedule_quality_findings(qs):
                    st.warning(finding)

            st.markdown("### Longest-path candidate")
            for r in longest_path_candidates(db,project.id):
                st.write(f"{r['depth']}. **{r['external_id']} — {r['name']}**")

        st.markdown("### 6-week lookahead")
        rows = lookahead_activities(db, project.id, weeks=6)
        lookahead_board(rows, lambda a: activity_subcontractors(db, project.id, a.id))
        st.markdown("### Activity commitments")
        for a in rows:
            with st.expander(f"{a.external_id} — {a.name} · {a.planned_start}"):
                subs = activity_subcontractors(db, project.id, a.id)
                if not subs:
                    st.warning("No subcontractor assigned.")
                release=evaluate_activity_release(db,project.id,a.id)
                if release["status"]=="READY":
                    st.success("READY TO RELEASE")
                elif release["status"]=="HOLD":
                    st.error("HOLD")
                else:
                    st.warning("AT RISK")
                for g in release["blocking_reasons"]+release["risk_reasons"]:
                    st.caption(f"{g['gate']} · {g['reason']}")
                for s in subs:
                    commitment=ensure_commitment(db,project.id,a,s.id)
                    st.write(f"**{s.name}** — {commitment_health(commitment)}")
                    st.caption(f"Response due {commitment.response_due or '—'}")

# --------------------------
# SCHEDULE ALIGNMENT
# --------------------------
elif page == "Schedule Alignment":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Field / CPM Alignment","Compare what the field plans to do with what the master schedule says should happen.",project.name)
        tabs=st.tabs(["Alignment","Drift Alerts","Schedule Versions","Activity Matching"])

        with tabs[0]:
            if st.button("Run current lookahead alignment",type="primary"):
                rows=align_current_lookahead(db,project.id,6)
                st.success(f"Aligned {len(rows)} lookahead activity record(s).")
                sync_drift_alerts(db,project.id)
                st.rerun()
            from db.models import LookaheadScheduleAlignment
            rows=db.query(LookaheadScheduleAlignment).filter(
                LookaheadScheduleAlignment.project_id==project.id
            ).order_by(LookaheadScheduleAlignment.created_at.desc()).all()
            latest={}
            for r in rows: latest.setdefault(r.schedule_activity_id,r)
            for r in latest.values():
                a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
                icon="✅" if r.alignment_status=="ALIGNED" else "⚠️"
                st.write(f"{icon} **{a.external_id if a else ''} — {a.name if a else r.schedule_activity_id}**")
                st.caption(f"{r.alignment_status} · variance {r.variance_days:+d} days · {r.reason}")

        with tabs[1]:
            from db.models import ScheduleDriftAlert
            rows=db.query(ScheduleDriftAlert).filter(
                ScheduleDriftAlert.project_id==project.id,
                ScheduleDriftAlert.status=="OPEN"
            ).order_by(ScheduleDriftAlert.severity.desc()).all()
            if not rows: st.success("No open field-vs-CPM drift alerts.")
            for r in rows:
                a=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
                st.write(f"**{r.severity} · {a.name if a else r.schedule_activity_id} · {r.variance_days:+d} days**")
                st.caption(r.message)

        with tabs[2]:
            from db.models import ScheduleVersion
            vname=st.text_input("Version name",placeholder="August 2026 Update")
            ddate=st.text_input("Data date",placeholder="YYYY-MM-DD")
            if st.button("Snapshot current schedule") and vname:
                snapshot_current_schedule(db,project.id,vname,ddate,"APP","")
                st.success("Schedule version saved.");st.rerun()
            versions=db.query(ScheduleVersion).filter(
                ScheduleVersion.project_id==project.id
            ).order_by(ScheduleVersion.created_at.desc()).all()
            for v in versions:
                st.write(f"**#{v.id} · {v.version_name}** · data date {v.data_date or '—'}")
            if len(versions)>=2:
                old=st.selectbox("Older version",[v.id for v in reversed(versions)],key="ver_old")
                new=st.selectbox("Newer version",[v.id for v in versions],key="ver_new")
                if st.button("Compare versions"):
                    for row in compare_versions(db,old,new)[:100]:
                        if row["status"]!="UNCHANGED":
                            st.write(f"**{row['status']} · {row['external_id']} · {row['name']}**")
                            for c in row.get("changes",[]):
                                st.caption(f"{c['field']}: {c['old']} → {c['new']}")

        with tabs[3]:
            label=st.text_input("Field activity label",placeholder="Level 2 east corridor overhead rough-in")
            if label:
                suggestions=suggest_matches(db,project.id,label)
                for s in suggestions:
                    st.write(f"**{s['external_id']} — {s['name']}** · match {s['score']:.0%}")
                    if st.button("Save match",key=f"save_match_{s['activity_id']}"):
                        save_match(db,project.id,label,s["activity_id"],s["score"],approved=False)
                        st.success("Match saved for review.")

# --------------------------
# MAKE READY
# --------------------------
elif page == "Make Ready":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Predictive Make-Ready","Clear tomorrow's constraints before they become today's delays.",project.name)
        c1,c2,c3=st.columns(3)
        with c1:
            if st.button("Refresh 6-week make-ready plan",type="primary",use_container_width=True):
                close_resolved_actions(db,project.id)
                created,snap=sync_make_ready_actions(db,project.id,6)
                st.success(f"Make-ready plan refreshed. {len(created)} new action(s).")
                st.rerun()
        with c2:
            if st.button("Prepare overdue follow-ups",use_container_width=True):
                rows=prepare_followups(db,project.id)
                st.success(f"{len(rows)} follow-up message(s) prepared for approval.")
                st.rerun()
        with c3:
            open_count=db.query(MakeReadyAction).filter(
                MakeReadyAction.project_id==project.id,
                MakeReadyAction.status=="OPEN"
            ).count()
            kpi("Open make-ready actions",open_count,"6-week constraint removal")

        rows=db.query(MakeReadyAction).filter(
            MakeReadyAction.project_id==project.id,
            MakeReadyAction.status=="OPEN"
        ).order_by(MakeReadyAction.required_by.asc()).all()

        if not rows:
            st.success("No open make-ready actions. Refresh the plan to evaluate the current lookahead.")

        groups={"CRITICAL":[],"HIGH":[],"NORMAL":[]}
        for r in rows: groups.setdefault(r.priority,[]).append(r)

        for priority in ["CRITICAL","HIGH","NORMAL"]:
            if not groups.get(priority): continue
            st.markdown(f"### {priority}")
            for r in groups[priority]:
                activity=db.query(ScheduleActivity).filter(ScheduleActivity.id==r.schedule_activity_id).first()
                with st.expander(f"{r.required_by or '—'} · {r.gate_name} · {activity.name if activity else r.title}"):
                    st.write(r.reason)
                    st.caption(f"Responsible: {r.responsible_type} · Escalation level {r.escalation_level}")
                    st.write(f"**Action:** {r.title}")

# --------------------------
# OPERATIONS INTELLIGENCE
# --------------------------
elif page == "Operations Intelligence":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Operations Intelligence","Run the weekly make-ready meeting, watch aging constraints, and learn from commitment performance.",project.name)

        tabs=st.tabs(["Weekly Make-Ready","Constraint Aging","Sub Reliability","Lead Times"])
        with tabs[0]:
            rows=weekly_make_ready_rows(db,project.id)
            if not rows:
                st.success("No open make-ready actions.")
            for r in rows:
                st.write(f"**{r['required_by']} · {r['activity_id']} · {r['activity']}**")
                st.caption(f"{r['gate']} · {r['priority']} · Escalation {r['escalation_level']}")
                st.write(r["reason"])
                st.divider()

        with tabs[1]:
            rows=constraint_aging(db,project.id)
            if not rows: st.success("No aging make-ready actions.")
            for r in rows:
                st.write(f"**{r['age_days']} days overdue · {r['gate']} · {r['title']}**")
                st.caption(f"Required by {r['required_by']} · {r['priority']}")

        with tabs[2]:
            st.caption("Reliability is a project-management signal, not a blame score. Upstream constraints still need root-cause review.")
            rows=subcontractor_reliability(db,project.id)
            for r in rows:
                st.write(f"**{r['name']} · {r['trade']} · {r['score']}/100**")
                st.caption(
                    f"Commitments {r['total_commitments']} · confirmed {r['confirmed']} · "
                    f"delay reports {r['delay_reports']} · pending {r['no_response']} · "
                    f"open quality {r['open_quality_items']}"
                )

        with tabs[3]:
            current=lead_times(db,company.id)
            for gate,days in current.items():
                new_days=st.number_input(gate,min_value=0,max_value=120,value=int(days),key=f"lead_{gate}")
                if st.button(f"Save {gate}",key=f"save_lead_{gate}"):
                    set_lead_time(db,company.id,gate,new_days)
                    st.success("Lead time updated.")
                    st.rerun()

# --------------------------
# FORECASTING
# --------------------------
elif page == "Forecasting":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Early-Warning Forecasting","See production drift and downstream exposure before they become official schedule delays.",project.name)
        tabs=st.tabs(["Production","Downstream Exposure","Milestones","CPM Forecast","Manpower"])

        with tabs[0]:
            acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={f"{a.external_id} — {a.name}":a for a in acts}
            if not amap:
                st.info("No schedule activities.")
            else:
                chosen=amap[st.selectbox("Activity",list(amap.keys()),key="prod_activity")]
                subs=project_subcontractors(db,project.id)
                submap={"No sub":None}; submap.update({s.name:s for s in subs})
                sub=submap[st.selectbox("Subcontractor",list(submap.keys()),key="prod_sub")]
                c1,c2,c3=st.columns(3)
                with c1: crew=st.number_input("Crew size",0,500,0)
                with c2: hours=st.number_input("Regular hours",0.0,24.0,8.0)
                with c3: ot=st.number_input("OT hours",0.0,24.0,0.0)
                c4,c5,c6=st.columns(3)
                with c4: qty=st.number_input("Quantity installed",0.0,10000000.0,0.0)
                with c5: planqty=st.number_input("Planned quantity",0.0,10000000.0,0.0)
                with c6: unit=st.text_input("Unit",placeholder="LF / SF / CY / EA")
                note=st.text_input("Production note")
                if st.button("Record production",type="primary"):
                    record_production(db,project.id,chosen.id,subcontractor_id=sub.id if sub else None,
                                      crew_size=crew,regular_hours=hours,overtime_hours=ot,
                                      quantity_installed=qty,quantity_unit=unit,
                                      planned_quantity=planqty,notes=note)
                    st.success("Production recorded."); st.rerun()

                signal=activity_drift_signal(db,project.id,chosen)
                st.markdown(f"### Drift signal: {signal['level']}")
                metrics=signal["metrics"]
                k1,k2,k3=st.columns(3)
                with k1: kpi("Installed",round(metrics["installed"],2),unit or "reported units")
                with k2: kpi("Reported plan",round(metrics["planned"],2),unit or "reported units")
                with k3: kpi("Labor hours",round(metrics["labor_hours"],1),"crew × regular hours")
                for reason in signal["reasons"]: st.warning(reason)

        with tabs[1]:
            actions=db.query(MakeReadyAction).filter(
                MakeReadyAction.project_id==project.id,
                MakeReadyAction.status=="OPEN"
            ).all()
            if not actions: st.success("No open make-ready actions.")
            for action in actions[:30]:
                created=calculate_exposure(db,project.id,action)
                with st.expander(f"{action.priority} · {action.title}"):
                    st.write(action.reason)
                    st.caption(f"Required by {action.required_by}")
                    from db.models import ScheduleExposureRecord
                    exposures=db.query(ScheduleExposureRecord).filter(
                        ScheduleExposureRecord.make_ready_action_id==action.id
                    ).order_by(ScheduleExposureRecord.relationship_depth.asc()).all()
                    if not exposures:
                        st.caption("No downstream relationships found.")
                    for e in exposures[:15]:
                        a=db.query(ScheduleActivity).filter(ScheduleActivity.id==e.exposed_activity_id).first()
                        if a:
                            st.write(f"→ Depth {e.relationship_depth}: **{a.name}** · modeled exposure {e.risk_days} day(s)")

        with tabs[2]:
            rows=milestone_exposure(db,project.id)
            if not rows:
                st.info("No milestone exposure is currently modeled.")
            for r in rows:
                st.write(f"**{r['activity']}**")
                st.caption(f"Modeled exposure: {r['risk_days']} day(s) · relationship depth {r['depth']}")

        with tabs[3]:
            st.caption("Forecast uses schedule relationships plus observed progress/production. It is advisory until tied to the authoritative CPM schedule.")
            if st.button("Run project forecast",type="primary"):
                proj,snaps=forecast_project(db,project.id)
                if proj:
                    st.success("Forecast updated.")
                    st.rerun()
            from db.models import ProjectForecastSnapshot,ActivityForecastSnapshot
            proj=db.query(ProjectForecastSnapshot).filter(
                ProjectForecastSnapshot.project_id==project.id
            ).order_by(ProjectForecastSnapshot.created_at.desc()).first()
            if proj:
                c1,c2,c3,c4=st.columns(4)
                with c1:kpi("Planned completion",proj.planned_completion,"Current imported schedule")
                with c2:kpi("Forecast completion",proj.forecast_completion,"Model projection")
                with c3:kpi("Variance",f"{proj.variance_days} days","Forecast vs planned")
                with c4:kpi("Confidence",f"{proj.milestone_confidence:.0%}","Evidence coverage")
                snaps=db.query(ActivityForecastSnapshot).filter(
                    ActivityForecastSnapshot.project_id==project.id,
                    ActivityForecastSnapshot.data_date==proj.data_date
                ).order_by(ActivityForecastSnapshot.forecast_variance_days.desc()).all()
                st.markdown("### Highest forecast variance")
                for s in snaps[:20]:
                    a=db.query(ScheduleActivity).filter(ScheduleActivity.id==s.schedule_activity_id).first()
                    st.write(f"**{a.name if a else s.schedule_activity_id}** · {s.forecast_variance_days:+d} days")
                    st.caption(f"Forecast finish {s.forecast_finish} · remaining {s.remaining_duration_days} days · confidence {s.confidence:.0%}")

        with tabs[4]:
            data=manpower_by_trade(db,project.id)
            if not data:
                st.info("Record field production to build manpower trends.")
            else:
                st.markdown("### Manpower by trade / day")
                for day,trades in data.items():
                    st.write(f"**{day}** — "+", ".join(f"{trade}: {crew}" for trade,crew in trades.items()))
            st.markdown("### Production trend by activity")
            for r in production_trends(db,project.id):
                pace="—" if r["pace_ratio"] is None else f"{r['pace_ratio']:.0%}"
                prod="—" if r["productivity"] is None else f"{r['productivity']:.2f}"
                st.write(f"**{r['activity']}**")
                st.caption(f"Installed {r['installed']:.1f} / plan {r['planned']:.1f} · pace {pace} · productivity {prod} units/labor-hour")

# --------------------------
# FIELD
# --------------------------
elif page == "Field":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Field Execution", "Capture what changed today and turn blockers into structured project action.", project.name)

        tabs=st.tabs(["Quick Update","Photo","Daily Report"])
        with tabs[0]:
            st.markdown("### Quick field update")
            st.caption("Type or dictate into the text box using your device keyboard's microphone.")
            activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={"General / project-wide":None}
            amap.update({f"{a.external_id} — {a.name}":a for a in activities})
            chosen=amap[st.selectbox("Activity",list(amap.keys()))]
            quick=st.text_area("What changed?",height=120,placeholder="Underground plumbing passed inspection. Electrician is waiting on embeds...")
            normalized=split_quick_update(quick)
            utype=st.selectbox("Update type",["PROGRESS","BLOCKER","SAFETY","QUALITY","INSPECTION","DELIVERY","COORDINATION"],
                               index=["PROGRESS","BLOCKER","SAFETY","QUALITY","INSPECTION","DELIVERY","COORDINATION"].index(normalized["suggested_type"]) if normalized["suggested_type"] in ["PROGRESS","BLOCKER","SAFETY","QUALITY","INSPECTION","DELIVERY","COORDINATION"] else 0)
            pct=st.number_input("Proposed % complete",0.0,100.0,0.0)
            if st.button("Save update",type="primary",use_container_width=True):
                row=record_field_update(
                    db,project.id,normalized["field_update"],user.id,chosen.id if chosen else None,
                    None,utype,pct
                )
                proposal=propose_constraint(
                    db,project.id,normalized["field_update"],schedule_activity_id=chosen.id if chosen else None,
                    source_type="FIELD_UPDATE",source_id=row.id
                )
                st.success("Field update recorded.")
                if proposal:
                    st.warning("Potential blocking constraint detected and sent for review.")

        with tabs[1]:
            st.markdown("### Field photo")
            activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={"General / project-wide":None}
            amap.update({f"{a.external_id} — {a.name}":a for a in activities})
            chosen=amap[st.selectbox("Attach to activity",list(amap.keys()),key="photo_activity")]
            locs=locations(db,project.id)
            locmap={"No location":None}
            locmap.update({f"{x.location_type} · {x.code or '—'} · {x.name}":x for x in locs})
            chosen_loc=locmap[st.selectbox("Location",list(locmap.keys()),key="photo_location")]
            photo=st.camera_input("Take a photo")
            if photo is None:
                photo=st.file_uploader("Or upload a photo",type=["jpg","jpeg","png"],key="field_photo_upload")
            caption=st.text_input("Caption / observation")
            if photo and st.button("Save field photo",use_container_width=True):
                row=save_field_photo(
                    db,project.id,photo.getvalue(),photo.name,caption,
                    schedule_activity_id=chosen.id if chosen else None,
                    observation=caption
                )
                proposals=analyze_photo(db,row,context=caption)
                if chosen_loc:
                    link_photo(db,row.id,chosen_loc.id)
                checks=propose_document_checks(db,row)
                st.success("Photo saved to the project field record.")
                if proposals:
                    st.info("A reviewable field-vision observation was created.")
                if checks:
                    st.info(f"{len(checks)} source-backed field verification check(s) created.")

        with tabs[2]:
            st.markdown("### AI field-vision review")
            proposals=db.query(FieldVisionProposal).filter(
                FieldVisionProposal.project_id==project.id,
                FieldVisionProposal.status=="PENDING_REVIEW"
            ).all()
            if proposals:
                for p in proposals:
                    with st.expander(f"{p.observation_type} — {p.title}"):
                        st.write(p.observation)
                        st.caption(f"Confidence {p.confidence:.0%}")
                        if p.suggested_action:
                            st.write("Suggested action:",p.suggested_action)
                        c1,c2=st.columns(2)
                        with c1:
                            if st.button("Approve observation",key=f"vision_yes_{p.id}"):
                                approve_proposal(db,p.id); st.rerun()
                        with c2:
                            if st.button("Reject",key=f"vision_no_{p.id}"):
                                reject_proposal(db,p.id); st.rerun()
            else:
                st.caption("No pending photo observations.")

            st.markdown("---")
            st.markdown("### Field vs. project documents")
            checks=db.query(FieldVerificationProposal).filter(
                FieldVerificationProposal.project_id==project.id,
                FieldVerificationProposal.status=="PENDING_REVIEW"
            ).all()
            if not checks:
                st.caption("No pending field/document verification checks.")
            for v in checks:
                with st.expander(f"{v.result} — {v.title}"):
                    st.write("**Field observation**")
                    st.write(v.field_observation)
                    if v.requirement_text:
                        st.write("**Candidate project requirement**")
                        st.write(v.requirement_text[:1800])
                        st.caption(f"Source: {v.source_ref or 'project document'} · page {v.source_page or '—'}")
                    st.warning("AI has not established compliance. Human verification is required.")
                    action=st.selectbox("On approval, create",["NO_ACTION","PROGRESS","PUNCH","CONSTRAINT"],key=f"verify_action_{v.id}")
                    if st.button("Approve verification review",key=f"verify_yes_{v.id}"):
                        approve_verification(db,v.id,action)
                        st.rerun()

            st.markdown("---")
            st.markdown("### Open punch / quality items")
            punch=db.query(PunchQualityItem).filter(
                PunchQualityItem.project_id==project.id,
                PunchQualityItem.status=="OPEN"
            ).all()
            for q in punch[:20]:
                st.write(f"**{q.title}**")
                st.caption(q.description[:400])

            st.markdown("---")
            st.markdown("### Site condition / access issue")
            acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={"No specific activity":None}; amap.update({f"{a.external_id} — {a.name}":a for a in acts})
            sc_act=amap[st.selectbox("Affected activity",list(amap.keys()),key="site_cond_act")]
            sc_type=st.selectbox("Condition type",["ACCESS","WEATHER","SITE_LOGISTICS","UTILITIES","HOUSEKEEPING","OTHER"])
            sc_desc=st.text_input("Condition",placeholder="Crane access blocked by material laydown")
            sc_block=st.checkbox("Blocks planned work",key="site_cond_block")
            if st.button("Record site condition") and sc_desc:
                record_site_condition(
                    db,project.id,sc_type,sc_desc,
                    status="BLOCKING" if sc_block else "MONITOR",
                    blocking=sc_block,
                    schedule_activity_id=sc_act.id if sc_act else None,
                    user_id=user.id
                )
                st.success("Site condition recorded.")
                st.rerun()

            st.markdown("---")
            st.markdown("### Daily superintendent report")
            report=get_or_create_daily_report(db,project.id,date.today().isoformat(),user.id)
            weather=st.text_input("Weather",value=report.weather)
            completed=st.text_area("Work completed",value=report.work_completed)
            delays=st.text_area("Delays / impacts",value=report.delays)
            safety=st.text_area("Safety",value=report.safety_notes)
            inspections=st.text_area("Inspections / testing",value=report.inspection_notes)
            tomorrow=st.text_area("Tomorrow plan",value=report.tomorrow_plan)
            if st.button("Save daily report"):
                update_daily_report(
                    db,report,weather=weather,work_completed=completed,delays=delays,
                    safety_notes=safety,inspection_notes=inspections,tomorrow_plan=tomorrow
                )
                st.success("Daily report saved.")

# --------------------------
# QUALITY & INSPECTIONS
# --------------------------
elif page == "Quality & Inspections":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Quality & Inspections","Assign issues, track inspections, and verify corrections before closing them.",project.name)
        qtab,itab=st.tabs(["Punch / Quality","Inspections & Tests"])
        with qtab:
            st.markdown("### Create issue")
            locs=locations(db,project.id)
            locmap={"No location":None}; locmap.update({f"{x.code or '—'} · {x.name}":x for x in locs})
            subs=project_subcontractors(db,project.id)
            submap={"Unassigned":None}; submap.update({s.name:s for s in subs})
            acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={"No activity":None}; amap.update({f"{a.external_id} — {a.name}":a for a in acts})
            title=st.text_input("Issue title")
            desc=st.text_area("Issue description")
            c1,c2,c3=st.columns(3)
            with c1: loc=locmap[st.selectbox("Location",list(locmap.keys()),key="q_loc")]
            with c2: sub=submap[st.selectbox("Responsible subcontractor",list(submap.keys()))]
            with c3: act=amap[st.selectbox("Related activity",list(amap.keys()),key="q_act")]
            priority=st.selectbox("Priority",["LOW","NORMAL","HIGH","CRITICAL"])
            due=st.text_input("Due date",placeholder="YYYY-MM-DD")
            if st.button("Create punch / quality issue",type="primary") and title:
                create_issue(db,project.id,title,desc,"PUNCH",priority,
                             loc.id if loc else None,act.id if act else None,
                             sub.id if sub else None,None,due)
                st.success("Issue created."); st.rerun()

            st.markdown("### Open / verification issues")
            issues=db.query(QualityIssue).filter(QualityIssue.project_id==project.id,QualityIssue.status!="CLOSED").all()
            for q in issues:
                with st.expander(f"{q.priority} · {q.title} · {q.status}"):
                    st.write(q.description)
                    st.caption(f"Due {q.due_date or '—'}")
                    if q.status=="READY_FOR_VERIFICATION":
                        st.warning("Correction photo submitted. Superintendent verification required.")
                        if st.button("Verify and close",key=f"close_q_{q.id}"):
                            close_issue(db,q.id)
                            notify_project_team(
                                db,project.id,
                                f"Quality item closed: {q.title}",
                                "Correction was verified by the project team and the readiness gate was released.",
                                "info"
                            )
                            st.rerun()

        with itab:
            st.markdown("### Schedule inspection / test")
            locs=locations(db,project.id)
            locmap={"No location":None}; locmap.update({f"{x.code or '—'} · {x.name}":x for x in locs})
            acts=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
            amap={"No activity":None}; amap.update({f"{a.external_id} — {a.name}":a for a in acts})
            itype=st.text_input("Inspection / test",placeholder="Footing rebar inspection")
            authority=st.text_input("Authority / inspector",placeholder="Building Department / Special Inspector")
            planned=st.text_input("Planned date",placeholder="YYYY-MM-DD")
            loc=locmap[st.selectbox("Inspection location",list(locmap.keys()),key="i_loc")]
            act=amap[st.selectbox("Related schedule activity",list(amap.keys()),key="i_act")]
            if st.button("Add inspection / test") and itype:
                create_inspection(db,project.id,itype,authority,planned,
                                  loc.id if loc else None,act.id if act else None)
                st.rerun()

            rows=db.query(InspectionTestRecord).filter(InspectionTestRecord.project_id==project.id).all()
            for r in rows:
                with st.expander(f"{r.inspection_type} · {r.result}"):
                    st.caption(f"{r.authority or 'Authority not set'} · Planned {r.planned_date or '—'}")
                    result=st.selectbox("Result",["PENDING","PASSED","FAILED","PARTIAL","CANCELLED"],key=f"ir_{r.id}")
                    actual=st.text_input("Actual date",key=f"ia_{r.id}",placeholder="YYYY-MM-DD")
                    note=st.text_area("Result notes",key=f"in_{r.id}")
                    if st.button("Record result",key=f"is_{r.id}"):
                        record_result(db,r.id,result,actual,note); st.rerun()

# --------------------------
# READINESS
# --------------------------
elif page == "Readiness":
    if not project:
        st.info("Choose a project.")
    else:
        hero("Construction Readiness", "Can we do it? What is stopping us? What is likely to stop us next?", project.name)
        state=load_project_state(db,project.id)
        tasks=load_tasks()
        graph=build_readiness_graph(state,tasks)
        metrics=graph_metrics(graph)

        cols=st.columns(4)
        with cols[0]: kpi("Nodes",metrics["nodes"],"Connected project intelligence")
        with cols[1]: kpi("HOLD",metrics["holds"],"Blocking conditions")
        with cols[2]: kpi("AT RISK",metrics["at_risk"],"Near-term exposure")
        with cols[3]: kpi("Not verified",metrics["not_verified"],"Missing evidence")

        actions=priority_actions(state,tasks)
        st.markdown("### Activity release decisions")
        activities=db.query(ScheduleActivity).filter(ScheduleActivity.project_id==project.id).all()
        if not activities:
            st.info("No persistent schedule activities are loaded.")
        for a in activities[:40]:
            result=evaluate_activity_release(db,project.id,a.id)
            icon="✅" if result["status"]=="READY" else "⛔" if result["status"]=="HOLD" else "⚠️"
            with st.expander(f"{icon} {a.external_id} — {a.name} — {result['status']}"):
                for g in result["gates"]:
                    st.write(f"**{g['gate']} — {g['status']}**")
                    st.caption(g["reason"])

        st.markdown("### Priority blockers and risks")
        for a in actions[:20]:
            st.write(f"**P{a['priority']} — {a['category']} — {a['title']}**")
            st.caption(a["reason"])

# --------------------------
# APPROVALS
# --------------------------
elif page == "Approvals":
    hero("Approval Queue", "Review AI-proposed and workflow-sensitive changes before they affect official project state.", project.name if project else company.name)
    rows=pending_approvals(db,project.id if project else None)
    if not rows:
        st.success("No pending approvals.")
    for r in rows:
        with st.expander(f"{r.approval_type} — {r.title}"):
            st.write(r.summary)
            note=st.text_area("Decision note",key=f"approval_note_{r.id}")
            c1,c2=st.columns(2)
            with c1:
                if st.button("Approve",key=f"approval_yes_{r.id}"):
                    decide_approval(db,r.id,user.id,True,note)
                    st.rerun()
            with c2:
                if st.button("Reject",key=f"approval_no_{r.id}"):
                    decide_approval(db,r.id,user.id,False,note)
                    st.rerun()

# --------------------------
# INBOX
# --------------------------
elif page == "Inbox":
    hero("Inbox", "Critical alerts, warnings, follow-ups, and project communication.", company.name)
    rows=user_inbox(db,company.id,user.id,include_read=True)
    if not rows:
        st.info("Inbox is empty.")
    for r in rows:
        icon="🔴" if r.severity=="critical" else "🟠" if r.severity=="warning" else "🔵"
        st.write(f"{icon} **{r.title}** — {r.status}")
        st.caption(r.message)
        if r.status=="UNREAD" and st.button("Mark read",key=f"mark_read_{r.id}"):
            mark_read(db,r.id)
            st.rerun()

db.close()
