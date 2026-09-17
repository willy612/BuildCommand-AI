"""8.14.0: task-based navigation over the existing construction tools.

One Ask service, explicit project handoffs, and no automatic external actions.
"""
from functools import wraps
from fastapi import Form
from fastapi.responses import RedirectResponse
from blueprint_field import esc

VERSION='8.14.0'
ESTIMATING_MAIN=(('Estimator Intelligence','/brain/estimator'),('Takeoff review','/brain/takeoff'),
                 ('Bid packages','/preconstruction/packages'),('Compare bids','/preconstruction/leveling'))
ESTIMATING_MORE=(('Preconstruction review','/preconstruction'),('Estimate overview','/estimate'),
                 ('Takeoff components','/brain/takeoff/components'),('Historical costs','/learning/costs'))
# Every older navigation item is either categorized here or intentionally hidden
# as a duplicate/settings/test entry. Its original handler is retained.
GROUPS=(
 ('Drawings & documents',(('Drawings','/workspace/drawings'),('Project Documents','/workspace/documents'),('RFIs / issues','/issues'),('Submittals','/submittals')),
  (('Earlier document uploads','/documents'),('Change events','/changes'),('Change packages','/change-package'))),
 ('Estimating & bidding',ESTIMATING_MAIN,ESTIMATING_MORE),
 ('Daily field work',(('Daily report','/daily-report'),('Punch list','/punch'),('Safety & inspections','/workspace/checklists')),
  (('Earlier inspections','/inspections'),('Earlier safety records','/safety'),('Quick field note','/quick-entry'),('Field log','/field'),('Production','/production'),('Reports & exports','/exports'),('PDF reports','/pdf-reports'))),
 ('Schedule & planning',(('Schedule','/schedule'),('Advanced Schedule Import','/advanced-schedule-import'),('3-week look-ahead','/lookahead-intelligence'),('Procurement','/procurement')),
  (('Project startup','/project-startup'),('Readiness','/readiness'),('Make ready','/make-ready'),('Recovery planning','/recovery'),('Project settings','/project-settings'))),
 ('People & coordination',(('Subcontractor directory','/workspace/directory'),('Trade sharing','/workspace/sharing'),('Meetings','/meetings'),('Portfolio','/workspace/portfolio')),
  (('Subcontractor communications','/sub-communications'),('Operating playbooks','/playbooks'),('Notifications','/notifications'))),
 ('BuildCommand AI',(('Ask, analyze & review','/workspace/brain'),),()),
)
AI_TOOLS=(('Plan scope & trade analysis','/blueprint-brain'),('Site photo analysis','/photo-ai'),('Document questions','/document-ai'),
 ('Project analysis','/ai-analysis'),('Project health','/project-health'),('Schedule health','/schedule-health'),
 ('Forecast','/predictive-forecast'),('Recorded photo insights','/photo-intelligence'),('Risk analysis','/risk'),
 ('Subcontractor scorecards','/sub-scorecards'),('Subcontractor risk','/sub-risk'),('RFI impact','/rfi-impact'),
 ('Procurement warnings','/procurement-warning'),('Recovery options','/ai-recovery'),('Draft an RFI','/rfi-drafting'),
 ('Meeting minutes','/meeting-minutes-ai'),('Weather impacts','/weather-impacts'),('Cost analysis','/cost-intelligence'),
 ('Weekly report','/weekly-report'),('Automatic daily draft','/auto-daily-report'),('Earlier morning brief','/morning-brief'),('Company memory','/memory'))
HIDDEN={'/','/ai-command','/assistant','/plans-specs-ai','/schedule-import','/actions','/portfolio','/portfolio-intelligence',
 '/mobile-home','/mobile-field-plus','/owner-dashboard','/team','/company-settings','/system-check','/beta-feedback','/setup','/invitations','/production-settings','/beta-checklist'}
CSS='''<style>.hub-section{margin:24px 0}.hub-actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.hub-actions form{margin:0}.hub-actions button{min-height:46px}.hub-summary{padding:16px 0;border-top:1px solid #dfe6ee}.hub-summary h3{margin:0 0 8px}.hub-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));gap:18px}.hub-section summary{cursor:pointer;font-weight:700;padding:12px 0}.hub-table{width:100%;border-collapse:collapse}.hub-table th,.hub-table td{text-align:left;padding:12px;border-bottom:1px solid #dfe6ee}.hub-count{font-size:26px;font-weight:700}.hub-select label{font-weight:700}.hub-select select{padding:12px;max-width:100%}.hub-back{display:inline-flex;min-height:44px;align-items:center;margin-bottom:16px}.hub-help{color:#596a7c;line-height:1.6}@media(max-width:700px){.hub-actions>*{width:100%}.hub-actions button{width:100%}.hub-table{font-size:14px}}</style>'''


def install(ns):
    hub=WorkspaceHub(ns);ns['app'].state.workspace_hub=hub;hub.register();return hub


class WorkspaceHub:
    def __init__(self,ns):
        self.ns=ns;self.field=ns['app'].state.blueprint_field;self.db=self.field.db;self.require=self.field.require
        self.routes=[]
        self.legacy_actions=next((r.endpoint for r in ns['app'].routes if getattr(r,'path','')=='/actions' and 'GET' in (r.methods or set())),None)
        self.estimating_handlers={(r.path,m):r.endpoint for r in ns['app'].routes if hasattr(r,'methods')
            for m in (r.methods or set()) if r.path.startswith(('/brain/estimator','/brain/takeoff','/preconstruction')) or r.path in {'/estimate','/learning/costs'}}

    def endpoint(self,fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not self.ns['_bc840_user']():return RedirectResponse('/login',303)
            try:return fn(*args,**kwargs)
            except self.ns['_BC850_Problem'] as exc:return self.ns['_bc830b_error'](exc.message,exc.status)
        return wrapped

    def user(self,c,pid=0,manager=False):
        user=self.ns['_bc850_actor'](c)
        self.require(self.ns['_bc840_tier'](user) not in {'trade','observer'},'Open My shared work for the files and work assigned to you.')
        if manager:self.require(self.ns['_bc850_manager'](user),'Your project leader manages this area.')
        if self.ns['_bc840_tier'](user) in {'owner','admin'}:
            projects=self.ns['_bc840_projects'](user)
        else:
            table=self.ns['_bc850_member_table']()
            projects=[dict(r) for r in c.execute(f'SELECT DISTINCT p.id,p.name,p.number,p.status FROM projects p JOIN {table} m ON m.project_id=p.id WHERE p.company_id=? AND m.user_id=? ORDER BY p.name,p.id',(user['company_id'],user['id'])).fetchall()]
        selected=pid or self.ns['_bc840_selected_project'](user)
        if pid:self.require(any(p['id']==pid for p in projects),'This project is not assigned to you.')
        project=next((p for p in projects if p['id']==selected),None)
        return user,project,projects

    def page(self,title,body):return self.ns['_bc840_page'](title,CSS+body)

    def selector(self,projects,project,action):
        return '<form class="hub-select" method="get" action="'+action+'"><label for="hub-project">Project</label> <select id="hub-project" name="project_id"><option value="">Choose a project</option>'+''.join(f'<option value="{p["id"]}"'+(' selected' if project and p['id']==project['id'] else '')+'>'+esc(p['name'])+'</option>' for p in projects)+'</select> <button>Open</button></form>'

    def open_form(self,pid,path,label):
        return f'<form method="post" action="/workspace/tools/projects/{pid}/open"><input type="hidden" name="tool" value="{esc(path)}"><button>{esc(label)}</button></form>'

    def destinations(self):
        items={path for _,main,more in GROUPS for _,path in main+more}|{path for _,path in AI_TOOLS}
        return items|{'/workspace/command','/workspace/scopes','/workspace/rfi-answers','/actions','/workspace/daily','/workspace/setup'}

    def tools(self):
        with self.db() as c:user,project,projects=self.user(c)
        if not project:return self.page('Field tools','<div class="hero"><h1>Choose a project first</h1><p>Open the job you want to work on.</p></div>'+self.ns['_bc840_project_cards'](projects))
        pid=project['id'];paths={r.path for r in self.ns['app'].routes if hasattr(r,'path')}
        body='<div class="hero"><h1>Field tools</h1><p>'+esc(project['name'])+' · Choose the work you need to do.</p></div><div class="hub-grid">'
        for title,main,more in GROUPS:
            items=[(label,path) for label,path in main if path in paths and (self.ns['_bc850_manager'](user) or path not in {'/workspace/sharing','/workspace/portfolio','/workspace/brain','/workspace/directory'})]
            if not items:continue
            body+='<section class="card hub-section"><h2>'+esc(title)+'</h2><div class="bc840-list">'+''.join(self.open_form(pid,path,label) for label,path in items)+'</div>'
            advanced=[(label,path) for label,path in more if path in paths]
            if advanced:body+='<details><summary>More '+esc(title.lower())+'</summary><div class="bc840-list">'+''.join(self.open_form(pid,path,label) for label,path in advanced)+'</div></details>'
            body+='</section>'
        return self.page('Field tools',body+'</div>')

    def open_tool(self,project_id:int,tool:str=Form(...)):
        self.require(tool in self.destinations(),'Choose a listed tool.',400)
        with self.db(True) as c:
            user,project,_=self.user(c,project_id)
            if tool in {'/workspace/command','/workspace/brain','/workspace/portfolio','/workspace/sharing','/workspace/directory','/workspace/scopes','/workspace/rfi-answers'}:
                self.field.actor(c,project_id,True)
            c.execute('INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id',(user['id'],project_id))
        suffix='?project_id='+str(project_id) if tool.startswith('/workspace/') and tool not in {'/workspace/portfolio','/workspace/setup'} else ''
        return RedirectResponse(tool+suffix,303)

    def quick_panel(self,user,projects):
        if not self.ns['_bc850_manager'](user):return ''
        with self.db() as c:user,project,projects=self.user(c,manager=True)
        pid=project['id'] if project else None
        body=CSS+'<section id="quick-actions" class="card hub-section"><h2>Quick Actions &amp; Follow-ups</h2>'
        if not project:return body+'<p>Choose a project below to see its work and next actions.</p></section>'
        body+='<p>'+esc(project['name'])+'</p><div class="hub-actions">'
        for label,path in [('Run today','/workspace/command'),('Drawings','/workspace/drawings'),('Estimating','/brain/estimator'),('Daily report','/workspace/daily'),('Safety & inspections','/workspace/checklists'),('Documents','/workspace/documents'),('Ask / analyze','/workspace/brain')]:body+=self.open_form(pid,path,label)
        body+='</div><h3>Work to follow up</h3>'
        with self.db() as c:
            self.field.actor(c,pid)
            tables=self.ns['_bc800_table_names']()
            rows=[dict(r) for r in c.execute("SELECT id,title,priority,due,owner FROM action_items WHERE project_id=? AND UPPER(COALESCE(status,''))='OPEN' ORDER BY CASE UPPER(priority) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,due,id LIMIT 8",(pid,)).fetchall()] if 'action_items' in tables else []
            for r in rows:
                body+='<article class="hub-summary"><h3>'+esc(r['title'])+'</h3><p>'+esc(r.get('owner') or 'Owner not recorded')+' · '+esc(r.get('priority') or 'Priority not set')+' · '+esc(r.get('due') or 'No due date')+f'</p><form method="post" action="/workspace/actions/{r["id"]}/complete"><input type="hidden" name="project_id" value="{pid}"><button>Mark complete</button></form></article>'
            actions=getattr(self.ns['app'].state,'command_actions',None)
            if actions:body+=actions.panel(c,user,project)
        if not rows:body+='<p>No open action items are recorded for this project.</p>'
        body+='<details><summary>All action records and history</summary>'+self.open_form(pid,'/actions','Manage action records')+'</details></section>'
        return body

    def complete_action(self,action_id:int,project_id:int=Form(...)):
        with self.db(True) as c:
            user,_=self.field.actor(c,project_id,True)
            row=c.execute('SELECT id,status FROM action_items WHERE id=? AND project_id=?',(action_id,project_id)).fetchone()
            self.require(row is not None,'This action is unavailable in this project.',404)
            self.require(str(row['status']).upper() in {'OPEN','COMPLETE'},'Reload this action before changing it.',409)
            if str(row['status']).upper()!='COMPLETE':
                c.execute("UPDATE action_items SET status='COMPLETE' WHERE id=? AND project_id=?",(action_id,project_id))
                self.ns['_bc850_event'](c,user,project_id,None,'ACTION_COMPLETED:'+str(action_id))
        return RedirectResponse('/workspace#quick-actions',303)

    def brain(self,project_id:int=0):
        with self.db() as c:user,project,projects=self.user(c,project_id,True)
        body='<div class="hero"><h1>BuildCommand AI</h1><p>Ask about the job, analyze new information, and review the next step.</p></div>'+self.selector(projects,project,'/workspace/brain')
        if not project:return self.page('BuildCommand AI',body+'<p>Choose your project to begin.</p>')
        pid=project['id'];center=self.ns['app'].state.command_center
        body+='<section class="card hub-section"><h2>Ask BuildCommand</h2><p>What do you need to handle today?</p>'+center.ask_form(pid)+'</section>'
        body+='<section class="card hub-section"><h2>Analyze something</h2><p>Choose what you are reviewing. BuildCommand opens the existing analysis workflow for this project.</p><div class="hub-actions">'
        for label,path in AI_TOOLS[:4]:body+=self.open_form(pid,path,label)
        body+='</div><details><summary>More analysis and saved insights</summary><div class="hub-grid">'
        paths={r.path for r in self.ns['app'].routes if hasattr(r,'path')}
        for label,path in AI_TOOLS[4:]:
            if path in paths:body+=self.open_form(pid,path,label)
        body+='</div>'+self.field.tool_form(pid,'analysis','Full project analysis')+'</details></section><section class="card hub-section"><h2>Review before issuing</h2><div class="hub-actions">'
        for label,path in [('Trade scopes','/workspace/scopes'),('Estimator Intelligence','/brain/estimator'),('RFI answers','/workspace/rfi-answers'),('Command & reviewed actions','/workspace/command')]:body+=self.open_form(pid,path,label)
        body+='</div><p class="hub-help">Ask uses saved project evidence. Analyzing and asking do not automatically issue instructions or send messages.</p></section>'
        return self.page('BuildCommand AI',body)

    def extra_evidence(self,c,user,pid):
        self.field.actor(c,pid)
        tables=self.ns['_bc800_table_names']();rows=[]
        if {'document_ai_chunks','attachments'}.issubset(tables):
            for r in c.execute('SELECT d.id,d.attachment_id,d.text_content,a.title FROM document_ai_chunks d JOIN attachments a ON a.id=d.attachment_id AND a.project_id=d.project_id AND a.company_id=d.company_id WHERE d.company_id=? AND d.project_id=? ORDER BY d.id DESC LIMIT 8',(user['company_id'],pid)).fetchall():rows.append(('Saved document excerpt',r['id'],r['title'],r['text_content'],f'/documents/{r["attachment_id"]}/view'))
        if 'meeting_ai_summaries' in tables:
            for r in c.execute('SELECT id,summary_text,created FROM meeting_ai_summaries WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 3',(user['company_id'],pid)).fetchall():rows.append(('Saved meeting summary',r['id'],'Meeting · '+str(r['created'] or ''),r['summary_text'],'/meeting-minutes-ai'))
        if 'document_markup_revisions' in tables:
            for r in c.execute('SELECT m.id,m.attachment_id,m.revision_title,m.notes FROM document_markup_revisions m JOIN attachments a ON a.id=m.attachment_id AND a.project_id=m.project_id AND a.company_id=m.company_id WHERE m.company_id=? AND m.project_id=? ORDER BY m.id DESC LIMIT 5',(user['company_id'],pid)).fetchall():rows.append(('Drawing markup note',r['id'],r['revision_title'],r['notes'],f'/workspace/drawings/{r["attachment_id"]}'))
        documents=getattr(self.ns['app'].state,'project_documents',None)
        if documents:rows.extend(documents.evidence(c,user,pid))
        return rows

    def portfolio(self):
        with self.db() as c:
            user,_,projects=self.user(c,manager=True);tables=self.ns['_bc800_table_names']();cards=[];healths=[]
            for p in projects:
                pid=p['id'];snapshot=None
                if callable(getattr(self.ns['_runtime'],'project_health_snapshot',None)):
                    try:snapshot=self.ns['_runtime'].project_health_snapshot(pid)
                    except Exception:snapshot=None
                if snapshot and isinstance(snapshot.get('overall'),(int,float)):healths.append(snapshot['overall'])
                risks=[dict(r) for r in c.execute('SELECT band FROM risks WHERE project_id=?',(pid,)).fetchall()] if 'risks' in tables else []
                ready=c.execute("SELECT count(*) AS n FROM make_ready WHERE project_id=? AND status='OPEN'",(pid,)).fetchone()['n'] if 'make_ready' in tables else 0
                critical=sum(r['band']=='CRITICAL' for r in risks);high=sum(r['band']=='HIGH' for r in risks);watch=sum(r['band']=='WATCH' for r in risks)
                attention=min(100,critical*30+high*15+watch*5+ready*5)
                activities=c.execute('SELECT count(*) AS n FROM activities WHERE project_id=?',(pid,)).fetchone()['n'] if 'activities' in tables else 0
                active=c.execute("SELECT count(*) AS n FROM activities WHERE project_id=? AND status='IN_PROGRESS'",(pid,)).fetchone()['n'] if 'activities' in tables else None
                latest=c.execute('SELECT report_date,manpower,delays FROM daily_reports WHERE project_id=? ORDER BY report_date DESC,id DESC LIMIT 1',(pid,)).fetchone() if 'daily_reports' in tables else None
                card='<article class="card"><div class="eyebrow">'+esc(p.get('number') or 'PROJECT')+'</div><h2>'+esc(p['name'])+'</h2><p>'+esc(str(p.get('status') or '').replace('_',' ').title())+'</p>'
                if snapshot:card+='<p><strong>Project health: '+esc(snapshot.get('overall'))+'/100</strong></p><p>Schedule '+esc(snapshot.get('schedule'))+' · Readiness '+esc(snapshot.get('readiness'))+' · Procurement '+esc(snapshot.get('procurement'))+'</p>'
                else:card+='<p>Project health is not available yet.</p>'
                card+='<p><strong>Attention: '+str(attention)+'/100</strong> · '+str(critical)+' critical risks · '+str(high)+' high risks · '+str(ready)+' open readiness items · '+str(watch)+' watch risks</p><p>'+str(activities)+' schedule activities'+(' · '+str(active)+' in progress' if active is not None else '')+'</p>'
                card+=('<p>Latest daily report: '+esc(latest['report_date'])+' · '+str(latest['manpower'] or 0)+' people</p><p>'+esc(latest['delays'] or 'No delay noted')+'</p>') if latest else '<p>No daily report saved yet.</p>'
                card+=self.open_form(pid,'/workspace/command','Open this project')+'</article>';cards.append((attention,card))
        body='<div class="hero"><h1>Portfolio</h1><p>Project status, field reports and project health together. Showing '+str(len(projects))+' projects you can access.</p></div>'
        if healths:body+='<p>Average recorded project health: <strong>'+str(round(sum(healths)/len(healths)))+'/100</strong> across '+str(len(healths))+' available scores.</p>'
        body+='<p class="hub-help">Higher health scores are better. Higher attention scores mean more recorded risk and readiness issues.</p><div class="hub-grid">'+(''.join(card for _,card in sorted(cards,key=lambda x:x[0],reverse=True)) or '<p>No projects assigned yet.</p>')+'</div>'
        return self.page('Portfolio',body)

    def brain_alias(self):return RedirectResponse('/workspace/brain',303)
    def portfolio_alias(self):return RedirectResponse('/workspace/portfolio',303)

    def estimating_health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes if hasattr(r,'methods') for m in (r.methods or set())}
        checks={'GET '+path:(path,'GET') in active for _,path in ESTIMATING_MAIN+ESTIMATING_MORE}
        checks.update(
            estimating_category_configured=any(title=='Estimating & bidding' and main==ESTIMATING_MAIN and more==ESTIMATING_MORE for title,main,more in GROUPS),
            project_handoff_configured=all(path in self.destinations() for _,path in ESTIMATING_MAIN+ESTIMATING_MORE),
            original_estimating_handlers_preserved=bool(self.estimating_handlers) and all(active.get(key) is fn for key,fn in self.estimating_handlers.items()),
            workspace_handlers_active=all(active.get((path,method)) is fn for method,path,fn in self.routes),
            estimator_schema_repair_installed=bool(getattr(self.ns['app'].state,'blueprint_batches',None) and self.ns['app'].state.blueprint_batches.estimator_ready),
            drawings_preserved=('/workspace/drawing-sheets/{sheet_id}','GET') in active,
            form_origin_guard_preserved=callable(self.ns.get('_bc861_same_origin')) and self.ns.get('_bc840_same_origin') is self.ns.get('_bc861_same_origin') and self.ns.get('_BC862_FORM_REFERRER_POLICY')=='same-origin')
        try:
            with self.db() as c:c.execute('SELECT id,company_id,project_id,quantity,material_unit_cost,labor_unit_cost,notes,verified FROM estimator_items WHERE 1=0')
            checks['estimator_schema_readable']=True
        except Exception:checks['estimator_schema_readable']=False
        return dict(app='BuildCommand AI',version='8.26.4',release='Estimating Navigation Recovery',
            status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
            scope='Route, navigation configuration and schema checks only. Verify the selected project, saved estimate values and role access on staging. No provider request, bid issue or email is performed.')

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes if hasattr(r,'methods') for m in (r.methods or set())}
        checks={method+' '+path:active.get((path,method)) is fn for method,path,fn in self.routes}
        checks.update(workspace_hub_installed=getattr(self.ns['app'].state,'workspace_hub',None) is self,
                      drawings_workspace_installed=getattr(self.ns['app'].state,'drawing_workspace',None) is not None,
                      daily_reports_preserved=getattr(self.ns['app'].state,'daily_reports',None) is not None,
                      reviewed_actions_preserved=getattr(self.ns['app'].state,'command_actions',None) is not None,
                      shared_pdf_view_preserved=('/workspace/shared/{share_id}/view','GET') in active)
        checks['ask_additional_sources_connected']=callable(getattr(self.ns['app'].state.command_center,'additional_evidence',None))
        try:
            with self.db() as c:
                c.execute('SELECT id,company_id,project_id,attachment_id,revision_no,annotation_json FROM document_markup_revisions WHERE 1=0')
                c.execute('SELECT id,company_id,project_id,original_name,stored_name,mime_type FROM attachments WHERE 1=0')
            checks['drawing_schema_readable']=True
        except Exception:checks['drawing_schema_readable']=False
        return dict(app='BuildCommand AI',version=VERSION,release='Simple Workspace & Drawings',status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Active routes and installation checks. Verify uploads, saved markups, project access, real browser drawing tools and the 30-second first-user walkthrough on staging.')

    def register(self):
        for path,method,fn in [('/workspace/tools','GET',self.tools),('/workspace/tools/projects/{project_id}/open','POST',self.open_tool),('/workspace/actions/{action_id}/complete','POST',self.complete_action),('/workspace/brain','GET',self.brain),('/workspace/portfolio','GET',self.portfolio)]:
            wrapped=self.endpoint(fn);self.ns['_bc840_replace'](path,method,wrapped);self.routes.append((method,path,wrapped))
        for path,fn in [('/portfolio',self.portfolio_alias),('/portfolio-intelligence',self.portfolio_alias),('/ai-command',self.brain_alias),('/assistant',self.brain_alias)]:self.ns['_bc840_replace'](path,'GET',self.endpoint(fn))
        path='/health/simple-drawings-8-14-0';self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
        path='/health/estimating-navigation-8-26-4';self.ns['app'].add_api_route(path,self.estimating_health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
