"""Trade look-aheads and make-ready records, sharing the existing schedule."""
import json
from datetime import timedelta
from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from blueprint_field import esc
from command_center import command_json

BASE='/workspace/readiness'
KINDS={'approval':'Approval','material':'Material / procurement','access':'Site access','inspection':'Inspection appointment','predecessor':'Earlier work','other':'Other constraint'}
STATES={'open':'Needs action','requested':'Requested','confirmed':'Confirmed appointment / delivery','cleared':'Cleared with evidence'}

class FieldReadiness:
    def __init__(self,b):
        self.b=b;self.delivery=b.app.state.field_delivery
        b.schema('bc824_assignments','activity_id BIGINT NOT NULL,sub_id BIGINT NOT NULL,recipient_id BIGINT NOT NULL,lead_days INTEGER NOT NULL,version INTEGER NOT NULL',',UNIQUE(company_id,project_id,activity_id)')
        b.schema('bc824_constraints','activity_id BIGINT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,owner TEXT NOT NULL,needed_date TEXT NOT NULL,expected_date TEXT NOT NULL,state TEXT NOT NULL,evidence TEXT NOT NULL,record_id BIGINT NOT NULL,version INTEGER NOT NULL,updated TEXT NOT NULL')
        b.schema('bc824_readiness_replies','package_id BIGINT NOT NULL,crew TEXT NOT NULL,materials TEXT NOT NULL,start_date TEXT NOT NULL,note TEXT NOT NULL,actor_id BIGINT NOT NULL,created TEXT NOT NULL')
        b.action('schedule_dates',self.date_binding,self.apply_dates)
        b.action('tomorrow_brief',self.brief_binding,self.apply_brief)
        b.route(BASE+'/projects/{project_id}/tomorrow','GET',self.tomorrow)
        b.route(BASE+'/projects/{project_id}/tomorrow','POST',self.review_tomorrow)
        for path,method,fn in [(BASE,'GET',self.index),(BASE+'/projects/{project_id}/assign','POST',self.assign),(BASE+'/projects/{project_id}/constraints','POST',self.constraint),(BASE+'/projects/{project_id}/lookahead','POST',self.lookahead),(BASE+'/projects/{project_id}/dates','POST',self.date_review),('/workspace/transmittals/{package_id}/readiness','POST',self.reply)]:b.route(path,method,fn)

    def activities(self,c,user,pid):
        # Parent project permission is checked before every call. No copies of activities.
        return [dict(r) for r in c.execute('SELECT * FROM activities WHERE project_id=? ORDER BY id LIMIT 3000',(pid,)).fetchall()]

    def assignments(self,c,user,pid):
        return [dict(r) for r in c.execute('SELECT a.*,s.name AS sub_name,s.trade FROM bc824_assignments a JOIN subs s ON s.id=a.sub_id AND s.project_id=a.project_id WHERE a.company_id=? AND a.project_id=? ORDER BY a.id',(user['company_id'],pid)).fetchall()]

    def constraints(self,c,user,pid):
        return [dict(r) for r in c.execute('SELECT * FROM bc824_constraints WHERE company_id=? AND project_id=? ORDER BY needed_date,id LIMIT 1000',(user['company_id'],pid)).fetchall()]

    def activity(self,c,user,pid,aid):
        row=c.execute('SELECT * FROM activities WHERE id=? AND project_id=?',(aid,pid)).fetchone()
        self.b.require(row is not None,'Choose an activity from this project.',404);return dict(row)

    def assign(self,project_id:int,request:Request,activity_id:int=Form(...),sub_id:int=Form(...),recipient_id:int=Form(...),lead_days:int=Form(21),version:int=Form(0)):
        b=self.b;b.origin(request);b.require(1<=lead_days<=90,'Choose 1 to 90 days of notice.',400)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);self.activity(c,user,project_id,activity_id)
            b.app.state.daily_reports.sub(c,user,project_id,sub_id);b.ns['_bc850_recipient'](c,user,project_id,recipient_id)
            old=c.execute('SELECT * FROM bc824_assignments WHERE company_id=? AND project_id=? AND activity_id=?',(user['company_id'],project_id,activity_id)).fetchone()
            b.require((old['version'] if old else 0)==version,'The activity assignment changed. Reload before saving.',409)
            data=dict(sub_id=sub_id,recipient_id=recipient_id,lead_days=lead_days)
            if old:c.execute('UPDATE bc824_assignments SET sub_id=?,recipient_id=?,lead_days=?,version=version+1 WHERE id=?',(*data.values(),old['id']))
            else:b.insert(c,'bc824_assignments',dict(company_id=user['company_id'],project_id=project_id,activity_id=activity_id,**data,version=1))
            b.event(c,user,project_id,'Activity trade assigned',activity_id,data)
        return RedirectResponse(BASE+'?project_id='+str(project_id),303)

    def constraint(self,project_id:int,request:Request,activity_id:int=Form(...),kind:str=Form(...),title:str=Form(...),owner:str=Form(''),needed_date:str=Form(''),expected_date:str=Form(''),state:str=Form('open'),evidence:str=Form(''),record_id:int=Form(0),constraint_id:int=Form(0),version:int=Form(0)):
        b=self.b;b.origin(request);b.require(kind in KINDS and state in STATES,'Choose a listed constraint and status.',400)
        data=dict(activity_id=activity_id,kind=kind,title=b.text(title,240,'what is needed',True),owner=b.text(owner,240),needed_date=b.day(needed_date),expected_date=b.day(expected_date),state=state,evidence=b.text(evidence,4000),record_id=record_id)
        b.require(state not in {'confirmed','cleared'} or bool(data['evidence']),'Record the confirmation or clearance evidence.',400)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);self.activity(c,user,project_id,activity_id)
            if record_id:
                _,rp,_=b.docs.record(c,record_id);b.require(rp['id']==project_id,'The reference must belong to this project.',403)
            if constraint_id:
                _,_,old=b.scope(c,'bc824_constraints',constraint_id,True);b.require(old['project_id']==project_id,'This constraint belongs to another project.',403)
                b.require(old['version']==version,'This constraint changed. Reopen it.',409)
                c.execute('UPDATE bc824_constraints SET '+','.join(k+'=?' for k in data)+',version=version+1,updated=? WHERE id=?',(*data.values(),b.now().isoformat(),constraint_id))
            else:constraint_id=b.insert(c,'bc824_constraints',dict(company_id=user['company_id'],project_id=project_id,**data,version=1,updated=b.now().isoformat()))
            b.event(c,user,project_id,'Make-ready record saved',constraint_id,data)
        return RedirectResponse(BASE+'?project_id='+str(project_id)+'#constraints',303)

    def package_binding(self,c,user,p,selection):
        ids=selection['activity_ids'];b=self.b;b.require(0<len(ids)<=100 and len(ids)==len(set(ids)),'Select 1 to 100 activities.',400)
        assignments={a['activity_id']:a for a in self.assignments(c,user,p['id'])};rows=[]
        for aid in ids:
            a=self.activity(c,user,p['id'],aid);mapping=assignments.get(aid)
            b.require(mapping and mapping['recipient_id']==selection['recipient_id'],'An activity recipient changed. Review the look-ahead again.',409)
            b.ns['_bc850_recipient'](c,user,p['id'],mapping['recipient_id'])
            # Explicit dates and exact source values participate in the review hash.
            rows.append(dict(activity=a,assignment=mapping))
        holds=[x for x in self.constraints(c,user,p['id']) if x['activity_id'] in ids]
        return dict(activities=rows,constraints=holds)

    def lookahead(self,project_id:int,request:Request,recipient_id:int=Form(...),activity_ids:list[int]=Form(default=[]),notify_email:str=Form('')):
        b=self.b;b.origin(request)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);selection=dict(activity_ids=activity_ids,recipient_id=recipient_id)
            source=self.package_binding(c,user,p,selection);lines=['Three-week look-ahead. Please confirm your crew, materials and planned start.']
            for item in source['activities']:
                a=item['activity'];lines.append(str(a['name'])+' | Start: '+str(a.get('start') or 'Not recorded')+' | Finish: '+str(a.get('finish') or 'Not recorded'))
                for hold in source['constraints']:
                    if hold['activity_id']==a['id'] and hold['state']!='cleared':lines.append('Needs coordination: '+hold['title']+'; '+STATES[hold['state']])
            message=b.text('\n'.join(lines),12000,'look-ahead instructions',True)
            data=dict(title=p['name']+' — Trade look-ahead',message=message,recipient_id=recipient_id,purpose='lookahead',due_date=(b.now().date()+timedelta(days=2)).isoformat(),expires=(b.now()+timedelta(days=30)).isoformat(),file_ids=[],notify_email=notify_email=='yes',readiness_snapshot=selection)
            bind=self.delivery.binding(c,user,p,data)
            return b.prepare(c,user,p,request,'issue_package',data,'Review trade look-ahead',self.delivery.preview_body(bind,data),BASE+'?project_id='+str(project_id))

    def date_binding(self,c,user,p,data):
        b=self.b;a=self.activity(c,user,p['id'],data['activity_id']);b.require('start' in a,'Use the existing schedule editor for this schedule format.',409)
        return dict(activity=a,assignments=self.assignments(c,user,p['id']),constraints=self.constraints(c,user,p['id']))

    def date_review(self,project_id:int,request:Request,activity_id:int=Form(...),start:str=Form(...),finish:str=Form(...),reason:str=Form(...)):
        b=self.b;b.origin(request);data=dict(activity_id=activity_id,start=b.day(start,True),finish=b.day(finish,True),reason=b.text(reason,2000,'a reason',True));b.require(start<=finish,'Finish must be on or after start.',400)
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);source=self.date_binding(c,user,p,data);a=source['activity']
            body='<section class="card"><h2>'+esc(a['name'])+'</h2><p>Current: '+esc(a.get('start') or 'Not recorded')+' to '+esc(a.get('finish') or 'Not recorded')+'</p><p>Proposed: '+esc(start)+' to '+esc(finish)+'</p><p>'+esc(data['reason'])+'</p><p>This changes this activity only. Linked activities are not rescheduled. Review dependencies in the existing recovery tools before approving. Issued notices keep their original dates; prepare a revised notice separately.</p><h3>Appointed trade</h3>'
            body+=''.join('<p>'+esc(x['sub_name'])+'</p>' for x in source['assignments'] if x['activity_id']==activity_id)+'</section>'
            return b.prepare(c,user,p,request,'schedule_dates',data,'Review schedule date change',body,BASE+'?project_id='+str(project_id))

    def apply_dates(self,c,user,p,data):
        before=self.date_binding(c,user,p,data)['activity'];c.execute('UPDATE activities SET start=?,finish=? WHERE id=? AND project_id=?',(data['start'],data['finish'],data['activity_id'],p['id']))
        self.b.event(c,user,p['id'],'Reviewed schedule date change',data['activity_id'],dict(before=before,after=data))
        return BASE+'?project_id='+str(p['id'])

    def reply(self,package_id:int,request:Request,crew:str=Form(...),materials:str=Form(...),start_date:str=Form(''),note:str=Form('')):
        b=self.b;b.origin(request);b.require(crew in {'ready','not_ready','unknown'} and materials in {'ready','not_ready','unknown'},'Choose crew and material readiness.',400)
        with b.db(True) as c:
            user,row,share,snapshot,manager=self.delivery.context(c,package_id,True)
            b.require(not manager and row['purpose']=='lookahead' and share['state']=='OPEN','Only the assigned trade can update this open look-ahead.',403)
            data=dict(crew=crew,materials=materials,start_date=b.day(start_date),note=b.text(note,2000))
            b.insert(c,'bc824_readiness_replies',dict(company_id=user['company_id'],project_id=row['project_id'],package_id=package_id,**data,actor_id=user['id'],created=b.now().isoformat()))
            b.event(c,user,row['project_id'],'Trade readiness reported',package_id,data)
        return RedirectResponse('/workspace/transmittals/'+str(package_id),303)

    def evidence(self,c,user,pid):
        result=[]
        for r in self.constraints(c,user,pid):
            if r['state']=='cleared':continue
            result.append(('Make-ready constraint',r['id'],r['title'],command_json({k:r[k] for k in ('activity_id','kind','owner','needed_date','expected_date','state','evidence')}),BASE+'?project_id='+str(pid)+'#constraints'))
        for r in c.execute('SELECT r.*,p.title FROM bc824_readiness_replies r JOIN bc824_packages p ON p.id=r.package_id AND p.company_id=r.company_id WHERE r.company_id=? AND r.project_id=? AND NOT EXISTS(SELECT 1 FROM bc824_readiness_replies newer WHERE newer.package_id=r.package_id AND newer.company_id=r.company_id AND newer.id>r.id) ORDER BY r.id DESC LIMIT 10',(user['company_id'],pid)).fetchall():result.append(('Trade readiness reply',r['id'],r['title'],'Crew: '+r['crew']+'; Materials: '+r['materials']+'; Planned start: '+r['start_date']+'; Note: '+r['note'],'/workspace/transmittals/'+str(r['package_id'])))
        return result[:35]

    def brief_binding(self,c,user,p,data):
        self.b.require(data['brief_date']==(self.b.now().date()+timedelta(days=1)).isoformat(),'The draft date changed. Prepare tomorrow’s current draft.',409)
        daily=self.b.app.state.daily_command
        return dict(current=daily.collect(c,user,p),readiness=self.evidence(c,user,p['id']),daily=daily.ns['app'].state.command_center.report_evidence(c,user,p['id']))

    def tomorrow(self,project_id:int):
        b=self.b;date=(b.now().date()+timedelta(days=1)).isoformat()
        with b.db() as c:
            user,p=b.actor(c,project_id);source=self.brief_binding(c,user,p,dict(brief_date=date));lines=['Draft for '+date+'. Based on records available today; confirm with the crews.']
            for kind,rid,title,detail,path in source['readiness'][:15]:lines.append(title+': '+detail)
            for report in source['daily'][:3]:lines.append('Daily report '+str(report.get('report_date'))+': '+str(report.get('work_completed') or 'Work not recorded')+'; Delays: '+str(report.get('delays') or 'None recorded'))
        notes='\n'.join(lines)[:6000]
        body='<div class="hero"><h1>Prepare tomorrow’s briefing</h1><p>'+esc(p['name'])+' · '+date+'</p></div><p>The saved briefing will keep today’s source snapshot and your direction for tomorrow. Nothing is sent to trades.</p><form class="card field-form" method="post">'+b.hidden('brief_date',date)+b.area('notes','Review tomorrow’s field direction',notes,6000)+'<button>Review tomorrow’s briefing</button></form>'+b.link('/workspace/command?project_id='+str(project_id),'Back to Command')
        return b.page('Tomorrow’s briefing',body)

    def review_tomorrow(self,project_id:int,request:Request,brief_date:str=Form(...),notes:str=Form(...)):
        b=self.b;b.origin(request);data=dict(brief_date=b.day(brief_date,True),notes=b.text(notes,6000,'briefing notes',True))
        with b.db(True) as c:
            user,p=b.actor(c,project_id,True);source=self.brief_binding(c,user,p,data)
            body='<section class="card"><h2>'+esc(brief_date)+'</h2><p>Prepared from the current records captured today. Recheck this briefing when work starts.</p><pre class="field-exact">'+esc(data['notes'])+'</pre></section>'+b.app.state.daily_command.snapshot_html(source['current'],True)
            return b.prepare(c,user,p,request,'tomorrow_brief',data,'Review tomorrow’s briefing',body,BASE+'/projects/'+str(project_id)+'/tomorrow')

    def apply_brief(self,c,user,p,data):
        b=self.b;source=self.brief_binding(c,user,p,data);snapshot=source['current'];now=b.now().isoformat()
        snapshot.update(brief_date=data['brief_date'],leader_notes='Prepared from the prior day’s current records. Recheck before work starts.\n\n'+data['notes'],prepared_at=now,prepared_by=user.get('display_name') or user['email'])
        daily=b.app.state.daily_command;rid=daily.insert(c,'bc_daily_command_briefs','company_id,project_id,brief_date,source_hash,snapshot_json,created_by,created_at',(user['company_id'],p['id'],data['brief_date'],daily.material_hash(snapshot),command_json(snapshot),user['id'],now))
        return '/workspace/command/briefs/'+str(rid)

    def index(self,project_id:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Get trades ready',BASE)
            if not p:return b.page('Get trades ready',body+'<p>Choose a project.</p>')
            pid=p['id'];activities=self.activities(c,user,pid);assignments={r['activity_id']:r for r in self.assignments(c,user,pid)};holds=self.constraints(c,user,pid)
            subs=[dict(r) for r in c.execute('SELECT id,name FROM subs WHERE project_id=? ORDER BY name',(pid,)).fetchall()];recipients=self.delivery.requests.recipients(c,user,pid)
            today=b.now().date();end=today+timedelta(days=21)
            body+='<p>Who is coming, what they need, and what is holding the work up.</p><div class="field-actions">'+b.link('/workspace/directory?project_id='+str(pid),'Subcontractor directory')+b.link('/workspace/checklists?project_id='+str(pid),'Safety & inspections')+b.link('/workspace/transmittals?project_id='+str(pid),'Issued packages')+'</div>'
            body+='<section class="card"><h2>Next three weeks</h2><p>'+today.isoformat()+' to '+end.isoformat()+'. Undated activities are listed below for setup.</p>'
            for recipient in recipients:
                selected=[a for a in activities if a.get('start') and today.isoformat()<=str(a['start'])[:10]<=end.isoformat() and assignments.get(a['id'],{}).get('recipient_id')==recipient['id']]
                if not selected:continue
                body+='<form class="field-form" method="post" action="'+BASE+'/projects/'+str(pid)+'/lookahead">'+b.hidden('recipient_id',recipient['id'])+'<h3>'+esc(recipient['display_name'] or recipient['email'])+'</h3>'
                for a in selected:body+='<label><input type="checkbox" name="activity_ids" value="'+str(a['id'])+'" checked> '+esc(a['name'])+' · '+esc(str(a['start'])[:10])+'</label>'
                body+='<label><input type="checkbox" name="notify_email" value="yes"> Queue an email after approval</label><button>Review trade look-ahead</button></form>'
            body+='<p>No notices are issued until you review and approve them.</p></section><section class="card"><h2>Activities & trade assignments</h2>'
            for a in activities[:300]:
                old=assignments.get(a['id'],{});sid=str(a['id']);open_holds=[x for x in holds if x['activity_id']==a['id'] and x['state']!='cleared']
                body+='<details class="field-row"><summary>'+esc(a['name'])+' · '+esc(old.get('sub_name','Assign a trade'))+' · '+str(len(open_holds))+' open constraints</summary><p>Start: '+esc(str(a.get('start') or 'Not recorded'))+' · Finish: '+esc(str(a.get('finish') or 'Not recorded'))+' · Recorded progress: '+esc(str(a.get('pct') if a.get('pct') is not None else 'Not recorded'))+'</p>'
                if old and a.get('start'):
                    try:notice=b.now().date().fromisoformat(str(a['start'])[:10])-timedelta(days=old['lead_days']);body+='<p>Prepare mobilization notice by '+notice.isoformat()+'</p>'
                    except ValueError:pass
                body+='<form class="field-form" method="post" action="'+BASE+'/projects/'+str(pid)+'/assign">'+b.hidden('activity_id',a['id'])+b.hidden('version',old.get('version',0))
                for name,label,options in [('sub_id','Directory subcontractor',[(r['id'],r['name']) for r in subs]),('recipient_id','Appointed recipient',[(r['id'],r['email']) for r in recipients])]:
                    body+='<label for="'+name+sid+'">'+label+'</label><select id="'+name+sid+'" name="'+name+'" required><option value="">Choose</option>'+''.join('<option value="'+str(k)+'"'+(' selected' if k==old.get(name) else '')+'>'+esc(v)+'</option>' for k,v in options)+'</select>'
                body+='<label for="lead'+sid+'">Mobilization lead days</label><input id="lead'+sid+'" name="lead_days" type="number" min="1" max="90" value="'+str(old.get('lead_days',21))+'"><button>Save trade assignment</button></form>'
                if 'start' in a:
                    body+='<form class="field-form" method="post" action="'+BASE+'/projects/'+str(pid)+'/dates">'+b.hidden('activity_id',a['id'])
                    for name,label in [('start','Proposed start'),('finish','Proposed finish'),('reason','Reason for change')]:body+='<label for="'+name+sid+'">'+label+'</label><input id="'+name+sid+'" name="'+name+'" type="'+('text' if name=='reason' else 'date')+'" required>'
                    body+='<button>Review date change</button></form>'
                body+='</details>'
            if not activities:body+='<p>Import the project schedule using Advanced Schedule Import first.</p>'
            body+='</section><section class="card" id="constraints"><h2>Approvals, inspections & materials</h2><p>Confirmed appointments or deliveries stay open until their result is cleared with evidence.</p>'
            for r in holds+[dict(id=0,version=0,activity_id=0,kind='approval',title='',owner='',needed_date='',expected_date='',state='open',evidence='',record_id=0)]:
                suffix=str(r['id']);body+='<details class="field-row"><summary>'+esc(r['title'] or 'Add a constraint, inspection or procurement item')+(' · '+STATES[r['state']] if r['id'] else '')+'</summary><form class="field-form" method="post" action="'+BASE+'/projects/'+str(pid)+'/constraints">'+b.hidden('constraint_id',r['id'])+b.hidden('version',r['version'])
                for name,label,options in [('activity_id','Activity',[(a['id'],a['name']) for a in activities]),('kind','Type',list(KINDS.items())),('state','Status',list(STATES.items()))]:
                    body+='<label for="hold-'+name+suffix+'">'+label+'</label><select id="hold-'+name+suffix+'" name="'+name+'">'+''.join('<option value="'+str(k)+'"'+(' selected' if k==r[name] else '')+'>'+esc(v)+'</option>' for k,v in options)+'</select>'
                for name,label in [('title','What is needed'),('owner','Responsible person / inspector'),('needed_date','Needed by'),('expected_date','Confirmed appointment / expected delivery'),('record_id','Document reference ID (optional)')]:
                    body+='<label for="hold-'+name+suffix+'">'+label+'</label><input id="hold-'+name+suffix+'" name="'+name+'" value="'+esc(r[name])+'" type="'+('date' if name.endswith('_date') else 'number' if name=='record_id' else 'text')+'"'+(' required' if name=='title' else '')+'>'
                body+='<label for="hold-evidence'+suffix+'">Confirmation, inspection result, order details or clearance evidence</label><textarea id="hold-evidence'+suffix+'" name="evidence" maxlength="4000">'+esc(r['evidence'])+'</textarea><button>Save make-ready record</button></form></details>'
            body+='</section><section class="card"><h2>Schedule analysis & recovery</h2>'+b.docs.hub.open_form(pid,'/schedule','Open schedule and existing recovery tools')+'<p>Daily reports record actual field work. Hours and crew size do not automatically become a completion percentage.</p>'+b.link('/workspace/daily?project_id='+str(pid),'Review actual daily work')+'</section>'
        return b.page('Get trades ready',body)
