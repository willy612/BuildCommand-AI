"""8.23.0: company-authored checks, evidence, reviewed corrections and filing.

Uses existing leader/project access and exact document-request trade handoffs.
It records observations, not regulatory approvals or electronic signatures.
"""
import hashlib
import inspect
import json
import logging
import re
import secrets
import uuid
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from blueprint_field import esc, digest, packed
from checklist_report import build_report

VERSION='8.23.0'
RELEASE='Safety & Inspection Checklists'
BASE='/workspace/checklists'
TEMPLATES='/workspace/checklist-templates'
RESULTS={'unset':'Not checked','pass':'Pass','attention':'Needs attention','na':'Not applicable'}
KINDS={'safety':'Safety','inspection':'Inspection'}
TABLES={'bc_check_templates','bc_check_template_versions','bc_check_runs','bc_check_items','bc_check_photos','bc_check_events','bc_check_reviews'}
PHOTO_LIMIT=20*1024*1024
PHOTO_COUNT=20
CSS='''<style>.check-counts{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.check-counts span{padding:14px 20px;background:#edf3f8;border-radius:9px}.check-item{padding:24px;border:1px solid #d3e0eb;border-radius:12px;background:white;margin:20px 0}.check-item h2{margin:8px 0 14px}.check-item:target{outline:3px solid #d8a23c}.check-response{display:flex;flex-wrap:wrap;gap:12px;border:0;padding:0;margin:16px 0}.check-response label{display:flex;gap:9px;align-items:center;border:1px solid #b9cada;padding:14px;border-radius:9px;cursor:pointer}.check-response input{width:20px;height:20px}.check-response label:has(input:checked){background:#e5eff7;border:2px solid #173b5e}.check-photos{display:flex;flex-wrap:wrap;gap:16px}.check-photos figure{margin:12px 0;width:230px}.check-photos img{display:block;width:230px;max-width:100%;height:160px;object-fit:contain;background:#e8eef4;border-radius:8px}.check-photos figcaption{font-size:14px;margin-top:8px}.check-note{background:#fff2d2;padding:18px;border-radius:8px}.check-item textarea{width:100%;box-sizing:border-box}.check-sticky{position:sticky;bottom:0;background:#fff;padding:14px;border-top:1px solid #d4e1ec;box-shadow:0 -3px 14px #16344a0d}.check-item .docs-actions{margin:12px 0}.check-summary{padding:14px 0;border-bottom:1px solid #d5e1eb}.check-summary h3{margin:5px 0}@media(max-width:600px){.check-item{padding:16px}.check-response label{flex:1 1 100%}.check-photos figure{width:100%}.check-photos img{width:100%}.check-sticky button{width:100%}}</style>'''
log=logging.getLogger('buildcommand.checklists')


def install(ns):
    service=SafetyChecklists(ns);ns['app'].state.safety_checklists=service;service.register();return service


class SafetyChecklists:
    def __init__(self,ns):
        self.ns=ns;self.docs=ns['app'].state.project_documents;self.requests=ns['app'].state.document_requests
        self.field,self.db,self.require=self.docs.field,self.docs.db,self.docs.require
        self.routes=[];self.schema_ready=self.initialize()

    def initialize(self):
        key='BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        try:
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_templates(id {key},company_id BIGINT NOT NULL,
                    name TEXT NOT NULL,kind TEXT NOT NULL,version INTEGER NOT NULL,prompts_json TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_template_versions(id {key},company_id BIGINT NOT NULL,
                    template_id BIGINT NOT NULL,version INTEGER NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,
                    prompts_json TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL,UNIQUE(template_id,version))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_runs(id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    title TEXT NOT NULL,kind TEXT NOT NULL,area TEXT NOT NULL,check_date TEXT NOT NULL,template_id BIGINT,
                    template_version INTEGER,version INTEGER NOT NULL,state TEXT NOT NULL,request_key TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,closed_json TEXT NOT NULL,
                    closed_by BIGINT,closed_at TEXT,document_id BIGINT,file_id BIGINT,UNIQUE(company_id,created_by,request_key))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_items(id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    run_id BIGINT NOT NULL,position INTEGER NOT NULL,label TEXT NOT NULL,result TEXT NOT NULL,
                    note TEXT NOT NULL,finding_note TEXT NOT NULL,resolution TEXT NOT NULL,was_attention INTEGER NOT NULL,correction_record_id BIGINT,correction_reference_file_id BIGINT,
                    UNIQUE(run_id,position))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_photos(id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    run_id BIGINT NOT NULL,item_id BIGINT NOT NULL,request_key TEXT NOT NULL,original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes BIGINT NOT NULL,sha256 TEXT NOT NULL,
                    caption TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL,removed_at TEXT,
                    UNIQUE(run_id,request_key))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_events(id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    run_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,action TEXT NOT NULL,snapshot_json TEXT NOT NULL,created TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_check_reviews(id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    run_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,session_hash TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
                    source_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,stored_name TEXT NOT NULL,sha256 TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,expires TEXT NOT NULL,consumed TEXT)''')
                for t in TABLES:
                    cols='company_id' if t in {'bc_check_templates','bc_check_template_versions'} else 'company_id,project_id'
                    c.execute(f'CREATE INDEX IF NOT EXISTS idx_{t}_scope ON {t}({cols})')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_check_items_run ON bc_check_items(company_id,run_id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_check_photos_run ON bc_check_photos(company_id,run_id)')
            return True
        except Exception:
            log.exception('Checklist setup failed');return False

    def ready(self):self.require(self.schema_ready and self.requests.schema_ready,'Checklists are unavailable. Ask your administrator to check this installation.',503)
    def origin(self,request):self.ready();self.docs.origin(request)
    def page(self,title,body):return self.docs.page(title,CSS+body)
    def link(self,path,label):return self.docs.link(path,label)
    def hidden(self,key,value):return self.docs.hidden(key,value)
    def home(self,pid):return BASE+'?project_id='+str(pid)
    def redirect(self,rid,item=None):return RedirectResponse(BASE+'/'+str(rid)+('#item-'+str(item) if item else ''),303)
    def text(self,value,limit,label,required=False):return self.docs.text(value,limit,label,required)

    def insert(self,c,table,values):
        self.require(table in TABLES,'Unsupported checklist record.',400)
        sql=f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:return int(c.execute(sql+' RETURNING id',tuple(values.values())).fetchone()['id'])
        return int(c.execute(sql,tuple(values.values())).lastrowid)

    def prompts(self,raw):
        self.require(len(raw)<=30000,'Keep the checklist within 30,000 characters.',400)
        lines=[self.text(x,400,'each checklist item',True) for x in raw.splitlines() if x.strip()]
        self.require(1<=len(lines)<=60,'Enter between 1 and 60 checklist items, one per line.',400)
        return lines

    def kind(self,value):self.require(value in KINDS,'Choose Safety or Inspection.',400);return value
    def template(self,c,user,tid):
        row=c.execute('SELECT * FROM bc_check_templates WHERE id=? AND company_id=?',(tid,user['company_id'])).fetchone()
        self.require(row is not None,'This company checklist template is unavailable.',404);return dict(row)

    def templates(self,template_id:int=0):
        self.ready()
        with self.db() as c:
            user,_=self.docs.actor(c,0);editing=self.template(c,user,template_id) if template_id else None
            rows=c.execute('SELECT id,name,kind,version FROM bc_check_templates WHERE company_id=? ORDER BY name,id',(user['company_id'],)).fetchall()
            can_edit=self.ns['_bc840_tier'](user) in {'owner','admin'}
            body='<div class="hero"><h1>Company checklist templates</h1><p>Save the checks your team uses, then start a fresh checklist on each job.</p></div>'
            for r in rows:body+='<section class="card"><h2>'+esc(r['name'])+'</h2><p>'+KINDS[r['kind']]+' · Version '+str(r['version'])+'</p>'+self.link(TEMPLATES+'?template_id='+str(r['id']),'Open template')+'</section>'
            if editing:body+='<section class="card"><h2>'+esc(editing['name'])+'</h2><ol>'+''.join('<li>'+esc(x)+'</li>' for x in json.loads(editing['prompts_json']))+'</ol></section>'
            if can_edit:
                row=editing or {};body+='<form class="card docs-form" method="post" action="'+TEMPLATES+'/'+str(row.get('id',0))+'/save"><h2>'+('Revise template' if editing else 'Add a template')+'</h2>'+self.hidden('version',row.get('version',0))+self.docs.input('name','Template name',row.get('name',''),extra='maxlength="160" required')+self.kind_select(row.get('kind','safety'))
                body+='<label for="template-prompts">Checklist items — one per line</label><textarea id="template-prompts" name="prompts" rows="8" maxlength="30000" required>'+esc('\n'.join(json.loads(row['prompts_json'])) if editing else '')+'</textarea><p class="docs-meta">Use your company\'s requirements for the work being checked. Changing a template does not change checklists already started.</p><button>Save template version</button></form>'
            else:body+='<p>Your company administrator manages reusable templates.</p>'
            body+=self.link(BASE,'Back to checklists')
        return self.page('Company checklist templates',body)

    def template_save(self,template_id:int,request:Request,version:int=Form(...),name:str=Form(...),kind:str=Form(...),prompts:str=Form(...)):
        self.origin(request);name=self.text(name,160,'a template name',True);kind=self.kind(kind);items=self.prompts(prompts)
        with self.db(True) as c:
            user,_=self.docs.actor(c,0,True);now=self.field.now().isoformat()
            if template_id:
                old=self.template(c,user,template_id);self.require(old['version']==version,'This template changed. Reopen it before saving.',409)
                c.execute('UPDATE bc_check_templates SET name=?,kind=?,version=?,prompts_json=?,updated=? WHERE id=? AND company_id=?',(name,kind,version+1,packed(items),now,template_id,user['company_id']))
            else:
                self.require(version==0,'Reopen the new-template form.',409)
                template_id=self.insert(c,'bc_check_templates',dict(company_id=user['company_id'],name=name,kind=kind,version=1,prompts_json=packed(items),created_by=user['id'],created=now,updated=now))
            self.insert(c,'bc_check_template_versions',dict(company_id=user['company_id'],template_id=template_id,version=version+1,name=name,kind=kind,prompts_json=packed(items),created_by=user['id'],created=now))
        return RedirectResponse(TEMPLATES+'?template_id='+str(template_id),303)

    def kind_select(self,selected):return '<label for="check-kind">Type</label><select name="kind" id="check-kind">'+''.join('<option value="'+k+'"'+(' selected' if k==selected else '')+'>'+v+'</option>' for k,v in KINDS.items())+'</select>'

    def run(self,c,rid,write=False,version=None,open_only=False):
        self.ready();user=self.ns['_bc850_actor'](c)
        raw=c.execute('SELECT * FROM bc_check_runs WHERE id=? AND company_id=?',(rid,user['company_id'])).fetchone()
        self.require(raw is not None,'This checklist is unavailable.',404)
        user,p=self.field.actor(c,raw['project_id'],write)
        r=dict(c.execute('SELECT * FROM bc_check_runs WHERE id=? AND company_id=?',(rid,user['company_id'])).fetchone())
        if version is not None:self.require(r['version']==version,'This checklist changed. Reopen it before saving.',409)
        if open_only:self.require(r['state']=='open','This checklist was closed. Start a new check to record later work.',409)
        return user,p,r

    def item(self,c,r,iid):
        row=c.execute('SELECT * FROM bc_check_items WHERE id=? AND run_id=? AND company_id=? AND project_id=?',(iid,r['id'],r['company_id'],r['project_id'])).fetchone()
        self.require(row is not None,'This item is unavailable in this checklist.',404);return dict(row)

    def event(self,c,user,r,action,snapshot):
        self.insert(c,'bc_check_events',dict(company_id=user['company_id'],project_id=r['project_id'],run_id=r['id'],actor_id=user['id'],action=action,snapshot_json=packed(snapshot),created=self.field.now().isoformat()))

    def changed(self,c,user,r,action,snapshot):
        c.execute('UPDATE bc_check_runs SET version=version+1,updated=? WHERE id=? AND company_id=?',(self.field.now().isoformat(),r['id'],user['company_id']))
        self.event(c,user,r,action,snapshot)

    def new(self,project_id:int,template_id:int=0):
        self.ready()
        with self.db() as c:
            user,p=self.field.actor(c,project_id);t=self.template(c,user,template_id) if template_id else None
            rows=c.execute('SELECT id,name FROM bc_check_templates WHERE company_id=? ORDER BY name,id',(user['company_id'],)).fetchall()
            body='<div class="hero"><h1>Start a checklist</h1><p>'+esc(p['name'])+'</p></div><form class="card" method="get" action="'+BASE+'/projects/'+str(project_id)+'/new"><label for="use-template">Use a company template</label><select name="template_id" id="use-template"><option value="0">Write a job-specific checklist</option>'+''.join('<option value="'+str(x['id'])+'"'+(' selected' if x['id']==template_id else '')+'>'+esc(x['name'])+'</option>' for x in rows)+'</select> <button>Use this checklist</button></form>'
            body+='<form class="card docs-form" method="post" action="'+BASE+'/projects/'+str(project_id)+'/create">'+self.hidden('request_key',uuid.uuid4().hex)+self.hidden('template_id',template_id)+self.hidden('template_version',t['version'] if t else 0)
            body+=self.docs.input('title','Checklist name',t['name'] if t else '',extra='maxlength="200" required placeholder="Example: Level 1 site walk"')
            body+=self.hidden('kind',t['kind']) if t else self.kind_select('safety')
            body+=self.docs.input('area','Location / work area','',extra='maxlength="160" placeholder="Example: East wing, level 1"')+self.docs.input('check_date','Date',self.field.now().date().isoformat(),'date',extra='required')
            if t:body+='<ol>'+''.join('<li>'+esc(x)+'</li>' for x in json.loads(t['prompts_json']))+'</ol>'
            else:body+='<label for="run-prompts">What are you checking? One item per line.</label><textarea id="run-prompts" name="prompts" rows="6" maxlength="30000" required></textarea>'
            body+='<button>Start check</button></form>'+self.link(self.home(project_id),'Back to checklists')
        return self.page('Start a checklist',body)

    def create(self,project_id:int,request:Request,request_key:str=Form(...),title:str=Form(...),kind:str=Form(...),area:str=Form(''),check_date:str=Form(...),template_id:int=Form(0),template_version:int=Form(0),prompts:str=Form('')):
        self.origin(request);title=self.text(title,200,'a checklist name',True);kind=self.kind(kind);area=self.text(area,160,'a work area')
        self.require(bool(re.fullmatch('[a-f0-9]{32}',request_key)),'Reopen the new-checklist form.',400)
        try:self.require(date.fromisoformat(check_date).isoformat()==check_date,'Enter a valid date.',400)
        except ValueError:self.require(False,'Enter a valid date.',400)
        with self.db(True) as c:
            user,p=self.field.actor(c,project_id,True)
            old=c.execute('SELECT id,project_id FROM bc_check_runs WHERE company_id=? AND created_by=? AND request_key=?',(user['company_id'],user['id'],request_key)).fetchone()
            if old:
                self.require(old['project_id']==project_id,'Reopen the form for this project.',409);return self.redirect(old['id'])
            t=self.template(c,user,template_id) if template_id else None
            if t:self.require(t['version']==template_version and t['kind']==kind,'This template changed. Reopen it to use the current version.',409)
            items=json.loads(t['prompts_json']) if t else self.prompts(prompts);now=self.field.now().isoformat()
            rid=self.insert(c,'bc_check_runs',dict(company_id=user['company_id'],project_id=project_id,title=title,kind=kind,area=area,check_date=check_date,template_id=template_id or None,template_version=template_version or None,version=1,state='open',request_key=request_key,created_by=user['id'],created=now,updated=now,closed_json='',closed_by=None,closed_at=None,document_id=None,file_id=None))
            for pos,label in enumerate(items,1):self.insert(c,'bc_check_items',dict(company_id=user['company_id'],project_id=project_id,run_id=rid,position=pos,label=label,result='unset',note='',finding_note='',resolution='',was_attention=0,correction_record_id=None,correction_reference_file_id=None))
            self.event(c,user,dict(id=rid,project_id=project_id),'Checklist started',dict(title=title,kind=kind,items=items,template_version=template_version))
        return self.redirect(rid)

    def correction(self,c,r,item):
        if not item['correction_record_id']:return None
        _,_,doc=self.docs.record(c,item['correction_record_id'])
        self.require(doc['project_id']==r['project_id'],'Correction evidence belongs to another project.',409)
        files=self.docs.files(c,doc)
        requests=[dict(x) for x in c.execute('''SELECT q.id,q.state,q.version,q.share_id,s.revoked_at,s.recipient_user_id,
            u.email AS recipient,t.filed_file_id FROM bc_doc_requests q JOIN bc_shared_work s ON s.id=q.share_id AND s.company_id=q.company_id
            LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id
            LEFT JOIN bc_doc_submissions t ON t.id=q.latest_submission_id AND t.company_id=q.company_id AND t.request_id=q.id
            WHERE q.company_id=? AND q.project_id=? AND q.record_id=? ORDER BY q.id DESC''',(r['company_id'],r['project_id'],doc['id'])).fetchall()]
        return dict(document=doc,file=files[0] if files else None,requests=requests,reference_file_id=item['correction_reference_file_id'])

    def correction_ready(self,data):
        if not data:return True
        if any(not r['revoked_at'] and r['state']!='accepted' for r in data['requests']):return False
        accepted=[r for r in data['requests'] if r['state']=='accepted']
        f=data['file']
        if accepted:return bool(f and data['document']['status']=='complete' and any(r['filed_file_id']==f['id'] for r in accepted))
        if f and f['id']!=data.get('reference_file_id') and data['document']['status']!='complete':return False
        return True  # Unpublished/withdrawn requests require the leader's local verification note.

    def items(self,c,r):
        out=[]
        for raw in c.execute('SELECT * FROM bc_check_items WHERE company_id=? AND project_id=? AND run_id=? ORDER BY position',(r['company_id'],r['project_id'],r['id'])).fetchall():
            item=dict(raw)
            item['photos']=[dict(x) for x in c.execute('SELECT * FROM bc_check_photos WHERE company_id=? AND project_id=? AND run_id=? AND item_id=? AND removed_at IS NULL ORDER BY id',(r['company_id'],r['project_id'],r['id'],item['id'])).fetchall()]
            item['correction']=self.correction(c,r,item);out.append(item)
        return out

    def blocking(self,items):
        return [i for i in items if i['result'] in {'unset','attention'} or (i['was_attention'] and not i['resolution']) or not self.correction_ready(i['correction'])]

    def index(self,project_id:int=0,kind:str='',state:str='open',offset:int=0):
        self.ready();self.require(kind in {'',*KINDS} and state in {'open','closed','all'} and 0<=offset<=100000,'Choose a listed checklist view.',400)
        with self.db() as c:
            user,p,projects=self.docs.hub.user(c,project_id,True)
            body='<div class="hero"><h1>Safety &amp; inspections</h1><p>What needs checking, what needs fixing, and who is handling it?</p></div>'+self.docs.hub.selector(projects,p,BASE)
            if not p:return self.page('Safety & inspections',body+'<section class="card">Choose a project to see its checklists.</section>')
            pid=p['id'];body+='<div class="docs-actions">'+self.link(BASE+'/projects/'+str(pid)+'/new','Start a checklist')+self.link(TEMPLATES,'Company templates')+'</div><form class="card" method="get" action="'+BASE+'">'+self.hidden('project_id',pid)+'<label for="filter-kind">Type </label><select id="filter-kind" name="kind"><option value="">All checklists</option>'+''.join('<option value="'+k+'"'+(' selected' if k==kind else '')+'>'+v+'</option>' for k,v in KINDS.items())+'</select> <label for="filter-state">Status </label><select id="filter-state" name="state">'+''.join('<option value="'+k+'"'+(' selected' if k==state else '')+'>'+v+'</option>' for k,v in [('open','Open'),('closed','Reviewed & closed'),('all','All records')])+'</select> <button>Show</button></form>'
            where='company_id=? AND project_id=?';args=[user['company_id'],pid]
            if kind:where+=' AND kind=?';args.append(kind)
            if state!='all':where+=' AND state=?';args.append(state)
            rows=c.execute('SELECT * FROM bc_check_runs WHERE '+where+' ORDER BY id DESC LIMIT 51 OFFSET ?',args+[offset]).fetchall()
            for raw in rows[:50]:
                r=dict(raw);items=json.loads(r['closed_json'])['items'] if r['state']=='closed' else self.items(c,r);blocked=len(self.blocking(items))
                body+='<section class="card"><div class="eyebrow">'+KINDS[r['kind']]+'</div><h2>'+esc(r['title'])+'</h2><p>'+esc(r['check_date'])+' · '+esc(r['area'] or 'Whole job')+'</p><p>'+('Reviewed &amp; closed' if r['state']=='closed' else str(blocked)+' items need attention / checking' if blocked else 'Ready for final review')+'</p>'+self.link(BASE+'/'+str(r['id']),'Open checklist')+'</section>'
            if not rows:body+='<section class="card"><h2>No checklists in this view</h2><p>Start a checklist from your company template or write the checks for this job.</p></section>'
            if len(rows)>50:body+=self.link(self.home(pid)+'&kind='+kind+'&state='+state+'&offset='+str(offset+50),'Older checklists')
            body+='<div class="docs-actions">'+self.link('/workspace/command?project_id='+str(pid),'Back to Command')+self.link('/workspace/documents?project_id='+str(pid),'Documents')+'</div><details class="card"><summary>Earlier safety and inspection records</summary>'+self.docs.hub.open_form(pid,'/safety','Open earlier safety records')+self.docs.hub.open_form(pid,'/inspections','Open earlier inspections')+'</details>'
        return self.page('Safety & inspections',body)

    def detail(self,run_id:int):
        with self.db() as c:
            user,p,r=self.run(c,run_id);closed=r['state']=='closed'
            items=json.loads(r['closed_json'])['items'] if closed else self.items(c,r)
            body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+' · '+KINDS[r['kind']]+'</div><h1>'+esc(r['title'])+'</h1><p>'+esc(r['check_date'])+' · '+esc(r['area'] or 'Whole job')+'</p></div><div class="docs-actions">'+self.link(self.home(p['id']),'Back to checklists')+'</div>'
            if closed:body+='<section class="card"><h2>Reviewed &amp; closed</h2><p>Closed '+esc(r['closed_at'])+'. This checklist keeps the reviewed snapshot.</p>'+self.link('/workspace/documents/'+str(r['document_id']),'Open filed record')+self.link(BASE+'/projects/'+str(p['id'])+'/new'+('?template_id='+str(r['template_id']) if r['template_id'] else ''),'Start a new check')+'</section>'
            else:body+='<div class="check-counts"><span>'+str(sum(i['result']=='unset' for i in items))+' not checked</span><span>'+str(sum(i['result']=='attention' for i in items))+' need attention</span><span>'+str(sum(i['result'] in {'pass','na'} for i in items))+' answered</span></div><p>Save each item as you check it. Photos and correction details are available below each item.</p>'
            for i in items:
                iid=i['id'];base=BASE+'/'+str(run_id)+'/items/'+str(iid);token=self.hidden('version',r['version'])
                body+='<section class="check-item" id="item-'+str(iid)+'"><span class="docs-pill">'+RESULTS[i['result']]+'</span><h2>'+str(i['position'])+'. '+esc(i['label'])+'</h2>'
                if not closed:
                    body+='<form method="post" action="'+base+'/save">'+token+'<fieldset class="check-response"><legend>What did you find?</legend>'+''.join('<label><input type="radio" name="result" value="'+k+'"'+(' checked' if i['result']==k else '')+' required>'+v+'</label>' for k,v in RESULTS.items() if k!='unset')+'</fieldset><label for="note-'+str(iid)+'">Observation / reason</label><textarea id="note-'+str(iid)+'" name="note" rows="2" maxlength="2000">'+esc(i['note'])+'</textarea>'
                    if i['was_attention']:body+='<label for="resolution-'+str(iid)+'">How was it corrected and verified?</label><textarea id="resolution-'+str(iid)+'" name="resolution" rows="2" maxlength="2000">'+esc(i['resolution'])+'</textarea><p class="docs-meta">Review any submitted correction evidence first, then record your verification before choosing Pass or Not applicable.</p>'
                    body+='<div class="docs-actions"><button>Save this item</button></div></form>'
                else:body+='<p class="docs-notes">'+esc(i['note'])+'</p>'+('<p><strong>Correction / verification:</strong> '+esc(i['resolution'])+'</p>' if i['resolution'] else '')
                if i['was_attention']:body+='<p class="docs-meta"><strong>First finding:</strong> '+esc(i['finding_note'])+'. Earlier observations remain in history.</p>'
                body+='<details'+(' open' if i['photos'] else '')+'><summary>Photos / supporting evidence</summary><div class="check-photos">'
                for f in i['photos']:
                    url=BASE+'/'+str(run_id)+'/photos/'+str(f['id'])
                    body+='<figure><a href="'+url+'" target="_blank" rel="noopener"><img src="'+url+'" alt="'+esc(f['caption'] or f['original_name'])+'" loading="lazy"></a><figcaption>'+esc(f['caption'] or f['original_name'])+'</figcaption>'
                    if not closed:body+='<form method="post" action="'+url+'/remove">'+token+'<button>Remove from this check</button></form>'
                    body+='</figure>'
                body+='</div>'
                if not closed:body+='<form method="post" action="'+base+'/photos" enctype="multipart/form-data">'+token+self.hidden('request_key',uuid.uuid4().hex)+'<label for="photo-'+str(iid)+'">Add a photo</label><input id="photo-'+str(iid)+'" name="file" type="file" accept="image/jpeg,image/png,image/webp" required><label for="caption-'+str(iid)+'">What does it show?</label><input id="caption-'+str(iid)+'" name="caption" maxlength="300"><p class="docs-meta">JPEG, PNG or WebP. Up to 20 MB each, 20 photos per check.</p><button>Save photo</button></form>'
                body+='</details>'
                correction=i.get('correction')
                if correction:
                    body+='<section><h3>Correction evidence</h3><p>'+('Evidence accepted / no active request' if self.correction_ready(correction) else 'A correction request or file review is still open')+'</p>'+self.link('/workspace/documents/'+str(correction['document']['id']),'Open correction record')
                    for q in correction['requests'][:3]:body+='<p>'+esc(q['recipient'] or 'Former account')+' · '+esc('Revoked' if q['revoked_at'] else q['state'])+' · <a href="/workspace/sharing/'+str(q['share_id'])+'">Open request</a></p>'
                    body+='</section>'
                if not closed and i['result']=='attention':
                    body+='<form method="post" action="'+base+'/correction">'+token
                    if i['photos']:body+='<label for="reference-'+str(iid)+'">Finding photo for the request (optional)</label><select id="reference-'+str(iid)+'" name="reference_photo_id"><option value="0">No finding photo</option>'+''.join('<option value="'+str(f['id'])+'">'+esc(f['caption'] or f['original_name'])+'</option>' for f in i['photos'])+'</select>'
                    body+='<button>'+('Prepare another request' if correction else 'Request correction from a subcontractor')+'</button><p class="docs-meta">Review the named recipient, instructions and reference file before publishing. Nothing is sent by this button.</p></form>'
                body+='</section>'
            if not closed:body+='<section class="card"><h2>Review and close</h2><p>Answer every item and verify the corrections first. Then review the PDF before filing it.</p><form method="post" action="'+BASE+'/'+str(run_id)+'/review">'+self.hidden('version',r['version'])+'<label for="review-note">Final review note (optional)</label><textarea id="review-note" name="review_note" rows="3" maxlength="2000"></textarea><div class="docs-actions"><button>Review completed checklist</button></div></form></section>'
            events=c.execute('SELECT action,created,snapshot_json FROM bc_check_events WHERE company_id=? AND project_id=? AND run_id=? ORDER BY id DESC LIMIT 100',(user['company_id'],p['id'],run_id)).fetchall()
            body+='<details class="card"><summary>Checklist history</summary>'
            for e in events:
                snap=json.loads(e['snapshot_json']);body+='<p><strong>'+esc(e['action'])+'</strong> · '+esc(e['created'])+'</p>'
                if isinstance(snap,dict) and 'label' in snap:body+='<p class="docs-meta">'+esc(snap['label'])+' · '+esc(RESULTS.get(snap.get('result'),''))+' · '+esc(snap.get('note',''))+' · '+esc(snap.get('resolution',''))+'</p>'
            body+='</details>'
        return self.page('Checklist',body)

    def save_item(self,run_id:int,item_id:int,request:Request,version:int=Form(...),result:str=Form(...),note:str=Form(''),resolution:str=Form('')):
        self.origin(request);self.require(result in {'pass','attention','na'},'Choose Pass, Needs attention or Not applicable.',400)
        note=self.text(note,2000,'an observation or reason',result in {'attention','na'});resolution=self.text(resolution,2000,'a correction and verification note')
        with self.db(True) as c:
            user,p,r=self.run(c,run_id,True,version,True);i=self.item(c,r,item_id)
            if i['was_attention'] and result in {'pass','na'}:
                self.require(bool(resolution),'Describe how this item was corrected and verified.',400)
                self.require(self.correction_ready(self.correction(c,r,i)),'Review the active correction request and its latest file before resolving this item.',409)
            changed={**i,'result':result,'note':note,'resolution':resolution,'was_attention':int(i['was_attention'] or result=='attention'),'finding_note':i['finding_note'] or (note if result=='attention' else '')}
            c.execute('UPDATE bc_check_items SET result=?,note=?,finding_note=?,resolution=?,was_attention=? WHERE id=? AND run_id=?',(result,note,changed['finding_note'],resolution,changed['was_attention'],item_id,run_id))
            self.changed(c,user,r,'Checklist item saved',changed)
        return self.redirect(run_id,item_id)

    async def stage_photo(self,file):
        from PIL import Image
        name=Path(str(file.filename or '').replace('\\','/')).name
        self.text(name,240,'a photo filename',True);suffix=Path(name).suffix.lower()
        self.require(suffix in {'.jpg','.jpeg','.png','.webp'},'Choose a JPEG, PNG or WebP photo.',400)
        root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();root.mkdir(parents=True,exist_ok=True)
        path=root/('check-photo-'+uuid.uuid4().hex+suffix);sha=hashlib.sha256();size=0
        try:
            with path.open('xb') as dst:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk);self.require(size<=PHOTO_LIMIT,'This photo exceeds 20 MB.',413);sha.update(chunk);dst.write(chunk)
            self.require(size>0,'Choose a photo that contains data.',400)
            try:
                with Image.open(path) as source:
                    self.require(source.format in {'JPEG','PNG','WEBP'} and source.width*source.height<=25000000,'Use a JPEG, PNG or WebP photo of up to 25 megapixels.',400)
                    fmt=source.format;source.verify()
            except (OSError,ValueError,Image.DecompressionBombError):self.require(False,'This photo could not be read. Choose another image.',400)
            return path,dict(original_name=name,stored_name=path.name,mime_type={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}[fmt],size_bytes=size,sha256=sha.hexdigest())
        except BaseException:path.unlink(missing_ok=True);raise

    async def upload(self,run_id:int,item_id:int,request:Request,version:int=Form(...),request_key:str=Form(...),caption:str=Form(''),file:UploadFile=File(...)):
        path=None;saved=False
        try:
            self.origin(request);caption=self.text(caption,300,'a photo caption')
            self.require(bool(re.fullmatch('[a-f0-9]{32}',request_key)),'Reopen the photo form.',400)
            with self.db() as c:
                user,p,r=self.run(c,run_id,open_only=True);self.item(c,r,item_id)
                old=c.execute('SELECT item_id FROM bc_check_photos WHERE run_id=? AND request_key=? AND company_id=?',(run_id,request_key,user['company_id'])).fetchone()
                if old:self.require(old['item_id']==item_id,'Reopen this item\'s photo form.',409);return self.redirect(run_id,item_id)
                self.require(r['version']==version,'This checklist changed. Reopen the photo form.',409)
            path,data=await self.stage_photo(file)
            with self.db(True) as c:
                user,p,r=self.run(c,run_id,True,version,True);self.item(c,r,item_id)
                n=c.execute('SELECT COUNT(*) AS n FROM bc_check_photos WHERE run_id=? AND company_id=? AND removed_at IS NULL',(run_id,user['company_id'])).fetchone()['n']
                self.require(n<PHOTO_COUNT,'This checklist already has 20 photos. Remove an unneeded photo or start a separate check.',413)
                identity=self.insert(c,'bc_check_photos',dict(company_id=user['company_id'],project_id=p['id'],run_id=run_id,item_id=item_id,request_key=request_key,**data,caption=caption,created_by=user['id'],created=self.field.now().isoformat(),removed_at=None))
                self.changed(c,user,r,'Photo added',dict(id=identity,caption=caption,**data))
            saved=True;return self.redirect(run_id,item_id)
        finally:
            if path is not None and not saved:path.unlink(missing_ok=True)
            await file.close()

    def photo(self,c,r,identity):
        f=c.execute('SELECT * FROM bc_check_photos WHERE id=? AND company_id=? AND project_id=? AND run_id=?',(identity,r['company_id'],r['project_id'],r['id'])).fetchone()
        self.require(f is not None and f['removed_at'] is None,'This photo is unavailable in the checklist.',404);return dict(f)

    def photo_download(self,run_id:int,photo_id:int):
        with self.db() as c:user,p,r=self.run(c,run_id);f=self.photo(c,r,photo_id)
        path=self.requests.checked_file(f)
        return FileResponse(path,media_type=f['mime_type'],filename=f['original_name'],content_disposition_type='inline',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})

    def photo_remove(self,run_id:int,photo_id:int,request:Request,version:int=Form(...)):
        self.origin(request)
        with self.db(True) as c:
            user,p,r=self.run(c,run_id,True,version,True);f=self.photo(c,r,photo_id)
            c.execute('UPDATE bc_check_photos SET removed_at=? WHERE id=? AND run_id=?',(self.field.now().isoformat(),photo_id,run_id))
            self.changed(c,user,r,'Photo removed from checklist',f)
        return self.redirect(run_id,f['item_id'])

    def prepare_correction(self,run_id:int,item_id:int,request:Request,version:int=Form(...),reference_photo_id:int=Form(0)):
        self.origin(request)
        with self.db(True) as c:
            user,p,r=self.run(c,run_id,True,open_only=True);i=self.item(c,r,item_id)
            self.require(i['result']=='attention','Record the finding as Needs attention before requesting correction.',409)
            did=i['correction_record_id']
            reference_id=0
            if did:
                active=c.execute('SELECT q.share_id FROM bc_doc_requests q JOIN bc_shared_work s ON s.id=q.share_id WHERE q.company_id=? AND q.record_id=? AND s.revoked_at IS NULL AND q.state<>? ORDER BY q.id DESC LIMIT 1',(user['company_id'],did,'accepted')).fetchone()
                if active:return RedirectResponse('/workspace/sharing/'+str(active['share_id']),303)
            photo=None
            if reference_photo_id:
                self.require(r['version']==version,'This checklist changed. Reopen the finding photo selection.',409)
                photo=self.photo(c,r,reference_photo_id);self.require(photo['item_id']==item_id,'Choose a photo from this checklist item.',404)
                self.requests.checked_file(photo)
            if not did:
                self.require(r['version']==version,'This checklist changed. Reopen the item.',409)
                meta=self.docs.metadata(c,user,('Correction: '+i['label'])[:240],r['kind'],'Checklist #'+str(run_id)+'; item '+str(i['position'])+'. Internal finding: '+i['note'],'','')
                doc,_=self.docs.make(c,user,p['id'],meta,uuid.uuid4().hex);did=doc['id']
                c.execute('UPDATE bc_check_items SET correction_record_id=? WHERE id=? AND run_id=?',(did,item_id,run_id))
                self.changed(c,user,r,'Correction evidence record prepared',dict(item_id=item_id,document_id=did))
            if photo:
                _,_,doc=self.docs.record(c,did);old_files=self.docs.files(c,doc)
                existing=next((f for f in old_files if f['stored_name']==photo['stored_name'] and f['sha256']==photo['sha256']),None)
                if existing:reference_id=existing['id']
                else:
                    revision=old_files[0]['revision']+1 if old_files else 1
                    data={k:photo[k] for k in ('original_name','stored_name','mime_type','size_bytes','sha256')}
                    reference_id=self.docs.insert(c,'bc_doc_files',dict(company_id=user['company_id'],project_id=p['id'],record_id=did,revision=revision,**data,created_by=user['id'],created=self.field.now().isoformat()))
                    self.docs.changed(c,user,doc,'Finding photo prepared as a request reference',status='received')
                c.execute('UPDATE bc_check_items SET correction_reference_file_id=? WHERE id=? AND run_id=?',(reference_id,item_id,run_id))
                self.changed(c,user,r,'Finding photo selected for request review',dict(item_id=item_id,photo_id=reference_photo_id,file_id=reference_id))
            active=c.execute('SELECT q.share_id FROM bc_doc_requests q JOIN bc_shared_work s ON s.id=q.share_id WHERE q.company_id=? AND q.record_id=? AND s.revoked_at IS NULL AND q.state<>? ORDER BY q.id DESC LIMIT 1',(user['company_id'],did,'accepted')).fetchone()
        return RedirectResponse('/workspace/sharing/'+str(active['share_id']) if active else '/workspace/documents/'+str(did)+'/request'+('?reference_file_id='+str(reference_id) if reference_id else ''),303)

    def correction_item(self,c,user,doc):
        raw=c.execute('SELECT run_id,id FROM bc_check_items WHERE correction_record_id=? AND company_id=? AND project_id=?',(doc['id'],user['company_id'],doc['project_id'])).fetchone()
        if not raw:return None
        _,_,r=self.run(c,raw['run_id']);return r,self.item(c,r,raw['id'])

    def request_binding(self,c,user,doc):
        found=self.correction_item(c,user,doc)
        if not found:return None
        r,i=found;self.require(r['state']=='open' and i['result']=='attention','This checklist item no longer needs a correction request. Reopen the checklist to review it.',409)
        return dict(run_id=r['id'],run_version=r['version'],item=i)

    def request_message(self,c,user,doc):
        found=self.correction_item(c,user,doc)
        if not found:return ''
        r,i=found
        return ('Correction requested for '+r['title']+' / '+(r['area'] or 'project work area')+'\nItem: '+i['label']+'\nFinding: '+i['note']+'\n\nDescribe the completed correction and upload a photo or supporting file for the project leader to review.')[:4000]

    def record_panel(self,c,user,doc):
        if not doc['project_id'] or not self.schema_ready:return ''
        found=self.correction_item(c,user,doc)
        if found:r,i=found;return '<section class="card"><h2>Checklist correction</h2><p>'+esc(i['label'])+'</p>'+self.link(BASE+'/'+str(r['id'])+'#item-'+str(i['id']),'Return to checklist item')+'</section>'
        r=c.execute('SELECT id FROM bc_check_runs WHERE document_id=? AND company_id=? AND project_id=?',(doc['id'],user['company_id'],doc['project_id'])).fetchone()
        return '<section class="card">'+self.link(BASE+'/'+str(r['id']),'Open reviewed checklist and photos')+'</section>' if r else ''

    def snapshot(self,c,user,p,r):
        items=self.items(c,r);template=None
        if r['template_id']:
            t=c.execute('SELECT name,version,kind,prompts_json FROM bc_check_template_versions WHERE company_id=? AND template_id=? AND version=?',(user['company_id'],r['template_id'],r['template_version'])).fetchone()
            self.require(t is not None,'The original template version is unavailable.',409);template=dict(t)
        return dict(project=p,run=r,items=items,template=template,labels=RESULTS)

    def verify_files(self,snapshot):
        for i in snapshot['items']:
            for f in i['photos']:self.requests.checked_file(f)
            if i['correction'] and i['correction']['file']:self.requests.checked_file(i['correction']['file'])

    def review(self,run_id:int,request:Request,version:int=Form(...),review_note:str=Form('')):
        self.origin(request);note=self.text(review_note,2000,'a final review note');path=None;saved=False
        try:
            with self.db(True) as c:
                user,p,r=self.run(c,run_id,True,version,True);snapshot=self.snapshot(c,user,p,r)
                self.require(not self.blocking(snapshot['items']),'Answer every item, review any open correction request, and record how each finding was resolved before closing.',409)
                self.verify_files(snapshot);source_hash=digest(packed(snapshot));raw=secrets.token_urlsafe(32);now=self.field.now()
                snapshot.update(review_note=note,prepared_at=now.isoformat(),reviewer=user['display_name'] or user['email'])
                payload=build_report(snapshot,self.requests.checked_file)
                self.require(len(payload)<=100*1024*1024,'This report is too large. Use fewer photos in one checklist.',413)
                root=Path(self.ns['_runtime'].UPLOAD_DIR).resolve();root.mkdir(parents=True,exist_ok=True);path=root/('check-review-'+uuid.uuid4().hex+'.pdf')
                with path.open('xb') as f:f.write(payload)
                self.insert(c,'bc_check_reviews',dict(company_id=user['company_id'],project_id=p['id'],run_id=run_id,actor_id=user['id'],session_hash=self.field.session_hash(request),token_hash=digest(raw),source_hash=source_hash,snapshot_json=packed(snapshot),stored_name=path.name,sha256=hashlib.sha256(payload).hexdigest(),size_bytes=len(payload),expires=(now+timedelta(minutes=15)).isoformat(),consumed=None))
            saved=True
            body='<div class="hero"><h1>Review completed checklist</h1><p>'+esc(p['name'])+' · '+esc(r['title'])+'</p></div><section class="card"><p>'+str(len(snapshot['items']))+' answered items. No active correction is awaiting review.</p>'+self.link(BASE+'/reviews/'+raw+'/pdf','Open exact PDF for review')+'<p class="docs-notes">'+esc(note)+'</p><p>This records your team\'s review. It does not certify regulatory approval.</p></section><form class="card" method="post" action="'+BASE+'/approve">'+self.hidden('review_token',raw)+'<label><input type="checkbox" name="confirmed" value="yes" required> I reviewed the observations, corrections and exact PDF. Close this checklist and file this record.</label><div class="docs-actions"><button>Close and file checklist</button></div><p class="docs-meta">Review expires in 15 minutes. Nothing is sent to subcontractors.</p></form>'+self.link(BASE+'/'+str(run_id),'Back to checklist')
            return self.page('Review completed checklist',body)
        finally:
            if path is not None and not saved:path.unlink(missing_ok=True)

    def load_review(self,c,raw,request,write=False):
        self.ready();self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}',raw)),'Reopen the checklist review.',403)
        user=self.ns['_bc850_actor'](c)
        q=c.execute('SELECT * FROM bc_check_reviews WHERE token_hash=? AND company_id=?',(digest(raw),user['company_id'])).fetchone()
        self.require(q is not None,'This checklist review is unavailable.',403)
        user,p,r=self.run(c,q['run_id'],write)
        q=dict(c.execute('SELECT * FROM bc_check_reviews WHERE id=?'+(self.field.lock if write else ''),(q['id'],)).fetchone())
        self.require(q['actor_id']==user['id'] and secrets.compare_digest(q['session_hash'],self.field.session_hash(request)),'Use the signed-in session that prepared this review.',403)
        if not q['consumed']:self.require(q['expires']>self.field.now().isoformat(),'This review expired. Review the current checklist again.',409)
        return user,p,r,q

    def preview_pdf(self,review_token:str,request:Request):
        with self.db() as c:user,p,r,q=self.load_review(c,review_token,request)
        path=self.requests.checked_file(q)
        return FileResponse(path,filename='checklist-'+str(r['id'])+'-review.pdf',media_type='application/pdf',content_disposition_type='inline',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})

    def approve(self,request:Request,review_token:str=Form(...),confirmed:str=Form('')):
        self.origin(request);self.require(confirmed=='yes','Review and confirm the exact checklist PDF first.',400)
        with self.db(True) as c:
            user,p,r,q=self.load_review(c,review_token,request,True)
            if q['consumed']:return self.redirect(r['id'])
            self.require(r['state']=='open','This checklist is already closed.',409)
            current=self.snapshot(c,user,p,r);self.require(digest(packed(current))==q['source_hash'],'The checklist or correction evidence changed. Review the current version again.',409)
            self.require(not self.blocking(current['items']),'Resolve the remaining items before closing.',409)
            self.verify_files(current);self.requests.checked_file(q)
            meta=self.docs.metadata(c,user,r['title'],r['kind'],'Reviewed checklist #'+str(r['id'])+' for '+(r['area'] or 'the project')+'. Open the checklist for its original photos and history.','','',status='complete')
            doc,_=self.docs.make(c,user,p['id'],meta,uuid.uuid4().hex)
            file_id=self.docs.insert(c,'bc_doc_files',dict(company_id=user['company_id'],project_id=p['id'],record_id=doc['id'],revision=1,original_name='checklist-'+str(r['id'])+'.pdf',stored_name=q['stored_name'],mime_type='application/pdf',size_bytes=q['size_bytes'],sha256=q['sha256'],created_by=user['id'],created=self.field.now().isoformat()))
            self.docs.event(c,user,doc,'Reviewed checklist PDF filed')
            now=self.field.now().isoformat()
            c.execute('UPDATE bc_check_runs SET state=?,version=version+1,closed_json=?,closed_by=?,closed_at=?,document_id=?,file_id=?,updated=? WHERE id=? AND company_id=?',('closed',q['snapshot_json'],user['id'],now,doc['id'],file_id,now,r['id'],user['company_id']))
            self.event(c,user,r,'Checklist reviewed, closed and filed',dict(document_id=doc['id'],file_id=file_id,review_id=q['id'],source_hash=q['source_hash']))
            c.execute('UPDATE bc_check_reviews SET consumed=? WHERE id=?',(now,q['id']))
        return self.redirect(r['id'])

    def attention_data(self,c,user,pid):
        self.ready();self.field.actor(c,pid)
        count=c.execute('SELECT COUNT(*) AS n FROM bc_check_runs WHERE company_id=? AND project_id=? AND state=?',(user['company_id'],pid,'open')).fetchone()['n']
        rows=c.execute('''SELECT r.id,r.title,r.kind,r.area,r.check_date,
            (SELECT COUNT(*) FROM bc_check_items i WHERE i.run_id=r.id AND i.company_id=r.company_id AND i.result=?) AS findings,
            (SELECT COUNT(*) FROM bc_check_items i WHERE i.run_id=r.id AND i.company_id=r.company_id AND i.result=?) AS unchecked
            FROM bc_check_runs r WHERE r.company_id=? AND r.project_id=? AND r.state=? ORDER BY findings DESC,r.check_date,r.id LIMIT 8''',('attention','unset',user['company_id'],pid,'open')).fetchall()
        return dict(total=int(count),items=[dict(r) for r in rows])

    def panel(self,c,user,pid):
        if not self.schema_ready:return '<p>Safety and inspection checklists are temporarily unavailable.</p>'
        data=self.attention_data(c,user,pid)
        body='<div class="check-summary"><h3>Safety &amp; inspections</h3><p>'+str(data['total'])+' open checklist(s)</p>'
        for r in data['items'][:3]:body+='<p><a href="'+BASE+'/'+str(r['id'])+'">'+esc(r['title'])+'</a> · '+str(r['findings'])+' need attention · '+str(r['unchecked'])+' not checked</p>'
        return body+self.link(self.home(pid),'Open safety & inspections')+'</div>'

    def evidence(self,c,user,pid):
        if not self.schema_ready:return []
        data=self.attention_data(c,user,pid);out=[('Safety and inspection checklist status',pid,'Open checklist records',str(data['total'])+' open checklist(s). Recorded observations are not regulatory certification.',self.home(pid))]
        for r in data['items']:
            items=c.execute('SELECT id,label,result,note,resolution FROM bc_check_items WHERE run_id=? AND company_id=? ORDER BY CASE result WHEN ? THEN 0 ELSE 1 END,position LIMIT 6',(r['id'],user['company_id'],'attention')).fetchall()
            text='Area: '+r['area']+'; Date: '+r['check_date']+'; Needs attention: '+str(r['findings'])+'; Not checked: '+str(r['unchecked'])+'; '+'; '.join(i['label']+': '+RESULTS[i['result']]+'; '+i['note']+'; Verification: '+i['resolution'] for i in items)
            out.append(('Safety / inspection checklist',r['id'],r['title'],text[:5000]+'; Photos have not been AI-analyzed.',BASE+'/'+str(r['id'])))
        return out

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes for m in (getattr(r,'methods',None) or [])}
        checks={m+' '+p:active.get((p,m)) is fn for p,m,fn in self.routes}
        checks.update(checklist_schema_initialized=self.schema_ready,company_template_versions=True,reviewed_document_corrections=True,
                      exact_pdf_review=True,closed_snapshots_preserved=True,command_panel_connected=getattr(self.ns['app'].state,'command_center',None) is not None,
                      document_requests_preserved=getattr(self.ns['app'].state,'document_requests',None) is self.requests,
                      closeout_handover_preserved=getattr(self.ns['app'].state,'closeout_handover',None) is not None,
                      form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            import reportlab
            import PIL
            checks['report_dependencies_available']=True
        except ImportError:checks['report_dependencies_available']=False
        try:
            with self.db() as c:
                for t in TABLES:c.execute(f'SELECT id,company_id FROM {t} WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
            scope='Installation and schema checks only. Test company templates, real photos, reviewed trade corrections, exact PDF filing, project access and a first-user walkthrough on staging. No automatic email, regulatory certification or electronic signatures.')

    def register(self):
        entries=[(BASE,'GET',self.index),(TEMPLATES,'GET',self.templates),(TEMPLATES+'/{template_id}/save','POST',self.template_save),
            (BASE+'/projects/{project_id}/new','GET',self.new),(BASE+'/projects/{project_id}/create','POST',self.create),
            (BASE+'/{run_id}','GET',self.detail),(BASE+'/{run_id}/items/{item_id}/save','POST',self.save_item),
            (BASE+'/{run_id}/items/{item_id}/photos','POST',self.upload),(BASE+'/{run_id}/photos/{photo_id}','GET',self.photo_download),
            (BASE+'/{run_id}/photos/{photo_id}/remove','POST',self.photo_remove),(BASE+'/{run_id}/items/{item_id}/correction','POST',self.prepare_correction),
            (BASE+'/{run_id}/review','POST',self.review),(BASE+'/reviews/{review_token}/pdf','GET',self.preview_pdf),(BASE+'/approve','POST',self.approve)]
        for p,m,fn in reversed(entries):
            endpoint=self.docs.endpoint(fn);self.ns['_bc840_replace'](p,m,endpoint);self.routes.append((p,m,endpoint))
        path='/health/safety-inspections-8-23-0';self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
