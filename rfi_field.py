"""8.10.0: reviewed RFI answer excerpts issued as versioned field directions.

The source RFI is read-only here. Publications contain only explicitly reviewed
text. Replacements reset acknowledgment for that recipient and retain history.
Source changes are detected on reads and before every non-blocker response.
"""
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response
from blueprint_field import digest, esc, packed

VERSION = '8.10.0'
RELEASE = 'RFI Answers to Field Work'
log = logging.getLogger('buildcommand.rfi_field')
CSS = '''<style>.bc8100-text{white-space:pre-wrap;overflow-wrap:anywhere}.bc8100-form{max-width:900px}
.bc8100-form label{display:block;margin-top:18px}.bc8100-form input:not([type=checkbox]),.bc8100-form textarea,.bc8100-form select{width:100%;box-sizing:border-box}
.bc8100-form button{margin-top:18px}.bc8100-warning{border-left:4px solid #c6942c;padding:16px;background:#fff4da;color:#513a17}
.bc8100-answer{white-space:pre-wrap;overflow-wrap:anywhere;border-left:3px solid #1b4d78;margin:16px 0;padding:16px;background:#eef4fa}</style>'''


def clean(value):
    return str(value or '').replace('\r\n','\n').replace('\r','\n').strip()


def install(ns):
    service = RfiField(ns)
    ns['app'].state.rfi_field = service
    ns['_BC850_SOURCES']['rfi_answer'] = ('RFI field direction','project_issues','title','due')
    service.register()
    return service


class RfiField:
    def __init__(self, ns):
        self.ns, self.rt = ns, ns['_runtime']
        self.field = ns['app'].state.blueprint_field
        self.db, self.require = self.field.db, self.field.require
        self.version, self.routes = VERSION, []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_rfi_answer_reviews(
                    id {key},token_hash TEXT NOT NULL UNIQUE,session_hash TEXT NOT NULL,
                    actor_user_id BIGINT NOT NULL,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    issue_id BIGINT NOT NULL,source_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,consumed_at TEXT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_rfi_answer_publications(
                    id {key},share_id BIGINT NOT NULL,share_version INTEGER NOT NULL,
                    company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,issue_id BIGINT NOT NULL,
                    source_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(share_id,share_version))''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_rfi_answer_reviews_expiry ON bc_rfi_answer_reviews(expires_at)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_rfi_answer_publications_project ON bc_rfi_answer_publications(company_id,project_id,issue_id)')
            return True
        except Exception:
            log.exception('RFI field schema setup failed')
            return False

    def actor(self,c,pid,write=False):
        self.require(self.schema_ready,'RFI field setup is unavailable. Check the release installation.',503)
        return self.field.actor(c,pid,write)

    def insert(self,c,table,columns,values):
        self.require(table in {'bc_rfi_answer_reviews','bc_rfi_answer_publications'},'Unsupported RFI operation.',400)
        sql = f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:
            return int(c.execute(sql+' RETURNING id',tuple(values)).fetchone()['id'])
        return int(c.execute(sql,tuple(values)).lastrowid)

    def source(self,c,user,pid,iid,lock=False):
        row = c.execute('SELECT id,title,description,response,status,issue_type FROM project_issues WHERE id=? AND project_id=?'+(self.field.lock if lock else ''),(iid,pid)).fetchone()
        project = c.execute('SELECT id FROM projects WHERE id=? AND company_id=?',(pid,user['company_id'])).fetchone()
        self.require(row is not None and project is not None and clean(row['issue_type']).upper()=='RFI','This RFI is unavailable in this project.',404)
        row = dict(row)
        source = {'issue_id':iid,'title':clean(row['title']),'question':clean(row['description']),
                  'answer':clean(row['response']),'status':clean(row['status']).upper(),
                  'control_id':None,'number':'','eligible':True,'reason':''}
        tables = self.ns['_bc800_table_names']()
        if {'rfi_issue_links','rfi_control'}.issubset(tables):
            links = c.execute('SELECT rfi_id FROM rfi_issue_links WHERE issue_id=? AND company_id=? AND project_id=?'+(self.field.lock if lock else ''),(iid,user['company_id'],pid)).fetchall()
            if len(links)>1:
                source.update(eligible=False,reason='Several controlled RFIs link to this issue. Ask your project administrator to reconcile the links.')
            elif links:
                control = c.execute('SELECT id,number,title,question,answer,status FROM rfi_control WHERE id=? AND company_id=? AND project_id=?'+(self.field.lock if lock else ''),(links[0]['rfi_id'],user['company_id'],pid)).fetchone()
                source['control_id'] = links[0]['rfi_id']
                if control is None:
                    source.update(eligible=False,reason='The linked controlled RFI is unavailable. Reconcile it before issuing field direction.')
                else:
                    source['number'] = clean(control['number'])
                    aligned = all(clean(control[k])==source[target] for k,target in [('title','title'),('question','question'),('answer','answer')])
                    if not aligned or clean(control['status']).upper() not in {'ANSWERED','CLOSED'}:
                        source.update(eligible=False,reason='The linked RFI and issue are not synchronized with an answered record. Update the answer in RFI Control, then reopen this review.')
        legacy = self.ns.get('_bc181831_canopy10_legacy')
        authoritative = self.ns.get('_bc181831_canopy10_authoritative')
        if callable(legacy) and callable(authoritative) and legacy(row):
            candidates = c.execute('SELECT id,title,description FROM project_issues WHERE project_id=?',(pid,)).fetchall()
            if any(authoritative(dict(r)) for r in candidates):
                source.update(eligible=False,reason='This legacy RFI is superseded. Use the authoritative RFI shown in Issues.')
        if source['status'] not in {'ANSWERED','CLOSED'} or not source['answer']:
            source.update(eligible=False,reason='Record the verified response and mark the RFI Answered before issuing field direction.')
        return source

    def fingerprint(self,source):
        # Closing an answered RFI without changing its content is not a revision.
        return digest(packed({k:source[k] for k in ('issue_id','title','question','answer','control_id','number','eligible')}))

    def list_sources(self,c,user,pid,source_id=None,query=''):
        sql = "SELECT id,title,due AS due_date FROM project_issues WHERE project_id=? AND UPPER(COALESCE(issue_type,''))='RFI'"
        args = [pid]
        self.require(c.execute('SELECT id FROM projects WHERE id=? AND company_id=?',(pid,user['company_id'])).fetchone() is not None,'This project is unavailable.',403)
        if source_id is not None: sql+=' AND id=?';args.append(source_id)
        elif query: sql+=' AND LOWER(title) LIKE LOWER(?)';args.append('%'+query[:120]+'%')
        rows = [dict(r) for r in c.execute(sql+' ORDER BY id DESC LIMIT 100',tuple(args)).fetchall()]
        if source_id is not None:
            self.require(bool(rows),'This RFI is unavailable in this project.',404)
            return rows[0]
        return rows

    def page(self,title,body):
        return self.field.page(title,CSS+body)

    def source_form(self,pid,iid):
        return self.field.tool_form(pid,'rfi-source-'+str(iid),'Open source RFI / record answer')

    def tool_destination(self,pid,tool):
        match = re.fullmatch(r'rfi-source-(\d+)',tool)
        if not match:return None
        with self.db() as c:
            user,_ = self.actor(c,pid)
            source = self.source(c,user,pid,int(match[1]))
        return '/project-control/rfis' if source['control_id'] else '/issues/'+str(source['issue_id'])+'/edit'

    def home(self,project_id:int=0,q:str=''):
        self.require(len(q)<=120,'Shorten your search to 120 characters.',400)
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            projects = self.ns['_bc850_projects'](c,user)
            if not project_id:
                row = c.execute('SELECT selected_project_id FROM user_state WHERE user_id=?',(user['id'],)).fetchone()
                project_id = int(row['selected_project_id'] or 0) if row else 0
            if not project_id:
                return self.page('RFI answers to field','<h1>Choose a project</h1>'+(''.join(f'<p><a href="/workspace/rfi-answers?project_id={p["id"]}">{esc(p["name"])}</a></p>' for p in projects) or '<p>Ask your company administrator to appoint you to a project.</p>'))
            user,project = self.actor(c,project_id)
            rows = self.list_sources(c,user,project_id,query=q)
            summary = self.brief_data(c,user,project)
        body = '<div class="hero"><div class="eyebrow">ANSWER TO ACTION</div><h1>RFI answers to field</h1><p>'+esc(project['name'])+' · Review the answer, issue clear direction, and track the trade’s response.</p></div>'
        body += self.brief_html(summary,True)
        body += f'<form class="card" method="get"><input type="hidden" name="project_id" value="{project_id}"><label for="rfi-search">Find an RFI</label> <input id="rfi-search" name="q" value="{esc(q)}" maxlength="120"> <button>Find</button></form><section class="card"><h2>Project RFIs</h2><p class="small">Newest 100 matching records. Open a record to verify its answer and linked control status.</p>'
        for row in rows:
            body += f'<p><a href="/workspace/rfi-answers/{row["id"]}/prepare?project_id={project_id}">#{row["id"]} · {esc(row["title"])}</a></p>'
        body += ('<p>No RFIs found. Create a question through the existing RFIs / issues or photo findings workflow.</p>' if not rows else '')+'</section>'
        body += f'<p><a href="/workspace/command?project_id={project_id}">Back to Command</a></p>'
        return self.page('RFI answers to field',body)

    def prepare(self,issue_id:int,project_id:int):
        with self.db() as c:
            user,project = self.actor(c,project_id)
            source = self.source(c,user,project_id,issue_id)
            table = self.ns['_bc850_member_table']()
            rows = c.execute(f'SELECT DISTINCT u.id,u.email,u.display_name,u.role FROM users u JOIN {table} m ON m.user_id=u.id WHERE m.project_id=? AND u.company_id=? ORDER BY u.email',(project_id,user['company_id'])).fetchall()
            recipients = [dict(r) for r in rows if self.ns['_bc840_tier'](dict(r))=='trade']
        body = '<div class="hero"><h1>Review RFI answer</h1><p>'+esc(project['name'])+' · Issue #'+str(issue_id)+' · '+esc(source['status'])+'</p><h2>'+esc(source['title'])+'</h2></div>'
        body += self.source_form(project_id,issue_id)
        body += '<section class="card"><h2>Source question · Internal review</h2><div class="bc8100-text">'+esc(source['question'])+'</div><h2>Recorded answer · Internal review</h2><div class="bc8100-text">'+esc(source['answer'] or 'No response recorded.')+'</div></section>'
        if not source['eligible']:
            return self.page('Review RFI answer',body+'<p class="bc8100-warning">'+esc(source['reason'])+'</p>')
        if not recipients:
            return self.page('Review RFI answer',body+f'<p class="card"><a href="/workspace/sharing/projects/{project_id}/team">Assign a subcontractor to this project</a> before issuing direction.</p>')
        excerpt = source['answer'] if len(source['answer'])<=8000 else ''
        body += f'<form class="card bc8100-form" method="post" action="/workspace/rfi-answers/{issue_id}/review"><h2>Prepare field direction</h2><input type="hidden" name="project_id" value="{project_id}"><input type="hidden" name="source_hash" value="{self.fingerprint(source)}"><label for="rfi-recipient">Assigned subcontractor</label><select id="rfi-recipient" name="recipient_user_id" required><option value="">Choose a person</option>'+''.join(f'<option value="{r["id"]}">{esc(r["display_name"] or r["email"])} · {esc(r["email"])}</option>' for r in recipients)+'</select>'
        body += '<label for="rfi-title">Shared title</label><input id="rfi-title" name="title" maxlength="240" required value="'+esc(source['title'][:240])+'"><label for="rfi-excerpt">Answer excerpt to share</label><textarea id="rfi-excerpt" name="answer_excerpt" rows="6" maxlength="8000" required>'+esc(excerpt)+'</textarea><p class="small">Keep the relevant passage exactly as recorded above. Remove unrelated text before previewing. Put your field interpretation in the instructions below.</p>'
        body += '<label for="rfi-context">Context to share (optional)</label><textarea id="rfi-context" name="context" rows="3" maxlength="2000"></textarea><label for="rfi-direction">Superintendent’s field instructions</label><textarea id="rfi-direction" name="instructions" rows="4" maxlength="6000" required placeholder="What should this subcontractor do, where, and what must they confirm?"></textarea><label for="rfi-reference">Verified answer / drawing reference (optional)</label><input id="rfi-reference" name="reference" maxlength="500"><label for="rfi-due">Due date (optional)</label><input id="rfi-due" name="due_date" type="date"><p>The preview shows everything this recipient receives. Publishing a replacement requires a new acknowledgment.</p><button>Preview field direction</button></form>'
        return self.page('Review RFI answer',body)

    def previous(self,c,user,pid,iid,uid):
        row = c.execute('''SELECT s.id,s.version,s.revoked_at,s.state,s.title,s.message,s.due_date,
            (SELECT MAX(id) FROM bc_shared_work_updates WHERE share_id=s.id AND share_version=s.version) AS latest_update_id
            FROM bc_shared_work s WHERE s.company_id=? AND s.project_id=? AND s.kind='rfi_answer' AND s.source_id=? AND s.recipient_user_id=?''',(user['company_id'],pid,iid,uid)).fetchone()
        return dict(row) if row else None

    def snapshot_html(self,data):
        body = '<section class="card"><h2>'+esc(data['title'])+'</h2><p><strong>For:</strong> '+esc(data['recipient_name'])+' · Due '+esc(data['due_date'] or 'Not set')+'</p><p class="small">Source RFI / issue #'+str(data['issue_id'])+(' · '+esc(data['number']) if data.get('number') else '')+'</p>'
        if data['context']:body += '<h3>Shared context</h3><div class="bc8100-text">'+esc(data['context'])+'</div>'
        body += '<h3>Issued answer excerpt</h3><blockquote class="bc8100-answer">'+esc(data['answer_excerpt'])+'</blockquote><h3>Superintendent’s field instructions</h3><div class="bc8100-text">'+esc(data['instructions'])+'</div>'
        if data['reference']:body += '<p><strong>Verified reference:</strong> '+esc(data['reference'])+'</p>'
        return body+'<p class="small">Reviewed by '+esc(data['reviewed_by'])+'. This publication does not attach drawings, change the project schedule or approve cost changes.</p></section>'

    def review(self,issue_id:int,request:Request,project_id:int=Form(...),source_hash:str=Form(...),recipient_user_id:int=Form(...),title:str=Form(...),answer_excerpt:str=Form(...),instructions:str=Form(...),context:str=Form(''),reference:str=Form(''),due_date:str=Form('')):
        title,answer_excerpt,instructions,context,reference,due_date = map(clean,(title,answer_excerpt,instructions,context,reference,due_date))
        self.require(bool(re.fullmatch('[0-9a-f]{64}',source_hash)),'Reopen the RFI review.',400)
        self.require(0<len(title)<=240 and 0<len(answer_excerpt)<=8000 and 0<len(instructions)<=6000 and len(context)<=2000 and len(reference)<=500,'Enter the answer excerpt, title and instructions within the displayed limits.',400)
        if due_date:
            try:
                self.require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',due_date)),'Enter a valid due date.',400)
                datetime.strptime(due_date,'%Y-%m-%d')
            except ValueError:self.require(False,'Enter a valid due date.',400)
        with self.db(True) as c:
            user,project = self.actor(c,project_id,True)
            source = self.source(c,user,project_id,issue_id,True)
            self.require(source['eligible'],source['reason'],409)
            self.require(secrets.compare_digest(source_hash,self.fingerprint(source)),'The RFI changed. Reopen and review its current answer.',409)
            self.require(answer_excerpt in source['answer'],'The excerpt must be an exact passage from the recorded answer. Put your interpretation in the field instructions.',400)
            recipient = self.ns['_bc850_recipient'](c,user,project_id,recipient_user_id)
            snapshot = dict(issue_id=issue_id,number=source['number'],title=title,answer_excerpt=answer_excerpt,instructions=instructions,context=context,reference=reference,due_date=due_date,recipient_name=recipient['display_name'] or recipient['email'],reviewed_by=user['display_name'] or user['email'])
            previous = self.previous(c,user,project_id,issue_id,recipient_user_id)
            previous_copy = None
            if previous:
                share = self.ns['_bc850_share'](c,user,previous['id'],True)
                previous_copy = json.loads(self.publication(c,share)['snapshot_json'])
                comparison = {k:v for k,v in snapshot.items() if k!='reviewed_by'}
                old = {k:v for k,v in previous_copy.items() if k!='reviewed_by'}
                self.require(previous['revoked_at'] is not None or comparison!=old or self.publication(c,share)['source_hash']!=source_hash,'This exact direction is already issued to that person. Open the current share instead.',409)
            token = secrets.token_urlsafe(32); now=self.field.now()
            c.execute('DELETE FROM bc_rfi_answer_reviews WHERE expires_at<?',(now.isoformat(),))
            payload = {'snapshot':snapshot,'project':project,'recipient':recipient,'previous':previous}
            self.insert(c,'bc_rfi_answer_reviews','token_hash,session_hash,actor_user_id,company_id,project_id,issue_id,source_hash,payload_json,expires_at',
                (digest(token),self.field.session_hash(request),user['id'],user['company_id'],project_id,issue_id,source_hash,packed(payload),(now+timedelta(minutes=15)).isoformat()))
        body = '<div class="hero"><h1>Preview field direction</h1><p>'+esc(project['name'])+'</p></div>'
        if previous_copy:
            body += '<div class="bc8100-warning"><strong>Replace the current direction for this recipient.</strong><p>The previous issued copy will remain in manager history. This recipient must acknowledge the new version.</p></div><details class="card"><summary>Previous issued copy</summary>'+self.snapshot_html(previous_copy)+'</details>'
        body += self.snapshot_html(snapshot)
        body += '<form class="card" method="post" action="/workspace/rfi-answers/publish"><input type="hidden" name="review_token" value="'+token+'"><p><label><input type="checkbox" name="confirmed" value="yes" required> I reviewed this answer excerpt, the field instructions and the named recipient.'+(' Replace their previous issued copy.' if previous else '')+'</label></p><p>Only the proposed issued copy is shared. Internal RFI notes and other answer text stay private.</p><button>'+('Publish replacement' if previous else 'Publish field direction')+'</button></form>'
        return self.page('Preview RFI field direction',body)

    def publish(self,request:Request,review_token:str=Form(...),confirmed:str=Form('')):
        self.require(confirmed=='yes','Review and confirm the field direction first.',400)
        self.require(bool(re.fullmatch('[A-Za-z0-9_-]{40,80}',review_token)),'Reopen the RFI review.',403)
        with self.db(True) as c:
            user=self.ns['_bc850_actor'](c)
            row=c.execute('SELECT * FROM bc_rfi_answer_reviews WHERE token_hash=? AND company_id=?',(digest(review_token),user['company_id'])).fetchone()
            self.require(row is not None,'This preview is unavailable.',403)
            user,project=self.actor(c,row['project_id'],True)
            row=dict(c.execute('SELECT * FROM bc_rfi_answer_reviews WHERE id=?'+self.field.lock,(row['id'],)).fetchone())
            self.require(row['actor_user_id']==user['id'] and secrets.compare_digest(row['session_hash'],self.field.session_hash(request)),'Review this direction in the same signed-in session.',403)
            self.require(not row['consumed_at'] and row['expires_at']>self.field.now().isoformat(),'This preview was used or expired. Review the direction again.',409)
            source=self.source(c,user,project['id'],row['issue_id'],True)
            self.require(source['eligible'] and secrets.compare_digest(row['source_hash'],self.fingerprint(source)),'The source RFI changed or is no longer ready to issue. Review it again.',409)
            payload=json.loads(row['payload_json']);snapshot=payload['snapshot']
            recipient=self.ns['_bc850_recipient'](c,user,project['id'],payload['recipient']['id'])
            self.require(project==payload['project'] and recipient==payload['recipient'],'The project or recipient changed. Review the direction again.',409)
            previous=self.previous(c,user,project['id'],row['issue_id'],recipient['id'])
            self.require(previous==payload['previous'],'The existing share or a trade response changed. Review the replacement again.',409)
            now=self.field.now().isoformat()
            if previous:
                sid,version=previous['id'],previous['version']+1
                c.execute("UPDATE bc_shared_work SET title=?,message=?,due_date=?,allow_response=1,version=?,revoked_at=NULL,state='OPEN',created_by=?,updated_at=? WHERE id=?",(snapshot['title'],snapshot['instructions'],snapshot['due_date'],version,user['id'],now,sid))
            else:
                version=1
                sid=self.ns['_bc850_insert'](c,'bc_shared_work','company_id,project_id,kind,source_id,recipient_user_id,title,message,due_date,allow_response,created_by,created_at,updated_at',
                    (user['company_id'],project['id'],'rfi_answer',row['issue_id'],recipient['id'],snapshot['title'],snapshot['instructions'],snapshot['due_date'],1,user['id'],now,now))
            self.insert(c,'bc_rfi_answer_publications','share_id,share_version,company_id,project_id,issue_id,source_hash,snapshot_json,created_by,created_at',
                (sid,version,user['company_id'],project['id'],row['issue_id'],row['source_hash'],packed(snapshot),user['id'],now))
            self.ns['_bc850_event'](c,user,project['id'],sid,'RFI_ANSWER_PUBLISHED:'+str(version))
            c.execute('UPDATE bc_rfi_answer_reviews SET consumed_at=? WHERE id=?',(now,row['id']))
        return RedirectResponse(f'/workspace/sharing/{sid}',status_code=303)

    def publication(self,c,share):
        row=c.execute('''SELECT * FROM bc_rfi_answer_publications WHERE share_id=? AND company_id=? AND project_id=?
            AND issue_id=? AND share_version<=? ORDER BY share_version DESC LIMIT 1''',(share['id'],share['company_id'],share['project_id'],share['source_id'],share['version'])).fetchone()
        self.require(row is not None,'This issued RFI direction is unavailable.',409)
        return dict(row)

    def current_hash(self,c,cid,pid,iid,lock=False):
        try:
            source=self.source(c,{'company_id':cid},pid,iid,lock)
            return self.fingerprint(source) if source['eligible'] else None
        except self.ns['_BC850_Problem'] as exc:
            if exc.status not in {403,404,409}:raise
            return None

    def changed(self,c,share,publication=None,lock=False):
        publication=publication or self.publication(c,share)
        return self.current_hash(c,share['company_id'],share['project_id'],share['source_id'],lock) != publication['source_hash']

    def validate_response(self,c,share,status):
        if status!='BLOCKED':
            self.require(not self.changed(c,share,lock=True),'The source RFI changed. Ask your project leader to review and reissue the direction. You can still report a blocker.',409)

    def issued_html(self,c,share,manager=False):
        publication=self.publication(c,share)
        body=''
        if self.changed(c,share,publication):
            body+='<p class="bc8100-warning"><strong>Source RFI needs review.</strong> The RFI changed or is unavailable since this direction was issued. Confirm with the project leader before acting. A new reviewed publication is required for acknowledgment or progress updates; you can still report a blocker.</p>'
        body+=self.snapshot_html(json.loads(publication['snapshot_json']))
        prefix='/workspace/sharing/' if manager else '/workspace/shared/'
        body+='<section class="card"><p>Issued version '+str(publication['share_version'])+' · '+esc(publication['created_at'])+' UTC</p>'
        if not share['revoked_at']:body+=f'<p><a href="{prefix}{share["id"]}/answer.txt">Download issued direction</a></p>'
        if manager:
            body+=f'<p><a href="/workspace/rfi-answers/{share["source_id"]}/prepare?project_id={share["project_id"]}">Review current answer / prepare replacement</a></p>'
            rows=c.execute('SELECT share_version,created_at FROM bc_rfi_answer_publications WHERE share_id=? AND company_id=? AND project_id=? ORDER BY share_version DESC',(share['id'],share['company_id'],share['project_id'])).fetchall()
            body+='<details><summary>Issued history</summary>'+''.join(f'<p><a href="/workspace/sharing/{share["id"]}/answer-history/{r["share_version"]}">Issued version {r["share_version"]}</a> · {esc(r["created_at"])}</p>' for r in rows)+'</details>'
        return CSS+body+'</section>'

    def history(self,share_id:int,version:int):
        with self.db() as c:
            user=self.ns['_bc850_actor'](c)
            share=self.ns['_bc850_share'](c,user,share_id,True)
            self.require(share['kind']=='rfi_answer','This item has no RFI answer history.',404)
            row=c.execute('SELECT snapshot_json,created_at FROM bc_rfi_answer_publications WHERE share_id=? AND share_version=? AND company_id=? AND project_id=?',(share_id,version,user['company_id'],share['project_id'])).fetchone()
            self.require(row is not None,'Issued version not found.',404)
        return self.page('RFI direction history','<h1>Issued version '+str(version)+'</h1><p>Historical copy · '+esc(row['created_at'])+' UTC</p>'+self.snapshot_html(json.loads(row['snapshot_json'])))

    def download(self,share_id,manager=False):
        with self.db() as c:
            user=self.ns['_bc850_actor'](c)
            share=self.ns['_bc850_share'](c,user,share_id,manager)
            self.require(share['kind']=='rfi_answer' and not share['revoked_at'],'This issued direction is unavailable.',403)
            publication=self.publication(c,share)
            changed=self.changed(c,share,publication)
        data=json.loads(publication['snapshot_json'])
        lines=['BuildCommand AI — Issued RFI field direction',data['title'],f'RFI / issue #{data["issue_id"]}',f'Issued version {publication["share_version"]} · {publication["created_at"]} UTC',
            'Source status at download: '+('CHANGED OR UNAVAILABLE — contact the project leader before acting.' if changed else 'Matches the reviewed record.'),'Recipient: '+data['recipient_name'],'Due: '+(data['due_date'] or 'Not set'),
            '', 'SHARED CONTEXT',data['context'] or 'Not recorded.','','ISSUED ANSWER EXCERPT',data['answer_excerpt'],'','FIELD INSTRUCTIONS',data['instructions'],'','REFERENCE',data['reference'] or 'Not recorded.',
            '', 'Fixed issued copy. Check the app for later revisions before acting. Drawings are shared separately.']
        return Response('\n'.join(lines),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="rfi-direction-{share_id}-v{publication["share_version"]}.txt"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    def trade_download(self,share_id:int):return self.download(share_id)
    def manager_download(self,share_id:int):return self.download(share_id,True)

    def brief_data(self,c,user,project):
        rows=c.execute('''SELECT s.id,s.source_id,s.title,s.version,s.state,s.due_date,p.source_hash,
            COALESCE(NULLIF(u.display_name,''),u.email,'Removed account') AS recipient_name,
            (SELECT status FROM bc_shared_work_updates WHERE share_id=s.id AND share_version=s.version ORDER BY id DESC LIMIT 1) AS latest_status
            FROM bc_shared_work s LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id
            LEFT JOIN bc_rfi_answer_publications p ON p.id=(SELECT id FROM bc_rfi_answer_publications
              WHERE share_id=s.id AND company_id=s.company_id AND project_id=s.project_id AND issue_id=s.source_id AND share_version<=s.version ORDER BY share_version DESC LIMIT 1)
            WHERE s.company_id=? AND s.project_id=? AND s.kind='rfi_answer' AND s.revoked_at IS NULL
            ORDER BY s.id DESC''',(user['company_id'],project['id'])).fetchall()
        sources={};items=[]
        for raw in rows:
            row=dict(raw);iid=row['source_id']
            if iid not in sources:sources[iid]=self.current_hash(c,user['company_id'],project['id'],iid)
            published_hash=row.pop('source_hash')
            row['needs_review']=sources[iid] is None or sources[iid]!=published_hash
            items.append(row)
        items.sort(key=lambda r:(not r['needs_review'],-r['id']))
        return {'total':len(items),'needs_review':sum(r['needs_review'] for r in items),'open':sum(r['state']=='OPEN' for r in items),'items':items[:20]}

    def brief_html(self,data,links=False):
        body='<section class="card"><h2>RFI field directions</h2><p>'+str(data['total'])+' current publication(s) · '+str(data['open'])+' open · '+str(data['needs_review'])+' source review(s) needed.</p>'
        if data['total']>len(data['items']):body+=f'<p class="small">Showing {len(data["items"])} of {data["total"]}; changed sources first. Open Trade sharing for the remaining work.</p>'
        for row in data['items']:
            title=esc(row['title'])
            if links:title=f'<a href="/workspace/sharing/{row["id"]}">'+title+'</a>'
            state='Source needs review' if row['needs_review'] else ('Responses closed' if row['state']=='CLOSED' else row['latest_status'] or 'Awaiting acknowledgment')
            body+='<p><strong>'+title+'</strong><br>'+esc(row['recipient_name'])+' · '+esc(state)+' · Due '+esc(row['due_date'] or 'Not set')+'</p>'
        return body+'</section>'

    def health(self):
        checks={'rfi_answer_schema_initialized':self.schema_ready}
        for method,path,endpoint in self.routes:
            routes=[r for r in self.ns['app'].routes if getattr(r,'path','')==path and method in (getattr(r,'methods',None) or set())]
            checks[method+' '+path]=len(routes)==1 and routes[0].endpoint is endpoint
        checks['reviewed_answer_sharing_supported']='rfi_answer' in self.ns['_BC850_SOURCES']
        checks['daily_briefing_preserved']=self.ns['app'].state.daily_command.schema_ready
        checks['photo_actions_preserved']=self.ns['app'].state.photo_field.schema_ready
        checks['form_origin_guard_preserved']=callable(self.ns.get('_bc861_same_origin'))
        try:
            with self.db() as c:
                c.execute('SELECT token_hash,session_hash,consumed_at FROM bc_rfi_answer_reviews WHERE 1=0')
                c.execute('SELECT share_id,share_version,source_hash,snapshot_json FROM bc_rfi_answer_publications WHERE 1=0')
                c.execute('SELECT id,project_id,issue_type,status,response FROM project_issues WHERE 1=0')
            checks['schema_readable']=True
        except Exception:
            log.exception('RFI field health check failed');checks['schema_readable']=False
        return {'app':'BuildCommand AI','version':VERSION,'release':RELEASE,'status':'ok' if all(checks.values()) else 'degraded','checks':checks,'passed':sum(checks.values()),'total':len(checks),'data_reset':False,'scope':'Schema and active route checks only. Test answered RFI publishing, a revised answer, real trade access and a saved briefing on staging.'}

    def register(self):
        for method,path,handler in [
            ('GET','/workspace/rfi-answers',self.home),
            ('GET','/workspace/rfi-answers/{issue_id}/prepare',self.prepare),
            ('POST','/workspace/rfi-answers/{issue_id}/review',self.review),
            ('POST','/workspace/rfi-answers/publish',self.publish),
            ('GET','/workspace/sharing/{share_id}/answer-history/{version}',self.history),
            ('GET','/workspace/shared/{share_id}/answer.txt',self.trade_download),
            ('GET','/workspace/sharing/{share_id}/answer.txt',self.manager_download)]:
            endpoint=self.ns['_bc850_endpoint'](handler)
            self.ns['_bc840_replace'](path,method,endpoint)
            self.routes.append((method,path,endpoint))
        self.ns['app'].add_api_route('/health/rfi-to-field-8-10-0',self.health,methods=['GET'])
        self.rt.PUBLIC_PATHS.add('/health/rfi-to-field-8-10-0')
