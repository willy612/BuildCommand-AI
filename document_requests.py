"""8.21.0: exact reviewed document requests, trade submissions and filing.

Uses existing shares for recipient/project revocation. Request and submission
files are immutable; internal document notes and other file versions stay private.
"""
import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from blueprint_field import esc, digest, packed
from project_documents import BASE, ALLOWED

VERSION='8.21.0'
RELEASE='Document Requests by Trade'
STATES={'requested':'Requested','submitted':'Submitted','changes':'Needs changes','accepted':'Accepted'}
TABLES={'bc_doc_requests','bc_doc_submissions','bc_doc_request_reviews'}


def install(ns):
    service=DocumentRequests(ns);ns['app'].state.document_requests=service
    ns['_BC850_SOURCES']['document_request']=('Document request','bc_doc_requests','title','due_date')
    service.register();return service


class DocumentRequests:
    def __init__(self,ns):
        self.ns=ns;self.docs=ns['app'].state.project_documents;self.field=self.docs.field
        self.db,self.require=self.docs.db,self.docs.require;self.routes=[]
        self.schema_ready=self.initialize()

    def initialize(self):
        key='BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        try:
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_requests(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,record_id BIGINT NOT NULL,share_id BIGINT UNIQUE,title TEXT NOT NULL,
                    due_date TEXT NOT NULL,reference_file_id BIGINT,state TEXT NOT NULL,version INTEGER NOT NULL,
                    latest_submission_id BIGINT,created_by BIGINT NOT NULL,created TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_submissions(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,request_id BIGINT NOT NULL,share_id BIGINT NOT NULL,share_version INTEGER NOT NULL,
                    update_id BIGINT NOT NULL UNIQUE,actor_id BIGINT NOT NULL,submission_key TEXT NOT NULL,
                    original_name TEXT NOT NULL,stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes BIGINT NOT NULL,
                    sha256 TEXT NOT NULL,message TEXT NOT NULL,created TEXT NOT NULL,decision TEXT NOT NULL,
                    review_message TEXT NOT NULL,reviewed_by BIGINT,reviewed_at TEXT,filed_file_id BIGINT,
                    UNIQUE(company_id,request_id,actor_id,submission_key))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_doc_request_reviews(id {key},company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,actor_id BIGINT NOT NULL,session_hash TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,payload_json TEXT NOT NULL,expires TEXT NOT NULL,consumed TEXT,result_share_id BIGINT)''')
                for table in ('bc_doc_requests','bc_doc_submissions'):
                    c.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_project ON {table}(company_id,project_id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_doc_requests_record ON bc_doc_requests(company_id,record_id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_doc_request_reviews_expiry ON bc_doc_request_reviews(expires)')
            return True
        except Exception:
            import logging
            logging.getLogger('buildcommand.document_requests').exception('Document request setup failed')
            return False

    def ready(self):self.require(self.schema_ready,'Document requests are unavailable. Ask your administrator to check this installation.',503)

    def insert(self,c,table,values):
        self.require(table in TABLES,'Unsupported request record.',400)
        sql=f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:return int(c.execute(sql+' RETURNING id',tuple(values.values())).fetchone()['id'])
        return int(c.execute(sql,tuple(values.values())).lastrowid)

    def redirect(self,sid,manager=True):return RedirectResponse(('/workspace/sharing/' if manager else '/workspace/shared/')+str(sid),303)
    def hidden(self,name,value):return self.docs.hidden(name,value)
    def text(self,value,limit,label,required=False):return self.docs.text(value,limit,label,required)
    def origin(self,request):self.ready();self.docs.origin(request)

    def context(self,c,sid,manager=False,write=False,version=None):
        self.ready();user=self.ns['_bc850_actor'](c)
        share=self.ns['_bc850_share'](c,user,sid,manager,write)
        self.require(share['kind']=='document_request','Open a document request.',404)
        row=c.execute('''SELECT r.* FROM bc_doc_requests r JOIN bc_doc_records d
            ON d.id=r.record_id AND d.company_id=r.company_id AND d.project_id=r.project_id
            WHERE r.id=? AND r.share_id=? AND r.company_id=? AND r.project_id=?''',
            (share['source_id'],sid,user['company_id'],share['project_id'])).fetchone()
        self.require(row is not None,'This document request is unavailable.',404)
        row=dict(row)
        if version is not None:self.require(version==row['version'],'This request changed. Reopen it before continuing.',409)
        if write:self.require(not share['revoked_at'] and share['state']=='OPEN','This request is closed or its access was revoked.',409)
        return user,share,row

    def checked_file(self,f):
        path=self.docs.file_path(f)
        with path.open('rb') as source:actual=hashlib.file_digest(source,'sha256').hexdigest()
        self.require(actual==f['sha256'],'The stored file changed. Reopen the record and check its file version.',409)
        return path

    def reference(self,c,row):
        if not row.get('reference_file_id'):return None
        f=c.execute('SELECT * FROM bc_doc_files WHERE id=? AND record_id=? AND company_id=? AND project_id=?',
                    (row['reference_file_id'],row['record_id'],row['company_id'],row['project_id'])).fetchone()
        self.require(f is not None,'The included reference file is unavailable.',404)
        return dict(f)

    def submissions(self,c,row):
        return [dict(r) for r in c.execute('SELECT * FROM bc_doc_submissions WHERE company_id=? AND project_id=? AND request_id=? AND share_id=? ORDER BY id DESC',
                                          (row['company_id'],row['project_id'],row['id'],row['share_id'])).fetchall()]

    def recipients(self,c,user,pid):
        table=self.ns['_bc850_member_table']()
        rows=c.execute(f'SELECT DISTINCT u.id,u.email,u.display_name,u.role FROM users u JOIN {table} m ON m.user_id=u.id WHERE u.company_id=? AND m.project_id=? ORDER BY u.email',(user['company_id'],pid)).fetchall()
        return [dict(r) for r in rows if self.ns['_bc840_tier'](dict(r))=='trade']

    def issue_binding(self,c,record_id,data):
        user,p,doc=self.docs.record(c,record_id,False,data['document_version'])
        self.require(p['id']>0,'Use a project document record, not a company template.',400)
        recipient=self.ns['_bc850_recipient'](c,user,p['id'],data['recipient_id'])
        reference=next((f for f in self.docs.files(c,doc) if f['id']==data['reference_file_id']),None)
        self.require(not data['reference_file_id'] or reference is not None,'Choose a file version from this record.',400)
        if reference:self.checked_file(reference)
        active=c.execute("SELECT r.id FROM bc_doc_requests r JOIN bc_shared_work s ON s.id=r.share_id WHERE r.company_id=? AND r.record_id=? AND s.revoked_at IS NULL AND r.state<>?",(user['company_id'],record_id,'accepted')).fetchone()
        self.require(active is None,'This record already has an active request. Open that request, or use a separate record for another trade.',409)
        binding=dict(project=p,recipient=recipient,reference=reference,document_version=doc['version'])
        checklists=getattr(self.ns['app'].state,'safety_checklists',None)
        context=checklists.request_binding(c,user,doc) if checklists else None
        if context is not None:binding['checklist_context']=context
        return user,p,doc,binding

    def token(self,c,user,pid,kind,data,request):
        raw=secrets.token_urlsafe(32);now=self.field.now()
        self.insert(c,'bc_doc_request_reviews',dict(company_id=user['company_id'],project_id=pid,actor_id=user['id'],session_hash=self.field.session_hash(request),token_hash=digest(raw),kind=kind,
            payload_json=packed(data),expires=(now+timedelta(minutes=15)).isoformat(),consumed=None,result_share_id=None))
        return raw

    def load_review(self,c,raw,request):
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}',raw)),'Reopen the review before approving.',403)
        user=self.ns['_bc850_actor'](c)
        row=c.execute('SELECT * FROM bc_doc_request_reviews WHERE token_hash=? AND company_id=?',(digest(raw),user['company_id'])).fetchone()
        self.require(row is not None,'This review is unavailable.',403)
        user,_=self.field.actor(c,row['project_id'],True)
        row=dict(c.execute('SELECT * FROM bc_doc_request_reviews WHERE id=?'+self.field.lock,(row['id'],)).fetchone())
        self.require(row['actor_id']==user['id'] and secrets.compare_digest(row['session_hash'],self.field.session_hash(request)),'Approve from the same signed-in session that reviewed this action.',403)
        self.require(row['expires']>self.field.now().isoformat(),'This review expired. Reopen the current request and review again.',409)
        return user,row,json.loads(row['payload_json'])

    def approval_form(self,raw,label,confirmation):
        return '<form class="card" method="post" action="/workspace/document-requests/approve">'+self.hidden('review_token',raw)+'<p><label><input type="checkbox" name="confirmed" value="yes" required> '+esc(confirmation)+'</label></p><button>'+esc(label)+'</button><p class="docs-meta">This preview expires in 15 minutes.</p></form>'

    def prepare(self,record_id:int,reference_file_id:int=0):
        self.ready()
        with self.db() as c:
            user,p,doc=self.docs.record(c,record_id);self.require(p['id']>0,'Choose a project document.',400)
            recipients=self.recipients(c,user,p['id']);files=self.docs.files(c,doc)
            self.require(reference_file_id==0 or any(f['id']==reference_file_id for f in files),'Choose a reference file from this document record.',404)
            checklists=getattr(self.ns['app'].state,'safety_checklists',None)
            suggested_message=checklists.request_message(c,user,doc) if checklists else ''
        body='<div class="hero"><h1>Request paperwork</h1><p>'+esc(p['name'])+' · '+esc(doc['title'])+'</p></div>'
        if not recipients:return self.docs.page('Request paperwork',body+'<section class="card"><p>Assign a subcontractor to this project first.</p>'+self.docs.link(f'/workspace/sharing/projects/{p["id"]}/team','Manage project subcontractors')+'</section>')
        body+='<form class="card docs-form" method="post" action="'+BASE+'/'+str(record_id)+'/request/review">'+self.hidden('document_version',doc['version'])
        body+='<label for="request-recipient">Request from</label><select id="request-recipient" name="recipient_id" required><option value="">Choose a subcontractor</option>'+''.join('<option value="'+str(u['id'])+'">'+esc(u['display_name'] or u['email'])+' · '+esc(u['email'])+'</option>' for u in recipients)+'</select>'
        body+=self.docs.input('title','Request name',doc['title'],extra='maxlength="240" required')+self.docs.input('due_date','Needed by',doc['due_date'],'date')
        body+='<label for="request-message">What should they provide?</label><textarea id="request-message" name="message" rows="5" maxlength="4000" required placeholder="Upload the signed warranty for your completed electrical work."></textarea><label for="request-reference">Include a reference file (optional)</label><select id="request-reference" name="reference_file_id"><option value="0">No file included</option>'+''.join('<option value="'+str(f['id'])+'">'+esc(f['original_name'])+' · version '+str(f['revision'])+'</option>' for f in files)+'</select><p class="docs-meta">They will see only the reviewed request and any entire file version selected here. Internal notes and other versions stay private. Use a separate document record for each trade deliverable.</p><button>Review request</button></form>'+self.docs.link(BASE+'/'+str(record_id),'Back to document')
        if suggested_message:body=body.replace('placeholder="Upload the signed warranty for your completed electrical work."></textarea>','placeholder="Upload the signed warranty for your completed electrical work.">'+esc(suggested_message)+'</textarea>')
        if reference_file_id:
            # Limit selection to the reference-file select: user and file IDs can overlap.
            marker='<select id="request-reference" name="reference_file_id">'
            before,after=body.split(marker,1)
            after=after.replace('<option value="'+str(reference_file_id)+'">','<option value="'+str(reference_file_id)+'" selected>',1)
            body=before+marker+after
        return self.docs.page('Request paperwork',body)

    def review_issue(self,record_id:int,request:Request,document_version:int=Form(...),recipient_id:int=Form(...),title:str=Form(...),message:str=Form(...),due_date:str=Form(''),reference_file_id:int=Form(0)):
        self.origin(request)
        title=self.text(title,240,'a request name',True);message=self.text(message,4000,'instructions',True)
        with self.db(True) as c:
            user,p,doc=self.docs.record(c,record_id,True,document_version)
            self.docs.metadata(c,user,title,doc['folder_key'],'','',due_date)
            data=dict(record_id=record_id,document_version=document_version,recipient_id=recipient_id,title=title,message=message,due_date=due_date,reference_file_id=reference_file_id)
            user,p,doc,binding=self.issue_binding(c,record_id,data);data['binding']=binding
            raw=self.token(c,user,p['id'],'issue',data,request)
        recipient=binding['recipient'];f=binding['reference']
        body='<div class="hero"><h1>Review document request</h1><p>'+esc(p['name'])+'</p></div><section class="card"><p><strong>To:</strong> '+esc(recipient['display_name'] or recipient['email'])+' · '+esc(recipient['email'])+'</p><h2>'+esc(title)+'</h2><p class="docs-notes">'+esc(message)+'</p><p>Needed by: '+esc(due_date or 'No date set')+'</p>'
        body+=('<p>Include entire file: '+esc(f['original_name'])+' · version '+str(f['revision'])+'</p>'+self.docs.link(f'{BASE}/{record_id}/files/{f["id"]}?preview=1','Review included file')) if f else '<p>No reference file included.</p>'
        body+='</section>'+self.approval_form(raw,'Publish request','I reviewed the recipient, instructions and any included file. Publish this request in their workspace.')+self.docs.link(f'{BASE}/{record_id}/request','Edit request')
        return self.docs.page('Review document request',body)

    def approve(self,request:Request,review_token:str=Form(...),confirmed:str=Form('')):
        self.origin(request);self.require(confirmed=='yes','Review and confirm this exact action first.',400)
        with self.db(True) as c:
            user,review,data=self.load_review(c,review_token,request)
            if review['consumed']:return self.redirect(review['result_share_id'])
            if review['kind']=='issue':
                user,p,doc,binding=self.issue_binding(c,data['record_id'],data)
                self.require(binding==data['binding'],'The recipient, document or included file changed. Review again.',409)
                now=self.field.now().isoformat()
                rid=self.insert(c,'bc_doc_requests',dict(company_id=user['company_id'],project_id=p['id'],record_id=doc['id'],share_id=None,title=data['title'],due_date=data['due_date'],reference_file_id=data['reference_file_id'] or None,state='requested',version=1,latest_submission_id=None,created_by=user['id'],created=now))
                values=dict(company_id=user['company_id'],project_id=p['id'],kind='document_request',source_id=rid,recipient_user_id=data['recipient_id'],title=data['title'],message=data['message'],due_date=data['due_date'],allow_response=1,created_by=user['id'],created_at=now,updated_at=now)
                sid=self.ns['_bc850_insert'](c,'bc_shared_work',','.join(values),tuple(values.values()))
                c.execute('UPDATE bc_doc_requests SET share_id=? WHERE id=?',(sid,rid))
                self.docs.event(c,user,doc,'Document request published #'+str(rid))
                self.ns['_bc850_event'](c,user,p['id'],sid,'DOCUMENT_REQUEST_PUBLISHED')
            elif review['kind']=='decision':
                sid=self.apply_decision(c,user,data)
            else:self.require(False,'This review type is unavailable.',400)
            c.execute('UPDATE bc_doc_request_reviews SET consumed=?,result_share_id=? WHERE id=?',(self.field.now().isoformat(),sid,review['id']))
        return self.redirect(sid)

    def render(self,c,user,share,manager=False):
        user,share,row=self.context(c,share['id'],manager)
        subs=self.submissions(c,row);f=self.reference(c,row)
        prefix=('/workspace/sharing/' if manager else '/workspace/shared/')+str(share['id'])
        status='Access revoked' if share['revoked_at'] else STATES[row['state']]
        body='<div class="hero"><div class="eyebrow">DOCUMENT REQUEST</div><h1>'+esc(share['title'])+'</h1><span class="docs-pill">'+status+'</span><p>Needed by: '+esc(share['due_date'] or 'No date set')+'</p></div><section class="card"><h2>What to provide</h2><p class="docs-notes">'+esc(share['message'])+'</p>'
        if manager:
            recipient=c.execute('SELECT display_name,email FROM users WHERE id=? AND company_id=?',(share['recipient_user_id'],share['company_id'])).fetchone()
            body+='<p>Requested from: '+esc((recipient['display_name'] or recipient['email']) if recipient else 'Former team member')+'</p>'+self.docs.link(BASE+'/'+str(row['record_id']),'Open internal document record')
        if f and not share['revoked_at']:
            body+='<h3>Included reference file</h3><p>'+esc(f['original_name'])+' · version '+str(f['revision'])+'</p>'+self.docs.link(prefix+'/request-reference?preview=1','Open / download reference')
        body+='</section>'
        if not manager and row['state'] in {'requested','changes'} and share['state']=='OPEN':
            body+='<form class="card docs-form" method="post" action="'+prefix+'/document-submit" enctype="multipart/form-data"><h2>Submit your document</h2>'+self.hidden('version',row['version'])+self.hidden('submission_key',uuid.uuid4().hex)+'<label for="request-file">File</label><input id="request-file" name="file" type="file" required accept="'+','.join(sorted(ALLOWED))+'"><label for="submission-message">Note to the superintendent (optional)</label><textarea id="submission-message" name="message" rows="3" maxlength="4000"></textarea><p class="docs-meta">Up to 100 MB. Submit one file for this deliverable. Your superintendent reviews it before filing.</p><button>Submit for review</button></form>'
        elif not manager:
            body+='<section class="card"><h2>'+('Document accepted' if row['state']=='accepted' else 'Waiting for review' if row['state']=='submitted' else 'Responses are closed')+'</h2><p>'+('Your submission was accepted and filed.' if row['state']=='accepted' else 'Your project leader will review the submitted file and respond here.')+'</p></section>'
        body+='<section class="card"><h2>Submissions &amp; review</h2>'
        for sub in subs:
            body+='<article class="docs-row"><h3>'+esc(sub['original_name'])+'</h3><p class="docs-meta">Submitted '+esc(sub['created'])+' · '+esc(STATES.get(sub['decision'],'Awaiting review'))+'</p><p class="docs-notes">'+esc(sub['message'])+'</p>'
            if not share['revoked_at']:body+=self.docs.link(prefix+'/document-submissions/'+str(sub['id'])+'?preview=1','Open / download submission')
            if sub['review_message']:body+='<p><strong>Project leader reply</strong></p><p class="docs-notes">'+esc(sub['review_message'])+'</p>'
            if manager and row['state']=='submitted' and row['latest_submission_id']==sub['id'] and not share['revoked_at'] and share['state']=='OPEN':
                body+='<form class="docs-form" method="post" action="'+prefix+'/document-review">'+self.hidden('version',row['version'])+self.hidden('submission_id',sub['id'])+'<label for="review-decision">Decision</label><select id="review-decision" name="decision"><option value="accept">Accept and file</option><option value="changes">Request changes</option></select><label for="review-note">Reply to subcontractor</label><textarea id="review-note" name="message" rows="3" maxlength="2000" placeholder="Explain what needs changing, or confirm acceptance."></textarea><button>Review decision</button></form>'
            body+='</article>'
        if not subs:body+='<p>No document submitted yet.</p>'
        body+='</section>'
        if manager and not share['revoked_at']:
            body+='<details class="card"><summary>Request access controls</summary><form method="post" action="/workspace/sharing/'+str(share['id'])+'/control">'+self.hidden('version',share['version'])+'<p>Revoking removes the subcontractor’s access to this request and its files. Accepted project files remain filed.</p><button name="action" value="revoke">Revoke request access</button></form></details>'
        body+=self.docs.link('/workspace/document-requests?project_id='+str(share['project_id']) if manager else '/workspace/shared','Back to requests' if manager else 'Back to my shared work')
        return self.docs.page('Document request',body)

    async def submit(self,share_id:int,request:Request,version:int=Form(...),submission_key:str=Form(...),message:str=Form(''),file:UploadFile=File(...)):
        path=None;saved=False
        try:
            self.origin(request);message=self.text(message,4000,'a submission note')
            self.require(bool(re.fullmatch(r'[a-f0-9]{32}',submission_key)),'Reopen this request before submitting.',400)
            with self.db() as c:
                user,share,row=self.context(c,share_id)
                existing=c.execute('SELECT id FROM bc_doc_submissions WHERE company_id=? AND request_id=? AND actor_id=? AND submission_key=?',(user['company_id'],row['id'],user['id'],submission_key)).fetchone()
                if existing:return self.redirect(share_id,False)
                self.require(row['version']==version and row['state'] in {'requested','changes'} and share['state']=='OPEN','This request changed or a submission is awaiting review. Reopen it.',409)
            path,data=await self.docs.stage(file)
            with self.db(True) as c:
                user,share,row=self.context(c,share_id,False,True,version)
                self.require(row['state'] in {'requested','changes'},'Your latest submission is already awaiting review.',409)
                now=self.field.now().isoformat()
                update=self.ns['_bc850_insert'](c,'bc_shared_work_updates','share_id,actor_user_id,share_version,status,message,created_at',
                    (share_id,user['id'],share['version'],'READY_FOR_REVIEW','Document submitted: '+data['original_name']+('\n'+message if message else ''),now))
                sid=self.insert(c,'bc_doc_submissions',dict(company_id=user['company_id'],project_id=share['project_id'],request_id=row['id'],share_id=share_id,share_version=share['version'],update_id=update,actor_id=user['id'],submission_key=submission_key,**data,message=message,created=now,decision='',review_message='',reviewed_by=None,reviewed_at=None,filed_file_id=None))
                c.execute('UPDATE bc_doc_requests SET state=?,version=version+1,latest_submission_id=? WHERE id=?',('submitted',sid,row['id']))
                c.execute('UPDATE bc_shared_work SET updated_at=? WHERE id=?',(now,share_id))
                self.ns['_bc850_event'](c,user,share['project_id'],share_id,'DOCUMENT_SUBMITTED:'+str(sid))
            saved=True
            return self.redirect(share_id,False)
        finally:
            if path is not None and not saved:path.unlink(missing_ok=True)
            await file.close()

    def decision_binding(self,c,sid,version,submission_id):
        user,share,row=self.context(c,sid,True,False,version)
        self.require(not share['revoked_at'] and share['state']=='OPEN' and row['state']=='submitted' and row['latest_submission_id']==submission_id,'This submission is no longer awaiting review. Reopen the request.',409)
        sub=next((s for s in self.submissions(c,row) if s['id']==submission_id),None)
        self.require(sub is not None and not sub['decision'],'Choose the current unreviewed submission.',409)
        self.checked_file(sub)
        _,p,doc=self.docs.record(c,row['record_id'])
        recipient=self.ns['_bc850_recipient'](c,user,share['project_id'],share['recipient_user_id'])
        return user,share,row,sub,doc,dict(request_version=row['version'],share_version=share['version'],share_state=share['state'],submission=sub,document_version=doc['version'],recipient=recipient,project=p)

    def review_decision(self,share_id:int,request:Request,version:int=Form(...),submission_id:int=Form(...),decision:str=Form(...),message:str=Form('')):
        self.origin(request);self.require(decision in {'accept','changes'},'Choose Accept and file or Request changes.',400)
        message=self.text(message,2000,'your reply',decision=='changes')
        with self.db(True) as c:
            self.context(c,share_id,True,True,version)
            user,share,row,sub,doc,binding=self.decision_binding(c,share_id,version,submission_id)
            data=dict(share_id=share_id,version=version,submission_id=submission_id,decision=decision,message=message,binding=binding)
            raw=self.token(c,user,share['project_id'],'decision',data,request)
        body='<div class="hero"><h1>'+('Accept and file this document?' if decision=='accept' else 'Request a revised document?')+'</h1><p>'+esc(share['title'])+'</p></div><section class="card"><h2>'+esc(sub['original_name'])+'</h2>'+self.docs.link(f'/workspace/sharing/{share_id}/document-submissions/{submission_id}?preview=1','Review exact submission')+'<p>Submitted by: '+esc(binding['recipient']['email'])+'</p><p class="docs-notes">'+esc(sub['message'])+'</p><h3>Your reply</h3><p class="docs-notes">'+esc(message or 'Accepted and filed.')+'</p>'
        if decision=='accept':body+='<p>File as a new version of <strong>'+esc(doc['title'])+'</strong>. Earlier project files remain available. Filing does not sign a contract, approve a submittal or certify an inspection.</p>'
        body+='</section>'+self.approval_form(raw,'Accept and file' if decision=='accept' else 'Request changes','I reviewed this exact file and reply. Apply the decision shown above.')+self.docs.link('/workspace/sharing/'+str(share_id),'Back to request')
        return self.docs.page('Review document decision',body)

    def apply_decision(self,c,user,data):
        sid=data['share_id'];self.context(c,sid,True,True,data['version'])
        user,share,row,sub,doc,binding=self.decision_binding(c,sid,data['version'],data['submission_id'])
        self.require(binding==data['binding'],'The submission, recipient or document record changed. Review the current version again.',409)
        now=self.field.now().isoformat();file_id=None
        if data['decision']=='accept':
            old=self.docs.files(c,doc);revision=(old[0]['revision'] if old else 0)+1
            content={k:sub[k] for k in ('original_name','stored_name','mime_type','size_bytes','sha256')}
            file_id=self.docs.insert(c,'bc_doc_files',dict(company_id=user['company_id'],project_id=share['project_id'],record_id=doc['id'],revision=revision,**content,created_by=user['id'],created=now))
            self.docs.changed(c,user,doc,'Accepted trade submission #'+str(sub['id'])+' as file version '+str(revision),status='complete')
            state='accepted';reply=data['message'] or 'Accepted and filed.'
            c.execute("UPDATE bc_shared_work SET state='CLOSED',updated_at=? WHERE id=?",(now,sid))
        else:
            state='changes';reply=data['message']
            c.execute('UPDATE bc_shared_work SET updated_at=? WHERE id=?',(now,sid))
        c.execute('UPDATE bc_doc_submissions SET decision=?,review_message=?,reviewed_by=?,reviewed_at=?,filed_file_id=? WHERE id=?',(state,reply,user['id'],now,file_id,sub['id']))
        c.execute('UPDATE bc_doc_requests SET state=?,version=version+1 WHERE id=?',(state,row['id']))
        c.execute('UPDATE bc_shared_work_updates SET reviewed_by=?,reviewed_at=?,review_message=? WHERE id=? AND share_id=?',(user['id'],now,reply,sub['update_id'],sid))
        self.ns['_bc850_event'](c,user,share['project_id'],sid,'DOCUMENT_'+state.upper()+':'+str(sub['id']))
        return sid

    def file_response(self,sid,manager,submission_id=None,preview=0):
        with self.db() as c:
            user,share,row=self.context(c,sid,manager)
            self.require(not share['revoked_at'],'File access was revoked.',403)
            f=next((s for s in self.submissions(c,row) if s['id']==submission_id),None) if submission_id is not None else self.reference(c,row)
            self.require(f is not None,'This file is unavailable on this request.',404)
        path=self.checked_file(f);inline=preview==1 and f['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}
        return FileResponse(path,filename=f['original_name'],media_type=f['mime_type'],content_disposition_type='inline' if inline else 'attachment',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'SAMEORIGIN'})

    def trade_reference(self,share_id:int,preview:int=0):return self.file_response(share_id,False,preview=preview)
    def leader_reference(self,share_id:int,preview:int=0):return self.file_response(share_id,True,preview=preview)
    def trade_file(self,share_id:int,submission_id:int,preview:int=0):return self.file_response(share_id,False,submission_id,preview)
    def leader_file(self,share_id:int,submission_id:int,preview:int=0):return self.file_response(share_id,True,submission_id,preview)

    def record_panel(self,c,user,doc):
        if not doc['project_id']:return ''
        self.ready();self.field.actor(c,doc['project_id'])
        rows=c.execute('SELECT r.state,r.share_id,s.revoked_at,s.title,u.email FROM bc_doc_requests r JOIN bc_shared_work s ON s.id=r.share_id LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id WHERE r.company_id=? AND r.project_id=? AND r.record_id=? ORDER BY r.id DESC LIMIT 50',(user['company_id'],doc['project_id'],doc['id'])).fetchall()
        body='<section class="card"><h2>Requests to subcontractors</h2><div class="docs-actions">'+self.docs.link(f'{BASE}/{doc["id"]}/request','Request this document')+'</div>'
        for r in rows:body+='<p>'+self.docs.link('/workspace/sharing/'+str(r['share_id']),r['title'])+' · '+esc(r['email'] or 'Former team member')+' · '+('Revoked' if r['revoked_at'] else STATES[r['state']])+'</p>'
        return body+'</section>'

    def index(self,project_id:int=0,state:str=''):
        self.ready();self.require(state in {'',*STATES},'Choose a listed request status.',400)
        with self.db() as c:
            user,p,projects=self.docs.hub.user(c,project_id,True)
            body='<div class="hero"><h1>Document requests</h1><p>Paperwork requested from your subcontractors, ready to review and file.</p></div>'+self.docs.hub.selector(projects,p,'/workspace/document-requests')
            if p:
                body+='<div class="docs-actions">'+self.docs.link(BASE+'?project_id='+str(p['id']),'Open project documents')+'</div><form method="get">'+self.hidden('project_id',p['id'])+'<label for="request-state">Status </label><select id="request-state" name="state"><option value="">All requests</option>'+''.join('<option value="'+k+'"'+(' selected' if state==k else '')+'>'+v+'</option>' for k,v in STATES.items())+'</select> <button>Show</button></form><section class="card">'
                sql='SELECT r.*,s.revoked_at,u.email FROM bc_doc_requests r JOIN bc_shared_work s ON s.id=r.share_id LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id WHERE r.company_id=? AND r.project_id=?';args=[user['company_id'],p['id']]
                if state:sql+=' AND r.state=? AND s.revoked_at IS NULL';args.append(state)
                rows=c.execute(sql+' ORDER BY r.id DESC LIMIT 100',args).fetchall()
                for r in rows:body+='<article class="docs-row"><h2>'+self.docs.link('/workspace/sharing/'+str(r['share_id']),r['title'])+'</h2><p>'+esc(r['email'] or 'Former team member')+' · '+('Revoked' if r['revoked_at'] else STATES[r['state']])+' · '+esc(r['due_date'] or 'No due date')+'</p></article>'
                body+=('<p>No matching requests. Open a document record and choose Request this document.</p>' if not rows else '<p class="docs-meta">Showing the latest 100 matching requests.</p>')+'</section>'
        return self.docs.page('Document requests',body)

    def existing(self,request_id,pid):
        with self.db() as c:
            user,_=self.field.actor(c,pid)
            row=c.execute('SELECT share_id FROM bc_doc_requests WHERE id=? AND company_id=? AND project_id=?',(request_id,user['company_id'],pid)).fetchone()
            self.require(row is not None,'This document request is unavailable.',404)
        return self.redirect(row['share_id'])

    def command_card(self,row):
        status='Submitted' if row.get('pending_status') else 'Accepted' if row['state']=='CLOSED' else 'Needs changes' if row.get('latest_status')=='IN_PROGRESS' else 'Requested'
        return '<article class="bc860-panel"><span class="bc860-tag">Document request · '+status+'</span><h3>'+esc(row['title'])+'</h3><p>'+esc(row['recipient_name'])+' · '+esc(row['due_date'] or 'No due date')+'</p>'+self.docs.link('/workspace/sharing/'+str(row['id']),'Review document request')+'</article>'

    def evidence(self,c,user,pid):
        if not self.schema_ready:return []
        self.field.actor(c,pid)
        rows=c.execute('SELECT r.id,r.title,r.state,r.due_date,r.share_id,s.revoked_at,u.email,d.original_name,d.message,d.review_message FROM bc_doc_requests r JOIN bc_shared_work s ON s.id=r.share_id LEFT JOIN users u ON u.id=s.recipient_user_id AND u.company_id=s.company_id LEFT JOIN bc_doc_submissions d ON d.id=r.latest_submission_id AND d.request_id=r.id AND d.company_id=r.company_id WHERE r.company_id=? AND r.project_id=? ORDER BY r.id DESC LIMIT 20',(user['company_id'],pid)).fetchall()
        return [('Document request status',r['id'],r['title'],'Status: '+('Revoked' if r['revoked_at'] else STATES[r['state']])+'; Assigned to: '+str(r['email'] or 'Former team member')+'; Due: '+r['due_date']+'; Latest submission: '+str(r['original_name'] or 'None')+'; Submission note: '+str(r['message'] or '')+'; Project leader reply: '+str(r['review_message'] or '')+'; File contents have not been analyzed.','/workspace/sharing/'+str(r['share_id'])) for r in rows]

    def health(self):
        active={(r.path,m):r.endpoint for r in self.ns['app'].routes for m in (getattr(r,'methods',None) or [])}
        checks={m+' '+p:active.get((p,m)) is fn for p,m,fn in self.routes}
        checks.update(request_schema_initialized=self.schema_ready,existing_trade_access_used=True,reviewed_publication=True,reviewed_acceptance=True,
            accepted_files_versioned=True,project_documents_preserved=getattr(self.ns['app'].state,'project_documents',None) is self.docs,
            form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                for t in sorted(TABLES):c.execute(f'SELECT id,company_id FROM {t} WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'degraded',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,
            scope='Installation and schema checks only. Test reviewed requests, assigned trade uploads, requested changes, exact file acceptance, retries, revocation and document history on staging. No email, signatures or compliance certification.')

    def register(self):
        entries=[('/workspace/document-requests','GET',self.index),(BASE+'/{record_id}/request','GET',self.prepare),(BASE+'/{record_id}/request/review','POST',self.review_issue),
            ('/workspace/document-requests/approve','POST',self.approve),('/workspace/shared/{share_id}/document-submit','POST',self.submit),
            ('/workspace/sharing/{share_id}/document-review','POST',self.review_decision),('/workspace/shared/{share_id}/request-reference','GET',self.trade_reference),
            ('/workspace/sharing/{share_id}/request-reference','GET',self.leader_reference),('/workspace/shared/{share_id}/document-submissions/{submission_id}','GET',self.trade_file),
            ('/workspace/sharing/{share_id}/document-submissions/{submission_id}','GET',self.leader_file)]
        for p,m,fn in reversed(entries):
            endpoint=self.docs.endpoint(fn);self.ns['_bc840_replace'](p,m,endpoint);self.routes.append((p,m,endpoint))
        path='/health/document-requests-8-21-0';self.ns['app'].add_api_route(path,self.health,methods=['GET']);self.ns['_runtime'].PUBLIC_PATHS.add(path)
