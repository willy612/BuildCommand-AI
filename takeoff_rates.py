"""Private company estimating defaults; copied into takeoffs, never shared with drawings."""
from fastapi import Request, Form
from fastapi.responses import JSONResponse, RedirectResponse
from blueprint_field import esc
from takeoff_geometry import OUTPUT
import math

TABLE='bc824_quantity_rates'

def price(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1000000:raise ValueError('Use a rate from 0 to 1,000,000 per unit.')
    return value

class RateBook:
    def __init__(self,takeoff):
        self.t=takeoff;self.b=takeoff.b
        self.b.schema(TABLE,'name TEXT NOT NULL,trade TEXT NOT NULL,kind TEXT NOT NULL,unit TEXT NOT NULL,material_rate DOUBLE PRECISION NOT NULL,labor_rate DOUBLE PRECISION NOT NULL,waste DOUBLE PRECISION NOT NULL,version BIGINT NOT NULL,updated_by BIGINT NOT NULL,updated TEXT NOT NULL',',UNIQUE(company_id,name,trade,unit)')

    def actor(self,c,write=False):
        u=self.b.user(c)
        if write:
            c.execute('SELECT id FROM companies WHERE id=?'+self.b.field.lock,(u['company_id'],)).fetchone()
            row=c.execute('SELECT id,company_id,role FROM users WHERE id=?'+self.b.field.lock,(u['id'],)).fetchone()
            self.b.require(row is not None and row['company_id']==u['company_id'],'Your account changed. Sign in again.',403)
            u=dict(row)
        self.b.require(self.t.ns['_bc850_manager'](u),'Your company estimating team manages its private rate book.',403)
        return u

    def rows(self,c,u):return [dict(r) for r in c.execute('SELECT * FROM '+TABLE+' WHERE company_id=? ORDER BY trade,name,id',(u['company_id'],)).fetchall()]

    def data(self):
        with self.b.db() as c:u=self.actor(c);rows=self.rows(c,u)
        return JSONResponse({'rates':rows},headers={'Cache-Control':'private, no-store'})

    def page(self):
        with self.b.db() as c:u=self.actor(c);rows=self.rows(c,u)
        body='<div class="hero"><h1>Company rate book</h1><p>Your prices, ready for the next takeoff.</p></div><div class="card"><p>Save what your company charges per square foot, linear foot, meter or item. Material and labor stay separate. Rates are copied into new takeoffs; later changes do not rewrite earlier bids. Use one company currency consistently.</p><p>Use separate items for different materials, tile sizes, framing assemblies or pipe sizes. Tax, overhead and profit are not added here. Verify inclusions before bidding.</p></div>'
        def form(row=None):
            r=row or {};rid=r.get('id',0)
            options=''.join('<option value="'+k+'|'+unit+'"'+(' selected' if r.get('unit')==unit else '')+'>'+label+'</option>' for k,unit,label in [('area','SF','Area / SF'),('area','M2','Area / m²'),('length','LF','Length / LF'),('length','M','Length / m'),('count','EA','Count / each')])
            return '<form class="card field-form" method="post" action="/workspace/takeoff-rates/save"><h2>'+('Edit rate' if row else 'Add a rate')+'</h2>'+self.b.hidden('rate_id',rid)+self.b.hidden('version',r.get('version',0))+self.b.input('name','Material / work item',r.get('name',''),extra='required maxlength="120" placeholder="12 x 24 tile installation"')+self.b.input('trade','Trade',r.get('trade',''),extra='required maxlength="80" placeholder="Tile, Framing, Plumbing..."')+'<label>Measured unit<select name="measure">'+options+'</select></label>'+self.b.input('material','Material per unit',r.get('material_rate',0),'number','min="0" max="1000000" step="0.01" required')+self.b.input('labor','Labor per unit',r.get('labor_rate',0),'number','min="0" max="1000000" step="0.01" required')+self.b.input('waste','Default waste %',r.get('waste',0),'number','min="0" max="100" step="0.1" required')+'<button>Save company rate</button></form>'
        body+=form()+''.join(form(r) for r in rows)+self.b.link('/workspace','Back to workspace')
        return self.b.page('Company rate book',body)

    def save(self,request:Request,name:str=Form(...),trade:str=Form(...),measure:str=Form(...),material:float=Form(0),labor:float=Form(0),waste:float=Form(0),rate_id:int=Form(0),version:int=Form(0)):
        self.b.origin(request)
        try:
            name=self.b.text(name,120,'item name',True);trade=self.b.text(trade,80,'trade',True)
            kind,unit=measure.split('|');self.b.require(kind in OUTPUT and unit in OUTPUT[kind],'Choose a valid measured unit.',400)
            material=price(material);labor=price(labor)
            self.b.require(math.isfinite(waste) and 0<=waste<=100 and (kind!='count' or waste==0),'Use 0–100% waste; counts require zero.',400)
        except (ValueError,TypeError):self.b.require(False,'Check the rate and unit values.',400)
        with self.b.db(True) as c:
            u=self.actor(c,True);rows=self.rows(c,u);old=next((r for r in rows if r['id']==rate_id),None)
            self.b.require(not rate_id or old is not None,'This company rate is unavailable.',404)
            self.b.require(version==(old['version'] if old else 0),'This rate changed. Reopen the rate book before saving.',409)
            self.b.require(not any(r['id']!=rate_id and (r['name'].casefold(),r['trade'].casefold(),r['unit'])==(name.casefold(),trade.casefold(),unit) for r in rows),'This item already has a rate. Edit its existing entry.',409)
            values=dict(name=name,trade=trade,kind=kind,unit=unit,material_rate=material,labor_rate=labor,waste=waste,version=version+1,updated_by=u['id'],updated=self.b.now().isoformat())
            if old:c.execute('UPDATE '+TABLE+' SET '+','.join(k+'=?' for k in values)+' WHERE id=? AND company_id=?',(*values.values(),rate_id,u['company_id']))
            else:
                self.b.require(len(rows)<300,'The company rate book supports 300 items.',409)
                rate_id=self.b.insert(c,TABLE,dict(company_id=u['company_id'],project_id=0,**values))
            self.b.event(c,u,0,'Company takeoff rate saved',rate_id,{'before':old,'after':values})
        return RedirectResponse('/workspace/takeoff-rates',303)
