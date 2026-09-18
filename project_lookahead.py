"""BuildCommand AI 8.29.0 — native, reviewed 3/6-week project lookahead.

The existing activities table remains the live schedule. New tables hold only
project calendar/template settings, additional activity detail, drafts and
immutable reviewed revisions. Existing activities are never reseeded/deleted.
"""
import csv
import io
import json
import logging
import secrets
from copy import deepcopy
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from blueprint_field import digest, esc
from command_center import command_json
from lookahead_calendar import (DEFAULT_CALENDAR, WorkCalendar, MAX_ACTIVITIES, day,
    integer, percent, completed, started, window, reflow, dependency_order, dependency_conflicts)
import lookahead_ui as ui

VERSION='8.29.0'
RELEASE='Live Project Lookahead'
BASE='/workspace/schedule'
T='bc824_lookahead_templates'
S='bc824_lookahead_settings'
D='bc824_lookahead_drafts'
A='bc824_lookahead_details'
H='bc824_lookahead_revisions'
log=logging.getLogger('buildcommand.lookahead')

def packed(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str,allow_nan=False)
def encode(v):return digest(packed(v))
def url(pid,**kwargs):return BASE+'?'+urlencode(dict(project_id=pid,**kwargs))
def string(v):return str(v if v is not None else '')

def install(ns):
    service=ProjectLookahead(ns)
    ns['app'].state.project_lookahead=service
    service.register()
    return service

class ProjectLookahead:
    def __init__(self,ns):
        self.ns=ns;self.app=ns['app'];self.b=self.app.state.connected_field
        self.hub=self.app.state.workspace_hub;self.routes=[]
        self.b.schema(T,'name TEXT NOT NULL,version BIGINT NOT NULL,calendar_json TEXT NOT NULL,updated TEXT NOT NULL',',UNIQUE(company_id,project_id)')
        self.b.schema(S,'template_name TEXT NOT NULL,template_version BIGINT NOT NULL,calendar_json TEXT NOT NULL,version BIGINT NOT NULL,configured INTEGER NOT NULL,updated TEXT NOT NULL',',UNIQUE(company_id,project_id)')
        self.b.schema(D,'version BIGINT NOT NULL,base_hash TEXT NOT NULL,payload_json TEXT NOT NULL,updated_by BIGINT NOT NULL,updated TEXT NOT NULL',',UNIQUE(company_id,project_id)')
        self.b.schema(A,'activity_id BIGINT NOT NULL,detail_json TEXT NOT NULL,version BIGINT NOT NULL',',UNIQUE(company_id,project_id,activity_id)')
        self.b.schema(H,'revision BIGINT NOT NULL,reason TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,approved_by BIGINT NOT NULL,approved_name TEXT NOT NULL,created TEXT NOT NULL',',UNIQUE(company_id,project_id,revision)')
        self.b.action('lookahead_publish',self.publish_binding,self.apply_publish)
        self.b.action('lookahead_share',self.share_binding,self.apply_share)

    def actor(self,c,pid,write=False):
        if write:return self.b.actor(c,pid,True)
        user,p,projects=self.hub.user(c,pid)
        self.b.require(p is not None,'Open an assigned project first.',403)
        return user,p

    def resolve(self,pid=0):
        _,p,_=self.app.state.simpler_workday.context(pid)
        return p

    def default(self,c,cid):
        row=c.execute(f'SELECT * FROM {T} WHERE company_id=? AND project_id=0',(cid,)).fetchone()
        return dict(row) if row else dict(name='BuildCommand Standard Lookahead',version=1,calendar_json=packed(DEFAULT_CALENDAR))

    def settings(self,c,u,pid,ensure=False):
        row=c.execute(f'SELECT * FROM {S} WHERE company_id=? AND project_id=?',(u['company_id'],pid)).fetchone()
        if not row:
            t=self.default(c,u['company_id'])
            data=dict(company_id=u['company_id'],project_id=pid,template_name=t['name'],template_version=t['version'],calendar_json=t['calendar_json'],version=1,configured=0,updated=self.b.now().isoformat())
            if ensure:
                c.execute(f'INSERT INTO {S}('+','.join(data)+') VALUES('+','.join('?' for _ in data)+') ON CONFLICT(company_id,project_id) DO NOTHING',tuple(data.values()))
                row=c.execute(f'SELECT * FROM {S} WHERE company_id=? AND project_id=?',(u['company_id'],pid)).fetchone()
            else:row=data
        return dict(row)

    def source(self,c,u,pid,lock=False):
        setting=self.settings(c,u,pid)
        raw=[dict(r) for r in c.execute('SELECT * FROM activities WHERE project_id=? ORDER BY id LIMIT 3001'+(self.b.field.lock if lock else ''),(pid,)).fetchall()]
        self.b.require(len(raw)<=MAX_ACTIVITIES,f'This lookahead supports {MAX_ACTIVITIES} activities. The original schedule remains available under More field tools.',413)
        details=[dict(r) for r in c.execute(f'SELECT * FROM {A} WHERE company_id=? AND project_id=? ORDER BY activity_id',(u['company_id'],pid)).fetchall()]
        # Only records actually present in the live schedule participate; historic
        # sidecars from a legacy deletion are not resurrected or reused.
        assignments=[dict(r) for r in c.execute('SELECT activity_id,sub_id,recipient_id,version FROM bc824_assignments WHERE company_id=? AND project_id=? ORDER BY activity_id',(u['company_id'],pid)).fetchall()]
        return dict(settings=setting,activities=raw,details=details,assignments=assignments)

    def canonical(self,source):
        cal=WorkCalendar.load(json.loads(source['settings']['calendar_json']))
        details={int(r['activity_id']):json.loads(r['detail_json']) for r in source['details']}
        rows=[]
        assigned={int(r['activity_id']):int(r['sub_id']) for r in source.get('assignments',[])}
        for raw in source['activities']:
            aid=int(raw['id']);m=details.get(aid,{})
            start=string(raw.get('start'))[:10];finish=string(raw.get('finish'))[:10]
            try:duration=cal.count(start,finish) if start and finish else None
            except ValueError:duration=None
            # Explicit zero-day milestones survive reload; legacy same-day work
            # is a one-day activity unless the superintendent marks a milestone.
            if m.get('milestone') and start==finish:duration=0
            row=dict(key=str(aid),id=aid,reference=string(raw.get('external_id')) or 'LA-'+str(aid),
                name=string(raw.get('name')),trade=string(raw.get('trade')),sub_id=int(m.get('sub_id',assigned.get(aid,0)) or 0),
                area=string(m.get('area')),requested_start=m.get('requested_start') if m.get('planned_signature')==[start,finish] else start,
                start=start,finish=finish,duration=duration,pct=percent(raw.get('pct') or 0),status=string(raw.get('status')),
                actual_start=string(m.get('actual_start')),actual_finish=string(m.get('actual_finish')),
                blocker=string(m.get('blocker')),notes=string(m.get('notes')),predecessors=[str(x) for x in m.get('predecessors',[])],lag=int(m.get('lag') or 0))
            rows.append(row)
        return dict(calendar=cal.dump(),rows=rows,configured=bool(source['settings']['configured']),notes=[],use_company_default=False)

    def draft(self,c,u,pid):
        row=c.execute(f'SELECT * FROM {D} WHERE company_id=? AND project_id=?',(u['company_id'],pid)).fetchone()
        return dict(row) if row else None

    def token(self,source,draft):return dict(base_hash=encode(source),draft_version=draft['version'] if draft else 0)

    def load_edit(self,c,u,p,form=None):
        self.settings(c,u,p['id'],True)
        source=self.source(c,u,p['id'],True);draft=self.draft(c,u,p['id'])
        if form is not None:
            version=integer(form.get('draft_version',''),0,2147483647,'draft version')
            self.b.require(version==(draft['version'] if draft else 0),'Someone saved another draft. Your entries are preserved below; reload the latest draft before merging your changes.',409)
            self.b.require(form.get('base_hash')==encode(source),'The live schedule changed. Your entries are preserved below; reopen the current schedule before saving.',409)
        if draft:
            self.b.require(draft['base_hash']==encode(source),'The live schedule changed after this draft began. The draft is preserved. Compare it with the live schedule, then discard and reapply your changes.',409)
        return source,draft,json.loads(draft['payload_json']) if draft else self.canonical(source)

    def save_draft(self,c,u,p,source,draft,payload):
        # Identity checks precede all writes. No live dates or progress change here.
        dependency_order(payload['rows'])
        now=self.b.now().isoformat()
        if draft:
            cur=c.execute(f'UPDATE {D} SET version=version+1,payload_json=?,updated_by=?,updated=? WHERE id=? AND version=?',(packed(payload),u['id'],now,draft['id'],draft['version']))
            self.b.require(cur.rowcount==1,'The draft changed. Reopen it before saving.',409)
        else:
            self.b.insert(c,D,dict(company_id=u['company_id'],project_id=p['id'],version=1,base_hash=encode(source),payload_json=packed(payload),updated_by=u['id'],updated=now))
        self.b.event(c,u,p['id'],'Lookahead draft saved',draft['id'] if draft else 0,dict(activity_count=len(payload['rows'])))

    async def form(self,request):
        self.b.origin(request)
        self.b.require(request.headers.get('content-type','').split(';')[0]=='application/x-www-form-urlencoded','Use the schedule form on this page.',415)
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk);self.b.require(size<=65536,'This schedule form is too large.',413);chunks.append(chunk)
        try:return parse_qs(b''.join(chunks).decode('utf-8'),keep_blank_values=True,max_num_fields=160,strict_parsing=False)
        except (ValueError,UnicodeError):self.b.require(False,'The form could not be read. Reload and try again.',400)

    @staticmethod
    def flat(data):return {k:v[-1] for k,v in data.items()}

    def context_data(self,c,u,p,mode='current'):
        source=self.source(c,u,p['id']);draft=self.draft(c,u,p['id'])
        payload=self.canonical(source)
        if mode=='draft':
            self.b.require(self.ns['_bc850_manager'](u),'Only project leaders can review schedule drafts.',403)
            if draft:payload=json.loads(draft['payload_json'])
        return source,draft,payload

    def current_subs(self,c,pid):return {int(r['id']):dict(r) for r in c.execute('SELECT id,name,trade FROM subs WHERE project_id=? ORDER BY name,id',(pid,)).fetchall()}

    def readiness(self,c,u,pid,rows):
        # Read saved evidence only. No GET causes a readiness recomputation, AI
        # call or silent clearance. A missing check remains unverified.
        holds=[dict(r) for r in c.execute("SELECT activity_id,title,state FROM bc824_constraints WHERE company_id=? AND project_id=? AND state<>'cleared'",(u['company_id'],pid)).fetchall()]
        checks=[]
        if 'lookahead_make_ready_checks' in self.ns['_bc800_table_names']():
            checks=[dict(r) for r in c.execute('SELECT activity_id,label,status,detail FROM lookahead_make_ready_checks WHERE project_id=?',(pid,)).fetchall()]
        groups={}
        for r in rows:
            reasons=[h['title'] for h in holds if str(h['activity_id'])==r['key']]
            checkrows=[h for h in checks if str(h['activity_id'])==r['key']]
            reasons += [h['label']+(': '+h['detail'] if h['detail'] else '') for h in checkrows if h['status']=='BLOCKED']
            if r.get('blocker'):reasons.insert(0,r['blocker'])
            groups[r['key']]=dict(label='Blocked' if reasons else 'Check readiness',reasons=list(dict.fromkeys(reasons)))
        return groups

    def index(self,project_id:int=0,weeks:int=3,week:str='',as_of:str='',mode:str='current',view:str='lookahead',page:int=1):
        p=self.resolve(project_id)
        if not p:return self.b.page('Schedule','<h1>Open a job to see its schedule</h1><p>Use Change job in the header.</p>')
        self.b.require(mode in {'current','draft'},'Choose Current or Draft.',400)
        try:
            with self.b.db(True) as c:
                u,p=self.actor(c,p['id']);self.settings(c,u,p['id'],True)
                source,draft,payload=self.context_data(c,u,p,mode);cal=WorkCalendar.load(payload['calendar'])
                today=day(as_of) or cal.today(self.b.now());first=day(week) or today-timedelta(days=today.weekday())
                chosen,counts,end=window(payload['rows'],first.isoformat(),weeks,today.isoformat(),view)
                pages=max(1,(len(chosen)+49)//50);page=integer(page,1,pages,'page')
                states=self.readiness(c,u,p['id'],chosen[(page-1)*50:page*50])
                model=dict(u=u,p=p,settings=source['settings'],source=source,draft=draft,payload=payload,
                    weeks=weeks,week=first.isoformat(),as_of=today.isoformat(),end=end,mode=mode,view=view,page=page,pages=pages,
                    rows=chosen[(page-1)*50:page*50],counts=counts,states=states,subs=self.current_subs(c,p['id']),
                    manager=self.ns['_bc850_manager'](u),stale=bool(draft and draft['base_hash']!=encode(source)))
            return self.b.page('Project lookahead',ui.index(self,model))
        except ValueError as exc:return self.error(str(exc),400,p['id'])

    def editor(self,project_id:int,key:str='new',posted=None,error='',status=200):
        with self.b.db(True) as c:
            u,p=self.actor(c,project_id,True);self.settings(c,u,project_id,True)
            source,draft,payload=self.context_data(c,u,p,'draft')
            if key=='new':
                row=dict(key='n'+secrets.token_hex(16),id=0,reference='',name='',trade='',sub_id=0,area='',requested_start='',start='',finish='',duration=1,pct=0,actual_start='',actual_finish='',blocker='',notes='',predecessors=[],lag=0)
            else:
                row=next((deepcopy(r) for r in payload['rows'] if r['key']==key),None)
                self.b.require(row is not None,'This activity is not in this project draft.',404)
            tokens=self.token(source,draft)
            if posted:
                row.update({k:v for k,v in posted.items() if k in row or k in {'requested_start','duration','lag'}})
                if isinstance(row.get('predecessors'),str):row['predecessors']=[v.strip() for v in row['predecessors'].split(',') if v.strip()]
                tokens={k:posted.get(k,v) for k,v in tokens.items()}
            model=dict(u=u,p=p,row=row,rows=payload['rows'],calendar=payload['calendar'],subs=self.current_subs(c,project_id),tokens=tokens,error=error,
                stale=bool(draft and draft['base_hash']!=encode(source)))
        response=self.b.page('Edit schedule activity',ui.editor(self,model));response.status_code=status;return response

    def edit_page(self,project_id:int,key:str='new'):return self.editor(project_id,key)

    def normalize_activity(self,c,u,p,form,old):
        b=self.b
        row=deepcopy(old)
        for name,limit,label,required in [('reference',60,'an activity reference',False),('name',240,'an activity description',True),('trade',100,'a trade',False),('area',140,'an area of work',False),('blocker',2000,'the blocker',False),('notes',4000,'the activity notes',False)]:
            row[name]=b.text(form.get(name,''),limit,label,required)
        row['sub_id']=integer(form.get('sub_id','0'),0,2147483647,'subcontractor')
        if row['sub_id']:
            subs=self.current_subs(c,p['id']);b.require(row['sub_id'] in subs,'Choose a subcontractor on this project.',403)
        row['duration']=integer(form.get('duration',''),0,1000,'duration in workdays')
        row['requested_start']=form.get('requested_start','');day(row['requested_start'])
        row['pct']=percent(form.get('pct','0'))
        for key in ('actual_start','actual_finish'):
            row[key]=form.get(key,'');day(row[key])
        b.require(not row['actual_finish'] or bool(row['actual_start']),'Enter the actual start before the actual finish.',400)
        b.require(not row['actual_finish'] or row['actual_finish']>=row['actual_start'],'Actual finish cannot be before actual start.',400)
        # Progress percentages are explicit observations. Actual finish implies
        # completion, but 100% alone does not invent an actual finish date.
        if row['actual_finish']:row['pct']=100.0
        if not old.get('status') or any(old.get(k)!=row.get(k) for k in ('pct','actual_start','actual_finish')):
            row['status']='COMPLETE' if row['pct']>=100 else 'IN_PROGRESS' if row['pct']>0 or row['actual_start'] else 'NOT_STARTED'
        row['predecessors']=[x.strip() for x in form.get('predecessors','').split(',') if x.strip()]
        row['lag']=integer(form.get('lag','0'),0,365,'predecessor lag in workdays')
        if not row['requested_start']:row.update(start='',finish='')
        return row

    async def save_activity(self,project_id:int,request:Request):
        form_data=await self.form(request);form=self.flat(form_data);form['predecessors']=','.join(form_data.get('predecessors',[]));key=form.get('key','');isnew=key.startswith('n')
        import re
        self.b.require(bool(re.fullmatch(r'(?:n[0-9a-f]{32}|[1-9][0-9]{0,18})',key)),'Reopen the activity editor.',400)
        try:
            with self.b.db(True) as c:
                u,p=self.actor(c,project_id,True);source,draft,payload=self.load_edit(c,u,p,form)
                old=next((r for r in payload['rows'] if r['key']==key),None)
                self.b.require(old is not None or isnew,'This activity is unavailable.',404)
                if old is None:
                    self.b.require(len(payload['rows'])<MAX_ACTIVITIES,'The project activity limit has been reached.',413)
                    old=dict(key=key,id=0,start='',finish='',actual_start='',actual_finish='')
                row=self.normalize_activity(c,u,p,form,old)
                if not row['reference'] and not row['id']:row['reference']='LA-'+key[-8:].upper()
                if row['reference']:
                    self.b.require(not any(r['key']!=key and r['reference']==row['reference'] for r in payload['rows']),'That activity reference is already used in this project.',400)
                proposed=[row if r['key']==key else r for r in payload['rows']]
                if not any(r['key']==key for r in proposed):proposed.append(row)
                cal=WorkCalendar.load(payload['calendar'])
                today=cal.today(self.b.now()).isoformat()
                self.b.require(all(not row.get(k) or row[k]<=today for k in ('actual_start','actual_finish')),'Actual dates cannot be in the future for this project timezone.',400)
                changed=set(k for k in ['requested_start','duration','predecessors','lag'] if old.get(k)!=row.get(k))
                if row['requested_start'] and (changed or isnew):row['start'],row['finish']=cal.plan(row['requested_start'],row['duration'])
                rows,notes=reflow(proposed,cal,[key] if changed or isnew else [])
                payload.update(rows=rows,notes=notes)
                self.save_draft(c,u,p,source,draft,payload)
            return RedirectResponse(url(project_id,mode='draft'),303)
        except (ValueError,self.ns['_BC850_Problem']) as exc:
            status=getattr(exc,'status',400)
            if status not in {400,409}:raise
            message=getattr(exc,'message',str(exc))
            # Re-authorize when rendering. Submitted visible values are kept,
            # but stale revision tokens are not silently replaced.
            return self.editor(project_id,'new' if isnew and not self._has_draft_key(project_id,key) else key,posted=form,error=message,status=status)

    def _has_draft_key(self,pid,key):
        with self.b.db() as c:
            u,p=self.actor(c,pid,True);d=self.draft(c,u,pid)
            return bool(d and any(r['key']==key for r in json.loads(d['payload_json'])['rows']))

    def calendar_page(self,project_id:int,posted=None,error='',status=200):
        with self.b.db(True) as c:
            u,p=self.actor(c,project_id,True);self.settings(c,u,project_id,True)
            source,draft,payload=self.context_data(c,u,p,'draft')
            values=dict(weekdays=payload['calendar']['weekdays'],timezone=payload['calendar']['timezone'],holidays='\n'.join(payload['calendar']['holidays']),extra_workdays='\n'.join(payload['calendar']['extra_workdays']))
            tokens=self.token(source,draft)
            if posted:
                values.update(posted);tokens={k:posted.get(k,v) for k,v in tokens.items()}
            model=dict(u=u,p=p,values=values,tokens=tokens,error=error,admin=self.ns['_bc840_tier'](u) in {'owner','admin'})
        response=self.b.page('Project work calendar',ui.calendar(self,model));response.status_code=status;return response

    def calendar_get(self,project_id:int):return self.calendar_page(project_id)

    async def save_calendar(self,project_id:int,request:Request):
        data=await self.form(request);form=self.flat(data)
        try:
            values=dict(weekdays=[integer(v,0,6,'weekday') for v in data.get('weekdays',[])],
                holidays=[x.strip() for x in form.get('holidays','').replace(',','\n').splitlines() if x.strip()],
                extra_workdays=[x.strip() for x in form.get('extra_workdays','').replace(',','\n').splitlines() if x.strip()],
                timezone=form.get('timezone','UTC'))
            cal=WorkCalendar.load(values)
            with self.b.db(True) as c:
                u,p=self.actor(c,project_id,True);source,draft,payload=self.load_edit(c,u,p,form)
                use_default=form.get('use_company_default')=='yes'
                self.b.require(not use_default or self.ns['_bc840_tier'](u) in {'owner','admin'},'Only company administrators can set a company template.',403)
                if values!=payload['calendar']:
                    payload['rows'],payload['notes']=reflow(payload['rows'],cal,[],calendar_change=True)
                payload.update(calendar=cal.dump(),configured=True,use_company_default=use_default)
                self.save_draft(c,u,p,source,draft,payload)
            return RedirectResponse(url(project_id,mode='draft'),303)
        except (ValueError,self.ns['_BC850_Problem']) as exc:
            if getattr(exc,'status',400) not in {400,409}:raise
            form['weekdays']=data.get('weekdays',[])
            return self.calendar_page(project_id,posted=form,error=getattr(exc,'message',str(exc)),status=getattr(exc,'status',400))

    def changes(self,before,after):
        old={r['key']:r for r in before['rows']};changes=[]
        for row in after['rows']:
            previous=old.get(row['key'])
            if previous!=row:changes.append(dict(before=previous,after=row))
        return changes

    def publish_binding(self,c,u,p,data):
        source=self.source(c,u,p['id'],True);draft=self.draft(c,u,p['id'])
        self.b.require(draft is not None and draft['id']==data['draft_id'] and draft['version']==data['draft_version'],'The draft changed. Review it again.',409)
        self.b.require(draft['base_hash']==encode(source),'The current schedule changed. Your draft is preserved; reconcile it before publishing.',409)
        payload=json.loads(draft['payload_json']);cal=WorkCalendar.load(payload['calendar'])
        conflicts=dependency_conflicts(payload['rows'],cal)
        self.b.require(not conflicts,' '.join(conflicts[:3]),409)
        subs=self.current_subs(c,p['id'])
        for row in payload['rows']:
            self.b.require(not row['sub_id'] or row['sub_id'] in subs,'An assigned subcontractor is no longer on this project. Correct the draft first.',409)
        template=None
        if payload['use_company_default']:
            self.b.require(self.ns['_bc840_tier'](u) in {'owner','admin'},'Only company administrators can approve a company template.',403)
            template=self.default(c,u['company_id'])
        return dict(source=source,draft=draft,subs=subs,template=template)

    async def review(self,project_id:int,request:Request):
        form=self.flat(await self.form(request))
        try:
            reason=self.b.text(form.get('reason'),2000,'a revision description',True)
            with self.b.db(True) as c:
                u,p=self.actor(c,project_id,True);draft=self.draft(c,u,project_id)
                self.b.require(draft is not None,'There is no saved draft to review.',409)
                self.b.require(draft['version']==integer(form.get('draft_version',''),1,2147483647,'draft version'),'The draft changed. Review its latest version.',409)
                data=dict(draft_id=draft['id'],draft_version=draft['version'],reason=reason)
                binding=self.publish_binding(c,u,p,data);before=self.canonical(binding['source']);after=json.loads(draft['payload_json'])
                body=ui.review(self,p,before,after,reason,self.changes(before,after))
                return self.b.prepare(c,u,p,request,'lookahead_publish',data,'Review schedule revision',body,url(project_id,mode='draft'))
        except ValueError as exc:return self.error(str(exc),400,project_id)

    def apply_publish(self,c,u,p,data):
        bind=self.publish_binding(c,u,p,data);source=bind['source'];before=self.canonical(source);after=json.loads(bind['draft']['payload_json'])
        changed=self.changes(before,after);mapping={r['key']:r['id'] for r in after['rows'] if r['id']}
        for change in changed:
            row=change['after']
            vals=(row['reference'],row['name'],row['trade'],row['start'] or None,row['finish'] or None,row['pct'],row['status'])
            if row['id']:
                cur=c.execute('UPDATE activities SET external_id=?,name=?,trade=?,start=?,finish=?,pct=?,status=? WHERE id=? AND project_id=?',vals+(row['id'],p['id']))
                self.b.require(cur.rowcount==1,'An activity changed. Prepare a new review.',409)
            else:
                statement='INSERT INTO activities(external_id,name,trade,start,finish,pct,status,project_id) VALUES(?,?,?,?,?,?,?,?)'
                result=c.execute(statement+(' RETURNING id' if self.b.field.postgres else ''),vals+(p['id'],))
                mapping[row['key']]=int(result.fetchone()['id'] if self.b.field.postgres else result.lastrowid)
        for change in changed:
            row=change['after'];aid=mapping[row['key']]
            detail={k:row[k] for k in ['sub_id','area','requested_start','actual_start','actual_finish','blocker','notes','lag']}
            detail.update(predecessors=[mapping[k] for k in row['predecessors']],milestone=row['duration']==0,planned_signature=[row['start'],row['finish']])
            c.execute(f'INSERT INTO {A}(company_id,project_id,activity_id,detail_json,version) VALUES(?,?,?,?,1) ON CONFLICT(company_id,project_id,activity_id) DO UPDATE SET detail_json=excluded.detail_json,version={A}.version+1',(u['company_id'],p['id'],aid,packed(detail)))
        c.execute(f'UPDATE {S} SET calendar_json=?,configured=?,version=version+1,updated=? WHERE company_id=? AND project_id=?',(packed(after['calendar']),int(after['configured']),self.b.now().isoformat(),u['company_id'],p['id']))
        if after['use_company_default']:
            # Only reusable weekly rules/timezone become company defaults.
            # Project holidays, exceptional Saturdays, dates and activities do not.
            reusable=dict(after['calendar'],holidays=[],extra_workdays=[])
            c.execute(f'INSERT INTO {T}(company_id,project_id,name,version,calendar_json,updated) VALUES(?,0,?,2,?,?) ON CONFLICT(company_id,project_id) DO UPDATE SET calendar_json=excluded.calendar_json,version={T}.version+1,updated=excluded.updated',(u['company_id'],'Company Lookahead',packed(reusable),self.b.now().isoformat()))
        revision=int(c.execute(f'SELECT COALESCE(MAX(revision),0)+1 AS n FROM {H} WHERE company_id=? AND project_id=?',(u['company_id'],p['id'])).fetchone()['n'])
        saved=self.canonical(self.source(c,u,p['id']))
        rid=self.b.insert(c,H,dict(company_id=u['company_id'],project_id=p['id'],revision=revision,reason=data['reason'],before_json=packed(before),after_json=packed(saved),approved_by=u['id'],approved_name=u.get('display_name') or u['email'],created=self.b.now().isoformat()))
        c.execute(f'DELETE FROM {D} WHERE id=? AND company_id=? AND project_id=?',(bind['draft']['id'],u['company_id'],p['id']))
        return BASE+'/projects/'+str(p['id'])+'/revisions/'+str(rid)

    async def discard(self,project_id:int,request:Request):
        form=self.flat(await self.form(request));self.b.require(form.get('confirmed')=='yes','Confirm discarding the saved draft.',400)
        try:version=integer(form.get('draft_version',''),1,2147483647,'draft version')
        except ValueError as exc:return self.error(str(exc),400,project_id)
        with self.b.db(True) as c:
            u,p=self.actor(c,project_id,True);draft=self.draft(c,u,project_id)
            self.b.require(draft is not None and draft['version']==version,'The draft changed. Reopen it before discarding.',409)
            self.b.event(c,u,project_id,'Lookahead draft discarded',draft['id'],json.loads(draft['payload_json']))
            c.execute(f'DELETE FROM {D} WHERE id=? AND company_id=? AND project_id=?',(draft['id'],u['company_id'],project_id))
        return RedirectResponse(url(project_id),303)

    def history(self,project_id:int,revision_id:int=0):
        with self.b.db() as c:
            u,p=self.actor(c,project_id)
            rows=[dict(r) for r in c.execute(f'SELECT id,revision,reason,approved_name,created FROM {H} WHERE company_id=? AND project_id=? ORDER BY revision DESC LIMIT 100',(u['company_id'],project_id)).fetchall()]
            record=None
            if revision_id:
                record=c.execute(f'SELECT * FROM {H} WHERE id=? AND company_id=? AND project_id=?',(revision_id,u['company_id'],project_id)).fetchone()
                self.b.require(record is not None,'This revision is unavailable in this project.',404);record=dict(record)
        return self.b.page('Schedule revision history',ui.history(self,p,rows,record))

    def export(self,project_id:int,weeks:int=3,week:str='',as_of:str='',view:str='lookahead'):
        try:
            with self.b.db() as c:
                u,p=self.actor(c,project_id);source=self.source(c,u,project_id);payload=self.canonical(source);cal=WorkCalendar.load(payload['calendar'])
                report=day(as_of) or cal.today(self.b.now());first=day(week) or report-timedelta(days=report.weekday())
                rows,_,end=window(payload['rows'],first.isoformat(),weeks,report.isoformat(),view);subs=self.current_subs(c,project_id)
            out=io.StringIO(newline='');writer=csv.writer(out)
            def safe(v):
                v=string(v)
                return "'"+v if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n')) else v
            writer.writerow([safe(x) for x in ['BuildCommand AI','Current saved schedule',p['name'],p.get('number',''),first.isoformat(),end]])
            writer.writerow(['Reference','Subcontractor','Trade','Area','Activity','Workdays','Start','Finish','Percent complete','Actual start','Actual finish','Actual workdays','Start variance (workdays)','Finish variance (workdays)','Blocker','Notes'])
            for r in rows:
                actual=cal.count(r['actual_start'],r['actual_finish']) if r['actual_start'] and r['actual_finish'] else ''
                sv=cal.variance(r['start'],r['actual_start']) if r['start'] and r['actual_start'] else ''
                fv=cal.variance(r['finish'],r['actual_finish']) if r['finish'] and r['actual_finish'] else ''
                writer.writerow([safe(v) for v in [r['reference'],subs.get(r['sub_id'],{}).get('name',''),r['trade'],r['area'],r['name'],r['duration'],r['start'],r['finish'],r['pct'],r['actual_start'],r['actual_finish'],actual,sv,fv,r['blocker'],r['notes']]])
            return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="BuildCommand-project-{project_id}-{weeks}-week.csv"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        except ValueError as exc:return self.error(str(exc),400,project_id)

    def share_page(self,project_id:int,weeks:int=3,week:str=''):
        try:
            self.b.require(weeks in {3,6},'Choose three or six weeks.',400)
            day(week)
        except ValueError as exc:return self.error(str(exc),400,project_id)
        with self.b.db() as c:
            u,p=self.actor(c,project_id,True);source=self.source(c,u,project_id);payload=self.canonical(source);cal=WorkCalendar.load(payload['calendar'])
            report=cal.today(self.b.now());first=day(week) or report-timedelta(days=report.weekday())
            rows,_,end=window(payload['rows'],first.isoformat(),weeks,report.isoformat(),'lookahead')
            table=self.ns['_bc850_member_table']()
            recipients=[dict(r) for r in c.execute(f'SELECT u.id,u.display_name,u.email,u.role FROM users u JOIN {table} m ON m.user_id=u.id WHERE m.project_id=? AND u.company_id=? ORDER BY u.email',(project_id,u['company_id'])).fetchall() if self.ns['_bc840_tier'](dict(r))=='trade']
            model=dict(p=p,rows=rows,recipients=recipients,weeks=weeks,week=first.isoformat(),end=end,draft=self.draft(c,u,project_id))
        return self.b.page('Share a trade lookahead',ui.share(self,model))

    def share_binding(self,c,u,p,data):
        self.b.require(not self.draft(c,u,p['id']),'Publish or discard the pending schedule draft before preparing a trade issue.',409)
        source=self.source(c,u,p['id'],True);rows=self.canonical(source)['rows'];ids=data['activity_ids']
        self.b.require(0<len(ids)<=100 and len(ids)==len(set(ids)),'Choose 1 to 100 distinct activities.',400)
        selected=[next((r for r in rows if r['id']==aid),None) for aid in ids]
        self.b.require(all(r is not None for r in selected),'An activity is unavailable in this project.',409)
        return dict(settings=source['settings'],rows=selected,delivery=self.app.state.field_delivery.binding(c,u,p,data['delivery']))

    async def share_review(self,project_id:int,request:Request):
        data=await self.form(request);f=self.flat(data)
        try:
            weeks=integer(f.get('weeks','3'),3,6,'view weeks');self.b.require(weeks in {3,6},'Choose three or six weeks.',400)
            first=day(f.get('week'),True);end=first+timedelta(days=weeks*7-1);day(end.isoformat(),True)
            ids=[integer(v,1,9223372036854775807,'activity') for v in data.get('activity_ids',[])]
            recipient=integer(f.get('recipient_id',''),1,9223372036854775807,'recipient');due=day(f.get('due_date',''))
            with self.b.db(True) as c:
                u,p=self.actor(c,project_id,True);source=self.source(c,u,project_id);rows=self.canonical(source)['rows']
                chosen=[next((r for r in rows if r['id']==aid),None) for aid in ids]
                self.b.require(all(r is not None for r in chosen),'Choose activities from this project.',403)
                lines=[f'{weeks}-week lookahead | {first.isoformat()} through {end.isoformat()}', 'Snapshot of the reviewed schedule. Please confirm crew, materials and start.']
                for r in chosen:lines.append(f"{r['reference']} | {r['name']} | {r['trade']} | {r['area']} | {r['start'] or 'Unscheduled'} to {r['finish'] or 'Unscheduled'} | {r['pct']:g}% complete")
                note=self.b.text(f.get('message',''),2000,'trade instructions')
                if note:lines += ['',note]
                message=self.b.text('\n'.join(lines),12000,'lookahead instructions',True)
                package=dict(title=p['name'][:160]+f' — {weeks}-week lookahead',message=message,recipient_id=recipient,purpose='lookahead',due_date=due.isoformat() if due else '',expires=(self.b.now()+timedelta(days=30)).isoformat(),file_ids=[],notify_email=False)
                payload=dict(activity_ids=ids,delivery=package)
                binding=self.share_binding(c,u,p,payload)
                body=self.app.state.field_delivery.preview_body(binding['delivery'],package)
                return self.b.prepare(c,u,p,request,'lookahead_share',payload,'Review trade lookahead',body,BASE+'/projects/'+str(project_id)+'/share?'+urlencode(dict(weeks=weeks,week=first.isoformat())))
        except ValueError as exc:return self.error(str(exc),400,project_id)

    def apply_share(self,c,u,p,data):
        self.share_binding(c,u,p,data)
        return self.app.state.field_delivery.apply(c,u,p,data['delivery'])

    def home_card(self,c,u,p):
        source=self.source(c,u,p['id']);payload=self.canonical(source);cal=WorkCalendar.load(payload['calendar']);today=cal.today(self.b.now());first=today-timedelta(days=today.weekday());rows,counts,_=window(payload['rows'],first.isoformat(),3,today.isoformat())
        due=[r for r in rows if r['overdue'] or (not completed(r) and r.get('start')==today.isoformat())][:3]
        body='<section class="sw-card la-home"><h2>Your job schedule</h2><p>'+str(counts['lookahead'])+' activities in the 3-week view · '+str(counts['carryover'])+' carryover</p>'
        for r in due:body+='<p><strong>'+esc(r['name'])+'</strong> — '+('Overdue; confirm the next step.' if r['overdue'] else 'Planned to start today; confirm readiness.')+'</p>'
        return body+'<a class="sw-primary" href="'+url(p['id'])+'">Open lookahead</a></section>'

    def error(self,message,status,pid):
        r=self.b.page('Schedule needs attention','<section class="card"><h1>Review this before continuing</h1><p role="alert">'+esc(message)+'</p><p>No live schedule changes were made by this request.</p><a href="'+url(pid,mode='draft')+'">Back to saved draft</a></section>');r.status_code=status;return r

    def register(self):
        specs=[(BASE,'GET',self.index),(BASE+'/projects/{project_id}/activity','GET',self.edit_page),
            (BASE+'/projects/{project_id}/activity','POST',self.save_activity),(BASE+'/projects/{project_id}/calendar','GET',self.calendar_get),
            (BASE+'/projects/{project_id}/calendar','POST',self.save_calendar),(BASE+'/projects/{project_id}/review','POST',self.review),
            (BASE+'/projects/{project_id}/discard','POST',self.discard),(BASE+'/projects/{project_id}/history','GET',self.history),
            (BASE+'/projects/{project_id}/revisions/{revision_id}','GET',self.history),(BASE+'/projects/{project_id}/export.csv','GET',self.export),
            (BASE+'/projects/{project_id}/share','GET',self.share_page),(BASE+'/projects/{project_id}/share','POST',self.share_review)]
        for path,method,fn in specs:
            self.b.route(path,method,fn);self.routes.append(self.b.routes[-1])
        health='/health/project-lookahead-8-29-0'
        self.ns['_bc840_replace'](health,'GET',self.health);self.ns['_runtime'].PUBLIC_PATHS.add(health)
        self.ns['BUILD_COMMAND_RELEASE']=VERSION;self.ns['BUILD_COMMAND_RELEASE_NAME']=RELEASE;self.app.version=VERSION

    def health(self):
        first={}
        for r in self.app.routes:
            for method in getattr(r,'methods',None) or []:first.setdefault((r.path,method),r.endpoint)
        checks={method+' '+path:first.get((path,method)) is fn for path,method,fn in self.routes}
        checks.update(installed=getattr(self.app.state,'project_lookahead',None) is self,
            release_active=self.app.version==VERSION,calendar_engine=WorkCalendar.load(DEFAULT_CALENDAR).plan('2026-09-21',5)==('2026-09-21','2026-09-25'),
            reviewed_publish='lookahead_publish' in self.b.actions,reviewed_trade_sharing='lookahead_share' in self.b.actions,
            existing_activities_source='activities' in str(self.source.__code__.co_consts),three_and_six_views=(window([], '2026-12-21',3,'2026-12-21')[2]=='2027-01-10' and window([], '2026-12-21',6,'2026-12-21')[2]=='2027-01-31'),form_origin_guard=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'],
            project_startup_guard=first.get(('/project-startup','GET')) is self.ns['_bc750_project_startup_page'],
            drawings_and_takeoff=bool(getattr(self.app.state,'drawing_takeoff',None)),simpler_workday=bool(getattr(self.app.state,'simpler_workday',None)))
        try:
            with self.b.db() as c:
                for t in [S,D,A,T,H]:c.execute('SELECT id,company_id,project_id FROM '+t+' WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        ok=all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if ok else 'attention',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Installation and bounded calculation checks only. Verify your real project, database, calendar, drafts and trade permissions on staging. No dates change and no messages are sent by this health check.'),status_code=200 if ok else 503)
