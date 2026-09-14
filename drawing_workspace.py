"""8.14.1 drawing library using the existing document and markup records.

Uploads remain original files. Markup saves create separate, versioned layers.
"""
import inspect
import json
import logging
from pathlib import Path
import re
import uuid
from functools import wraps
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from blueprint_field import esc

log=logging.getLogger('buildcommand.drawings')
LIMIT=500*1024*1024
VERSION='8.14.1'
RELEASE='Drawings Query Fix'


# Use original SVG paths instead of text glyphs. The legacy shell may already
# contain mojibake; replace button contents by stable IDs, never by bad bytes.
TOOL_ICONS={
    'pen':'<path d="m4 16 12-12 4 4L8 20H4v-4Zm10-10 4 4"/>',
    'cloud':'<path d="M6 18a4 4 0 0 1-2-7 4 4 0 0 1 5-5 4 4 0 0 1 7 0 4 4 0 0 1 5 5 4 4 0 0 1-2 7 4 4 0 0 1-7 1 4 4 0 0 1-6-1Z"/>',
    'arrow':'<path d="M4 20 20 4M8 4h12v12"/>',
    'rect':'<rect x="4" y="5" width="16" height="14" rx="1"/>',
    'text':'<path d="M4 5h16M12 5v15M8 20h8"/>',
    'stamp':'<path d="M5 16h14v4H5zM8 16v-3l2-2V6a2 2 0 0 1 4 0v5l2 2v3M4 22h16"/>',
    'calibrate':'<path d="M3 3v18h18M7 16l10-9M7 12v4h4M13 7h4v4"/>',
    'measure':'<path d="m3 16 13-13 5 5L8 21 3 16Zm5-5 3 3m1-7 3 3m1-7 3 3"/>',
    'pan':'<path d="M12 3v18M3 12h18M8 7l4-4 4 4M8 17l4 4 4-4M7 8l-4 4 4 4m10-8 4 4-4 4"/>',
    'undo':'<path d="m8 5-5 5 5 5M3 10h10a6 6 0 0 1 0 12"/>',
    'clear':'<path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 11v6m4-6v6"/>',
    'zoom-out':'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6M7 10h6"/>',
    'zoom-in':'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6M7 10h6m-3-3v6"/>',
    'fit':'<path d="M9 3H3v6m12-6h6v6M3 15v6h6m12-6v6h-6"/>',
    'previous':'<path d="m15 5-7 7 7 7"/>',
    'next':'<path d="m9 5 7 7-7 7"/>',
}
TOOL_IDS={'bcPrev':'previous','bcNext':'next','bcZoomOut':'zoom-out','bcZoomIn':'zoom-in','bcFit':'fit','bcUndo':'undo','bcClear':'clear','bcCalibrate':'calibrate','bcMarkupTrigger':'pen'}


def clean_tool_icons(html):
    def replace(match):
        attrs=match[1]
        if not re.search(r'class="[^"]*\bbc81-(?:tool|iconbtn|trigger)\b',attrs):return match[0]
        tool=re.search(r'data-tool="([^"]+)"',attrs);identity=re.search(r'\bid="([^"]+)"',attrs)
        key=tool[1] if tool else TOOL_IDS.get(identity[1] if identity else '')
        if key not in TOOL_ICONS:return match[0]
        title=re.search(r'\btitle="([^"]+)"',attrs)
        label='Markup' if identity and identity[1]=='bcMarkupTrigger' else title[1] if title else key.title()
        svg='<svg class="drawing-tool-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'+TOOL_ICONS[key]+'</svg>'
        return '<button'+attrs+'>'+svg+'<span class="drawing-tool-label">'+label+'</span></button>'
    return re.sub(r'<button([^>]*)>(.*?)</button>',replace,html,flags=re.S)


TOOL_STYLE='''<style>
.bc81-toolbar{flex-wrap:wrap!important;gap:8px!important}.bc81-toolbar .bc81-iconbtn{width:auto!important;height:auto!important;min-width:62px!important;min-height:48px!important;display:inline-flex!important;flex-direction:column;gap:3px;padding:8px!important}.drawing-tool-icon{display:block;flex-shrink:0;max-width:none!important}.bc81-menu{width:350px!important;max-width:calc(100vw - 32px)!important;box-sizing:border-box;right:0!important;left:auto!important;padding:14px!important}.bc81-grid{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:8px!important}.bc81-toolbar .bc81-tool{width:100%!important;min-width:0!important;height:80px!important;padding:10px 6px!important;box-sizing:border-box;display:flex!important;flex-direction:column;justify-content:center;gap:8px!important}.drawing-tool-label{font:600 13px/1.2 Arial,sans-serif!important;white-space:normal!important;word-break:normal!important;overflow-wrap:normal!important;text-align:center;max-width:100%;letter-spacing:normal!important}.bc81-trigger{display:inline-flex!important;align-items:center;gap:9px;min-height:48px!important}.bc81-trigger .drawing-tool-label{font-size:15px!important}.bc81-settings{flex-wrap:wrap!important}.bc81-toolbar button:focus-visible{outline:3px solid #e8b64e;outline-offset:2px}
</style>'''


def install(ns):
    service=DrawingWorkspace(ns);ns['app'].state.drawing_workspace=service;service.register();return service


class DrawingWorkspace:
    def __init__(self,ns):
        self.ns=ns;self.hub=ns['app'].state.workspace_hub;self.field=self.hub.field;self.db=self.field.db;self.require=self.field.require
        self.original_viewer=next((r.endpoint for r in ns['app'].routes if getattr(r,'path','')=='/documents/{attachment_id}/view' and 'GET' in (getattr(r,'methods',None) or set())),None)
        self.original_drawings=next((r.endpoint for r in ns['app'].routes if getattr(r,'path','')=='/drawings' and 'GET' in (getattr(r,'methods',None) or set())),None)

    def endpoint(self,fn):
        if not inspect.iscoroutinefunction(fn):return self.hub.endpoint(fn)
        @wraps(fn)
        async def wrapped(*args,**kwargs):
            if not self.ns['_bc840_user']():return RedirectResponse('/login',303)
            try:return await fn(*args,**kwargs)
            except self.ns['_BC850_Problem'] as exc:return self.ns['_bc830b_error'](exc.message,exc.status)
        return wrapped

    def context(self,c,aid,write=False):
        user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM attachments WHERE id=? AND company_id=?',(aid,user['company_id'])).fetchone()
        self.require(row is not None,'This drawing is not available.',404)
        row=dict(row);user,project,_=self.hub.user(c,row['project_id'])
        self.require(project is not None,'This project is not assigned to you.')
        if write:user,project=self.field.actor(c,row['project_id'],True)
        return user,row,project

    def legacy_context(self,attachment_id):
        try:
            with self.db() as c:return self.context(c,int(attachment_id))
        except self.ns['_BC850_Problem']:return self.ns['_bc840_user'](),None,None

    def file_path(self,row):
        root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();name=str(row.get('stored_name') or '')
        self.require(name and Path(name).name==name,'The drawing file is unavailable.',404)
        path=root/name
        self.require(not path.is_symlink() and path.is_file() and path.resolve().parent==root,'The drawing file is unavailable.',404)
        return path

    def list_rows(self,c,company_id,project_id,q=''):
        # PgCompat converts '?' to psycopg placeholders. Keep every LIKE
        # pattern in the parameter values: a literal '%.pdf' in SQL is parsed
        # by psycopg as an invalid placeholder even inside SQL quotes.
        pattern='%'+q.lower()[:120]+'%'
        return [dict(r) for r in c.execute(
            "SELECT * FROM attachments WHERE company_id=? AND project_id=? "
            "AND COALESCE(category,'')<>? "
            "AND (LOWER(original_name) LIKE ? OR LOWER(original_name) LIKE ? "
            "OR LOWER(original_name) LIKE ? OR LOWER(original_name) LIKE ? "
            "OR LOWER(original_name) LIKE ?) "
            "AND (LOWER(COALESCE(title,'')) LIKE ? OR LOWER(original_name) LIKE ?) "
            "ORDER BY id DESC LIMIT 100",
            (company_id,project_id,'DRAWING_RELEASE','%.pdf','%.png','%.jpg','%.jpeg','%.webp',pattern,pattern)
        ).fetchall()]

    def index(self,project_id:int=0,q:str=''):
        with self.db() as c:
            user,project,projects=self.hub.user(c,project_id)
            rows=self.list_rows(c,user['company_id'],project['id'],q) if project else []
        body='<div class="hero"><div class="eyebrow">FIELD DRAWINGS</div><h1>Drawings</h1><p>Open plans, check a detail and save a field markup.</p></div>'+self.hub.selector(projects,project,'/workspace/drawings')
        if not project:return self.hub.page('Drawings',body+'<p>Choose your job to see its drawings.</p>')
        pid=project['id']
        manager=self.ns['_bc850_manager'](user)
        if manager:
            body+=f'<section class="card hub-section"><h2>Upload drawings</h2><form method="post" action="/workspace/drawings/projects/{pid}/upload" enctype="multipart/form-data"><p><label for="drawing-title">Drawing set title (optional)</label><br><input id="drawing-title" name="title" maxlength="240" placeholder="Example: Level 1 electrical plans"></p><p><label for="drawing-upload">PDF or image · up to 500 MB</label><br><input id="drawing-upload" name="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" required></p><button>Upload drawings</button></form><p class="hub-help">Existing project PDFs and images appear below. Uploads and markups stay internal until a project leader shares an approved file.</p></section>'
        body+=f'<form method="get" action="/workspace/drawings"><input type="hidden" name="project_id" value="{pid}"><label for="find-drawing">Find a drawing</label> <input id="find-drawing" name="q" maxlength="120" value="{esc(q[:120])}"> <button>Find</button></form><div class="hub-grid hub-section">'
        for row in rows:
            body+='<article class="card"><div class="eyebrow">'+esc(row.get('category') or 'DRAWING')+'</div><h2>'+esc(row.get('title') or row['original_name'])+'</h2><p>'+esc(row['original_name'])+'</p>'+f'<a class="bc840-button" href="/workspace/drawings/{row["id"]}">{"Open &amp; mark up" if manager else "Open drawing"}</a></article>'
        if not rows:body+='<p>No matching drawings. Upload the first plan set for this project.</p>'
        body+='</div>'
        if manager:body+='<details class="card hub-section"><summary>Sheet picker and revision controls</summary>'+self.hub.open_form(pid,'/drawings','Open the existing sheet picker')+'<p>Open a drawing for redlines, clouds, arrows, notes, stamps, calibrated measurements and revision layers.</p></details>'
        return self.hub.page('Drawings',body)

    async def upload(self,project_id:int,file:UploadFile=File(...),title:str=Form('')):
        with self.db() as c:self.field.actor(c,project_id)
        name=Path(str(file.filename or '').replace('\\','/')).name[:240]
        self.require(self.ns['_bc8131_preview_extension'](name) is not None,'Upload a PDF, PNG, JPEG or WebP drawing.',400)
        root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();root.mkdir(parents=True,exist_ok=True)
        stored='drawing-'+uuid.uuid4().hex+Path(name).suffix.lower();path=root/stored;total=0;saved=False
        try:
            with path.open('xb') as dest:
                first=await file.read(1024*1024)
                mime=self.ns['_bc8131_preview_type'](name,first[:16])
                self.require(mime is not None,'This file does not match its PDF or image extension.',400)
                chunk=first
                while chunk:
                    total+=len(chunk);self.require(total<=LIMIT,'This file exceeds the 500 MB upload limit.',413)
                    dest.write(chunk);chunk=await file.read(1024*1024)
            with self.db(True) as c:
                user,project=self.field.actor(c,project_id,True)
                sql='INSERT INTO attachments(company_id,project_id,category,title,original_name,stored_name,mime_type,size_bytes,created_by,created) VALUES(?,?,?,?,?,?,?,?,?,?)'
                vals=(user['company_id'],project_id,'PLANS',title.strip()[:240] or name,name,stored,mime,total,user['id'],self.field.now().isoformat())
                if self.field.postgres:aid=int(c.execute(sql+' RETURNING id',vals).fetchone()['id'])
                else:c.execute(sql,vals);aid=int(c.execute('SELECT last_insert_rowid() AS id').fetchone()['id'])
                self.ns['_bc850_event'](c,user,project_id,None,'DRAWING_UPLOADED:'+str(aid))
            saved=True
            return RedirectResponse(f'/workspace/drawings/{aid}',303)
        finally:
            await file.close()
            if not saved and path.exists():path.unlink()

    def content(self,attachment_id:int):
        with self.db() as c:user,row,project=self.context(c,attachment_id)
        path=self.file_path(row)
        with path.open('rb') as source:media=self.ns['_bc8131_preview_type'](row['original_name'],source.read(16))
        return FileResponse(path,filename=row['original_name'],media_type=media or 'application/octet-stream',content_disposition_type='inline' if media else 'attachment',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'SAMEORIGIN'})

    def view(self,attachment_id:int,request:Request):
        with self.db() as c:
            user,row,project=self.context(c,attachment_id);self.file_path(row)
            if not self.ns['_bc850_manager'](user):
                return self.hub.page('Drawing','<div class="hero"><h1>'+esc(row.get('title') or row['original_name'])+'</h1><p>'+esc(project['name'])+'</p></div><div class="card"><a class="bc840-button" target="_blank" rel="noopener" href="/documents/'+str(attachment_id)+'/content">Open drawing</a><p>Your project leader manages saved field markups.</p></div>')
            latest=c.execute('SELECT MAX(revision_no) AS n FROM document_markup_revisions WHERE company_id=? AND project_id=? AND attachment_id=?',(user['company_id'],project['id'],attachment_id)).fetchone()
            rev=int(latest['n'] or 0)
        self.require(self.original_viewer is not None,'The drawing viewer is unavailable.',503)
        response=self.original_viewer(attachment_id,request)
        if response.status_code!=200:return response
        html=response.body.decode('utf-8')
        html=html.replace('← Documents','← Drawings').replace('href="/documents"','href="/workspace/drawings?project_id='+str(project['id'])+'"')
        html=html.replace('/api/documents/'+str(attachment_id)+'/markups','/workspace/drawings/'+str(attachment_id)+'/markups')
        # The original JavaScript builds the URL with DOC_ID; replace that exact
        # path as well and add a revision guard without changing stored geometry.
        html=html.replace('"/api/documents/"+DOC_ID+"/markups"','"/workspace/drawings/"+DOC_ID+"/markups"')
        html=html.replace('JSON.stringify({revision_title:',f'JSON.stringify({{base_revision:{rev},revision_title:')
        try:page=max(1,min(10000,int(request.query_params.get('page') or 1)))
        except ValueError:page=1
        html=html.replace('let tool="pan",pageNum=1,',f'let tool="pan",pageNum={page},')
        html=html.replace('getDocument(window.BC_DOC_URL)','getDocument({url:window.BC_DOC_URL,isEvalSupported:false})')
        html=html.replace('pdf=p;await fitToScreen()','pdf=p;pageNum=Math.min(pageNum,pdf.numPages);await fitToScreen()')
        html=html.replace('<div id="bcToolStatus"','<p class="hub-help" id="drawing-load-state" role="status">Use Pan to move around. Open Markup to choose a tool, then save your revision below.</p><div id="bcToolStatus"',1)
        html=html.replace('<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js">','<script onerror="document.getElementById(\'drawing-load-state\').textContent=\'The drawing renderer could not load. Use Open Original to view the PDF, then reload to mark up.\'" src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js">')
        html=html.replace('pdf=p;pageNum=Math.min(pageNum,pdf.numPages);await fitToScreen()})', 'pdf=p;pageNum=Math.min(pageNum,pdf.numPages);await fitToScreen()}).catch(()=>{document.getElementById("drawing-load-state").textContent="This PDF could not be rendered. Use Open Original to check the file, then reload to mark up.";})')
        html=clean_tool_icons(html)
        html=html.replace('Drawing & Document Workspace','Drawings')
        html=html.replace('</head>','<style>.bc81-toolbar button{color:#fff!important;background:#25394d!important;min-height:44px}.bc81-toolbar{position:sticky;top:8px;z-index:10}.bc81-menu{color:white}.bc81-trigger{min-width:118px}.bc81-menu{width:290px;max-width:calc(100vw - 48px);min-width:0!important}.bc81-grid{grid-template-columns:repeat(3,1fr)!important}.bc81-tool{width:80px!important;height:70px!important;flex-direction:column;gap:5px}.drawing-tool-label{font-size:11px;line-height:1.15}.bc81-tool:focus-visible{outline:3px solid #e7b448}.bc81-settings select{color:#172b3e}@media(max-width:700px){#bcStageWrap{min-height:320px!important;height:65vh!important}}</style></head>',1)
        support='''<script>(()=>{let dirty=false;const canvas=document.getElementById('bcMarkupCanvas');if(canvas)canvas.addEventListener('pointerdown',()=>{const mode=document.getElementById('bcToolStatus');if(mode&&!mode.textContent.includes('Tool: Pan'))dirty=true;});for(const id of ['bcUndo','bcClear','bcRevTitle','bcRevNotes']){const el=document.getElementById(id);if(el)el.addEventListener(el.tagName==='BUTTON'?'click':'input',()=>{dirty=true;});}window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});for(const pair of [['bcSaveMarkup','bcSaveStatus'],['bcPublishCurrent','bcPublishStatus']]){const button=document.getElementById(pair[0]),status=document.getElementById(pair[1]);if(!button||!button.onclick)continue;const previous=button.onclick;button.onclick=async function(event){try{await previous.call(this,event);if(pair[0]==='bcSaveMarkup'&&status.textContent.startsWith('Saved markup revision'))dirty=false;}catch(error){status.textContent='The request could not finish. Your notes are still on this page. Check your connection and try again.';}};}})();</script>'''
        html=html.replace('</body>',support+'</body>',1)
        html=html.replace('</head>',TOOL_STYLE+'</head>',1)
        if not re.search(r'<meta\s+charset=',html,re.I):html=html.replace('<head>','<head><meta charset="utf-8">',1)
        return HTMLResponse(html,headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin'})

    async def save_markup(self,attachment_id:int,request:Request):
        self.require(self.ns['_bc840_same_origin'](request),'Reload the drawing from this BuildCommand address before saving.',403)
        parts=[];size=0
        async for chunk in request.stream():
            size+=len(chunk);self.require(size<=5*1024*1024,'The markup is too large.',413);parts.append(chunk)
        raw=b''.join(parts)
        try:
            data=json.loads(raw);annotations=data['annotations'];base=data['base_revision']
            self.require(type(base) is int and base>=0,'Reload the drawing before saving.',400)
            self.require(isinstance(annotations,dict) and isinstance(annotations.get('pages'),dict),'The markup must contain drawing pages.',400)
            payload=json.dumps(annotations,separators=(',',':'),allow_nan=False)
        except (ValueError,TypeError,KeyError):return JSONResponse({'error':'Reload the drawing and try saving again.'},400)
        with self.db(True) as c:
            user,row,project=self.context(c,attachment_id,True)
            latest=c.execute('SELECT MAX(revision_no) AS n FROM document_markup_revisions WHERE company_id=? AND project_id=? AND attachment_id=?',(user['company_id'],project['id'],attachment_id)).fetchone()
            current=int(latest['n'] or 0)
            if current!=base:return JSONResponse({'error':'A newer markup was saved. Keep your notes, reload the drawing and review that revision before saving.'},409)
            rev=current+1
            sql='INSERT INTO document_markup_revisions(company_id,project_id,attachment_id,revision_no,revision_title,notes,annotation_json,created_by,created) VALUES(?,?,?,?,?,?,?,?,?)'
            vals=(user['company_id'],project['id'],attachment_id,rev,str(data.get('revision_title') or '')[:250],str(data.get('notes') or '')[:4000],payload,user['id'],self.field.now().isoformat())
            if self.field.postgres:rid=int(c.execute(sql+' RETURNING id',vals).fetchone()['id'])
            else:c.execute(sql,vals);rid=int(c.execute('SELECT last_insert_rowid() AS id').fetchone()['id'])
            self.ns['_bc850_event'](c,user,project['id'],None,'DRAWING_MARKUP_SAVED:'+str(attachment_id)+':'+str(rev))
        return JSONResponse({'status':'ok','markup_revision_id':rid,'revision_no':rev,'attachment_id':attachment_id})

    def health(self):
        checks=dict(self.hub.health()['checks'])
        checks['drawings_hotfix_release_active']=self.ns.get('BUILD_COMMAND_RELEASE') in {VERSION,'8.15.0','8.16.0'}
        try:
            # Exercise the same bounded read as the page, including all LIKE
            # parameters, without returning any customer data or writing rows.
            with self.db() as c:self.list_rows(c,0,0)
            checks['drawing_library_query_executed']=True
        except Exception:
            log.exception('Drawing library query health check failed')
            checks['drawing_library_query_executed']=False
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,
            status='ok' if ok else 'degraded',checks=checks,passed=sum(checks.values()),
            total=len(checks),data_reset=False,
            scope='Active routes, installation and execution of the drawing-list query on the configured database. No customer records are returned. Verify opening Drawings, search, uploads and saved markups on staging.'),status_code=200 if ok else 503)

    def register(self):
        # The old renderer/API call this context dynamically, so they now honor
        # appointed-project access in addition to the existing company boundary.
        self.ns['_bc181877_context']=self.legacy_context
        self.hub.destinations_original=self.hub.destinations
        original=self.hub.destinations
        self.hub.destinations=lambda:original()|{'/drawings'}
        for path,method,fn in [('/workspace/drawings','GET',self.index),('/workspace/drawings/projects/{project_id}/upload','POST',self.upload),('/workspace/drawings/{attachment_id}','GET',self.view),('/workspace/drawings/{attachment_id}/markups','POST',self.save_markup),('/documents/{attachment_id}/content','GET',self.content),('/documents/{attachment_id}/view','GET',self.view),('/api/documents/{attachment_id}/markups','POST',self.save_markup)]:
            wrapped=self.endpoint(fn);self.ns['_bc840_replace'](path,method,wrapped);self.hub.routes.append((method,path,wrapped))
        path='/health/drawings-query-8-14-1'
        self.ns['app'].add_api_route(path,self.health,methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add(path)
