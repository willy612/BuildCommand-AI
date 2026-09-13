"""8.13.0: project subcontractor contacts and daily reports with separate crews.

Uses existing subs and daily_reports IDs. Directory links are internal records,
not grants or deliveries. All changes use the sharing company/project locks.
"""
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from fastapi import Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from blueprint_field import esc, digest, packed

VERSION = '8.13.0'
RELEASE = 'Daily Reports by Trade'
DIRECTORY = '/workspace/directory'
DAILY = '/workspace/daily'
log = logging.getLogger('buildcommand.daily_reports')
CONTACT_FIELDS = ('contact_name', 'email', 'phone', 'field_contact', 'field_phone', 'notes')
SITE_FIELDS = ('weather', 'work_completed', 'delays', 'deliveries', 'inspections', 'safety', 'tomorrow_plan')
KINDS = {'RFI': ('project_issues', 'title', 'RFIs'), 'SUBMITTAL': ('submittals', 'title', 'Submittals'),
         'DOCUMENT': ('attachments', 'title', 'Documents'), 'ACTIVITY': ('activities', 'name', 'Schedule activities')}
TRADES = ('Concrete', 'Demolition', 'Doors & hardware', 'Drywall & framing', 'Electrical', 'Fire sprinkler',
          'Flooring & tile', 'HVAC', 'Low voltage', 'Painting', 'Plumbing', 'Roofing', 'Site work', 'Steel', 'Storefront & glazing')
CSS = '''<style>
.bc-daily{max-width:1080px;margin:auto;overflow-wrap:anywhere}.bc-daily h1{font-size:clamp(28px,4vw,40px)}
.bc-daily .daily-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.bc-daily .daily-grid>*{min-width:0}
.bc-daily label{display:block;font-weight:700;margin:14px 0 6px}.bc-daily input:not([type=hidden]),.bc-daily select,.bc-daily textarea{width:100%;box-sizing:border-box;padding:12px;border:1px solid #a7b8cb;border-radius:8px;font:inherit}
.bc-daily textarea{min-height:95px}.bc-daily input[type=checkbox]{width:auto;margin-right:8px}.bc-daily .daily-actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:18px 0}
.bc-daily button,.bc-daily .bc840-button{min-height:46px;white-space:normal}.bc-daily .crew-card{border-left:5px solid #d6a63f}
.bc-daily .daily-note{padding:16px;background:#eef3f8;border-radius:9px}.bc-daily .daily-total{font-size:22px;font-weight:700}
.bc-daily .record{border-top:1px solid #d8e2ed;padding:16px 0}.bc-daily .record:first-of-type{border-top:0}.bc-daily summary{cursor:pointer;font-weight:700;padding:12px 0}
.bc-daily .exact{white-space:pre-wrap;line-height:1.6}.bc-daily :focus-visible{outline:3px solid #ad7100;outline-offset:3px}
@media(max-width:700px){.bc-daily .daily-grid{grid-template-columns:1fr}.bc-daily .daily-actions>*{width:100%;box-sizing:border-box}}
@media print{.no-print{display:none!important}body{background:white!important;color:black!important}.card{break-inside:avoid;border:1px solid #aaa;padding:16px;margin:14px 0}.bc-daily{max-width:none}}
</style>'''


def install(ns):
    service = DailyReports(ns)
    ns['app'].state.daily_reports = service
    service.register()
    return service


class DailyReports:
    def __init__(self, ns):
        self.ns, self.field = ns, ns['app'].state.blueprint_field
        self.db, self.require = self.field.db, self.field.require
        self.routes = []
        self.schema_ready = self.initialize()
        self.legacy_daily = next((r.endpoint for r in ns['app'].routes if getattr(r, 'path', '') == '/daily-report' and 'GET' in (getattr(r, 'methods', None) or set())), None)

    def initialize(self):
        key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        try:
            with self.db(True) as c:
                c.execute('''CREATE TABLE IF NOT EXISTS bc_subcontractor_details(
                    sub_id BIGINT PRIMARY KEY,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    contact_name TEXT NOT NULL DEFAULT '',email TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',
                    field_contact TEXT NOT NULL DEFAULT '',field_phone TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_subcontractor_record_links(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,sub_id BIGINT NOT NULL,
                    kind TEXT NOT NULL,record_id BIGINT NOT NULL,created_by BIGINT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(company_id,project_id,sub_id,kind,record_id))''')
                c.execute('''CREATE TABLE IF NOT EXISTS bc_daily_report_details(
                    report_id BIGINT PRIMARY KEY,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    site_json TEXT NOT NULL,other_people INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,created_by BIGINT NOT NULL,updated_by BIGINT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_daily_trade_entries(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,report_id BIGINT NOT NULL,sub_id BIGINT NOT NULL,
                    name_snapshot TEXT NOT NULL,trade_snapshot TEXT NOT NULL,contact_snapshot TEXT NOT NULL,
                    workers INTEGER NOT NULL,hours_per_person TEXT NOT NULL DEFAULT '',work_area TEXT NOT NULL DEFAULT '',
                    work_completed TEXT NOT NULL DEFAULT '',delays TEXT NOT NULL DEFAULT '',created_by BIGINT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(company_id,project_id,report_id,sub_id))''')
                for name, table in [('sub_details','bc_subcontractor_details'),('sub_records','bc_subcontractor_record_links'),('daily_details','bc_daily_report_details'),('daily_crews','bc_daily_trade_entries')]:
                    c.execute(f'CREATE INDEX IF NOT EXISTS idx_bc_{name}_project ON {table}(company_id,project_id)')
            return True
        except Exception:
            log.exception('Daily report and directory schema initialization failed')
            return False

    def endpoint(self, fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not self.ns['_bc840_user'](): return RedirectResponse('/login', 303)
            try:
                self.require(self.schema_ready, 'Daily report setup is unavailable. Ask your administrator to check this release.', 503)
                return fn(*args, **kwargs)
            except self.ns['_BC850_Problem'] as exc:
                return self.ns['_bc830b_error'](exc.message, exc.status)
            except Exception:
                log.exception('Daily report or directory action failed handler=%s', fn.__name__)
                return self.ns['_bc830b_error']('This action could not be completed. Reload the page and check the saved record before trying again.', 503)
        return wrapped

    def actor(self, c, pid, lock=False):
        return self.field.actor(c, pid, lock)

    def page(self, title, body):
        return self.ns['_bc840_page'](title, CSS+'<div class="bc-daily">'+body+'</div>')

    def link(self, path, label):
        return f'<a class="bc840-button" href="{esc(path)}">{esc(label)}</a>'

    def input(self, name, label, value='', typ='text', required=False, extra='', field_id=None):
        return f'<label for="{field_id or name}">{esc(label)}</label><input id="{field_id or name}" name="{name}" type="{typ}" value="{esc(value)}"'+(' required' if required else '')+' '+extra+'>'

    def area(self, name, label, value='', maximum=6000, field_id=None):
        return f'<label for="{field_id or name}">{esc(label)}</label><textarea id="{field_id or name}" name="{name}" maxlength="{maximum}">{esc(value)}</textarea>'

    def text(self, value, limit, label, required=False):
        result=str(value or '').strip()
        self.require((bool(result) or not required) and len(result)<=limit and '\x00' not in result, f'Enter {label} within {limit} characters.', 400)
        return result

    def day(self, value):
        try:
            self.require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)), 'Enter a valid report date.', 400)
            return date.fromisoformat(value).isoformat()
        except (ValueError, TypeError):
            self.require(False, 'Enter a valid report date.', 400)

    def insert(self, c, table, columns, values):
        self.require(table in {'subs','daily_reports','bc_subcontractor_record_links','bc_daily_trade_entries'}, 'Unsupported record.', 400)
        sql=f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres: return int(c.execute(sql+' RETURNING id', tuple(values)).fetchone()['id'])
        return int(c.execute(sql, tuple(values)).lastrowid)

    def event(self, c, user, pid, action):
        self.ns['_bc850_event'](c, user, pid, None, action)

    def chosen(self, c, pid):
        user=self.ns['_bc850_actor'](c)
        projects=self.ns['_bc850_projects'](c,user)
        if not pid:
            selected=self.ns['_bc840_selected_project'](user)
            pid=selected if any(p['id']==selected for p in projects) else 0
        if pid: user, project=self.actor(c,pid)
        else: project=None
        return user, project, projects

    def project_picker(self,projects,pid,path):
        if len(projects)<2:return ''
        body=f'<form method="get" action="{path}"><label for="daily-project">Project</label><select id="daily-project" name="project_id">'
        for p in projects:
            body+=f'<option value="{p["id"]}"'+(' selected' if p['id']==pid else '')+'>'+esc(p['name'])+'</option>'
        return body+'</select><div class="daily-actions"><button>Open project</button></div></form>'

    def chooser(self, title, projects, path):
        body='<div class="hero"><h1>'+title+'</h1><p>Choose the job you are working on.</p></div>'
        for p in projects:
            body+='<div class="card"><h2>'+esc(p['name'])+'</h2>'+self.link(path+'?project_id='+str(p['id']),'Open this project')+'</div>'
        if not projects: body+='<div class="card"><p>Ask your company administrator to assign a project, or create your first job.</p>'+self.link('/workspace/setup','Open job setup')+'</div>'
        return self.page(title,body)

    def sub(self, c, user, pid, sid, lock=False):
        row=c.execute('SELECT id,project_id,name,trade FROM subs WHERE id=? AND project_id=?'+(self.field.lock if lock else ''),(sid,pid)).fetchone()
        self.require(row is not None, 'This subcontractor is not on this project.', 404)
        result=dict(row)
        meta=c.execute('SELECT * FROM bc_subcontractor_details WHERE sub_id=? AND company_id=? AND project_id=?',(sid,user['company_id'],pid)).fetchone()
        result.update({k:'' for k in CONTACT_FIELDS})
        result['revision']=0
        if meta: result.update({k:dict(meta)[k] for k in (*CONTACT_FIELDS,'revision')})
        return result

    def legacy_profiles(self,c,user,pid):
        if 'project_trade_directory' not in self.ns['_bc800_table_names']():return []
        return [dict(r) for r in c.execute("SELECT * FROM project_trade_directory WHERE company_id=? AND project_id=? ORDER BY trade,id",(user['company_id'],pid)).fetchall() if str(r['company_name'] or '').strip()]

    def import_profile(self,project_id:int,profile_id:int,source_hash:str=Form(...)):
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            profile=next((r for r in self.legacy_profiles(c,user,project_id) if r['id']==profile_id),None)
            self.require(profile is not None,'This saved trade profile is unavailable.',404)
            self.require(digest(packed(profile))==source_hash,'The saved trade profile changed. Reload the directory before importing.',409)
            name=self.text(profile.get('company_name'),160,'the company name',True)
            trade=self.text(profile.get('trade'),100,'the trade',True)
            matches=c.execute('SELECT id FROM subs WHERE project_id=? AND LOWER(TRIM(name))=LOWER(?) AND LOWER(TRIM(trade))=LOWER(?)',(project_id,name,trade)).fetchall()
            self.require(len(matches)<=1,'More than one directory record matches. Open the exact subcontractor to update its contact.',409)
            existing_id=matches[0]['id'] if matches else None
            if existing_id:
                self.require(not c.execute('SELECT sub_id FROM bc_subcontractor_details WHERE sub_id=?',(existing_id,)).fetchone(),'This subcontractor already has a saved contact profile. Open it to review changes before replacing contact information.',409)
            values=[self.text(profile.get(k),limit,k) for k,limit in (('contact_name',120),('email',254),('phone',60),('notes',2000))]
            self.require(not values[1] or bool(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',values[1])),'Correct the email in the saved trade profile before importing.',400)
            sid=existing_id or self.insert(c,'subs','project_id,name,trade',(project_id,name,trade))
            now=self.field.now().isoformat()
            sql='INSERT INTO bc_subcontractor_details(sub_id,company_id,project_id,contact_name,email,phone,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)'
            if self.field.postgres:sql+=' RETURNING sub_id'
            c.execute(sql,(sid,user['company_id'],project_id,*values,user['id'],now,now))
            self.event(c,user,project_id,f'DIRECTORY_PROFILE_IMPORTED:{profile_id}:{sid}')
        return RedirectResponse(f'{DIRECTORY}/projects/{project_id}/subs/{sid}',303)

    def directory(self, project_id:int=0, q:str=''):
        with self.db() as c:
            user,project,projects=self.chosen(c,project_id)
            if not project:return self.chooser('Subcontractor directory',projects,DIRECTORY)
            pid=project['id']
            rows=[self.sub(c,user,pid,r['id']) for r in c.execute('SELECT id FROM subs WHERE project_id=? ORDER BY trade,name,id',(pid,)).fetchall()]
            profiles=self.legacy_profiles(c,user,pid)
        q=self.text(q,120,'a search')
        shown=[r for r in rows if not q or q.casefold() in ' '.join(str(r.get(k) or '') for k in ('name','trade','contact_name','email')).casefold()]
        body='<div class="hero"><div class="eyebrow">'+esc(project['name'])+'</div><h1>Subcontractor directory</h1><p>Keep each company, trade and contact together. Use the same record in daily reports and to attach job records.</p></div>'
        body+=self.project_picker(projects,pid,DIRECTORY)
        body+='<div class="daily-actions">'+self.link(f'{DIRECTORY}/projects/{pid}/new','Add subcontractor')+self.link(f'{DAILY}?project_id={pid}','Daily reports')+'</div>'
        body+=f'<form method="get" action="{DIRECTORY}"><input type="hidden" name="project_id" value="{pid}">'+self.input('q','Find a company or trade',q,extra='maxlength="120"')+'<div class="daily-actions"><button>Find</button></div></form>'
        for s in shown:
            body+='<article class="card"><div class="eyebrow">'+esc(s['trade'])+'</div><h2>'+esc(s['name'])+'</h2><p>'+esc(s['contact_name'] or 'Contact not added')+'</p><p>'+esc(s['email'])+' '+esc(s['phone'])+'</p>'+self.link(f'{DIRECTORY}/projects/{pid}/subs/{s["id"]}','Open subcontractor')+'</article>'
        if not shown:body+='<div class="card"><h2>'+('No matching subcontractors' if q else 'Add your first subcontractor')+'</h2><p>Use the company name, trade and the contact who handles this job.</p></div>'
        if profiles:
            body+='<details class="card"><summary>Use a contact saved in an earlier trade profile</summary><p>Choose the exact company to copy. The original profile and its scope links remain unchanged.</p>'
            for profile in profiles:
                body+=f'<form method="post" action="{DIRECTORY}/projects/{pid}/import-profile/{profile["id"]}"><input type="hidden" name="source_hash" value="{digest(packed(profile))}"><p><strong>'+esc(profile['company_name'])+'</strong> · '+esc(profile['trade'])+'</p><p>'+esc(profile.get('contact_name') or '')+' · '+esc(profile.get('email') or '')+'</p><button>Use this saved contact</button></form>'
            body+='</details>'
        body+='<details class="card"><summary>Existing trade scopes and hubs</summary><p>The earlier trade hubs, standard scopes and inferred trade matches are still available. Directory attachments here are explicit selections.</p>'+self.legacy_form(pid,'directory','Open existing trade hubs')+'</details>'
        return self.page('Subcontractor directory',body)

    def new_sub(self, project_id:int):
        return self.sub_form(project_id,0)

    def sub_form(self, project_id:int, sub_id:int):
        with self.db() as c:
            user,p=self.actor(c,project_id)
            s=self.sub(c,user,project_id,sub_id) if sub_id else dict(id=0,name='',trade='',**{k:'' for k in CONTACT_FIELDS})
        action=f'{DIRECTORY}/projects/{project_id}/subs/'+str(sub_id)+'/save'
        body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+'</div><h1>'+('Edit subcontractor' if sub_id else 'Add subcontractor')+'</h1><p>Use a separate entry for each company and trade working on this job.</p></div><section class="card"><form method="post" action="'+action+'">'
        body+=f'<input type="hidden" name="source_hash" value="{digest(packed(s)) if sub_id else ""}">'
        body+=self.input('name','Subcontractor company',s['name'],required=True,extra='maxlength="160"')
        body+=self.input('trade','Trade',s['trade'],required=True,extra='maxlength="100" list="trade-options"')+'<datalist id="trade-options">'+''.join('<option value="'+esc(t)+'">' for t in TRADES)+'</datalist><div class="daily-grid"><div>'
        body+=self.input('contact_name','Primary contact',s['contact_name'],extra='maxlength="120"')+self.input('email','Email for job correspondence',s['email'],'email',extra='maxlength="254"')+self.input('phone','Phone',s['phone'],'tel',extra='maxlength="60"')+'</div><div>'
        body+=self.input('field_contact','Foreman / field contact',s['field_contact'],extra='maxlength="120"')+self.input('field_phone','Foreman phone',s['field_phone'],'tel',extra='maxlength="60"')+'</div></div>'
        body+='<details><summary>Additional notes</summary>'+self.area('notes','Contact notes',s['notes'],2000)+'</details><p>Saving a contact does not invite an account or grant access to project files.</p><div class="daily-actions"><button>Save subcontractor</button><a href="'+DIRECTORY+'?project_id='+str(project_id)+'">Back to directory</a></div></form></section>'
        return self.page('Subcontractor contact',body)

    def save_sub(self, project_id:int, sub_id:int, name:str=Form(...), trade:str=Form(...), source_hash:str=Form(''),
                 contact_name:str=Form(''), email:str=Form(''), phone:str=Form(''), field_contact:str=Form(''), field_phone:str=Form(''), notes:str=Form('')):
        name=self.text(name,160,'the company name',True);trade=self.text(trade,100,'the trade',True)
        values=[self.text(v,n,label) for v,n,label in ((contact_name,120,'the contact name'),(email,254,'the email'),(phone,60,'the phone'),(field_contact,120,'the foreman name'),(field_phone,60,'the foreman phone'),(notes,2000,'contact notes'))]
        self.require(not values[1] or bool(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',values[1])), 'Enter one valid contact email.',400)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            if sub_id:
                old=self.sub(c,user,project_id,sub_id,True)
                self.require(digest(packed(old))==source_hash,'This contact changed. Reload its profile before saving your changes.',409)
            duplicates=c.execute('SELECT id,name,trade FROM subs WHERE project_id=?',(project_id,)).fetchall()
            key=lambda v:' '.join(v.split()).casefold()
            self.require(not any(r['id']!=sub_id and key(r['name'] or '')==key(name) and key(r['trade'] or '')==key(trade) for r in duplicates),'That company and trade are already listed. Open the existing contact to update it.',409)
            if not sub_id:sub_id=self.insert(c,'subs','project_id,name,trade',(project_id,name,trade))
            else:c.execute('UPDATE subs SET name=?,trade=? WHERE id=? AND project_id=?',(name,trade,sub_id,project_id))
            now=self.field.now().isoformat()
            sql='''INSERT INTO bc_subcontractor_details(sub_id,company_id,project_id,contact_name,email,phone,field_contact,field_phone,notes,created_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sub_id) DO UPDATE SET contact_name=excluded.contact_name,email=excluded.email,phone=excluded.phone,
                field_contact=excluded.field_contact,field_phone=excluded.field_phone,notes=excluded.notes,revision=bc_subcontractor_details.revision+1,updated_at=excluded.updated_at'''
            if self.field.postgres:sql+=' RETURNING sub_id'
            c.execute(sql,(sub_id,user['company_id'],project_id,*values,user['id'],now,now))
            self.event(c,user,project_id,'DIRECTORY_SAVED:'+str(sub_id))
        return RedirectResponse(f'{DIRECTORY}/projects/{project_id}/subs/{sub_id}',303)

    def source(self,c,user,pid,kind,rid):
        self.require(kind in KINDS,'Choose an RFI, submittal, document or schedule activity.',400)
        table=KINDS[kind][0]
        sql=f'SELECT * FROM {table} WHERE id=? AND project_id=?'
        args=[rid,pid]
        if kind=='DOCUMENT':sql+=' AND company_id=?';args.append(user['company_id'])
        if kind=='RFI':sql+=" AND UPPER(issue_type)='RFI'"
        row=c.execute(sql,tuple(args)).fetchone()
        self.require(row is not None,'That record is unavailable in this project.',404)
        return dict(row)

    def sources(self,c,user,pid,kind):
        table=KINDS[kind][0];sql=f'SELECT * FROM {table} WHERE project_id=?';args=[pid]
        if kind=='DOCUMENT':sql+=' AND company_id=?';args.append(user['company_id'])
        if kind=='RFI':sql+=" AND UPPER(issue_type)='RFI'"
        return [dict(r) for r in c.execute(sql+' ORDER BY id DESC',tuple(args)).fetchall()]

    def linked(self,c,user,pid,sid):
        result=[]
        for r in c.execute('SELECT * FROM bc_subcontractor_record_links WHERE company_id=? AND project_id=? AND sub_id=? ORDER BY id',(user['company_id'],pid,sid)).fetchall():
            link=dict(r)
            try:link['record']=self.source(c,user,pid,link['kind'],link['record_id'])
            except self.ns['_BC850_Problem']:link['record']=None
            result.append(link)
        return result

    def notice_date(self,row):
        try:
            raw=str(row.get('start') or '').strip()
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):return None
            start=date.fromisoformat(raw)
            return start,(start-timedelta(days=21))
        except (ValueError,OverflowError):return None

    def sub_detail(self,project_id:int,sub_id:int):
        with self.db() as c:
            user,p=self.actor(c,project_id);s=self.sub(c,user,project_id,sub_id);links=self.linked(c,user,project_id,sub_id)
        base=f'{DIRECTORY}/projects/{project_id}/subs/{sub_id}'
        body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+' · '+esc(s['trade'])+'</div><h1>'+esc(s['name'])+'</h1><p>Contact information and the job records you have attached to this subcontractor.</p></div><div class="daily-actions">'+self.link(base+'/edit','Edit contact')+self.link(base+'/records','Attach job records')+self.link(f'{DAILY}?project_id={project_id}','Daily reports')+'</div>'
        body+='<section class="card"><h2>Job contacts</h2><p><strong>'+esc(s['contact_name'] or 'Primary contact not added')+'</strong><br>'+esc(s['email'] or 'Email not added')+'<br>'+esc(s['phone'])+'</p><p><strong>Foreman:</strong> '+esc(s['field_contact'] or 'Not added')+' · '+esc(s['field_phone'])+'</p>'
        if s['notes']:body+='<details><summary>Contact notes</summary><p class="exact">'+esc(s['notes'])+'</p></details>'
        body+='</section><section class="card"><h2>Attached job records</h2><p>These links organize your records internally. Share approved information through Trade sharing when you are ready.</p>'
        for link in links:
            row=link['record'];label=KINDS[link['kind']][2]
            body+='<div class="record"><strong>'+esc(label)+': '+esc((row or {}).get(KINDS[link['kind']][1]) or 'Record no longer available')+'</strong>'
            if row:
                body+=f'<form method="post" action="{base}/open-record"><input type="hidden" name="link_id" value="{link["id"]}"><button>Open record</button></form>'
            body+='</div>'
        if not links:body+='<p>No records attached yet. Choose Attach job records to select RFIs, submittals, plans or activities.</p>'
        body+='</section><section class="card"><h2>Three weeks before they arrive</h2><p>Attach this subcontractor’s actual schedule activities. Each valid start date shows when its 21-day notice should be prepared. Confirm the date and contact before sending.</p>'
        activities=[r['record'] for r in links if r['kind']=='ACTIVITY' and r['record']]
        for r in activities:
            dates=self.notice_date(r)
            body+='<div class="record"><strong>'+esc(r.get('name') or 'Activity')+'</strong><p>'
            body+=('On site: '+dates[0].isoformat()+' · Prepare notice by: '+dates[1].isoformat()) if dates else 'Start date needed in the schedule.'
            if dates and dates[1]<self.field.now().date():body+=' · Notice date has passed; check current job status.'
            body+='</p></div>'
        if not activities:body+='<p>No schedule activities attached yet.</p>'
        body+='<p class="daily-note">Planning dates only. No notice is queued or sent automatically in this release.</p></section>'+self.link(DIRECTORY+'?project_id='+str(project_id),'Back to directory')
        return self.page('Subcontractor',body)

    def records_form(self,project_id:int,sub_id:int):
        with self.db() as c:
            user,p=self.actor(c,project_id);s=self.sub(c,user,project_id,sub_id);linked=self.linked(c,user,project_id,sub_id)
            choices={kind:self.sources(c,user,project_id,kind) for kind in KINDS}
        current={(r['kind'],r['record_id']) for r in linked};base=f'{DIRECTORY}/projects/{project_id}/subs/{sub_id}'
        body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+'</div><h1>Attach records to '+esc(s['name'])+'</h1><p>Select the records for this company and trade. This does not send them to anyone.</p></div>'
        # Additive links avoid wiping another user's attachments when an older
        # form is submitted. Removal is a separate explicit action per record.
        body+=f'<form method="post" action="{base}/records"><div class="daily-grid">'
        for kind,rows in choices.items():
            body+='<section class="card"><h2>'+KINDS[kind][2]+'</h2>'
            for r in rows:
                title=r.get(KINDS[kind][1]) or r.get('original_name') or kind.title()
                if (kind,r['id']) in current:body+='<p>'+esc(title)+' · Attached</p>'
                else:body+=f'<label><input type="checkbox" name="records" value="{kind}:{r["id"]}">'+esc(title)+'</label>'
            if not rows:body+='<p>No records available for this project.</p>'
            body+='</section>'
        body+='</div><div class="daily-actions"><button>Attach selected records</button><a href="'+base+'">Back to subcontractor</a></div></form>'
        if linked:
            body+='<details class="card"><summary>Remove an attachment link</summary>'
            for r in linked:
                title=(r['record'] or {}).get(KINDS[r['kind']][1]) or 'Unavailable record'
                body+=f'<form method="post" action="{base}/records/{r["id"]}/remove"><p>'+esc(title)+'</p><button>Remove this link</button></form>'
            body+='</details>'
        return self.page('Attach subcontractor records',body)

    def attach(self,project_id:int,sub_id:int,records:list[str]=Form(default=[])):
        self.require(0<len(records)<=200,'Select between 1 and 200 job records.',400)
        parsed=set()
        for raw in records:
            parts=raw.split(':');self.require(len(parts)==2 and parts[0] in KINDS and parts[1].isdigit(),'Choose records from the displayed list.',400)
            parsed.add((parts[0],int(parts[1])))
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);self.sub(c,user,project_id,sub_id,True)
            for kind,rid in sorted(parsed):
                self.source(c,user,project_id,kind,rid)
                exists=c.execute('SELECT id FROM bc_subcontractor_record_links WHERE company_id=? AND project_id=? AND sub_id=? AND kind=? AND record_id=?',(user['company_id'],project_id,sub_id,kind,rid)).fetchone()
                if not exists:
                    self.insert(c,'bc_subcontractor_record_links','company_id,project_id,sub_id,kind,record_id,created_by,created_at',(user['company_id'],project_id,sub_id,kind,rid,user['id'],self.field.now().isoformat()))
                    self.event(c,user,project_id,f'DIRECTORY_ATTACH:{sub_id}:{kind}:{rid}')
        return RedirectResponse(f'{DIRECTORY}/projects/{project_id}/subs/{sub_id}',303)

    def remove_link(self,project_id:int,sub_id:int,link_id:int):
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);self.sub(c,user,project_id,sub_id,True)
            c.execute('DELETE FROM bc_subcontractor_record_links WHERE id=? AND company_id=? AND project_id=? AND sub_id=?',(link_id,user['company_id'],project_id,sub_id))
            self.event(c,user,project_id,f'DIRECTORY_UNLINK:{sub_id}:{link_id}')
        return RedirectResponse(f'{DIRECTORY}/projects/{project_id}/subs/{sub_id}/records',303)

    def select(self,c,user,pid):
        sql='INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id'
        if self.field.postgres:sql+=' RETURNING user_id'
        c.execute(sql,(user['id'],pid))

    def open_record(self,project_id:int,sub_id:int,link_id:int=Form(...)):
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);self.sub(c,user,project_id,sub_id)
            row=c.execute('SELECT * FROM bc_subcontractor_record_links WHERE id=? AND company_id=? AND project_id=? AND sub_id=?',(link_id,user['company_id'],project_id,sub_id)).fetchone()
            self.require(row is not None,'This link is unavailable.',404)
            source=self.source(c,user,project_id,row['kind'],row['record_id']);rid=source['id']
            destination={'RFI':f'/issues/{rid}','SUBMITTAL':f'/submittals/{rid}/brain','DOCUMENT':f'/documents/{rid}/view','ACTIVITY':'/schedule'}[row['kind']]
            self.select(c,user,project_id)
        return RedirectResponse(destination,303)

    def daily_entry(self):
        user=self.ns['_bc840_user']()
        if user and not self.ns['_bc850_manager'](user) and self.legacy_daily:
            value=self.legacy_daily()
            return HTMLResponse(value) if isinstance(value,str) else value
        return self.index()

    def legacy_page(self):
        self.require(self.legacy_daily is not None,'The earlier daily report screen is unavailable.',404)
        value=self.legacy_daily()
        return HTMLResponse(value) if isinstance(value,str) else value

    def legacy_form(self,pid,tool,label):
        return f'<form method="post" action="{DAILY}/projects/{pid}/legacy"><input type="hidden" name="tool" value="{tool}"><button>{esc(label)}</button></form>'

    def open_legacy(self,project_id:int,tool:str=Form(...)):
        destinations={'daily':'/daily-report/legacy','directory':'/subcontractors'}
        self.require(tool in destinations,'Choose the daily report or existing trade hubs.',400)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True);self.select(c,user,project_id)
        return RedirectResponse(destinations[tool],303)

    def index(self,project_id:int=0):
        with self.db() as c:
            user,p,projects=self.chosen(c,project_id)
            if not p:return self.chooser('Daily reports',projects,DAILY)
            pid=p['id']
            rows=[dict(r) for r in c.execute('SELECT id,report_date,manpower FROM daily_reports WHERE project_id=? ORDER BY report_date DESC,id DESC LIMIT 100',(pid,)).fetchall()]
        body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+'</div><h1>Daily reports</h1><p>Record the day, then add each subcontractor who was actually on site. Every company and trade gets its own section.</p></div>'
        body+=self.project_picker(projects,pid,DAILY)
        body+=f'<section class="card"><h2>Today’s report</h2><form method="post" action="{DAILY}/projects/{pid}/create">'+self.input('report_date','Report date',self.field.now().date().isoformat(),'date',True)+'<div class="daily-actions"><button>Start / open daily report</button>'+self.link(DIRECTORY+'?project_id='+str(pid),'Subcontractor directory')+'</div></form><p>If this date already has a report, its most recent report opens.</p></section>'
        body+='<section class="card"><h2>Saved reports</h2>'
        for r in rows:
            body+='<div class="record"><strong>'+esc(r['report_date'])+'</strong> · '+esc(r.get('manpower') or 0)+' people on site · '+self.link(f'{DAILY}/reports/{r["id"]}','Open report')+'</div>'
        if not rows:body+='<p>No daily reports saved for this job yet.</p>'
        body+='</section><details class="card"><summary>Earlier reporting tools</summary><p>Existing report analysis, automatic drafts and PDF exports remain available.</p>'+self.legacy_form(pid,'daily','Open earlier daily report tools')+'</details>'
        return self.page('Daily reports',body)

    def meta_insert(self,c,user,pid,row,other_people):
        now=self.field.now().isoformat();site={k:str(row.get(k) or '') for k in SITE_FIELDS}
        sql='''INSERT INTO bc_daily_report_details(report_id,company_id,project_id,site_json,other_people,created_by,updated_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)'''
        if self.field.postgres:sql+=' RETURNING report_id'
        c.execute(sql,(row['id'],user['company_id'],pid,packed(site),other_people,user['id'],user['id'],now,now))

    def create_report(self,project_id:int,report_date:str=Form(...)):
        day=self.day(report_date)
        with self.db(True) as c:
            user,p=self.actor(c,project_id,True)
            existing=c.execute('SELECT id FROM daily_reports WHERE project_id=? AND report_date=? ORDER BY id DESC LIMIT 1',(project_id,day)).fetchone()
            if existing:rid=existing['id']
            else:
                rid=self.insert(c,'daily_reports','project_id,report_date,weather,manpower,work_completed,delays,deliveries,inspections,safety,tomorrow_plan,created',
                                (project_id,day,'',0,'','','','','','',self.field.now().isoformat()))
                row=dict(c.execute('SELECT * FROM daily_reports WHERE id=? AND project_id=?',(rid,project_id)).fetchone())
                self.meta_insert(c,user,project_id,row,0)
                self.event(c,user,project_id,'DAILY_CREATED:'+str(rid))
        return RedirectResponse(f'{DAILY}/reports/{rid}',303)

    def report(self,c,rid,lock=False):
        user=self.ns['_bc850_actor'](c)
        first=c.execute('SELECT d.project_id FROM daily_reports d JOIN projects p ON p.id=d.project_id WHERE d.id=? AND p.company_id=?',(rid,user['company_id'])).fetchone()
        self.require(first is not None,'This daily report is unavailable.',404)
        user,p=self.actor(c,first['project_id'],lock)
        row=c.execute('SELECT * FROM daily_reports WHERE id=? AND project_id=?'+(self.field.lock if lock else ''),(rid,p['id'])).fetchone()
        self.require(row is not None,'This daily report is unavailable.',404)
        meta=c.execute('SELECT * FROM bc_daily_report_details WHERE report_id=? AND company_id=? AND project_id=?',(rid,user['company_id'],p['id'])).fetchone()
        entries=[dict(r) for r in c.execute('SELECT * FROM bc_daily_trade_entries WHERE report_id=? AND company_id=? AND project_id=? ORDER BY trade_snapshot,name_snapshot,id',(rid,user['company_id'],p['id'])).fetchall()]
        return user,p,dict(row),dict(meta) if meta else None,entries

    def revision(self,meta,revision):
        self.require(meta is not None,'Enable separate trade entries on this report first.',409)
        self.require(meta['revision']==revision,'This report was updated in another window. Reload it before saving your changes.',409)

    def adopt(self,report_id:int,other_people:int=Form(...)):
        self.require(0<=other_people<=10000,'Enter the GC / other crew count from 0 to 10,000.',400)
        with self.db(True) as c:
            user,p,row,meta,entries=self.report(c,report_id,True)
            self.require(meta is None,'This report already has trade entries enabled. Open its current version.',409)
            self.meta_insert(c,user,p['id'],row,other_people)
            # Keep the legacy report exactly as saved until the superintendent
            # confirms site notes or saves the first actual trade entry.
            self.event(c,user,p['id'],'DAILY_BREAKDOWN_ENABLED:'+str(report_id))
        return RedirectResponse(f'{DAILY}/reports/{report_id}',303)

    def sync(self,c,user,pid,rid):
        meta=dict(c.execute('SELECT * FROM bc_daily_report_details WHERE report_id=? AND company_id=? AND project_id=?',(rid,user['company_id'],pid)).fetchone())
        site=json.loads(meta['site_json'])
        entries=[dict(r) for r in c.execute('SELECT * FROM bc_daily_trade_entries WHERE report_id=? AND company_id=? AND project_id=? ORDER BY trade_snapshot,name_snapshot,id',(rid,user['company_id'],pid)).fetchall()]
        work=[site.get('work_completed','')];delays=[site.get('delays','')]
        for e in entries:
            label=e['trade_snapshot']+' — '+e['name_snapshot']
            parts=[label, str(e['workers'])+' people', ('Hours per person: '+e['hours_per_person']) if e['hours_per_person'] else 'Hours not entered']
            if e['work_area']:parts.append('Area: '+e['work_area'])
            if e['work_completed']:parts.append('Work: '+e['work_completed'])
            if e['delays']:delays.append(label+': '+e['delays'])
            work.append('\n'.join(parts))
        total=int(meta['other_people'])+sum(int(e['workers']) for e in entries)
        c.execute('''UPDATE daily_reports SET weather=?,manpower=?,work_completed=?,delays=?,deliveries=?,inspections=?,safety=?,tomorrow_plan=? WHERE id=? AND project_id=?''',
                  (site.get('weather',''),total,'\n\n'.join(x for x in work if x),'\n\n'.join(x for x in delays if x),site.get('deliveries',''),site.get('inspections',''),site.get('safety',''),site.get('tomorrow_plan',''),rid,pid))
        c.execute('UPDATE bc_daily_report_details SET revision=revision+1,updated_by=?,updated_at=? WHERE report_id=? AND company_id=? AND project_id=?',(user['id'],self.field.now().isoformat(),rid,user['company_id'],pid))

    def save_site(self,report_id:int,revision:int=Form(...),other_people:int=Form(0),weather:str=Form(''),work_completed:str=Form(''),delays:str=Form(''),
                  deliveries:str=Form(''),inspections:str=Form(''),safety:str=Form(''),tomorrow_plan:str=Form('')):
        self.require(0<=other_people<=10000,'Enter the GC / other crew count from 0 to 10,000.',400)
        site={k:self.text(v,6000 if k!='weather' else 300,k.replace('_',' ')) for k,v in zip(SITE_FIELDS,(weather,work_completed,delays,deliveries,inspections,safety,tomorrow_plan))}
        with self.db(True) as c:
            user,p,row,meta,entries=self.report(c,report_id,True);self.revision(meta,revision)
            c.execute('UPDATE bc_daily_report_details SET site_json=?,other_people=? WHERE report_id=? AND company_id=? AND project_id=?',(packed(site),other_people,report_id,user['company_id'],p['id']))
            self.sync(c,user,p['id'],report_id);self.event(c,user,p['id'],'DAILY_SITE_SAVED:'+str(report_id))
        return RedirectResponse(f'{DAILY}/reports/{report_id}',303)

    def crew_values(self,workers,hours_per_person,work_area,work_completed,delays):
        self.require(1<=workers<=10000,'Enter the number of people actually on site, from 1 to 10,000.',400)
        raw=self.text(hours_per_person,16,'hours per person')
        if raw:
            try:
                hours=Decimal(raw)
                self.require(hours.is_finite() and 0<=hours<=24 and hours.as_tuple().exponent>=-2,'Enter hours per person from 0 to 24, with up to two decimals.',400)
                raw=format(hours,'f')
            except InvalidOperation:self.require(False,'Enter a valid number of hours per person.',400)
        return workers,raw,self.text(work_area,240,'the work area'),self.text(work_completed,6000,'work completed'),self.text(delays,3000,'trade delays')

    def add_crew(self,report_id:int,revision:int=Form(...),sub_id:int=Form(...),workers:int=Form(...),hours_per_person:str=Form(''),work_area:str=Form(''),work_completed:str=Form(''),delays:str=Form('')):
        values=self.crew_values(workers,hours_per_person,work_area,work_completed,delays)
        with self.db(True) as c:
            user,p,row,meta,entries=self.report(c,report_id,True);self.revision(meta,revision)
            self.require(not any(e['sub_id']==sub_id for e in entries),'That subcontractor is already in this report. Edit its existing section.',409)
            s=self.sub(c,user,p['id'],sub_id,True)
            contact=packed({k:s.get(k,'') for k in ('contact_name','email','phone','field_contact','field_phone')})
            now=self.field.now().isoformat()
            eid=self.insert(c,'bc_daily_trade_entries','company_id,project_id,report_id,sub_id,name_snapshot,trade_snapshot,contact_snapshot,workers,hours_per_person,work_area,work_completed,delays,created_by,created_at,updated_at',
                            (user['company_id'],p['id'],report_id,sub_id,s['name'] or '',s['trade'] or '',contact,*values,user['id'],now,now))
            self.sync(c,user,p['id'],report_id);self.event(c,user,p['id'],f'DAILY_CREW_ADDED:{report_id}:{eid}')
        return RedirectResponse(f'{DAILY}/reports/{report_id}',303)

    def edit_crew(self,report_id:int,entry_id:int,revision:int=Form(...),workers:int=Form(...),hours_per_person:str=Form(''),work_area:str=Form(''),work_completed:str=Form(''),delays:str=Form('')):
        values=self.crew_values(workers,hours_per_person,work_area,work_completed,delays)
        with self.db(True) as c:
            user,p,row,meta,entries=self.report(c,report_id,True);self.revision(meta,revision)
            self.require(any(e['id']==entry_id for e in entries),'This trade entry is unavailable.',404)
            c.execute('UPDATE bc_daily_trade_entries SET workers=?,hours_per_person=?,work_area=?,work_completed=?,delays=?,updated_at=? WHERE id=? AND report_id=? AND company_id=? AND project_id=?',(*values,self.field.now().isoformat(),entry_id,report_id,user['company_id'],p['id']))
            self.sync(c,user,p['id'],report_id);self.event(c,user,p['id'],f'DAILY_CREW_EDITED:{report_id}:{entry_id}')
        return RedirectResponse(f'{DAILY}/reports/{report_id}',303)

    def remove_crew(self,report_id:int,entry_id:int,revision:int=Form(...)):
        with self.db(True) as c:
            user,p,row,meta,entries=self.report(c,report_id,True);self.revision(meta,revision)
            self.require(any(e['id']==entry_id for e in entries),'This trade entry is unavailable.',404)
            c.execute('DELETE FROM bc_daily_trade_entries WHERE id=? AND report_id=? AND company_id=? AND project_id=?',(entry_id,report_id,user['company_id'],p['id']))
            self.sync(c,user,p['id'],report_id);self.event(c,user,p['id'],f'DAILY_CREW_REMOVED:{report_id}:{entry_id}')
        return RedirectResponse(f'{DAILY}/reports/{report_id}',303)

    def crew_fields(self,e=None):
        e=e or {};suffix=str(e.get('id','new'))
        body='<div class="daily-grid"><div>'+self.input('workers','People on site',e.get('workers',''),'number',True,'min="1" max="10000"',field_id='workers-'+suffix)+'</div><div>'
        body+=self.input('hours_per_person','Hours per person (optional)',e.get('hours_per_person',''),'number',False,'min="0" max="24" step="0.01"',field_id='hours-'+suffix)+'</div></div>'
        body+=self.input('work_area','Work area',e.get('work_area',''),extra='maxlength="240"',field_id='area-'+suffix)
        body+=self.area('work_completed','Work completed',e.get('work_completed',''),field_id='work-'+suffix)+self.area('delays','Delays / blockers',e.get('delays',''),3000,field_id='delays-'+suffix)
        return body

    def detail(self,report_id:int):
        with self.db() as c:
            user,p,row,meta,entries=self.report(c,report_id)
            people=[dict(r) for r in c.execute('SELECT id,name,trade FROM subs WHERE project_id=? ORDER BY trade,name,id',(p['id'],)).fetchall()]
        pid=p['id'];base=f'{DAILY}/reports/{report_id}'
        body='<div class="hero"><div class="eyebrow">'+esc(p['name'])+'</div><h1>Daily report · '+esc(row['report_date'])+'</h1><p>Add only the subcontractors who were on site. Save each section before opening another one.</p></div>'
        body+='<div class="daily-actions">'+self.link(base+'/print','Print report')+self.link(base+'/download','Download report')+self.link(DIRECTORY+'?project_id='+str(pid),'Subcontractor directory')+'</div>'
        if not meta:
            body+='<section class="card"><h2>Earlier saved report</h2><p>Existing recorded total: '+esc(row.get('manpower') or 0)+' people.</p><div class="exact">'+esc(self.report_text(p,row,None,[]))+'</div></section>'
            body+=f'<section class="card"><h2>Add separate subcontractor sections</h2><p>Enter only GC / other people who will not be counted in the subcontractor sections. This prevents counting the original combined manpower twice.</p><form method="post" action="{base}/enable">'+self.input('other_people','GC / other people, excluding subcontractor crews','','number',True,'min="0" max="10000"')+'<div class="daily-actions"><button>Enable trade entries</button></div></form><p>The earlier report remains unchanged until you save site notes or a trade entry.</p></section>'
            return self.page('Daily report',body)
        revision=meta['revision'];hidden=f'<input type="hidden" name="revision" value="{revision}">';site=json.loads(meta['site_json'])
        total=meta['other_people']+sum(e['workers'] for e in entries)
        known_hours=sum(Decimal(e['hours_per_person'])*e['workers'] for e in entries if e['hours_per_person'])
        missing=sum(not e['hours_per_person'] for e in entries)
        body+='<section class="card"><h2>People on site</h2><p class="daily-total">'+str(total)+' people · '+str(len(entries))+' subcontractor crews</p><p>'+str(meta['other_people'])+' GC / other people + '+str(sum(e['workers'] for e in entries))+' subcontractor workers.</p>'
        if entries:body+='<p>Recorded subcontractor person-hours: '+format(known_hours,'f')+('. Hours missing for '+str(missing)+' crew(s).' if missing else '.')+'</p>'
        body+='<details><summary>Weather and general site notes</summary><form method="post" action="'+base+'/site">'+hidden+self.input('weather','Weather / site conditions',site.get('weather',''),extra='maxlength="300"')+self.input('other_people','GC / other people, excluding the crews below',meta['other_people'],'number',True,'min="0" max="10000"')
        labels={'work_completed':'General site work (trade work goes below)','delays':'General site delays','deliveries':'Deliveries','inspections':'Inspections','safety':'Safety notes','tomorrow_plan':'Tomorrow’s plan'}
        for key,label in labels.items():body+=self.area(key,label,site.get(key,''),field_id='site-'+key)
        body+='<div class="daily-actions"><button>Save site notes</button></div></form></details></section>'
        if int(row.get('manpower') or 0)!=total:body+='<p class="daily-note">This earlier report still has its original total of '+esc(row.get('manpower') or 0)+' people. Save site notes or a trade entry to apply the breakdown above.</p>'
        present={e['sub_id'] for e in entries};available=[s for s in people if s['id'] not in present]
        body+='<section class="card"><h2>Subcontractors here today</h2><p>Select a company from your directory and enter its crew. A company left out is not recorded as present or absent.</p>'
        if available:
            body+='<form method="post" action="'+base+'/crews">'+hidden+'<label for="daily-sub">Subcontractor and trade</label><select id="daily-sub" name="sub_id" required><option value="" selected disabled>Choose who was here</option>'
            for s in available:body+=f'<option value="{s["id"]}">'+esc(s['trade'] or 'Trade not set')+' · '+esc(s['name'])+'</option>'
            body+='</select>'+self.crew_fields()+'<div class="daily-actions"><button>Add this subcontractor to today</button></div></form>'
        else:body+='<p>'+('All directory companies have entries for this report.' if people else 'Add your subcontractors to this project directory first.')+'</p>'
        body+='</section>'
        for e in entries:
            hours=' · '+e['hours_per_person']+' hours per person' if e['hours_per_person'] else ' · Hours not entered'
            body+='<article class="card crew-card"><div class="eyebrow">'+esc(e['trade_snapshot'])+'</div><h2>'+esc(e['name_snapshot'])+'</h2><p><strong>'+str(e['workers'])+' people'+esc(hours)+'</strong></p><p>Area: '+esc(e['work_area'] or 'Not entered')+'</p><div class="exact">'+esc(e['work_completed'] or 'Work notes not entered')+'</div>'
            if e['delays']:body+='<p><strong>Delays / blockers</strong></p><div class="exact">'+esc(e['delays'])+'</div>'
            contact=json.loads(e['contact_snapshot'])
            body+='<details><summary>Edit this crew</summary><form method="post" action="'+base+f'/crews/{e["id"]}/save">'+hidden+self.crew_fields(e)+'<div class="daily-actions"><button>Save this crew</button></div></form></details>'
            body+='<details><summary>Contact recorded that day / remove entry</summary><p>'+esc(contact.get('field_contact') or contact.get('contact_name') or 'Contact not entered')+' · '+esc(contact.get('field_phone') or contact.get('phone') or '')+'</p><p>Company, trade and contact details are kept as recorded when this entry was added.</p><form method="post" action="'+base+f'/crews/{e["id"]}/remove">'+hidden+'<button>Remove from this day’s report</button></form></details></article>'
        body+='<p>'+self.link(DAILY+'?project_id='+str(pid),'Back to daily reports')+'</p>'
        return self.page('Daily report',body)

    def report_text(self,p,row,meta,entries):
        lines=['BuildCommand AI — Daily Report',str(p['name'])+' · '+str(p.get('number') or ''),'Date: '+str(row['report_date']),'Saved people on site: '+str(row.get('manpower') or 0),'']
        site=json.loads(meta['site_json']) if meta else row
        if meta:lines+=['GC / other people: '+str(meta['other_people']),'']
        for k in SITE_FIELDS:lines += [k.replace('_',' ').title(),str(site.get(k) or 'Not entered'),'']
        for e in entries:
            lines += [e['trade_snapshot']+' — '+e['name_snapshot'],'People: '+str(e['workers']),'Hours per person: '+(e['hours_per_person'] or 'Not entered'),'Work area: '+(e['work_area'] or 'Not entered'),'Work completed: '+(e['work_completed'] or 'Not entered'),'Delays / blockers: '+(e['delays'] or 'Not entered'),'']
        lines += ['Saved working report; later edits may change it.','Built By Willy LaHood © 2026']
        return '\n'.join(lines)

    def print_report(self,report_id:int):
        with self.db() as c:
            user,p,row,meta,entries=self.report(c,report_id)
        body='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily report</title>'+CSS+'</head><body><main class="bc-daily"><div class="no-print daily-actions"><button onclick="window.print()">Print / save PDF</button><a href="'+DAILY+f'/reports/{report_id}">Back to report</a></div><h1>Daily report</h1><div class="exact">'+esc(self.report_text(p,row,meta,entries))+'</div></main></body></html>'
        return HTMLResponse(body,headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin'})

    def download(self,report_id:int):
        with self.db() as c:
            user,p,row,meta,entries=self.report(c,report_id)
        return Response(self.report_text(p,row,meta,entries),media_type='text/plain',headers={'Cache-Control':'no-store','Content-Disposition':f'attachment; filename="daily-report-{report_id}.txt"','X-Content-Type-Options':'nosniff'})

    def evidence(self,c,user,pid):
        if not self.schema_ready:return []
        rows=c.execute('SELECT report_id FROM bc_daily_report_details WHERE company_id=? AND project_id=? ORDER BY report_id DESC LIMIT 5',(user['company_id'],pid)).fetchall()
        result=[]
        for row in rows:
            r=c.execute('SELECT id,report_date,manpower,work_completed,delays,safety FROM daily_reports WHERE id=? AND project_id=?',(row['report_id'],pid)).fetchone()
            if r:result.append(dict(r))
        return result

    def health(self):
        checks={f'{method} {path}':next((r.endpoint is endpoint for r in self.ns['app'].routes if getattr(r,'path','')==path and method in (getattr(r,'methods',None) or set())),False) for method,path,endpoint in self.routes}
        checks.update(directory_and_daily_schema_initialized=self.schema_ready,ask_daily_reports_connected=callable(getattr(self.ns['app'].state.command_center,'report_evidence',None)),first_job_setup_preserved=getattr(self.ns['app'].state,'project_setup',None) is not None,
                      reviewed_actions_preserved=getattr(self.ns['app'].state,'command_actions',None) is not None,
                      form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                for sql in ('SELECT id,project_id,name,trade FROM subs WHERE 1=0','SELECT id,project_id,report_date,manpower,work_completed FROM daily_reports WHERE 1=0',
                            'SELECT sub_id,email,field_contact FROM bc_subcontractor_details WHERE 1=0','SELECT kind,record_id FROM bc_subcontractor_record_links WHERE 1=0',
                            'SELECT report_id,revision,site_json FROM bc_daily_report_details WHERE 1=0','SELECT report_id,sub_id,workers,hours_per_person,contact_snapshot FROM bc_daily_trade_entries WHERE 1=0'):c.execute(sql)
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return {'app':'BuildCommand AI','version':VERSION,'release':RELEASE,'status':'ok' if all(checks.values()) else 'degraded','checks':checks,'passed':sum(checks.values()),'total':len(checks),'data_reset':False,
                'scope':'Schema and active handler checks only. Test actual trade crews, saved totals, report exports, project isolation and linked schedule dates on staging. No notices are sent automatically.'}

    def register(self):
        specs=[('POST',DIRECTORY+'/projects/{project_id}/import-profile/{profile_id}',self.import_profile),('GET',DIRECTORY,self.directory),('GET',DIRECTORY+'/projects/{project_id}/new',self.new_sub),
               ('GET',DIRECTORY+'/projects/{project_id}/subs/{sub_id}',self.sub_detail),('GET',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/edit',self.sub_form),
               ('POST',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/save',self.save_sub),('GET',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/records',self.records_form),
               ('POST',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/records',self.attach),('POST',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/records/{link_id}/remove',self.remove_link),
               ('POST',DIRECTORY+'/projects/{project_id}/subs/{sub_id}/open-record',self.open_record),
               ('GET',DAILY,self.index),('POST',DAILY+'/projects/{project_id}/create',self.create_report),('POST',DAILY+'/projects/{project_id}/legacy',self.open_legacy),
               ('GET',DAILY+'/reports/{report_id}',self.detail),('POST',DAILY+'/reports/{report_id}/enable',self.adopt),('POST',DAILY+'/reports/{report_id}/site',self.save_site),
               ('POST',DAILY+'/reports/{report_id}/crews',self.add_crew),('POST',DAILY+'/reports/{report_id}/crews/{entry_id}/save',self.edit_crew),
               ('POST',DAILY+'/reports/{report_id}/crews/{entry_id}/remove',self.remove_crew),('GET',DAILY+'/reports/{report_id}/print',self.print_report),('GET',DAILY+'/reports/{report_id}/download',self.download),
               ('GET','/daily-report/legacy',self.legacy_page)]
        for method,path,fn in specs:
            endpoint=self.endpoint(fn);self.ns['app'].add_api_route(path,endpoint,methods=[method]);self.routes.append((method,path,endpoint))
        endpoint=self.endpoint(self.daily_entry);self.ns['_bc840_replace']('/daily-report','GET',endpoint);self.routes.append(('GET','/daily-report',endpoint))
        self.ns['app'].add_api_route('/health/daily-trades-8-13-0',self.health,methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add('/health/daily-trades-8-13-0')
