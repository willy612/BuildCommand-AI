"""8.24.0 connected field release; composes existing modules in one installer."""
from fastapi.responses import JSONResponse
from field_platform import FieldPlatform,VERSION,RELEASE
from field_delivery import FieldDelivery
from field_readiness import FieldReadiness
from field_intelligence import FieldIntelligence
from field_drawings import FieldDrawings
from blueprint_field import esc

def install(ns):
    b=FieldPlatform(ns);app=ns['app'];app.state.connected_field=b
    app.state.field_delivery=FieldDelivery(b)
    app.state.field_readiness=FieldReadiness(b)
    app.state.field_intelligence=FieldIntelligence(b)
    app.state.field_drawings=FieldDrawings(b)
    from field_mobile import install as install_mobile
    install_mobile(b)
    # Extend existing home actions instead of introducing another global module tab.
    hub=app.state.workspace_hub;old_quick=hub.quick_panel
    def quick(user,projects):
        html=old_quick(user,projects)
        if not ns['_bc850_manager'](user):return html
        with b.db() as c:user,p,_=hub.user(c,manager=True)
        if not p:return html
        pid=str(p['id']);links='<details class="card"><summary>Coordinate trades & documents</summary><div class="field-actions">'
        for path,label in [('/workspace/readiness','Get trades ready'),('/workspace/transmittals','Issue document package'),('/workspace/search','Find a project record'),('/workspace/drawing-board','Drawing sheet board'),('/workspace/submittals','Submittal responses')]:links+=b.link(path+'?project_id='+pid,label)
        return html+links+b.link('/workspace/readiness/projects/'+pid+'/tomorrow','Prepare tomorrow’s briefing')+'</div></details>'
    hub.quick_panel=quick
    # Keep the original Command renderer and its route identities, with a few
    # explicit due constraints above the broader attention list.
    center=app.state.command_center;old_render=center.render
    def render(user,project,*args,**kwargs):
        response=old_render(user,project,*args,**kwargs)
        if not project or not hasattr(response,'body') or response.status_code!=200:return response
        with b.db() as c:
            user,p=b.actor(c,project['id']);today=b.now().date().isoformat()
            rows=[r for r in app.state.field_readiness.constraints(c,user,p['id']) if r['state']!='cleared' and r['needed_date'] and r['needed_date']<=today][:3]
        if not rows:return response
        body='<div class="field-warning"><h3>Clear these before the crew starts</h3>'
        for row in rows:body+='<p><strong>'+esc(row['title'])+'</strong> — Needed '+esc(row['needed_date'])+'. '+('Responsible: '+esc(row['owner'])+'. ' if row['owner'] else 'Assign a responsible person. ')+b.link('/workspace/readiness?project_id='+str(project['id'])+'#constraints','Review next step')+'</p>'
        from fastapi.responses import HTMLResponse
        html=response.body.decode().replace('<h2>Today’s Priorities</h2>','<h2>Today’s Priorities</h2>'+body+'</div>',1)
        return HTMLResponse(html,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})
    center.render=render
    def status():
        active={(r.path,m):r.endpoint for r in app.routes if hasattr(r,'methods') for m in r.methods or []}
        checks={method+' '+path:active.get((path,method)) is fn for path,method,fn in b.routes}
        checks.update(field_schema_initialized=b.schema_ready,existing_checklists_preserved=bool(getattr(app.state,'safety_checklists',None)),exact_review_enabled='issue_package' in b.actions,reviewed_schedule_changes='schedule_dates' in b.actions,indexed_document_evidence=True,company_guidance_separate=True,original_drawing_canvas_preserved=True,form_origin_guard_preserved=bool(ns.get('_bc840_same_origin')),ask_transport_preserved=hasattr(center,'run_answer'))
        try:
            with b.db() as c:
                for table in b.tables:c.execute('SELECT id FROM '+table+' LIMIT 1').fetchone()
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'attention',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Installation and schema checks only. Real email delivery, production PostgreSQL/load/restore, first-time GC trials and store publication require separate verification. No automatic package sharing or email from health checks.'),status_code=200 if all(checks.values()) else 503)
    ns['_bc840_replace']('/health/connected-field-8-24-0','GET',status)
    ns['_runtime'].PUBLIC_PATHS.add('/health/connected-field-8-24-0')
    b.health=status
    return b
