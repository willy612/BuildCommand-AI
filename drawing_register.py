"""Original BuildCommand drawing register: reviewed sheets over preserved files.

PDF.js reads page suggestions in the browser; the server validates and records
the leader's reviewed sheet list. Sheet references preserve the original set
and existing per-page markup layers. No OCR or per-sheet file export is claimed.
"""
import json
import logging
import re
from datetime import date
from urllib.parse import urlencode

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from blueprint_field import esc, packed, digest

VERSION='8.15.0'
RELEASE='Drawing Register & Sheet Viewer'
MAX_SHEETS=500
DISCIPLINES=('General','Civil','Architectural','Structural','Mechanical','Electrical','Plumbing','Fire protection','Landscape','Other')
PDFJS='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js'
log=logging.getLogger('buildcommand.drawing_register')


def js(value):
    return packed(value).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')


def install(ns):
    service=DrawingRegister(ns)
    ns['app'].state.drawing_register=service
    service.register()
    return service


class DrawingRegister:
    def __init__(self,ns):
        self.ns=ns;self.drawings=ns['app'].state.drawing_workspace;self.hub=self.drawings.hub
        self.field=self.drawings.field;self.db=self.field.db;self.require=self.field.require
        self.routes=[];self.schema_ready=self.initialize()

    def initialize(self):
        try:
            with self.db(True) as c:
                key='BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_sets(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    attachment_id BIGINT NOT NULL,name TEXT NOT NULL,area TEXT NOT NULL,
                    area_key TEXT NOT NULL,revision_label TEXT NOT NULL,drawing_date TEXT NOT NULL,
                    status TEXT NOT NULL,version INTEGER NOT NULL,review_json TEXT NOT NULL,
                    review_hash TEXT NOT NULL,reviewed_by BIGINT,created_by BIGINT NOT NULL,
                    created TEXT NOT NULL,published TEXT,
                    UNIQUE(company_id,project_id,attachment_id))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_sheets(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    set_id BIGINT NOT NULL,attachment_id BIGINT NOT NULL,page_number INTEGER NOT NULL,
                    area TEXT NOT NULL,area_key TEXT NOT NULL,sheet_number TEXT NOT NULL,
                    sheet_key TEXT NOT NULL,title TEXT NOT NULL,discipline TEXT NOT NULL,
                    revision_label TEXT NOT NULL,revision_no INTEGER NOT NULL,drawing_date TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created TEXT NOT NULL,
                    UNIQUE(set_id,page_number),UNIQUE(company_id,project_id,area_key,sheet_key,revision_no))''')
                c.execute('''CREATE TABLE IF NOT EXISTS bc_drawing_heads(
                    company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,area_key TEXT NOT NULL,
                    sheet_key TEXT NOT NULL,sheet_id BIGINT NOT NULL,
                    PRIMARY KEY(company_id,project_id,area_key,sheet_key))''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_drawing_sets_project ON bc_drawing_sets(company_id,project_id,status)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_drawing_sheets_project ON bc_drawing_sheets(company_id,project_id,area_key,sheet_key)')
            return True
        except Exception:
            log.exception('Drawing register initialization failed');return False

    def ready(self):self.require(self.schema_ready,'Drawing setup is unavailable. Ask your administrator to check the release installation.',503)

    def insert(self,c,sql,params):
        if self.field.postgres:return int(c.execute(sql+' RETURNING id',params).fetchone()['id'])
        c.execute(sql,params);return int(c.execute('SELECT last_insert_rowid() AS id').fetchone()['id'])

    def page(self,title,body):return self.hub.page(title,STYLE+body)

    def set_context(self,c,set_id,write=False):
        self.ready();user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM bc_drawing_sets WHERE id=? AND company_id=?',(set_id,user['company_id'])).fetchone()
        self.require(row is not None,'This drawing set is not available.',404);row=dict(row)
        user,project=self.field.actor(c,row['project_id'],write)
        if write:
            # The first read identifies the lock scope. Reload the review after
            # acquiring those locks so a competing review cannot be overwritten.
            fresh=c.execute('SELECT * FROM bc_drawing_sets WHERE id=? AND company_id=? AND project_id=?'+self.field.lock,
                            (set_id,user['company_id'],project['id'])).fetchone()
            self.require(fresh is not None,'This drawing set is not available.',404);row=dict(fresh)
        source=c.execute('SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?',(row['attachment_id'],row['company_id'],row['project_id'])).fetchone()
        self.require(source is not None,'The original drawing file is unavailable.',404)
        return user,project,row,dict(source)

    def sheet_context(self,c,sheet_id):
        self.ready();user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM bc_drawing_sheets WHERE id=? AND company_id=?',(sheet_id,user['company_id'])).fetchone()
        self.require(row is not None,'This drawing sheet is not available.',404);row=dict(row)
        user,project,_=self.hub.user(c,row['project_id']);self.require(project is not None,'This project is not assigned to you.')
        self.drawings.context(c,row['attachment_id'])
        return user,project,row

    def current_rows(self,c,cid,pid,q='',discipline=''):
        return [dict(r) for r in c.execute('''SELECT s.* FROM bc_drawing_heads h JOIN bc_drawing_sheets s
            ON s.id=h.sheet_id AND s.company_id=h.company_id AND s.project_id=h.project_id
            WHERE h.company_id=? AND h.project_id=?
            AND (LOWER(s.sheet_number) LIKE ? OR LOWER(s.title) LIKE ?)
            AND (?='' OR s.discipline=?) ORDER BY s.area_key,s.discipline,s.sheet_key LIMIT 500''',
            (cid,pid,'%'+q.lower()[:120]+'%','%'+q.lower()[:120]+'%',discipline,discipline)).fetchall()]

    def index(self,project_id:int=0,q:str='',discipline:str=''):
        self.ready()
        with self.db() as c:
            user,project,projects=self.hub.user(c,project_id)
            rows=self.current_rows(c,user['company_id'],project['id'],q,discipline) if project else []
            manager=self.ns['_bc850_manager'](user)
            sets=[dict(r) for r in c.execute('SELECT * FROM bc_drawing_sets WHERE company_id=? AND project_id=? AND status<>? ORDER BY id DESC LIMIT 100',(user['company_id'],project['id'],'ARCHIVED')).fetchall()] if project and manager else []
        body='<div class="dr-heading"><div><div class="eyebrow">YOUR JOB · DRAWINGS</div><h1>Find the right sheet.</h1><p>Current drawings, clear revisions and room to work.</p></div></div>'+self.hub.selector(projects,project,'/workspace/drawings')
        if not project:return self.page('Drawings',body+'<div class="card"><p>Choose your job to see its drawing register.</p></div>')
        pid=project['id'];pending=[s for s in sets if s['status']=='DRAFT']
        if manager:
            body+=f'<div class="dr-actions"><a class="bc840-button" href="#upload-drawings">Upload drawing set</a><a class="dr-secondary" href="/workspace/drawings/import?project_id={pid}">Add existing plans</a>'+(f'<a href="#drawing-sets">{len(pending)} set(s) need review</a>' if pending else '')+'</div>'
        body+=f'<form class="dr-search" method="get" action="/workspace/drawings"><input type="hidden" name="project_id" value="{pid}"><label>Find a sheet<input name="q" value="{esc(q[:120])}" placeholder="Sheet number or title" maxlength="120"></label><label>Discipline<select name="discipline"><option value="">All disciplines</option>'+''.join(f'<option {"selected" if discipline==d else ""}>{esc(d)}</option>' for d in DISCIPLINES)+'</select></label><button>Find drawings</button></form>'
        body+='<div class="card dr-register"><div class="dr-heading"><h2>Current drawings</h2><span>'+str(len(rows))+' shown · up to 500</span></div>'
        if rows:
            body+='<div class="dr-table-wrap"><table><thead><tr><th>Sheet</th><th>Drawing</th><th>Discipline</th><th>Revision</th><th>Issued</th><th></th></tr></thead><tbody>'
            for r in rows:
                body+=f'<tr><td><a class="dr-sheet-number" href="/workspace/drawing-sheets/{r["id"]}">{esc(r["sheet_number"])}</a></td><td><a href="/workspace/drawing-sheets/{r["id"]}">{esc(r["title"])}</a><small>{esc(r["area"])}</small></td><td>{esc(r["discipline"])}</td><td><span class="dr-tag">{esc(r["revision_label"] or "Original")}</span></td><td>{esc(r["drawing_date"] or "—")}</td><td><a href="/workspace/drawing-sheets/{r["id"]}/history">History</a></td></tr>'
            body+='</tbody></table></div>'
        else:body+='<div class="dr-empty"><h3>'+('No matching sheets.' if q or discipline else 'Your drawing register starts here.')+'</h3><p>'+('Try another sheet number or discipline.' if q or discipline else 'Upload a drawing set, review its sheet list, then make it current. Existing plan files can be added without uploading again.')+'</p></div>'
        body+='</div>'
        if manager:
            body+=f'<details class="card dr-section" id="upload-drawings"><summary>Upload a drawing set</summary><p>A PDF becomes a list of individual sheets. Review the sheet numbers before making them current.</p><form method="post" action="/workspace/drawing-sets/projects/{pid}/upload" enctype="multipart/form-data" class="dr-upload"><label>Set name<input name="name" maxlength="240" required placeholder="Issued for construction · September"></label><label>Building / area<input name="area" maxlength="100" value="Main"></label><label>Revision label<input name="revision_label" maxlength="40" placeholder="Original, Rev 1, Addendum 2"></label><label>Drawing date<input name="drawing_date" type="date"></label><label class="dr-wide">PDF or image · up to 500 MB · up to 500 sheets<input type="file" name="file" accept=".pdf,.png,.jpg,.jpeg,.webp" required></label><button>Upload &amp; review sheets</button></form></details>'
            body+='<details class="card dr-section" id="drawing-sets" '+('open' if pending else '')+'><summary>Drawing sets &amp; pending reviews</summary><p>Making sheets current updates the internal drawing register. Trade sharing remains a separate approval.</p>'
            body+=''.join(f'<div class="dr-set"><div><b>{esc(s["name"])}</b><small>{esc(s["area"])} · {esc(s["revision_label"] or "Original")} · {esc(s["created"][:10])}</small></div><a href="/workspace/drawing-sets/{s["id"]}">{"Review sheets" if s["status"]=="DRAFT" else "Open set"}</a></div>' for s in sets) or '<p>No drawing sets added yet.</p>'
            body+='</details>'
        return self.page('Drawings',body)

    def imports(self,project_id:int=0):
        self.ready()
        with self.db() as c:
            user,project,projects=self.hub.user(c,project_id,manager=True)
            rows=self.drawings.list_rows(c,user['company_id'],project['id']) if project else []
            registered={int(r['attachment_id']) for r in c.execute('SELECT attachment_id FROM bc_drawing_sets WHERE company_id=? AND project_id=?',(user['company_id'],project['id'])).fetchall()} if project else set()
        body='<h1>Add existing plans</h1><p>Choose only construction drawings. Other PDFs and photos stay out of the drawing register.</p>'+self.hub.selector(projects,project,'/workspace/drawings/import')
        if project:
            body+='<div class="dr-actions"><a href="/workspace/drawings?project_id='+str(project['id'])+'">← Drawing register</a></div>'
            for r in rows:
                if r['id'] in registered:continue
                body+=f'<details class="card dr-section"><summary>{esc(r.get("title") or r["original_name"])}</summary><p>{esc(r["original_name"])}</p><form method="post" action="/workspace/drawing-sets/projects/{project["id"]}/import/{r["id"]}" class="dr-upload"><label>Set name<input name="name" maxlength="240" value="{esc(r.get("title") or r["original_name"])}" required></label><label>Building / area<input name="area" maxlength="100" value="Main"></label><label>Revision label<input name="revision_label" maxlength="40"></label><label>Drawing date<input name="drawing_date" type="date"></label><button>Add to drawing review</button></form></details>'
            if not any(r['id'] not in registered for r in rows):body+='<div class="card"><p>No additional PDF or image files to add. Upload a new drawing set from Drawings.</p></div>'
        return self.page('Add existing plans',body)

    def metadata(self,name,area,revision_label,drawing_date):
        name=str(name).strip();area=str(area).strip() or 'Main';revision_label=str(revision_label).strip();drawing_date=str(drawing_date).strip()
        self.require(0<len(name)<=240 and len(area)<=100 and len(revision_label)<=40,'Enter a set name and shorter drawing details.',400)
        if drawing_date:
            try:self.require(date.fromisoformat(drawing_date).isoformat()==drawing_date,'Use a valid drawing date.',400)
            except ValueError:self.require(False,'Use a valid drawing date.',400)
        return name,area,revision_label,drawing_date

    def add_set(self,project_id,attachment_id,name,area,revision_label,drawing_date):
        self.ready();name,area,revision_label,drawing_date=self.metadata(name,area,revision_label,drawing_date)
        with self.db(True) as c:
            user,project=self.field.actor(c,project_id,True)
            source=c.execute('SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?',(attachment_id,user['company_id'],project_id)).fetchone()
            self.require(source is not None,'Choose a drawing from this project.',404);source=dict(source)
            path=self.drawings.file_path(source)
            with path.open('rb') as f:kind=self.ns['_bc8131_preview_type'](source['original_name'],f.read(16))
            self.require(kind is not None,'Choose a valid PDF or image drawing.',400)
            prior=c.execute('SELECT id,status FROM bc_drawing_sets WHERE company_id=? AND project_id=? AND attachment_id=?',(user['company_id'],project_id,attachment_id)).fetchone()
            if prior:
                self.require(prior['status']!='ARCHIVED','This set was removed from review. Upload a new copy to start a new review.',409)
                return int(prior['id'])
            sid=self.insert(c,'''INSERT INTO bc_drawing_sets(company_id,project_id,attachment_id,name,area,area_key,revision_label,drawing_date,status,version,review_json,review_hash,created_by,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project_id,attachment_id,name,area,area.casefold(),revision_label,drawing_date,'DRAFT',1,'','',user['id'],self.field.now().isoformat()))
            self.ns['_bc850_event'](c,user,project_id,None,'DRAWING_SET_ADDED:'+str(sid))
        return sid

    async def upload(self,project_id:int,file:UploadFile=File(...),name:str=Form(...),area:str=Form('Main'),revision_label:str=Form(''),drawing_date:str=Form('')):
        self.ready();self.metadata(name,area,revision_label,drawing_date)
        response=await self.drawings.upload(project_id,file,name)
        aid=int(response.headers['location'].rsplit('/',1)[1])
        sid=self.add_set(project_id,aid,name,area,revision_label,drawing_date)
        return RedirectResponse('/workspace/drawing-sets/'+str(sid),303)

    def import_file(self,project_id:int,attachment_id:int,name:str=Form(...),area:str=Form('Main'),revision_label:str=Form(''),drawing_date:str=Form('')):
        sid=self.add_set(project_id,attachment_id,name,area,revision_label,drawing_date)
        return RedirectResponse('/workspace/drawing-sets/'+str(sid),303)

    def set_page(self,set_id:int):
        with self.db() as c:
            user,project,row,source=self.set_context(c,set_id)
            sheets=[dict(r) for r in c.execute('SELECT * FROM bc_drawing_sheets WHERE set_id=? AND company_id=? AND project_id=? ORDER BY page_number',(set_id,user['company_id'],project['id'])).fetchall()]
        body=f'<p><a href="/workspace/drawings?project_id={project["id"]}">← Drawing register</a></p><div class="eyebrow">{esc(row["status"])} · {esc(row["area"])}</div><h1>{esc(row["name"])}</h1><p>{esc(source["original_name"])} · {esc(row["revision_label"] or "Original")}</p>'
        if row['status']!='DRAFT':
            body+='<div class="card"><h2>Sheets in this set</h2>'+(''.join(f'<p><a href="/workspace/drawing-sheets/{s["id"]}"><b>{esc(s["sheet_number"])}</b> · {esc(s["title"])}</a></p>' for s in sheets) or '<p>This draft was removed from review. The original file is preserved.</p>')+'</div>'
            return self.page('Drawing set',body)
        saved=json.loads(row['review_json'])['sheets'] if row['review_json'] else []
        config=dict(set_id=set_id,version=row['version'],source='/documents/'+str(source['id'])+'/content',pdf=source['original_name'].lower().endswith('.pdf'),sheets=saved,disciplines=DISCIPLINES,max_sheets=MAX_SHEETS)
        body+=f'<div class="card"><h2>1. Check the sheet list</h2><p>Read PDF sheets to get page and sheet-number suggestions. Check every number and title. Scanned sheets may need manual names; no OCR is run here.</p><div class="dr-actions"><button type="button" id="dr-read">Read PDF sheets</button><button type="button" id="dr-add">Add a sheet manually</button><a href="/documents/{source["id"]}/content" target="_blank" rel="noopener">Open original set</a></div><p role="status" id="dr-status"></p><div class="dr-table-wrap"><table class="dr-edit"><thead><tr><th>Page</th><th>Sheet number</th><th>Drawing title</th><th>Discipline</th><th></th></tr></thead><tbody id="dr-sheet-rows"></tbody></table></div><p>Each row opens the specified page of the original set. Include only pages that belong in the register.</p><button type="button" id="dr-review">Review &amp; continue</button><noscript><p>Enable JavaScript to prepare the sheet list. You can still open the original file.</p></noscript></div>'
        body+=f'<details class="card dr-section"><summary>Remove this draft from review</summary><form method="post" action="/workspace/drawing-sets/{set_id}/archive"><input type="hidden" name="version" value="{row["version"]}"><p>The original uploaded file will remain in project files.</p><button>Remove draft</button></form></details>'
        body+=f'<script>window.BC_DRAWING_REVIEW={js(config)};</script><script src="{PDFJS}"></script><script>'+REVIEW_JS+'</script>'
        return self.page('Review drawing sheets',body)

    def normalized_sheets(self,data):
        self.require(isinstance(data,list) and 0<len(data)<=MAX_SHEETS,'Review between 1 and 500 drawing sheets.',400)
        rows=[];pages=set();keys=set()
        for item in data:
            self.require(isinstance(item,dict),'Check the sheet list.',400)
            page=item.get('page_number');number=str(item.get('sheet_number') or '').strip();title=str(item.get('title') or '').strip();discipline=str(item.get('discipline') or 'General')
            self.require(type(page) is int and 1<=page<=MAX_SHEETS,'Use page numbers from 1 to 500.',400)
            self.require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}',number)),'Use a sheet number of up to 40 letters, numbers, spaces, periods or dashes.',400)
            key=re.sub(r'\s+','',number).upper()
            self.require(page not in pages and key not in keys,'Each page and sheet number must appear only once in a set.',400)
            self.require(0<len(title)<=240 and discipline in DISCIPLINES,'Give every sheet a title and choose its discipline.',400)
            pages.add(page);keys.add(key);rows.append(dict(page_number=page,sheet_number=number,sheet_key=key,title=title,discipline=discipline))
        return sorted(rows,key=lambda r:r['page_number'])

    def heads(self,c,row,sheets):
        rows=c.execute('SELECT sheet_key,sheet_id FROM bc_drawing_heads WHERE company_id=? AND project_id=? AND area_key=?',(row['company_id'],row['project_id'],row['area_key'])).fetchall()
        current={r['sheet_key']:int(r['sheet_id']) for r in rows}
        return {s['sheet_key']:current.get(s['sheet_key'],0) for s in sheets}

    async def review(self,set_id:int,request:Request):
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk);self.require(size<=1024*1024,'The drawing review is too large.',413);chunks.append(chunk)
        try:data=json.loads(b''.join(chunks));self.require(isinstance(data,dict),'Check the review form.',400)
        except (ValueError,TypeError):self.require(False,'Check the review form.',400)
        sheets=self.normalized_sheets(data.get('sheets'));version=data.get('version')
        self.require(type(version) is int,'Reload this review before continuing.',400)
        with self.db(True) as c:
            user,project,row,source=self.set_context(c,set_id,True)
            self.require(row['status']=='DRAFT' and row['version']==version,'This set changed. Reload it before reviewing.',409)
            self.drawings.file_path(source)
            self.require(source['original_name'].lower().endswith('.pdf') or all(s['page_number']==1 for s in sheets),'An image has only one sheet.',400)
            review=dict(set_id=set_id,attachment_id=source['id'],sheets=sheets,heads=self.heads(c,row,sheets),version=version+1)
            token=digest(packed(review))
            c.execute('UPDATE bc_drawing_sets SET review_json=?,review_hash=?,reviewed_by=?,version=? WHERE id=? AND company_id=?',(packed(review),token,user['id'],version+1,set_id,user['company_id']))
        return JSONResponse({'review_url':'/workspace/drawing-sets/'+str(set_id)+'/review'})

    def review_page(self,set_id:int):
        with self.db() as c:
            user,project,row,source=self.set_context(c,set_id)
            self.require(row['status']=='DRAFT' and row['review_json'],'Prepare the sheet list before making it current.',409)
            self.require(row['reviewed_by']==user['id'],'Open the sheet list and review it with your account first.',403)
            review=json.loads(row['review_json']);heads=self.heads(c,row,review['sheets'])
            self.require(heads==review['heads'],'Current drawings changed. Return to the sheet list and review again.',409)
        changes=sum(bool(x) for x in heads.values())
        body=f'<p><a href="/workspace/drawing-sets/{set_id}">← Edit sheet list</a></p><div class="eyebrow">REVIEW BEFORE APPLYING</div><h1>Make these sheets current?</h1><p>{esc(row["name"])} · {esc(row["area"])} · {esc(row["revision_label"] or "Original")}</p><div class="card"><h2>{len(review["sheets"])} sheets · {changes} replace current sheets</h2><p>Replaced sheets remain in revision history. Drawings in other areas and sheets absent from this set stay current.</p><div class="dr-table-wrap"><table><thead><tr><th>Page</th><th>Sheet</th><th>Title</th><th>Change</th></tr></thead><tbody>'
        for s in review['sheets']:
            previous=heads[s['sheet_key']]
            change=f'<a href="/workspace/drawing-sheets/{previous}" target="_blank" rel="noopener">Replaces current sheet</a>' if previous else 'New sheet'
            body+=f'<tr><td>{s["page_number"]}</td><td>{esc(s["sheet_number"])}</td><td>{esc(s["title"])}</td><td>{change}</td></tr>'
        body+=f'</tbody></table></div><form method="post" action="/workspace/drawing-sets/{set_id}/publish"><input type="hidden" name="version" value="{row["version"]}"><input type="hidden" name="review_hash" value="{esc(row["review_hash"])}"><p><label><input type="checkbox" name="confirmed" value="yes" required> I checked the pages, sheet numbers and revisions for this area.</label></p><p>This updates the internal register. It does not send files or grant subcontractor access.</p><button>Make sheets current</button></form></div>'
        return self.page('Confirm current drawings',body)

    def publish(self,set_id:int,version:int=Form(...),review_hash:str=Form(...),confirmed:str=Form('')):
        self.require(confirmed=='yes','Confirm the reviewed sheet list before making it current.',400)
        with self.db(True) as c:
            user,project,row,source=self.set_context(c,set_id,True)
            self.require(row['version']==version and row['review_hash']==review_hash and bool(review_hash),'This review changed. Review the sheet list again.',409)
            self.require(row['reviewed_by']==user['id'],'Review the sheet list with your account first.',403)
            if row['status']=='PUBLISHED':return RedirectResponse('/workspace/drawing-sets/'+str(set_id),303)
            self.require(row['status']=='DRAFT','This draft is no longer available.',409)
            review=json.loads(row['review_json']);self.require(self.heads(c,row,review['sheets'])==review['heads'],'Current drawings changed. Review this set again before replacing them.',409)
            self.drawings.file_path(source);now=self.field.now().isoformat()
            for s in review['sheets']:
                previous=review['heads'][s['sheet_key']];revision=1
                if previous:
                    last=c.execute('SELECT revision_no FROM bc_drawing_sheets WHERE id=? AND company_id=? AND project_id=?',(previous,user['company_id'],project['id'])).fetchone()
                    self.require(last is not None,'The current revision changed. Review this set again.',409);revision=int(last['revision_no'])+1
                sid=self.insert(c,'''INSERT INTO bc_drawing_sheets(company_id,project_id,set_id,attachment_id,page_number,area,area_key,sheet_number,sheet_key,title,discipline,revision_label,revision_no,drawing_date,created_by,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(user['company_id'],project['id'],set_id,source['id'],s['page_number'],row['area'],row['area_key'],s['sheet_number'],s['sheet_key'],s['title'],s['discipline'],row['revision_label'],revision,row['drawing_date'],user['id'],now))
                # Explicit RETURNING prevents the legacy PgCompat adapter from
                # guessing an 'id' column and rolling back the whole publish.
                c.execute('''INSERT INTO bc_drawing_heads(company_id,project_id,area_key,sheet_key,sheet_id) VALUES(?,?,?,?,?)
                    ON CONFLICT(company_id,project_id,area_key,sheet_key) DO UPDATE SET sheet_id=excluded.sheet_id
                    RETURNING sheet_id''',(user['company_id'],project['id'],row['area_key'],s['sheet_key'],sid)).fetchone()
            c.execute('UPDATE bc_drawing_sets SET status=?,published=? WHERE id=? AND company_id=?',('PUBLISHED',now,set_id,user['company_id']))
            self.ns['_bc850_event'](c,user,project['id'],None,'DRAWING_SET_CURRENT:'+str(set_id))
        return RedirectResponse('/workspace/drawings?project_id='+str(project['id']),303)

    def archive(self,set_id:int,version:int=Form(...)):
        with self.db(True) as c:
            user,project,row,source=self.set_context(c,set_id,True)
            self.require(row['status']=='DRAFT' and row['version']==version,'This set changed. Reload before removing the draft.',409)
            c.execute('UPDATE bc_drawing_sets SET status=?,version=? WHERE id=? AND company_id=?',('ARCHIVED',version+1,set_id,user['company_id']))
            self.ns['_bc850_event'](c,user,project['id'],None,'DRAWING_SET_ARCHIVED:'+str(set_id))
        return RedirectResponse('/workspace/drawings?project_id='+str(project['id']),303)

    def history(self,sheet_id:int):
        with self.db() as c:
            user,project,row=self.sheet_context(c,sheet_id)
            revisions=[dict(r) for r in c.execute('SELECT * FROM bc_drawing_sheets WHERE company_id=? AND project_id=? AND area_key=? AND sheet_key=? ORDER BY revision_no DESC',(user['company_id'],project['id'],row['area_key'],row['sheet_key'])).fetchall()]
        body=f'<p><a href="/workspace/drawings?project_id={project["id"]}">← Drawing register</a></p><h1>{esc(row["sheet_number"])} · Revision history</h1><p>{esc(row["area"])}</p><div class="card">'
        for index,r in enumerate(revisions):body+=f'<div class="dr-set"><div><b>{esc(r["revision_label"] or "Original")}</b><small>{esc(r["title"])} · {esc(r["created"][:10])} · {"Current" if index==0 else "Previous revision"}</small></div><a href="/workspace/drawing-sheets/{r["id"]}">Open revision</a></div>'
        return self.page('Drawing revision history',body+'</div>')

    def viewer(self,sheet_id:int,request:Request):
        with self.db() as c:
            user,project,row=self.sheet_context(c,sheet_id)
            current=self.current_rows(c,user['company_id'],project['id'])
            head=c.execute('SELECT sheet_id FROM bc_drawing_heads WHERE company_id=? AND project_id=? AND area_key=? AND sheet_key=?',(user['company_id'],project['id'],row['area_key'],row['sheet_key'])).fetchone()
        is_current=head is not None and int(head['sheet_id'])==sheet_id
        warning='' if is_current else '<strong class="dr-old">Previous revision — check the current sheet before using this direction.</strong>'
        header=f'<header class="dr-view-header"><a href="/workspace/drawings?project_id={project["id"]}">← Drawings</a><div><b>{esc(row["sheet_number"])}</b> · {esc(row["title"])}<small>{esc(project["name"])} · {esc(row["area"])} · {esc(row["revision_label"] or "Original")}</small></div><a href="/workspace/drawing-sheets/{sheet_id}/history">Revisions</a><a href="/workspace/brain?project_id={project["id"]}">Ask BuildCommand</a></header>'
        rail='<aside class="dr-sheet-rail"><label>Find a sheet<input id="dr-view-filter" placeholder="Number or title"></label><nav aria-label="Current drawing sheets">'
        for s in current:rail+=f'<a class="dr-rail-link {"active" if s["id"]==sheet_id else ""}" href="/workspace/drawing-sheets/{s["id"]}" {"aria-current=page" if s["id"]==sheet_id else ""}><b>{esc(s["sheet_number"])}</b><span>{esc(s["title"])}</span><small>{esc(s["area"])}</small></a>'
        rail+='</nav></aside>'
        if self.ns['_bc850_manager'](user):
            scoped=Request({**request.scope,'query_string':urlencode({'page':row['page_number']}).encode()})
            response=self.drawings.view(row['attachment_id'],scoped)
            if response.status_code!=200:return response
            html=response.body.decode();html=html.replace('<body','<body data-drawing-sheet="'+str(sheet_id)+'"',1)
            body_start=html.index('>',html.index('<body'))+1;html=html[:body_start]+header+rail+html[body_start:]
            html=html.replace('<div class="bc81-toolbar">',warning+'<div class="dr-view-links"><a href="/documents/'+str(row['attachment_id'])+'/content#page='+str(row['page_number'])+'" target="_blank" rel="noopener">Open original set</a><a href="#bcRevTitle">Save markup notes</a><span>Source page '+str(row['page_number'])+'</span></div><div class="bc81-toolbar">',1)
            # Do not silently clamp an invalid reviewed page onto a different sheet.
            html=html.replace('pageNum=Math.min(pageNum,pdf.numPages);','if(pageNum>pdf.numPages)throw new Error("The reviewed page is outside this PDF.");')
            html=html.replace('</head>',STYLE+VIEW_STYLE+'</head>',1)
            html=html.replace('</body>','<script>'+VIEW_JS+'</script></body>',1)
            return HTMLResponse(html,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})
        content=f'<main class="dr-read-view">{warning}<p>Source page {row["page_number"]}. Your project leader manages saved markups.</p><a target="_blank" rel="noopener" href="/documents/{row["attachment_id"]}/content#page={row["page_number"]}">Open original set</a><iframe title="{esc(row["sheet_number"])}" src="/documents/{row["attachment_id"]}/content#page={row["page_number"]}&amp;view=FitH"></iframe></main>'
        return HTMLResponse('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(row['sheet_number'])+' · BuildCommand AI</title>'+STYLE+VIEW_STYLE+'</head><body>'+header+rail+content+'<script>'+VIEW_JS+'</script></body></html>',headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes if hasattr(r,'methods') for m in r.methods or []}
        checks={m+' '+p:active.get((p,m)) is f for m,p,f in self.routes}
        checks['drawing_register_schema_initialized']=self.schema_ready
        checks['drawing_register_installed']=getattr(self.ns['app'].state,'drawing_register',None) is self
        checks['original_viewer_preserved']=callable(self.drawings.original_viewer)
        checks['shared_pdf_view_preserved']=('/workspace/shared/{share_id}/view','GET') in active
        checks['form_origin_guard_preserved']=callable(self.ns.get('_bc840_same_origin'))
        try:
            with self.db() as c:
                self.current_rows(c,0,0);self.drawings.list_rows(c,0,0)
                c.execute('SELECT review_hash,review_json,version FROM bc_drawing_sets WHERE 1=0')
            checks['drawing_queries_executed']=True
        except Exception:checks['drawing_queries_executed']=False
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if ok else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Schema, active handlers and drawing query checks. Test real PDF sheet reading, reviewed revisions, saved markups and company/project roles on staging. Browser suggestions require human review; no OCR or automatic trade sharing.'),status_code=200 if ok else 503)

    def alias(self):return RedirectResponse('/workspace/drawings',303)

    def extra_evidence(self,c,user,pid):
        rows=self.previous_evidence(c,user,pid)
        if self.schema_ready:
            for sheet in self.current_rows(c,user['company_id'],pid)[:20]:
                text=f"Current drawing register entry: {sheet['sheet_number']} — {sheet['title']}. Area: {sheet['area']}. Discipline: {sheet['discipline']}. Revision: {sheet['revision_label'] or 'Original'}. Drawing date: {sheet['drawing_date'] or 'not entered'}. Source page: {sheet['page_number']}. Metadata only; the PDF content was not analyzed by registering this sheet."
                rows.append(('Current drawing sheet',sheet['id'],sheet['sheet_number']+' · '+sheet['title'],text,'/workspace/drawing-sheets/'+str(sheet['id'])))
        return rows

    def register(self):
        self.previous_evidence=self.hub.extra_evidence
        self.hub.extra_evidence=self.extra_evidence
        for path,method,fn in [('/workspace/drawings','GET',self.index),('/workspace/drawings/import','GET',self.imports),('/workspace/drawing-sets/projects/{project_id}/upload','POST',self.upload),('/workspace/drawing-sets/projects/{project_id}/import/{attachment_id}','POST',self.import_file),('/workspace/drawing-sets/{set_id}','GET',self.set_page),('/workspace/drawing-sets/{set_id}/review','POST',self.review),('/workspace/drawing-sets/{set_id}/review','GET',self.review_page),('/workspace/drawing-sets/{set_id}/publish','POST',self.publish),('/workspace/drawing-sets/{set_id}/archive','POST',self.archive),('/workspace/drawing-sheets/{sheet_id}','GET',self.viewer),('/workspace/drawing-sheets/{sheet_id}/history','GET',self.history),('/drawings','GET',self.alias)]:
            wrapped=self.drawings.endpoint(fn);self.ns['_bc840_replace'](path,method,wrapped);self.routes.append((method,path,wrapped))
            # Keep installation checks pointing at the active replacement route.
            self.hub.routes[:]=[(m,p,wrapped if (m,p)==(method,path) else f) for m,p,f in self.hub.routes]
        # Preserve the former whole-file list for older bookmarks/integrations.
        self.ns['_bc840_replace']('/workspace/drawings/files','GET',self.drawings.endpoint(self.drawings.index))
        path='/health/drawing-register-8-15-0';self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)


STYLE='''<style>
.dr-heading{display:flex;align-items:center;justify-content:space-between;gap:20px}.dr-heading h1{margin-bottom:10px}.dr-heading p{color:#52667b}.dr-actions{display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin:22px 0}.dr-secondary{border:1px solid #bac9d8;padding:14px 18px;border-radius:8px;text-decoration:none;font-weight:700}.dr-search{display:flex;align-items:end;gap:16px;flex-wrap:wrap;margin:24px 0}.dr-search label:first-child{flex:1;min-width:210px}.dr-search label{font-weight:700}.dr-search input,.dr-search select{display:block;width:100%;margin-top:8px}.dr-register{padding:24px!important}.dr-table-wrap{overflow-x:auto}.dr-register table,.dr-table-wrap table{width:100%;border-collapse:collapse;margin-top:16px;text-align:left}.dr-table-wrap th{font-size:12px;letter-spacing:.05em;color:#53697d;text-transform:uppercase;background:#f1f5f9}.dr-table-wrap th,.dr-table-wrap td{padding:16px;border-bottom:1px solid #dce4ed}.dr-table-wrap td a{text-decoration:none}.dr-sheet-number{font-size:18px;font-weight:800}.dr-table-wrap small,.dr-set small{display:block;color:#63758a;margin-top:7px}.dr-tag{display:inline-block;background:#e5f1ed;color:#205948;padding:6px 10px;border-radius:5px;font-size:13px;font-weight:700}.dr-empty{padding:46px 20px;text-align:center;color:#52677a}.dr-empty h3{color:#17324a}.dr-section{margin-top:24px}.dr-section summary{cursor:pointer;font-size:19px;font-weight:750;padding:8px}.dr-upload{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:20px 0}.dr-upload label{font-weight:650}.dr-upload input{display:block;width:100%;margin-top:7px}.dr-wide{grid-column:1/-1}.dr-set{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:19px 0;border-bottom:1px solid #dce4ed}.dr-edit input{min-width:120px;width:100%}.dr-edit input[type=number]{min-width:70px;width:85px}.dr-edit td{padding:9px}.dr-edit select{min-width:140px}.dr-old{display:block;padding:16px;background:#fff1ce;color:#7a4400;margin-bottom:12px}.dr-read-view iframe{display:block;width:100%;height:78vh;margin-top:15px;border:0;background:#fff}@media(max-width:650px){.dr-upload{grid-template-columns:1fr}.dr-heading{display:block}.dr-register{padding:15px!important}.dr-actions{gap:12px}.dr-table-wrap td,.dr-table-wrap th{padding:12px}}
</style>'''

VIEW_STYLE='''<style>
body{margin:0;background:#e9eef3;color:#182d42;font-family:Arial,sans-serif}.bc8102-sidebar,.bc840-header,.bc840-footer,.bc840-main>.hub-back,.bc840-main>.hero{display:none!important}.bc8102-content{margin-left:0!important}.bc840-main{margin:88px 18px 30px 262px!important;padding:0!important;max-width:none!important}.dr-view-header{position:fixed;top:0;left:0;right:0;z-index:110;height:74px;box-sizing:border-box;display:flex;align-items:center;gap:24px;background:#102638;color:#fff;padding:12px 24px;border-bottom:3px solid #e7ad3c}.dr-view-header div{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.dr-view-header a{color:#fff;font-size:14px;text-decoration:none;white-space:nowrap}.dr-view-header b{color:#f4bd52;font-size:20px}.dr-view-header small{display:block;color:#c1d0dc;margin-top:6px;font-size:12px}.dr-sheet-rail{position:fixed;top:74px;bottom:0;left:0;width:242px;padding:18px;box-sizing:border-box;background:#fff;border-right:1px solid #c8d5e0;overflow:auto}.dr-sheet-rail label{font-size:13px;font-weight:700}.dr-sheet-rail input{width:100%;box-sizing:border-box;margin:10px 0 15px;padding:10px;border:1px solid #b7c8d5;border-radius:5px}.dr-rail-link{display:block;padding:14px 10px;text-decoration:none;color:#244159;border-bottom:1px solid #e3eaf0}.dr-rail-link b,.dr-rail-link span,.dr-rail-link small{display:block}.dr-rail-link b{font-size:16px}.dr-rail-link span{font-size:13px;margin-top:6px;line-height:1.4}.dr-rail-link small{font-size:11px;color:#64798a;margin-top:5px}.dr-rail-link.active{background:#e9f1f6;border-left:4px solid #c28a20}.dr-rail-link[hidden]{display:none}.dr-view-links{display:flex;align-items:center;gap:20px;margin-bottom:12px;font-size:13px}.dr-view-links span{margin-left:auto;color:#566c80}#bcPrev,#bcNext,#bcPageLabel{display:none!important}.bc840-main>.grid2>.card:nth-child(2){display:none!important}.bc840-main>.grid2{grid-template-columns:1fr!important}#bcStageWrap{height:calc(100vh - 280px)!important;min-height:360px!important;background:#536573!important}#drawing-load-state{font-size:13px}.dr-read-view{margin:90px 20px 20px 262px}.bc81-toolbar{background:#233e52!important;top:80px!important}.bc81-label{color:white!important}#bcSaveMarkup{background:#173f5d!important;color:white!important}@media(max-width:900px){.dr-sheet-rail{position:static;width:auto;height:auto;max-height:180px;margin-top:74px}.dr-sheet-rail nav{display:flex;gap:8px}.dr-rail-link{min-width:145px}.bc840-main,.dr-read-view{margin:18px!important}.dr-view-header{padding:10px 12px;gap:12px}.dr-view-header a{font-size:12px}.dr-view-header small{max-width:38vw;overflow:hidden;text-overflow:ellipsis}.dr-view-header>a:last-child{display:none}.bc81-toolbar{top:80px!important}#bcStageWrap{height:65vh!important}.dr-view-links{flex-wrap:wrap}}
</style>'''

VIEW_JS='''(()=>{const input=document.getElementById('dr-view-filter');if(input)input.addEventListener('input',()=>{const q=input.value.trim().toLowerCase();document.querySelectorAll('.dr-rail-link').forEach(link=>{link.hidden=!link.textContent.toLowerCase().includes(q);});});})();'''

REVIEW_JS=r'''(()=>{
const config=window.BC_DRAWING_REVIEW,rows=document.getElementById('dr-sheet-rows'),status=document.getElementById('dr-status'),read=document.getElementById('dr-read'),review=document.getElementById('dr-review');
let dirty=false;
function addRow(data){if(rows.children.length>=config.max_sheets){status.textContent='This set is limited to 500 sheets.';return;}const tr=document.createElement('tr');for(const [key,value] of [['page_number',data.page_number],['sheet_number',data.sheet_number||''],['title',data.title||'']]){const td=document.createElement('td'),input=document.createElement('input');input.dataset.field=key;input.value=value;input.setAttribute('aria-label',key.replace('_',' '));input.type=key==='page_number'?'number':'text';if(input.type==='number'){input.min=1;input.max=config.max_sheets;}else input.maxLength=key==='sheet_number'?40:240;input.required=true;td.append(input);tr.append(td);}const td=document.createElement('td'),select=document.createElement('select');select.dataset.field='discipline';select.setAttribute('aria-label','Discipline');for(const d of config.disciplines){const option=document.createElement('option');option.value=d;option.textContent=d;option.selected=d===data.discipline;select.append(option);}td.append(select);tr.append(td);const action=document.createElement('td'),remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.onclick=()=>{tr.remove();dirty=true;};action.append(remove);tr.append(action);rows.append(tr);}
for(const sheet of config.sheets)addRow(sheet);
document.getElementById('dr-add').onclick=()=>{const pages=[...rows.querySelectorAll('[data-field=page_number]')].map(x=>Number(x.value)||0);const page=Math.max(0,...pages)+1;addRow({page_number:page,sheet_number:'SHEET-'+String(page).padStart(3,'0'),title:'Page '+page,discipline:'General'});dirty=true;};
rows.addEventListener('input',()=>{dirty=true;});window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
function discipline(number){const code=number.toUpperCase();if(code.startsWith('SHEET-'))return 'General';if(code.startsWith('FP'))return 'Fire protection';return ({A:'Architectural',S:'Structural',C:'Civil',M:'Mechanical',E:'Electrical',P:'Plumbing',L:'Landscape'})[code[0]]||'General';}
function sheetGuess(items,page,seen){
 const texts=items.map(x=>(x.str||'').trim());
 const candidates=texts.map(x=>x.match(/^[A-Z]{1,3}[-.]?[0-9]{1,3}(?:[.][0-9]{1,2})?(?= |$)/i)).filter(Boolean).map(x=>x[0].toUpperCase());
 let number='SHEET-'+String(page).padStart(3,'0');const candidate=candidates[candidates.length-1];if(candidate&&!seen.has(candidate))number=candidate;
 const titles=texts.filter(x=>x.length>=8&&x.length<=180&&/(plan|detail|elevation|section|schedule|notes|diagram|layout|site|foundation|roof|ceiling)/i.test(x));
 return {page_number:page,sheet_number:number,title:titles[titles.length-1]||'',discipline:discipline(number)};
}
read.onclick=async()=>{if(rows.children.length&&!confirm('Replace this working sheet list with fresh page suggestions?'))return;read.disabled=true;review.disabled=true;let pdf=null;try{const suggestions=[];if(config.pdf){if(!window.pdfjsLib)throw new Error('The PDF reader could not load. Reload or add sheet rows manually.');pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';pdf=await pdfjsLib.getDocument({url:config.source,isEvalSupported:false}).promise;if(pdf.numPages>config.max_sheets)throw new Error('This PDF has more than 500 pages. Use smaller drawing sets.');const seen=new Set();for(let n=1;n<=pdf.numPages;n++){status.textContent='Reading page '+n+' of '+pdf.numPages+'…';let suggestion=sheetGuess([],n,seen);try{const page=await pdf.getPage(n),content=await page.getTextContent();suggestion=sheetGuess(content.items,n,seen);page.cleanup();}catch(error){}seen.add(suggestion.sheet_number.toUpperCase());suggestions.push(suggestion);}}else suggestions.push({page_number:1,sheet_number:'SHEET-001',title:'Image drawing',discipline:'General'});rows.replaceChildren();for(const s of suggestions)addRow(s);dirty=true;status.textContent=suggestions.length+' sheet(s) ready to check. Confirm numbers, replace placeholder titles and remove pages you do not need.';}catch(error){status.textContent=error.message||'The file could not be read. Open the original and enter sheet rows manually.';}finally{read.disabled=false;review.disabled=false;if(pdf)await pdf.destroy();}};
review.onclick=async()=>{if(!rows.children.length){status.textContent='Read the PDF or add a sheet before continuing.';return;}const sheets=[];for(const tr of rows.children){const data={};for(const input of tr.querySelectorAll('[data-field]')){if(!input.reportValidity())return;data[input.dataset.field]=input.dataset.field==='page_number'?Number(input.value):input.value;}sheets.push(data);}review.disabled=true;try{status.textContent='Preparing your review…';const response=await fetch('/workspace/drawing-sets/'+config.set_id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:config.version,sheets})});if(!response.ok){const raw=await response.text();const parsed=new DOMParser().parseFromString(raw,'text/html');status.textContent=parsed.querySelector('p')?.textContent||'This set changed or could not be saved. Keep your entries and check your connection; reload only when you are ready to discard them.';return;}const result=await response.json();if(result.review_url!=='/workspace/drawing-sets/'+config.set_id+'/review')throw new Error('Unexpected review address.');dirty=false;window.location.assign(result.review_url);}catch(error){status.textContent='The review could not finish. Your sheet entries are still here. Check your connection and try again.';}finally{review.disabled=false;}};
})();'''
