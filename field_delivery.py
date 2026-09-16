"""Exact document packages and trade receipts; reuses existing share revocation."""
import io
import json
import re
import secrets
import tempfile
import zipfile
from datetime import timedelta
from functools import wraps
from pathlib import Path
from fastapi import Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from blueprint_field import esc
from command_center import command_json

BASE = '/workspace/transmittals'
MAX_BYTES = 200*1024*1024


class FieldDelivery:
    def __init__(self, core):
        self.b = core
        self.pilot_delivery_version = "8.25.0"
        self.ns, self.docs = core.ns, core.docs
        self.requests = core.app.state.document_requests
        core.schema('bc824_packages', '''title TEXT NOT NULL,message TEXT NOT NULL,purpose TEXT NOT NULL,
            due_date TEXT NOT NULL,expires TEXT NOT NULL,recipient_id BIGINT NOT NULL,share_id BIGINT,
            snapshot_json TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL,
            opened_at TEXT,acknowledged_at TEXT,acknowledgment TEXT NOT NULL''')
        core.schema('bc824_contacts', '''sub_id BIGINT NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,email TEXT NOT NULL,
            phone TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL''')
        core.schema('bc824_submittal_reviews', '''record_id BIGINT NOT NULL,file_id BIGINT NOT NULL,decision TEXT NOT NULL,
            response TEXT NOT NULL,reviewer_id BIGINT NOT NULL,created TEXT NOT NULL''')
        core.schema('bc824_mail', '''package_id BIGINT NOT NULL,recipient_id BIGINT NOT NULL,email TEXT NOT NULL,
            subject TEXT NOT NULL,body TEXT NOT NULL,send_after TEXT NOT NULL,state TEXT NOT NULL,attempted_at TEXT,
            sent_at TEXT,error_code TEXT NOT NULL,message_key TEXT NOT NULL UNIQUE''')
        core.action('issue_package', self.binding, self.apply)
        core.action('submittal_response', self.response_binding, self.apply_response)
        core.action('package_reminder', self.reminder_binding, self.apply_reminder)
        for path,method,fn in [
            (BASE,'GET',self.index),(BASE+'/projects/{project_id}/new','GET',self.new),
            (BASE+'/projects/{project_id}/review','POST',self.review),
            (BASE+'/{package_id}','GET',self.detail),(BASE+'/{package_id}/files/{file_id}','GET',self.file),
            (BASE+'/{package_id}/download','GET',self.download),(BASE+'/{package_id}/acknowledge','POST',self.acknowledge),
            (BASE+'/{package_id}/reminder','POST',self.reminder),
            ('/workspace/submittals','GET',self.submittals),
            ('/workspace/submittals/{record_id}/response','POST',self.response_review),
            ('/workspace/directory/projects/{project_id}/subs/{sub_id}/contacts','GET',self.contacts),
            ('/workspace/directory/projects/{project_id}/subs/{sub_id}/contacts/{contact_id}','POST',self.save_contact),
            ('/workspace/inbox','GET',self.inbox)]:core.route(path,method,fn)
        self.install_sharing()

    def install_sharing(self):
        b=self.b;ns=self.ns
        ns['_BC850_SOURCES']['field_package']=('Reviewed package','bc824_packages','title','due_date')
        original_source=ns['_bc850_source']
        def source(c,user,pid,kind,source_id=None,query=''):
            if kind!='field_package':return original_source(c,user,pid,kind,source_id,query)
            sql='SELECT id,title,due_date FROM bc824_packages WHERE company_id=? AND project_id=?'
            args=[user['company_id'],pid]
            if source_id is not None:sql+=' AND id=?';args.append(source_id)
            elif query:sql+=' AND LOWER(title) LIKE LOWER(?)';args.append('%'+query[:120]+'%')
            rows=[dict(r) for r in c.execute(sql+' ORDER BY id DESC LIMIT 100',args).fetchall()]
            if source_id is not None:
                b.require(bool(rows),'This package is unavailable.',404)
                return rows[0]
            return rows
        ns['_bc850_source']=source
        original_detail=ns['_bc850_detail']
        def detail(sid,manager):
            with b.db() as c:
                user=b.user(c);share=ns['_bc850_share'](c,user,sid,manager)
                if share['kind']=='field_package':return self.detail(share['source_id'])
            return original_detail(sid,manager)
        ns['_bc850_detail']=detail
        original_publish=ns['bc850_publish']
        @wraps(original_publish)
        def publish(*args,**kwargs):
            kind=kwargs.get('kind',args[1] if len(args)>1 else None)
            b.require(kind!='field_package','Review the complete package before issuing it.',400)
            return original_publish(*args,**kwargs)
        ns['bc850_publish']=ns['_bc850_endpoint'](publish)
        ns['_bc840_replace']('/workspace/sharing/publish','POST',ns['bc850_publish'])
        original_prepare=ns['bc850_prepare_share']
        @wraps(original_prepare)
        def prepare(*args,**kwargs):
            kind=kwargs.get('kind',args[1] if len(args)>1 else None)
            if kind=='field_package':
                pid=kwargs.get('project_id',args[0] if args else 0)
                with b.db() as c:b.actor(c,pid)
                return RedirectResponse(BASE+'/projects/'+str(pid)+'/new',303)
            return original_prepare(*args,**kwargs)
        ns['bc850_prepare_share']=ns['_bc850_endpoint'](prepare)
        ns['_bc840_replace']('/workspace/sharing/new','GET',ns['bc850_prepare_share'])

    def choices(self,c,user,pid):
        return [dict(r) for r in c.execute('''SELECT f.*,d.title,d.folder_key,d.version AS record_version,d.status
            FROM bc_doc_files f JOIN bc_doc_records d ON d.id=f.record_id AND d.company_id=f.company_id
            AND d.project_id=f.project_id WHERE f.company_id=? AND f.project_id=? AND NOT EXISTS
            (SELECT 1 FROM bc_doc_files newer WHERE newer.record_id=f.record_id AND newer.company_id=f.company_id AND newer.revision>f.revision)
            ORDER BY d.title,d.id LIMIT 300''',(user['company_id'],pid)).fetchall()]

    def binding(self,c,user,p,data):
        b=self.b
        recipient=self.ns['_bc850_recipient'](c,user,p['id'],data['recipient_id'])
        ids=data['file_ids'];b.require(len(ids)<=50 and len(ids)==len(set(ids)),'Choose up to 50 distinct file versions.',400)
        choices={r['id']:r for r in self.choices(c,user,p['id'])}
        files=[]
        for fid in ids:
            b.require(fid in choices,'A selected file is no longer current. Review the current version.',409)
            f=choices[fid];self.requests.checked_file(f);files.append(f)
        b.require(sum(f['size_bytes'] for f in files)<=MAX_BYTES,'Use a package with no more than 200 MB of files.',413)
        b.require(data['purpose'] in {'submittal','documents','lookahead','closeout'},'Choose a package purpose.',400)
        b.require(data['expires']>b.now().isoformat(),'The package expiry has passed. Review again.',409)
        extra={}
        if data.get('readiness_snapshot'):
            extra=b.app.state.field_readiness.package_binding(c,user,p,data['readiness_snapshot'])
        return dict(project=p,recipient=recipient,files=files,extra=extra)

    def new(self,project_id:int,purpose:str='documents'):
        b=self.b
        b.require(purpose in {'submittal','documents','lookahead','closeout'},'Choose a package purpose.',400)
        with b.db() as c:
            user,p=b.actor(c,project_id);files=self.choices(c,user,project_id)
            recipients=self.requests.recipients(c,user,project_id)
        body='<div class="hero"><h1>Prepare a document package</h1><p>'+esc(p['name'])+'</p></div>'
        body+='<form class="card field-form" method="post" action="'+BASE+'/projects/'+str(project_id)+'/review"><label for="package-purpose">Package purpose</label><select id="package-purpose" name="purpose">'+''.join('<option value="'+k+'"'+(' selected' if k==purpose else '')+'>'+v+'</option>' for k,v in [('documents','Project documents'),('submittal','Submittal issue'),('closeout','Closeout delivery'),('lookahead','Trade look-ahead')])+'</select>'
        body+=b.input('title','Package name',extra='maxlength="240" required')+b.area('message','Instructions to the recipient')
        body+='<label for="package-recipient">Appointed subcontractor</label><select id="package-recipient" name="recipient_id" required><option value="">Choose the recipient</option>'+''.join('<option value="'+str(r['id'])+'">'+esc(r['display_name'] or r['email'])+' · '+esc(r['email'])+'</option>' for r in recipients)+'</select>'
        body+=b.input('due_date','Response needed by','','date')+b.input('valid_days','Access expires in days','30','number','min="1" max="365" required')
        body+='<h2>Choose exact files</h2><p>Only checked files are included. Each selection shares that entire file. No files are selected automatically.</p>'
        for f in files:
            body+='<label><input type="checkbox" name="file_ids" value="'+str(f['id'])+'"> '+esc(f['title'])+' · '+esc(f['original_name'])+' · version '+str(f['revision'])+'</label>'
        if not files:body+='<p>Add files to Documents first, or issue instructions without a file.</p>'
        body+='<details><summary>Email notification</summary><label><input type="checkbox" name="notify_email" value="yes"> Queue an email with the reviewed title and sign-in link to this recipient.</label><p class="field-muted">Email requires the configured delivery worker. It does not attach files or grant access to a new account.</p></details><button>Review package</button></form>'+b.link(BASE+'?project_id='+str(project_id),'Back to packages')
        return b.page('Prepare package',body)

    def preview_body(self,binding,data):
        b=self.b;recipient=binding['recipient']
        body='<section class="card"><h2>'+esc(data['title'])+'</h2><p><strong>To:</strong> '+esc(recipient['display_name'] or recipient['email'])+' · '+esc(recipient['email'])+'</p><p class="field-exact">'+esc(data['message'])+'</p><p>Response due: '+esc(data['due_date'] or 'No date')+' · Access expires: '+esc(data['expires'][:10])+'</p>'
        for f in binding['files']:
            body+='<p>'+b.link('/workspace/documents/'+str(f['record_id'])+'/files/'+str(f['id'])+'?preview=1','Open '+f['original_name']+' · version '+str(f['revision']))+'</p>'
        if not binding['files']:body+='<p>No files included.</p>'
        body+='<p>'+('An email notification will be queued for the recipient above.' if data['notify_email'] else 'Published in the app. No email will be queued.')+'</p></section>'
        return body

    def review(self,project_id:int,request:Request,title:str=Form(...),message:str=Form(...),recipient_id:int=Form(...),
               purpose:str=Form('submittal'),due_date:str=Form(''),valid_days:int=Form(30),file_ids:list[int]=Form(default=[]),notify_email:str=Form('')):
        b=self.b;b.origin(request);b.require(1<=valid_days<=365,'Choose 1 to 365 days of access.',400)
        data=dict(title=b.text(title,240,'a package title',True),message=b.text(message,6000,'instructions',True),recipient_id=recipient_id,
            purpose=purpose,due_date=b.day(due_date),expires=(b.now()+timedelta(days=valid_days)).isoformat(),file_ids=file_ids,notify_email=notify_email=='yes')
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);binding=self.binding(c,user,p,data)
            return b.prepare(c,user,p,request,'issue_package',data,'Review exact package',self.preview_body(binding,data),BASE+'/projects/'+str(project_id)+'/new')

    def apply(self,c,user,p,data):
        b=self.b;binding=self.binding(c,user,p,data);now=b.now().isoformat()
        # Only selected, reviewable fields enter the recipient snapshot.
        files=[{k:f[k] for k in ('id','record_id','original_name','revision','stored_name','mime_type','size_bytes','sha256','title')} for f in binding['files']]
        snapshot=dict(files=files,recipient=binding['recipient'],project_name=p['name'])
        rid=b.insert(c,'bc824_packages',dict(company_id=user['company_id'],project_id=p['id'],title=data['title'],message=data['message'],purpose=data['purpose'],due_date=data['due_date'],expires=data['expires'],recipient_id=data['recipient_id'],share_id=None,snapshot_json=command_json(snapshot),created_by=user['id'],created=now,opened_at=None,acknowledged_at=None,acknowledgment=''))
        values=dict(company_id=user['company_id'],project_id=p['id'],kind='field_package',source_id=rid,recipient_user_id=data['recipient_id'],title=data['title'],message=data['message'],due_date=data['due_date'],allow_response=0,created_by=user['id'],created_at=now,updated_at=now)
        sid=self.ns['_bc850_insert'](c,'bc_shared_work',','.join(values),tuple(values.values()))
        c.execute('UPDATE bc824_packages SET share_id=? WHERE id=?',(sid,rid))
        self.ns['_bc850_event'](c,user,p['id'],sid,'REVIEWED_PACKAGE_PUBLISHED')
        if data['notify_email']:self.queue(c,user,p,rid,binding['recipient'],data['title'],now)
        return BASE+'/'+str(rid)

    def context(self,c,rid,write=False):
        b=self.b;user=b.user(c)
        row=c.execute('SELECT * FROM bc824_packages WHERE id=? AND company_id=?',(rid,user['company_id'])).fetchone()
        b.require(row is not None,'This package is unavailable.',403);row=dict(row)
        manager=self.ns['_bc850_manager'](user)
        share=self.ns['_bc850_share'](c,user,row['share_id'],manager,write)
        b.require(share['kind']=='field_package' and share['source_id']==rid and share['recipient_user_id']==row['recipient_id'],'This package is unavailable.',403)
        snapshot=json.loads(row['snapshot_json'])
        if not manager:
            b.require(row['expires']>b.now().isoformat(),'Package access has expired. Ask your superintendent for a current issue.',403)
            b.require(user['email']==snapshot['recipient']['email'],'This package needs a new recipient review.',403)
        return user,row,share,snapshot,manager

    def index(self,project_id:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Document packages',BASE)
            if not p:return b.page('Document packages',body+'<p>Choose a project.</p>')
            rows=c.execute('SELECT q.*,s.revoked_at FROM bc824_packages q JOIN bc_shared_work s ON s.id=q.share_id WHERE q.company_id=? AND q.project_id=? ORDER BY q.id DESC LIMIT 100',(user['company_id'],p['id'])).fetchall()
            body+='<div class="field-actions">'+b.link(BASE+'/projects/'+str(p['id'])+'/new','Prepare package')+b.link('/workspace/submittals?project_id='+str(p['id']),'Submittal reviews')+'</div>'
            for r in rows:
                label='Access revoked' if r['revoked_at'] else 'Access expired' if r['expires']<=b.now().isoformat() else 'Acknowledged' if r['acknowledged_at'] else 'Opened' if r['opened_at'] else 'Issued in app'
                body+='<section class="card"><span class="field-pill">'+label+'</span><h2>'+esc(r['title'])+'</h2><p>Due '+esc(r['due_date'] or 'not set')+'</p>'+b.link(BASE+'/'+str(r['id']),'Open package')+'</section>'
            if not rows:body+='<section class="card"><h2>No packages issued yet</h2><p>Choose exact document versions and review who will receive them.</p></section>'
        return b.page('Document packages',body)

    def detail(self,package_id:int):
        b=self.b
        with b.db(True) as c:
            user,row,share,snapshot,manager=self.context(c,package_id,True)
            if not manager and not row['opened_at']:
                row['opened_at']=b.now().isoformat();c.execute('UPDATE bc824_packages SET opened_at=? WHERE id=?',(row['opened_at'],package_id))
                b.event(c,user,row['project_id'],'Package opened',package_id,dict(share_id=share['id']))
            body='<div class="hero"><h1>'+esc(row['title'])+'</h1><p>'+esc(snapshot['project_name'])+'</p></div><section class="card"><p class="field-exact">'+esc(row['message'])+'</p><p>Needed by: '+esc(row['due_date'] or 'No date')+' · Access expires: '+esc(row['expires'][:10])+'</p>'
            if share['revoked_at']:body+='<p class="field-warning">Trade access was revoked.</p>'
            for f in snapshot['files']:body+='<div class="field-row"><strong>'+esc(f['title'])+'</strong><p>'+esc(f['original_name'])+' · version '+str(f['revision'])+'</p>'+b.link(BASE+'/'+str(package_id)+'/files/'+str(f['id'])+'?preview=1','Open file')+' '+b.link(BASE+'/'+str(package_id)+'/files/'+str(f['id']),'Download')+'</div>'
            if snapshot['files']:body+=b.link(BASE+'/'+str(package_id)+'/download','Download reviewed package ZIP')
            body+='</section><section class="card"><h2>Receipt</h2><p>Issued: '+esc(row['created'])+'<br>Opened in app: '+esc(row['opened_at'] or 'Not recorded')+'<br>Acknowledged: '+esc(row['acknowledged_at'] or 'Not yet')+'</p><p class="field-exact">'+esc(row['acknowledgment'])+'</p><p class="field-muted">Opening a page is not an acknowledgment or approval of the work.</p></section>'
            if row['purpose']=='lookahead':
                body+='<section class="card"><h2>Trade readiness</h2>'
                replies=c.execute('SELECT * FROM bc824_readiness_replies WHERE company_id=? AND project_id=? AND package_id=? ORDER BY id DESC LIMIT 10',(user['company_id'],row['project_id'],package_id)).fetchall()
                for reply in replies:body+='<p>'+esc(reply['created'])+' · Crew: '+esc(reply['crew'].replace('_',' '))+' · Materials: '+esc(reply['materials'].replace('_',' '))+' · Planned start: '+esc(reply['start_date'] or 'Not confirmed')+'</p><p class="field-exact">'+esc(reply['note'])+'</p>'
                if not replies:body+='<p>No readiness confirmation yet.</p>'
                if not manager and share['state']=='OPEN':
                    body+='<form class="field-form" method="post" action="'+BASE+'/'+str(package_id)+'/readiness">'
                    for key,label in [('crew','Crew ready?'),('materials','Materials ready?')]:body+='<label for="ready-'+key+'">'+label+'</label><select id="ready-'+key+'" name="'+key+'"><option value="unknown">Need to confirm</option><option value="ready">Ready</option><option value="not_ready">Not ready</option></select>'
                    body+=b.input('start_date','Confirmed start date','','date')+b.area('note','What needs coordination?',maximum=2000)+'<button>Send readiness update</button></form>'
                body+='</section>'
            if not manager and not row['acknowledged_at'] and share['state']=='OPEN':body+='<form class="card field-form" method="post" action="'+BASE+'/'+str(package_id)+'/acknowledge">'+b.area('message','Reply / readiness note (optional)',maximum=2000)+'<label><input type="checkbox" name="confirmed" value="yes" required> I received this exact package.</label><button>Acknowledge receipt</button></form>'
            if manager:
                body+='<section class="card"><h2>Email notification</h2>'
                from pilot_readiness import STATUS
                jobs=c.execute('SELECT id,state,send_after,sent_at FROM bc824_mail WHERE package_id=? AND company_id=? ORDER BY id DESC LIMIT 20',(package_id,user['company_id'])).fetchall()
                if not jobs:body+='<p>No email notification queued. This package is available in the app.</p>'
                for job in jobs:
                    label,note=STATUS.get(job['state'],('Needs review','Open the notification to check its state.'))
                    body+='<p><strong>'+esc(label)+'</strong> · '+esc(job['sent_at'] or job['send_after'])+'</p><p>'+esc(note)+'</p>'+b.link('/workspace/delivery/'+str(job['id']),'Review notification')
                body+='<p class="field-muted">Mail acceptance, opening and acknowledgment are separate events.</p>'+b.link('/workspace/delivery?project_id='+str(row['project_id']),'Delivery & receipts')+'</section><form class="card field-form" method="post" action="'+BASE+'/'+str(package_id)+'/reminder">'+b.input('send_date','Reminder date',b.now().date().isoformat(),'date','required')+'<button>Review email reminder</button></form>'
                body+=b.link(BASE+'?project_id='+str(row['project_id']),'Back to packages')
                body+='<form class="card" method="post" action="/workspace/sharing/'+str(share['id'])+'/control">'+b.hidden('version',share['version'])+b.hidden('action','revoke')+'<button>Revoke trade access</button></form>'
            else:body+=b.link('/workspace/inbox','Back to my work')
        return b.page('Reviewed package',body)

    def acknowledge(self,package_id:int,request:Request,confirmed:str=Form(''),message:str=Form('')):
        b=self.b;b.origin(request);b.require(confirmed=='yes','Confirm receipt first.',400);message=b.text(message,2000)
        with b.db(True) as c:
            user,row,share,snapshot,manager=self.context(c,package_id,True)
            b.require(not manager,'Only the named recipient can acknowledge this package.',403)
            if not row['acknowledged_at']:
                b.require(share['state']=='OPEN','Responses are closed for this package.',409)
                c.execute('UPDATE bc824_packages SET acknowledged_at=?,acknowledgment=? WHERE id=?',(b.now().isoformat(),message,package_id))
                b.event(c,user,row['project_id'],'Package acknowledged',package_id,dict(message=message))
        return RedirectResponse(BASE+'/'+str(package_id),303)

    def file(self,package_id:int,file_id:int,preview:int=0):
        b=self.b
        with b.db() as c:
            _,row,share,snapshot,manager=self.context(c,package_id)
            f=next((r for r in snapshot['files'] if r['id']==file_id),None);b.require(f is not None,'This file was not included.',404)
            path=self.requests.checked_file(f)
        inline=preview and f['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}
        return FileResponse(path,media_type=f['mime_type'],filename=f['original_name'],content_disposition_type='inline' if inline else 'attachment',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

    def download(self,package_id:int):
        b=self.b
        with b.db() as c:
            _,row,share,snapshot,manager=self.context(c,package_id)
            files=[(f,self.requests.checked_file(f)) for f in snapshot['files']]
            b.require(sum(f['size_bytes'] for f,_ in files)<=MAX_BYTES,'Package size exceeds this download limit.',413)
            # Disk-backed export avoids holding every PDF in memory per request.
            handle=tempfile.NamedTemporaryFile(prefix='bc-package-',suffix='.zip',delete=False);archive=Path(handle.name);handle.close()
            try:
              with zipfile.ZipFile(archive,'w',zipfile.ZIP_STORED) as z:
                index='<html><meta charset="utf-8"><title>'+esc(row['title'])+'</title><h1>BuildCommand AI</h1><h2>'+esc(row['title'])+'</h2><p>'+esc(row['message'])+'</p><ul>'
                manifest=[]
                for f,path in files:
                    safe=re.sub(r'[^A-Za-z0-9._-]+','_',f['original_name'])[:130] or 'file'
                    name='Files/'+str(f['id'])+'-v'+str(f['revision'])+'-'+safe
                    z.write(path,name);index+='<li><a href="'+name+'">'+esc(f['original_name'])+' — version '+str(f['revision'])+'</a></li>'
                    manifest.append({k:f[k] for k in ('id','record_id','original_name','revision','sha256')})
                z.writestr('Start-here.html',index+'</ul><p>Built By Willy LaHood ©2026</p></html>')
                z.writestr('Manifest.json',command_json(dict(package_id=package_id,files=manifest)))
            except BaseException:
                archive.unlink(missing_ok=True);raise
        return FileResponse(archive,filename='package-'+str(package_id)+'.zip',media_type='application/zip',headers={'Cache-Control':'private, no-store'},background=BackgroundTask(archive.unlink,missing_ok=True))

    def queue(self,c,user,p,rid,recipient,title,when):
        b=self.b
        return b.insert(c,'bc824_mail',dict(company_id=user['company_id'],project_id=p['id'],package_id=rid,recipient_id=recipient['id'],email=recipient['email'],subject=' '.join(('BuildCommand AI: '+title).split())[:240],body='A reviewed package is available in BuildCommand AI. Sign in with this email address to open it.',send_after=when,state='queued',attempted_at=None,sent_at=None,error_code='',message_key=secrets.token_hex(24)))

    def reminder_binding(self,c,user,p,data):
        _,row,share,snapshot,manager=self.context(c,data['package_id'])
        self.b.require(manager and row['project_id']==p['id'],'This package is unavailable.',403)
        self.b.require(not share['revoked_at'] and row['expires']>self.b.now().isoformat() and not row['acknowledged_at'],'This package does not need an outstanding-receipt reminder.',409)
        pending=c.execute("SELECT id FROM bc824_mail WHERE package_id=? AND company_id=? AND state IN ('queued','sending','uncertain') LIMIT 1",(row['id'],user['company_id'])).fetchone()
        self.b.require(not pending,'An email is already pending or its delivery is unconfirmed. Open Delivery & receipts before preparing another reminder.',409)
        self.b.require(share['state']=='OPEN','This package is closed. Review its current state before preparing a reminder.',409)
        recipient=self.ns['_bc850_recipient'](c,user,p['id'],row['recipient_id'])
        self.b.require(recipient['email']==snapshot['recipient']['email'],'The recipient changed. Issue a new reviewed package.',409)
        return dict(row=row,share=share,recipient=recipient)

    def apply_reminder(self,c,user,p,data):
        bind=self.reminder_binding(c,user,p,data)
        self.queue(c,user,p,data['package_id'],bind['recipient'],'Reminder: '+bind['row']['title'],data['send_after'])
        return BASE+'/'+str(data['package_id'])

    def reminder(self,package_id:int,request:Request,send_date:str=Form(...)):
        b=self.b;b.origin(request);day=b.day(send_date,True);b.require(day>=b.now().date().isoformat(),'Choose today or a future date.',400)
        data=dict(package_id=package_id,send_after=day+'T00:00:00+00:00')
        with b.db(True) as c:
            user,row,share,snapshot,manager=self.context(c,package_id,True);b.require(manager,'Only project leaders prepare reminders.',403)
            user,p=b.actor(c,row['project_id'],True);bind=self.reminder_binding(c,user,p,data)
            b.require(data['send_after']<row['expires'],'Choose a date before access expires.',400)
            body='<section class="card"><p>To: '+esc(bind['recipient']['email'])+'</p><p>Subject: '+esc('BuildCommand AI: Reminder: '+row['title'])+'</p><p>A reviewed package is available in BuildCommand AI. Sign in with this email address to open it.</p><p>Queue date (UTC): '+esc(day)+'</p><p>Skipped if access is revoked, expired or already acknowledged before processing. The configured worker delivers the notification.</p></section>'
            return b.prepare(c,user,p,request,'package_reminder',data,'Review reminder',body,BASE+'/'+str(package_id))

    def response_binding(self,c,user,p,data):
        _,project,doc=self.docs.record(c,data['record_id']);self.b.require(project['id']==p['id'] and doc['folder_key']=='submittal','Choose a submittal document in this project.',404)
        files=self.docs.files(c,doc);self.b.require(files and files[0]['id']==data['file_id'],'The submittal file changed. Review the current version.',409)
        self.requests.checked_file(files[0]);return dict(document=doc,file=files[0])

    def apply_response(self,c,user,p,data):
        b=self.b;binding=self.response_binding(c,user,p,data)
        b.insert(c,'bc824_submittal_reviews',dict(company_id=user['company_id'],project_id=p['id'],record_id=data['record_id'],file_id=data['file_id'],decision=data['decision'],response=data['response'],reviewer_id=user['id'],created=b.now().isoformat()))
        self.docs.changed(c,user,binding['document'],'Submittal response recorded for file '+str(data['file_id']),status='complete' if data['decision']=='accepted' else 'received')
        return '/workspace/submittals?project_id='+str(p['id'])

    def response_review(self,record_id:int,request:Request,file_id:int=Form(...),decision:str=Form(...),response:str=Form(...)):
        b=self.b;b.origin(request);b.require(decision in {'accepted','accepted_with_notes','revise','rejected'},'Choose a review response.',400)
        data=dict(record_id=record_id,file_id=file_id,decision=decision,response=b.text(response,4000,'review response',True))
        with b.db(True) as c:
            user,p,doc=self.docs.record(c,record_id,True);binding=self.response_binding(c,user,p,data)
            body='<section class="card"><h2>'+esc(doc['title'])+'</h2><p>Decision: '+esc(decision.replace('_',' '))+'</p><p class="field-exact">'+esc(data['response'])+'</p>'+b.link('/workspace/documents/'+str(record_id)+'/files/'+str(file_id)+'?preview=1','Open exact file for review')+'<p>This records the project team response. Issuing it to a trade requires its own package review.</p></section>'
            return b.prepare(c,user,p,request,'submittal_response',data,'Review submittal response',body,'/workspace/submittals?project_id='+str(p['id']))

    def submittals(self,project_id:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Submittals & responses','/workspace/submittals')
            if not p:return b.page('Submittals',body)
            body+='<p>Record your response against an exact document revision, then issue the reviewed files in a package.</p><div class="field-actions">'+b.link('/workspace/documents/new?project_id='+str(p['id'])+'&folder=submittal','Add submittal document')+b.link(BASE+'/projects/'+str(p['id'])+'/new','Prepare submittal package')+'</div>'
            for f in self.choices(c,user,p['id']):
                if f['folder_key']!='submittal':continue
                reviews=[dict(r) for r in c.execute('SELECT * FROM bc824_submittal_reviews WHERE company_id=? AND record_id=? ORDER BY id DESC',(user['company_id'],f['record_id'])).fetchall()]
                current=next((r for r in reviews if r['file_id']==f['id']),None)
                body+='<section class="card"><h2>'+esc(f['title'])+'</h2><p>Current file version '+str(f['revision'])+' · '+esc(current['decision'].replace('_',' ') if current else 'Needs review')+'</p>'+b.link('/workspace/documents/'+str(f['record_id']),'Open document & versions')
                body+='<form class="field-form" method="post" action="/workspace/submittals/'+str(f['record_id'])+'/response">'+b.hidden('file_id',f['id'])+'<label for="decision-'+str(f['id'])+'">Response</label><select id="decision-'+str(f['id'])+'" name="decision">'+''.join('<option value="'+key+'">'+label+'</option>' for key,label in [('accepted','Accepted'),('accepted_with_notes','Accepted with notes'),('revise','Revise and resubmit'),('rejected','Rejected')])+'</select><label for="response-'+str(f['id'])+'">Response notes</label><textarea id="response-'+str(f['id'])+'" name="response" maxlength="4000" required></textarea><button>Review response</button></form><details><summary>Response history</summary>'
                for r in reviews:body+='<p>File #'+str(r['file_id'])+' · '+esc(r['decision'])+' · '+esc(r['response'])+'</p>'
                body+='</details></section>'
            body+='<details class="card"><summary>Earlier submittal register and AI reviews</summary>'+self.docs.hub.open_form(p['id'],'/submittals','Open existing submittal register')+'</details>'
        return b.page('Submittals',body)

    def contacts(self,project_id:int,sub_id:int):
        b=self.b
        with b.db() as c:
            user,p=b.actor(c,project_id);sub=b.app.state.daily_reports.sub(c,user,project_id,sub_id)
            rows=[dict(r) for r in c.execute('SELECT * FROM bc824_contacts WHERE company_id=? AND project_id=? AND sub_id=? ORDER BY id',(user['company_id'],project_id,sub_id)).fetchall()]
        body='<div class="hero"><h1>'+esc(sub['name'])+' contacts</h1><p>Office, project manager and foreman contacts. Contact entries do not create logins.</p></div>'
        for row in rows+[dict(id=0,version=0,name='',role='Foreman',email='',phone='')]:
            suffix=str(row['id']);body+='<form class="card field-form" method="post" action="/workspace/directory/projects/'+str(project_id)+'/subs/'+str(sub_id)+'/contacts/'+suffix+'"><h2>'+('Add contact' if not row['id'] else esc(row['name']))+'</h2>'+b.hidden('version',row['version'])
            for name,label in [('name','Name'),('role','Job role'),('email','Email'),('phone','Phone')]:body+='<label for="contact-'+name+'-'+suffix+'">'+label+'</label><input id="contact-'+name+'-'+suffix+'" name="'+name+'" value="'+esc(row[name])+'" maxlength="254"'+(' required' if name in {'name','role'} else '')+'>'
            body+='<button>Save contact</button></form>'
        body+=b.link('/workspace/directory/projects/'+str(project_id)+'/subs/'+str(sub_id),'Back to subcontractor')
        return b.page('Subcontractor contacts',body)

    def save_contact(self,project_id:int,sub_id:int,contact_id:int,request:Request,version:int=Form(...),name:str=Form(...),role:str=Form(...),email:str=Form(''),phone:str=Form('')):
        b=self.b;b.origin(request)
        data=dict(name=b.text(name,120,'a name',True),role=b.text(role,80,'a job role',True),email=b.text(email,254),phone=b.text(phone,60))
        b.require(not data['email'] or re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',data['email']),'Enter one valid email.',400)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);b.app.state.daily_reports.sub(c,user,project_id,sub_id)
            if contact_id:
                _,_,old=b.scope(c,'bc824_contacts',contact_id,True);b.require(old['project_id']==project_id and old['sub_id']==sub_id,'This contact belongs to a different subcontractor.',404);b.require(old['version']==version,'The contact changed. Reopen it.',409)
                c.execute('UPDATE bc824_contacts SET name=?,role=?,email=?,phone=?,version=version+1 WHERE id=?',(*data.values(),contact_id))
            else:contact_id=b.insert(c,'bc824_contacts',dict(company_id=user['company_id'],project_id=project_id,sub_id=sub_id,**data,version=1,created=b.now().isoformat()))
            b.event(c,user,project_id,'Directory contact saved',contact_id,data)
        return RedirectResponse('/workspace/directory/projects/'+str(project_id)+'/subs/'+str(sub_id)+'/contacts',303)

    def inbox(self):
        b=self.b
        with b.db() as c:
            user=b.user(c);b.require(self.ns['_bc840_tier'](user)=='trade','Subcontractors use My work. Project leaders use Command.',403)
            rows=c.execute('SELECT id FROM bc824_packages WHERE company_id=? AND recipient_id=? ORDER BY id DESC LIMIT 100',(user['company_id'],user['id'])).fetchall()
            body='<div class="hero"><h1>My work</h1><p>Your project leaders share the files, dates and requests you need here.</p></div><div class="field-actions">'+b.link('/workspace/shared','Assigned work, drawings & uploads')+'</div>'
            assigned=c.execute("SELECT s.id FROM bc_shared_work s WHERE s.company_id=? AND s.recipient_user_id=? AND s.revoked_at IS NULL AND s.kind<>'field_package' ORDER BY s.id DESC LIMIT 50",(user['company_id'],user['id'])).fetchall()
            cards=[]
            for shared in assigned:
                try:share=self.ns['_bc850_share'](c,user,shared['id'])
                except self.ns['_BC850_Problem'] as exc:
                    if exc.status in {403,404}:continue
                    raise
                cards.append(self.ns['_bc850_share_card'](share,False))
            if cards:body+='<h2>Assigned work & uploads</h2><div class="field-grid">'+''.join(cards)+'</div>'
            body+='<h2>Document packages & look-aheads</h2>'
            found=False
            for r in rows:
                try:_,row,share,snapshot,_=self.context(c,r['id'])
                except self.ns['_BC850_Problem'] as exc:
                    if exc.status in {403,404}:continue
                    raise
                found=True;body+='<section class="card"><h2>'+esc(row['title'])+'</h2><p>'+esc(snapshot['project_name'])+' · Due '+esc(row['due_date'] or 'not set')+'</p><p>'+('Receipt acknowledged' if row['acknowledged_at'] else 'Please open and acknowledge')+'</p>'+b.link(BASE+'/'+str(row['id']),'Open reviewed package')+'</section>'
            if not found:body+='<section class="card"><p>No current document packages. Your other assigned work is available above.</p></section>'
        return b.page('My work',body)
