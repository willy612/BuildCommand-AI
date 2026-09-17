"""8.27.0: saved manual takeoffs and exact, reviewed estimator quantity transfer."""
import csv
import io
import json
import math
import re
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from blueprint_field import esc, digest
from command_center import command_json
from drawing_register import js
from takeoff_geometry import calculate, scale_stamp, totals, OUTPUT
from takeoff_ui import STYLE, PANEL, SCRIPT
from takeoff_rates import RateBook, price

VERSION='8.27.0'
RELEASE='Scaled Drawing Takeoff'
BASE='/workspace/drawing-takeoff'
G='bc824_quantity_groups'
M='bc824_quantity_marks'
T='bc824_quantity_transfers'

def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def number(value,lo=0,hi=100):
    if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Enter a valid number within the allowed range.')
    return value

def install(ns):
    s=DrawingTakeoff(ns);ns['app'].state.drawing_takeoff=s;s.register();return s

class DrawingTakeoff:
    def __init__(self,ns):
        self.ns=ns;self.app=ns['app'];self.b=self.app.state.connected_field;self.scale=self.app.state.drawing_scale
        self.b.schema(G,"name TEXT NOT NULL,trade TEXT NOT NULL,kind TEXT NOT NULL,unit TEXT NOT NULL,waste DOUBLE PRECISION NOT NULL,material_rate DOUBLE PRECISION NOT NULL,labor_rate DOUBLE PRECISION NOT NULL,version BIGINT NOT NULL,estimate_id BIGINT,request_key TEXT NOT NULL,request_hash TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL",',UNIQUE(company_id,project_id,request_key)')
        self.b.schema(M,"group_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,label TEXT NOT NULL,version BIGINT NOT NULL,active INTEGER NOT NULL,snapshot_json TEXT NOT NULL,request_key TEXT NOT NULL,request_hash TEXT NOT NULL,updated_by BIGINT NOT NULL,updated TEXT NOT NULL",',UNIQUE(company_id,project_id,request_key)')
        self.b.schema(T,"group_id BIGINT NOT NULL,group_version BIGINT NOT NULL,estimate_id BIGINT NOT NULL,snapshot_json TEXT NOT NULL,approved_by BIGINT NOT NULL,approved TEXT NOT NULL",',UNIQUE(company_id,project_id,group_id,group_version,estimate_id)')
        with self.b.db(True) as c:
            c.execute('CREATE INDEX IF NOT EXISTS idx_quantity_marks_group ON '+M+'(company_id,project_id,group_id,active)')
        self.b.action('scaled_takeoff',self.binding,self.apply)
        self.routes=[]
        self.rates=RateBook(self)
        old=self.scale.decorate
        def decorate(response,sheet_id):
            response=old(response,sheet_id)
            if response.status_code!=200 or 'id="bcMarkupCanvas"' not in response.body.decode():return response
            with self.b.db() as c:
                user,p,s,g,key=self.scale.context(c,sheet_id)
                if not ns['_bc850_manager'](user):return response
            html=response.body.decode().replace('</head>',STYLE+'</head>',1)
            html=html.replace('</body>',PANEL+'<script>window.BC_TAKEOFF_CONFIG='+js({'sheet_id':sheet_id,'project_id':p['id']})+';'+SCRIPT+'</script></body>',1)
            return HTMLResponse(html,headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})
        self.scale.decorate=decorate;self.decorator=decorate

    def manager(self,c,pid,write=False):
        u,p=self.b.actor(c,pid,write)
        self.b.require(self.ns['_bc850_manager'](u),'Project managers and appointed superintendents manage takeoffs.',403)
        return u,p

    def group(self,c,gid,write=False):
        u,p,row=self.b.scope(c,G,gid,write);self.manager(c,p['id'])
        return u,p,row

    def sheet(self,c,sid,write=False):
        u,p,s,g,key=self.scale.context(c,sid,write);self.manager(c,p['id'])
        return u,p,s,self.scale.state(c,u,s,g,key)

    async def payload(self,request):
        self.b.origin(request);chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk);self.b.require(size<=65536,'This takeoff request is too large.',413);chunks.append(chunk)
        try:data=json.loads(b''.join(chunks))
        except (ValueError,UnicodeError):self.b.require(False,'The takeoff could not be read.',400)
        self.b.require(isinstance(data,dict),'Enter the takeoff information.',400)
        return data

    def key(self,data):
        value=data.get('request_key','');self.b.require(isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_-]{20,80}',value),'Reload the takeoff before saving.',400);return value

    def version(self,data,row):
        self.b.require(type(data.get('version')) is int and data['version']==row['version'],'This takeoff changed. Reload and review the saved quantities.',409)

    def groups(self,c,u,pid):
        return [dict(r) for r in c.execute(f'SELECT * FROM {G} WHERE company_id=? AND project_id=? ORDER BY trade,name,id',(u['company_id'],pid)).fetchall()]

    def rows(self,c,u,g):
        return [dict(r) for r in c.execute(f'SELECT * FROM {M} WHERE company_id=? AND project_id=? AND group_id=? AND active=1 ORDER BY id',(u['company_id'],g['project_id'],g['id'])).fetchall()]

    def summary(self,c,u,g,live=False,cache=None):
        cache={} if cache is None else cache;rows=self.rows(c,u,g);marks=[]
        for r in rows:
            snap=json.loads(r['snapshot_json']);stale=False
            if live:
                sid=r['sheet_id']
                if sid not in cache:
                    try:cache[sid]=self.sheet(c,sid)[3]
                    except (self.ns['_BC850_Problem'],OSError):cache[sid]=None
                state=cache[sid];stale=state is None or not state['current'] or scale_stamp(state,g['kind'])!=snap['stamp']
            marks.append(dict(id=r['id'],sheet_id=r['sheet_id'],label=r['label'],version=r['version'],stale=stale,**snap))
        return dict(group=g,marks=marks,totals=totals([x['measurement'] for x in marks],g['waste']),stale=any(x['stale'] for x in marks))

    def state(self,sheet_id:int):
        with self.b.db() as c:
            u,p,s,scale=self.sheet(c,sheet_id)
            cache={sheet_id:scale}
            groups=[self.summary(c,u,g,True,cache) for g in self.groups(c,u,p['id'])]
            for group in groups:group['marks']=[mark for mark in group['marks'] if mark['sheet_id']==sheet_id]
        return JSONResponse(dict(scale=scale,groups=groups),headers={'Cache-Control':'private, no-store'})

    async def create(self,project_id:int,request:Request):
        data=await self.payload(request)
        try:
            name=self.b.text(data.get('name'),120,'item name',True);trade=self.b.text(data.get('trade'),80,'trade',True)
            kind=data.get('kind');unit=data.get('unit');self.b.require(kind in OUTPUT and unit in OUTPUT[kind],'Choose Length, Area or Count and its unit.',400)
            waste=number(data.get('waste',0));self.b.require(kind!='count' or waste==0,'Count items use exact counts; add spares as separate counted items.',400)
            material=price(data.get('material_rate',0));labor=price(data.get('labor_rate',0))
            key=self.key(data);finger=digest(encode(dict(name=name,trade=trade,kind=kind,unit=unit,waste=waste,material=material,labor=labor)))
            with self.b.db(True) as c:
                u,p=self.manager(c,project_id,True)
                old=c.execute(f'SELECT * FROM {G} WHERE company_id=? AND project_id=? AND request_key=?',(u['company_id'],project_id,key)).fetchone()
                if old:
                    self.b.require(old['request_hash']==finger,'This save key was already used. Reload to create another item.',409);return JSONResponse({'group_id':old['id']})
                self.b.require(len(self.groups(c,u,project_id))<200,'This project has reached 200 takeoff items.',409)
                gid=self.b.insert(c,G,dict(company_id=u['company_id'],project_id=project_id,name=name,trade=trade,kind=kind,unit=unit,waste=waste,material_rate=material,labor_rate=labor,version=1,estimate_id=None,request_key=key,request_hash=finger,created_by=u['id'],created=self.b.now().isoformat()))
                self.b.event(c,u,project_id,'Takeoff item created',gid,data)
            return JSONResponse({'group_id':gid})
        except (ValueError,TypeError) as e:return JSONResponse({'error':str(e)},400)

    async def settings(self,group_id:int,request:Request):
        data=await self.payload(request)
        try:
            waste=number(data.get('waste'));material=price(data.get('material_rate',0));labor=price(data.get('labor_rate',0))
            with self.b.db(True) as c:
                u,p,g=self.group(c,group_id,True);self.version(data,g)
                self.b.require(g['kind']!='count' or waste==0,'Count items use exact counts.',400)
                c.execute(f'UPDATE {G} SET waste=?,material_rate=?,labor_rate=?,version=version+1 WHERE id=?',(waste,material,labor,group_id));self.b.event(c,u,p['id'],'Takeoff waste changed',group_id,{'before':g,'after':{'waste':waste,'material_rate':material,'labor_rate':labor}})
            return JSONResponse({'saved':True})
        except (ValueError,TypeError) as e:return JSONResponse({'error':str(e)},400)

    async def save(self,sheet_id:int,request:Request):
        data=await self.payload(request)
        try:
            self.b.require(type(data.get('group_id')) is int,'Choose a takeoff item.',400)
            label=self.b.text(data.get('label'),120,'room or location',True);key=self.key(data)
            # Hash the exact submitted operation so a connection retry cannot add it twice.
            finger=digest(encode({k:v for k,v in data.items() if k!='request_key'}))
            with self.b.db(True) as c:
                u,p,s,state=self.sheet(c,sheet_id,True)
                old=c.execute(f'SELECT * FROM {M} WHERE company_id=? AND project_id=? AND request_key=?',(u['company_id'],p['id'],key)).fetchone()
                if old:
                    self.b.require(old['request_hash']==finger and old['sheet_id']==sheet_id,'This save key has already been used. Reload the takeoff.',409)
                    return JSONResponse({'saved':True,'mark_id':old['id']})
                _,gp,g=self.group(c,data['group_id'],True);self.b.require(gp['id']==p['id'],'Choose an item from this project.',403);self.version(data,g)
                self.b.require(data.get('stamp')==scale_stamp(state,g['kind']),'This source or scale changed. Reload and check the measurement.',409)
                value=calculate(data.get('shape'),state,g['kind'],g['unit'])
                snapshot=dict(stamp=scale_stamp(state,g['kind']),measurement=value,sheet_number=s['sheet_number'],revision=s.get('revision_label',''),geometry=state['geometry'])
                mid=data.get('mark_id');previous=None
                if mid is not None:
                    self.b.require(type(mid) is int,'Choose a saved measurement.',400)
                    previous=c.execute(f'SELECT * FROM {M} WHERE id=? AND company_id=? AND project_id=? AND group_id=? AND sheet_id=? AND active=1',(mid,u['company_id'],p['id'],g['id'],sheet_id)).fetchone()
                    self.b.require(previous is not None,'This saved measurement is unavailable.',404)
                    # Preserve the earlier operation key in the event; new row revisions remain auditable.
                    c.execute(f'UPDATE {M} SET active=0 WHERE id=?',(mid,))
                else:self.b.require(len(self.rows(c,u,g))<2000,'This item has reached 2,000 measurements.',409)
                rid=self.b.insert(c,M,dict(company_id=u['company_id'],project_id=p['id'],group_id=g['id'],sheet_id=sheet_id,label=label,version=(previous['version']+1 if previous else 1),active=1,snapshot_json=encode(snapshot),request_key=key,request_hash=finger,updated_by=u['id'],updated=self.b.now().isoformat()))
                c.execute(f'UPDATE {G} SET version=version+1 WHERE id=?',(g['id'],))
                self.b.event(c,u,p['id'],'Takeoff measurement saved',rid,{'replaces':mid,'snapshot':snapshot,'label':label})
            return JSONResponse({'saved':True,'mark_id':rid})
        except (ValueError,TypeError,OverflowError) as e:return JSONResponse({'error':str(e)},400)

    async def remove(self,group_id:int,mark_id:int,request:Request):
        data=await self.payload(request)
        with self.b.db(True) as c:
            u,p,g=self.group(c,group_id,True);self.version(data,g)
            row=c.execute(f'SELECT * FROM {M} WHERE id=? AND company_id=? AND project_id=? AND group_id=? AND active=1',(mark_id,u['company_id'],p['id'],group_id)).fetchone()
            self.b.require(row is not None,'This measurement was already removed or is unavailable.',409)
            c.execute(f'UPDATE {M} SET active=0 WHERE id=?',(mark_id,));c.execute(f'UPDATE {G} SET version=version+1 WHERE id=?',(group_id,))
            self.b.event(c,u,p['id'],'Takeoff measurement removed',mark_id,dict(row))
        return JSONResponse({'removed':True})

    def targets(self,c,u,pid):
        # Only offer scope items visible in the existing current estimate.
        run=c.execute('SELECT id FROM blueprint_runs WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1',(u['company_id'],pid)).fetchone()
        if not run:return []
        return [dict(r) for r in c.execute('''SELECT e.* FROM estimator_items e JOIN blueprint_scope_items s ON s.id=e.blueprint_scope_item_id AND s.company_id=e.company_id AND s.project_id=e.project_id WHERE e.company_id=? AND e.project_id=? AND s.run_id=? ORDER BY e.trade,e.id''',(u['company_id'],pid,run['id'])).fetchall()]

    def binding(self,c,u,p,data):
        self.manager(c,p['id'],True)
        _,gp,g=self.group(c,data['group_id'],True);self.b.require(gp['id']==p['id'],'This item is unavailable.',403)
        summary=self.summary(c,u,g,True)
        self.b.require(summary['marks'] and not summary['stale'],'Remeasure or recheck outdated quantities before reviewing the estimate.',409)
        target=c.execute('SELECT * FROM estimator_items WHERE id=? AND company_id=? AND project_id=?'+self.b.field.lock,(data['estimate_id'],u['company_id'],p['id'])).fetchone()
        self.b.require(target is not None and target['id'] in {x['id'] for x in self.targets(c,u,p['id'])},'Choose an item from the current estimate. Reopen Estimating if the scope changed.',409)
        target=dict(target)
        aliases={'FT':'LF','FEET':'LF','FT2':'SF','SQ FT':'SF','SQFT':'SF','M²':'M2','EACH':'EA','PCS':'EA'}
        unit=str(target.get('unit') or '').strip().upper();unit=aliases.get(unit,unit)
        self.b.require(unit==g['unit'] or (not unit and not target.get('material_unit_cost') and not target.get('labor_unit_cost')),'The estimate unit must match the takeoff. Review its unit and per-unit pricing in Estimating first.',409)
        self.b.require(not g['estimate_id'] or g['estimate_id']==target['id'],'This takeoff is already linked to another estimate item. Keep its contribution traceable.',409)
        other=c.execute(f'SELECT id FROM {G} WHERE company_id=? AND project_id=? AND estimate_id=? AND id<>?',(u['company_id'],p['id'],target['id'],g['id'])).fetchone()
        self.b.require(other is None,'Another takeoff item already supplies this estimate quantity. Add measurements to that item instead.',409)
        basis=data.get('quantity_basis','net');self.b.require(basis in {'net','order'},'Choose net quantity or quantity with waste.',400)
        return dict(takeoff=summary,estimate=target,quantity_basis=basis,quantity=round(summary['totals']['net' if basis=='net' else 'quantity'],6))

    def review(self,group_id:int,request:Request,estimate_id:int=Form(...),quantity_basis:str=Form('net')):
        self.b.origin(request)
        with self.b.db(True) as c:
            u,p,g=self.group(c,group_id,True);data=dict(group_id=group_id,estimate_id=estimate_id,quantity_basis=quantity_basis);source=self.binding(c,u,p,data);t=source['takeoff']['totals'];old=source['estimate']
            body='<div class="card"><h2>'+esc(g['name'])+'</h2><p>'+esc(g['trade'])+'</p>'+self.total_html(t,g['unit'])
            body+='<p>Quantity basis: <b>'+('Net installed quantity' if quantity_basis=='net' else 'Quantity including waste')+'</b>. The existing estimator applies this one quantity to both material and labor rates.</p><h3>Replace estimate quantity</h3><p>'+esc(old['description'])+'</p><p><strong>'+esc(old.get('quantity') or 0)+' '+esc(old.get('unit') or '(no unit)')+' → '+format(source['quantity'],'.6f').rstrip('0').rstrip('.')+' '+esc(g['unit'])+'</strong></p><p>This sets the full item quantity; it does not add to the old quantity. Material and labor rates, quote, allowance, markup and notes stay unchanged. The item will be marked for verification.</p>'
            body+='<p>Material / unit: '+esc(old.get('material_unit_cost') or 0)+' · Labor / unit: '+esc(old.get('labor_unit_cost') or 0)+' · Quote: '+esc(old.get('subcontract_quote') or 0)+' · Allowance: '+esc(old.get('allowance') or 0)+' · Markup: '+esc(old.get('markup_pct') or 0)+'%</p></div>'+self.mark_table(source['takeoff']['marks'],g)
            return self.b.prepare(c,u,p,request,'scaled_takeoff',data,'Review takeoff into estimate',body,BASE+'/groups/'+str(group_id))

    def apply(self,c,u,p,data):
        source=self.binding(c,u,p,data);g=source['takeoff']['group'];target=source['estimate'];qty=source['quantity']
        c.execute('UPDATE estimator_items SET quantity=?,unit=?,verified=0,updated=? WHERE id=? AND company_id=? AND project_id=?',(qty,g['unit'],self.b.now().isoformat(),target['id'],u['company_id'],p['id']))
        c.execute(f'UPDATE {G} SET estimate_id=? WHERE id=?',(target['id'],g['id']))
        existing=c.execute(f'SELECT id FROM {T} WHERE company_id=? AND project_id=? AND group_id=? AND group_version=? AND estimate_id=?',(u['company_id'],p['id'],g['id'],g['version'],target['id'])).fetchone()
        self.b.require(existing is None,'This takeoff version was already transferred. Review its history before making another change.',409)
        tid=self.b.insert(c,T,dict(company_id=u['company_id'],project_id=p['id'],group_id=g['id'],group_version=g['version'],estimate_id=target['id'],snapshot_json=command_json(source),approved_by=u['id'],approved=self.b.now().isoformat()))
        return BASE+'/transfers/'+str(tid)

    def total_html(self,t,unit):
        def f(v):return f'{v:,.3f}'.rstrip('0').rstrip('.')
        return '<p>Measured <b>'+f(t['gross'])+'</b> − Cutouts <b>'+f(t['deductions'])+'</b> = Net <b>'+f(t['net'])+' '+esc(unit)+'</b></p><p>Waste '+f(t['waste_pct'])+'%: '+f(t['waste'])+' · Quantity with waste <b>'+f(t['quantity'])+' '+esc(unit)+'</b></p>'

    def mark_table(self,marks,g):
        body='<div class="card field-scroll"><table class="field-table"><tr><th>Sheet / location</th><th>Measured</th><th>Cutouts</th><th>Net</th><th>Scale</th></tr>'
        for m in marks:
            v=m['measurement'];cal=v['calibration'];status='Recheck source / scale' if m.get('stale') else (cal['name']+' · '+cal['profile']['label']+' · v'+str(cal['version']) if cal else 'Count')
            body+='<tr><td><a href="/workspace/drawing-sheets/'+str(m['sheet_id'])+'?takeoff='+str(g['id'])+'">'+esc(m['sheet_number'])+' · '+esc(m['label'])+'</a></td><td>'+f"{v['gross']:,.3f}"+'</td><td>'+f"{v['deductions']:,.3f}"+'</td><td>'+f"{v['net']:,.3f}"+' '+esc(g['unit'])+'</td><td>'+esc(status)+'</td></tr>'
        return body+'</table></div>'

    def detail(self,group_id:int):
        with self.b.db() as c:
            u,p,g=self.group(c,group_id);summary=self.summary(c,u,g,True);targets=self.targets(c,u,p['id'])
            history=[dict(r) for r in c.execute(f'SELECT id,approved,group_version FROM {T} WHERE company_id=? AND project_id=? AND group_id=? ORDER BY id DESC',(u['company_id'],p['id'],group_id)).fetchall()]
        body='<div class="hero"><h1>'+esc(g['name'])+'</h1><p>'+esc(p['name'])+' · '+esc(g['trade'])+'</p></div><div class="card">'+self.total_html(summary['totals'],g['unit'])
        body+='<p>Takeoff price: <b>'+format(summary['totals']['quantity']*g['material_rate']+summary['totals']['net']*g['labor_rate'],',.2f')+'</b> in company currency · Material / unit '+esc(g['material_rate'])+' · Labor / unit '+esc(g['labor_rate'])+'. Material uses quantity with waste; labor uses net installed quantity. Rates are private defaults; quantity transfer preserves existing estimator rates.</p>'
        body+='<p>Saved quantities use the scale recorded when you measured. Recheck any changed sheet or scale before transferring them.</p>'+self.b.link(BASE+'/projects/'+str(p['id']),'All takeoff items')+' · '+self.b.link(BASE+'/groups/'+str(group_id)+'/export','Download CSV')+'</div>'+self.mark_table(summary['marks'],g)
        if summary['stale']:body+='<div class="card field-warning">Some measurements need a source or scale check. Open their sheet and use Recheck scale, or measure the current revision.</div>'
        elif targets and summary['marks']:
            options=''.join('<option value="'+str(x['id'])+'"'+(' selected' if x['id']==g['estimate_id'] else '')+'>'+esc(x['trade'])+' · '+esc(x['description'])+' ('+esc(x.get('quantity') or 0)+' '+esc(x.get('unit') or '')+')</option>' for x in targets)
            body+='<form class="card field-form" method="post" action="'+BASE+'/groups/'+str(group_id)+'/review"><h2>Review into estimate</h2><label>Existing estimate item<select name="estimate_id" required>'+options+'</select></label><label>Quantity to transfer<select name="quantity_basis"><option value="net">Net installed quantity</option><option value="order">Quantity including waste</option></select></label><button>Review quantity change</button></form>'
        else:body+='<div class="card"><p>'+('Add measurements on a drawing to get started.' if not summary['marks'] else 'Open Estimating to prepare the project scope items, then choose the item that should receive this quantity.')+'</p>'+self.b.link('/workspace/tools?project_id='+str(p['id']),'Open estimating tools')+'</div>'
        body+='<div class="card"><h2>Transfer history</h2>'+(''.join('<p>'+self.b.link(BASE+'/transfers/'+str(h['id']),'Version '+str(h['group_version'])+' · '+h['approved'])+'</p>' for h in history) or '<p>No quantities transferred yet.</p>')+'</div>'
        return self.b.page('Drawing takeoff',body)

    def project(self,project_id:int):
        with self.b.db() as c:
            u,p=self.manager(c,project_id);groups=[self.summary(c,u,g) for g in self.groups(c,u,project_id)]
        body='<div class="hero"><h1>Drawing takeoff</h1><p>'+esc(p['name'])+'</p></div><div class="card"><p>Open a sheet, choose Menu → Takeoff, then trace a length, outline an area or count items. Each item keeps its trade, material and room quantities together.</p>'+self.b.link('/workspace/drawings?project_id='+str(project_id),'Open drawings')+'</div>'
        for trade in sorted({s['group']['trade'] for s in groups}):
            body+='<section class="card"><h2>'+esc(trade)+'</h2>'
            # Unlike units and different materials are intentionally not added into a misleading total.
            for s in [x for x in groups if x['group']['trade']==trade]:
                g=s['group'];body+='<div class="field-row">'+self.b.link(BASE+'/groups/'+str(g['id']),g['name'])+self.total_html(s['totals'],g['unit'])+'</div>'
            body+='</section>'
        return self.b.page('Project takeoff',body)

    def transfer(self,transfer_id:int):
        with self.b.db() as c:
            u,p,row=self.b.scope(c,T,transfer_id);self.manager(c,p['id']);source=json.loads(row['snapshot_json'])
        s=source['takeoff'];g=s['group'];body='<div class="hero"><h1>Quantity transferred</h1><p>Approved '+esc(row['approved'])+' · User '+str(row['approved_by'])+'</p></div><div class="card"><h2>'+esc(g['name'])+'</h2>'+self.total_html(s['totals'],g['unit'])+'<p>Approved quantity: '+esc(source['quantity'])+' '+esc(g['unit'])+' ('+esc(source['quantity_basis'])+'). Estimate item: '+esc(source['estimate']['description'])+'. Prices and notes were preserved. This is the saved approval snapshot.</p>'+self.b.link(BASE+'/groups/'+str(g['id']),'Back to takeoff')+'<form method="post" action="/workspace/tools/projects/'+str(p['id'])+'/open"><input type="hidden" name="tool" value="/brain/estimator"><button>Open estimate</button></form></div>'+self.mark_table(s['marks'],g)
        return self.b.page('Takeoff approval',body)

    def export(self,group_id:int):
        with self.b.db() as c:
            u,p,g=self.group(c,group_id);s=self.summary(c,u,g,True)
        out=io.StringIO();w=csv.writer(out)
        def safe(v):
            v=str(v);return "'"+v if v.lstrip().startswith(('=','+','-','@')) else v
        w.writerow(['Trade','Item','Sheet','Room / location','Gross','Cutouts','Net','Unit','Scale status'])
        for m in s['marks']:
            v=m['measurement'];w.writerow([safe(g['trade']),safe(g['name']),safe(m['sheet_number']),safe(m['label']),v['gross'],v['deductions'],v['net'],g['unit'],'Recheck' if m['stale'] else 'Current'])
        w.writerow([]);w.writerow(['Material per unit',g['material_rate']]);w.writerow(['Labor per unit',g['labor_rate']]);w.writerow(['Takeoff price in company currency',s['totals']['quantity']*g['material_rate']+s['totals']['net']*g['labor_rate']]);w.writerow(['Waste %',g['waste']]);w.writerow(['Quantity including waste',s['totals']['quantity'],g['unit']])
        return Response(out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="takeoff-{group_id}.csv"','Cache-Control':'private, no-store'})

    def health(self):
        active={(r.path,m):r.endpoint for r in self.app.routes for m in (getattr(r,'methods',None) or [])}
        checks={m+' '+p:active.get((p,m)) is fn for p,m,fn in self.routes}
        checks.update(schema_initialized=self.b.schema_ready,source_scale_connected=self.scale.decorate is self.decorator,exact_estimate_review='scaled_takeoff' in self.b.actions,compact_menu_preserved=bool(getattr(self.app.state,'drawing_work',None)),form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.b.db() as c:
                for t in (G,M,T,'bc824_quantity_rates'):c.execute('SELECT id FROM '+t+' WHERE 1=0')
            checks['schema_readable']=True
        except Exception:checks['schema_readable']=False
        return JSONResponse(dict(app='BuildCommand AI',version=VERSION,release=RELEASE,status='ok' if all(checks.values()) else 'attention',checks=checks,passed=sum(checks.values()),total=len(checks),data_reset=False,scope='Installation checks only. Verify calibrated drawing quantities, cutouts, exact estimate review, roles and PostgreSQL on staging. Manual takeoff; no AI detection, automatic sharing or bidding invitations.'),status_code=200 if all(checks.values()) else 503)

    def register(self):
        for p,m,fn in [('/workspace/takeoff-rates','GET',self.rates.page),('/workspace/takeoff-rates/data','GET',self.rates.data),('/workspace/takeoff-rates/save','POST',self.rates.save),(BASE+'/sheets/{sheet_id}','GET',self.state),(BASE+'/projects/{project_id}','GET',self.project),(BASE+'/projects/{project_id}/groups','POST',self.create),(BASE+'/groups/{group_id}','GET',self.detail),(BASE+'/groups/{group_id}/settings','POST',self.settings),(BASE+'/sheets/{sheet_id}/marks','POST',self.save),(BASE+'/groups/{group_id}/marks/{mark_id}/remove','POST',self.remove),(BASE+'/groups/{group_id}/review','POST',self.review),(BASE+'/groups/{group_id}/export','GET',self.export),(BASE+'/transfers/{transfer_id}','GET',self.transfer)]:
            self.b.route(p,m,fn);self.routes.append(self.b.routes[-1])
        path='/health/scaled-takeoff-8-27-0';self.ns['_bc840_replace'](path,'GET',self.health);self.ns['_runtime'].PUBLIC_PATHS.add(path)
