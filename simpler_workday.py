"""BuildCommand AI 8.28.0 — A Simpler Workday.
Presentation/navigation layer over the installed, authorized 8.27.1 services.
No schema changes, new roles, background jobs, automatic approvals or deliveries.
"""
import logging
import re
from contextvars import ContextVar
from html import escape
from urllib.parse import parse_qs, urlencode
from fastapi import Form
from fastapi.responses import RedirectResponse, JSONResponse

VERSION = '8.28.0'
RELEASE = 'A Simpler Workday'
_request = ContextVar('bc828_request', default=('', {}))
log = logging.getLogger('buildcommand.simpler_workday')

# (Label, path, requires an appointed project manager). Existing handlers enforce
# their own permissions too; navigation never grants access.
AREAS = {
 'estimating': ('Estimating', 'From drawings to a reviewed bid.', (
    ('Review plans', '/blueprint-brain', True), ('Estimator Intelligence', '/brain/estimator', False),
    ('Quantities & takeoff', '/brain/takeoff', False), ('Trade scopes', '/workspace/scopes', True),
    ('Bid packages', '/preconstruction/packages', False), ('Compare bids', '/preconstruction/leveling', False)), (
    ('Preconstruction review', '/preconstruction', False), ('Estimate overview', '/estimate', False),
    ('Takeoff components', '/brain/takeoff/components', False), ('Historical costs', '/learning/costs', False))),
 'field': ('Field work', 'Keep today’s work moving.', (
    ('Daily report', '/workspace/daily', True), ('Schedule', '/workspace/schedule', True),
    ('Safety & inspections', '/workspace/checklists', True), ('Site photos', '/photo-ai', True),
    ('Punch list', '/punch', False), ('Project startup', '/project-startup', True)), (
    ('Earlier schedule tools', '/schedule', False), ('Advanced schedule import', '/advanced-schedule-import', False),
    ('Procurement', '/procurement', False), ('Make ready', '/make-ready', False),
    ('Readiness', '/readiness', False), ('Recovery planning', '/recovery', False),
    ('Field log', '/field', False), ('Quick field note', '/quick-entry', False),
    ('Earlier inspections', '/inspections', False), ('Earlier safety records', '/safety', False),
    ('Production', '/production', False), ('Reports & exports', '/exports', False), ('PDF reports', '/pdf-reports', False))),
 'records': ('Project records', 'Find the answer, document, or decision you need.', (
    ('RFIs & issues', '/issues', False), ('Submittals', '/submittals', False),
    ('Project documents', '/workspace/documents', True), ('RFI answers to field', '/workspace/rfi-answers', True),
    ('Closeout & handover', '/workspace/closeout', True), ('Document requests', '/workspace/document-requests', True)), (
    ('Earlier document uploads', '/documents', False), ('Change events', '/changes', False),
    ('Change packages', '/change-package', False), ('Meetings', '/meetings', False))),
 'team': ('Team', 'The people on this job and the work shared with them.', (
    ('Subcontractor directory', '/workspace/directory', True), ('Shared work', '/workspace/sharing', True)), (
    ('Subcontractor communications', '/sub-communications', True), ('Operating playbooks', '/playbooks', False),
    ('Portfolio', '/workspace/portfolio', True), ('Project settings', '/project-settings', True))),
}
NAV = (('My workspace', '/workspace'), ('Drawings', '/workspace/drawings'),
       ('Estimating', '/workspace/estimating'), ('Field work', '/workspace/field'),
       ('Project records', '/workspace/records'), ('Team', '/workspace/team'))
CSS = '''<style id="bc828-styles">
.bc840-main{max-width:1440px;margin:0 auto;width:100%}.bc840-header{gap:18px;align-items:center;flex-wrap:wrap}
.sw{--sw-ink:#182d46;--sw-muted:#536579;--sw-line:#dce4ed;color:var(--sw-ink);max-width:1140px;margin:auto}
.sw h1{font-size:clamp(27px,3vw,38px);line-height:1.16;letter-spacing:-.035em;margin:8px 0 12px;overflow-wrap:anywhere}
.sw h2{font-size:21px;line-height:1.3;margin:0 0 8px}.sw h3{font-size:17px;margin:0 0 5px;line-height:1.4}
.sw p{color:var(--sw-muted);line-height:1.55}.sw-kicker{font-size:12px;letter-spacing:.1em;font-weight:750;text-transform:uppercase;color:#785d23}
.sw-job{display:flex;gap:18px;justify-content:space-between;align-items:flex-start;margin:12px 0 26px}.sw-job p{margin:0}
.sw-pill{border:1px solid var(--sw-line);border-radius:30px;padding:6px 12px;font-size:12px;white-space:nowrap;background:white}
.sw-columns{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:22px;align-items:start}
.sw-card{background:#fff;border:1px solid var(--sw-line);border-radius:16px;padding:24px;margin-bottom:22px;box-shadow:0 3px 16px #13283d05;min-width:0}
.sw-card-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}.sw-card-heading h2{margin:0}
.sw-priority{display:flex;align-items:flex-start;gap:14px;padding:18px 0;border-top:1px solid #e6ecf2}.sw-priority p{margin:4px 0;font-size:14px}
.sw-number{flex:none;width:28px;height:28px;border-radius:9px;background:#f2f5f9;display:grid;place-items:center;font-size:13px;font-weight:750}
.sw-priority a{color:#173d67;text-decoration:none}.sw-priority a:hover{text-decoration:underline}.sw-meta{font-size:12px!important;color:#5b6d80!important}
.sw-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.sw-actions form,.sw-link-grid form{margin:0;display:flex}
.sw-actions a,.sw-actions button,.sw-link-grid a,.sw-link-grid button{width:100%;display:flex;align-items:center;justify-content:flex-start;text-align:left;min-height:54px;padding:14px;border-radius:10px;border:1px solid #ccd8e5;background:#f8fafc;color:#183b61;text-decoration:none;font-weight:650;line-height:1.3}
.sw .sw-primary{background:#173e68;color:white;border-color:#173e68}.sw-actions a:hover,.sw-actions button:hover,.sw-link-grid button:hover{border-color:#557694;background:#edf3f9;color:#173e68}
.sw-more{margin:18px 0 0}.sw summary{cursor:pointer;font-weight:650;min-height:44px;padding:10px 0}.sw-more>div{margin-top:12px}
.sw #command-question{min-height:100px;width:100%;margin:8px 0}.sw .examples{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.sw .examples button{font-size:12px;min-height:40px;padding:8px 10px;background:#f4f7fa;color:#35516c;border:1px solid #d3dfe9}
.sw .muted,.sw-help{font-size:13px;color:#5b6d80}.sw button{min-height:44px}.sw .sw-quiet-link{display:inline-flex;align-items:center;min-height:44px;font-size:14px}
.sw .sw-empty{border:1px dashed #cdd9e5;border-radius:12px;padding:20px;background:#fafcfe}.sw .sw-empty p{margin:0 0 12px}
.sw-link-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.sw-projects{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.sw-project{border:1px solid var(--sw-line);border-radius:12px;padding:18px;background:#fff}.sw-project p{font-size:13px;margin:8px 0}.sw-project button{margin-top:8px}
.sw-status{padding:12px 16px;margin:12px 0;border-radius:10px;border:1px solid #b5cfc4;background:#eff8f2;color:#254f3c}
.sw-alert{border-color:#d9c9a4;background:#fffbf2;color:#644d1e}.sw-decision{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid #e2e9f0}
.sw-header-job{display:flex;align-items:center;gap:16px;flex:1;justify-content:flex-end;min-width:0}.sw-header-job>div{min-width:0}.sw-header-job small{display:block;color:#5b6d80;font-size:11px;text-transform:uppercase;letter-spacing:.07em}.sw-header-job strong{font-size:14px;display:block;overflow-wrap:anywhere}
.sw-switch{position:relative;flex-shrink:0}.sw-switch>summary{cursor:pointer;min-height:44px;display:flex;align-items:center;color:#214569;font-size:13px}
.sw-switch[open] .bc840-select{position:absolute;z-index:80;right:0;top:100%;width:min(480px,calc(100vw - 40px));background:white;padding:18px;box-shadow:0 12px 35px #10263a20;border:1px solid #d2deea;border-radius:12px;display:flex;flex-wrap:wrap;gap:10px}
.sw-switch .bc840-select select{flex:1;min-width:0;width:100%}.sw-ask-link{white-space:nowrap;display:inline-flex;align-items:center;min-height:44px;font-size:13px}
.sw-mode{display:flex;gap:14px;font-size:13px;margin:8px 0 20px}.sw-mode a[aria-current]{color:#172f4d;font-weight:750;text-decoration:none}
.sw-context-note{font-size:13px;color:#536579;margin:6px 0 16px}.sw-draft{font-size:13px;padding:10px 12px;background:#f5f8fb;border:1px solid #d7e2ec;border-radius:8px;margin:10px 0}.sw-draft button{margin:5px 8px 5px 0;font-size:13px}
:focus-visible{outline:3px solid #426fa5;outline-offset:3px}
@media(max-width:1000px){.sw-columns{grid-template-columns:1fr}.sw-projects,.sw-link-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.sw-header-job{justify-content:flex-start}}
@media(max-width:600px){.bc840-header{flex-wrap:wrap;gap:12px}.bc8102-breadcrumb{width:100%;min-width:0;flex-wrap:wrap}.sw-header-job{flex-basis:100%}.sw-card{padding:18px;border-radius:12px}.sw-job{margin-top:4px}.sw-job .sw-pill{display:none}.sw-projects,.sw-link-grid{grid-template-columns:1fr}.sw-actions{gap:9px}.sw-actions a,.sw-actions button{padding:12px;font-size:14px}.sw-header-job{width:100%;gap:10px;flex-wrap:wrap}.sw-header-job>div{flex:1 1 160px;min-width:0}.sw-ask-link{margin-left:auto}.sw-switch[open] .bc840-select{right:-12px}.sw-card-heading{align-items:flex-start}}
</style>'''

DRAFT_SCRIPT = r'''<script id="bc828-drafts">(()=>{
'use strict';const root=document.querySelector('[data-bc828-account]');if(!root)return;
const identity=root.dataset.bc828Account,project=root.dataset.bc828Project||'none';
const prefix='bc828:draft:'+identity+':',ttl=24*60*60*1000;
function values(form){const out={};for(const e of form.elements){if(!e.name||e.disabled||!['TEXTAREA','INPUT'].includes(e.tagName))continue;
if(e.tagName==='INPUT'&&!['text','number','date','time'].includes(e.type))continue;
if(e.value.length<=12000)out[e.name]=e.value;}return out;}
function get(key){try{let v=JSON.parse(sessionStorage.getItem(key)||'null');if(!v||!Number.isFinite(v.at)||Date.now()-v.at>ttl||v.at>Date.now()+300000||!v.fields||typeof v.fields!=='object'||Array.isArray(v.fields)){sessionStorage.removeItem(key);return null;}return v;}catch(_){return null;}}
function del(key){try{sessionStorage.removeItem(key);}catch(_){}}
for(const form of document.querySelectorAll('form')){let path;try{path=new URL(form.action,location.href).pathname;}catch(_){continue;}
if(!(form.querySelector('#command-question')||/^\/workspace\/daily\/reports\/\d+\/site$/.test(path)))continue;
const key=prefix+project+':'+path,initial=values(form);let old=get(key),restorePending=false;
const note=document.createElement('div');note.className='sw-draft';note.setAttribute('role','status');note.hidden=true;form.append(note);
function message(text){note.replaceChildren(document.createTextNode(text));note.hidden=false;}
if(old&&JSON.stringify(old.fields)!==JSON.stringify(initial)){
restorePending=true;message('An unfinished draft is available in this tab. Review it before saving. ');
const restore=document.createElement('button');restore.type='button';restore.textContent='Restore draft';
restore.onclick=()=>{for(const e of form.elements){if(Object.hasOwn(initial,e.name)&&typeof old.fields[e.name]==='string'&&old.fields[e.name].length<=12000)e.value=old.fields[e.name];}restorePending=false;message('Draft restored. Not yet saved to the project.');};
const discard=document.createElement('button');discard.type='button';discard.textContent='Discard draft';discard.onclick=()=>{del(key);restorePending=false;note.hidden=true;};note.append(restore,discard);
}else if(old){del(key);}
form.addEventListener('input',()=>{if(restorePending)return;try{sessionStorage.setItem(key,JSON.stringify({at:Date.now(),fields:values(form)}));message('Draft kept in this tab. Not yet saved to the project.');}catch(_){message('This browser cannot keep a draft. Keep this page open until your work is saved.');}},true);
form.addEventListener('submit',()=>{if(restorePending)return;try{sessionStorage.setItem(key,JSON.stringify({at:Date.now(),fields:values(form)}));}catch(_){} });
}
for(const f of document.querySelectorAll('form[action="/logout"]'))f.addEventListener('submit',()=>{try{for(let i=sessionStorage.length-1;i>=0;i--){const k=sessionStorage.key(i);if(k&&k.startsWith(prefix))sessionStorage.removeItem(k);}}catch(_){}});
})();</script>'''

class WorkdayContextMiddleware:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        try: query = parse_qs(scope.get('query_string', b'').decode('utf-8'), max_num_fields=150)
        except (ValueError, UnicodeDecodeError): query = {}
        token = _request.set((scope.get('path', ''), query))
        try: await self.app(scope, receive, send)
        finally: _request.reset(token)


def esc(value): return escape(str(value if value is not None else ''), quote=True)

def install(ns):
    service = SimplerWorkday(ns)
    ns['app'].state.simpler_workday = service
    service.install()
    return service

class SimplerWorkday:
    def __init__(self, ns):
        self.ns = ns
        self.app = ns['app']
        self.hub = self.app.state.workspace_hub
        self.field = self.app.state.blueprint_field
        self.db, self.require = self.field.db, self.field.require
        self.previous_shell = ns['_bc840_shell']
        self.previous_select = ns['bc840_select_project']
        self.previous_command_render = self.app.state.command_center.render
        self.routes = []
        self.original_handlers = {(r.path,m): r.endpoint for r in self.app.routes if hasattr(r,'methods') for m in (r.methods or ())}
        self.original_selector = self.hub.selector

    def projects(self, user):
        if self.ns['_bc840_tier'](user) in {'owner','admin','trade','observer'}:
            return self.ns['_bc840_projects'](user)
        with self.db() as c:
            _, _, projects = self.hub.user(c)
        return projects

    def context(self, project_id=0):
        user = self.ns['_bc840_user']()
        self.require(user, 'Sign in again to continue.', 401)
        self.require(project_id >= 0, 'Choose an available project.', 400)
        projects = self.projects(user)
        pid = project_id or self.ns['_bc840_selected_project'](user)
        project = next((p for p in projects if p['id'] == pid), None)
        if project_id: self.require(project is not None, 'This project is not assigned to your account.', 403)
        # Do not silently choose another job when a stale selection is present.
        if not pid and len(projects) == 1: project = projects[0]
        return user, project, projects

    def viewed_project(self, user, projects):
        path, query = _request.get()
        pid = self.ns['_bc840_selected_project'](user)
        if query.get('project_id'):
            try: pid = int(query['project_id'][0])
            except (TypeError, ValueError): pid = None
        else:
            match = re.search(r'/projects/(\d+)(?:/|$)', path)
            if match: pid = int(match.group(1))
            else:
                resources = (
                  (r'/workspace/drawing-sheets/(\d+)(?:/|$)', 'bc_drawing_sheets'),
                  (r'/workspace/drawing-sets/(\d+)(?:/|$)', 'bc_drawing_sets'),
                  (r'/workspace/drawings/(\d+)(?:/|$)', 'attachments'),
                  (r'/workspace/daily/reports/(\d+)(?:/|$)', 'daily_reports'),
                  (r'/workspace/(?:shared|sharing)/(\d+)(?:/|$)', 'bc_shared_work'),
                  (r'/workspace/checklists/(\d+)(?:/|$)', 'bc_check_runs'),
                  (r'/workspace/documents/(\d+)(?:/|$)', 'bc_doc_records'),
                  (r'/workspace/command/actions/(\d+)(?:/|$)', 'bc_command_action_plans'),
                )
                for pattern, table in resources:
                    m = re.match(pattern, path)
                    if not m: continue
                    try:
                        with self.db() as c:
                            row = c.execute(f'SELECT project_id FROM {table} WHERE id=?', (int(m.group(1)),)).fetchone()
                        pid = row['project_id'] if row else None
                    except Exception:
                        log.warning('Workday header could not resolve record context', exc_info=True)
                        pid = None
                    break
        if not pid and len(projects) == 1 and not query.get('project_id'):
            return projects[0]
        return next((p for p in projects if p['id'] == pid), None)

    def area_for(self, path):
        if path.startswith(('/company','/billing','/choose-plan','/workspace/access','/workspace/company-')): return 'Company','/company'
        if path.startswith(('/workspace/drawing','/documents/')) and path != '/workspace/document-requests': return 'Drawings','/workspace/drawings'
        if path in {'/workspace','/workspace/projects'} or re.fullmatch(r'/workspace/projects/\d+',path): return 'My workspace','/workspace'
        for key, (title, _, main, more) in AREAS.items():
            if path == '/workspace/'+key: return title, '/workspace/'+key
            for _, target, _ in main+more:
                if path == target or path.startswith(target+'/'): return title,'/workspace/'+key
        if path.startswith('/workspace/shared'): return 'My shared work','/workspace/shared'
        return 'My workspace','/workspace'

    def destination(self, path, pid):
        if (path == '/workspace' or path.startswith('/workspace/')) and path not in {'/workspace/portfolio','/workspace/setup'}:
            return path+'?'+urlencode({'project_id':pid})
        return path

    def shell(self, title, body, *args, **kwargs):
        user = self.ns['_bc840_user']()
        if not user: return self.previous_shell(title,body,*args,**kwargs)
        # Preserve the actual service data/permission checks; only reorganize its shell.
        projects = self.projects(user)
        project = self.viewed_project(user,projects)
        pid = project['id'] if project else 0
        path = _request.get()[0] or self.ns['_bc840_request_path'].get()
        tier = self.ns['_bc840_tier'](user)
        area, parent = self.area_for(path)
        nav = NAV if tier not in {'trade','observer'} else (NAV[0],) + ((('My shared work','/workspace/shared'),) if tier=='trade' else ())
        parts=[]
        for label,url in nav:
            active = parent == url or (url=='/workspace' and path=='/workspace/command')
            href = self.destination(url,pid) if pid else url
            parts.append('<a class="bc8102-link" href="'+esc(href)+'"'+(' aria-current="page"' if active else '')+'>'+self.ns['_bc8102_icon']('home' if url=='/workspace' else 'scope')+'<span>'+esc(label)+'</span></a>')
        html = self.previous_shell(title,body,*args,**kwargs)
        html = re.sub(r'(<nav class="bc840-nav" aria-label="Main navigation">).*?(</nav>)',lambda m:m[1]+''.join(parts)+m[2],html,count=1,flags=re.S)
        # Remove only the known global form; page-specific filter and destination
        # selectors (e.g. assigning a template to a project) are intentionally retained.
        html = re.sub(r'<form class="bc840-select".*?</form>','',html,count=1,flags=re.S)
        label=' · '.join(str(v) for v in (project.get('number'),project['name']) if v) if project else 'Choose a project'
        options=''.join('<option value="'+str(p['id'])+'"'+(' selected' if pid==p['id'] else '')+'>'+esc(' · '.join(str(v) for v in (p.get('number'),p['name']) if v))+'</option>' for p in projects)
        switch = ('<details class="sw-switch" id="change-project"><summary>Change job</summary><form class="bc840-select" method="post" action="/workspace/select-project">'
                  '<label for="bc840-project">Current project</label><select id="bc840-project" name="project_id" required><option value="">Choose a project</option>'+options+
                  '</select><input type="hidden" name="return_to" value="'+esc(parent if parent!='/company' else '/workspace')+'"><button>Open job</button></form></details>') if options else ''
        ask = '<a class="sw-ask-link" href="/workspace'+('?project_id='+str(pid) if pid else '')+'#ask">Ask BuildCommand</a>' if self.ns['_bc850_manager'](user) else ''
        header = '<div class="sw-header-job"><div><small>Current job</small><strong>'+esc(label)+'</strong></div>'+switch+ask+'</div>'
        html = html.replace('</header><main',header+'</header><main',1)
        # Known hub/daily/Command pickers are navigation, not editing fields.
        html = re.sub(r'<form\b[^>]*method="get"[^>]*>(?:(?!</form>).)*<select id="(?:command-project|hub-project|daily-project)"(?:(?!</form>).)*</form>','',html,flags=re.S)
        # A deterministic parent link must not invoke history.back(), which can
        # return to a different job or a previously submitted form.
        if area != 'Company':
            back_parent = '/workspace' if path == parent else parent
            back_label = 'My workspace' if back_parent == '/workspace' else area
            back_url = self.destination(back_parent,pid) if pid else back_parent
            html = re.sub(r'(<a class="hub-back" )data-bc-back href="[^"]*"([^>]*>).*?</a>',
                lambda m:m[1]+'data-bc828-back href="'+esc(back_url)+'"'+m[2]+'← '+esc(back_label)+'</a>',html,count=1,flags=re.S)
        # Return links and the logo must follow the viewed job, including explicit
        # deep links that do not mutate the stored project selection.
        if pid:
            html = html.replace('href="/workspace"', 'href="/workspace?project_id='+str(pid)+'"')
            html = html.replace('href="/workspace#', 'href="/workspace?project_id='+str(pid)+'#')
        html = html.replace('</head>',CSS+'</head>',1)
        identity = str(user['company_id'])+':'+str(user['id'])
        html = html.replace('<main id="main-content"','<main data-bc828-account="'+esc(identity)+'" data-bc828-project="'+str(pid)+'" id="main-content"',1)
        html = html.replace('</body>',DRAFT_SCRIPT+'</body>',1)
        return html

    def page(self, title, body):
        return self.ns['_bc840_page'](title,'<div class="sw">'+body+'</div>')

    def tool(self, pid, path, label, primary=False):
        if path.startswith('/workspace/'):
            return '<a'+(' class="sw-primary"' if primary else '')+' href="'+esc(self.destination(path,pid))+'">'+esc(label)+'</a>'
        # The existing POST handoff selects the job before a legacy tool opens.
        return self.hub.open_form(pid,path,label)

    def project_cards(self, projects, selected):
        if not projects: return self.ns['_bc840_project_cards'](projects)
        cards=''
        for p in projects:
            cards+='<article class="sw-project"><span class="sw-kicker">'+esc(p.get('number') or 'Project')+'</span><h3>'+esc(p['name'])+'</h3><p>'+esc(str(p.get('status') or 'Status not recorded').replace('_',' ').title())+'</p>'
            if selected and p['id']==selected['id']: cards+='<span class="sw-help">Current job</span>'
            else: cards+='<form method="post" action="/workspace/select-project"><input type="hidden" name="project_id" value="'+str(p['id'])+'"><button>Open job</button></form>'
            cards+='</article>'
        return '<div class="sw-projects">'+cards+'</div>'

    def collect(self, user, project):
        """Read existing authorized queues. No fabricated scores or provider calls."""
        pid, cid = project['id'], user['company_id']
        data={'cards':[], 'warnings':[], 'totals':{}, 'drafts':0, 'followups':0}
        with self.db() as c:
            self.field.actor(c,pid)
            totals,_,rows = self.ns['_bc860_rows'](c,user,pid,'attention','',1)
            data['totals']=totals
            for r in rows:
                reason = 'Resolve the blocker with '+r['recipient_name']+'.' if r['blocked'] else 'Review the latest update from '+r['recipient_name']+'.' if r['pending_count'] else 'Confirm the due date and next step with '+r['recipient_name']+'.'
                data['cards'].append((0 if r['blocked'] else 2, r['title'],reason,'/workspace/sharing/'+str(r['id']),'Shared work',r.get('due_date') or ''))
            try:
                s=self.app.state.safety_checklists
                checks=s.attention_data(c,user,pid)
                for r in checks['items']:
                    if r['findings']: data['cards'].append((0,r['title'],str(r['findings'])+' recorded finding(s) need attention.','/workspace/checklists/'+str(r['id']),'Safety / inspection',r['check_date']))
            except Exception:
                log.exception('Workday checklist summary unavailable'); data['warnings'].append('Safety and inspection summary is unavailable. Open Field work to check the records.')
            rfis=self.app.state.rfi_field.brief_data(c,user,project)
            for r in rfis['items']:
                if r['needs_review']: data['cards'].append((0,r['title'],'The issued RFI answer changed. Review before directing the trade.','/workspace/sharing/'+str(r['id']),'RFI source review',r.get('due_date') or ''))
            actions=self.app.state.command_actions
            follows=actions.brief_data(c,user,project);data['followups']=follows['total']
            for r in follows['items'][:8]: data['cards'].append((1,r['title'],r['note'] or 'Review this planned follow-up.','/workspace/command/actions/'+str(r['plan_id']),'Planned follow-up',r['followup_date']))
            data['drafts']=int(c.execute("SELECT COUNT(*) AS n FROM bc_command_action_plans WHERE company_id=? AND project_id=? AND created_by=? AND state IN ('DRAFT','FAILED')",(cid,pid,user['id'])).fetchone()['n'])
            rows=c.execute("SELECT id,title,priority,due,owner FROM action_items WHERE project_id=? AND UPPER(COALESCE(status,''))='OPEN' ORDER BY CASE UPPER(priority) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,due,id LIMIT 12",(pid,)).fetchall()
            for row in rows:
                r=dict(row);data['cards'].append((0 if str(r['priority']).upper()=='CRITICAL' else 1 if str(r['priority']).upper()=='HIGH' else 3,r['title'],'Confirm the next step'+(' with '+r['owner'] if r.get('owner') else '')+'.','/workspace/workday/actions?project_id='+str(pid),'Action record',r.get('due') or ''))
        # The existing deterministic daily engine is a fallback, not a new AI call.
        if len(data['cards'])<3:
            priorities=self.app.state.daily_command.priorities(pid)
            if not priorities['available']: data['warnings'].append('Calculated project priorities are unavailable; saved work queues are shown.')
            for r in priorities.get('morning',[]):
                data['cards'].append((4,r.get('title') or 'Review project priority',r.get('recommended_action') or r.get('reason') or 'Verify the project record.','/workspace/command/projects/'+str(pid)+'/analysis','Suggested by project analysis',r.get('due') or ''))
        seen=set();cards=[]
        for item in sorted(data['cards'],key=lambda x:(x[0],str(x[5]) or '9999')):
            key=(item[1].strip().lower(),item[3])
            if key not in seen: cards.append(item);seen.add(key)
        data['cards']=cards
        return data

    def priorities_html(self, data, pid):
        body='<section class="sw-card" id="today"><div class="sw-card-heading"><h2>Today’s priorities</h2><span class="sw-help">Start here</span></div>'
        cards=data['cards']
        for i,(_,title,reason,url,source,due) in enumerate(cards[:3],1):
            body+='<article class="sw-priority"><span class="sw-number">'+str(i)+'</span><div><h3><a href="'+esc(url)+'">'+esc(title)+'</a></h3><p>'+esc(reason)+'</p><p class="sw-meta">'+esc(source)+(' · Due '+esc(due) if due else '')+'</p></div></article>'
        if not cards: body+='<div class="sw-empty"><p>No urgent items were found in the saved queues checked here. This is not an all-clear for the job.</p><a href="/workspace/field?project_id='+str(pid)+'">Review today’s field work</a></div>'
        body+='<a class="sw-quiet-link" href="/workspace/command?project_id='+str(pid)+'&amp;view=all">Full work &amp; decision queue →</a><br><a class="sw-quiet-link" href="/workspace/workday/actions?project_id='+str(pid)+'">All open action records →</a>'
        for warning in data['warnings']: body+='<p class="sw-help">'+esc(warning)+'</p>'
        return body+'</section>'

    def trade_home(self,user,project):
        pid=project['id'];table=self.ns['_bc850_member_table']()
        with self.db() as c:
            # Query recipient + company + project + membership BEFORE displaying
            # counts/titles. Revalidate every share with the installed service.
            rows=c.execute(f'''SELECT s.id FROM bc_shared_work s JOIN projects p ON p.id=s.project_id
             WHERE s.company_id=? AND p.company_id=? AND s.project_id=? AND s.recipient_user_id=? AND s.revoked_at IS NULL
             AND EXISTS(SELECT 1 FROM {table} m WHERE m.user_id=s.recipient_user_id AND m.project_id=s.project_id)
             ORDER BY CASE WHEN s.state='OPEN' THEN 0 ELSE 1 END,CASE WHEN s.due_date='' THEN 1 ELSE 0 END,s.due_date,s.id DESC LIMIT 20''',
             (user['company_id'],user['company_id'],pid,user['id'])).fetchall()
            shares=[]
            for row in rows:
                try: shares.append(self.ns['_bc850_share'](c,user,row['id']))
                except self.ns['_BC850_Problem'] as exc:
                    if exc.status not in {403,404}: raise
        body='<section class="sw-card"><h2>Assigned to me</h2><p>Only work your project leader has shared with you appears here.</p>'
        for i,s in enumerate(shares[:3],1):
            body+='<article class="sw-priority"><span class="sw-number">'+str(i)+'</span><div><h3><a href="/workspace/shared/'+str(s['id'])+'">'+esc(s['title'])+'</a></h3><p>'+('Response enabled' if s['allow_response'] and s['state']=='OPEN' else 'For your reference')+(' · Due '+esc(s['due_date']) if s['due_date'] else '')+'</p></div></article>'
        if not shares: body+='<div class="sw-empty"><p>No work has been shared with you on this job yet. Your project leader controls which drawings and requests appear.</p></div>'
        body+='<div class="sw-actions"><a href="/workspace/shared?project_id='+str(pid)+'">All my shared work</a><a href="/workspace/shared?project_id='+str(pid)+'&amp;kind=document">Shared drawings &amp; documents</a></div></section>'
        return body

    def home(self, project_id:int=0, mode:str='field', notice:str=''):
        user,project,projects=self.context(project_id)
        tier=self.ns['_bc840_tier'](user);manager=self.ns['_bc850_manager'](user)
        self.require(mode in {'field','estimating'},'Choose Field or Estimating view.',400)
        body=''
        if notice=='project-changed': body+='<div class="sw-status" role="status">Current job updated.</div>'
        if not project:
            body+='<div class="sw-job"><div><span class="sw-kicker">My workspace</span><h1>Which job are you working on?</h1><p>Open a project to see its next useful action.</p></div></div>'
        else:
            pid=project['id']
            body+='<div class="sw-job"><div><span class="sw-kicker">'+esc(project.get('number') or 'Current job')+' · My workspace</span><h1>'+esc(project['name'])+'</h1><p>'+('Your assigned work, drawings, and responses.' if tier=='trade' else 'A clear place to start. One job. Your next step.')+'</p></div><span class="sw-pill">'+esc(str(project.get('status') or 'Project').replace('_',' ').title())+'</span></div>'
            if tier=='trade': body+=self.trade_home(user,project)
            elif tier=='observer': body+='<section class="sw-card"><h2>Your project overview</h2><p>Your access is read-only. Internal GC tools and unshared records remain private.</p><a href="/workspace/projects/'+str(pid)+'">Open assigned project overview</a></section>'
            else:
                body+='<nav class="sw-mode" aria-label="Workspace view"><a href="/workspace?project_id='+str(pid)+'&amp;mode=field"'+(' aria-current="page"' if mode=='field' else '')+'>Field work</a><a href="/workspace?project_id='+str(pid)+'&amp;mode=estimating"'+(' aria-current="page"' if mode=='estimating' else '')+'>Estimating</a></nav>'
                if mode=='estimating':
                    body+='<section class="sw-card"><h2>Move your estimate forward</h2><p>Continue with the drawings, quantities, or bid you are working on.</p><div class="sw-actions">'
                    for label,path in [('Open drawings','/workspace/drawings'),('Estimator Intelligence','/brain/estimator'),('Review quantities','/brain/takeoff'),('Bid packages','/preconstruction/packages')]:body+=self.tool(pid,path,label)
                    body+='</div><a class="sw-quiet-link" href="/workspace/estimating?project_id='+str(pid)+'">All estimating tools →</a></section>'
                body+='<div class="sw-columns"><div>'
                data=None
                if manager:
                    try: data=self.collect(user,project)
                    except self.ns['_BC850_Problem']: raise
                    except Exception:
                        log.exception('Workday priority summary unavailable')
                        body+='<section class="sw-card sw-alert"><h2>Priorities could not load</h2><p>Your project records have not been changed. Open the work queue or reload this page.</p><a href="/workspace/command?project_id='+str(pid)+'&amp;view=all">Open work queue</a></section>'
                    if data: body+=self.priorities_html(data,pid)
                else: body+='<section class="sw-card"><h2>Pick up your project work</h2><p>Open drawings or project records. Your project leader manages field briefings and trade sharing.</p></section>'
                if manager and getattr(self.app.state,'project_lookahead',None):
                    try:
                        with self.db() as c:
                            self.field.actor(c,pid)
                            body+=self.app.state.project_lookahead.home_card(c,user,project)
                    except self.ns['_BC850_Problem']: raise
                    except Exception:
                        log.exception('Lookahead workspace summary unavailable')
                        body+='<section class="sw-card"><h2>Project schedule</h2><p>The schedule summary could not load.</p><a href="/workspace/schedule?project_id='+str(pid)+'">Open schedule</a></section>'
                body+='<section class="sw-card" id="quick-actions"><h2>Quick actions</h2><p>Go straight to the work.</p><div class="sw-actions">'+self.tool(pid,'/workspace/drawings','Open drawings',True)
                for label,path in ([('Daily report','/workspace/daily'),('Add a site photo','/photo-ai'),('Share work','/workspace/sharing')] if manager else [('RFIs & issues','/issues'),('Schedule','/workspace/schedule'),('Submittals','/submittals')]):body+=self.tool(pid,path,label)
                body+='</div><details class="sw-more"><summary>More actions</summary><div class="sw-actions">'
                for label,path in [('Schedule','/workspace/schedule'),('RFIs & issues','/issues')]+([('Safety & inspections','/workspace/checklists'),('Project startup','/project-startup'),('Project documents','/workspace/documents'),('Review trade scopes','/workspace/scopes')] if manager else []):body+=self.tool(pid,path,label)
                body+='</div></details></section></div><div>'
                if manager:
                    body+='<section class="sw-card" id="ask"><h2>Ask BuildCommand</h2><p>Get help from the saved records on this job.</p>'+self.app.state.command_center.ask_form(pid)+'<p class="sw-help">Answers and drafts require your review. Asking does not approve work or send a message.</p></section>'
                    if data:
                        t=data['totals']
                        body+='<details class="sw-card" id="pending-decisions"><summary>Pending decisions</summary><p class="sw-help">These queues can overlap; they are not added into one total.</p>'
                        for label,key,view in [('Updates to review','review','review'),('Blocked shared work','blocked','blocked'),('Ready for review','ready','ready'),('Overdue shared work','overdue','overdue')]:body+='<a class="sw-decision" href="/workspace/command?project_id='+str(pid)+'&amp;view='+view+'"><span>'+label+'</span><strong>'+str(t.get(key,0))+'</strong></a>'
                        body+='<a class="sw-decision" href="/workspace/command/projects/'+str(pid)+'/actions"><span>Your drafts / due follow-ups</span><strong>'+str(data['drafts'])+' / '+str(data['followups'])+'</strong></a></details>'
                    body+='<details class="sw-card"><summary>Briefing &amp; project intelligence</summary><p><a href="/workspace/command/projects/'+str(pid)+'/brief">Review today’s briefing</a></p><p><a href="/workspace/command/projects/'+str(pid)+'/briefs">Saved briefings</a></p><p><a href="/workspace/brain?project_id='+str(pid)+'">More analysis tools</a></p><p><a href="/workspace/command?project_id='+str(pid)+'&amp;view=all">Full Command work queue</a></p></details>'
                else: body+='<section class="sw-card"><h2>Project records</h2><p>Find RFIs, submittals, and the information your role can access.</p><a href="/workspace/records?project_id='+str(pid)+'">Open project records →</a></section>'
                body+='</div></div>'
        body+='<section class="sw-card" id="my-projects"><details'+(' open' if not project else '')+'><summary id="my-projects-heading">My projects ('+str(len(projects))+')</summary>'+self.project_cards(projects,project)+'</details>'
        setup=getattr(self.app.state,'project_setup',None)
        if setup and setup.admin(user):body+='<a class="sw-quiet-link" href="/workspace/setup/new">Create a project →</a>'
        return self.page('My workspace',body+'</section>')

    def select_project(self, project_id:int=Form(...), return_to:str=Form('/workspace')):
        user,project,_=self.context(project_id)
        result=self.previous_select(project_id)
        if result.status_code!=303:return result
        allowed={'/workspace','/workspace/drawings',*(('/workspace/'+k) for k in AREAS)}
        target=return_to if return_to in allowed else '/workspace'
        target+=('?'+urlencode({'project_id':project_id,'notice':'project-changed'}))
        return RedirectResponse(target,303,headers={'Cache-Control':'no-store'})

    def area(self, area:str, project_id:int=0):
        self.require(area in AREAS,'This work area is unavailable.',404)
        user,project,projects=self.context(project_id)
        self.require(self.ns['_bc840_tier'](user) not in {'trade','observer'},'Open My workspace for your assigned work.',403)
        if not project:return self.home()
        title,intro,main,more=AREAS[area];pid=project['id'];manager=self.ns['_bc850_manager'](user)
        paths={r.path for r in self.app.routes if hasattr(r,'path')}
        body='<div class="sw-job"><div><span class="sw-kicker">'+esc(project['name'])+'</span><h1>'+title+'</h1><p>'+intro+'</p></div></div><section class="sw-card"><div class="sw-link-grid">'
        for label,path,needs_manager in main:
            if path in paths and (not needs_manager or manager): body+=self.tool(pid,path,label)
        body+='</div>'
        if area=='team' and manager:
            body+='<p><a href="/workspace/sharing/projects/'+str(pid)+'/team">People assigned to this job →</a></p>'
            if self.ns['_bc840_tier'](user) in {'owner','admin'}:body+='<p><a href="/company/invitations">Invite someone →</a></p>'
        body+='<details class="sw-more"><summary>More '+title.lower()+'</summary><div class="sw-link-grid">'
        for label,path,needs_manager in more:
            if path in paths and (not needs_manager or manager):body+=self.tool(pid,path,label)
        return self.page(title,body+'</div></details></section>')

    def estimating(self,project_id:int=0):return self.area('estimating',project_id)
    def field_work(self,project_id:int=0):return self.area('field',project_id)
    def records(self,project_id:int=0):return self.area('records',project_id)
    def team(self,project_id:int=0):return self.area('team',project_id)

    def action_records(self,project_id:int=0):
        user,project,_=self.context(project_id)
        self.require(self.ns['_bc850_manager'](user),'Your project leader manages action records.',403)
        if not project:return self.home()
        pid=project['id']
        with self.db() as c:
            self.field.actor(c,pid)
            rows=c.execute("SELECT id,title,owner,priority,due FROM action_items WHERE project_id=? AND UPPER(COALESCE(status,''))='OPEN' ORDER BY CASE UPPER(priority) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,due,id LIMIT 100",(pid,)).fetchall()
        body='<h1>Open action records</h1><p>'+esc(project['name'])+'</p><section class="sw-card">'
        for r in rows:
            body+='<article class="sw-priority"><div><h3>'+esc(r['title'])+'</h3><p>'+esc(r['owner'] or 'Owner not recorded')+' · '+esc(r['priority'])+(' · Due '+esc(r['due']) if r['due'] else '')+'</p><form method="post" action="/workspace/actions/'+str(r['id'])+'/complete"><input type="hidden" name="project_id" value="'+str(pid)+'"><button>Mark complete</button></form></div></article>'
        if not rows:body+='<p>No open action records are saved for this job.</p>'
        body+='<p class="sw-help">Showing up to 100 open records. All history stays in the existing action register.</p>'+self.hub.open_form(pid,'/actions','All action history')
        return self.page('Action records',body+'</section>')

    def command_render(self,user,project,projects,totals,matched,rows,view,q,page,notice):
        # Existing filtered queues stay intact; the default Command landing is
        # now the same job home as My workspace, not another competing dashboard.
        if view=='attention' and not q and page==1 and not notice:
            return self.home(project_id=project['id'])
        return self.previous_command_render(user,project,projects,totals,matched,rows,view,q,page,notice)

    def install(self):
        self.ns['_bc840_shell']=self.shell
        self.ns['_runtime'].shell=self.ns['_bc840_shell']
        self.app.add_middleware(WorkdayContextMiddleware)
        self.app.state.command_center.render=self.command_render
        specs=(('GET','/workspace',self.home),('POST','/workspace/select-project',self.select_project),
               ('GET','/workspace/estimating',self.estimating),('GET','/workspace/field',self.field_work),
               ('GET','/workspace/records',self.records),('GET','/workspace/team',self.team),
               ('GET','/workspace/workday/actions',self.action_records))
        for method,path,fn in specs:
            endpoint=self.hub.endpoint(fn)
            self.ns['_bc840_replace'](path,method,endpoint)
            self.routes.append((method,path,endpoint))
        health='/health/simpler-workday-8-28-0'
        self.app.add_api_route(health,self.health,methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add(health)
        self.ns['BUILD_COMMAND_RELEASE']=VERSION
        self.ns['BUILD_COMMAND_RELEASE_NAME']=RELEASE
        self.app.version=VERSION

    def health(self):
        active={(r.path,m):r.endpoint for r in self.app.routes if hasattr(r,'methods') for m in (r.methods or ())}
        changed={('/workspace','GET'),('/workspace/select-project','POST')}
        checks={method+' '+path:active.get((path,method)) is fn for method,path,fn in self.routes}
        checks.update(
          installed=getattr(self.app.state,'simpler_workday',None) is self,
          release_active=self.app.version in {VERSION,'8.29.0'},
          previous_tool_handlers_preserved=all(active.get(k) is v for k,v in self.original_handlers.items() if k not in changed),
          single_private_shell=self.ns['_runtime'].shell is self.ns['_bc840_shell'],
          command_uses_job_home=self.app.state.command_center.render==self.command_render,
          existing_project_selection_retained=callable(self.previous_select),
          existing_project_startup_guard_retained=active.get(('/project-startup','GET')) is self.original_handlers.get(('/project-startup','GET')),
          blueprint_analysis_preserved=bool(getattr(self.app.state,'blueprint_batches',None)),
          scaled_takeoff_preserved=bool(getattr(self.app.state,'drawing_takeoff',None)),
          daily_reports_preserved=bool(getattr(self.app.state,'daily_reports',None)),
          reviewed_trade_sharing_preserved=active.get(('/workspace/shared/{share_id}/respond','POST')) is self.original_handlers.get(('/workspace/shared/{share_id}/respond','POST')),
          form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'],
          company_and_owner_boundaries_retained=callable(self.ns['_bc840_owner_path']) and callable(self.ns['_bc840_admin_path']),
        )
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if ok else 'attention',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
          scope='Installation and retained-handler checks only. Real browser, account, PostgreSQL and uncoached contractor walkthroughs still require staging verification.'),status_code=200 if ok else 503)
