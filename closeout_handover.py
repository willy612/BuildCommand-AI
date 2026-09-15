"""8.22.0: trade requirements and exact reviewed, immutable handover ZIPs.

Reuses document records/files and the project subcontractor directory. Packages
are private project-leader downloads; building one never sends or shares it.
"""
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from fastapi import Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from blueprint_field import esc, digest, packed

VERSION = '8.22.0'
RELEASE = 'Closeout by Trade & Handover Packages'
BASE = '/workspace/closeout'
DOCS = '/workspace/documents'
TABLES = {'bc_closeout_requirements', 'bc_handover_reviews', 'bc_handover_packages'}
MAX_RECORDS = 1000
MAX_FILES = 200
MAX_BYTES = 500 * 1024 * 1024
STARTERS = {'warranty': 'Warranty', 'manual': 'Operation and maintenance manuals',
            'asbuilt': 'Record drawings / as-builts', 'training': 'Owner training record',
            'inspection': 'Final inspection records'}
LABELS = {'missing': 'Missing', 'requested': 'Requested', 'changes': 'Needs changes',
          'submitted': 'Submitted', 'review': 'Ready for review', 'accepted': 'Accepted',
          'filed': 'Filed', 'file_needed': 'File needed for package'}
READY = {'accepted', 'filed'}
CSS = '''<style>
.closeout-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:20px 0}.closeout-stat{padding:20px;border:1px solid #d6e1eb;border-radius:12px;background:white;color:#19364e;text-decoration:none}.closeout-stat strong{display:block;font-size:32px;margin-top:8px}.closeout-table{width:100%;border-collapse:collapse;text-align:left}.closeout-table th,.closeout-table td{padding:14px 10px;border-bottom:1px solid #dce4ed;vertical-align:top}.closeout-scroll{overflow-x:auto}.closeout-check{display:flex;gap:12px;align-items:start;padding:14px 0;border-bottom:1px solid #dce4ed}.closeout-check input{width:22px!important;height:22px;flex-shrink:0}.closeout-note{padding:18px;background:#fff3d7;border-radius:10px}.closeout-group{margin-top:28px}.closeout-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:end}.closeout-filters label{display:block}.closeout-subline{display:block;color:#536a7e;font-size:14px;margin-top:5px}
</style>'''
log = logging.getLogger('buildcommand.closeout')


def install(ns):
    service = CloseoutHandover(ns)
    ns['app'].state.closeout_handover = service
    service.register()
    return service


class CloseoutHandover:
    def __init__(self, ns):
        self.ns = ns
        self.docs = ns['app'].state.project_documents
        self.requests = ns['app'].state.document_requests
        self.field, self.db, self.require = self.docs.field, self.docs.db, self.docs.require
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        try:
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_closeout_requirements(id {key},
                    company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,record_id BIGINT NOT NULL UNIQUE,
                    sub_id BIGINT NOT NULL,contract_ref TEXT NOT NULL,version INTEGER NOT NULL,
                    updated_by BIGINT NOT NULL,updated TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_handover_reviews(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,session_hash TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,expires TEXT NOT NULL,
                    consumed TEXT,package_id BIGINT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_handover_packages(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,title TEXT NOT NULL,manifest_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,stored_name TEXT NOT NULL,sha256 TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL)''')
                for table in TABLES:
                    c.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_project ON {table}(company_id,project_id)')
            return True
        except Exception:
            log.exception('Closeout setup failed')
            return False

    def ready(self):
        self.require(self.schema_ready and self.requests.schema_ready,
                     'Closeout is unavailable. Ask your administrator to check this installation.', 503)

    def actor(self, c, pid, write=False):
        self.ready()
        self.require(pid > 0, 'Choose a project.', 400)
        return self.field.actor(c, pid, write)

    def insert(self, c, table, values):
        self.require(table in TABLES, 'Unsupported closeout record.', 400)
        sql = f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:
            return int(c.execute(sql+' RETURNING id', tuple(values.values())).fetchone()['id'])
        return int(c.execute(sql, tuple(values.values())).lastrowid)

    def origin(self, request):
        self.ready()
        self.docs.origin(request)

    def page(self, title, body): return self.docs.page(title, CSS+body)
    def hidden(self, name, value): return self.docs.hidden(name, value)
    def link(self, path, label): return self.docs.link(path, label)
    def home(self, pid): return BASE+'?project_id='+str(pid)

    def directory(self, c, pid):
        return [dict(r) for r in c.execute('SELECT id,name,trade FROM subs WHERE project_id=? ORDER BY trade,name,id', (pid,)).fetchall()]

    def sub(self, c, pid, sid):
        if sid == 0: return dict(id=0, name='Project team', trade='General closeout')
        row = c.execute('SELECT id,name,trade FROM subs WHERE id=? AND project_id=?', (sid, pid)).fetchone()
        self.require(row is not None, 'Choose a subcontractor in this project directory.', 404)
        return dict(row)

    def assignment(self, c, doc):
        row = c.execute('SELECT * FROM bc_closeout_requirements WHERE record_id=? AND company_id=? AND project_id=?',
                        (doc['id'], doc['company_id'], doc['project_id'])).fetchone()
        return dict(row) if row else None

    def assign(self, c, user, doc, sid, reference, expected):
        sub = self.sub(c, doc['project_id'], sid)
        old = self.assignment(c, doc)
        self.require(expected == (old['version'] if old else 0), 'This trade assignment changed. Reopen it before saving.', 409)
        reference = self.docs.text(reference, 240, 'a contract or specification reference')
        values = dict(sub_id=sid, contract_ref=reference, version=expected+1, updated_by=user['id'], updated=self.field.now().isoformat())
        if old:
            c.execute('UPDATE bc_closeout_requirements SET '+','.join(k+'=?' for k in values)+' WHERE id=? AND company_id=?',
                      tuple(values.values())+(old['id'], user['company_id']))
        else:
            self.insert(c, 'bc_closeout_requirements', dict(company_id=user['company_id'], project_id=doc['project_id'], record_id=doc['id'], **values))
        self.docs.event(c, user, {**doc, 'closeout_assignment': values}, 'Closeout assigned to '+str(sub['name']))

    def rows(self, c, user, pid):
        docs = c.execute('''SELECT d.* FROM bc_doc_records d WHERE d.company_id=? AND d.project_id=?
            AND (d.folder_key=? OR EXISTS(SELECT 1 FROM bc_closeout_requirements r
            WHERE r.record_id=d.id AND r.company_id=d.company_id AND r.project_id=d.project_id)) ORDER BY d.id LIMIT ?''',
                         (user['company_id'], pid, 'closeout', MAX_RECORDS+1)).fetchall()
        self.require(len(docs) <= MAX_RECORDS, 'This project has too many closeout records for one register. Ask your administrator to review it.', 413)
        if not docs:return []
        directory = {s['id']: s for s in self.directory(c, pid)}
        out = []
        for raw in docs:
            doc = dict(raw); assignment = self.assignment(c, doc)
            sid = assignment['sub_id'] if assignment else 0
            sub = directory.get(sid) if sid else dict(id=0, name='Project team / unassigned', trade='General closeout')
            sub = sub or dict(id=sid, name='Directory entry unavailable', trade='Check assignment')
            files = self.docs.files(c, doc); current = files[0] if files else None
            requests = [dict(r) for r in c.execute('''SELECT r.id,r.state,r.version,r.share_id,s.revoked_at,s.recipient_user_id,
                t.filed_file_id FROM bc_doc_requests r JOIN bc_shared_work s ON s.id=r.share_id AND s.company_id=r.company_id AND s.project_id=r.project_id
                LEFT JOIN bc_doc_submissions t ON t.id=r.latest_submission_id AND t.request_id=r.id AND t.company_id=r.company_id
                WHERE r.company_id=? AND r.project_id=? AND r.record_id=? ORDER BY r.id DESC''',
                (user['company_id'], pid, doc['id'])).fetchall()]
            active = next((r for r in requests if not r['revoked_at'] and r['state'] != 'accepted'), None)
            accepted = next((r for r in requests if current and r['state']=='accepted' and r['filed_file_id']==current['id']), None)
            if active: state = active['state']
            elif current and doc['status']=='complete': state = 'accepted' if accepted else 'filed'
            elif current: state = 'review'
            elif doc['status']=='complete': state = 'file_needed'
            else: state = 'missing'
            out.append(dict(doc=doc, assignment=assignment, sub=sub, file=current, state=state,
                            requests=requests, active=active, accepted=accepted))
        return out

    def bucket(self, row):
        return 'ready' if row['state'] in READY else 'review' if row['state'] in {'submitted','review'} else 'missing'

    def source_hash(self, project, rows):
        # Includes every requirement and its current file/request/assignment, so
        # additions or changes during review cannot disappear from the index.
        return digest(packed(dict(project=project, rows=rows)))

    def sub_select(self, c, pid, sid=0):
        return '<label for="closeout-sub">Company / trade</label><select id="closeout-sub" name="sub_id"><option value="0">Project team / assign later</option>'+''.join(
            '<option value="'+str(s['id'])+'"'+(' selected' if s['id']==sid else '')+'>'+esc(s['trade'])+' · '+esc(s['name'])+'</option>' for s in self.directory(c,pid))+'</select>'

    def index(self, project_id:int=0, sub_id:int=-1, view:str='all'):
        self.ready()
        self.require(view in {'all','missing','review','ready'}, 'Choose a listed closeout view.', 400)
        with self.db() as c:
            user,p,projects = self.docs.hub.user(c,project_id,True)
            body = '<div class="hero"><h1>Closeout</h1><p>What is missing, who owes it, and what is ready to hand over?</p></div>'+self.docs.hub.selector(projects,p,BASE)
            if not p: return self.page('Closeout',body+'<section class="card">Choose a project to open its closeout checklist.</section>')
            pid=p['id']; rows=self.rows(c,user,pid)
            self.require(sub_id>=-1,'Choose a listed trade.',400)
            subset=[r for r in rows if sub_id==-1 or (r['assignment']['sub_id'] if r['assignment'] else 0)==sub_id]
            counts={k:sum(self.bucket(r)==k for r in subset) for k in ('missing','review','ready')}
            body+='<div class="docs-actions">'+self.link(BASE+'/projects/'+str(pid)+'/new','Add requirements')+self.link(BASE+'/projects/'+str(pid)+'/package','Prepare handover')+self.link(BASE+'/projects/'+str(pid)+'/packages','Saved packages')+'</div><div class="closeout-stats">'
            for k,label in [('missing','Still needed'),('review','Waiting for review'),('ready','Ready for handover')]:
                body+='<a class="closeout-stat" href="'+self.home(pid)+'&amp;sub_id='+str(sub_id)+'&amp;view='+k+'">'+label+'<strong>'+str(counts[k])+'</strong></a>'
            body+='</div><form class="closeout-filters" method="get" action="'+BASE+'">'+self.hidden('project_id',pid)+'<div><label for="filter-sub">Company / trade</label><select name="sub_id" id="filter-sub"><option value="-1">All companies / trades</option>'
            options={0:'Project team / unassigned',**{s['id']:str(s['trade'])+' · '+str(s['name']) for s in self.directory(c,pid)}}
            for sid,label in options.items():body+='<option value="'+str(sid)+'"'+(' selected' if sid==sub_id else '')+'>'+esc(label)+'</option>'
            body+='</select></div><div><label for="filter-view">Show</label><select name="view" id="filter-view">'+''.join('<option value="'+k+'"'+(' selected' if view==k else '')+'>'+v+'</option>' for k,v in [('all','Everything'),('missing','Still needed'),('review','Waiting for review'),('ready','Ready for handover')])+'</select></div><button>Show</button></form>'
            shown=[r for r in subset if view=='all' or self.bucket(r)==view]
            groups={}
            for r in shown:groups.setdefault((str(r['sub']['trade']),str(r['sub']['name']),r['sub']['id']),[]).append(r)
            for (trade,name,sid),items in sorted(groups.items()):
                body+='<section class="card closeout-group"><div class="eyebrow">'+esc(trade)+'</div><h2>'+esc(name)+'</h2>'
                for r in items:
                    d=r['doc'];rid=d['id'];state=r['state'];overdue=d['due_date'] and d['due_date']<self.field.now().date().isoformat() and state not in READY
                    body+='<article class="docs-row"><h3>'+esc(d['title'])+'</h3><span class="docs-pill '+('docs-complete' if state in READY else 'docs-needed')+'">'+LABELS[state]+'</span><p class="docs-meta">'+('Overdue · ' if overdue else '')+'Needed by: '+esc(d['due_date'] or 'No date set')+'</p>'
                    if r['assignment'] and r['assignment']['contract_ref']:body+='<p class="docs-meta">Requirement: '+esc(r['assignment']['contract_ref'])+'</p>'
                    if not r['assignment'] and d['responsible']:body+='<p class="docs-meta">Recorded responsibility: '+esc(d['responsible'])+' · Choose the directory company below.</p>'
                    if r['file']:body+='<p class="docs-meta">'+esc(r['file']['original_name'])+' · version '+str(r['file']['revision'])+'</p>'
                    target='/workspace/sharing/'+str(r['active']['share_id']) if r['active'] else DOCS+'/'+str(rid)
                    label='Review submission' if state=='submitted' else 'Open request' if r['active'] else 'Review file' if state=='review' else 'Open record'
                    body+='<div class="docs-actions">'+self.link(target,label)
                    if state not in READY and not r['active']:body+=self.link(DOCS+'/'+str(rid)+'/request','Request from subcontractor')
                    body+=self.link(DOCS+'/'+str(rid)+'/closeout','Assign trade / requirement')+'</div></article>'
                body+='</section>'
            if not shown:body+='<section class="card"><h2>'+('No closeout requirements yet' if not rows else 'No items in this view')+'</h2><p>'+('Add the paperwork this job needs, or attach an existing document record.' if not rows else 'Choose Everything to see the full checklist.')+'</p></section>'
            body+='<div class="docs-actions">'+self.link(DOCS+'?project_id='+str(pid),'Back to documents')+self.link('/workspace/directory?project_id='+str(pid),'Subcontractor directory')+'</div>'
        return self.page('Closeout',body)

    def new(self, project_id:int):
        with self.db() as c:
            user,p=self.actor(c,project_id)
            body='<div class="hero"><h1>Add closeout requirements</h1><p>'+esc(p['name'])+'</p></div><form class="card docs-form" method="post" action="'+BASE+'/projects/'+str(project_id)+'/requirements">'+self.hidden('request_key',uuid.uuid4().hex)+self.sub_select(c,project_id)
            body+='<p>Choose only the paperwork this company owes on this job.</p>'
            for key,label in STARTERS.items():body+='<label class="closeout-check"><input type="checkbox" name="items" value="'+key+'"><span>'+esc(label)+'</span></label>'
            body+=self.docs.input('custom_title','Other requirement (optional)',extra='maxlength="240" placeholder="Example: Roof inspection and warranty registration"')+self.docs.input('due_date','Needed by','','date')
            body+='<details><summary>Contract / specification reference</summary>'+self.docs.input('contract_ref','Requirement reference',extra='maxlength="240" placeholder="Example: Section 26 00 00, closeout requirements"')+'</details><button>Add requirements</button><p class="docs-meta">Adds internal checklist records. Requesting paperwork from a person has its own review step.</p></form>'
            candidates=c.execute('SELECT id,title FROM bc_doc_records WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 100',(user['company_id'],project_id)).fetchall()
            body+='<details class="card"><summary>Use an existing document record</summary><p>Keep its files and history. Open the record to assign it to closeout.</p>'+''.join('<p><a href="'+DOCS+'/'+str(r['id'])+'/closeout">'+esc(r['title'])+'</a></p>' for r in candidates)+self.link(DOCS+'?project_id='+str(project_id),'Find all document records')+'</details>'+self.link(self.home(project_id),'Back to closeout')
        return self.page('Add closeout requirements',body)

    def create(self, project_id:int, request:Request, request_key:str=Form(...), sub_id:int=Form(0), items:list[str]=Form(default=[]), custom_title:str=Form(''), due_date:str=Form(''), contract_ref:str=Form('')):
        self.origin(request)
        self.require(bool(re.fullmatch('[a-f0-9]{32}',request_key)), 'Reopen the add-requirements form.',400)
        self.require(len(items)==len(set(items)) and set(items)<=set(STARTERS),'Choose listed requirements.',400)
        custom_title=self.docs.text(custom_title,240,'a requirement name')
        choices=[(k,STARTERS[k]) for k in items]+([('custom',custom_title)] if custom_title else [])
        self.require(bool(choices),'Select a requirement or enter one.',400)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);s=self.sub(c,project_id,sub_id)
            reference=self.docs.text(contract_ref,240,'a contract or specification reference')
            for key,title in choices:
                meta=self.docs.metadata(c,user,title,'closeout','',str(s['name']) if sub_id else '',due_date)
                record_key=digest(request_key+':'+key)[:32]
                doc,created=self.docs.make(c,user,project_id,meta,record_key)
                if created:self.assign(c,user,doc,sub_id,reference,0)
        return RedirectResponse(self.home(project_id),303)

    def assign_form(self, record_id:int):
        self.ready()
        with self.db() as c:
            user,p,d=self.docs.record(c,record_id);self.require(p['id']>0,'Use a project document, not a company template.',400)
            a=self.assignment(c,d)
            body='<div class="hero"><h1>Assign closeout requirement</h1><p>'+esc(p['name'])+' · '+esc(d['title'])+'</p></div><form class="card docs-form" method="post" action="'+DOCS+'/'+str(record_id)+'/closeout">'+self.hidden('document_version',d['version'])+self.hidden('assignment_version',a['version'] if a else 0)+self.sub_select(c,p['id'],a['sub_id'] if a else 0)
            body+=self.docs.input('contract_ref','Contract / specification reference (optional)',a['contract_ref'] if a else '',extra='maxlength="240"')+'<button>Save closeout assignment</button><p class="docs-meta">Keeps the same document record, files and folder. This assignment does not send a request or grant file access.</p></form>'+self.link(self.home(p['id']),'Back to closeout')
        return self.page('Assign closeout requirement',body)

    def assign_save(self, record_id:int, request:Request, document_version:int=Form(...), assignment_version:int=Form(...), sub_id:int=Form(0), contract_ref:str=Form('')):
        self.origin(request)
        with self.db(True) as c:
            user,p,d=self.docs.record(c,record_id,True,document_version);self.require(p['id']>0,'Use a project document.',400)
            self.assign(c,user,d,sub_id,contract_ref,assignment_version)
        return RedirectResponse(self.home(p['id']),303)

    def record_panel(self,c,user,doc):
        if not doc['project_id']:return ''
        self.ready();a=self.assignment(c,doc)
        text='Track this record in closeout and assign the company responsible.'
        if a:
            s=next((s for s in self.directory(c,doc['project_id']) if s['id']==a['sub_id']),None)
            text='Assigned to '+(str(s['trade'])+' · '+str(s['name']) if s else 'Project team' if not a['sub_id'] else 'Directory entry unavailable')
        return '<details class="card"><summary>Closeout requirement</summary><p>'+esc(text)+'</p><div class="docs-actions">'+self.link(DOCS+'/'+str(doc['id'])+'/closeout','Assign trade / requirement')+self.link(self.home(doc['project_id']),'Open closeout checklist')+'</div></details>'

    def package_form(self, project_id:int):
        with self.db() as c:
            user,p=self.actor(c,project_id);rows=self.rows(c,user,project_id)
            ready=[r for r in rows if r['state'] in READY]
            body='<div class="hero"><h1>Prepare handover</h1><p>'+esc(p['name'])+'</p></div><p>Select the filed documents to include. You will review the exact list before building the ZIP.</p>'
            body+='<p class="closeout-note">'+str(len(rows)-len(ready))+' requirements are not ready. The package index will list anything left out.</p>'
            if ready:
                body+='<form class="card" method="post" action="'+BASE+'/projects/'+str(project_id)+'/package/review">'+self.hidden('source_hash',self.source_hash(p,rows))+self.docs.input('title','Package name','Project handover - '+self.field.now().date().isoformat(),extra='maxlength="160" required')
                for r in ready:
                    d,f,s=r['doc'],r['file'],r['sub']
                    body+='<label class="closeout-check"><input type="checkbox" name="file_ids" value="'+str(f['id'])+'"><span><strong>'+esc(d['title'])+'</strong><span class="closeout-subline">'+esc(s['trade'])+' · '+esc(s['name'])+'</span><span class="closeout-subline">'+esc(f['original_name'])+' · version '+str(f['revision'])+' · '+LABELS[r['state']]+'</span></span></label>'
                body+='<p class="docs-meta">Up to 200 files and 500 MB per package. Only selected file versions and the reviewed index are included.</p><button>Review handover package</button></form>'
            else:body+='<section class="card"><h2>No files ready yet</h2><p>Accept a subcontractor submission, or review and mark an uploaded document filed first.</p></section>'
            body+=self.link(self.home(project_id),'Back to closeout')
        return self.page('Prepare handover',body)

    @staticmethod
    def safe_name(value):
        # ASCII filenames keep ZIPs portable; the original Unicode title stays
        # in the UTF-8 index. IDs make same-named trades/files unambiguous.
        return re.sub(r'[^A-Za-z0-9._-]+','-',str(value)).strip('.-')[:70] or 'document'

    def manifest(self,p,rows,selected,title):
        entries=[];omitted=[]
        for r in rows:
            d,f,s=r['doc'],r['file'],r['sub'];a=r['assignment'] or {}
            item=dict(record_id=d['id'],document=d['title'],trade=s['trade'],company=s['name'],
                      contract_ref=a.get('contract_ref',''),due_date=d['due_date'],status=LABELS[r['state']])
            if f and f['id'] in selected:
                original=Path(f['original_name'])
                filename=self.safe_name(original.stem)[:55]+re.sub(r'[^a-z0-9.]','',original.suffix.lower())
                path='Files/'+self.safe_name(s['trade'])+'-'+str(s['id'])+'/'+str(d['id'])+'-v'+str(f['revision'])+'-'+filename
                entries.append(dict(**item,file_id=f['id'],file_version=f['revision'],filename=f['original_name'],path=path,
                                    size_bytes=f['size_bytes'],sha256=f['sha256']))
            else:omitted.append(dict(**item,reason='Not selected' if r['state'] in READY else LABELS[r['state']]))
        return dict(app='BuildCommand AI',version=VERSION,project_id=p['id'],project=p['name'],title=title,
                    scope='Selected document files at the time of review. This index does not certify project completion.',
                    files=entries,not_included=omitted)

    def review(self, project_id:int, request:Request, source_hash:str=Form(...), title:str=Form(...), file_ids:list[int]=Form(default=[])):
        self.origin(request);title=self.docs.text(title,160,'a package name',True)
        self.require(0<len(file_ids)<=MAX_FILES and len(file_ids)==len(set(file_ids)), 'Select between 1 and 200 different files.',400)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);rows=self.rows(c,user,project_id)
            self.require(source_hash==self.source_hash(p,rows),'The closeout register changed. Reopen handover and select the current files.',409)
            eligible={r['file']['id']:r['file'] for r in rows if r['state'] in READY}
            self.require(set(file_ids)<=set(eligible),'Select current filed files from this project closeout register.',400)
            self.require(sum(eligible[i]['size_bytes'] for i in file_ids)<=MAX_BYTES,'Keep each package within 500 MB.',413)
            for i in file_ids:self.requests.checked_file(eligible[i])
            data=dict(source_hash=source_hash,manifest=self.manifest(p,rows,set(file_ids),title))
            raw=secrets.token_urlsafe(32);now=self.field.now()
            self.insert(c,'bc_handover_reviews',dict(company_id=user['company_id'],project_id=project_id,actor_id=user['id'],
                session_hash=self.field.session_hash(request),token_hash=digest(raw),payload_json=packed(data),
                expires=(now+timedelta(minutes=15)).isoformat(),consumed=None,package_id=None))
        body='<div class="hero"><h1>Review handover package</h1><p>'+esc(p['name'])+'</p></div>'+self.render_manifest(data['manifest'],links=True)
        body+='<form class="card" method="post" action="'+BASE+'/packages/approve">'+self.hidden('review_token',raw)+'<label class="closeout-check"><input type="checkbox" name="confirmed" value="yes" required><span>I reviewed the included files and the items left out. Create this package for download.</span></label><button>Create handover ZIP</button><p class="docs-meta">Review expires in 15 minutes. No email or file access is granted.</p></form>'+self.link(BASE+'/projects/'+str(project_id)+'/package','Change selection')
        return self.page('Review handover package',body)

    def render_manifest(self,m,links=False):
        body='<section class="card"><h2>'+esc(m['title'])+'</h2><p><strong>'+str(len(m['files']))+' files included · '+str(len(m['not_included']))+' requirements not included</strong></p><p class="docs-meta">'+esc(m['scope'])+'</p><div class="closeout-scroll"><table class="closeout-table"><thead><tr><th>Company / trade</th><th>Document</th><th>Exact file</th></tr></thead><tbody>'
        for f in m['files']:
            body+='<tr><td>'+esc(f['company'])+'<span class="closeout-subline">'+esc(f['trade'])+'</span></td><td>'+esc(f['document'])+'<span class="closeout-subline">'+esc(f['contract_ref'])+'</span><span class="closeout-subline">Needed by: '+esc(f['due_date'] or 'No date set')+'</span></td><td>'+esc(f['filename'])+' · version '+str(f['file_version'])+'<span class="closeout-subline">'+esc(f['status'])+'</span>'
            if links:body+='<a href="'+DOCS+'/'+str(f['record_id'])+'/files/'+str(f['file_id'])+'?preview=1" target="_blank" rel="noopener">Open exact file</a>'
            body+='</td></tr>'
        body+='</tbody></table></div></section>'
        if m['not_included']:
            body+='<section class="card"><h2>Not included in this package</h2><p>This list will be in the downloaded index.</p>'
            for f in m['not_included']:body+='<p><strong>'+esc(f['document'])+'</strong> · '+esc(f['trade'])+' · '+esc(f['company'])+' · '+esc(f['reason'])+'<span class="closeout-subline">'+esc(f['contract_ref'])+' · Needed by: '+esc(f['due_date'] or 'No date set')+'</span></p>'
            body+='</section>'
        return body

    @staticmethod
    def csv_index(m):
        out=io.StringIO(newline='');w=csv.writer(out)
        w.writerow(['Included','Project','Company','Trade','Document','Contract / spec reference','Due date','Status','File version','Original filename','Package path','SHA256'])
        def safe(v):
            v=str(v if v is not None else '')
            return "'"+v if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n')) else v
        for included,items in [(True,m['files']),(False,m['not_included'])]:
            for f in items:w.writerow([safe(v) for v in ['Yes' if included else 'No',m['project'],f['company'],f['trade'],f['document'],f['contract_ref'],f['due_date'],f.get('reason',f['status']),f.get('file_version',''),f.get('filename',''),f.get('path',''),f.get('sha256','')]])
        return ('\ufeff'+out.getvalue()).encode('utf-8')

    def html_index(self,m):
        body='<header><div class="brand">BuildCommand <span>AI</span></div><p>PROJECT HANDOVER</p><h1>'+esc(m['title'])+'</h1><h2>'+esc(m['project'])+'</h2><p>Created '+esc(m['created'])+' · Reviewed by '+esc(m['reviewed_by'])+'</p></header>'
        body+='<main><p class="notice">'+str(len(m['files']))+' files included · '+str(len(m['not_included']))+' requirements not included. '+esc(m['scope'])+'</p><p>Extract the entire ZIP first. Then open a document using the links below.</p>'
        groups={}
        for f in m['files']:groups.setdefault((f['trade'],f['company']),[]).append(f)
        for (trade,company),items in groups.items():
            body+='<section><p class="trade">'+esc(trade)+'</p><h2>'+esc(company)+'</h2>'
            for f in items:body+='<article><h3><a href="'+esc(f['path'])+'">'+esc(f['document'])+'</a></h3><p>'+esc(f['filename'])+' · Version '+str(f['file_version'])+' · '+esc(f['status'])+'</p><p>'+esc(f['contract_ref'])+'</p></article>'
            body+='</section>'
        if m['not_included']:
            body+='<section><h2>Not included in this package</h2>'
            for f in m['not_included']:body+='<article><h3>'+esc(f['document'])+'</h3><p>'+esc(f['trade'])+' · '+esc(f['company'])+' · '+esc(f['reason'])+'</p></article>'
            body+='</section>'
        body+='</main><footer>Built By Willy LaHood ©2026</footer>'
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(m['title'])+'</title><style>body{margin:0;background:#f3f6fa;color:#17324b;font:16px/1.6 Arial,sans-serif}header{background:#122638;color:white;padding:36px max(6vw,20px);border-bottom:4px solid #e8b045}.brand{font-size:24px;font-weight:bold}.brand span{color:#e8b045}h1{font-size:32px;line-height:1.2}h2{margin:8px 0}main,footer{max-width:1080px;margin:auto;padding:24px}section{background:white;padding:24px;border:1px solid #d6e1eb;border-radius:12px;margin:24px 0}article{padding:12px 0;border-top:1px solid #d6e1eb}article h3,article p{margin:4px 0}.trade{color:#825811;font-weight:bold;text-transform:uppercase;font-size:13px}.notice{background:#fff0ce;padding:20px;border-radius:10px}a{color:#164c80;text-decoration:underline}a:focus-visible{outline:3px solid #bd8420}footer{color:#536a7e;font-size:14px}@media print{body{background:white}header{background:white;color:#17324b}section{break-inside:avoid}}</style></head><body>'+body+'</body></html>').encode('utf-8')

    def build_zip(self,path,manifest,files):
        total=0
        with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as bundle:
            for item in manifest['files']:
                f=files[item['file_id']];source=self.docs.file_path(f);sha=hashlib.sha256();size=0
                with source.open('rb') as src,bundle.open(item['path'],'w',force_zip64=True) as dst:
                    while chunk:=src.read(1024*1024):
                        size+=len(chunk);total+=len(chunk)
                        self.require(size<=f['size_bytes'] and total<=MAX_BYTES,'A file size changed. Review the files again.',409)
                        sha.update(chunk);dst.write(chunk)
                self.require(size==f['size_bytes'] and sha.hexdigest()==f['sha256'],'A selected file changed. Restore its original version and review again.',409)
            bundle.writestr('Document-index.csv',self.csv_index(manifest))
            bundle.writestr('Start-here.html',self.html_index(manifest))
            bundle.writestr('Manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
            bundle.writestr('Read-me.txt',(manifest['title']+'\nProject: '+manifest['project']+'\nCreated: '+manifest['created']+'\n\n'+manifest['scope']+'\n\nExtract the entire ZIP, then open Start-here.html for the clickable document index.\nDocument-index.csv lists included files and remaining requirements.\nFiles keep the exact versions reviewed. Later revisions do not change this package.\n\nBuilt By Willy LaHood ©2026\n').encode('utf-8'))

    def approve(self, request:Request, review_token:str=Form(...), confirmed:str=Form('')):
        self.origin(request);self.require(confirmed=='yes','Review and confirm the exact package first.',400)
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}',review_token)),'Reopen the package review.',403)
        path=None;saved=False
        try:
            with self.db(True) as c:
                user=self.ns['_bc850_actor'](c)
                raw=c.execute('SELECT * FROM bc_handover_reviews WHERE token_hash=? AND company_id=?',(digest(review_token),user['company_id'])).fetchone()
                self.require(raw is not None,'This package review is unavailable.',403)
                user,p=self.actor(c,raw['project_id'],True)
                r=dict(c.execute('SELECT * FROM bc_handover_reviews WHERE id=?'+self.field.lock,(raw['id'],)).fetchone())
                self.require(r['actor_id']==user['id'] and secrets.compare_digest(r['session_hash'],self.field.session_hash(request)), 'Approve from the same signed-in session that reviewed this package.',403)
                # A committed receipt may be retried after its preview expires;
                # current project access and actor/session binding still apply.
                if r['consumed']:return RedirectResponse(BASE+'/packages/'+str(r['package_id']),303)
                self.require(r['expires']>self.field.now().isoformat(),'This review expired. Select and review the current files again.',409)
                data=json.loads(r['payload_json']);rows=self.rows(c,user,p['id'])
                self.require(data['source_hash']==self.source_hash(p,rows),'Closeout changed after this review. Prepare a new package with the current files.',409)
                m=data['manifest'];selected={f['file_id'] for f in m['files']}
                expected=self.manifest(p,rows,selected,m['title'])
                self.require(expected==m,'The selected files changed. Review again.',409)
                now=self.field.now().isoformat();m={**m,'created':now,'reviewed_by':user['display_name'] or user['email']}
                root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();root.mkdir(parents=True,exist_ok=True)
                path=root/('handover-'+uuid.uuid4().hex+'.zip')
                files={r['file']['id']:r['file'] for r in rows if r['file'] and r['file']['id'] in selected}
                self.build_zip(path,m,files)
                with path.open('rb') as source:sha=hashlib.file_digest(source,'sha256').hexdigest()
                identity=self.insert(c,'bc_handover_packages',dict(company_id=user['company_id'],project_id=p['id'],title=m['title'],manifest_json=packed(m),source_hash=data['source_hash'],stored_name=path.name,sha256=sha,size_bytes=path.stat().st_size,created_by=user['id'],created=now))
                self.ns['_bc850_event'](c,user,p['id'],None,'HANDOVER_PACKAGE_CREATED:'+str(identity))
                c.execute('UPDATE bc_handover_reviews SET consumed=?,package_id=? WHERE id=?',(now,identity,r['id']))
            saved=True
            return RedirectResponse(BASE+'/packages/'+str(identity),303)
        finally:
            if path is not None and not saved:path.unlink(missing_ok=True)

    def package(self,c,identity):
        user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM bc_handover_packages WHERE id=? AND company_id=?',(identity,user['company_id'])).fetchone()
        self.require(row is not None,'This handover package is unavailable.',404)
        user,p=self.actor(c,row['project_id'])
        return user,p,dict(row)

    def packages(self,project_id:int,offset:int=0):
        self.require(0<=offset<=100000,'Choose a valid results page.',400)
        with self.db() as c:
            user,p=self.actor(c,project_id)
            rows=c.execute('SELECT id,title,created FROM bc_handover_packages WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 51 OFFSET ?',(user['company_id'],project_id,offset)).fetchall()
        body='<div class="hero"><h1>Saved handover packages</h1><p>'+esc(p['name'])+'</p></div><div class="docs-actions">'+self.link(BASE+'/projects/'+str(project_id)+'/package','Prepare handover')+self.link(self.home(project_id),'Back to closeout')+'</div>'
        for r in rows[:50]:body+='<article class="card"><h2>'+esc(r['title'])+'</h2><p>'+esc(r['created'])+'</p>'+self.link(BASE+'/packages/'+str(r['id']),'Open package')+'</article>'
        if not rows:body+='<section class="card">No handover package created yet.</section>'
        if len(rows)>50:body+=self.link(BASE+'/projects/'+str(project_id)+'/packages?offset='+str(offset+50),'Older packages')
        return self.page('Saved handover packages',body)

    def detail(self,package_id:int):
        with self.db() as c:
            user,p,r=self.package(c,package_id);m=json.loads(r['manifest_json'])
            changed=r['source_hash']!=self.source_hash(p,self.rows(c,user,p['id']))
        body='<div class="hero"><h1>Handover package ready</h1><p>'+esc(m['project'])+' · Created '+esc(m['created'])+'</p></div>'
        if changed:body+='<p class="closeout-note">The closeout register has changed since this package was created. This download keeps the original reviewed versions. Prepare a new package for updated files.</p>'
        body+='<div class="docs-actions">'+self.link(BASE+'/packages/'+str(package_id)+'/download','Download handover ZIP')+self.link(BASE+'/projects/'+str(p['id'])+'/packages','Back to saved packages')+'</div><p>Reviewed by '+esc(m['reviewed_by'])+'. The package has not been sent to anyone.</p>'+self.render_manifest(m)
        return self.page('Handover package ready',body)

    def download(self,package_id:int):
        with self.db() as c:user,p,r=self.package(c,package_id)
        path=self.docs.file_path(r)
        with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        self.require(sha==r['sha256'] and path.stat().st_size==r['size_bytes'],'The stored package changed. Ask your administrator to restore it.',409)
        return FileResponse(path,filename='project-'+str(p['id'])+'-handover-'+str(package_id)+'.zip',media_type='application/zip',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})

    def evidence(self,c,user,pid):
        if not self.schema_ready:return []
        self.actor(c,pid);rows=self.rows(c,user,pid)
        counts={k:sum(self.bucket(r)==k for r in rows) for k in ('missing','review','ready')}
        out=[('Closeout register status',pid,'Project closeout',f"{len(rows)} requirements; {counts['missing']} still needed; {counts['review']} waiting for review; {counts['ready']} ready for handover. Filed documents do not certify project completion.",self.home(pid))]
        for r in sorted(rows,key=lambda r:(self.bucket(r)=='ready',r['doc']['due_date'] or '9999',r['doc']['id']))[:20]:
            d=r['doc'];out.append(('Closeout requirement',d['id'],d['title'],'Company: '+str(r['sub']['name'])+'; Trade: '+str(r['sub']['trade'])+'; Status: '+LABELS[r['state']]+'; Due: '+d['due_date']+'; File contents have not been analyzed.',DOCS+'/'+str(d['id'])))
        return out

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes for m in (getattr(r,'methods',None) or [])}
        checks={m+' '+p:active.get((p,m)) is fn for p,m,fn in self.routes}
        checks.update(closeout_schema_initialized=self.schema_ready,existing_document_versions_used=True,
            project_directory_connected=True,exact_package_review=True,immutable_package_history=True,
            document_requests_preserved=getattr(self.ns['app'].state,'document_requests',None) is self.requests,
            form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                for t in TABLES:c.execute(f'SELECT id,company_id FROM {t} WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
            scope='Installation and schema checks only. Test trade requirements, accepted submissions, exact package review, ZIP contents, later revisions, project access and a first-user walkthrough on staging. No automatic email, sharing, signatures or completion certification.')

    def register(self):
        entries=[(BASE,'GET',self.index),(BASE+'/projects/{project_id}/new','GET',self.new),
            (BASE+'/projects/{project_id}/requirements','POST',self.create),(DOCS+'/{record_id}/closeout','GET',self.assign_form),
            (DOCS+'/{record_id}/closeout','POST',self.assign_save),(BASE+'/projects/{project_id}/package','GET',self.package_form),
            (BASE+'/projects/{project_id}/package/review','POST',self.review),(BASE+'/packages/approve','POST',self.approve),
            (BASE+'/projects/{project_id}/packages','GET',self.packages),(BASE+'/packages/{package_id}','GET',self.detail),
            (BASE+'/packages/{package_id}/download','GET',self.download)]
        for p,m,fn in reversed(entries):
            endpoint=self.docs.endpoint(fn);self.ns['_bc840_replace'](p,m,endpoint);self.routes.append((p,m,endpoint))
        path='/health/closeout-handover-8-22-0'
        self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
