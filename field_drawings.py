"""Drawing comparison and reviewed measurement records; original viewer stays intact."""
import csv
import io
import json
import math
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from blueprint_field import esc
from command_center import command_json
from drawing_register import js

PDF_SCRIPT='<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>'
COMPARE_JS=r'''(async()=>{const c=window.BC_COMPARE,s=document.querySelector('#compare-status');try{if(!window.pdfjsLib)throw Error('Renderer unavailable. Use Open sheet to compare the originals.');pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';for(let i=0;i<c.length;i++){const task=pdfjsLib.getDocument({url:'/documents/'+c[i].attachment_id+'/content',isEvalSupported:false});const pdf=await task.promise,p=await pdf.getPage(c[i].page_number),base=p.getViewport({scale:1}),view=p.getViewport({scale:Math.min(1.5,1800/Math.max(base.width,base.height))});const canvas=document.getElementById('compare-'+i);canvas.width=view.width;canvas.height=view.height;await p.render({canvasContext:canvas.getContext('2d'),viewport:view}).promise;await pdf.destroy();}s.textContent='Both source pages loaded. Check alignment before interpreting differences.';const holder=document.querySelector('#compare-pages'),top=document.querySelector('#compare-1');document.querySelector('#compare-mode').onchange=e=>{holder.classList.toggle('overlay',e.target.value==='overlay');};document.querySelector('#compare-opacity').oninput=e=>{if(top)top.style.opacity=e.target.value/100;};}catch(e){s.textContent='The comparison could not load. Open each original sheet to continue.';}})();'''

class FieldDrawings:
    def __init__(self,b):
        self.b=b;self.reg=b.app.state.drawing_register;self.pins=b.app.state.drawing_field;self.scale=b.app.state.drawing_scale
        b.schema('bc824_sheet_visits','user_id BIGINT NOT NULL,sheet_id BIGINT NOT NULL,favorite INTEGER NOT NULL,opened TEXT NOT NULL',',UNIQUE(company_id,user_id,sheet_id)')
        b.schema('bc824_takeoffs','sheet_id BIGINT NOT NULL,title TEXT NOT NULL,snapshot_json TEXT NOT NULL,created_by BIGINT NOT NULL,created TEXT NOT NULL')
        b.action('measurement_record',self.measurement_binding,self.apply_measurement)
        for path,method,fn in [('/workspace/drawing-board','GET',self.board),('/workspace/drawing-sheets/{sheet_id}/compare','GET',self.compare),('/workspace/drawing-sheets/{sheet_id}/favorite','POST',self.favorite),('/workspace/drawing-sheets/{sheet_id}/takeoff','POST',self.review_measurement),('/workspace/takeoffs/{takeoff_id}','GET',self.takeoff)]:b.route(path,method,fn)
        original=self.scale.decorate
        def decorate(response,sheet_id):
            response=original(response,sheet_id)
            if response.status_code!=200:return response
            with b.db(True) as c:
                user,p,sheet=self.reg.sheet_context(c,sheet_id)
                if not b.ns['_bc850_manager'](user):return response
                old=c.execute('SELECT id FROM bc824_sheet_visits WHERE company_id=? AND user_id=? AND sheet_id=?',(user['company_id'],user['id'],sheet_id)).fetchone()
                if old:c.execute('UPDATE bc824_sheet_visits SET opened=? WHERE id=?',(b.now().isoformat(),old['id']))
                else:b.insert(c,'bc824_sheet_visits',dict(company_id=user['company_id'],project_id=p['id'],user_id=user['id'],sheet_id=sheet_id,favorite=0,opened=b.now().isoformat()))
                notes=self.pins.pins(c,user,sheet);kinds={str(n['id']):[] for n in notes}
                for n in notes:
                    kinds[str(n['id'])]=[r['kind'] for r in c.execute('SELECT kind FROM bc_drawing_work_links WHERE company_id=? AND project_id=? AND sheet_id=? AND pin_id=?',(user['company_id'],p['id'],sheet_id,n['id'])).fetchall()]
            html=response.body.decode();links='<a href="/workspace/drawing-sheets/'+str(sheet_id)+'/compare">Compare / quantities</a><a href="/workspace/drawing-board?project_id='+str(p['id'])+'">Sheet board</a><label for="field-pin-filter">Show work</label><select id="field-pin-filter"><option value="all">All work cards</option><option value="rfi">Linked RFIs</option><option value="photo">Linked photos</option><option value="submittal">Linked submittals</option><option value="schedule">Linked activities</option></select>'
            html=html.replace('<nav class="df-actions" aria-label="Drawing actions">','<nav class="df-actions" aria-label="Drawing actions">'+links,1)
            script='const kinds='+js(kinds)+''';document.getElementById('field-pin-filter')?.addEventListener('change',e=>{for(const n of document.querySelectorAll('[data-pin-id],[data-work-id]')){const id=n.dataset.pinId||n.dataset.workId;n.hidden=e.target.value!=='all'&&!(kinds[id]||[]).includes(e.target.value);}});'''
            return HTMLResponse(html.replace('</body>','<script>'+script+'</script></body>',1),headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})
        self.scale.decorate=decorate

    def board(self,project_id:int=0,q:str='',favorites:int=0):
        b=self.b
        with b.db() as c:
            user,p,body=b.chooser(c,project_id,'Drawing sheet board','/workspace/drawing-board')
            if not p:return b.page('Drawing board',body)
            rows=self.reg.current_rows(c,user['company_id'],p['id'],b.text(q,120));visits={r['sheet_id']:dict(r) for r in c.execute('SELECT * FROM bc824_sheet_visits WHERE company_id=? AND project_id=? AND user_id=?',(user['company_id'],p['id'],user['id'])).fetchall()}
            rows.sort(key=lambda r:(visits.get(r['id'],{}).get('opened',''),r['id']),reverse=True)
            if favorites:rows=[r for r in rows if visits.get(r['id'],{}).get('favorite')]
            body+='<form class="card field-form" method="get">'+b.hidden('project_id',p['id'])+b.input('q','Sheet number or title',q)+'<label><input type="checkbox" name="favorites" value="1"'+(' checked' if favorites else '')+'> My favorites</label><button>Find sheets</button></form><p>Current sheets, most recently opened first. Thumbnails load when selected.</p><div class="field-grid">'
            for r in rows:
                sid=str(r['id']);body+='<section class="card"><h2>'+esc(r['sheet_number'])+'</h2><p>'+esc(r['title'])+'</p><p>'+esc(r['discipline'])+' · '+esc(r['revision_label'])+'</p><button type="button" class="field-thumb" data-attachment="'+str(r['attachment_id'])+'" data-page="'+str(r['page_number'])+'">Show thumbnail</button><canvas hidden style="width:100%"></canvas>'+b.link('/workspace/drawing-sheets/'+sid,'Open sheet')+'<form method="post" action="/workspace/drawing-sheets/'+sid+'/favorite">'+b.hidden('value',0 if visits.get(r['id'],{}).get('favorite') else 1)+'<button>'+('Remove favorite' if visits.get(r['id'],{}).get('favorite') else 'Add favorite')+'</button></form></section>'
            body+='</div>'+b.link('/workspace/drawings?project_id='+str(p['id']),'Drawing register & uploads')
        script=r'''for(const button of document.querySelectorAll('.field-thumb'))button.onclick=async()=>{button.disabled=true;try{pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';const pdf=await pdfjsLib.getDocument({url:'/documents/'+button.dataset.attachment+'/content',isEvalSupported:false}).promise,p=await pdf.getPage(Number(button.dataset.page)),v=p.getViewport({scale:1}),viewport=p.getViewport({scale:320/v.width}),canvas=button.nextElementSibling;canvas.width=viewport.width;canvas.height=viewport.height;canvas.hidden=false;await p.render({canvasContext:canvas.getContext('2d'),viewport}).promise;await pdf.destroy();button.hidden=true;}catch(e){button.textContent='Preview unavailable — open sheet';}};'''
        return b.page('Drawing board',body+PDF_SCRIPT+'<script>'+script+'</script>')

    def favorite(self,sheet_id:int,request:Request,value:int=Form(...)):
        b=self.b;b.origin(request);b.require(value in {0,1},'Choose a favorite state.',400)
        with b.db(True) as c:
            user,p,sheet=self.reg.sheet_context(c,sheet_id);b.actor(c,p['id'],True)
            old=c.execute('SELECT id FROM bc824_sheet_visits WHERE company_id=? AND user_id=? AND sheet_id=?',(user['company_id'],user['id'],sheet_id)).fetchone()
            if old:c.execute('UPDATE bc824_sheet_visits SET favorite=? WHERE id=?',(value,old['id']))
            else:b.insert(c,'bc824_sheet_visits',dict(company_id=user['company_id'],project_id=p['id'],user_id=user['id'],sheet_id=sheet_id,favorite=value,opened=''))
        return RedirectResponse('/workspace/drawing-board?project_id='+str(p['id']),303)

    def compare(self,sheet_id:int,other:int=0):
        b=self.b
        with b.db() as c:
            user,p,sheet=self.reg.sheet_context(c,sheet_id);b.actor(c,p['id'])
            versions=[dict(r) for r in c.execute('SELECT * FROM bc_drawing_sheets WHERE company_id=? AND project_id=? AND area_key=? AND sheet_key=? ORDER BY revision_no DESC,id DESC',(user['company_id'],p['id'],sheet['area_key'],sheet['sheet_key'])).fetchall()]
            candidate=next((r for r in versions if r['id']==other),None) if other else next((r for r in versions if r['id']!=sheet_id),None)
            b.require(not other or candidate is not None,'Choose a revision of this project sheet.',404)
            if candidate:self.reg.sheet_context(c,candidate['id'])
            body='<div class="hero"><h1>'+esc(sheet['sheet_number'])+' — Revision review</h1><p>'+esc(sheet['title'])+'</p></div>'+b.link('/workspace/drawing-sheets/'+str(sheet_id),'Back to full-page drawing')
            body+='<section class="card"><form method="get"><label for="other">Compare against</label><select id="other" name="other">'+''.join('<option value="'+str(r['id'])+'"'+(' selected' if candidate and r['id']==candidate['id'] else '')+'>'+esc(r['revision_label'])+' · revision '+str(r['revision_no'])+'</option>' for r in versions if r['id']!=sheet_id)+'</select><button>Compare</button></form><p>Overlay uses page bounds. Different crop, rotation, size or placement can look like a design change. Verify the originals; this does not automatically detect changes.</p>'
            if candidate:
                body+='<label for="compare-mode">View</label><select id="compare-mode"><option value="side">Side by side</option><option value="overlay">Overlay</option></select><label for="compare-opacity">Second revision opacity</label><input id="compare-opacity" type="range" min="0" max="100" value="50"><p id="compare-status" role="status">Loading source pages...</p><div id="compare-pages"><div><p>'+esc(sheet['revision_label'])+'</p><canvas id="compare-0"></canvas></div><div><p>'+esc(candidate['revision_label'])+'</p><canvas id="compare-1"></canvas></div></div>'
            else:body+='<p>There is one registered revision. Add a revised sheet to compare it here.</p>'
            body+='</section><section class="card"><h2>Review work affected by this sheet</h2>'
            for revision in [sheet]+([candidate] if candidate else []):
                for n in self.pins.pins(c,user,revision):body+='<p>'+esc(revision['revision_label'])+' · '+b.link('/workspace/drawing-sheets/'+str(revision['id'])+'/work/'+str(n['id']),n['title'])+'</p>'
            body+='<p>Work cards and their linked RFIs, photos and records stay attached to the original revision. Review their relevance before creating new work.</p></section><section class="card"><h2>Reviewed measurement record</h2><p>Record saved line measurements using this sheet’s current whole-sheet scale. Detail-area measurements remain available in the drawing and are not included in this export.</p><form class="field-form" method="post" action="/workspace/drawing-sheets/'+str(sheet_id)+'/takeoff">'+b.input('title','Quantity record name',extra='required maxlength="200"')+'<button>Review saved measurements</button></form>'
            for r in c.execute('SELECT id,title FROM bc824_takeoffs WHERE company_id=? AND project_id=? AND sheet_id=? ORDER BY id DESC',(user['company_id'],p['id'],sheet_id)).fetchall():body+='<p>'+b.link('/workspace/takeoffs/'+str(r['id']),r['title'])+'</p>'
            body+='</section><style>#compare-pages{display:grid;grid-template-columns:1fr 1fr;gap:12px}#compare-pages canvas{width:100%}#compare-pages.overlay{display:block;position:relative}#compare-pages.overlay>div:nth-child(2){position:absolute;inset:0}#compare-pages.overlay>div:nth-child(2) canvas{opacity:.5}@media(max-width:650px){#compare-pages{grid-template-columns:1fr}}</style>'
        if candidate:body+=PDF_SCRIPT+'<script>window.BC_COMPARE='+js([sheet,candidate])+';</script><script>'+COMPARE_JS+'</script>'
        return b.page('Drawing comparison',body)

    def measurement_binding(self,c,user,p,data):
        b=self.b;_,project,sheet,geometry,key=self.scale.context(c,data['sheet_id']);b.require(project['id']==p['id'],'This sheet is unavailable.',403)
        state=self.scale.state(c,user,sheet,geometry,key);profile=state['profile'];b.require(profile is not None and state['current'],'Calibrate the current sheet revision first.',409)
        rev,items=self.pins.latest_markup(c,user,sheet);quantities=[]
        for n,item in enumerate(items):
            meta=item.get('measurement') or {}
            if item.get('type')!='measure' or meta.get('sheet_id')!=sheet['id'] or meta.get('scale_version')!=state['version'] or meta.get('area_id') or meta.get('method')!=profile['method'] or meta.get('factor')!=profile['factor']:continue
            points=[item.get(k) for k in ('x1','y1','x2','y2')]
            b.require(all(type(v) in {int,float} and math.isfinite(v) and 0<=v<=1 for v in points),'A saved measurement has invalid endpoints.',409)
            value=math.hypot((points[2]-points[0])*geometry['width'],(points[3]-points[1])*geometry['height'])*profile['factor']
            quantities.append(dict(markup=n+1,value=round(value,6),unit=profile['unit'],points=points))
        b.require(bool(quantities),'Save a whole-sheet measurement using the current scale, then review again.',409)
        return dict(sheet=sheet,scale=state,markup_revision=rev,quantities=quantities)

    def review_measurement(self,sheet_id:int,request:Request,title:str=Form(...)):
        b=self.b;b.origin(request);data=dict(sheet_id=sheet_id,title=b.text(title,200,'a name',True))
        with b.db(True) as c:
            user,p,sheet=self.reg.sheet_context(c,sheet_id);b.actor(c,p['id'],True);source=self.measurement_binding(c,user,p,data)
            body='<section class="card"><h2>'+esc(data['title'])+'</h2><p>Sheet '+esc(sheet['sheet_number'])+' · revision '+str(sheet['revision_no'])+' · saved markup '+str(source['markup_revision'])+'</p><p>Scale: '+esc(source['scale']['profile']['label'])+'</p>'
            for q in source['quantities']:body+='<p>Line '+str(q['markup'])+': '+str(q['value'])+' '+q['unit']+'</p>'
            body+='<p>These measured lengths are a field record. Confirm scope, deductions, waste and drawing dimensions before using them for ordering or payment.</p></section>'
            return b.prepare(c,user,p,request,'measurement_record',data,'Review measured quantities',body,'/workspace/drawing-sheets/'+str(sheet_id)+'/compare')

    def apply_measurement(self,c,user,p,data):
        source=self.measurement_binding(c,user,p,data);rid=self.b.insert(c,'bc824_takeoffs',dict(company_id=user['company_id'],project_id=p['id'],sheet_id=data['sheet_id'],title=data['title'],snapshot_json=command_json(source),created_by=user['id'],created=self.b.now().isoformat()))
        return '/workspace/takeoffs/'+str(rid)

    def takeoff(self,takeoff_id:int,download:int=0):
        b=self.b
        with b.db() as c:user,p,row=b.scope(c,'bc824_takeoffs',takeoff_id)
        data=json.loads(row['snapshot_json']);sheet=data['sheet']
        if download:
            buf=io.StringIO();writer=csv.writer(buf);writer.writerow(['Sheet ID','Revision','Markup revision','Scale version','Line','Length','Unit'])
            for q in data['quantities']:writer.writerow([sheet['id'],sheet['revision_no'],data['markup_revision'],data['scale']['version'],q['markup'],q['value'],q['unit']])
            return Response(buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="measurements-'+str(takeoff_id)+'.csv"','Cache-Control':'private, no-store'})
        body='<div class="hero"><h1>'+esc(row['title'])+'</h1><p>Reviewed '+esc(row['created'])+'</p></div><section class="card"><p>Sheet '+esc(sheet['sheet_number'])+' · revision '+str(sheet['revision_no'])+'</p><p>Scale '+esc(data['scale']['profile']['label'])+'</p>'
        for q in data['quantities']:body+='<p>Line '+str(q['markup'])+': '+str(q['value'])+' '+q['unit']+'</p>'
        return b.page('Reviewed measurements',body+b.link('/workspace/takeoffs/'+str(takeoff_id)+'?download=1','Download CSV')+b.link('/workspace/drawing-sheets/'+str(sheet['id']),'Open original sheet revision')+'</section>')
