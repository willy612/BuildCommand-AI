"""One authorized project search, indexed evidence and reviewed company notes."""
import json
import re
import subprocess
import sys
from pathlib import Path
from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from blueprint_field import esc
from command_center import command_json

BASE='/workspace/search'

class FieldIntelligence:
    def __init__(self,b):
        self.b=b
        b.schema('bc824_text','file_id BIGINT NOT NULL,record_id BIGINT NOT NULL,sha256 TEXT NOT NULL,page INTEGER NOT NULL,content TEXT NOT NULL,method TEXT NOT NULL,created TEXT NOT NULL',',UNIQUE(company_id,project_id,file_id,page)')
        b.schema('bc824_scope_rules','trade TEXT NOT NULL,instruction TEXT NOT NULL,version INTEGER NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL')
        for path,method,fn in [(BASE,'GET',self.search),('/workspace/documents/{record_id}/index','POST',self.index_file),('/workspace/documents/{record_id}/text','GET',self.document_text),('/workspace/scope-review','GET',self.scope_review),('/workspace/scope-rules','GET',self.rules),('/workspace/scope-rules','POST',self.save_rule)]:b.route(path,method,fn)
        b.route('/workspace/documents/{record_id}/suggested-links','GET',self.suggested_links)
        center=b.app.state.command_center;original=center.additional_evidence
        def evidence(c,user,pid):return self.evidence(c,user,pid)+b.app.state.field_readiness.evidence(c,user,pid)+original(c,user,pid)
        center.additional_evidence=evidence

    def evidence(self,c,user,pid):
        rows=c.execute('''SELECT x.*,d.title FROM bc824_text x JOIN bc_doc_files f ON f.id=x.file_id AND f.sha256=x.sha256 AND f.company_id=x.company_id AND f.project_id=x.project_id JOIN bc_doc_records d ON d.id=x.record_id AND d.company_id=x.company_id AND d.project_id=x.project_id WHERE x.company_id=? AND x.project_id=? AND NOT EXISTS(SELECT 1 FROM bc_doc_files newer WHERE newer.record_id=f.record_id AND newer.company_id=f.company_id AND newer.revision>f.revision) ORDER BY x.id DESC LIMIT 24''',(user['company_id'],pid)).fetchall()
        result=[('Document page',r['id'],r['title']+' · page '+str(r['page']),r['method']+'; '+r['content'][:1600],'/workspace/documents/'+str(r['record_id'])+'/text?page='+str(r['page'])) for r in rows]
        for r in c.execute('SELECT r.* FROM bc824_scope_rules r WHERE r.company_id=? AND r.project_id IN (0,?) AND NOT EXISTS(SELECT 1 FROM bc824_scope_rules newer WHERE newer.company_id=r.company_id AND newer.project_id=r.project_id AND LOWER(newer.trade)=LOWER(r.trade) AND newer.id>r.id) ORDER BY r.id DESC LIMIT 15',(user['company_id'],pid)).fetchall():result.append(('Approved scope note',r['id'],r['trade'],('Project exception: ' if r['project_id'] else 'Company guidance: ')+r['instruction'],'/workspace/scope-rules?project_id='+str(pid)))
        return result

    def index_file(self,record_id:int,request:Request,file_id:int=Form(...),ocr:str=Form('')):
        b=self.b;b.origin(request)
        with b.db() as c:
            user,p,row=b.docs.record(c,record_id);files=b.docs.files(c,row)
            b.require(files and files[0]['id']==file_id,'Choose the current document version.',409);f=dict(files[0]);path=b.app.state.document_requests.checked_file(f)
            b.require((f['mime_type']=='application/pdf' or Path(f['original_name']).suffix.lower()=='.txt'),'Index a PDF or plain-text document.',400)
            b.require(f['size_bytes']<=30*1024*1024,'Index files up to 30 MB.',413)
            identity=(user['id'],user['company_id'],row['version'])
        try:
            proc=subprocess.run([sys.executable,str(Path(__file__).with_name('field_extract.py')),str(path),'ocr' if ocr=='yes' else 'text'],timeout=110,capture_output=True,text=True)
            result=json.loads(proc.stdout)
        except (subprocess.TimeoutExpired,ValueError,OSError):b.require(False,'Text extraction could not finish. Try a smaller document.',503)
        b.require(proc.returncode==0 and not result.get('error'),result.get('error','Text extraction could not finish.'),400)
        pages=result['pages'];b.require(any(x['text'].strip() for x in pages),'No readable text was found. Try OCR for a scanned PDF.',400)
        with b.db(True) as c:
            user,p,row=b.docs.record(c,record_id,True);current=b.docs.files(c,row)
            b.require((user['id'],user['company_id'],row['version'])==identity and current and current[0]['id']==file_id,'The document or access changed. Index its current version again.',409)
            b.app.state.document_requests.checked_file(current[0])
            c.execute('DELETE FROM bc824_text WHERE company_id=? AND project_id=? AND record_id=?',(user['company_id'],p['id'],record_id))
            for page in pages:b.insert(c,'bc824_text',dict(company_id=user['company_id'],project_id=p['id'],file_id=file_id,record_id=record_id,sha256=f['sha256'],page=page['page'],content=page['text'],method=page['method'],created=b.now().isoformat()))
            b.event(c,user,p['id'],'Document indexed for project search',record_id,dict(file_id=file_id,pages=len(pages),ocr=ocr=='yes'))
        return RedirectResponse('/workspace/documents/'+str(record_id)+'/text',303)

    def document_text(self,record_id:int,page:int=1):
        b=self.b
        with b.db() as c:
            user,p,row=b.docs.record(c,record_id);files=b.docs.files(c,row);fid=files[0]['id'] if files else 0
            pages=[dict(r) for r in c.execute('SELECT * FROM bc824_text WHERE company_id=? AND project_id=? AND record_id=? AND file_id=? ORDER BY page',(user['company_id'],p['id'],record_id,fid)).fetchall()]
        body='<div class="hero"><h1>'+esc(row['title'])+'</h1><p>Searchable text from the current file. Verify measurements and critical wording against the original.</p></div><div class="field-actions">'+b.link('/workspace/documents/'+str(record_id),'Back to document')+'</div>'
        for r in pages:
            body+='<section class="card" id="page-'+str(r['page'])+'"><h2>Page '+str(r['page'])+'</h2><p>'+esc(r['method'])+'</p>'+b.link('/workspace/documents/'+str(record_id)+'/files/'+str(fid)+'?preview=1#page='+str(r['page']),'Open source page')+'<pre class="field-exact">'+esc(r['content'])+'</pre></section>'
        if not pages:body+='<section class="card"><p>The current version has not been indexed. Open Project Search to prepare its text.</p></section>'
        return b.page('Document evidence',body)

    def search(self,project_id:int=0,q:str='',page:int=1):
        b=self.b;q=b.text(q,120);b.require(1<=page<=10000,'Choose a valid search page.',400)
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Find it on this job',BASE)
            if not p:return b.page('Project search',body)
            pid=p['id'];body+='<form class="card field-form" method="get" action="'+BASE+'">'+b.hidden('project_id',pid)+b.input('q','Drawing, RFI, document, trade or phrase',q,extra='maxlength="120"')+'<button>Search this project</button></form>'
            pattern='%'+q.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%';offset=(page-1)*20;results=[]
            # Each source applies project/company predicates before reading text.
            sources=[('RFI','project_issues','title','response',"UPPER(issue_type)='RFI'",'/workspace/rfi-answers/{id}/prepare?project_id='+str(pid)),('Submittal','submittals','title','notes','1=1','/workspace/submittals?project_id='+str(pid)),('Schedule','activities','name','name','1=1','/workspace/readiness?project_id='+str(pid)),('Directory','subs','name','trade','1=1','/workspace/directory/projects/'+str(pid)+'/subs/{id}'),('Document','bc_doc_records','title','notes','company_id='+str(int(user['company_id'])),'/workspace/documents/{id}'),('Checklist','bc_check_runs','title','area','company_id='+str(int(user['company_id'])),'/workspace/checklists/{id}')]
            sources += [('Photo finding','bc_photo_analyses','original_name','result_text','company_id='+str(int(user['company_id'])),'/workspace/photos/{id}'),('Daily report','daily_reports','report_date','work_completed','1=1','/workspace/daily/reports/{id}'),('Drawing','bc_drawing_sheets','sheet_number','title','company_id='+str(int(user['company_id']))+' AND EXISTS(SELECT 1 FROM bc_drawing_heads h WHERE h.sheet_id=bc_drawing_sheets.id AND h.company_id=bc_drawing_sheets.company_id AND h.project_id=bc_drawing_sheets.project_id)','/workspace/drawing-sheets/{id}'),('Work card','bc_drawing_pins','title','body','company_id='+str(int(user['company_id']))+" AND (visibility='team' OR created_by="+str(int(user['id']))+")",'/workspace/drawing-sheets/{id}')]
            available=b.ns['_bc800_table_names']()
            for kind,table,title,detail,condition,path in (sources if q else []):
                if table not in available:continue
                rows=c.execute(f"SELECT id,{title} AS title,{detail} AS detail{',sheet_id' if table=='bc_drawing_pins' else ''} FROM {table} WHERE project_id=? AND {condition} AND (LOWER(COALESCE({title},'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE({detail},'')) LIKE ? ESCAPE '\\') ORDER BY id DESC LIMIT 21 OFFSET ?",(pid,pattern,pattern,offset)).fetchall()
                results.extend(dict(kind=kind,id=r['id'],title=r['title'],detail=r['detail'] or '',path=('/workspace/drawing-sheets/'+str(r['sheet_id'])+'/work/'+str(r['id']) if table=='bc_drawing_pins' else path.format(id=r['id']))) for r in rows)
            for r in c.execute("""SELECT x.*,d.title FROM bc824_text x JOIN bc_doc_files f ON f.id=x.file_id AND f.sha256=x.sha256 AND f.company_id=x.company_id JOIN bc_doc_records d ON d.id=x.record_id AND d.company_id=x.company_id WHERE x.company_id=? AND x.project_id=? AND LOWER(x.content) LIKE ? ESCAPE '\\' AND NOT EXISTS(SELECT 1 FROM bc_doc_files newer WHERE newer.record_id=f.record_id AND newer.company_id=f.company_id AND newer.revision>f.revision) ORDER BY x.id DESC LIMIT 21 OFFSET ?""",(user['company_id'],pid,pattern,offset)).fetchall() if q else []:
                pos=r['content'].lower().find(q.lower());start=max(0,pos-100);results.append(dict(kind='Document page',id=r['id'],title=r['title']+' · page '+str(r['page']),detail=r['content'][start:start+650],path='/workspace/documents/'+str(r['record_id'])+'/text?page='+str(r['page'])+'#page-'+str(r['page'])))
            body+='<section class="card"><h2>Matches</h2><p>Up to 21 matches per source per page. Indexed text is limited to files you prepared below.</p>'
            for r in results:body+='<article class="field-row"><span class="field-pill">'+r['kind']+'</span><h3>'+b.link(r['path'],r['title'])+'</h3><p class="field-exact">'+esc(r['detail'][:650])+'</p></article>'
            if not results:body+='<p>'+('No matching records in this project.' if q else 'Enter a word or phrase to find its project records.')+'</p>'
            body+='</section><form method="get">'+b.hidden('project_id',pid)+b.hidden('q',q)+b.hidden('page',page+1)+'<button>Next search page</button></form>'
            body+='<details class="card"><summary>Prepare document text for search & Ask</summary><p>PDF/text extraction runs on this server. OCR is optional for scans. Indexed excerpts can enter a reviewed Ask request just like other project records.</p>'
            for f in b.app.state.field_delivery.choices(c,user,pid):
                if f['mime_type']!='application/pdf' and Path(f['original_name']).suffix.lower()!='.txt':continue
                count=c.execute('SELECT COUNT(*) AS n FROM bc824_text WHERE company_id=? AND file_id=?',(user['company_id'],f['id'])).fetchone()['n']
                body+='<form class="field-form field-row" method="post" action="/workspace/documents/'+str(f['record_id'])+'/index">'+b.hidden('file_id',f['id'])+'<strong>'+esc(f['title'])+' · version '+str(f['revision'])+'</strong><p>'+str(count)+' pages indexed</p><label><input type="checkbox" name="ocr" value="yes"> OCR pages without readable text</label><button>Prepare searchable text</button></form>'+b.link('/workspace/documents/'+str(f['record_id'])+'/suggested-links','Review possible record links')
            body+='</details>'+b.link('/workspace/scope-review?project_id='+str(pid),'Review scope overlaps & possible links')
        return b.page('Project search',body)

    def rules(self,project_id:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Company scope guidance','/workspace/scope-rules')
            if not p:return b.page('Scope guidance',body)
            rows=c.execute('SELECT * FROM bc824_scope_rules WHERE company_id=? AND project_id IN(0,?) ORDER BY id DESC LIMIT 100',(user['company_id'],p['id'])).fetchall()
            for r in rows:body+='<section class="card"><h2>'+esc(r['trade'])+'</h2><p>'+('Project exception' if r['project_id'] else 'Company guidance')+' · '+esc(r['created'])+'</p><p class="field-exact">'+esc(r['instruction'])+'</p></section>'
            body+='<form class="card field-form" method="post" action="/workspace/scope-rules">'+b.hidden('project_id',p['id'])+b.input('trade','Trade',extra='required maxlength="100"')+b.area('instruction','Approved scope guidance or project exception')+'<label><input type="checkbox" name="company_wide" value="yes"> Company-wide guidance (company administrator only)</label><label><input type="checkbox" name="confirmed" value="yes" required> I reviewed this guidance for use in future project answers.</label><button>Record approved guidance</button><p>Earlier notes remain in history. These notes never rewrite an issued scope automatically.</p></form>'
        return b.page('Scope guidance',body)

    def save_rule(self,request:Request,project_id:int=Form(...),trade:str=Form(...),instruction:str=Form(...),company_wide:str=Form(''),confirmed:str=Form('')):
        b=self.b;b.origin(request);b.require(confirmed=='yes','Review the guidance before saving.',400)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True)
            b.require(company_wide!='yes' or b.ns['_bc840_tier'](user) in {'owner','admin'},'Only a company administrator can approve company-wide guidance.',403)
            pid=0 if company_wide=='yes' else project_id
            data=dict(trade=b.text(trade,100,'a trade',True),instruction=b.text(instruction,4000,'guidance',True))
            prior=c.execute('SELECT MAX(version) AS n FROM bc824_scope_rules WHERE company_id=? AND project_id=? AND LOWER(trade)=LOWER(?)',(user['company_id'],pid,data['trade'])).fetchone()
            rid=b.insert(c,'bc824_scope_rules',dict(company_id=user['company_id'],project_id=pid,**data,version=int(prior['n'] or 0)+1,created_by=user['id'],created=b.now().isoformat()))
            b.event(c,user,pid,'Scope guidance approved',rid,data)
        return RedirectResponse('/workspace/scope-rules?project_id='+str(project_id),303)

    def scope_review(self,project_id:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Check scope coordination','/workspace/scope-review')
            if not p:return b.page('Scope coordination',body)
            rows=[]
            if 'blueprint_scope_items' in b.ns['_bc800_table_names']():
                rows=[dict(r) for r in c.execute("SELECT i.* FROM blueprint_scope_items i JOIN blueprint_runs r ON r.id=i.run_id AND r.company_id=i.company_id WHERE i.company_id=? AND i.project_id=? AND UPPER(r.status) IN ('COMPLETE','COMPLETED','SUCCESS') ORDER BY i.id DESC LIMIT 500",(user['company_id'],p['id'])).fetchall()]
            groups={}
            for r in rows:
                key=' '.join(re.findall(r'\w+',str(r.get('requirement') or '').lower()))
                if key:groups.setdefault(key,[]).append(r)
            body+='<p>These are coordination leads from the newest 500 extracted requirements. Repeated wording is not proof of an overlap; missing records are not proof of a scope gap.</p><section class="card"><h2>Dates that need coordination</h2>'
            readiness=b.app.state.field_readiness;activities={a['id']:a for a in readiness.activities(c,user,p['id'])};conflicts=0
            for hold in readiness.constraints(c,user,p['id']):
                start=str(activities.get(hold['activity_id'],{}).get('start') or '')[:10]
                if hold['state']!='cleared' and start and hold['expected_date'] and hold['expected_date']>start:
                    conflicts+=1;body+='<p><strong>'+esc(hold['title'])+'</strong>: expected '+esc(hold['expected_date'])+'; activity starts '+esc(start)+'. '+b.link('/workspace/readiness?project_id='+str(p['id'])+'#constraints','Review the source dates')+'</p>'
            if not conflicts:body+='<p>No recorded expected dates later than their linked activity start were found. Missing dates remain unknown.</p>'
            body+='</section><section class="card"><h2>Possible overlaps</h2>'
            found=False
            for group in groups.values():
                if len({r.get('trade') for r in group})<2:continue
                found=True;body+='<div class="field-row"><p>'+esc(group[0]['requirement'])+'</p>'
                for r in group:body+='<p>'+esc(r.get('trade'))+' · '+esc(r.get('source_sheet'))+' · '+esc(r.get('source_detail'))+'</p>'
                body+=b.link('/workspace/scopes?project_id='+str(p['id']),'Review original scopes')+'</div>'
            if not found:body+='<p>No exact repeated requirements across trades were found in this selection. A project scope review is still needed.</p>'
            body+='</section><section class="card"><h2>Check responsibility gaps</h2><p>Review scopes without an appointed trade and check exclusions before publishing.</p>'+b.link('/workspace/scopes?project_id='+str(p['id']),'Review trade scopes')+b.link('/workspace/scope-rules?project_id='+str(p['id']),'Company guidance & project exceptions')+'</section><section class="card"><h2>Investigate conflicting direction</h2><p>Search a location, sheet number or requirement to compare the dated RFI answer, document revision and field record. Ask can use indexed text and its source links; it cannot prove that every document agrees.</p>'+b.link(BASE+'?project_id='+str(p['id']),'Find related records')+'</section>'
        return b.page('Scope coordination',body)

    def suggested_links(self,record_id:int):
        b=self.b;stop={'the','and','for','with','from','this','that','project','review','document','installation'}
        def words(value):return {w for w in re.findall(r'[a-z0-9][a-z0-9.-]{2,}',str(value).lower()) if w not in stop}
        with b.db() as c:
            user,p,row=b.docs.record(c,record_id);b.require(p['id']>0,'Choose a project document.',400);terms=words(row['title']+' '+row['notes'])
            linked={(r['kind'],r['source_id']) for r in b.docs.links(c,user,row)};suggestions=[]
            for kind,table,title,extra in [('rfi','project_issues','title'," AND UPPER(issue_type)='RFI'"),('submittal','submittals','title',''),('document','attachments','title',' AND company_id='+str(int(user['company_id'])))]:
                for source in c.execute('SELECT id,'+title+' AS title FROM '+table+' WHERE project_id=?'+extra+' ORDER BY id DESC LIMIT 1000',(p['id'],)).fetchall():
                    matches=terms&words(source['title'])
                    if len(matches)>=2 and (kind,source['id']) not in linked:suggestions.append(dict(kind=kind,source_id=source['id'],title=source['title'],matches=sorted(matches)))
            suggestions.sort(key=lambda r:len(r['matches']),reverse=True)
            body='<div class="hero"><h1>Possible related records</h1><p>'+esc(row['title'])+'</p></div><p>Suggestions use matching title and note terms in this project. Review relevance yourself; nothing is linked automatically.</p>'
            for r in suggestions[:20]:
                body+='<form class="card field-form" method="post" action="/workspace/documents/'+str(record_id)+'/links">'+b.hidden('version',row['version'])+b.hidden('kind',r['kind'])+b.hidden('source_id',r['source_id'])+'<h2>'+esc(r['title'])+'</h2><p>'+esc(r['kind'])+' · Matching terms: '+esc(', '.join(r['matches']))+'</p><button>Confirm relevant & link record</button></form>'
            if not suggestions:body+='<section class="card"><p>No strong title matches in the newest 1,000 records of each type. You can still choose a record manually.</p></section>'
        return b.page('Possible links',body+b.link('/workspace/documents/'+str(record_id),'Back to document & manual links'))
