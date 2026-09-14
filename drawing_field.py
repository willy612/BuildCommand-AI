"""8.16.0: location notes and explicitly reviewed single-sheet releases.

Releases use the established document snapshot sharing/access controls. Their
generated attachment is separate from the source drawing and never changes it.
"""
from datetime import timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import tempfile

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from blueprint_field import esc, packed, digest
from drawing_register import js
from drawing_export import make_sheet, dependencies_ready, ExportProblem

VERSION='8.16.0'
RELEASE='Drawings to Field'
log=logging.getLogger('buildcommand.drawing_field')


def install(ns):
    service=DrawingField(ns);ns['app'].state.drawing_field=service;service.register();return service


class DrawingField:
    def __init__(self,ns):
        self.ns=ns;self.reg=ns['app'].state.drawing_register;self.drawings=self.reg.drawings
        self.field=self.reg.field;self.hub=self.reg.hub;self.db=self.field.db;self.require=self.field.require
        self.routes=[];self.schema_ready=self.initialize()

    def initialize(self):
        try:
            with self.db(True) as c:
                key='BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_pins(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,
                    x DOUBLE PRECISION NOT NULL,y DOUBLE PRECISION NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,
                    visibility TEXT NOT NULL,status TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_release_reviews(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,
                    created_by BIGINT NOT NULL,session_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,preview_file TEXT NOT NULL,expires_at TEXT NOT NULL,
                    created TEXT NOT NULL,share_id BIGINT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_releases(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,
                    area_key TEXT NOT NULL,sheet_key TEXT NOT NULL,recipient_user_id BIGINT NOT NULL,
                    share_id BIGINT NOT NULL UNIQUE,review_id BIGINT NOT NULL UNIQUE,snapshot_json TEXT NOT NULL,
                    acknowledged_at TEXT,created TEXT NOT NULL)''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_drawing_pins_sheet ON bc_drawing_pins(company_id,project_id,sheet_id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_drawing_releases_sheet ON bc_drawing_releases(company_id,project_id,area_key,sheet_key,recipient_user_id)')
            return True
        except Exception:
            log.exception('Drawing field setup failed');return False

    def ready(self):self.require(self.schema_ready,'Drawing field setup is unavailable. Ask your administrator to check this installation.',503)
    def page(self,title,body):return self.reg.page(title,FIELD_STYLE+body)

    def context(self,c,sheet_id,write=False):
        self.ready();user,project,sheet=self.reg.sheet_context(c,sheet_id)
        if write:user,project=self.field.actor(c,project['id'],True)
        return user,project,sheet

    def pins(self,c,user,sheet,team_only=False):
        rows=c.execute('''SELECT * FROM bc_drawing_pins WHERE company_id=? AND project_id=? AND sheet_id=?
            AND (visibility='team' OR created_by=?) ORDER BY id LIMIT 500''',(user['company_id'],sheet['project_id'],sheet['id'],user['id'])).fetchall()
        return [dict(r) for r in rows if not team_only or r['visibility']=='team']

    def latest_markup(self,c,user,sheet):
        row=c.execute('''SELECT revision_no,annotation_json FROM document_markup_revisions
            WHERE company_id=? AND project_id=? AND attachment_id=? ORDER BY revision_no DESC LIMIT 1''',
            (user['company_id'],sheet['project_id'],sheet['attachment_id'])).fetchone()
        if not row:return 0,[]
        try:items=json.loads(row['annotation_json']).get('pages',{}).get(str(sheet['page_number']),[])
        except (ValueError,AttributeError):self.require(False,'This saved markup could not be read. Save it again before sharing.',409)
        return int(row['revision_no']),items

    def head(self,c,user,sheet):
        row=c.execute('SELECT sheet_id FROM bc_drawing_heads WHERE company_id=? AND project_id=? AND area_key=? AND sheet_key=?',
            (user['company_id'],sheet['project_id'],sheet['area_key'],sheet['sheet_key'])).fetchone()
        return int(row['sheet_id']) if row else 0

    def viewer(self,sheet_id:int,request:Request):
        response=self.reg.viewer(sheet_id,request)
        if response.status_code!=200:return response
        with self.db() as c:
            user,project,sheet=self.context(c,sheet_id);notes=self.pins(c,user,sheet)
        manager=self.ns['_bc850_manager'](user)
        html=response.body.decode()
        controls='<nav class="df-actions" aria-label="Drawing actions"><button type="button" id="df-sheets" aria-expanded="true">Sheets</button>'
        if manager:
            controls+=f'<button type="button" id="df-markup">Mark up</button><button type="button" id="df-note">Pin a note</button><button type="button" id="df-save">Save markups</button><a class="df-primary" href="/workspace/drawing-sheets/{sheet_id}/release">Share this sheet</a>'
        controls+=f'<a href="/workspace/drawing-sheets/{sheet_id}/notes">Field notes <span>{len(notes)}</span></a>'
        if manager:controls+=f'<a href="/workspace/drawing-sheets/{sheet_id}/releases">Issued copies</a>'
        controls+='</nav>'
        if manager:
            html=html.replace('<div class="dr-view-links">',controls+'<div class="dr-view-links">',1)
            html=html.replace('<div class="bc81-toolbar">','<div class="bc81-toolbar df-tools">',1)
            html=html.replace('canvas.style.height=h+"px";draw()}', 'canvas.style.height=h+"px";draw();document.getElementById("bcStage").dataset.drawReady="yes";window.dispatchEvent(new Event("bc-drawing-rendered"))}')
        else:html=html.replace('<main class="dr-read-view">','<main class="dr-read-view">'+controls,1)
        panel=f'''<dialog id="df-dialog"><form id="df-note-form"><div class="eyebrow">NOTE ON {esc(sheet['sheet_number'])}</div><h2>What needs attention here?</h2><p id="df-position"></p>
            <label>Short title<input name="title" maxlength="160" required placeholder="Example: Check wall clearance"></label>
            <label>Field note<textarea name="body" maxlength="2000" rows="4" required placeholder="What did you see? What needs checking?"></textarea></label>
            <label>Who can see it?<select name="visibility"><option value="private">Only me</option><option value="team">Project team</option></select></label>
            <p class="df-help">Team notes stay internal. You choose which notes to include when sharing a sheet with a trade.</p>
            <div class="df-actions"><button type="submit">Save note</button><button type="button" id="df-cancel">Cancel</button></div><p id="df-note-status" role="status"></p></form></dialog>'''
        config={'sheet_id':sheet_id,'manager':manager,'pins':[{'id':n['id'],'x':float(n['x']),'y':float(n['y']),'title':n['title'],'visibility':n['visibility'],'status':n['status']} for n in notes]}
        html=html.replace('</head>',FIELD_STYLE+VIEW_STYLE+'</head>',1)
        html=html.replace('</body>',(panel if manager else '')+'<script>window.BC_DRAWING_FIELD='+js(config)+';</script><script>'+VIEW_JS+'</script></body>',1)
        return HTMLResponse(html,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})

    async def add_pin(self,sheet_id:int,request:Request):
        self.require(self.ns['_bc840_same_origin'](request),'Reload this drawing before saving a note.',403)
        size=0;parts=[]
        async for block in request.stream():
            size+=len(block);self.require(size<=16384,'Use a shorter field note.',413);parts.append(block)
        try:data=json.loads(b''.join(parts))
        except ValueError:self.require(False,'The note could not be read. Try again.',400)
        self.require(isinstance(data,dict),'Enter a field note.',400)
        x=data.get('x');y=data.get('y');title=str(data.get('title') or '').strip();body=str(data.get('body') or '').strip();visibility=data.get('visibility','private')
        self.require(all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in (x,y)),'Choose a location inside the sheet.',400)
        self.require(0<len(title)<=160 and 0<len(body)<=2000 and visibility in {'private','team'},'Enter a short title, a note and who can see it.',400)
        with self.db(True) as c:
            user,project,sheet=self.context(c,sheet_id,True)
            count=c.execute('SELECT COUNT(*) AS n FROM bc_drawing_pins WHERE company_id=? AND project_id=? AND sheet_id=?',(user['company_id'],project['id'],sheet_id)).fetchone()['n']
            self.require(count<500,'This sheet already has 500 field notes.',409)
            pid=self.reg.insert(c,'''INSERT INTO bc_drawing_pins(company_id,project_id,sheet_id,x,y,title,body,visibility,status,created_by,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],sheet_id,x,y,title,body,visibility,'OPEN',user['id'],self.field.now().isoformat()))
            self.ns['_bc850_event'](c,user,project['id'],None,'DRAWING_PIN_ADDED:'+str(pid))
        return JSONResponse({'id':pid,'url':f'/workspace/drawing-sheets/{sheet_id}/notes#pin-{pid}'})

    def notes(self,sheet_id:int):
        with self.db() as c:
            user,project,sheet=self.context(c,sheet_id);notes=self.pins(c,user,sheet)
        body=f'<p><a href="/workspace/drawing-sheets/{sheet_id}">← Back to drawing</a></p><div class="eyebrow">{esc(sheet["sheet_number"])} · {esc(sheet["revision_label"] or "Original")}</div><h1>Field notes</h1><p>Notes stay with this exact sheet revision.</p>'
        for n in notes:
            body+=f'<article class="card df-note-card" id="pin-{n["id"]}"><span class="df-tag">{"Only me" if n["visibility"]=="private" else "Project team"} · {esc(n["status"].capitalize())}</span><h2>{esc(n["title"])}</h2><p style="white-space:pre-wrap">{esc(n["body"])}</p><p><a href="/workspace/drawing-sheets/{sheet_id}?pin={n["id"]}">Show this location</a></p>'
            if self.ns['_bc850_manager'](user):body+=f'<form method="post" action="/workspace/drawing-sheets/{sheet_id}/notes/{n["id"]}/status"><input type="hidden" name="expected" value="{n["status"]}"><button name="status" value="{"CLOSED" if n["status"]=="OPEN" else "OPEN"}">{"Close note" if n["status"]=="OPEN" else "Reopen note"}</button></form>'
            body+='</article>'
        if not notes:body+='<div class="card"><h2>No field notes yet.</h2><p>Open the drawing and choose Pin a note to mark a location.</p></div>'
        return self.page('Drawing field notes',body)

    def pin_status(self,sheet_id:int,pin_id:int,status:str=Form(...),expected:str=Form(...)):
        self.require(status in {'OPEN','CLOSED'} and expected in {'OPEN','CLOSED'},'Choose a note status.',400)
        with self.db(True) as c:
            user,project,sheet=self.context(c,sheet_id,True)
            note=next((n for n in self.pins(c,user,sheet) if n['id']==pin_id),None)
            self.require(note is not None,'This note is unavailable.',404)
            self.require(note['status'] in {expected,status},'This note changed. Reload it first.',409)
            if note['status']!=status:
                c.execute('UPDATE bc_drawing_pins SET status=? WHERE id=? AND company_id=?',(status,pin_id,user['company_id']))
                self.ns['_bc850_event'](c,user,project['id'],None,'DRAWING_PIN_'+status+':'+str(pin_id))
        return RedirectResponse(f'/workspace/drawing-sheets/{sheet_id}/notes#pin-{pin_id}',303)

    def recipients(self,c,user,pid):
        table=self.ns['_bc850_member_table']()
        rows=c.execute(f'SELECT u.id,u.display_name,u.email,u.role FROM users u JOIN {table} m ON m.user_id=u.id WHERE u.company_id=? AND m.project_id=? ORDER BY u.email',(user['company_id'],pid)).fetchall()
        return [dict(r) for r in rows if self.ns['_bc840_tier'](dict(r))=='trade']

    def active_releases(self,c,user,sheet,recipient):
        return [dict(r) for r in c.execute('''SELECT r.id,r.share_id,s.version,s.title FROM bc_drawing_releases r
            JOIN bc_shared_work s ON s.id=r.share_id AND s.company_id=r.company_id AND s.project_id=r.project_id
            WHERE r.company_id=? AND r.project_id=? AND r.area_key=? AND r.sheet_key=? AND r.recipient_user_id=?
            AND s.revoked_at IS NULL ORDER BY r.id''',(user['company_id'],sheet['project_id'],sheet['area_key'],sheet['sheet_key'],recipient)).fetchall()]

    def prepare(self,sheet_id:int):
        with self.db() as c:
            user,project,sheet=self.context(c,sheet_id);user,project=self.field.actor(c,project['id'])
            self.require(self.head(c,user,sheet)==sheet_id,'Open the current revision before preparing a trade copy.',409)
            recipients=self.recipients(c,user,project['id']);rev,items=self.latest_markup(c,user,sheet);notes=[n for n in self.pins(c,user,sheet,True) if n['status']=='OPEN']
        body=f'<p><a href="/workspace/drawing-sheets/{sheet_id}">← Back to drawing</a></p><div class="eyebrow">SHARE ONE SHEET</div><h1>{esc(sheet["sheet_number"])} · {esc(sheet["title"])}</h1><p>{esc(sheet["area"])} · {esc(sheet["revision_label"] or "Original")}</p>'
        if not recipients:return self.page('Share drawing',body+f'<div class="card"><h2>Choose your trade team first.</h2><p>Invite the subcontractor and assign them to this project.</p><a href="/workspace/sharing/projects/{project["id"]}/team">Manage project team</a></div>')
        body+=f'<form class="card df-form" method="post" action="/workspace/drawing-sheets/{sheet_id}/release/review"><h2>1. Choose the trade and included notes</h2><label>Assigned subcontractor<select name="recipient_user_id" required><option value="">Choose a person</option>'+''.join(f'<option value="{r["id"]}">{esc(r["display_name"] or r["email"])} · {esc(r["email"])}</option>' for r in recipients)+'</select></label>'
        body+='<label>Message<textarea name="message" maxlength="2000" rows="3" placeholder="What should this trade check or do?"></textarea></label>'
        body+=f'<input type="hidden" name="markup_revision" value="{rev}"><label><input type="checkbox" name="include_markup" value="yes"> Include saved markup layer {rev} ({len(items)} markups on this sheet)</label><p class="df-help">Only saved markups are available. Review the PDF before approving. Original PDF comments and form fields are not included.</p><fieldset><legend>Include project team notes (optional)</legend>'
        for n in notes:body+=f'<label><input type="checkbox" name="pin_ids" value="{n["id"]}"> {esc(n["title"])}</label><p class="df-help">{esc(n["body"][:180])}</p>'
        if not notes:body+='<p>No open team notes on this revision.</p>'
        body+='</fieldset><p class="df-help">Private notes cannot be shared. Selected team notes appear as numbered pins with a notes page in the PDF.</p><button>Prepare PDF preview</button><p>No trade access changes until you approve the preview.</p></form>'
        return self.page('Share one drawing sheet',body)

    def file_digest(self,path):
        with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

    def folder(self,name):
        root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();folder=root/name
        self.require(not folder.is_symlink(),'Drawing export storage is unavailable.',503);folder.mkdir(exist_ok=True)
        return folder

    def store_preview(self,data):
        folder=self.folder('_bc_drawing_previews');name=hashlib.sha256(data).hexdigest()+'.pdf';target=folder/name
        self.require(not target.is_symlink(),'Drawing export storage is unavailable.',503)
        with tempfile.NamedTemporaryFile(dir=folder,delete=False) as f:temp=Path(f.name);f.write(data)
        try:
            if target.exists():temp.unlink()
            else:os.replace(temp,target)
        finally:
            if temp.exists():temp.unlink()
        return name

    def preview_path(self,review):
        name=review['preview_file'];self.require(bool(re.fullmatch(r'[a-f0-9]{64}\.pdf',name)),'This PDF preview is unavailable.',404)
        path=self.folder('_bc_drawing_previews')/name
        self.require(not path.is_symlink() and path.is_file(),'This PDF preview is unavailable.',404)
        self.require(self.file_digest(path)==name[:-4],'This preview changed. Prepare the sheet again.',409)
        return path

    def review(self,sheet_id:int,request:Request,recipient_user_id:int=Form(...),message:str=Form(''),markup_revision:int=Form(0),include_markup:str=Form(''),pin_ids:list[int]=Form([])):
        self.require(len(message)<=2000 and include_markup in {'','yes'} and len(pin_ids)<=50 and len(set(pin_ids))==len(pin_ids),'Choose up to 50 notes and a short message.',400)
        # Capture authorized inputs, render outside the DB transaction, then
        # approval rechecks the entire capture under the project lock.
        with self.db() as c:
            user,project,sheet=self.context(c,sheet_id);user,project=self.field.actor(c,project['id'])
            self.require(self.head(c,user,sheet)==sheet_id,'The current drawing changed. Open it and review again.',409)
            recipient=self.ns['_bc850_recipient'](c,user,project['id'],recipient_user_id)
            rev,items=self.latest_markup(c,user,sheet)
            self.require(not include_markup or rev==markup_revision,'A newer markup was saved. Reopen Share this sheet.',409)
            notes=[n for n in self.pins(c,user,sheet,True) if n['id'] in pin_ids and n['status']=='OPEN']
            self.require(len(notes)==len(pin_ids),'Choose only open project team notes on this sheet.',400)
            source=dict(c.execute('SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?',(sheet['attachment_id'],user['company_id'],project['id'])).fetchone())
            previous=self.active_releases(c,user,sheet,recipient_user_id)
        path=self.drawings.file_path(source);source_hash=self.file_digest(path)
        label=sheet['sheet_number']+' - '+sheet['title']+' - '+(sheet['revision_label'] or 'Original')
        try:data=make_sheet(path,int(sheet['page_number']),items if include_markup else [],notes,{'label':label})
        except ExportProblem as exc:
            log.warning('Drawing export could not be prepared sheet_id=%s',sheet_id,exc_info=bool(exc.__cause__))
            self.require(False,str(exc),422)
        self.require(self.file_digest(path)==source_hash,'The original changed while preparing this copy. Try again.',409)
        preview=self.store_preview(data);now=self.field.now()
        payload={'sheet':sheet,'recipient':recipient,'markup_revision':rev if include_markup else None,'pins':notes,'message':message.strip(),'previous':previous,'label':label,'download_name':re.sub(r'[^A-Za-z0-9._-]','_',sheet['sheet_number'])+'-rev-'+str(sheet['revision_no'])+'.pdf'}
        with self.db(True) as c:
            fresh,_=self.field.actor(c,project['id'],True);self.require(fresh['id']==user['id'],'Sign in again to review this sheet.',403)
            rid=self.reg.insert(c,'''INSERT INTO bc_drawing_release_reviews(company_id,project_id,sheet_id,created_by,session_hash,payload_json,source_hash,preview_file,expires_at,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],sheet_id,user['id'],self.field.session_hash(request),packed(payload),source_hash,preview,(now+timedelta(minutes=30)).isoformat(),now.isoformat()))
        return RedirectResponse(f'/workspace/drawing-releases/{rid}/review',303)

    def review_context(self,c,review_id,request,lock=False):
        self.ready();row=c.execute('SELECT * FROM bc_drawing_release_reviews WHERE id=?',(review_id,)).fetchone()
        self.require(row is not None,'This sheet review is unavailable.',404);row=dict(row)
        user,project=self.field.actor(c,row['project_id'],lock)
        if lock:row=dict(c.execute('SELECT * FROM bc_drawing_release_reviews WHERE id=?'+self.field.lock,(review_id,)).fetchone())
        self.require(user['company_id']==row['company_id'] and user['id']==row['created_by'] and secrets.compare_digest(row['session_hash'],self.field.session_hash(request)),'Open this review in the session that prepared it.',403)
        self.require(row['share_id'] is not None or row['expires_at']>self.field.now().isoformat(),'This review expired. Prepare the sheet again.',409)
        return user,project,row,json.loads(row['payload_json'])

    def review_page(self,review_id:int,request:Request):
        with self.db() as c:user,project,row,payload=self.review_context(c,review_id,request)
        if row['share_id']:return RedirectResponse('/workspace/sharing/'+str(row['share_id']),303)
        self.preview_path(row);sheet=payload['sheet'];recipient=payload['recipient']
        body=f'<p><a href="/workspace/drawing-sheets/{sheet["id"]}/release">← Back to choices</a></p><div class="eyebrow">REVIEW BEFORE SHARING</div><h1>Check the exact trade copy.</h1><div class="card"><h2>{esc(payload["label"])}</h2><p><b>Recipient:</b> {esc(recipient["display_name"] or recipient["email"])} · {esc(recipient["email"])}</p><p style="white-space:pre-wrap">{esc(payload["message"])}</p><p>Saved markup: {payload["markup_revision"] if payload["markup_revision"] is not None else "Not included"} · {len(payload["pins"])} selected field notes</p>'
        if payload['previous']:
            body+='<div class="df-warning"><b>These earlier copies will be withdrawn for this person:</b><ul>'+''.join(f'<li>{esc(p["title"])}</li>' for p in payload['previous'])+'</ul><p>The new copy will need a new acknowledgment.</p></div>'
        else:body+='<p>This creates a new shared item for the selected person.</p>'
        body+=f'<p><a target="_blank" rel="noopener" href="/workspace/drawing-releases/{review_id}/preview.pdf">Open PDF preview in a new tab</a></p><iframe class="df-preview" title="Exact PDF to be shared" src="/workspace/drawing-releases/{review_id}/preview.pdf"></iframe></div><form class="card df-note-card" method="post" action="/workspace/drawing-releases/{review_id}/approve"><label><input type="checkbox" name="confirmed" value="yes" required> I checked this PDF, the recipient, the notes, and any copies being replaced.</label><p>The PDF shown here will be shared in BuildCommand. No email is sent.</p><button>Approve &amp; share this copy</button></form>'
        return self.page('Review drawing release',body)

    def preview(self,review_id:int,request:Request):
        with self.db() as c:user,project,row,payload=self.review_context(c,review_id,request)
        return FileResponse(self.preview_path(row),filename=payload['download_name'],media_type='application/pdf',content_disposition_type='inline',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'SAMEORIGIN','Referrer-Policy':'no-referrer'})

    def approve(self,review_id:int,request:Request,confirmed:str=Form('')):
        self.require(confirmed=='yes','Check the PDF and confirm before sharing.',400)
        with self.db(True) as c:
            user,project,row,payload=self.review_context(c,review_id,request,True)
            if row['share_id']:
                self.ns['_bc850_share'](c,user,row['share_id'],True)
                return RedirectResponse('/workspace/sharing/'+str(row['share_id']),303)
            _,_,sheet=self.context(c,row['sheet_id'])
            self.require(sheet==payload['sheet'] and self.head(c,user,sheet)==sheet['id'],'The drawing revision changed. Prepare a fresh preview.',409)
            c.execute('SELECT id FROM users WHERE id=?'+self.field.lock,(payload['recipient']['id'],)).fetchone()
            recipient=self.ns['_bc850_recipient'](c,user,project['id'],payload['recipient']['id'])
            self.require(recipient==payload['recipient'],'The recipient changed. Prepare a fresh preview.',409)
            live=self.active_releases(c,user,sheet,recipient['id'])
            self.require(live==payload['previous'],'An earlier trade copy changed. Prepare a fresh preview.',409)
            if payload['markup_revision'] is not None:
                rev,_=self.latest_markup(c,user,sheet);self.require(rev==payload['markup_revision'],'A new markup was saved. Prepare a fresh preview.',409)
            wanted={p['id'] for p in payload['pins']};notes=[n for n in self.pins(c,user,sheet,True) if n['id'] in wanted]
            self.require(notes==payload['pins'],'A selected field note changed. Prepare a fresh preview.',409)
            source=dict(c.execute('SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?',(sheet['attachment_id'],user['company_id'],project['id'])).fetchone())
            self.require(self.file_digest(self.drawings.file_path(source))==row['source_hash'],'The original drawing changed. Prepare a fresh preview.',409)
            preview=self.preview_path(row);now=self.field.now().isoformat()
            # Retain the exact reviewed bytes as a separate document. Existing
            # snapshot serving/revocation then protects every open/download.
            root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();stored='issued-sheet-'+row['preview_file'];dest=root/stored
            self.require(not dest.is_symlink(),'Drawing export storage is unavailable.',503)
            if not dest.exists():
                with dest.open('xb') as f:f.write(preview.read_bytes())
            self.require(self.file_digest(dest)==row['preview_file'][:-4],'This issued file changed. Prepare a fresh preview.',409)
            aid=self.reg.insert(c,'''INSERT INTO attachments(company_id,project_id,category,title,original_name,stored_name,mime_type,size_bytes,created_by,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],'DRAWING_RELEASE',payload['label'],payload['download_name'],stored,'application/pdf',dest.stat().st_size,user['id'],now))
            snapshot,name=self.ns['_bc850_snapshot']({'stored_name':stored,'original_name':payload['download_name']})
            sid=self.ns['_bc850_insert'](c,'bc_shared_work','company_id,project_id,kind,source_id,recipient_user_id,title,message,due_date,allow_response,snapshot_file,download_name,created_by,created_at,updated_at',
                (user['company_id'],project['id'],'document',aid,recipient['id'],payload['label'],payload['message'],'',1,snapshot,name,user['id'],now,now))
            self.reg.insert(c,'''INSERT INTO bc_drawing_releases(company_id,project_id,sheet_id,area_key,sheet_key,recipient_user_id,share_id,review_id,snapshot_json,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],sheet['id'],sheet['area_key'],sheet['sheet_key'],recipient['id'],sid,review_id,packed(payload),now))
            for previous in live:
                c.execute('UPDATE bc_shared_work SET revoked_at=?,updated_at=?,version=version+1 WHERE id=? AND company_id=?',(now,now,previous['share_id'],user['company_id']))
                self.ns['_bc850_event'](c,user,project['id'],previous['share_id'],'DRAWING_COPY_REPLACED:'+str(sid))
            c.execute('UPDATE bc_drawing_release_reviews SET share_id=? WHERE id=?',(sid,review_id))
            self.ns['_bc850_event'](c,user,project['id'],sid,'DRAWING_SHEET_ISSUED')
        return RedirectResponse('/workspace/sharing/'+str(sid),303)

    def issued_html(self,c,share,manager=False):
        if not self.schema_ready:return ''
        row=c.execute('SELECT * FROM bc_drawing_releases WHERE share_id=? AND company_id=? AND project_id=?',(share['id'],share['company_id'],share['project_id'])).fetchone()
        if not row:return ''
        payload=json.loads(row['snapshot_json']);sheet=payload['sheet'];body='<div class="card"><div class="eyebrow">REVIEWED DRAWING COPY</div><h2>'+esc(sheet['sheet_number'])+' · '+esc(sheet['revision_label'] or 'Original')+'</h2><p>One drawing sheet'+(' with selected field notes.' if payload['pins'] else '.')+'</p>'
        body+='<p>'+('Acknowledged '+esc(row['acknowledged_at'][:16].replace('T',' '))+' UTC' if row['acknowledged_at'] else 'Waiting for acknowledgment')+'</p>'
        head=c.execute('SELECT sheet_id FROM bc_drawing_heads WHERE company_id=? AND project_id=? AND area_key=? AND sheet_key=?',(share['company_id'],share['project_id'],sheet['area_key'],sheet['sheet_key'])).fetchone()
        if head and head['sheet_id']!=row['sheet_id']:body+='<p><b>A newer revision exists. Ask your project leader which drawing to use.</b></p>'
        if not manager and not share['revoked_at'] and not row['acknowledged_at']:
            body+=f'<form method="post" action="/workspace/shared/{share["id"]}/drawing-acknowledge"><input type="hidden" name="version" value="{share["version"]}"><label><input type="checkbox" name="confirmed" value="yes" required> I received and reviewed this drawing copy.</label><p><button>Acknowledge drawing</button></p></form>'
        if manager:body+=f'<p><a href="/workspace/drawing-sheets/{row["sheet_id"]}/releases">Drawing issue history</a></p>'
        return body+'</div>'

    def acknowledge(self,share_id:int,version:int=Form(...),confirmed:str=Form('')):
        self.require(confirmed=='yes','Open the drawing and confirm you reviewed this copy.',400)
        with self.db(True) as c:
            self.ready();user=self.ns['_bc850_actor'](c);share=self.ns['_bc850_share'](c,user,share_id,False,True)
            self.require(share['version']==version,'This copy changed. Open the latest shared item.',409)
            row=c.execute('SELECT * FROM bc_drawing_releases WHERE share_id=? AND company_id=? AND project_id=?',(share_id,user['company_id'],share['project_id'])).fetchone()
            self.require(row is not None,'This item is not a drawing release.',404)
            if not row['acknowledged_at']:
                now=self.field.now().isoformat();c.execute('UPDATE bc_drawing_releases SET acknowledged_at=? WHERE id=?',(now,row['id']))
                self.ns['_bc850_insert'](c,'bc_shared_work_updates','share_id,actor_user_id,share_version,status,message,created_at',
                    (share_id,user['id'],version,'ACKNOWLEDGED','Received and reviewed this drawing copy.',now))
                self.ns['_bc850_event'](c,user,share['project_id'],share_id,'DRAWING_COPY_ACKNOWLEDGED')
        return RedirectResponse('/workspace/shared/'+str(share_id),303)

    def releases(self,sheet_id:int):
        with self.db() as c:
            user,project,sheet=self.context(c,sheet_id);self.field.actor(c,project['id'])
            rows=c.execute('''SELECT r.*,s.title,s.revoked_at,u.email FROM bc_drawing_releases r JOIN bc_shared_work s ON s.id=r.share_id
                LEFT JOIN users u ON u.id=r.recipient_user_id WHERE r.company_id=? AND r.project_id=? AND r.area_key=? AND r.sheet_key=? ORDER BY r.id DESC LIMIT 200''',
                (user['company_id'],project['id'],sheet['area_key'],sheet['sheet_key'])).fetchall()
        body=f'<p><a href="/workspace/drawing-sheets/{sheet_id}">← Back to drawing</a></p><h1>{esc(sheet["sheet_number"])} · Issued copies</h1><p>Every approved copy keeps its recipient, revision and acknowledgment.</p><div class="card">'
        for r in rows:body+=f'<article class="df-note-card"><h2><a href="/workspace/sharing/{r["share_id"]}">{esc(r["title"])}</a></h2><p>{esc(r["email"] or "Removed account")} · {"Withdrawn" if r["revoked_at"] else "Shared"} · {"Acknowledged" if r["acknowledged_at"] else "Waiting for acknowledgment"}</p><p>{esc(r["created"][:16].replace("T"," "))} UTC</p></article>'
        if not rows:body+='<p>No sheets have been issued to a trade yet.</p>'
        return self.page('Drawing issue history',body+'</div>')

    def extra_evidence(self,c,user,pid):
        rows=self.previous_evidence(c,user,pid)
        if self.schema_ready:
            notes=c.execute('''SELECT n.*,s.sheet_number,s.revision_label FROM bc_drawing_pins n JOIN bc_drawing_sheets s ON s.id=n.sheet_id AND s.company_id=n.company_id AND s.project_id=n.project_id
                JOIN bc_drawing_heads h ON h.sheet_id=s.id AND h.company_id=s.company_id AND h.project_id=s.project_id
                WHERE n.company_id=? AND n.project_id=? AND n.visibility='team' AND n.status='OPEN' ORDER BY n.id DESC LIMIT 30''',(user['company_id'],pid)).fetchall()
            for n in notes:rows.append(('Drawing field note',n['id'],n['title'],f"Team field note on {n['sheet_number']}, revision {n['revision_label'] or 'Original'}: {n['body']}. Human-entered note; not verified plan analysis.",f"/workspace/drawing-sheets/{n['sheet_id']}/notes#pin-{n['id']}"))
        return rows

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes if hasattr(r,'methods') for m in r.methods or []}
        checks={m+' '+p:active.get((p,m)) is f for m,p,f in self.routes}
        checks['drawing_field_schema_initialized']=self.schema_ready
        checks['pdf_export_dependencies_available']=dependencies_ready()
        checks['drawing_register_preserved']=getattr(self.ns['app'].state,'drawing_register',None) is self.reg
        checks['saved_markup_handler_preserved']=('/workspace/drawings/{attachment_id}/markups','POST') in active
        checks['shared_pdf_access_preserved']=('/workspace/shared/{share_id}/view','GET') in active
        checks['form_origin_guard_preserved']=callable(self.ns.get('_bc840_same_origin'))
        try:
            with self.db() as c:
                c.execute('SELECT x,y,visibility FROM bc_drawing_pins WHERE 1=0');c.execute('SELECT share_id,acknowledged_at FROM bc_drawing_releases WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if ok else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Installation, schema and dependency checks only. Verify real drawings, pinned notes, the exact PDF preview, trade access, acknowledgment and replacement on staging.'),status_code=200 if ok else 503)

    def register(self):
        self.previous_evidence=self.hub.extra_evidence;self.hub.extra_evidence=self.extra_evidence
        routes=[('GET','/workspace/drawing-sheets/{sheet_id}',self.viewer),('POST','/workspace/drawing-sheets/{sheet_id}/notes',self.add_pin),('GET','/workspace/drawing-sheets/{sheet_id}/notes',self.notes),('POST','/workspace/drawing-sheets/{sheet_id}/notes/{pin_id}/status',self.pin_status),('GET','/workspace/drawing-sheets/{sheet_id}/release',self.prepare),('POST','/workspace/drawing-sheets/{sheet_id}/release/review',self.review),('GET','/workspace/drawing-releases/{review_id}/review',self.review_page),('GET','/workspace/drawing-releases/{review_id}/preview.pdf',self.preview),('POST','/workspace/drawing-releases/{review_id}/approve',self.approve),('POST','/workspace/shared/{share_id}/drawing-acknowledge',self.acknowledge),('GET','/workspace/drawing-sheets/{sheet_id}/releases',self.releases)]
        for method,path,fn in routes:
            wrapped=self.drawings.endpoint(fn);self.ns['_bc840_replace'](path,method,wrapped);self.routes.append((method,path,wrapped))
            self.reg.routes[:]=[(m,p,wrapped if (m,p)==(method,path) else f) for m,p,f in self.reg.routes]
        path='/health/drawings-to-field-8-16-0';self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)


FIELD_STYLE='''<style>
.df-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}.df-actions button,.df-actions a{font:600 14px Arial,sans-serif;min-height:44px;box-sizing:border-box;display:inline-flex;align-items:center;padding:10px 16px;border:1px solid #bfccd7;border-radius:8px;background:white;color:#17334b;text-decoration:none;cursor:pointer}.df-actions .df-primary{background:#173b57;color:white;border-color:#173b57}.df-actions a span{margin-left:7px;background:#eaf0f4;padding:3px 6px;border-radius:4px}.df-help{font-size:14px;line-height:1.5;color:#52687c}.df-form label,#df-dialog label{display:block;font-weight:600;margin:16px 0}.df-form input:not([type=checkbox]),.df-form select,.df-form textarea,#df-dialog input,#df-dialog select,#df-dialog textarea{width:100%;display:block;box-sizing:border-box;padding:12px;border:1px solid #b9c9d5;border-radius:6px;margin-top:8px;font:16px Arial}.df-form fieldset{border:1px solid #d1dce6;border-radius:8px;padding:18px;margin-top:20px}.df-note-card{margin:18px 0;padding:20px;border-bottom:1px solid #d4dee6}.df-tag{display:inline-block;padding:6px 10px;background:#ecf2f6;color:#1d465f;border-radius:5px;font-size:13px;font-weight:bold}.df-warning{background:#fff3d8;padding:16px;border-radius:8px;color:#6e4900}.df-preview{border:1px solid #ccd7df;border-radius:8px;width:100%;height:65vh;min-height:350px}#df-dialog{width:min(520px,90vw);box-sizing:border-box;border:1px solid #becbd7;border-radius:14px;padding:28px;box-shadow:0 18px 70px #142c4560;color:#18354d}#df-dialog::backdrop{background:#10273c80}#df-dialog h2{margin:8px 0}#df-dialog button[type=submit]{background:#173b57;color:#fff}.df-actions :focus-visible{outline:3px solid #d6a238;outline-offset:2px}
</style>'''

VIEW_STYLE='''<style>
.dr-view-header{background:#112b40;border-color:#efb64b}.dr-view-header b{color:#f4bf55}.dr-view-links{margin-top:6px}.dr-view-links a[href="#bcRevTitle"]{display:none}.df-tools{display:none!important}.df-markup-open .df-tools{display:flex!important}.df-markup-open #bcStageWrap{height:calc(100vh - 370px)!important}.df-tools #bcZoomIn,.df-tools #bcZoomOut,.df-tools #bcFit,.df-tools #bcZoomLabel{display:none}.df-zoom{position:absolute;bottom:18px;left:18px;z-index:5;display:flex;background:#fff;border-radius:8px;box-shadow:0 2px 12px #10273c40;overflow:hidden}.df-zoom button{background:white!important;color:#16344b!important;border:0;border-right:1px solid #d1dce3;border-radius:0!important;padding:10px 14px;min-height:44px;cursor:pointer}.df-sheets-hidden .dr-sheet-rail{display:none}.df-sheets-hidden .bc840-main,.df-sheets-hidden .dr-read-view{margin-left:20px!important}.df-pin-layer{position:absolute;inset:0;z-index:3;pointer-events:none}.df-pin{position:absolute;transform:translate(-50%,-50%);width:30px;height:30px;min-height:30px!important;padding:0!important;border:2px solid white!important;border-radius:50%!important;background:#173e5d!important;color:white!important;font:bold 13px Arial!important;box-shadow:0 1px 6px #10273c70;pointer-events:auto;cursor:pointer}.df-pin[data-private=true]{background:#765c2b!important}.df-pin[data-closed=true]{opacity:.5}.df-pin:focus{outline:4px solid #efb54d}.df-note-mode .df-pin-layer{pointer-events:auto;cursor:crosshair;background:#eab64812}#bcStageWrap{position:relative!important;height:calc(100vh - 320px)!important}.df-note-mode #bcMarkupCanvas{pointer-events:none!important}.df-actions{margin-top:2px}.df-actions button[aria-pressed=true]{background:#eaf1f6;border-color:#274e6c}#drawing-load-state{font-size:13px}.df-save-panel{margin-top:20px}.df-save-panel>summary{padding:14px;font-weight:bold;cursor:pointer}.df-save-panel>div{margin:0!important}@media(max-width:900px){.df-sheets-hidden .bc840-main,.df-sheets-hidden .dr-read-view{margin:12px!important}#bcStageWrap{height:65vh!important}.df-actions{gap:7px}.df-actions button,.df-actions a{font-size:13px;padding:10px}.df-markup-open #bcStageWrap{height:60vh!important}}@media(prefers-reduced-motion:no-preference){.df-pin:focus{transition:outline .15s}}
</style>'''

VIEW_JS=r'''(()=>{
const config=window.BC_DRAWING_FIELD,body=document.body,byId=id=>document.getElementById(id),sheets=byId('df-sheets');
sheets?.addEventListener('click',()=>{const hidden=body.classList.toggle('df-sheets-hidden');sheets.setAttribute('aria-expanded',String(!hidden));});
if(!config.manager)return;
const stage=byId('bcStage'),wrap=byId('bcStageWrap'),dialog=byId('df-dialog'),form=byId('df-note-form'),status=byId('df-note-status');
let picking=false,point=null,dirty=false;
byId('df-note').disabled=true;
function ready(){byId('df-note').disabled=stage?.dataset.drawReady!=='yes';}
window.addEventListener('bc-drawing-rendered',ready);ready();
byId('df-markup').onclick=()=>{stopPicking();const open=body.classList.toggle('df-markup-open');byId('df-markup').setAttribute('aria-pressed',String(open));if(open)byId('bcMarkupTrigger')?.click();};
const save=byId('bcSaveMarkup'),group=save?.closest('.grid2');
if(group){const details=document.createElement('details');details.className='df-save-panel';details.id='df-save-panel';const summary=document.createElement('summary');summary.textContent='Save markup revision';group.before(details);details.append(summary,group);}
byId('df-save').onclick=()=>{const details=byId('df-save-panel');if(details){details.open=true;details.scrollIntoView({block:'start',behavior:'smooth'});byId('bcRevTitle')?.focus();}};
if(!stage||!wrap)return;
const layer=document.createElement('div');layer.className='df-pin-layer';stage.append(layer);
for(const [i,p] of config.pins.entries()){const button=document.createElement('button');button.type='button';button.className='df-pin';button.style.left=(p.x*100)+'%';button.style.top=(p.y*100)+'%';button.textContent=String(i+1);button.dataset.private=String(p.visibility==='private');button.dataset.closed=String(p.status==='CLOSED');button.dataset.pinId=String(p.id);button.setAttribute('aria-label',(p.visibility==='private'?'Private note: ':'Team note: ')+p.title);button.title=p.title;button.onclick=e=>{e.stopPropagation();if(!picking)location.assign('/workspace/drawing-sheets/'+config.sheet_id+'/notes#pin-'+p.id);};layer.append(button);}
const zoom=document.createElement('div');zoom.className='df-zoom';for(const [label,target] of [['-','bcZoomOut'],['Fit','bcFit'],['+','bcZoomIn']]){const button=document.createElement('button');button.type='button';button.textContent=label;button.setAttribute('aria-label',target==='bcFit'?'Fit sheet':target==='bcZoomOut'?'Zoom out':'Zoom in');button.onclick=()=>byId(target)?.click();zoom.append(button);}wrap.parentElement.style.position='relative';wrap.parentElement.append(zoom);
function stopPicking(){picking=false;body.classList.remove('df-note-mode');byId('df-note').setAttribute('aria-pressed','false');}
byId('df-note').onclick=()=>{document.querySelector('[data-tool=pan]')?.click();picking=true;body.classList.add('df-note-mode');byId('df-note').setAttribute('aria-pressed','true');byId('drawing-load-state').textContent='Tap the drawing where you want to leave a note. Press Escape to cancel.';};
layer.addEventListener('pointerdown',e=>{if(!picking)return;e.preventDefault();e.stopPropagation();const r=layer.getBoundingClientRect();if(r.width<20||r.height<20)return;point={x:Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),y:Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))};stopPicking();byId('df-position').textContent='This note will stay at the selected location on this revision.';dialog.showModal();form.elements.title.focus();});
byId('df-cancel').onclick=()=>{if(dirty&&!confirm('Discard this unsaved note?'))return;dialog.close();dirty=false;form.reset();};
dialog.addEventListener('cancel',e=>{if(dirty&&!confirm('Discard this unsaved note?'))e.preventDefault();else{dirty=false;form.reset();}});
document.addEventListener('keydown',e=>{if(e.key==='Escape')stopPicking();});form.addEventListener('input',()=>{dirty=true;});window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
form.onsubmit=async e=>{e.preventDefault();if(!point||!form.reportValidity())return;const submit=form.querySelector('[type=submit]');submit.disabled=true;status.textContent='Saving field note…';try{const response=await fetch('/workspace/drawing-sheets/'+config.sheet_id+'/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...point,title:form.elements.title.value,body:form.elements.body.value,visibility:form.elements.visibility.value})});if(!response.ok){const doc=new DOMParser().parseFromString(await response.text(),'text/html');status.textContent=doc.querySelector('p')?.textContent||'The note could not be saved. Your text is still here.';return;}dirty=false;dialog.close();location.reload();}catch(error){status.textContent='Connection interrupted. Your note is still here; try saving again.';}finally{submit.disabled=false;}};
const focus=new URLSearchParams(location.search).get('pin');if(focus&&/^[0-9]+$/.test(focus)){const target=layer.querySelector('[data-pin-id="'+focus+'"]');if(target){let attempts=0;const go=()=>{if(stage.clientWidth<20&&attempts++<20){setTimeout(go,200);return;}target.scrollIntoView({block:'center',inline:'center'});target.focus({preventScroll:true});};setTimeout(go,300);}}
})();'''
