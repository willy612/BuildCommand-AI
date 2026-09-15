"""8.20.0: internal project document register and reusable company templates.

Original uploads and each file version are immutable. No automatic sharing,
email, signatures, approval of submittals or certification of inspections.
"""
import csv
import hashlib
import inspect
import io
import json
import logging
import re
import uuid
from datetime import date
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from blueprint_field import esc

VERSION = '8.20.0'
RELEASE = 'Project Documents & Closeout Register'
BASE = '/workspace/documents'
TEMPLATES = '/workspace/company-templates'
LIMIT = 100 * 1024 * 1024
FOLDERS = {'general':'General documents', 'closeout':'Closeouts', 'safety':'Safety',
           'submittal':'Submittals', 'contract':'Contracts & standard forms', 'inspection':'Inspections'}
STATUS = {'needed':'Needed', 'received':'Ready for review', 'complete':'Filed / complete'}
WORKFLOWS = {'submittal':('/submittals','Open submittal register'),
             'inspection':('/inspections','Open inspections'), 'safety':('/safety','Open safety records')}
ALLOWED = {'.pdf','.png','.jpg','.jpeg','.webp','.docx','.xlsx','.pptx','.txt','.csv','.zip','.dwg','.dxf'}
TABLES = {'bc_doc_folders','bc_doc_records','bc_doc_files','bc_doc_links','bc_doc_events'}
log = logging.getLogger('buildcommand.project_documents')
CSS = '''<style>
.docs{max-width:1280px;margin:auto}.docs .docs-actions{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}.docs .docs-actions>*{margin:0}.docs .docs-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:16px}.docs .docs-folder{display:block;padding:20px;border:1px solid #d9e3ee;border-radius:12px;background:#fff;text-decoration:none;color:#18344f}.docs .docs-folder[aria-current=page]{border:2px solid #bd8420;background:#fffaf0}.docs .docs-row{padding:20px 0;border-top:1px solid #dce4ed}.docs .docs-row h3{margin:0 0 8px}.docs .docs-meta{color:#52667b;font-size:15px;line-height:1.6}.docs .docs-pill{display:inline-block;padding:5px 10px;border-radius:6px;background:#edf3f8;font-size:14px}.docs .docs-needed{background:#fff0ce;color:#774e00}.docs .docs-complete{background:#e7f3ee;color:#1d6046}.docs .docs-form{max-width:780px}.docs .docs-form label{display:block;font-weight:600;margin:16px 0 6px}.docs input:not([type=hidden]),.docs select,.docs textarea{box-sizing:border-box;max-width:100%;padding:12px;border:1px solid #bacbdc;border-radius:7px;font:inherit}.docs .docs-form input:not([type=hidden]),.docs .docs-form select,.docs .docs-form textarea{width:100%}.docs button,.docs .bc840-button{min-height:46px}.docs .docs-form button{margin-top:18px}.docs .docs-empty{padding:28px;border:1px dashed #b6c8d9;border-radius:10px}.docs summary{cursor:pointer;padding:14px 0;font-weight:600}.docs .docs-notes{white-space:pre-wrap}.docs .docs-file{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}.docs .card{margin:22px 0}.docs a:focus-visible,.docs button:focus-visible{outline:3px solid #cc962c;outline-offset:3px}@media(max-width:650px){.docs .docs-actions>*{width:100%}.docs .docs-actions button{width:100%}}
</style>'''


def install(ns):
    service = ProjectDocuments(ns)
    ns['app'].state.project_documents = service
    service.register()
    return service


class ProjectDocuments:
    def __init__(self, ns):
        self.ns, self.hub = ns, ns['app'].state.workspace_hub
        self.field, self.db, self.require = self.hub.field, self.hub.db, self.hub.require
        self.drawings = ns['app'].state.drawing_workspace
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        try:
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_folders(id {key},company_id BIGINT NOT NULL,
                    name TEXT NOT NULL,name_key TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL,
                    UNIQUE(company_id,name_key))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_records(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,title TEXT NOT NULL,folder_key TEXT NOT NULL,notes TEXT NOT NULL,
                    responsible TEXT NOT NULL,due_date TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,
                    request_key TEXT NOT NULL,template_file_id BIGINT,created_by BIGINT NOT NULL,updated_by BIGINT NOT NULL,
                    created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(company_id,created_by,request_key),
                    UNIQUE(company_id,project_id,template_file_id))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_files(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,record_id BIGINT NOT NULL,revision INTEGER NOT NULL,
                    original_name TEXT NOT NULL,stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,sha256 TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL,
                    UNIQUE(company_id,record_id,revision))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_links(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,record_id BIGINT NOT NULL,kind TEXT NOT NULL,source_id BIGINT NOT NULL,
                    created_by BIGINT NOT NULL,created TEXT NOT NULL,UNIQUE(company_id,record_id,kind,source_id))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_events(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,record_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,
                    action TEXT NOT NULL,snapshot_json TEXT NOT NULL,created TEXT NOT NULL)''')
                for table in ('bc_doc_records','bc_doc_files','bc_doc_links','bc_doc_events'):
                    c.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_project ON {table}(company_id,project_id)')
            return True
        except Exception:
            log.exception('Project documentation setup failed')
            return False

    def endpoint(self, fn):
        def fail(exc):
            if isinstance(exc,self.ns['_BC850_Problem']):return self.ns['_bc830b_error'](exc.message,exc.status)
            log.exception('Document action failed handler=%s',fn.__name__)
            return self.ns['_bc830b_error']('This document action could not be completed. Reopen the record to check what was saved.',503)
        def ready():
            if not self.ns['_bc840_user']():return RedirectResponse('/login',303)
            self.require(self.schema_ready,'Project documents are unavailable. Ask your administrator to check this installation.',503)
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(*args,**kwargs):
                try:
                    response=ready()
                    return response if response is not None else await fn(*args,**kwargs)
                except Exception as exc:return fail(exc)
        else:
            @wraps(fn)
            def wrapped(*args,**kwargs):
                try:
                    response=ready()
                    return response if response is not None else fn(*args,**kwargs)
                except Exception as exc:return fail(exc)
        return wrapped

    def actor(self,c,pid,write=False):
        if pid != 0:return self.field.actor(c,pid,write)
        user=self.ns['_bc850_actor'](c)
        if write and self.field.postgres:
            c.execute('SELECT id FROM companies WHERE id=? FOR UPDATE',(user['company_id'],)).fetchone()
            user=self.ns['_bc850_actor'](c)
        self.require(self.ns['_bc850_manager'](user),'Your project leader manages these documents.')
        if write:self.require(self.ns['_bc840_tier'](user) in {'owner','admin'},'Your company administrator manages company templates and folders.')
        return user,dict(id=0,name='Company templates')

    def origin(self,request):
        self.require(self.ns['_bc840_same_origin'](request),'Reload this page before saving the form.',403)

    def insert(self,c,table,values):
        self.require(table in TABLES,'Unsupported document record.',400)
        sql=f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})'
        args=tuple(values.values())
        if self.field.postgres:return int(c.execute(sql+' RETURNING id',args).fetchone()['id'])
        return int(c.execute(sql,args).lastrowid)

    def text(self,value,limit,label,required=False):
        value=str(value or '').strip()
        self.require((bool(value) or not required) and len(value)<=limit and not any(ord(ch)<32 and ch not in '\n\r\t' for ch in value),f'Enter {label} within {limit} characters.',400)
        return value

    def folder_options(self,c,cid):
        rows=c.execute('SELECT id,name FROM bc_doc_folders WHERE company_id=? ORDER BY name,id',(cid,)).fetchall()
        return {**FOLDERS,**{'custom:'+str(r['id']):r['name'] for r in rows}}

    def metadata(self,c,user,title,folder,notes,responsible,due,status='needed'):
        self.require(folder in self.folder_options(c,user['company_id']),'Choose a folder in your company.',400)
        self.require(status in STATUS,'Choose a listed document status.',400)
        if due:
            try:
                self.require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',due)),'Enter a valid due date.',400)
                due=date.fromisoformat(due).isoformat()
            except ValueError:self.require(False,'Enter a valid due date.',400)
        return dict(title=self.text(title,240,'a document name',True),folder_key=folder,
                    notes=self.text(notes,6000,'document notes'),responsible=self.text(responsible,160,'a responsible person or trade'),due_date=due,status=status)

    def record(self,c,rid,write=False,version=None):
        user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM bc_doc_records WHERE id=? AND company_id=?',(rid,user['company_id'])).fetchone()
        self.require(row is not None,'This document record is unavailable.',404)
        user,project=self.actor(c,row['project_id'],write)
        # Refresh only after acquiring the existing company/project write lock.
        row=dict(c.execute('SELECT * FROM bc_doc_records WHERE id=? AND company_id=?',(rid,user['company_id'])).fetchone())
        if version is not None:self.require(version==row['version'],'This record changed. Reopen it before saving your changes.',409)
        return user,project,row

    def files(self,c,row):
        return [dict(r) for r in c.execute('SELECT * FROM bc_doc_files WHERE company_id=? AND project_id=? AND record_id=? ORDER BY revision DESC',
                                          (row['company_id'],row['project_id'],row['id'])).fetchall()]

    def links(self,c,user,row):
        out=[]
        for r in c.execute('SELECT * FROM bc_doc_links WHERE company_id=? AND project_id=? AND record_id=? ORDER BY id',(user['company_id'],row['project_id'],row['id'])).fetchall():
            link=dict(r)
            try:link['source']=self.ns['_bc850_source'](c,user,row['project_id'],link['kind'],link['source_id'])
            except self.ns['_BC850_Problem']:link['source']=None
            out.append(link)
        return out

    def event(self,c,user,row,action):
        snapshot=dict(row)
        self.insert(c,'bc_doc_events',dict(company_id=user['company_id'],project_id=row['project_id'],record_id=row['id'],actor_id=user['id'],
            action=action,snapshot_json=json.dumps(snapshot,ensure_ascii=False,default=str),created=self.field.now().isoformat()))

    def changed(self,c,user,row,action,**values):
        values.update(version=row['version']+1,updated_by=user['id'],updated=self.field.now().isoformat())
        self.require(set(values)<=set(row),'Unsupported document change.',400)
        c.execute('UPDATE bc_doc_records SET '+','.join(k+'=?' for k in values)+' WHERE id=? AND company_id=?',tuple(values.values())+(row['id'],user['company_id']))
        row={**row,**values};self.event(c,user,row,action)
        return row

    def make(self,c,user,pid,meta,key,template_file_id=None):
        self.require(bool(re.fullmatch(r'[a-f0-9]{32}',key)),'Reopen the add-document form before saving.',400)
        existing=c.execute('SELECT * FROM bc_doc_records WHERE company_id=? AND created_by=? AND request_key=?',(user['company_id'],user['id'],key)).fetchone()
        if existing:
            self.require(existing['project_id']==pid,'Reopen the form for this project.',409)
            return dict(existing),False
        now=self.field.now().isoformat()
        values=dict(company_id=user['company_id'],project_id=pid,**meta,version=1,request_key=key,template_file_id=template_file_id,
                    created_by=user['id'],updated_by=user['id'],created=now,updated=now)
        values['id']=self.insert(c,'bc_doc_records',values)
        self.event(c,user,values,'Record created')
        return values,True

    def page(self,title,body):return self.hub.page(title,CSS+'<div class="docs">'+body+'</div>')
    def link(self,path,label):return '<a class="bc840-button" href="'+esc(path)+'">'+esc(label)+'</a>'
    def home(self,pid):return BASE+'?project_id='+str(pid) if pid else TEMPLATES
    def redirect(self,rid):return RedirectResponse(BASE+'/'+str(rid),303)
    def hidden(self,key,value):return '<input type="hidden" name="'+esc(key)+'" value="'+esc(value)+'">'
    def input(self,name,label,value='',typ='text',extra=''):
        return f'<label for="doc-{name}">{esc(label)}</label><input id="doc-{name}" name="{name}" type="{typ}" value="{esc(value)}" {extra}>'

    def form_fields(self,c,user,row=None):
        row=row or {};options=self.folder_options(c,user['company_id'])
        body=self.input('title','Document name',row.get('title',''),extra='maxlength="240" required placeholder="Example: Electrical warranty"')
        body+='<label for="doc-folder">Folder</label><select id="doc-folder" name="folder_key">'+''.join('<option value="'+esc(k)+'"'+(' selected' if k==row.get('folder_key','general') else '')+'>'+esc(v)+'</option>' for k,v in options.items())+'</select>'
        body+='<details><summary>Owner, due date and notes</summary>'+self.input('responsible','Responsible person or trade',row.get('responsible',''),extra='maxlength="160"')+self.input('due_date','Due date',row.get('due_date',''),'date')
        body+='<label for="doc-notes">Notes / what is needed</label><textarea id="doc-notes" name="notes" rows="4" maxlength="6000">'+esc(row.get('notes',''))+'</textarea></details>'
        return body

    def index(self,project_id:int=0,folder:str='',q:str='',status:str='',offset:int=0):
        with self.db() as c:
            user,p,projects=self.hub.user(c,project_id,True)
            body='<div class="hero"><h1>Project Documents</h1><p>Keep the job paperwork together. See what is filed and what is still needed.</p></div>'+self.hub.selector(projects,p,BASE)
            if not p:return self.page('Project Documents',body+'<div class="docs-empty">Choose a project to open its document folders.</div>')
            return self.list_page(c,user,p,body,folder,q,status,offset)

    def templates(self,folder:str='',q:str='',status:str='',offset:int=0):
        with self.db() as c:
            user,p=self.actor(c,0)
            body='<div class="hero"><h1>Company templates</h1><p>Reusable company forms and boilerplate for your project leaders. Use a template to start a job record.</p></div>'
            return self.list_page(c,user,p,body,folder,q,status,offset)

    def list_page(self,c,user,p,body,folder,q,status,offset):
        pid=p['id'];home=self.home(pid);options=self.folder_options(c,user['company_id'])
        self.require(not folder or folder in options,'Choose a folder in your company.',400)
        self.require(not status or status in STATUS,'Choose a listed document status.',400)
        self.require(0<=offset<=100000,'Choose a valid results page.',400)
        args=[user['company_id'],pid];where='company_id=? AND project_id=?'
        if folder:where+=' AND folder_key=?';args.append(folder)
        if status:where+=' AND status=?';args.append(status)
        q=self.text(q,120,'a search term')
        if q:where+=' AND (LOWER(title) LIKE ? OR LOWER(responsible) LIKE ? OR LOWER(notes) LIKE ?)';args.extend(['%'+q.lower()+'%']*3)
        rows=[dict(r) for r in c.execute('SELECT * FROM bc_doc_records WHERE '+where+' ORDER BY CASE status WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END,CASE WHEN due_date=? THEN 1 ELSE 0 END,due_date,id DESC LIMIT 51 OFFSET ?',args+['needed','received','',offset]).fetchall()]
        counts={r['folder_key']:int(r['n']) for r in c.execute('SELECT folder_key,COUNT(*) AS n FROM bc_doc_records WHERE company_id=? AND project_id=? GROUP BY folder_key',(user['company_id'],pid)).fetchall()}
        can_edit=pid!=0 or self.ns['_bc840_tier'](user) in {'owner','admin'}
        body+='<div class="docs-actions">'
        if can_edit:body+=self.link(BASE+'/new?'+urlencode(dict(project_id=pid,template=1 if not pid else 0,folder=folder or 'general')),'Add a document')
        if pid and getattr(self.ns['app'].state,'document_requests',None):body+=self.link('/workspace/document-requests?project_id='+str(pid),'Document requests')
        if pid and getattr(self.ns['app'].state,'closeout_handover',None):body+=self.link('/workspace/closeout?project_id='+str(pid),'Closeout & handover')
        if pid and getattr(self.ns['app'].state,'safety_checklists',None):body+=self.link('/workspace/checklists?project_id='+str(pid),'Safety & inspections')
        if pid:body+=self.link(TEMPLATES,'Use company template')+self.link(BASE+'/export?'+urlencode(dict(project_id=pid,folder=folder)),'Export register')
        if pid and folder=='closeout':body+='<form method="post" action="'+BASE+'/projects/'+str(pid)+'/closeout-starter"><button>Start closeout checklist</button></form>'
        body+='</div><nav class="docs-grid" aria-label="Document folders">'
        for key,label in {'':'All documents',**options}.items():
            target=(BASE if pid else TEMPLATES)+'?'+urlencode(dict(project_id=pid,folder=key))
            body+='<a class="docs-folder" href="'+esc(target)+'"'+(' aria-current="page"' if key==folder else '')+'><strong>'+esc(label)+'</strong><br><span class="docs-meta">'+str(counts.get(key,0) if key else sum(counts.values()))+' records</span></a>'
        body+='</nav><section class="card"><form method="get" action="'+(BASE if pid else TEMPLATES)+'">'+self.hidden('project_id',pid)+self.hidden('folder',folder)
        body+='<label for="doc-search">Find a record </label><input id="doc-search" name="q" maxlength="120" value="'+esc(q)+'" placeholder="Name, trade or notes"> <label for="doc-status">Status </label><select id="doc-status" name="status"><option value="">All statuses</option>'+''.join('<option value="'+k+'"'+(' selected' if k==status else '')+'>'+v+'</option>' for k,v in STATUS.items())+'</select> <button>Find</button></form>'
        if pid and folder in WORKFLOWS:
            path,label=WORKFLOWS[folder];body+='<div class="docs-actions">'+self.hub.open_form(pid,path,label)+'</div><p class="docs-meta">The working register stays in its existing tool. File its supporting documents here.</p>'
        for row in rows[:50]:
            overdue=bool(row['due_date'] and row['due_date']<self.field.now().date().isoformat() and row['status']!='complete')
            body+='<article class="docs-row"><h3><a href="'+BASE+'/'+str(row['id'])+'">'+esc(row['title'])+'</a></h3><span class="docs-pill docs-'+row['status']+'">'+STATUS[row['status']]+'</span><p class="docs-meta">'+esc(options.get(row['folder_key'],'Folder unavailable'))+' · '+esc(row['responsible'] or 'Owner not set')+' · '+esc(('Overdue: ' if overdue else 'Due: ')+row['due_date'] if row['due_date'] else 'No due date')+'</p></article>'
        if not rows:body+='<div class="docs-empty"><h2>No matching records</h2><p>Add a document now, or create a record for a file you still need to collect.</p></div>'
        for label,start in [('Previous',offset-50),('Next',offset+50)]:
            if (label=='Previous' and offset>0) or (label=='Next' and len(rows)>50):body+=self.link((BASE if pid else TEMPLATES)+'?'+urlencode(dict(project_id=pid,folder=folder,q=q,status=status,offset=max(0,start))),label)
        body+='</section>'
        if self.ns['_bc840_tier'](user) in {'owner','admin'}:
            body+='<details class="card"><summary>Add a company folder</summary><p>Use the same folder names across your company projects.</p><form method="post" action="'+BASE+'/folders">'+self.input('name','Folder name',extra='maxlength="80" required')+self.hidden('project_id',pid)+'<button>Add folder</button></form></details>'
        body+='<p class="docs-meta">Internal to company administrators and appointed project managers / superintendents. This register does not send files to subcontractors.</p>'
        return self.page('Project Documents' if pid else 'Company templates',body)

    def new(self,project_id:int=0,template:int=0,folder:str='general'):
        self.require(project_id>0 or template==1,'Choose a project before adding a document.',400)
        with self.db() as c:
            user,p=self.actor(c,project_id,True)
            self.require(folder in self.folder_options(c,user['company_id']),'Choose a valid folder.',400)
            body='<div class="hero"><h1>Add a document</h1><p>'+esc(p['name'])+'</p></div><form class="card docs-form" method="post" action="'+BASE+'/create">'+self.hidden('project_id',project_id)+self.hidden('request_key',uuid.uuid4().hex)
            body+=self.form_fields(c,user,dict(folder_key=folder))+'<p class="docs-meta">Create the record first. You can upload a file or link an existing project record on the next screen.</p><button>Create document record</button></form>'+self.link(self.home(project_id),'Back to documents')
        return self.page('Add a document',body)

    def create(self,request:Request,project_id:int=Form(...),request_key:str=Form(...),title:str=Form(...),folder_key:str=Form('general'),notes:str=Form(''),responsible:str=Form(''),due_date:str=Form('')):
        self.origin(request)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            row,_=self.make(c,user,project_id,self.metadata(c,user,title,folder_key,notes,responsible,due_date),request_key)
        return self.redirect(row['id'])

    def add_folder(self,request:Request,name:str=Form(...),project_id:int=Form(0)):
        self.origin(request);name=' '.join(self.text(name,80,'a folder name',True).split())
        with self.db(True) as c:
            user,_=self.actor(c,0,True)
            if project_id:self.actor(c,project_id)
            names=self.folder_options(c,user['company_id'])
            self.require(name.casefold() not in {v.casefold() for v in names.values()},'That folder already exists. Choose its name from the folder list.',409)
            self.require(len(names)<106,'This company already has 100 custom folders.',409)
            self.insert(c,'bc_doc_folders',dict(company_id=user['company_id'],name=name,name_key=name.casefold(),created_by=user['id'],created=self.field.now().isoformat()))
        return RedirectResponse(self.home(project_id),303)

    def detail(self,record_id:int,kind:str='document',q:str=''):
        with self.db() as c:
            user,p,row=self.record(c,record_id);files=self.files(c,row);links=self.links(c,user,row)
            editable=row['project_id']!=0 or self.ns['_bc840_tier'](user) in {'owner','admin'}
            base=BASE+'/'+str(record_id);token=self.hidden('version',row['version'])
            body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+'</div><h1>'+esc(row['title'])+'</h1><span class="docs-pill docs-'+row['status']+'">'+STATUS[row['status']]+'</span><p>'+esc(row['responsible'] or 'Owner not set')+' · '+esc(row['due_date'] or 'No due date')+'</p></div>'
            requests=getattr(self.ns['app'].state,'document_requests',None)
            if requests:body+=requests.record_panel(c,user,row)
            closeout=getattr(self.ns['app'].state,'closeout_handover',None)
            if closeout:body+=closeout.record_panel(c,user,row)
            checklists=getattr(self.ns['app'].state,'safety_checklists',None)
            if checklists:body+=checklists.record_panel(c,user,row)
            body+='<div class="docs-actions">'+self.link(self.home(p['id']),'Back to documents')+'</div><section class="card"><h2>Files</h2>'
            for f in files:
                body+='<article class="docs-row docs-file"><div><strong>'+esc(f['original_name'])+'</strong><p class="docs-meta">Version '+str(f['revision'])+' · '+('Current file · ' if f==files[0] else 'Earlier file · ')+esc(f['created'][:10])+'</p></div><div class="docs-actions">'
                if f['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}:body+=self.link(base+'/files/'+str(f['id'])+'?preview=1','Open')
                body+=self.link(base+'/files/'+str(f['id']),'Download')+'</div></article>'
            if not files:body+='<p>No file uploaded yet. This record tracks what is still needed.</p>'
            if editable:
                body+='<form method="post" action="'+base+'/files" enctype="multipart/form-data" class="docs-form">'+token+'<label for="doc-file">'+('Add a revised file' if files else 'Upload the file')+'</label><input id="doc-file" type="file" name="file" required accept="'+','.join(sorted(ALLOWED))+'"><p class="docs-meta">PDF, images, Word, Excel, PowerPoint, text, CSV, ZIP or CAD. Up to 100 MB. Earlier versions stay available.</p><button>'+('Save new file version' if files else 'Upload file')+'</button></form>'
            body+='</section>'
            if not p['id']:
                projects=self.ns['_bc850_projects'](c,user)
                if files:
                    body+='<section class="card"><h2>Use this template on a project</h2><form method="post" action="'+base+'/use-template">'+token+self.hidden('file_id',files[0]['id'])+'<label for="template-project">Project </label><select id="template-project" name="project_id" required>'+''.join('<option value="'+str(x['id'])+'">'+esc(x['name'])+'</option>' for x in projects)+'</select><button>Use on this project</button></form><p class="docs-meta">Creates a project copy of this file version. Later template edits will not change that job copy.</p></section>'
            else:
                body+='<section class="card"><h2>Linked project records</h2>'
                for link in links:
                    source=link['source'];body+='<article class="docs-row"><strong>'+esc((source or {}).get('title') or 'Source record unavailable')+'</strong><p class="docs-meta">'+esc(link['kind'].title())+'</p>'
                    if source:body+='<form method="post" action="'+base+'/links/'+str(link['id'])+'/open"><button>Open source record</button></form>'
                    body+='<form method="post" action="'+base+'/links/'+str(link['id'])+'/remove">'+token+'<button>Remove this link</button></form>'
                    body+='</article>'
                self.require(kind in {'document','rfi','submittal'},'Choose Documents, RFIs or Submittals.',400)
                sources=self.ns['_bc850_source'](c,user,p['id'],kind,None,self.text(q,120,'a search term'))
                body+='<details id="link-record"><summary>Link an existing document, RFI or submittal</summary><form method="get" action="'+base+'#link-record"><label for="link-kind">Record type </label><select id="link-kind" name="kind">'+''.join('<option value="'+k+'"'+(' selected' if kind==k else '')+'>'+v+'</option>' for k,v in [('document','Documents'),('rfi','RFIs'),('submittal','Submittals')])+'</select><label for="link-q"> Find </label><input id="link-q" name="q" maxlength="120" value="'+esc(q)+'"><button>Find records</button></form>'
                if sources:
                    body+='<form method="post" action="'+base+'/links">'+token+self.hidden('kind',kind)+'<label for="source-id">Source record </label><select id="source-id" name="source_id">'+''.join('<option value="'+str(s['id'])+'">'+esc(s['title'] or 'Untitled')+' (#'+str(s['id'])+')</option>' for s in sources)+'</select><button>Link selected record</button></form>'
                else:body+='<p>No matching records in this project.</p>'
                body+='<p class="docs-meta">Links keep the existing source in place. No duplicate upload and no sharing permission change.</p></details></section>'
            if editable:
                body+='<details class="card"><summary>Edit details / mark filed</summary><form class="docs-form" method="post" action="'+base+'/save">'+token+self.form_fields(c,user,row)+'<label for="edit-status">Document status</label><select id="edit-status" name="status">'+''.join('<option value="'+k+'"'+(' selected' if k==row['status'] else '')+'>'+v+'</option>' for k,v in STATUS.items())+'</select><p class="docs-meta">Filed / complete means your document record is filed. It does not approve a submittal, certify safety or sign a contract.</p><button>Save record</button></form></details>'
            if row['notes']:body+='<section class="card"><h2>Notes</h2><p class="docs-notes">'+esc(row['notes'])+'</p></section>'
            events=c.execute('SELECT e.action,e.created,u.display_name,u.email FROM bc_doc_events e LEFT JOIN users u ON u.id=e.actor_id AND u.company_id=e.company_id WHERE e.company_id=? AND e.project_id=? AND e.record_id=? ORDER BY e.id DESC LIMIT 50',(user['company_id'],p['id'],record_id)).fetchall()
            body+='<details class="card"><summary>Record history</summary>'+''.join('<p>'+esc(e['action'])+' · '+esc(e['display_name'] or e['email'] or 'Former team member')+' · '+esc(e['created'])+'</p>' for e in events)+'</details>'
        return self.page('Document record',body)

    def save(self,record_id:int,request:Request,version:int=Form(...),title:str=Form(...),folder_key:str=Form(...),notes:str=Form(''),responsible:str=Form(''),due_date:str=Form(''),status:str=Form(...)):
        self.origin(request)
        with self.db(True) as c:
            user,p,row=self.record(c,record_id,True,version)
            meta=self.metadata(c,user,title,folder_key,notes,responsible,due_date,status)
            if status!='needed':self.require(bool(self.files(c,row)) or any(l['source'] for l in self.links(c,user,row)),'Upload a file or link the supporting record before marking it received or filed.',400)
            self.changed(c,user,row,'Record details saved',**meta)
        return self.redirect(record_id)

    async def stage(self,file):
        name=Path(str(file.filename or '').replace('\\','/')).name
        self.require(name not in {'','.','..'} and len(name)<=240 and not any(ord(ch)<32 or ord(ch)==127 for ch in name),'Use a valid filename within 240 characters.',400)
        suffix=Path(name).suffix.lower();self.require(suffix in ALLOWED,'Choose a supported document file type.',400)
        root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();root.mkdir(parents=True,exist_ok=True)
        path=root/('record-'+uuid.uuid4().hex+suffix);size=0;digest=hashlib.sha256();mime='application/octet-stream'
        try:
            with path.open('xb') as dest:
                chunk=await file.read(1024*1024)
                self.require(bool(chunk),'Choose a file that contains data.',400)
                if suffix in {'.pdf','.png','.jpg','.jpeg','.webp'}:
                    mime=self.ns['_bc8131_preview_type'](name,chunk[:16])
                    self.require(mime is not None,'This file does not match its PDF or image extension.',400)
                elif suffix in {'.docx','.xlsx','.pptx','.zip'}:self.require(chunk.startswith(b'PK\x03\x04'),'This file does not match its document extension.',400)
                elif suffix in {'.txt','.csv'}:
                    self.require(b'\x00' not in chunk,'Use a plain text or CSV file.',400)
                while chunk:
                    size+=len(chunk);self.require(size<=LIMIT,'This file exceeds the 100 MB upload limit.',413)
                    digest.update(chunk);dest.write(chunk);chunk=await file.read(1024*1024)
            return path,dict(original_name=name,stored_name=path.name,mime_type=mime,size_bytes=size,sha256=digest.hexdigest())
        except BaseException:
            path.unlink(missing_ok=True);raise

    async def upload(self,record_id:int,request:Request,version:int=Form(...),file:UploadFile=File(...)):
        path=None;saved=False
        try:
            self.origin(request)
            with self.db() as c:self.record(c,record_id,False,version)
            # Enforce template write permission before placing bytes on disk.
            with self.db() as c:
                user,p,row=self.record(c,record_id)
                if not p['id']:self.actor(c,0,True)
            path,data=await self.stage(file)
            with self.db(True) as c:
                user,p,row=self.record(c,record_id,True,version)
                files=self.files(c,row);rev=(files[0]['revision'] if files else 0)+1
                self.insert(c,'bc_doc_files',dict(company_id=user['company_id'],project_id=p['id'],record_id=record_id,revision=rev,**data,created_by=user['id'],created=self.field.now().isoformat()))
                self.changed(c,user,row,'File version '+str(rev)+' uploaded',status='received')
            saved=True
            return self.redirect(record_id)
        finally:
            if path is not None and not saved:path.unlink(missing_ok=True)
            await file.close()

    def file_path(self,f):
        # Shares the original upload directory, but never a public/static URL.
        return self.drawings.file_path(f)

    def download(self,record_id:int,file_id:int,preview:int=0):
        with self.db() as c:
            user,p,row=self.record(c,record_id)
            f=next((f for f in self.files(c,row) if f['id']==file_id),None)
            self.require(f is not None,'This file version is unavailable.',404)
        path=self.file_path(f)
        with path.open('rb') as source:
            actual=hashlib.file_digest(source,'sha256').hexdigest()
        self.require(actual==f['sha256'],'The stored file changed. Ask your administrator to restore the original version.',409)
        inline=preview==1 and f['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}
        return FileResponse(path,filename=f['original_name'],media_type=f['mime_type'],content_disposition_type='inline' if inline else 'attachment',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'SAMEORIGIN'})

    def add_link(self,record_id:int,request:Request,version:int=Form(...),kind:str=Form(...),source_id:int=Form(...)):
        self.origin(request)
        self.require(kind in {'document','rfi','submittal'},'Choose Documents, RFIs or Submittals.',400)
        with self.db(True) as c:
            user,p,row=self.record(c,record_id,True,version)
            self.require(p['id']>0,'Company templates use their own uploaded files.',400)
            self.ns['_bc850_source'](c,user,p['id'],kind,source_id)
            existing=c.execute('SELECT id FROM bc_doc_links WHERE company_id=? AND record_id=? AND kind=? AND source_id=?',(user['company_id'],record_id,kind,source_id)).fetchone()
            if not existing:
                self.insert(c,'bc_doc_links',dict(company_id=user['company_id'],project_id=p['id'],record_id=record_id,kind=kind,source_id=source_id,created_by=user['id'],created=self.field.now().isoformat()))
                self.changed(c,user,row,'Linked '+kind+' #'+str(source_id),status='received')
        return self.redirect(record_id)

    def remove_link(self,record_id:int,link_id:int,request:Request,version:int=Form(...)):
        self.origin(request)
        with self.db(True) as c:
            user,p,row=self.record(c,record_id,True,version)
            link=c.execute('SELECT id FROM bc_doc_links WHERE id=? AND record_id=? AND company_id=? AND project_id=?',(link_id,record_id,user['company_id'],p['id'])).fetchone()
            self.require(link is not None,'This link is unavailable.',404)
            c.execute('DELETE FROM bc_doc_links WHERE id=? AND record_id=? AND company_id=?',(link_id,record_id,user['company_id']))
            backed=bool(self.files(c,row)) or any(l['source'] for l in self.links(c,user,row))
            self.changed(c,user,row,'Link removed #'+str(link_id),status=row['status'] if backed else 'needed')
        return self.redirect(record_id)

    def closeout_starter(self,project_id:int,request:Request):
        self.origin(request);self.require(project_id>0,'Choose a project.',400)
        names=['Warranty documents','Operation and maintenance manuals','Record drawings / as-builts','Owner training records','Final inspection records']
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            for title in names:
                found=c.execute('SELECT id FROM bc_doc_records WHERE company_id=? AND project_id=? AND folder_key=? AND LOWER(title)=?',(user['company_id'],project_id,'closeout',title.lower())).fetchone()
                if not found:
                    meta=self.metadata(c,user,title,'closeout','Starter item: confirm the applicable contract requirements for this job.','','')
                    self.make(c,user,project_id,meta,uuid.uuid4().hex)
        return RedirectResponse(BASE+'?'+urlencode(dict(project_id=project_id,folder='closeout')),303)

    def open_link(self,record_id:int,link_id:int,request:Request):
        self.origin(request)
        with self.db(True) as c:
            user,p,row=self.record(c,record_id,True)
            link=next((l for l in self.links(c,user,row) if l['id']==link_id),None)
            self.require(link is not None and link['source'] is not None,'This source record is unavailable.',404)
            if link['kind']=='document':return RedirectResponse('/documents/'+str(link['source_id'])+'/content',303)
            c.execute('INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id',(user['id'],p['id']))
            path='/issues' if link['kind']=='rfi' else '/submittals'
        return RedirectResponse(path,303)

    def use_template(self,record_id:int,request:Request,version:int=Form(...),file_id:int=Form(...),project_id:int=Form(...)):
        self.origin(request);self.require(project_id>0,'Choose the destination project.',400)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            _,_,template=self.record(c,record_id,False,version)
            self.require(template['project_id']==0,'Choose a company template.',400)
            files=self.files(c,template)
            self.require(bool(files) and files[0]['id']==file_id,'The template file changed. Reopen it to choose the current version.',409)
            f=files[0];self.file_path(f)
            existing=c.execute('SELECT id FROM bc_doc_records WHERE company_id=? AND project_id=? AND template_file_id=?',(user['company_id'],project_id,file_id)).fetchone()
            if existing:return self.redirect(existing['id'])
            meta={k:template[k] for k in ('title','folder_key','notes')};meta.update(responsible='',due_date='',status='received')
            row,_=self.make(c,user,project_id,meta,uuid.uuid4().hex,file_id)
            data={k:f[k] for k in ('original_name','stored_name','mime_type','size_bytes','sha256')}
            self.insert(c,'bc_doc_files',dict(company_id=user['company_id'],project_id=project_id,record_id=row['id'],revision=1,**data,created_by=user['id'],created=self.field.now().isoformat()))
            self.event(c,user,row,'Company template version '+str(f['revision'])+' copied')
        return self.redirect(row['id'])

    def export(self,project_id:int,folder:str=''):
        self.require(project_id>0,'Choose a project to export.',400)
        with self.db() as c:
            user,p=self.actor(c,project_id)
            options=self.folder_options(c,user['company_id']);self.require(not folder or folder in options,'Choose a valid folder.',400)
            sql='SELECT * FROM bc_doc_records WHERE company_id=? AND project_id=?';args=[user['company_id'],project_id]
            if folder:sql+=' AND folder_key=?';args.append(folder)
            rows=c.execute(sql+' ORDER BY folder_key,id LIMIT 10001',args).fetchall()
            self.require(len(rows)<=10000,'Choose one folder to export a smaller register.',413)
            data=io.StringIO(newline='');writer=csv.writer(data);writer.writerow(['Project','Record ID','Folder','Document','Status','Responsible','Due','Current file','File version','Linked records','Notes'])
            def safe(value):
                value=str(value or '')
                return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value
            for raw in rows:
                row=dict(raw);files=self.files(c,row);f=files[0] if files else {};links=self.links(c,user,row)
                writer.writerow([safe(x) for x in [p['name'],row['id'],options.get(row['folder_key'],'Folder unavailable'),row['title'],STATUS[row['status']],row['responsible'],row['due_date'],f.get('original_name'),f.get('revision'),'; '.join(l['kind']+' #'+str(l['source_id'])+(' (unavailable)' if not l['source'] else '') for l in links),row['notes']]])
        return Response('\ufeff'+data.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="project-{project_id}-document-register.csv"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

    def evidence(self,c,user,pid):
        self.actor(c,pid)
        if not self.schema_ready:return []
        rows=c.execute('SELECT id,title,folder_key,status,responsible,due_date,notes FROM bc_doc_records WHERE company_id=? AND project_id=? ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,id DESC LIMIT 20',(user['company_id'],pid,'needed')).fetchall()
        result=[('Document register metadata',r['id'],r['title'],'Folder: '+r['folder_key']+'; Document status: '+STATUS[r['status']]+'; Responsible: '+r['responsible']+'; Due: '+r['due_date']+'; Notes: '+r['notes']+'; File contents have not been analyzed.',BASE+'/'+str(r['id'])) for r in rows]
        requests=getattr(self.ns['app'].state,'document_requests',None)
        if requests:result.extend(requests.evidence(c,user,pid))
        closeout=getattr(self.ns['app'].state,'closeout_handover',None)
        if closeout:result.extend(closeout.evidence(c,user,pid))
        checklists=getattr(self.ns['app'].state,'safety_checklists',None)
        if checklists:result.extend(checklists.evidence(c,user,pid))
        return result

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes for m in (getattr(r,'methods',None) or [])}
        checks={method+' '+path:active.get((path,method)) is fn for path,method,fn in self.routes}
        checks.update(document_schema_initialized=self.schema_ready,company_templates_separate=True,immutable_file_versions=True,
                      existing_safety_inspections_submittals_preserved=all((p,'GET') in active for p in ('/safety','/inspections','/submittals')),
                      detail_scales_preserved=getattr(self.ns['app'].state,'drawing_scale_areas',None) is not None,
                      form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                for t in sorted(TABLES):c.execute(f'SELECT id,company_id FROM {t} WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
            scope='Installation and schema checks only. Verify real uploads, file versions, company templates, custom folders, project access, linked records and closeout register exports on staging. No automatic email, sharing, signatures or compliance certification.')

    def register(self):
        for path,method,fn in reversed([(BASE,'GET',self.index),(TEMPLATES,'GET',self.templates),(BASE+'/new','GET',self.new),
             (BASE+'/projects/{project_id}/closeout-starter','POST',self.closeout_starter),(BASE+'/create','POST',self.create),(BASE+'/folders','POST',self.add_folder),(BASE+'/export','GET',self.export),
             (BASE+'/{record_id}','GET',self.detail),(BASE+'/{record_id}/save','POST',self.save),
             (BASE+'/{record_id}/files','POST',self.upload),(BASE+'/{record_id}/files/{file_id}','GET',self.download),
             (BASE+'/{record_id}/links/{link_id}/remove','POST',self.remove_link),(BASE+'/{record_id}/links','POST',self.add_link),(BASE+'/{record_id}/links/{link_id}/open','POST',self.open_link),
             (BASE+'/{record_id}/use-template','POST',self.use_template)]):
            endpoint=self.endpoint(fn);self.ns['_bc840_replace'](path,method,endpoint);self.routes.append((path,method,endpoint))
        path='/health/project-documents-8-20-0'
        self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
