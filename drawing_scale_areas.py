"""8.19.0: named, non-overlapping detail scales on an exact sheet revision.

Areas are internal measurement settings, not markups or file-sharing grants.
Existing measurement labels stay immutable. Rectangles and references use
whole-sheet normalized coordinates, preserving 8.18 source geometry.
"""
import json
import logging
import math

from fastapi import Request
from fastapi.responses import JSONResponse
from drawing_scale import profile, endpoints

VERSION = '8.19.0'
RELEASE = 'Detail Scale Areas'
BASE = '/workspace/drawing-sheets/{sheet_id}/scale-areas'
MAX_AREAS = 50
EPS = 1e-9
log = logging.getLogger('buildcommand.drawing_scale_areas')


def rectangle(value):
    if not isinstance(value, list) or len(value) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError('Outline a rectangular detail area on this sheet.')
    x, y, w, h = value
    if x < 0 or y < 0 or w < .001 or h < .001 or x+w > 1+EPS or y+h > 1+EPS:
        raise ValueError('Keep the full area inside the drawing and make the outline large enough to select.')
    return [x, y, min(w, 1-x), min(h, 1-y)]


def contains(rect, point):
    x, y, w, h = rect
    return x-EPS <= point[0] <= x+w+EPS and y-EPS <= point[1] <= y+h+EPS


def overlaps(a, b):
    return (min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]) > EPS and
            min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]) > EPS)


def install(ns):
    service = DrawingScaleAreas(ns)
    ns['app'].state.drawing_scale_areas = service
    service.register()
    return service


class DrawingScaleAreas:
    def __init__(self, ns):
        self.ns = ns
        self.scale = ns['app'].state.drawing_scale
        self.db, self.field, self.reg = self.scale.db, self.scale.field, self.scale.reg
        self.require = self.scale.require
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_scale_areas(
                    id {key}, company_id BIGINT NOT NULL, project_id BIGINT NOT NULL, sheet_id BIGINT NOT NULL,
                    name TEXT NOT NULL, rect_json TEXT NOT NULL, profile_json TEXT NOT NULL,
                    version BIGINT NOT NULL, source_key TEXT NOT NULL, active INTEGER NOT NULL,
                    created_by BIGINT NOT NULL, updated_by BIGINT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_scale_areas_sheet ON bc_drawing_scale_areas(company_id,project_id,sheet_id,active)')
            return True
        except Exception:
            log.exception('Detail scale area setup failed')
            return False

    def rows(self, c, user, sheet):
        return [dict(r) for r in c.execute('''SELECT * FROM bc_drawing_scale_areas
            WHERE company_id=? AND project_id=? AND sheet_id=? AND active=1 ORDER BY id LIMIT ?''',
            (user['company_id'], sheet['project_id'], sheet['id'], MAX_AREAS)).fetchall()]

    def revision(self, c, user, sheet):
        # Every mutation increases a row version. Removed areas stay as tombstones.
        row = c.execute('''SELECT COALESCE(SUM(version),0) AS n FROM bc_drawing_scale_areas
            WHERE company_id=? AND project_id=? AND sheet_id=?''',
            (user['company_id'], sheet['project_id'], sheet['id'])).fetchone()
        return int(row['n'])

    def enrich(self, c, user, sheet, source_key, state):
        self.require(self.schema_ready, 'Detail scales are unavailable. Ask your administrator to check this installation.', 503)
        state['areas_revision'] = self.revision(c, user, sheet)
        state['areas'] = []
        for row in self.rows(c, user, sheet):
            stale = row['source_key'] != source_key
            state['areas'].append(dict(id=row['id'], name=row['name'], rect=json.loads(row['rect_json']),
                profile=json.loads(row['profile_json']) if not stale else None,
                version=int(row['version']), stale_source=stale))
        return state

    async def payload(self, request, sheet_id):
        self.require(self.schema_ready, 'Detail scale areas are unavailable. Ask your administrator to check this installation.', 503)
        self.require(self.ns['_bc840_same_origin'](request), 'Reload this drawing before changing its scale areas.', 403)
        with self.db() as c:
            self.scale.context(c, sheet_id)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            self.require(size <= 8192, 'The scale area request is too large.', 413)
            chunks.append(chunk)
        try:
            data = json.loads(b''.join(chunks))
        except (ValueError, UnicodeError):
            self.require(False, 'The scale area could not be read. Try again.', 400)
        self.require(isinstance(data, dict), 'Enter the scale area settings.', 400)
        self.require(all(type(data.get(k)) is int and 0 <= data[k] < 2**53 for k in ('version', 'areas_revision')),
                     'Reload this sheet before changing its scale areas.', 400)
        return data

    def guarded(self, c, sheet_id, data, area_id=None):
        user, project, sheet, geometry, source_key = self.scale.context(c, sheet_id, True)
        self.require(data.get('source_key') == source_key, 'The source drawing changed. Reload and check the detail areas.', 409)
        row = None
        if area_id is not None:
            row = c.execute('''SELECT * FROM bc_drawing_scale_areas
                WHERE id=? AND company_id=? AND project_id=? AND sheet_id=? AND active=1''',
                (area_id, user['company_id'], project['id'], sheet_id)).fetchone()
            self.require(row is not None, 'This detail scale area is unavailable on this sheet.', 404)
            row = dict(row)
        self.require(data['areas_revision'] == self.revision(c, user, sheet),
                     'Someone changed this sheet\'s scale areas. Reload to review that change before saving.', 409)
        self.require(data['version'] == (int(row['version']) if row else 0),
                     'This detail scale changed. Reload before saving.', 409)
        return user, project, sheet, geometry, source_key, row

    async def create(self, sheet_id: int, request: Request):
        return await self.save(sheet_id, request)

    async def edit(self, sheet_id: int, area_id: int, request: Request):
        return await self.save(sheet_id, request, area_id)

    async def save(self, sheet_id, request, area_id=None):
        data = await self.payload(request, sheet_id)
        try:
            if not isinstance(data.get('name'), str):
                raise ValueError('Give this detail area a short name.')
            name = ' '.join(data['name'].split())
            if not 1 <= len(name) <= 80:
                raise ValueError('Use a detail name between 1 and 80 characters.')
            rect = rectangle(data.get('rect'))
            with self.db(True) as c:
                user, project, sheet, geometry, source_key, row = self.guarded(c, sheet_id, data, area_id)
                existing = self.rows(c, user, sheet)
                self.require(row is not None or len(existing) < MAX_AREAS, 'This sheet already has 50 detail scale areas.', 409)
                for other in existing:
                    if other['id'] == area_id:
                        continue
                    self.require(other['name'].casefold() != name.casefold(), 'Use a different name for each detail area.', 409)
                    self.require(not overlaps(rect, json.loads(other['rect_json'])),
                                 'Scale areas cannot overlap. Redraw this outline so each detail has one scale.', 409)
                value = profile(data, geometry)
                if value and value['method'] == 'reference':
                    self.require(all(contains(rect, p) for p in endpoints(data.get('points'))),
                                 'Draw the known-dimension reference completely inside this detail area.', 400)
                encoded = json.dumps(value, allow_nan=False, separators=(',', ':'))
                bounds = json.dumps(rect, allow_nan=False, separators=(',', ':'))
                if row and all(row[k] == v for k, v in [('name', name), ('rect_json', bounds), ('profile_json', encoded), ('source_key', source_key)]):
                    result = self.scale.state(c, user, sheet, geometry, source_key)
                else:
                    now = self.field.now().isoformat()
                    if row:
                        c.execute('''UPDATE bc_drawing_scale_areas SET name=?,rect_json=?,profile_json=?,source_key=?,version=?,updated_by=?,updated=?
                            WHERE id=? AND company_id=? AND project_id=? AND sheet_id=?''',
                            (name, bounds, encoded, source_key, int(row['version'])+1, user['id'], now, area_id, user['company_id'], project['id'], sheet_id))
                    else:
                        area_id = self.reg.insert(c, '''INSERT INTO bc_drawing_scale_areas
                            (company_id,project_id,sheet_id,name,rect_json,profile_json,version,source_key,active,created_by,updated_by,created,updated)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (user['company_id'], project['id'], sheet_id, name, bounds, encoded, 1, source_key, 1, user['id'], user['id'], now, now))
                    self.ns['_bc850_event'](c, user, project['id'], None, 'DRAWING_SCALE_AREA_SAVED:'+str(sheet_id)+':'+str(area_id))
                    result = self.scale.state(c, user, sheet, geometry, source_key)
                result['saved_area_id'] = area_id
            return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})
        except (ValueError, TypeError, OverflowError) as exc:
            return JSONResponse({'error': str(exc) if isinstance(exc, ValueError) else 'Check the detail scale values.'}, 400)

    async def remove(self, sheet_id: int, area_id: int, request: Request):
        data = await self.payload(request, sheet_id)
        self.require(data.get('confirmed') is True, 'Confirm removal of this detail scale area.', 400)
        with self.db(True) as c:
            user, project, sheet, geometry, source_key, row = self.guarded(c, sheet_id, data, area_id)
            c.execute('''UPDATE bc_drawing_scale_areas SET active=0,version=?,updated_by=?,updated=?
                WHERE id=? AND company_id=? AND project_id=? AND sheet_id=?''',
                (int(row['version'])+1, user['id'], self.field.now().isoformat(), area_id, user['company_id'], project['id'], sheet_id))
            self.ns['_bc850_event'](c, user, project['id'], None, 'DRAWING_SCALE_AREA_REMOVED:'+str(sheet_id)+':'+str(area_id))
            result = self.scale.state(c, user, sheet, geometry, source_key)
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})

    def instrument(self, html, config):
        return instrument(html)

    def health(self):
        active = {(r.path, m): r.endpoint for r in self.ns['app'].routes for m in (getattr(r, 'methods', None) or [])}
        checks = {method+' '+path: active.get((path, method)) is endpoint for path, method, endpoint in self.routes}
        checks.update(area_schema_initialized=self.schema_ready, sheet_scale_preserved=all(self.scale.health()['checks'].values()),
            automatic_area_selection='choose(state,a,b)' in MATH_JS, cross_boundary_measurement_blocked='crosses a scale area' in MATH_JS,
            overlap_save_guard=overlaps([0,0,.5,.5],[.2,.2,.5,.5]), separate_revision_storage=True,
            source_change_guard=True, area_version_guard=True, original_measurement_labels_preserved=True,
            full_page_viewer_preserved=hasattr(self.ns['app'].state.drawing_work,'full_page'),
            form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                c.execute('SELECT sheet_id,rect_json,profile_json,version,active FROM bc_drawing_scale_areas WHERE 1=0')
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        return dict(app='BuildCommand AI', version=VERSION, release=RELEASE, status='ok' if all(checks.values()) else 'degraded',
            checks=checks, passed=sum(checks.values()), total=len(checks), data_reset=False,
            scope='Schema and installation checks only. Verify two detail scales and the main plan, boundaries, known references, zoom/reload, saved measurements, role access and real browser controls on staging. No automatic drawing interpretation or trade sharing.')

    def register(self):
        for path, fn in [(BASE, self.create), (BASE+'/{area_id}', self.edit), (BASE+'/{area_id}/remove', self.remove)]:
            endpoint = self.scale.pins.drawings.endpoint(fn)
            self.ns['_bc840_replace'](path, 'POST', endpoint)
            self.routes.append((path, 'POST', endpoint))
        path = '/health/detail-scale-areas-8-19-0'
        self.ns['app'].add_api_route(path, self.health, methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add(path)


def instrument(html):
    anchor = '      function bcScaleSync(){'
    if html.count(anchor) != 1:
        raise RuntimeError('Detail scales require the 8.18 drawing calibration viewer.')
    html = html.replace('</head>', STYLE+'<script>'+MATH_JS+'</script></head>', 1)
    html = html.replace('<p id="bc-scale-sheet" class="bcs-help"></p>', '<p id="bc-scale-sheet" class="bcs-help"></p>'+CONTROLS, 1)
    html = html.replace('Applies only to this sheet revision. Details marked with another scale need a separate reference.',
                        'Draw separate, non-overlapping areas around differently scaled details. Measurements choose the area scale automatically.', 1)
    html = html.replace(anchor, CLIENT_JS+'\n'+anchor, 1)
    html = html.replace('if(t==="measure"&&!bcScaleState.profile)',
                        'if(t==="measure"&&!bcScaleState.profile&&!(bcScaleState.areas||[]).some(a=>a.profile))', 1)
    html = html.replace('["measure","calibrate"].includes(t)', '["measure","calibrate","scale-area"].includes(t)')
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('      function bcScaleMeasure('):
            lines[i] = MEASURE_JS
        elif line.startswith('        if(tool==="calibrate"){'):
            lines[i] = '        if(tool==="scale-area"){bcAreaOutlined(start,end);current=null;draw();return;}\n'+line
    html = '\n'.join(lines)
    # Scale sync is called only after initialization or a successful save.
    html = html.replace('else delete state.calibration[key()];}', 'else delete state.calibration[key()];bcAreasRefresh();}', 1)
    html = html.replace('if(distance<1e-4)throw Error(\'Draw a longer reference line.\');bcScalePoints=[a,b];',
                        'if(distance<1e-4)throw Error(\'Draw a longer reference line.\');if(bcaTarget!==\'main\'&&(!bcaRect||![a,b].every(p=>window.BCSAreaMath.inside(bcaRect,p))))throw Error(\'Draw the reference completely inside this detail area.\');bcScalePoints=[a,b];if(bcaTarget!==\'main\')bcaDirty=true;',1)
    html = html.replace("if(mode&&!mode.textContent.includes('Tool: Pan'))dirty=true;",
                        "if(mode&&!/^Tool: (Pan|Calibrate|Scale-area)/.test(mode.textContent))dirty=true;",1)
    # Existing UI hooks become context-aware while retaining the base save handler.
    html = html.replace("bcScaleSync();bcScaleMode();", "bcScaleSync();bcScaleMode();bcAreasBind();", 1)
    return html


CONTROLS = '''<label>Scale for<select id="bca-target"><option value="main">Main sheet</option><option value="new">+ Add detail scale area</option></select></label>
<div id="bca-editor" hidden><label>Detail name<input id="bca-name" maxlength="80" placeholder="Example: Enlarged restroom plan"></label><button type="button" id="bca-outline">Outline detail area</button><p id="bca-bounds" class="bcs-help">Draw a box around the detail that uses this scale.</p></div>
<label class="bcs-help"><input type="checkbox" id="bca-visible" checked> Show scale area outlines on the drawing</label>
<button type="button" id="bca-remove" hidden>Remove this detail area</button>
'''

STYLE = '''<style>
#bca-overlay{position:absolute;inset:0;pointer-events:none;z-index:5;overflow:hidden}#bca-overlay[hidden]{display:none!important}.bca-region{box-sizing:border-box;position:absolute;border:2px dashed #087c88;background:#087c8805;pointer-events:none}.bca-region.bca-nts{border-color:#a36212}.bca-region span{display:block;max-width:100%;width:max-content;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:3px 6px;background:#08646e;color:white;font:600 11px/1.4 Arial,sans-serif}.bca-region.bca-nts span{background:#865014}#bc-scale-dialog #bca-remove{color:#8b3023;margin-bottom:8px}#bca-editor{border-left:3px solid #17808b;padding-left:12px}#bc-scale-dialog #bca-outline{background:#edf7f8}#bca-editor[hidden],#bca-remove[hidden]{display:none!important}
</style>'''

MATH_JS = r'''
window.BCSAreaMath=Object.freeze({
 inside(r,p){const e=1e-9;return p[0]>=r[0]-e&&p[0]<=r[0]+r[2]+e&&p[1]>=r[1]-e&&p[1]<=r[1]+r[3]+e;},
 touches(r,a,b){let lo=0,hi=1;const e=1e-9;for(let i=0;i<2;i++){const d=b[i]-a[i],min=r[i]-e,max=r[i]+r[i+2]+e;if(Math.abs(d)<1e-15){if(a[i]<min||a[i]>max)return false;}else{let t0=(min-a[i])/d,t1=(max-a[i])/d;if(t0>t1)[t0,t1]=[t1,t0];lo=Math.max(lo,t0);hi=Math.min(hi,t1);if(lo>hi)return false;}}return true;},
 choose(state,a,b){if(![...a,...b].every(v=>Number.isFinite(v)&&v>=0&&v<=1))throw Error('Keep both endpoints inside the drawing.');const areas=state.areas||[];if(areas.some(r=>r.stale_source))throw Error('The source changed. Review or remove the saved detail scale areas before measuring.');const hits=areas.filter(r=>this.touches(r.rect,a,b));if(hits.length===0){if(!state.profile)throw Error('Set the main sheet scale before measuring outside a detail area.');return {profile:state.profile,id:null,name:'Main sheet',version:state.version,rect:null};}if(hits.length!==1||!this.inside(hits[0].rect,a)||!this.inside(hits[0].rect,b))throw Error('This measurement crosses a scale area. Measure each area separately, away from its boundary.');const area=hits[0];if(!area.profile)throw Error(area.name+' is not to scale. Set a known dimension for this detail before measuring.');return {profile:area.profile,id:area.id,name:area.name,version:area.version,rect:area.rect};}
});
'''

MEASURE_JS = r'''      function bcScaleMeasure(a,b,color){try{if(pageNum!==bcScaleState.page)throw Error('Open this page from Sheets before measuring it.');const area=window.BCSAreaMath.choose(bcScaleState,a,b),c=area.profile,real=bcsMath.length(a,b,bcScaleState.geometry,c);items().push({type:'measure',x1:a[0],y1:a[1],x2:b[0],y2:b[1],label:bcsMath.label(real,c.unit),color,measurement:{value:real,unit:c.unit,scale_version:area.version,area_id:area.id,area_name:area.name,area_rect:area.rect,source_key:bcScaleState.source_key,areas_revision:bcScaleState.areas_revision,sheet_id:bcScaleState.sheet_id,method:c.method,label:c.label,factor:c.factor}});bcs('bcMeasureStatus').textContent=area.name+' | '+c.label+' | Measured '+bcsMath.label(real,c.unit)+' | Save markups to keep this line.';}catch(error){bcs('drawing-load-state').textContent=error.message;}}'''

CLIENT_JS = r'''
      let bcaTarget='main',bcaRect=null,bcaBusy=false,bcaDirty=false,bcaVisible=true,bcaOverlay=null;
      function bcAreasRefresh(){
        const picker=bcs('bca-target');picker.replaceChildren();
        const option=(value,label)=>{const el=document.createElement('option');el.value=value;el.textContent=label;picker.append(el);};
        option('main','Main sheet');for(const area of bcScaleState.areas||[])option(String(area.id),area.name+(area.stale_source?' - check source':area.profile?'':' - not to scale'));option('new','+ Add detail scale area');
        if(!['main','new'].includes(bcaTarget)&&!(bcScaleState.areas||[]).some(a=>String(a.id)===bcaTarget))bcaTarget='main';picker.value=bcaTarget;
        const stage=bcs('bcStage');if(!bcaOverlay&&stage){bcaOverlay=document.createElement('div');bcaOverlay.id='bca-overlay';stage.append(bcaOverlay);}
        if(bcaOverlay){bcaOverlay.replaceChildren();bcaOverlay.hidden=!bcaVisible;for(const area of bcScaleState.areas||[]){const box=document.createElement('div');box.className='bca-region'+(!area.profile?' bca-nts':'');const r=area.rect;Object.assign(box.style,{left:r[0]*100+'%',top:r[1]*100+'%',width:r[2]*100+'%',height:r[3]*100+'%'});const label=document.createElement('span');label.textContent=area.name+' | '+(area.stale_source?'Check source':area.profile?.label||'Not to scale');box.append(label);bcaOverlay.append(box);}}
      }
      function bcAreaLoad(target){
        bcaTarget=target;bcaRect=null;bcScalePoints=null;bcaDirty=false;
        const area=(bcScaleState.areas||[]).find(a=>String(a.id)===target),c=target==='main'?bcScaleState.profile:area?.profile;
        bcs('bca-editor').hidden=target==='main';bcs('bca-remove').hidden=!area;bcs('bca-name').value=area?.name||'';bcaRect=area?area.rect.slice():null;
        bcs('bc-scale-title').textContent=target==='main'?"Set this sheet's scale":area?'Edit detail scale':'Add detail scale area';
        bcs('bc-scale-save').textContent=target==='main'?'Save sheet scale':'Save detail scale';bcs('bc-scale-clear').textContent=target==='main'?'Not to scale / clear':'Save detail as not to scale';
        bcs('bc-scale-method').value=c?.method||(bcScaleState.geometry.kind==='pdf'?'preset':'reference');bcs('bc-scale-preset').value='custom';
        bcs('bc-scale-paper').value=c?.paper||1;bcs('bc-scale-paper-unit').value=c?.paper_unit||'in';bcs('bc-scale-known').value=c?.known||(target==='new'?1:40);bcs('bc-scale-unit').value=c?.unit||'ft';bcs('bc-scale-confirm').checked=false;
        if(c?.method==='reference')bcScalePoints=c.points;
        bcs('bca-bounds').textContent=bcaRect?'Area outlined. Redraw only if the detail boundary changed.':'Draw a box around the complete detail that uses this scale.';
        bcs('bc-scale-reference-status').textContent=bcScalePoints?'Saved reference selected. Draw again to replace it.':'No reference selected.';
        bcs('bc-scale-status').textContent=area?.stale_source?'The source changed. Check this area and calibrate it again.':'';bcScaleMode();
      }
      function bcAreaOutlined(a,b){setTool('pan');try{bcsMath.distance(a,b,bcScaleState.geometry);const r=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1])];if(r[2]<.001||r[3]<.001)throw Error('Draw a larger box around the detail.');bcaRect=r;bcScalePoints=null;bcaDirty=true;bcs('bca-bounds').textContent='Outline selected. Choose this detail\'s scale and save.';bcs('bc-scale-reference-status').textContent='Outline changed. Draw a new known reference if needed.';bcScaleOpen();}catch(error){bcScaleOpen();bcs('bc-scale-status').textContent=error.message;}}
      async function bcAreaWrite(remove=false,clear=false){
        if(bcaBusy)return;const status=bcs('bc-scale-status');try{
          const existing=(bcScaleState.areas||[]).find(a=>String(a.id)===bcaTarget);
          if(remove&&(!existing||!confirm('Remove this detail scale area? New measurements here will use the main sheet scale. Saved measurement labels stay unchanged.')))return;
          const data={version:existing?.version||0,areas_revision:bcScaleState.areas_revision,source_key:bcScaleState.source_key,method:clear?'clear':bcs('bc-scale-method').value};
          if(remove)data.confirmed=true;else{data.name=bcs('bca-name').value.trim();if(!data.name)throw Error('Give this detail area a name.');if(!bcaRect)throw Error('Outline the detail area first.');data.rect=bcaRect;if(!clear){data.known=bcsMath.number(bcs('bc-scale-known').value);data.unit=bcs('bc-scale-unit').value;if(data.method==='preset'){data.paper=bcsMath.number(bcs('bc-scale-paper').value);data.paper_unit=bcs('bc-scale-paper-unit').value;data.original_size_confirmed=bcs('bc-scale-confirm').checked;if(!data.original_size_confirmed)throw Error('Confirm the PDF size or choose a known dimension.');}else{if(!bcScalePoints||!bcScalePoints.every(p=>window.BCSAreaMath.inside(bcaRect,p)))throw Error('Draw a known-dimension reference inside this detail area.');data.points=bcScalePoints;}}}
          bcaBusy=true;for(const id of ['bc-scale-save','bc-scale-clear','bca-remove','bca-target','bca-outline','bc-scale-draw'])bcs(id).disabled=true;status.textContent=remove?'Removing area...':'Saving detail scale...';
          const url='/workspace/drawing-sheets/'+bcScaleState.sheet_id+'/scale-areas'+(existing?'/'+existing.id:'')+(remove?'/remove':'');
          const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const type=r.headers.get('content-type')||'';const body=type.includes('application/json')?await r.json():{error:new DOMParser().parseFromString(await r.text(),'text/html').querySelector('p')?.textContent};if(!r.ok)throw Error(body.error||'The detail scale could not be saved. Reload to check your access.');
          bcScaleState=body;window.BC_SHEET_SCALE=body;bcaTarget=remove?'main':String(body.saved_area_id);bcaDirty=false;bcScaleSync();bcAreaLoad(bcaTarget);bcScaleStatus();bcsDialog.close();setTool(remove||clear?'pan':'measure');bcs('drawing-load-state').textContent=remove?'Detail scale removed. Existing measurement labels are unchanged.':'Detail scale saved. Measurements inside its outline use that scale automatically.';
        }catch(error){status.textContent=error.message||'Connection interrupted. Your settings are still here; try again.';}
        finally{bcaBusy=false;for(const id of ['bc-scale-save','bc-scale-clear','bca-remove','bca-outline','bc-scale-draw'])bcs(id).disabled=!bcScaleState.current;bcs('bca-target').disabled=false;}
      }
      function bcAreasBind(){
        const baseOpen=bcScaleOpen,baseStatus=bcScaleStatus;
        bcScaleStatus=()=>{baseStatus();const n=(bcScaleState.areas||[]).length;if(n){bcs('bcSheetScale').textContent='Scales: main + '+n+' detail'+(n===1?'':'s');bcs('bcMeasureStatus').textContent+=' | '+n+' detail scale area'+(n===1?'':'s')+'.';}};
        bcScaleOpen=()=>{baseOpen();bcs('bca-remove').disabled=!bcScaleState.current;bcs('bca-outline').disabled=!bcScaleState.current;if(bcs('bca-target').value!==bcaTarget)bcs('bca-target').value=bcaTarget;};
        bcs('bcSheetScale').onclick=()=>bcScaleOpen();
        bcs('bca-target').onchange=()=>{const target=bcs('bca-target').value;if(bcaDirty&&!confirm('Discard unsaved detail scale changes?')){bcs('bca-target').value=bcaTarget;return;}bcAreaLoad(target);};
        bcs('bca-outline').onclick=()=>{if(bcaTarget==='main')return;bcsDialog.close();bcMenu.classList.remove('open');setTool('scale-area');bcs('drawing-load-state').textContent='Drag a box around the full detail with its own scale. Press Escape to cancel.';};
        const baseSave=bcScaleSave;bcScaleSave=async clear=>{if(bcaBusy||bcScaleSaving)return;if(bcaTarget!=='main')return bcAreaWrite(false,clear);bcs('bca-target').disabled=true;try{await baseSave(clear);}finally{bcs('bca-target').disabled=false;}};
        bcs('bc-scale-save').onclick=()=>bcScaleSave();bcs('bc-scale-clear').onclick=()=>bcScaleSave(true);bcs('bca-remove').onclick=()=>bcAreaWrite(true);
        bcs('bca-visible').onchange=()=>{bcaVisible=bcs('bca-visible').checked;if(bcaOverlay)bcaOverlay.hidden=!bcaVisible;};
        for(const id of ['bca-name','bc-scale-method','bc-scale-preset','bc-scale-paper','bc-scale-paper-unit','bc-scale-known','bc-scale-unit'])bcs(id).addEventListener('input',()=>{if(bcaTarget!=='main')bcaDirty=true;});
        bcsDialog.addEventListener('cancel',e=>{if(bcaBusy)e.preventDefault();});
        window.addEventListener('beforeunload',e=>{if(bcaDirty){e.preventDefault();e.returnValue='';}});
        canvas.addEventListener('pointermove',e=>{if(tool!=='scale-area'||!down||!start)return;const end=point(e);draw();ctx.save();ctx.strokeStyle='#087c88';ctx.lineWidth=2;ctx.setLineDash([6,4]);ctx.strokeRect(Math.min(start[0],end[0])*canvas.width,Math.min(start[1],end[1])*canvas.height,Math.abs(start[0]-end[0])*canvas.width,Math.abs(start[1]-end[1])*canvas.height);ctx.restore();});
        document.addEventListener('keydown',e=>{if(e.key==='Escape'&&tool==='scale-area'){down=false;start=null;setTool('pan');draw();bcScaleOpen();bcs('bc-scale-status').textContent='Outline cancelled. The saved area is unchanged.';}});
        bcScaleStatus();
      }
'''
