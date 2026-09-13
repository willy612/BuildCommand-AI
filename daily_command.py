"""8.8.0 daily field briefings built from authorized project records.

GETs are read-only. Preview tokens bind the actor and session to an exact
snapshot. Saving rechecks access and visible source contents, writes an
immutable briefing and its audit event, then consumes the preview atomically.
No external AI request or notification is made by this module.
"""
import json
import logging
import re
import secrets
from datetime import timedelta
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from blueprint_field import esc, packed, digest

VERSION = '8.8.0'
RELEASE = 'Daily Command Briefing'
log = logging.getLogger('buildcommand.daily_command')
ATTENTION_FIELDS = ('id', 'title', 'kind', 'version', 'due_date', 'state', 'recipient_name',
                    'latest_status', 'latest_message', 'latest_time', 'latest_id', 'pending_id',
                    'pending_status', 'pending_message', 'pending_time', 'pending_count',
                    'blocked', 'overdue', 'ready')
ACTION_FIELDS = ('source_type', 'source_id', 'title', 'reason', 'recommended_action',
                 'trade', 'due', 'priority', 'state')
CSS = '''<style>
.bc880{color:#172a3e;max-width:1100px;margin:auto;overflow-wrap:anywhere}
.bc880 h1{font-size:clamp(28px,4vw,40px);line-height:1.15}.bc880 h2{font-size:23px}
.bc880 h3{font-size:18px}.bc880 p,.bc880 li{line-height:1.6}
.bc880 .card{padding:24px;margin:20px 0;border:1px solid #dce4ed;border-radius:12px;background:white}
.bc880 .small{font-size:13px;color:#53667a}.bc880 .warning{background:#fff4dc;border-color:#ead5a8}
.bc880 .grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}
.bc880 table{width:100%;border-collapse:collapse}.bc880 th,.bc880 td{padding:12px;text-align:left;border-bottom:1px solid #dce4ed;vertical-align:top}
.bc880 textarea{width:100%;box-sizing:border-box;font:inherit;padding:12px;border-radius:8px;border:1px solid #aabace}
.bc880 .actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:20px 0}
.bc880 .button,.bc880 button{padding:12px 18px;background:#173e68;color:white;border:0;border-radius:8px;text-decoration:none;font:inherit;font-weight:700;cursor:pointer}
.bc880 .table-wrap{overflow-x:auto}.bc880 blockquote{margin:10px 0;padding:12px;border-left:3px solid #e4ad3e;background:#f5f7fa;white-space:pre-wrap}
@media(max-width:720px){.bc880 .grid{grid-template-columns:1fr}.bc880 .card{padding:18px}.bc880 th,.bc880 td{padding:9px}}
@media print{body{background:white!important;margin:0;font:11pt Arial}.bc880{max-width:none}.bc880 .no-print{display:none!important}.bc880 .card{border:0;border-radius:0;padding:8px 0;margin:12px 0}.bc880 .grid{display:block}.bc880 h2,.bc880 h3{break-after:avoid}.bc880 tr,.bc880 article{break-inside:avoid}.bc880 a{color:inherit;text-decoration:none}}
</style>'''


def install(namespace):
    service = DailyCommand(namespace)
    namespace['app'].state.daily_command = service
    service.register()
    return service


class DailyCommand:
    def __init__(self, ns):
        self.ns = ns
        self.rt = ns['_runtime']
        self.field = ns['app'].state.blueprint_field
        self.db = self.field.db
        self.require = self.field.require
        self.version = VERSION
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_daily_brief_reviews(
                    id {key},token_hash TEXT NOT NULL UNIQUE,session_hash TEXT NOT NULL,
                    actor_user_id BIGINT NOT NULL,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    source_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,consumed_at TEXT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_daily_command_briefs(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    brief_date TEXT NOT NULL,source_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created_at TEXT NOT NULL)''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_daily_brief_project ON bc_daily_command_briefs(company_id,project_id,id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_daily_review_expiry ON bc_daily_brief_reviews(expires_at)')
            return True
        except Exception:
            log.exception('Daily briefing schema setup failed')
            return False

    def actor(self, c, pid, write=False):
        self.require(self.schema_ready, 'Daily briefing setup is unavailable. Check the release installation.', 503)
        return self.field.actor(c, pid, write)

    def insert(self, c, table, columns, values):
        self.require(table in {'bc_daily_brief_reviews','bc_daily_command_briefs'}, 'Unsupported briefing operation.', 400)
        sql = f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:
            return int(c.execute(sql+' RETURNING id', tuple(values)).fetchone()['id'])
        return int(c.execute(sql, tuple(values)).lastrowid)

    def priorities(self, pid):
        unavailable = {'available':False, 'headline':'Project priorities are unavailable. Review Project analysis before acting on this briefing.',
                       'morning':[], 'midday':[], 'closeout':[]}
        try:
            engine = self.ns.get('_bc200_brain')
            if not callable(engine):
                return unavailable
            result = engine(pid)
            if not result or int((result.get('project') or {}).get('id') or 0) != pid:
                log.warning('Daily brief rejected missing or mismatched priority context project_id=%s', pid)
                return unavailable
            output = {'available':True, 'headline':str(result.get('headline') or 'Review the connected project priorities below.')}
            for key in ('morning','midday','closeout'):
                output[key] = [{field:item.get(field) for field in ACTION_FIELDS} for item in list(result.get(key) or [])[:3]]
            return output
        except Exception:
            log.exception('Daily brief priority calculation failed project_id=%s', pid)
            return unavailable

    def collect(self, c, user, project):
        pid, cid = int(project['id']), int(user['company_id'])
        totals, matched, rows = self.ns['_bc860_rows'](c, user, pid, 'attention', '', 1)
        attention = [{k:row.get(k) for k in ATTENTION_FIELDS} for row in rows]
        scope_total = c.execute("SELECT COUNT(*) AS n FROM bc_shared_work WHERE company_id=? AND project_id=? AND kind='scope' AND revoked_at IS NULL", (cid,pid)).fetchone()['n']
        scopes = [dict(r) for r in c.execute('''SELECT s.id,s.title,s.version,s.due_date,s.state,s.allow_response,
            COALESCE(NULLIF(u.display_name,''),u.email,'Removed account') AS recipient_name,
            (SELECT status FROM bc_shared_work_updates WHERE share_id=s.id AND share_version=s.version ORDER BY id DESC LIMIT 1) AS latest_status
            FROM bc_shared_work s LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id
            WHERE s.company_id=? AND s.project_id=? AND s.kind='scope' AND s.revoked_at IS NULL
            ORDER BY s.id DESC LIMIT 20''', (cid,pid)).fetchall()]
        result = {'project':{'id':pid, 'name':str(project['name']), 'number':str(project.get('number') or '')},
                  'company_id':cid, 'brief_date':self.field.now().date().isoformat(),
                  'totals':totals, 'attention_total':matched, 'attention':attention,
                  'scope_total':scope_total, 'scopes':scopes, 'priorities':self.priorities(pid)}
        result['latest_analysis'] = None
        if 'blueprint_runs' in self.ns['_bc800_table_names']():
            run = c.execute("SELECT id,created FROM blueprint_runs WHERE company_id=? AND project_id=? AND UPPER(status) IN ('COMPLETE','COMPLETED','SUCCESS') ORDER BY id DESC LIMIT 1",(cid,pid)).fetchone()
            result['latest_analysis'] = dict(run) if run else None
        photos = getattr(self.ns['app'].state, 'photo_field', None)
        if photos is not None:
            result['photo_actions'] = photos.brief_data(c,user,project)
        rfis = getattr(self.ns['app'].state, 'rfi_field', None)
        if rfis is not None:
            result['rfi_directions'] = rfis.brief_data(c,user,project)
        # Lists are deliberately bounded. Counts always cover all current shares.
        return result

    def material_hash(self, data):
        return digest(packed({k:v for k,v in data.items() if k not in {'leader_notes','prepared_at','prepared_by'}}))

    def status_label(self, row):
        if row['state']=='CLOSED': return 'Responses closed'
        status = row.get('latest_status')
        if status: return self.ns['_BC850_STATUSES'].get(status,'Update received')
        return 'Awaiting acknowledgment' if row.get('allow_response') else 'Replies disabled'

    def plan_html(self, data):
        p = data['priorities']
        body = '<section class="card'+(' warning' if not p['available'] else '')+'"><h2>Today’s project priorities</h2><p>'+esc(p['headline'])+'</p>'
        if p['available']:
            body += '<div class="grid">'
            for key,label in [('morning','Morning'),('midday','Midday'),('closeout','Closeout')]:
                body += '<div><h3>'+label+'</h3>'
                for action in p[key]:
                    body += '<article><p><strong>'+esc(action.get('title'))+'</strong><br>'+esc(action.get('recommended_action') or action.get('reason'))+'</p>'
                    refs = ' · '.join(str(action.get(k) or '') for k in ('source_type','source_id','trade','due') if action.get(k))
                    if refs: body += '<p class="small">Source / responsibility: '+esc(refs)+'</p>'
                    body += '</article>'
                if not p[key]: body += '<p class="small">No actions listed in this block.</p>'
                body += '</div>'
            body += '</div>'
        return body+'</section>'

    def snapshot_html(self, data, links=False):
        run = data.get('latest_analysis')
        body = '<p class="small">Latest completed Blueprint analysis: '+('run #'+str(run['id'])+' · '+esc(run['created']) if run else 'none recorded')+'. Issued scopes retain their reviewed source versions.</p>'
        body += '<section class="card"><h2>Team attention</h2><p>'
        t = data['totals']
        body += f'{t["blocked"]} blocked · {t["overdue"]} overdue · {t["review"]} awaiting review · {t["ready"]} ready for review</p>'
        body += '<p class="small">Counts cover all current shared work and may overlap. Due dates use the UTC calendar.</p>'
        if data['attention_total'] > len(data['attention']):
            body += f'<p class="warning">Showing the first {len(data["attention"])} of {data["attention_total"]} attention items, ordered by urgency. Open Command for the remaining work.</p>'
        for row in data['attention']:
            title = esc(row['title'])
            if links: title = f'<a href="/workspace/sharing/{row["id"]}">'+title+'</a>'
            body += '<article><h3>'+title+'</h3><p>'+esc(row['recipient_name'])+' · '+esc(row.get('latest_status') or 'Awaiting update')+(' · Due '+esc(row['due_date']) if row['due_date'] else '')+'</p>'
            if row['latest_message']: body += '<blockquote>'+esc(row['latest_message'])+'</blockquote>'
            if row['pending_id'] and row['pending_id'] != row['latest_id']:
                body += '<p><strong>Earlier unread update</strong></p><blockquote>'+esc(row['pending_message'])+'</blockquote>'
            if row['pending_count']: body += '<p class="small">'+str(row['pending_count'])+' unread update(s)</p>'
            body += '</article>'
        if not data['attention']: body += '<p>No shared-work items currently need attention.</p>'
        body += '</section>'+self.plan_html(data)
        body += '<section class="card"><h2>Issued trade scopes</h2><p>'+str(data['scope_total'])+' current publication(s).</p>'
        if data['scope_total'] > len(data['scopes']):
            body += f'<p class="small">Showing the newest {len(data["scopes"])} publications. Open Trade sharing for the rest.</p>'
        if data['scopes']:
            body += '<div class="table-wrap"><table><thead><tr><th>Scope</th><th>Subcontractor</th><th>Status</th><th>Due</th></tr></thead><tbody>'
            for row in data['scopes']:
                title = esc(row['title'])
                if links: title = f'<a href="/workspace/sharing/{row["id"]}">'+title+'</a>'
                body += '<tr><td>'+title+'</td><td>'+esc(row['recipient_name'])+'</td><td>'+esc(self.status_label(row))+'</td><td>'+esc(row['due_date'] or 'Not set')+'</td></tr>'
            body += '</tbody></table></div>'
        else: body += '<p>No trade scopes have been issued on this project.</p>'
        body += '</section>'
        if data.get('photo_actions') is not None:
            body += self.ns['app'].state.photo_field.brief_html(data['photo_actions'],links)
        if data.get('rfi_directions') is not None:
            body += self.ns['app'].state.rfi_field.brief_html(data['rfi_directions'],links)
        if data.get('leader_notes'):
            body += '<section class="card"><h2>Superintendent’s notes</h2><p style="white-space:pre-wrap">'+esc(data['leader_notes'])+'</p></section>'
        return body

    def page(self, title, body):
        return self.field.page(title, CSS+'<div class="bc880">'+body+'</div>')

    def panel(self, user, pid):
        body = '<section class="bc860-panel"><h2>Daily field briefing</h2><p>Review project priorities, issued scopes and subcontractor blockers together.</p>'
        body += f'<div class="bc860-tools"><a class="bc860-button" href="/workspace/command/projects/{pid}/brief">Review today’s brief</a><a class="bc860-button secondary" href="/workspace/command/projects/{pid}/briefs">Saved briefs</a><a class="bc860-button secondary" href="/workspace/scopes?project_id={pid}">Review &amp; publish trade scopes</a><a class="bc860-button secondary" href="/photo-ai?project_id={pid}">Photo findings &amp; actions</a><a class="bc860-button secondary" href="/workspace/rfi-answers?project_id={pid}">RFI answers to field</a></div>'
        for key,label in [('blueprint','Blueprint Brain'),('photo','Analyze a photo'),('brief','AI Morning Brief'),('daily','Daily report')]:
            body += self.field.tool_form(pid,key,label)
        body += '</section>'
        try:
            with self.db() as c:
                user, project = self.actor(c,pid)
                photos = getattr(self.ns['app'].state, 'photo_field', None)
                if photos is not None:
                    body += photos.brief_html(photos.brief_data(c,user,project),True)
                rfis = getattr(self.ns['app'].state, 'rfi_field', None)
                if rfis is not None:
                    body += rfis.brief_html(rfis.brief_data(c,user,project),True)
                # The interactive queue below already provides shared-item detail.
                # Avoid calculating its totals again just to render the toolbar.
                latest = c.execute('SELECT id,brief_date,created_at FROM bc_daily_command_briefs WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1',(user['company_id'],pid)).fetchone()
                if latest:
                    body += '<section class="bc860-panel"><h2>Last saved field briefing</h2><p>'+esc(latest['brief_date'])+' · Saved '+esc(latest['created_at'])+' UTC</p><a href="/workspace/command/briefs/'+str(latest['id'])+'">Open saved copy</a></section>'
            body += CSS+'<div class="bc880">'+self.plan_html({'priorities':self.priorities(pid)})+'</div>'
        except Exception:
            log.exception('Daily Command panel unavailable project_id=%s',pid)
            body += '<section class="bc860-panel"><p>The briefing summary is temporarily unavailable. You can still review shared-work updates below.</p></section>'
        return body

    def compose(self, project_id:int, draft_notes:str=''):
        self.require(len(draft_notes)<=6000, 'Keep briefing notes within 6,000 characters.',400)
        with self.db() as c:
            user, project = self.actor(c,project_id)
            data = self.collect(c,user,project)
        body = '<h1>Review today’s brief</h1><p>'+esc(project['name'])+' · '+esc(data['brief_date'])+' UTC</p><p>Check the current records below and add the direction your team needs today.</p>'
        body += self.snapshot_html(data,True)
        body += f'<form class="card" method="post" action="/workspace/command/projects/{project_id}/brief/review"><input type="hidden" name="source_hash" value="{self.material_hash(data)}"><label for="leader-notes"><strong>Superintendent’s notes</strong></label><p class="small">Add ownership, follow-up times, inspections or today’s field direction. These notes remain in the internal briefing.</p><textarea id="leader-notes" name="leader_notes" rows="5" maxlength="6000">'+esc(draft_notes)+'</textarea><p><button>Preview daily briefing</button></p></form>'
        body += f'<p><a href="/workspace/command?project_id={project_id}">Back to Command</a></p>'
        return self.page('Review daily briefing',body)

    def review(self, project_id:int, request:Request, source_hash:str=Form(...), leader_notes:str=Form('')):
        self.require(bool(re.fullmatch(r'[0-9a-f]{64}',source_hash)), 'Reload the briefing before reviewing it.',400)
        leader_notes = leader_notes.strip()
        self.require(len(leader_notes)<=6000,'Keep the briefing notes within 6,000 characters.',400)
        with self.db(True) as c:
            user, project = self.actor(c,project_id,True)
            data = self.collect(c,user,project)
            self.require(secrets.compare_digest(source_hash,self.material_hash(data)), 'The displayed project records changed. Reload the briefing and review the current information.',409)
            data.update(leader_notes=leader_notes,prepared_at=self.field.now().isoformat(),prepared_by=user.get('display_name') or user['email'])
            token = secrets.token_urlsafe(32)
            now = self.field.now()
            c.execute('DELETE FROM bc_daily_brief_reviews WHERE expires_at<?',(now.isoformat(),))
            self.insert(c,'bc_daily_brief_reviews','token_hash,session_hash,actor_user_id,company_id,project_id,source_hash,snapshot_json,expires_at',
                        (digest(token),self.field.session_hash(request),user['id'],user['company_id'],project_id,source_hash,packed(data),(now+timedelta(minutes=15)).isoformat()))
        body = '<h1>Preview daily briefing</h1><p>'+esc(project['name'])+' · '+esc(data['brief_date'])+' UTC</p>'+self.snapshot_html(data)
        body += '<form class="card" method="post" action="/workspace/command/briefs/save"><input type="hidden" name="review_token" value="'+token+'"><p><label><input type="checkbox" name="confirmed" value="yes" required> I reviewed this briefing, its listed limitations and my field notes.</label></p><p>This saves the exact copy above. It does not send a message or change the project’s work records.</p><button>Save reviewed briefing</button></form>'
        body += f'<p><a href="/workspace/command/projects/{project_id}/brief">Back to current records</a></p>'
        return self.page('Preview daily briefing',body)

    def save(self, request:Request, review_token:str=Form(...), confirmed:str=Form('')):
        self.require(confirmed=='yes','Review and confirm the briefing before saving it.',400)
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}',review_token)), 'This preview is invalid. Review the briefing again.',403)
        with self.db(True) as c:
            row = c.execute('SELECT * FROM bc_daily_brief_reviews WHERE token_hash=?',(digest(review_token),)).fetchone()
            self.require(row is not None,'This preview is unavailable or expired. Review the briefing again.',409)
            preview = dict(row)
            user, project = self.actor(c,preview['project_id'],True)
            self.require(user['id']==preview['actor_user_id'] and user['company_id']==preview['company_id'] and secrets.compare_digest(self.field.session_hash(request),preview['session_hash']), 'Review this briefing from your own signed-in session.',403)
            row = c.execute('SELECT * FROM bc_daily_brief_reviews WHERE id=?'+self.field.lock,(preview['id'],)).fetchone()
            self.require(row is not None,'This preview expired. Review the briefing again.',409)
            preview = dict(row)
            self.require(not preview['consumed_at'] and preview['expires_at']>self.field.now().isoformat(),'This preview expired or was already saved. Review the briefing again.',409)
            current = self.collect(c,user,project)
            self.require(secrets.compare_digest(self.material_hash(current),preview['source_hash']), 'The displayed project records changed after your preview. Review the current briefing before saving.',409)
            data = json.loads(preview['snapshot_json'])
            now = self.field.now().isoformat()
            bid = self.insert(c,'bc_daily_command_briefs','company_id,project_id,brief_date,source_hash,snapshot_json,created_by,created_at',
                              (user['company_id'],project['id'],data['brief_date'],preview['source_hash'],preview['snapshot_json'],user['id'],now))
            self.ns['_bc850_event'](c,user,project['id'],None,'DAILY_BRIEF_SAVED:'+str(bid))
            c.execute('UPDATE bc_daily_brief_reviews SET consumed_at=? WHERE id=?',(now,preview['id']))
        return RedirectResponse('/workspace/command/briefs/'+str(bid),status_code=303)

    def saved(self,c,brief_id):
        user = self.ns['_bc850_actor'](c)
        row = c.execute('SELECT * FROM bc_daily_command_briefs WHERE id=? AND company_id=?',(brief_id,user['company_id'])).fetchone()
        self.require(row is not None,'This saved briefing is unavailable.',404)
        self.actor(c,row['project_id'])
        return dict(row),json.loads(row['snapshot_json'])

    def history(self, project_id:int, before_id:int=0):
        self.require(before_id>=0,'Choose a valid history page.',400)
        with self.db() as c:
            user, project = self.actor(c,project_id)
            rows = c.execute('SELECT id,brief_date,created_at FROM bc_daily_command_briefs WHERE company_id=? AND project_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 26', (user['company_id'],project_id,before_id,before_id)).fetchall()
        body = '<h1>Saved daily briefings</h1><p>'+esc(project['name'])+'</p>'
        for row in rows[:25]:
            body += '<article class="card"><h2><a href="/workspace/command/briefs/'+str(row['id'])+'">'+esc(row['brief_date'])+' · Brief #'+str(row['id'])+'</a></h2><p>Saved '+esc(row['created_at'])+' UTC</p></article>'
        if not rows: body += f'<div class="card"><p>No daily field briefings have been saved for this project yet.</p><p>Existing analysis and earlier briefs remain in <a href="/workspace/command/projects/{project_id}/analysis">Project analysis</a>.</p></div>'
        if len(rows)>25: body += f'<p><a href="/workspace/command/projects/{project_id}/briefs?before_id={rows[24]["id"]}">Older briefings</a></p>'
        body += f'<p><a href="/workspace/command/projects/{project_id}/brief">Review today’s brief</a> · <a href="/workspace/command?project_id={project_id}">Back to Command</a></p>'
        return self.page('Saved daily briefings',body)

    def saved_body(self,row,data):
        return '<h1>Daily field briefing</h1><p><strong>'+esc(data['project']['name'])+'</strong> · '+esc(data['brief_date'])+' UTC</p><p class="small">Brief #'+str(row['id'])+' · Prepared by '+esc(data.get('prepared_by'))+' · Saved '+esc(row['created_at'])+' UTC</p><p class="small">Saved copy of the reviewed records. Later project updates do not rewrite this briefing.</p>'+self.snapshot_html(data)

    def detail(self,brief_id:int):
        with self.db() as c: row,data = self.saved(c,brief_id)
        body = '<div class="actions"><a class="button" href="/workspace/command/briefs/'+str(brief_id)+'/print">Print / Save PDF</a><a href="/workspace/command/briefs/'+str(brief_id)+'/download">Download text</a></div>'+self.saved_body(row,data)
        body += f'<p><a href="/workspace/command/projects/{row["project_id"]}/briefs">Saved briefs</a> · <a href="/workspace/command?project_id={row["project_id"]}">Back to Command</a></p>'
        return self.page('Saved daily briefing',body)

    def print_view(self,brief_id:int):
        with self.db() as c: row,data = self.saved(c,brief_id)
        body = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="same-origin"><title>BuildCommand AI · Daily briefing</title>'+CSS+'</head><body><main class="bc880"><p class="no-print">Use your browser’s Print command to print or save as PDF. <a href="/workspace/command/briefs/'+str(brief_id)+'">Return to briefing</a></p><p><strong>BuildCommand AI</strong></p>'+self.saved_body(row,data)+'</main></body></html>'
        return HTMLResponse(body,headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin'})

    def download(self,brief_id:int):
        with self.db() as c: row,data = self.saved(c,brief_id)
        t=data['totals']
        lines=['BuildCommand AI — Daily field briefing',data['project']['name'],data['brief_date']+' UTC',f'Brief #{brief_id} · Saved {row["created_at"]} UTC',f'Prepared by: {data.get("prepared_by","")}', '', 'TEAM ATTENTION',f'{t["blocked"]} blocked; {t["overdue"]} overdue; {t["review"]} awaiting review; {t["ready"]} ready for review.', 'Counts cover all current shared work and may overlap.',f'Showing {len(data["attention"])} of {data["attention_total"]} attention items.']
        run=data.get('latest_analysis')
        lines.insert(5,'Latest completed Blueprint analysis: '+(f'run #{run["id"]} · {run["created"]}' if run else 'none recorded'))
        for item in data['attention']:
            lines += ['',str(item['title']),str(item['recipient_name'])+' · '+str(item['latest_status'] or 'Awaiting update'),'Due: '+str(item['due_date'] or 'Not set'),str(item['latest_message'] or '')]
            if item['pending_id'] and item['pending_id']!=item['latest_id']: lines += ['Earlier unread update: '+str(item['pending_message'])]
        lines += ['', 'PROJECT PRIORITIES',data['priorities']['headline']]
        for key in ('morning','midday','closeout'):
            lines.append(key.upper())
            for action in data['priorities'][key]:
                lines += [str(action.get('title') or ''),str(action.get('recommended_action') or action.get('reason') or ''),'Source: '+' · '.join(str(action.get(k)) for k in ('source_type','source_id','trade','due') if action.get(k))]
        lines += ['', 'ISSUED TRADE SCOPES',f'Showing {len(data["scopes"])} of {data["scope_total"]} current publications.']
        for item in data['scopes']: lines.append(str(item['title'])+' · '+str(item['recipient_name'])+' · '+self.status_label(item)+' · Due '+str(item['due_date'] or 'Not set'))
        photos = data.get('photo_actions')
        if photos is not None:
            lines += ['', 'OPEN PHOTO ACTIONS', str(photos['total'])+' open correction(s) or RFI(s).']
            if photos['total']>len(photos['items']):
                lines.append('Showing newest '+str(len(photos['items']))+' of '+str(photos['total'])+'.')
            for row in photos['items']:
                lines.append(row['title']+' | '+row['mode']+' | '+row['state']+' | Due: '+(row['due_date'] or 'Not set')+' | Photo updates: '+str(row['evidence_count']))
        rfis = data.get('rfi_directions')
        if rfis is not None:
            lines += ['', 'RFI FIELD DIRECTIONS', str(rfis['total'])+' current publication(s); '+str(rfis['needs_review'])+' source review(s) needed.']
            if rfis['total']>len(rfis['items']):
                lines.append('Showing '+str(len(rfis['items']))+' of '+str(rfis['total'])+'.')
            for row in rfis['items']:
                state = 'Source needs review' if row['needs_review'] else row['latest_status'] or row['state']
                lines.append(row['title']+' | '+row['recipient_name']+' | '+state+' | Due: '+(row['due_date'] or 'Not set'))
        lines += ['', 'SUPERINTENDENT NOTES',data.get('leader_notes') or 'None recorded.', '', 'Fixed reviewed copy. Later project updates do not rewrite this briefing.']
        return Response('\n'.join(lines)+'\n',media_type='text/plain; charset=utf-8',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':f'attachment; filename="daily-command-{brief_id}-{data["brief_date"]}.txt"'})

    def health(self):
        checks={'brief_schema_initialized':self.schema_ready}
        for method,path,endpoint in self.routes:
            routes=[r for r in self.ns['app'].routes if getattr(r,'path','')==path and method in (getattr(r,'methods',None) or set())]
            checks[method+' '+path]=len(routes)==1 and routes[0].endpoint is endpoint
        checks['scope_publications_preserved']=self.field.schema_ready and self.field.version=='8.7.0'
        checks['daily_command_panel_installed']=getattr(self.ns['app'].state,'daily_command',None) is self
        checks['form_origin_guard_preserved']=callable(self.ns.get('_bc861_same_origin'))
        try:
            with self.db() as c:
                c.execute('SELECT token_hash,session_hash,source_hash,consumed_at FROM bc_daily_brief_reviews WHERE 1=0')
                c.execute('SELECT id,company_id,project_id,snapshot_json FROM bc_daily_command_briefs WHERE 1=0')
            checks['schema_readable']=True
        except Exception:
            log.exception('Daily command health schema check failed')
            checks['schema_readable']=False
        return {'app':'BuildCommand AI','version':VERSION,'release':RELEASE,'status':'ok' if all(checks.values()) else 'degraded','checks':checks,'passed':sum(checks.values()),'total':len(checks),'data_reset':False,'scope':'Schema and active route checks only. Verify real project roles, briefing review/save and downloads on staging.'}

    def register(self):
        for method,path,handler in [
            ('GET','/workspace/command/projects/{project_id}/brief',self.compose),
            ('POST','/workspace/command/projects/{project_id}/brief/review',self.review),
            ('POST','/workspace/command/briefs/save',self.save),
            ('GET','/workspace/command/projects/{project_id}/briefs',self.history),
            ('GET','/workspace/command/briefs/{brief_id}',self.detail),
            ('GET','/workspace/command/briefs/{brief_id}/print',self.print_view),
            ('GET','/workspace/command/briefs/{brief_id}/download',self.download)]:
            endpoint=self.ns['_bc850_endpoint'](handler)
            self.ns['app'].add_api_route(path,endpoint,methods=[method])
            self.routes.append((method,path,endpoint))
        self.ns['app'].add_api_route('/health/daily-command-8-8-0',self.health,methods=['GET'])
        self.rt.PUBLIC_PATHS.add('/health/daily-command-8-8-0')
