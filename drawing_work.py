"""8.17.0: explicit job-record links and reviewed actions at drawing pins.

Cards reuse 8.16 pins; links confer no permissions or trade access. Ask reads
only the selected card and its live linked records, never claims to read a PDF.
"""
import logging
import re
import secrets
from datetime import timedelta

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from blueprint_field import esc, digest
from command_center import AskFailure, command_json
from drawing_register import js

VERSION = '8.17.1'
RELEASE = 'Full-Page Drawings'
BASE = '/workspace/drawing-sheets'
KINDS = {'rfi': ('project_issues', 'RFI'), 'submittal': ('submittals', 'Submittal'),
         'schedule': ('activities', 'Schedule activity'), 'photo': ('bc_photo_analyses', 'Photo analysis'),
         'document': ('attachments', 'Document')}
log = logging.getLogger('buildcommand.drawing_work')


def install(ns):
    service = DrawingWork(ns)
    ns['app'].state.drawing_work = service
    service.register()
    return service


class DrawingWork:
    def __init__(self, ns):
        self.ns = ns
        self.pins = ns['app'].state.drawing_field
        self.field = self.pins.field
        self.reg = self.pins.reg
        self.drawings = self.pins.drawings
        self.center = ns['app'].state.command_center
        self.actions = ns['app'].state.command_actions
        self.db = self.field.db
        self.require = self.field.require
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_work_links(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,
                    pin_id BIGINT NOT NULL,kind TEXT NOT NULL,record_id BIGINT NOT NULL,
                    created_by BIGINT NOT NULL,created TEXT NOT NULL,
                    UNIQUE(company_id,project_id,pin_id,kind,record_id))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_work_drafts(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,pin_id BIGINT NOT NULL,
                    created_by BIGINT NOT NULL,request_key TEXT NOT NULL,plan_id BIGINT NOT NULL,
                    created TEXT NOT NULL,UNIQUE(company_id,created_by,request_key))''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_drawing_work_pin ON bc_drawing_work_links(company_id,project_id,pin_id)')
            return True
        except Exception:
            log.exception('Drawing work card setup failed')
            return False

    def origin(self, request):
        self.require(self.ns['_bc840_same_origin'](request), 'Reload this drawing before submitting the form.', 403)

    def context(self, c, sheet_id, pin_id=None, lock=False):
        self.require(self.schema_ready, 'Drawing work cards are unavailable. Ask your administrator to check the installation.', 503)
        user, project, sheet = self.pins.context(c, sheet_id)
        user, project = self.field.actor(c, project['id'], lock)
        note = None
        if pin_id is not None:
            row = c.execute('''SELECT * FROM bc_drawing_pins WHERE id=? AND company_id=? AND project_id=?
                AND sheet_id=? AND (visibility='team' OR created_by=?)'''+(self.field.lock if lock else ''),
                (pin_id, user['company_id'], project['id'], sheet_id, user['id'])).fetchone()
            self.require(row is not None, 'This work card is unavailable.', 404)
            note = dict(row)
        return user, project, sheet, note

    def source(self, c, user, pid, kind, rid, required=True):
        self.require(kind in KINDS, 'Choose a supported project record.', 400)
        table = KINDS[kind][0]
        sql = f'SELECT t.* FROM {table} t JOIN projects p ON p.id=t.project_id WHERE t.id=? AND t.project_id=? AND p.company_id=?'
        args = [rid, pid, user['company_id']]
        if kind in {'photo', 'document'}:
            sql += ' AND t.company_id=?'
            args.append(user['company_id'])
        if kind == 'rfi':
            sql += " AND UPPER(COALESCE(t.issue_type,''))='RFI'"
        row = c.execute(sql, tuple(args)).fetchone()
        if row is None:
            if required:
                self.require(False, 'This record is unavailable in this project.', 404)
            return None
        r = dict(row)
        title = str(r.get('title') or r.get('name') or r.get('original_name') or KINDS[kind][1])[:240]
        fields = {'rfi': ('status', 'description', 'response', 'due'),
                  'submittal': ('status', 'notes', 'due_date'),
                  'schedule': ('status', 'start', 'finish', 'pct'),
                  'photo': ('result_text',), 'document': ('original_name',)}[kind]
        parts = [f'{k.replace("_", " ").capitalize()}: {r[k]}' for k in fields if r.get(k) not in (None, '')]
        detail = '\n'.join(parts)[:4000] or 'No additional details recorded.'
        if kind == 'photo':
            detail = 'AI observations; verify against the actual photo and field conditions.\n'+detail
        if kind == 'document':
            detail += '\nDocument title only. File contents have not been read for this answer.'
        return {'kind': kind, 'id': rid, 'title': title, 'detail': detail,
                'status': str(r.get('status') or 'Not recorded')[:100]}

    def links(self, c, user, project, sheet, note):
        rows = c.execute('''SELECT * FROM bc_drawing_work_links WHERE company_id=? AND project_id=?
            AND sheet_id=? AND pin_id=? ORDER BY id LIMIT 30''',
            (user['company_id'], project['id'], sheet['id'], note['id'])).fetchall()
        return [{**dict(r), 'record': self.source(c, user, project['id'], r['kind'], r['record_id'], False)} for r in rows]

    def capture(self, c, sheet_id, pin_id, lock=False):
        user, project, sheet, note = self.context(c, sheet_id, pin_id, lock)
        links = self.links(c, user, project, sheet, note)
        snapshot = {'sheet': sheet, 'note': note, 'links': links, 'head': self.pins.head(c, user, sheet)}
        return user, project, snapshot, digest(command_json(snapshot))

    def card_url(self, sid, pin_id):
        return f'{BASE}/{sid}/work/{pin_id}'

    def panel(self, sid, pin_id, content, embedded=False, status=200):
        body = f'<div class="dw-card-content"><p><a href="{BASE}/{sid}?pin={pin_id}" target="_top">Back to drawing</a></p>'+content+'</div>'
        if embedded:
            html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Drawing work card</title>'+STYLE+'</head><body class="dw-embedded">'+body+'</body></html>'
            return HTMLResponse(html, status_code=status, headers={'Cache-Control':'private, no-store', 'Referrer-Policy':'same-origin', 'X-Frame-Options':'SAMEORIGIN'})
        response = self.reg.page('Drawing work card', STYLE+body)
        response.status_code = status
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    def card(self, sheet_id:int, pin_id:int, embedded:bool=False):
        with self.db() as c:
            user, project, snap, stamp = self.capture(c, sheet_id, pin_id)
            drafts = c.execute('''SELECT d.plan_id,p.state FROM bc_drawing_work_drafts d
                JOIN bc_command_action_plans p ON p.id=d.plan_id AND p.company_id=d.company_id AND p.project_id=d.project_id
                WHERE d.company_id=? AND d.project_id=? AND d.pin_id=? AND d.created_by=? ORDER BY d.id DESC LIMIT 5''',
                (user['company_id'], project['id'], pin_id, user['id'])).fetchall()
        note = snap['note']; sheet = snap['sheet']; url = self.card_url(sheet_id,pin_id)
        body = f'<div class="dw-eyebrow">{esc(sheet["sheet_number"])} · {esc(sheet["revision_label"] or "Original")}</div><h1>{esc(note["title"])}</h1>'
        body += '<p class="dw-tag">'+('Only me' if note['visibility']=='private' else 'Project team')+' · '+esc(note['status'].capitalize())+'</p><p class="dw-note">'+esc(note['body'])+'</p>'
        if snap['head'] != sheet_id:
            body += '<p class="dw-warning">Previous drawing revision. Review the current sheet before preparing new direction. Links stay with this revision.</p>'
        body += '<h2>Connected records</h2>'
        if not snap['links']:
            body += '<p>No records linked yet. Attach the RFI, photo analysis or other record that belongs to this location.</p>'
        for link in snap['links']:
            r = link['record']
            body += f'<div class="dw-record" id="record-{link["id"]}"><strong>'+esc(KINDS[link['kind']][1])+' · '+esc(r['title'] if r else 'Record unavailable')+'</strong>'
            if r:
                body += '<p class="dw-muted">'+esc(r['status'])+'</p><details><summary>Read recorded details</summary><p class="dw-note">'+esc(r['detail'])+'</p></details>'
                body += f'<form method="post" action="{url}/records/{link["id"]}/open" target="_top"><button>Open record</button></form>'
            body += f'<details><summary>Remove link</summary><form method="post" action="{url}/records/{link["id"]}/remove"><input type="hidden" name="embedded" value="{str(embedded).lower()}"><p>The original record stays in the project.</p><button>Remove this link</button></form></details></div>'
        body += f'<p><a class="dw-button" href="{url}/records'+('?embedded=true' if embedded else '')+'">Attach records</a></p>'
        body += '<p class="dw-muted">Links organize internal work. They do not share a file or authorize a design change.</p>'
        if note['visibility']=='team' and note['status']=='OPEN' and snap['head']==sheet_id:
            body += '<h2>Prepare the next step</h2><p>Choose a subcontractor and briefing date in the review.</p>'
            body += f'<form method="post" action="{url}/prepare" target="_top"><input type="hidden" name="expected_hash" value="{stamp}"><input type="hidden" name="request_key" value="{secrets.token_urlsafe(24)}"><button class="dw-primary">Prepare notice &amp; follow-up</button></form>'
            body += self.ask_form(url,embedded)
        elif note['visibility']=='private':
            body += '<p class="dw-warning">This is a private note. To prepare team direction, create a Project team work card with only the information you want to share.</p>'
        for d in drafts:
            body += f'<p><a href="/workspace/command/actions/{d["plan_id"]}" target="_top">Your prepared action · {esc(d["state"])}</a></p>'
        body += f'<details><summary>{"Close" if note["status"]=="OPEN" else "Reopen"} work card</summary><form method="post" action="{BASE}/{sheet_id}/notes/{pin_id}/status" target="_top"><input type="hidden" name="expected" value="{note["status"]}"><button name="status" value="{"CLOSED" if note["status"]=="OPEN" else "OPEN"}">{"Close" if note["status"]=="OPEN" else "Reopen"} card</button></form><p class="dw-muted">This only changes this card. It does not close an RFI or complete a field action.</p></details>'
        return self.panel(sheet_id,pin_id,body,embedded)

    def records(self,sheet_id:int,pin_id:int,kind:str='rfi',q:str='',embedded:bool=False):
        self.require(kind in KINDS,'Choose a supported record type.',400)
        q=q.strip()[:120]
        with self.db() as c:
            user,project,sheet,note=self.context(c,sheet_id,pin_id)
            table=KINDS[kind][0]
            name='name' if kind=='schedule' else 'original_name' if kind=='photo' else 'title'
            sql=f'SELECT t.id FROM {table} t JOIN projects p ON p.id=t.project_id WHERE t.project_id=? AND p.company_id=?'
            args=[project['id'],user['company_id']]
            if kind in {'photo','document'}:sql+=' AND t.company_id=?';args.append(user['company_id'])
            if kind=='rfi':sql+=" AND UPPER(COALESCE(t.issue_type,''))='RFI'"
            if q:sql+=f" AND LOWER(COALESCE(t.{name},'')) LIKE LOWER(?)";args.append('%'+q+'%')
            rows=c.execute(sql+' ORDER BY t.id DESC LIMIT 51',tuple(args)).fetchall()
            choices=[self.source(c,user,project['id'],kind,r['id']) for r in rows[:50]]
            linked={(r['kind'],r['record_id']) for r in self.links(c,user,project,sheet,note)}
        url=self.card_url(sheet_id,pin_id)
        body='<h1>Attach records</h1><p>'+esc(note['title'])+'</p><p>Select records that actually belong to this location. Existing links and records are kept.</p>'
        carry=f'<input type="hidden" name="embedded" value="{str(embedded).lower()}">'
        body+=f'<form method="get" action="{url}/records">'+carry+'<label>Record type<select name="kind">'+''.join(f'<option value="{k}"'+(' selected' if k==kind else '')+'>'+v[1]+'</option>' for k,v in KINDS.items())+'</select></label><label>Find by title<input name="q" maxlength="120" value="'+esc(q)+'"></label><button>Find records</button></form>'
        body+=f'<form method="post" action="{url}/records">'+carry
        for r in choices:
            if (kind,r['id']) in linked:body+='<p>'+esc(r['title'])+' · Attached</p>'
            else:body+=f'<label class="dw-choice"><input type="checkbox" name="records" value="{kind}:{r["id"]}"> {esc(r["title"])}<small>{esc(r["status"])}</small></label>'
        if not choices:body+='<p>No matching records in this project.</p>'
        if len(rows)>50:body+='<p>Showing the newest 50 matches. Narrow your search to find an older record.</p>'
        if choices:body+='<button class="dw-primary">Attach selected records</button>'
        body+=f'</form><p><a href="{url}'+('?embedded=true' if embedded else '')+'">Back to work card</a></p>'
        return self.panel(sheet_id,pin_id,body,embedded)

    def attach(self,sheet_id:int,pin_id:int,request:Request,records:list[str]=Form(default=[]),embedded:bool=Form(False)):
        self.origin(request)
        self.require(0<len(records)<=30,'Select between 1 and 30 records.',400)
        parsed=set()
        for value in records:
            parts=value.split(':')
            self.require(len(parts)==2 and parts[0] in KINDS and bool(re.fullmatch(r'[0-9]{1,18}',parts[1])),'Choose records from the list.',400)
            parsed.add((parts[0],int(parts[1])))
        with self.db(True) as c:
            user,project,sheet,note=self.context(c,sheet_id,pin_id,True)
            existing={(r['kind'],r['record_id']) for r in self.links(c,user,project,sheet,note)}
            self.require(len(existing|parsed)<=30,'Keep at most 30 records on one work card.',400)
            for kind,rid in sorted(parsed):
                self.source(c,user,project['id'],kind,rid)
                if (kind,rid) not in existing:
                    self.reg.insert(c,'''INSERT INTO bc_drawing_work_links(company_id,project_id,sheet_id,pin_id,kind,record_id,created_by,created)
                        VALUES(?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],sheet_id,pin_id,kind,rid,user['id'],self.field.now().isoformat()))
                    self.ns['_bc850_event'](c,user,project['id'],None,f'DRAWING_WORK_LINK:{pin_id}:{kind}:{rid}')
        return RedirectResponse(self.card_url(sheet_id,pin_id)+('?embedded=true' if embedded else ''),303)

    def remove(self,sheet_id:int,pin_id:int,link_id:int,request:Request,embedded:bool=Form(False)):
        self.origin(request)
        with self.db(True) as c:
            user,project,sheet,note=self.context(c,sheet_id,pin_id,True)
            row=c.execute('SELECT id FROM bc_drawing_work_links WHERE id=? AND company_id=? AND project_id=? AND pin_id=? AND sheet_id=?',
                (link_id,user['company_id'],project['id'],pin_id,sheet_id)).fetchone()
            if row:
                c.execute('DELETE FROM bc_drawing_work_links WHERE id=?',(row['id'],))
                self.ns['_bc850_event'](c,user,project['id'],None,f'DRAWING_WORK_UNLINK:{pin_id}:{link_id}')
        return RedirectResponse(self.card_url(sheet_id,pin_id)+('?embedded=true' if embedded else ''),303)

    def open_record(self,sheet_id:int,pin_id:int,link_id:int,request:Request):
        self.origin(request)
        with self.db(True) as c:
            user,project,sheet,note=self.context(c,sheet_id,pin_id,True)
            link=next((r for r in self.links(c,user,project,sheet,note) if r['id']==link_id),None)
            self.require(link and link['record'],'This linked record is unavailable.',404)
            r=link['record'];rid=r['id']
            destination={'rfi':f'/issues/{rid}','submittal':f'/submittals/{rid}/brain','schedule':'/schedule',
                'photo':f'/workspace/photos/{rid}','document':f'/documents/{rid}/view'}[r['kind']]
            self.ns['app'].state.daily_reports.select(c,user,project['id'])
        return RedirectResponse(destination,303)

    def prepare(self,sheet_id:int,pin_id:int,request:Request,expected_hash:str=Form(...),request_key:str=Form(...)):
        self.origin(request)
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{24,80}',request_key)) and bool(re.fullmatch(r'[0-9a-f]{64}',expected_hash)), 'Open the work card again before preparing direction.',400)
        with self.db(True) as c:
            user,project,snap,stamp=self.capture(c,sheet_id,pin_id,True)
            self.field.session_hash(request)
            previous=c.execute('SELECT pin_id,plan_id FROM bc_drawing_work_drafts WHERE company_id=? AND created_by=? AND request_key=?',
                (user['company_id'],user['id'],request_key)).fetchone()
            if previous:
                self.require(previous['pin_id']==pin_id,'Open this work card again.',409)
                return RedirectResponse('/workspace/command/actions/'+str(previous['plan_id']),303)
            self.require(secrets.compare_digest(stamp,expected_hash),'This card or a linked record changed. Open it again and review the current information.',409)
            note=snap['note'];sheet=snap['sheet']
            self.require(note['visibility']=='team' and note['status']=='OPEN','Prepare direction from an open Project team work card.',409)
            self.require(snap['head']==sheet_id,'Open the current drawing revision before preparing direction.',409)
            self.require(all(r['record'] is not None for r in snap['links']),'Remove unavailable record links before preparing direction.',409)
            self.require(self.actions.schema_ready,'Reviewed actions are unavailable. Ask your administrator to check this installation.',503)
            source=next((r['record'] for r in snap['links'] if r['kind'] in {'rfi','submittal','schedule'}),None)
            source_value=f'{source["kind"]}:{source["id"]}' if source else f'project:{project["id"]}'
            linked='; '.join(KINDS[r['kind']][1]+' #'+str(r['record_id'])+' '+r['record']['title'] for r in snap['links'])
            heading=f'{sheet["sheet_number"]} · {sheet["revision_label"] or "Original"} · {note["title"]}'
            message=(heading+'\n\nField note: '+note['body']+'\n\nPlease review this item and confirm the next step. Verify against approved project direction before proceeding.')[:5500]
            brief=(heading+'\n'+note['body']+'\nRelated records: '+(linked or 'None linked.')+'\nDrawing work card: '+self.card_url(sheet_id,pin_id))[:6000]
            context=self.center.context(c,user,project);now=self.field.now().isoformat()
            payload={'notice':True,'brief':True,'source':source_value,'recipient_user_id':0,'title':heading[:240],
                'message':message,'briefing_note':brief,'followup_date':(self.field.now().date()+timedelta(days=1)).isoformat(),'channel':'in_app'}
            plan_id=self.actions.insert(c,'bc_command_action_plans','company_id,project_id,created_by,request_key,question,answer,context_hash,context_json,payload_json,created_at,updated_at',
                (user['company_id'],project['id'],user['id'],'DW-'+request_key,'Prepare direction from this drawing work card.',brief,digest(command_json(context)),command_json(context),command_json(payload),now,now))
            self.reg.insert(c,'''INSERT INTO bc_drawing_work_drafts(company_id,project_id,pin_id,created_by,request_key,plan_id,created)
                VALUES(?,?,?,?,?,?,?)''',(user['company_id'],project['id'],pin_id,user['id'],request_key,plan_id,now))
            self.ns['_bc850_event'](c,user,project['id'],None,f'DRAWING_WORK_DRAFT:{pin_id}:{plan_id}')
        return RedirectResponse('/workspace/command/actions/'+str(plan_id),303)

    def ask_form(self,url,embedded,question='What needs to happen next for this work?'):
        return f'<details class="dw-ask"'+(' open' if question!='What needs to happen next for this work?' else '')+f'><summary>Ask about this work</summary><p class="dw-muted">Uses this team note and its linked records. Drawing graphics and document contents are not read here.</p><form method="post" action="{url}/ask"><input type="hidden" name="embedded" value="{str(embedded).lower()}"><label>Your question<textarea name="question" maxlength="1500" required rows="3">'+esc(question)+'</textarea></label><button>Ask BuildCommand</button></form></details>'

    def ask_context(self,project,snap):
        note=snap['note'];sheet=snap['sheet']
        evidence=[{'key':'E1','kind':'Human field note','record_id':note['id'],'title':note['title'],
            'detail':note['body'],'path':self.card_url(sheet['id'],note['id'])}]
        for r in snap['links']:
            if r['record']:
                record=r['record']
                evidence.append({'key':'E'+str(len(evidence)+1),'kind':KINDS[r['kind']][1],'record_id':r['record_id'],
                    'title':record['title'],'detail':record['detail'],
                    'path':self.card_url(sheet['id'],note['id'])+f'#record-{r["id"]}'})
        return {'project':{'id':project['id'],'name':project['name']},'drawing':{k:sheet[k] for k in ('sheet_number','title','revision_label','page_number','area')},
            'source_coverage':'Only this human-entered team work card and its explicitly linked records. The link asserts relevance, not correctness or approval. No drawing geometry, PDF contents, image pixels, unrelated project records or private notes were read. State what is missing; do not infer a conflict, measurement, approved design, inspection result or schedule delay from a title or missing data.',
            'unavailable_records':[{'kind':KINDS[r['kind']][1],'id':r['record_id']} for r in snap['links'] if not r['record']],
            'evidence':evidence}

    def ask(self,sheet_id:int,pin_id:int,request:Request,question:str=Form(...),embedded:bool=Form(False)):
        self.origin(request);question=question.strip()
        self.require(0<len(question)<=1500,'Ask a question within 1,500 characters.',400)
        with self.db() as c:
            user,project,snap,stamp=self.capture(c,sheet_id,pin_id)
            self.require(snap['note']['visibility']=='team','Private notes are not sent to Ask.',403)
            self.require(snap['head']==sheet_id,'Ask from the current drawing revision.',409)
            identity=(user['id'],user['company_id']);context=self.ask_context(project,snap)
        result=None;failure=None
        try:result=self.center.run_answer(question,context)
        except AskFailure as exc:failure=exc
        with self.db() as c:
            fresh,project,current,current_stamp=self.capture(c,sheet_id,pin_id)
            self.require((fresh['id'],fresh['company_id'])==identity,'Your access changed. Sign in again.',403)
            self.require(current_stamp==stamp,'This card or its records changed while the answer was prepared. Open the card and ask again.',409)
        url=self.card_url(sheet_id,pin_id)
        if failure:
            body='<h1>Your question is saved below</h1><p>'+esc(failure.message)+'</p><p class="dw-muted">Reference: '+esc(failure.reference)+'</p>'+self.ask_form(url,embedded,question)
            return self.panel(sheet_id,pin_id,body,embedded,failure.status)
        body='<div class="dw-eyebrow">Ask BuildCommand · Work card</div><h1>'+esc(question)+'</h1><p class="dw-note">'+esc(result['answer'])+'</p>'
        selected=[e for e in context['evidence'] if e['key'] in result.get('evidence_ids',[])]
        if not selected:body+='<p class="dw-warning">No supporting record was identified for this answer. Check the suggestion before acting.</p>'
        if result.get('checks'):body+='<h2>Check before acting</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in result['checks'])+'</ul>'
        body+='<h2>Records behind the answer</h2>'+''.join('<details class="dw-record"><summary>'+esc(e['kind']+' · '+e['title'])+'</summary><p class="dw-note">'+esc(e['detail'])+'</p></details>' for e in selected)
        body+='<p class="dw-muted">Based on recorded text, not a visual reading of this drawing. Nothing was sent or changed.</p>'+f'<a class="dw-button" href="{url}'+('?embedded=true' if embedded else '')+'">Review work card and next step</a>'+self.ask_form(url,embedded,question)
        return self.panel(sheet_id,pin_id,body,embedded)

    def viewer(self,sheet_id:int,request:Request):
        response=self.previous_viewer(sheet_id,request)
        if response.status_code!=200:return response
        with self.db() as c:
            user,project,sheet=self.pins.context(c,sheet_id)
            if not self.ns['_bc850_manager'](user):return self.full_page(response,sheet_id)
            notes=self.pins.pins(c,user,sheet)
        html=response.body.decode('utf-8')
        # Fit the actual viewport, including a short landscape screen or a
        # large sheet. Keep normalized markup coordinates and calibrated units.
        html=html.replace('Math.max(320,wrap.clientWidth-24),ah=Math.max(320,wrap.clientHeight-24)',
                          'Math.max(1,wrap.clientWidth-24),ah=Math.max(1,wrap.clientHeight-24)')
        html=html.replace('zoom=Math.max(.25,Math.min(3,Math.min(aw/',
                          'zoom=Math.max(.01,Math.min(3,Math.min(aw/')
        html=html.replace('zoom=Math.max(.5,zoom-.25)', 'zoom=Math.max(.01,zoom/1.25)')
        html=html.replace('zoom=Math.min(3,zoom+.25)', 'zoom=Math.min(3,zoom*1.25)')
        # Existing pin/render/markup behavior remains in its own module. Its
        # extension event opens the card without navigating away from the sheet.
        html=html.replace("if(!picking)location.assign('/workspace/drawing-sheets/'+config.sheet_id+'/notes#pin-'+p.id);",
            "if(!picking){if(window.BC_DRAWING_WORK_OPEN)window.BC_DRAWING_WORK_OPEN(p.id);else location.assign('/workspace/drawing-sheets/'+config.sheet_id+'/notes#pin-'+p.id);}")
        html=html.replace("dirty=false;dialog.close();location.reload();", "dirty=false;dialog.close();const saved=await response.json();location.assign('/workspace/drawing-sheets/'+config.sheet_id+'?pin='+saved.id);")
        html=html.replace("target.scrollIntoView({block:'center',inline:'center'});target.focus({preventScroll:true});", "if(window.innerWidth>1100||!window.BC_DRAWING_WORK_OPEN){target.scrollIntoView({block:'center',inline:'center'});target.focus({preventScroll:true});}")
        html=html.replace('>Pin a note</button>','>Add work card</button>')
        # Keep older help/test text discoverable while making the main action plain.
        html=html.replace('<p id="df-position"></p>','<p id="df-position"></p><p class="df-help">Pin a note to start a work card. Then attach its project records.</p>')
        html=html.replace('>Field notes <span>','>All field notes <span>')
        controls='<button type="button" id="dw-toggle" aria-expanded="true">Work cards</button>'
        html=html.replace('<nav class="df-actions" aria-label="Drawing actions">','<nav class="df-actions" aria-label="Drawing actions">'+controls,1)
        items=''.join(f'<button type="button" class="dw-card-pick" data-work-id="{n["id"]}"><span>{i+1} · {esc(n["title"])}</span><small>{"Only me" if n["visibility"]=="private" else "Team"} · {esc(n["status"].capitalize())}</small></button>' for i,n in enumerate(notes))
        aside='<aside id="dw-panel" aria-label="Drawing work cards"><div class="dw-panel-head"><strong>Work at this location</strong><button type="button" id="dw-close" aria-label="Close work cards">Close</button></div><div id="dw-list"><h2>What needs attention?</h2><p>Tap a pin on the drawing or choose a card below.</p>'+items+('' if notes else '<p>No work cards yet. Choose <b>Add work card</b>, then tap the drawing.</p>')+'</div><div id="dw-selected" hidden><button type="button" id="dw-show-list">All work cards</button><iframe id="dw-frame" title="Selected drawing work card"></iframe></div></aside>'
        html=html.replace('</head>',STYLE+VIEW_STYLE+'</head>',1)
        html=html.replace('</body>',aside+'<script>window.BC_DRAWING_WORK='+js({'sheet_id':sheet_id,'pins':[n['id'] for n in notes]})+';</script><script>'+VIEW_JS+'</script></body>',1)
        return self.full_page(HTMLResponse(html,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'}),sheet_id)

    def full_page(self,response,sheet_id=None):
        """Presentation only: retain the existing canvas, handlers and forms."""
        html=response.body.decode('utf-8')
        def body(match):
            attrs=match[1]
            if 'class="' in attrs:attrs=attrs.replace('class="','class="dw-fullpage ',1)
            else:attrs+=' class="dw-fullpage"'
            return '<body'+attrs+' data-drawing-layout="full-page-8.17.1">'
        html=re.sub(r'<body([^>]*)>',body,html,count=1)
        html=html.replace('</head>',FULL_PAGE_STYLE+'</head>',1)
        html=html.replace('</body>','<script>'+FULL_PAGE_JS+'</script></body>',1)
        result=HTMLResponse(html,status_code=response.status_code,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})
        scale=getattr(self.ns['app'].state,'drawing_scale',None)
        return scale.decorate(result,sheet_id) if scale and sheet_id else result

    def extra_evidence(self,c,user,pid):
        rows=self.previous_evidence(c,user,pid)
        if not self.schema_ready or not self.ns['_bc850_manager'](user):return rows
        cards=c.execute('''SELECT n.id,n.sheet_id FROM bc_drawing_pins n JOIN bc_drawing_heads h
            ON h.sheet_id=n.sheet_id AND h.company_id=n.company_id AND h.project_id=n.project_id
            WHERE n.company_id=? AND n.project_id=? AND n.visibility='team' AND n.status='OPEN'
            AND EXISTS(SELECT 1 FROM bc_drawing_work_links l WHERE l.pin_id=n.id AND l.company_id=n.company_id AND l.project_id=n.project_id)
            ORDER BY n.id DESC LIMIT 20''',(user['company_id'],pid)).fetchall()
        for card in cards:
            _,project,snap,_=self.capture(c,card['sheet_id'],card['id'])
            records='; '.join(KINDS[r['kind']][1]+' #'+str(r['record_id'])+': '+(r['record']['title']+' ('+r['record']['status']+')' if r['record'] else 'Unavailable') for r in snap['links'])
            rows.append(('Drawing work card',card['id'],snap['note']['title'],
                'Human-linked records on '+snap['sheet']['sheet_number']+'. Relevance is entered by the project team; not a verified design finding. '+records[:1400],self.card_url(card['sheet_id'],card['id'])))
        return rows

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes if hasattr(r,'methods') for m in r.methods or []}
        checks={m+' '+p:active.get((p,m)) is f for m,p,f in self.routes}
        checks.update(work_card_schema_initialized=self.schema_ready,existing_pins_preserved=self.pins.schema_ready,
            reviewed_actions_preserved=self.actions.schema_ready,shared_drawing_access_preserved=('/workspace/shared/{share_id}/view','GET') in active,
            ask_transport_preserved=callable(self.center.request_response),form_origin_guard_preserved=callable(self.ns.get('_bc840_same_origin')),
            saved_markups_preserved=('/workspace/drawings/{attachment_id}/markups','POST') in active)
        try:
            with self.db() as c:
                c.execute('SELECT pin_id,kind,record_id FROM bc_drawing_work_links WHERE 1=0')
                c.execute('SELECT pin_id,plan_id FROM bc_drawing_work_drafts WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if ok else 'degraded',checks=checks,
            passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Installation and schema checks only. Test real pins, record links, access, Ask and reviewed notice/briefing actions on staging. Ask uses linked record text, not PDF/image analysis. No automatic sharing or email.'),status_code=200 if ok else 503)

    def full_page_health(self):
        import json
        result=json.loads(self.health().body)
        result['checks'].update(full_page_layout_installed=callable(self.full_page),
            panels_closed_by_default='show(false);' in VIEW_JS,
            fullscreen_control_configured='document.documentElement.requestFullscreen' in FULL_PAGE_JS,
            existing_canvas_preserved='dfp-canvas' in FULL_PAGE_JS)
        result.update(passed=sum(result['checks'].values()),total=len(result['checks']),
            scope='Installation and layout configuration checks only. Verify full-page fit, pan/zoom, markups, save controls, sheet/work-card panels and browser fullscreen on staging. No data reset or permission changes.')
        ok=all(result['checks'].values());result['status']='ok' if ok else 'degraded'
        return JSONResponse(result,status_code=200 if ok else 503)

    def register(self):
        self.previous_viewer=self.pins.viewer
        self.previous_evidence=self.pins.hub.extra_evidence
        self.pins.hub.extra_evidence=self.extra_evidence
        path=BASE+'/{sheet_id}/work/{pin_id}'
        routes=[('GET',BASE+'/{sheet_id}',self.viewer),('GET',path,self.card),('GET',path+'/records',self.records),
            ('POST',path+'/records',self.attach),('POST',path+'/records/{link_id}/remove',self.remove),
            ('POST',path+'/records/{link_id}/open',self.open_record),('POST',path+'/prepare',self.prepare),('POST',path+'/ask',self.ask)]
        for method,p,fn in routes:
            endpoint=self.drawings.endpoint(fn);self.ns['_bc840_replace'](p,method,endpoint);self.routes.append((method,p,endpoint))
            for service in (self.pins,self.reg):service.routes[:]=[(m,q,endpoint if (m,q)==(method,p) else f) for m,q,f in service.routes]
        health='/health/drawing-work-cards-8-17-0'
        self.ns['app'].add_api_route(health,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(health)
        health='/health/drawings-full-page-8-17-1'
        self.ns['app'].add_api_route(health,self.full_page_health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(health)


STYLE='''<style>
.dw-card-content{font:15px/1.55 Arial,sans-serif;color:#1b3042;max-width:900px;margin:auto;overflow-wrap:anywhere}.dw-card-content h1{font-size:26px;line-height:1.2;margin:12px 0}.dw-card-content h2{font-size:18px;margin:24px 0 10px}.dw-eyebrow{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:#825b17;font-weight:700}.dw-tag{font-size:12px;font-weight:700;color:#425d72}.dw-note{white-space:pre-wrap}.dw-muted{color:#516679;font-size:13px}.dw-warning{background:#fff3da;color:#6f4a0d;border-radius:8px;padding:12px}.dw-record{padding:14px 0;border-top:1px solid #d8e2e9}.dw-record strong{display:block}.dw-record p{margin:8px 0}.dw-card-content button,.dw-button{display:inline-block;box-sizing:border-box;min-height:44px;border:1px solid #b5c6d3;border-radius:8px;background:white;color:#193f5a;padding:10px 14px;font:600 14px/1.5 Arial,sans-serif;text-decoration:none;cursor:pointer}.dw-card-content .dw-primary{background:#173e5b;color:white;border-color:#173e5b;width:100%}.dw-card-content label{display:block;margin:12px 0;font-weight:600}.dw-card-content textarea,.dw-card-content input:not([type=checkbox]):not([type=hidden]),.dw-card-content select{box-sizing:border-box;display:block;width:100%;font:16px Arial,sans-serif;color:#1b3042;background:white;border:1px solid #afc1cf;border-radius:6px;padding:10px;margin-top:6px}.dw-choice{padding:10px;border-bottom:1px solid #dbe4eb}.dw-choice small{display:block;margin-left:25px;color:#536a7b}.dw-card-content details{margin:12px 0}.dw-card-content summary{min-height:36px;cursor:pointer;font-weight:600}.dw-card-content a{color:#205073}.dw-embedded{margin:0;background:white;padding:16px}.dw-embedded h1{font-size:22px}.dw-embedded .dw-card-content>p:first-child{display:none}.dw-card-content button:focus-visible,.dw-card-content a:focus-visible{outline:3px solid #b87910;outline-offset:2px}
</style>'''

VIEW_STYLE='''<style>
#dw-panel{box-sizing:border-box;background:white;border:1px solid #ccd9e3;border-radius:12px;color:#1b3042;overflow:hidden;font:15px/1.5 Arial,sans-serif;min-width:0}.dw-panel-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:14px;border-bottom:1px solid #dce5eb;background:#f6f8fa}#dw-list{padding:16px}#dw-list h2{font-size:20px;margin:0 0 10px}#dw-panel button{background:white;color:#183e59;border:1px solid #b8c9d5;border-radius:8px;min-height:44px;padding:9px 12px;cursor:pointer;font:600 14px Arial,sans-serif}#dw-panel .dw-card-pick{display:block;width:100%;text-align:left;margin:10px 0;padding:14px}#dw-panel .dw-card-pick span{display:block;overflow-wrap:anywhere}#dw-panel .dw-card-pick small{display:block;color:#536b7e;font-size:12px;margin-top:6px}#dw-panel .dw-card-pick[aria-pressed=true]{background:#fff2d4;border-color:#af7b20}#dw-frame{display:block;width:100%;height:640px;border:0;background:white}#dw-show-list{margin:12px 12px 0}body.dw-open .bc840-main{margin-right:350px!important}body.dw-open #dw-panel{position:fixed;right:14px;top:135px;bottom:14px;width:322px;overflow:auto;z-index:12}#dw-panel[hidden],#dw-selected[hidden],#dw-list[hidden]{display:none!important}.df-pin[aria-pressed=true]{outline:3px solid #ebb74b}.dw-open .df-actions{padding-right:0}.dw-open .dr-view-header{flex-wrap:wrap;gap:12px}.dw-open .dr-view-header>a{font-size:14px}@media(max-width:1100px){body.dw-open .bc840-main{margin-right:0!important}body.dw-open #dw-panel{position:relative;inset:auto;width:auto;margin:16px;max-width:none}#dw-frame{height:650px}}
</style>'''

VIEW_JS=r'''(()=>{
const config=window.BC_DRAWING_WORK,byId=id=>document.getElementById(id),body=document.body,panel=byId('dw-panel'),frame=byId('dw-frame'),list=byId('dw-list'),selected=byId('dw-selected'),toggle=byId('dw-toggle');
if(!config||!panel||!toggle)return;
const parent=byId('main-content');if(parent)parent.append(panel);
function show(open){body.classList.toggle('dw-open',open);panel.hidden=!open;toggle.setAttribute('aria-expanded',String(open));window.dispatchEvent(new Event('resize'));}
window.BC_DRAWING_WORK_OPEN=id=>{id=Number(id);if(!config.pins.includes(id))return;show(true);list.hidden=true;selected.hidden=false;frame.src='/workspace/drawing-sheets/'+config.sheet_id+'/work/'+id+'?embedded=true';document.querySelectorAll('[data-pin-id]').forEach(el=>el.setAttribute('aria-pressed',String(Number(el.dataset.pinId)===id)));document.querySelectorAll('[data-work-id]').forEach(el=>el.setAttribute('aria-pressed',String(Number(el.dataset.workId)===id)));if(window.innerWidth<=1100)panel.scrollIntoView({block:'start'});};
toggle.onclick=()=>show(panel.hidden);byId('dw-close').onclick=()=>show(false);byId('dw-show-list').onclick=()=>{list.hidden=false;selected.hidden=true;};
panel.querySelectorAll('[data-work-id]').forEach(button=>button.onclick=()=>window.BC_DRAWING_WORK_OPEN(button.dataset.workId));
// Keep the drawing dominant on first open. Sheets are one click away.
body.classList.add('df-sheets-hidden');byId('df-sheets')?.setAttribute('aria-expanded','false');show(false);
const pin=new URLSearchParams(location.search).get('pin');if(pin&&/^[0-9]+$/.test(pin))window.BC_DRAWING_WORK_OPEN(pin);
})();'''


FULL_PAGE_STYLE='''<style>
html:has(body.dw-fullpage){height:100%;overflow:hidden}
body.dw-fullpage{--dfp-top:64px;--dfp-controls:122px;margin:0!important;padding:0!important;height:100vh;height:100dvh;min-height:0!important;overflow:hidden!important;display:flex;flex-direction:column;background:#4d5d6a}
body.dw-fullpage .dr-view-header{position:relative!important;inset:auto!important;flex:0 0 auto;height:auto!important;min-height:64px;padding:8px 16px;gap:14px;flex-wrap:nowrap!important}
body.dw-fullpage .dr-view-header b{font-size:18px}body.dw-fullpage .dr-view-header small{margin-top:3px}
body.dw-fullpage .bc8102-content{margin:0!important;padding:0!important;display:flex;flex-direction:column;flex:1;min-height:0;min-width:0;width:100%}
body.dw-fullpage .bc840-main,body.dw-fullpage.dw-open .bc840-main,body.dw-fullpage.df-sheets-hidden .bc840-main{display:flex;flex-direction:column;flex:1;min-height:0;min-width:0;width:100%;max-width:none!important;padding:0!important;margin:0!important}
body.dw-fullpage .dfp-canvas{position:relative;display:flex;flex-direction:column;flex:1;min-height:0;min-width:0;padding:0!important;margin:0!important;border:0!important;border-radius:0!important;box-shadow:none!important;overflow:visible}
body.dw-fullpage .df-actions{box-sizing:border-box;flex:0 0 auto;display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:7px 12px;margin:0;background:#fff;border-bottom:1px solid #d3dee7;z-index:24}
body.dw-fullpage .df-actions button,body.dw-fullpage .df-actions a,body.dw-fullpage .df-actions summary{min-height:42px;padding:8px 12px;font-size:13px;margin:0;box-sizing:border-box;white-space:nowrap}
body.dw-fullpage #bcStageWrap,body.dw-fullpage.df-markup-open #bcStageWrap{box-sizing:border-box;flex:1;height:0!important;min-height:0!important;width:100%;padding:12px!important;margin:0!important;border:0;border-radius:0!important;overscroll-behavior:contain}
body.dw-fullpage #drawing-load-state{flex:0 0 auto;font-size:12px;line-height:1.4;padding:4px 12px!important;margin:0!important;background:#f2f5f7;color:#3f566b}
body.dw-fullpage #bcToolStatus,body.dw-fullpage #bcMeasureStatus{display:none;margin:0!important;padding:3px 12px;background:#f2f5f7;font-size:12px}
body.dw-fullpage.df-markup-open #bcToolStatus,body.dw-fullpage.df-markup-open #bcMeasureStatus{display:block}
body.dw-fullpage .df-tools{flex:0 0 auto;margin:0!important;border-radius:0!important;position:relative!important;top:auto!important;z-index:23;max-height:45dvh;overflow:visible}
body.dw-fullpage .bc81-menu{position:fixed!important;top:calc(var(--dfp-controls) + 62px)!important;right:12px!important;left:auto!important;max-height:calc(100dvh - var(--dfp-controls) - 78px);overflow:auto}
body.dw-fullpage .df-zoom{bottom:16px;left:16px;z-index:8}
body.dw-fullpage .dr-sheet-rail{position:fixed!important;left:8px!important;top:var(--dfp-controls)!important;bottom:8px!important;width:min(300px,calc(100vw - 16px))!important;max-height:none!important;height:auto!important;margin:0!important;padding:14px!important;z-index:30;box-shadow:0 8px 25px #10263845;border:1px solid #c6d4de;border-radius:10px;overflow:auto}
body.dw-fullpage .dr-sheet-rail nav{display:block!important}body.dw-fullpage .dr-rail-link{min-width:0!important}
body.dw-fullpage.dw-open #dw-panel{position:fixed!important;right:8px!important;top:var(--dfp-controls)!important;bottom:8px!important;width:min(350px,calc(100vw - 16px))!important;max-width:none!important;margin:0!important;z-index:31;box-shadow:0 8px 25px #10263845;overflow:auto}
body.dw-fullpage #dw-panel[hidden]{display:none!important}
body.dw-fullpage .dr-read-view,body.dw-fullpage.df-sheets-hidden .dr-read-view{display:flex;flex-direction:column;flex:1;min-height:0;padding:0!important;margin:0!important}
body.dw-fullpage .dr-read-view>iframe{display:block;flex:1;width:100%;height:0!important;min-height:0!important;border:0;margin:0}
body.dw-fullpage .dr-read-view>p,body.dw-fullpage .dr-read-view>a{margin:0;padding:4px 12px;font-size:12px;background:white}
body.dw-fullpage .dr-old{display:block!important;flex:0 0 auto;background:#fff0ca;color:#704a0d;padding:7px 12px;font-size:13px}
#dfp-more{position:relative;margin-left:auto}#dfp-more>summary{display:flex;align-items:center;cursor:pointer;border:1px solid #c3d1dd;border-radius:8px;color:#193f5a;background:#fff;font-weight:700}#dfp-more>summary:after{content:' +';margin-left:6px}#dfp-more[open]>summary:after{content:' -'}
#dfp-more>div{position:absolute;right:0;top:calc(100% + 5px);z-index:50;background:white;border:1px solid #c4d2dc;box-shadow:0 8px 25px #10263840;border-radius:10px;padding:10px;min-width:240px;max-width:calc(100vw - 24px);max-height:65dvh;overflow:auto;display:flex;flex-direction:column;gap:5px}#dfp-more>div>a,#dfp-more>div>button{text-align:left;display:block;white-space:normal!important}#dfp-more .dr-view-links{margin:0;display:flex;flex-direction:column;align-items:stretch;gap:4px}#dfp-more .dr-view-links span{margin:4px 12px;font-size:12px}
#dfp-details{box-sizing:border-box;width:min(850px,calc(100vw - 24px));max-height:calc(100dvh - 32px);border:1px solid #bdcddb;border-radius:12px;padding:20px;color:#18334a;background:white;overflow:auto}#dfp-details::backdrop{background:#0c243c70}#dfp-details .dfp-dialog-head{display:flex;justify-content:space-between;align-items:center;gap:12px;position:sticky;top:-20px;margin:-20px -20px 18px;padding:14px 20px;background:white;z-index:4;border-bottom:1px solid #dce5eb}#dfp-details .dfp-dialog-head h2{margin:0;font-size:20px}#dfp-details button{min-height:44px;padding:10px 14px;border:1px solid #bacbd8;border-radius:8px;cursor:pointer}#dfp-details .grid2{display:block}#dfp-details .grid2>.card:nth-child(2){display:none!important}#dfp-details input,#dfp-details textarea{box-sizing:border-box;width:100%}#dfp-details .hub-back{display:none!important}
@media(max-width:650px){body.dw-fullpage .dr-view-header{min-height:56px;padding:6px 10px;gap:10px}body.dw-fullpage .dr-view-header small{max-width:55vw}body.dw-fullpage .df-actions{padding:5px 7px;gap:5px}body.dw-fullpage .df-actions button,body.dw-fullpage .df-actions a,body.dw-fullpage .df-actions summary{font-size:12px;padding:8px 9px;min-height:42px}body.dw-fullpage #bcStageWrap{padding:5px!important}body.dw-fullpage .bc81-menu{right:auto!important;left:0!important;max-height:55dvh;overflow:auto}#dfp-more>div{min-width:210px}}
@media(max-height:500px){body.dw-fullpage .dr-view-header small{display:none}body.dw-fullpage .dr-view-header{min-height:44px}body.dw-fullpage #drawing-load-state{font-size:11px}body.dw-fullpage.df-markup-open .df-tools{max-height:40dvh;overflow:auto}}
</style>'''

FULL_PAGE_JS=r'''(()=>{
const byId=id=>document.getElementById(id),body=document.body,wrap=byId('bcStageWrap'),main=byId('main-content'),bar=document.querySelector('.df-actions'),header=document.querySelector('.dr-view-header');
if(!bar)return;
body.classList.add('df-sheets-hidden');byId('df-sheets')?.setAttribute('aria-expanded','false');
const canvasCard=wrap?.parentElement;if(canvasCard)canvasCard.classList.add('dfp-canvas');
const button=(id,label)=>{const b=document.createElement('button');b.type='button';b.id=id;b.textContent=label;return b;};
const more=document.createElement('details');more.id='dfp-more';const summary=document.createElement('summary');summary.textContent='More';more.append(summary);const menu=document.createElement('div');more.append(menu);
for(const link of [...bar.querySelectorAll('a')]){if(!link.classList.contains('df-primary'))menu.append(link);}
const original=document.querySelector('.dr-view-links');if(original)menu.append(original);
const detailsButton=button('dfp-open-details','Drawing details & history');
let detailDialog=null;
if(main&&canvasCard){
 detailDialog=document.createElement('dialog');detailDialog.id='dfp-details';detailDialog.setAttribute('aria-labelledby','dfp-detail-title');
 const heading=document.createElement('div');heading.className='dfp-dialog-head';const title=document.createElement('h2');title.id='dfp-detail-title';title.textContent='Drawing details & saved markups';const close=button('dfp-close-details','Close');heading.append(title,close);detailDialog.append(heading);
 for(const element of [...main.children]){if(element!==canvasCard&&element.id!=='dw-panel'&&!['SCRIPT','STYLE'].includes(element.tagName))detailDialog.append(element);}
 body.append(detailDialog);menu.append(detailsButton);close.onclick=()=>detailDialog.close();detailsButton.onclick=()=>{more.open=false;detailDialog.showModal();};
 const save=byId('df-save');if(save)save.onclick=()=>{more.open=false;detailDialog.showModal();const panel=byId('df-save-panel');if(panel)panel.open=true;byId('bcRevTitle')?.focus();};
}
const fullscreen=button('dfp-fullscreen','Full screen');fullscreen.setAttribute('aria-pressed','false');
if(document.documentElement.requestFullscreen){bar.append(fullscreen);fullscreen.onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch(error){const message=byId('drawing-load-state');if(message)message.textContent='Browser full screen is unavailable. The drawing still fills this page.';}};}
bar.append(more);
const sheets=byId('df-sheets'),cards=byId('dw-toggle'),rail=document.querySelector('.dr-sheet-rail');
if(rail){rail.id='dfp-sheet-list';sheets?.setAttribute('aria-controls',rail.id);const close=button('dfp-close-sheets','Close sheets');rail.prepend(close);close.onclick=()=>{body.classList.add('df-sheets-hidden');sheets?.setAttribute('aria-expanded','false');sheets?.focus();};}
cards?.setAttribute('aria-controls','dw-panel');
sheets?.addEventListener('click',()=>{more.open=false;if(!body.classList.contains('df-sheets-hidden'))byId('dw-close')?.click();});
cards?.addEventListener('click',()=>{more.open=false;body.classList.add('df-sheets-hidden');sheets?.setAttribute('aria-expanded','false');});
const openWork=window.BC_DRAWING_WORK_OPEN;if(openWork)window.BC_DRAWING_WORK_OPEN=id=>{body.classList.add('df-sheets-hidden');sheets?.setAttribute('aria-expanded','false');openWork(id);};
document.addEventListener('click',event=>{if(!more.contains(event.target))more.open=false;});
document.addEventListener('keydown',event=>{if(event.key!=='Escape'||document.querySelector('dialog[open]'))return;more.open=false;if(body.classList.contains('dw-open')){byId('dw-close')?.click();cards?.focus();}else if(!body.classList.contains('df-sheets-hidden')){body.classList.add('df-sheets-hidden');sheets?.setAttribute('aria-expanded','false');sheets?.focus();}});
function geometry(){body.style.setProperty('--dfp-top',(header?.getBoundingClientRect().bottom||0)+'px');body.style.setProperty('--dfp-controls',(Math.ceil(bar.getBoundingClientRect().bottom)+6)+'px');}
if(window.ResizeObserver){const observer=new ResizeObserver(geometry);observer.observe(bar);if(header)observer.observe(header);}window.addEventListener('resize',geometry);window.visualViewport?.addEventListener('resize',geometry);geometry();
let fitted=false;function firstFit(){if(fitted||byId('bcStage')?.dataset.drawReady!=='yes')return;fitted=true;requestAnimationFrame(()=>byId('bcFit')?.click());}window.addEventListener('bc-drawing-rendered',firstFit);firstFit();
document.addEventListener('fullscreenchange',()=>{fullscreen.textContent=document.fullscreenElement?'Exit full screen':'Full screen';fullscreen.setAttribute('aria-pressed',String(!!document.fullscreenElement));geometry();requestAnimationFrame(()=>byId('bcFit')?.click());});
})();'''
