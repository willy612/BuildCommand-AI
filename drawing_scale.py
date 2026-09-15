"""8.18.0: scale belongs to a registered sheet revision, never a whole upload.

Measurements use normalized endpoints and source page geometry, not screen
pixels. Scale saves are independent of markup saves and retain optimistic
version checks. Existing measurement labels are immutable snapshots.
"""
import json
import logging
import math
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from blueprint_field import digest
from drawing_register import js

VERSION = '8.18.0'
RELEASE = 'Sheet Scale & Calibration'
BASE = '/workspace/drawing-sheets'
UNITS = {'ft': .3048, 'in': .0254, 'm': 1., 'mm': .001}
log = logging.getLogger('buildcommand.drawing_scale')


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Enter a positive number, such as 40, 1/4 or 1 1/2.')
    try:
        words = str(value).strip().split()
        if len(words) == 2 and '/' in words[1] and words[0].isdigit():
            number = float(Fraction(words[0]) + Fraction(words[1]))
        elif len(words) == 1:
            number = float(Fraction(words[0]))
        else:
            raise ValueError()
    except (ValueError, ZeroDivisionError, OverflowError):
        raise ValueError('Enter a positive number, such as 40, 1/4 or 1 1/2.') from None
    if not math.isfinite(number) or not 0 < number <= 1e9:
        raise ValueError('Enter a positive length within the supported range.')
    return number


def endpoints(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('Draw a reference across a known dimension on this sheet.')
    for p in value:
        if not isinstance(p, list) or len(p) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in p):
            raise ValueError('Keep both measurement endpoints inside the drawing.')
    if value[0] == value[1]:
        raise ValueError('Choose two different endpoints on the known dimension.')
    return value


def page_geometry(page):
    """PDF.js view is the nonempty intersection of MediaBox and CropBox.

    Width/height are rotated source units. /UserUnit is physical size, in
    multiples of 1/72 inch; PDF.js 3.x viewport dimensions omit that multiplier.
    """
    media = [float(v) for v in page.mediabox]
    crop = [float(v) for v in page.cropbox]
    def ordered(b):
        return [min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])]
    media, crop = ordered(media), ordered(crop)
    box = [max(media[0], crop[0]), max(media[1], crop[1]), min(media[2], crop[2]), min(media[3], crop[3])]
    if box[2] <= box[0] or box[3] <= box[1]:
        box = media
    width, height = box[2]-box[0], box[3]-box[1]
    rotation = int(page.get('/Rotate', 0)) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError('This PDF has an unsupported page rotation.')
    if rotation in (90, 270):
        width, height = height, width
    user_unit = float(page.get('/UserUnit', 1))
    if any(not math.isfinite(v) or v <= 0 for v in (width, height, user_unit)):
        raise ValueError('This PDF has invalid page dimensions.')
    return {'kind': 'pdf', 'width': width, 'height': height, 'user_unit': user_unit,
            'paper_width_in': width*user_unit/72, 'paper_height_in': height*user_unit/72}


@lru_cache(maxsize=64)
def source_geometry(path, size, modified_ns, page_number):
    # The immutable stored upload and its file stat are part of the cache key.
    if size > 500*1024*1024:
        raise ValueError('This drawing exceeds the supported file size.')
    with open(path, 'rb') as stream:
        prefix = stream.read(8)
        stream.seek(0)
        if prefix.startswith(b'%PDF-'):
            from pypdf import PdfReader
            reader = PdfReader(stream)
            if not 1 <= page_number <= len(reader.pages):
                raise ValueError('This sheet is outside the source PDF.')
            return page_geometry(reader.pages[page_number-1])
        from PIL import Image
        with Image.open(stream) as img:
            width, height = img.size
            if img.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
            if page_number != 1 or width <= 0 or height <= 0:
                raise ValueError('This image has invalid sheet dimensions.')
            return {'kind': 'image', 'width': width, 'height': height, 'user_unit': None,
                    'paper_width_in': None, 'paper_height_in': None}


def profile(data, geometry):
    mode = data.get('method')
    if mode == 'clear':
        return None
    unit = data.get('unit')
    if unit not in UNITS:
        raise ValueError('Choose feet, inches, meters or millimeters.')
    known = positive(data.get('known'))
    result = {'method': mode, 'unit': unit, 'known': known, 'space': 'base', 'scale_schema': 1,
              'width': geometry['width'], 'height': geometry['height']}
    if mode == 'preset':
        if geometry['kind'] != 'pdf':
            raise ValueError('Use a known dimension to calibrate an image or scan.')
        if data.get('original_size_confirmed') is not True:
            raise ValueError('Confirm the PDF is at the stated drawing size, or use a known dimension.')
        paper = positive(data.get('paper'))
        paper_unit = data.get('paper_unit')
        if paper_unit not in ('in', 'mm'):
            raise ValueError('Choose inches or millimeters for the paper dimension.')
        paper_inches = paper if paper_unit == 'in' else paper / 25.4
        result.update(paper=paper, paper_unit=paper_unit,
                      factor=known*geometry['user_unit']/(paper_inches*72),
                      label=f'{paper:g} {paper_unit} = {known:g} {unit}')
    elif mode == 'reference':
        points = endpoints(data.get('points'))
        distance = math.hypot((points[1][0]-points[0][0])*geometry['width'],
                              (points[1][1]-points[0][1])*geometry['height'])
        if distance < 1e-4:
            raise ValueError('Draw a longer reference for reliable calibration.')
        result.update(points=points, factor=known/distance, label=f'{known:g} {unit} reference')
    else:
        raise ValueError('Choose a printed scale or a known dimension.')
    if not math.isfinite(result['factor']) or not 1e-12 <= result['factor'] <= 1e12:
        raise ValueError('This scale is outside the supported range. Check the dimensions.')
    return result


def install(ns):
    service = DrawingScale(ns)
    ns['app'].state.drawing_scale = service
    service.register()
    return service


class DrawingScale:
    def __init__(self, ns):
        self.ns = ns
        self.pins = ns['app'].state.drawing_field
        self.field, self.reg, self.db = self.pins.field, self.pins.reg, self.pins.db
        self.require = self.field.require
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_drawing_scales(
                    id {key}, company_id BIGINT NOT NULL, project_id BIGINT NOT NULL,
                    sheet_id BIGINT NOT NULL, version BIGINT NOT NULL, source_key TEXT NOT NULL,
                    profile_json TEXT NOT NULL, updated_by BIGINT NOT NULL, updated TEXT NOT NULL,
                    UNIQUE(company_id,project_id,sheet_id))''')
            return True
        except Exception:
            log.exception('Drawing scale setup failed')
            return False

    def context(self, c, sheet_id, write=False):
        self.require(self.schema_ready, 'Sheet scales are unavailable. Ask your administrator to check the installation.', 503)
        user, project, sheet = self.pins.context(c, sheet_id)
        user, project = self.field.actor(c, project['id'], write)
        if write:
            self.require(self.pins.head(c, user, sheet) == sheet_id,
                         'Open the current sheet revision before changing its scale.', 409)
        _, attachment, _ = self.pins.drawings.context(c, sheet['attachment_id'])
        path = self.pins.drawings.file_path(attachment)
        stat = path.stat()
        try:
            geometry = dict(source_geometry(str(path), stat.st_size, stat.st_mtime_ns, int(sheet['page_number'])))
        except Exception:
            log.warning('Drawing scale geometry unavailable sheet=%s', sheet_id)
            self.require(False, 'The source page size could not be read. Reopen the original drawing and check the file.', 409)
        source_key = digest(json.dumps({'sheet': sheet_id, 'attachment': sheet['attachment_id'], 'page': sheet['page_number'],
                             'file': attachment['stored_name'], 'size': stat.st_size, 'modified': stat.st_mtime_ns,
                             'geometry': geometry}, sort_keys=True, separators=(',', ':')))
        return user, project, sheet, geometry, source_key

    def stored(self, c, user, sheet):
        row = c.execute('SELECT * FROM bc_drawing_scales WHERE company_id=? AND project_id=? AND sheet_id=?',
                        (user['company_id'], sheet['project_id'], sheet['id'])).fetchone()
        return dict(row) if row else None

    def state(self, c, user, sheet, geometry, source_key):
        row = self.stored(c, user, sheet)
        stale = row is not None and row['source_key'] != source_key
        result = {'sheet_id': sheet['id'], 'sheet_number': sheet['sheet_number'], 'page': sheet['page_number'],
                'version': int(row['version']) if row else 0, 'source_key': source_key, 'geometry': geometry,
                'profile': json.loads(row['profile_json']) if row and not stale else None,
                'stale_source': stale, 'current': self.pins.head(c, user, sheet) == sheet['id']}
        areas = getattr(self.ns['app'].state, 'drawing_scale_areas', None)
        return areas.enrich(c, user, sheet, source_key, result) if areas else result

    def get(self, sheet_id: int):
        with self.db() as c:
            user, _, sheet, geometry, source_key = self.context(c, sheet_id)
            return JSONResponse(self.state(c, user, sheet, geometry, source_key), headers={'Cache-Control': 'private, no-store'})

    async def save(self, sheet_id: int, request: Request):
        self.require(self.ns['_bc840_same_origin'](request), 'Reload this drawing before saving its scale.', 403)
        # Authorize before parsing input, then authorize again under the write lock.
        with self.db() as c:
            self.context(c, sheet_id)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            self.require(size <= 8192, 'The scale request is too large.', 413)
            chunks.append(chunk)
        try:
            data = json.loads(b''.join(chunks))
            if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] < 0:
                raise ValueError('Reload the sheet before saving its scale.')
            with self.db(True) as c:
                user, project, sheet, geometry, source_key = self.context(c, sheet_id, True)
                row = self.stored(c, user, sheet)
                version = int(row['version']) if row else 0
                self.require(data['version'] == version, 'Someone changed this sheet scale. Reload to review their change before saving.', 409)
                self.require(data.get('source_key') == source_key, 'The source drawing changed. Reload and calibrate this sheet again.', 409)
                value = profile(data, geometry)
                encoded = json.dumps(value, allow_nan=False, separators=(',', ':'))
                if row and row['source_key'] == source_key and row['profile_json'] == encoded:
                    return JSONResponse(self.state(c, user, sheet, geometry, source_key))
                now = self.field.now().isoformat()
                if row:
                    c.execute('UPDATE bc_drawing_scales SET version=?,source_key=?,profile_json=?,updated_by=?,updated=? WHERE id=? AND company_id=? AND project_id=?',
                              (version+1, source_key, encoded, user['id'], now, row['id'], user['company_id'], project['id']))
                else:
                    self.reg.insert(c, '''INSERT INTO bc_drawing_scales(company_id,project_id,sheet_id,version,source_key,profile_json,updated_by,updated)
                        VALUES(?,?,?,?,?,?,?,?)''', (user['company_id'], project['id'], sheet_id, 1, source_key, encoded, user['id'], now))
                self.ns['_bc850_event'](c, user, project['id'], None, 'DRAWING_SCALE_SAVED:'+str(sheet_id)+':'+str(version+1))
                result = self.state(c, user, sheet, geometry, source_key)
            return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})
        except (ValueError, TypeError, OverflowError) as exc:
            return JSONResponse({'error': str(exc) if isinstance(exc, ValueError) else 'Check the scale values and try again.'}, 400)

    def decorate(self, response, sheet_id):
        html = response.body.decode()
        if 'id="bcMarkupCanvas"' not in html:
            return response  # Existing read-only and shared viewers retain their permissions.
        with self.db() as c:
            user, _, sheet, geometry, source_key = self.context(c, sheet_id)
            config = self.state(c, user, sheet, geometry, source_key)
        html = instrument(html, config)
        areas = getattr(self.ns['app'].state, 'drawing_scale_areas', None)
        if areas:
            html = areas.instrument(html, config)
        return HTMLResponse(html, headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'same-origin'})

    def health(self):
        active = {(r.path, m): r.endpoint for r in self.ns['app'].routes for m in (getattr(r, 'methods', None) or [])}
        checks = {m+' '+path: active.get((path, m)) is endpoint for path, m, endpoint in self.routes}
        checks.update(scale_schema_initialized=self.schema_ready, per_sheet_revision_storage=True,
                      source_page_geometry=True, zoom_independent_measurements=True, printed_scale_presets=True,
                      known_dimension_calibration=True, scale_version_guard=True, measurements_keep_original_labels=True,
                      full_page_drawings_preserved=all(json.loads(self.ns['app'].state.drawing_work.full_page_health().body)['checks'].values()),
                      form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                c.execute('SELECT sheet_id,version,source_key,profile_json FROM bc_drawing_scales WHERE 1=0')
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        return dict(app='BuildCommand AI', version=VERSION, release=RELEASE, status='ok' if all(checks.values()) else 'degraded',
                    checks=checks, passed=sum(checks.values()), total=len(checks), data_reset=False,
                    scope='Schema and installation checks only. Verify two differently scaled sheets, a known dimension, zoom, reload, saved measurements and project access on staging. Printed scales require a correctly sized PDF; mixed-scale details need their own reference.')

    def register(self):
        for path, method, endpoint in [(BASE+'/{sheet_id}/scale', 'GET', self.get), (BASE+'/{sheet_id}/scale', 'POST', self.save)]:
            self.ns['_bc840_replace'](path, method, self.pins.drawings.endpoint(endpoint))
            self.routes.append((path, method, next(r.endpoint for r in self.ns['app'].routes if r.path == path and method in r.methods)))
        path = '/health/drawing-scale-8-18-0'
        self.ns['app'].add_api_route(path, self.health, methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add(path)


def instrument(html, config):
    # Extend the existing canvas closure; do not create a competing renderer.
    anchors = ['      async function fitToScreen(){', '      function updateCal(){',
               '        if(tool==="calibrate"){', '        if(tool==="measure"){',
               '      document.getElementById("bcCalibrate").onclick=']
    if any(html.count(a) != 1 for a in anchors):
        raise RuntimeError('The drawing scale integration does not match this installed viewer.')
    html = html.replace('</head>', STYLE+'<script>window.BC_SHEET_SCALE='+js(config)+';'+MATH_JS+'</script></head>', 1)
    html = html.replace('<button type="button" id="df-markup">', '<button type="button" id="bcSheetScale">Set sheet scale</button><button type="button" id="df-markup">', 1)
    # Dialog must exist before the original canvas script executes.
    html = html.replace('    <script>\n    (function(){', DIALOG+'    <script>\n    (function(){', 1)
    html = html.replace(anchors[0], CLIENT_JS+'\n'+anchors[0], 1)
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(anchors[1]):
            lines[i] = '      function updateCal(){bcScaleStatus();}'
        elif line.startswith(anchors[2]):
            lines[i] = '        if(tool==="calibrate"){bcScaleReference(start,end);current=null;draw();return;}'
        elif line.startswith(anchors[3]):
            lines[i] = '        if(tool==="measure"){bcScaleMeasure(start,end,color);}'
        elif line.startswith(anchors[4]):
            lines[i] = '      document.getElementById("bcCalibrate").onclick=()=>bcScaleOpen();'
    html = '\n'.join(lines)
    html = html.replace('function setTool(t){tool=t;', 'function setTool(t){if(t==="measure"&&!bcScaleState.profile){bcScaleOpen();return;}document.body.classList.toggle("bcs-measuring",["measure","calibrate"].includes(t));if(["measure","calibrate"].includes(t))window.dispatchEvent(new Event("bc-scale-mode"));tool=t;', 1)
    html = html.replace("function stopPicking(){picking=false;", "window.addEventListener('bc-scale-mode',()=>stopPicking());\nfunction stopPicking(){picking=false;", 1)
    return html


STYLE = '''<style>
#bc-scale-dialog{box-sizing:border-box;width:min(540px,calc(100vw - 24px));max-height:calc(100dvh - 24px);overflow:auto;border:1px solid #bdcddb;border-radius:14px;background:#fff;color:#18334a;padding:22px;font:15px/1.5 Arial,sans-serif}#bc-scale-dialog::backdrop{background:#0c243c80}#bc-scale-dialog h2{margin:0;font-size:24px}#bc-scale-dialog p{margin:10px 0}#bc-scale-dialog label{display:block;font-weight:600;margin:12px 0}#bc-scale-dialog input:not([type=checkbox]),#bc-scale-dialog select{box-sizing:border-box;width:100%;min-height:44px;padding:9px;border:1px solid #a9bfce;border-radius:7px;background:white;color:#16334a;font:16px Arial,sans-serif}#bc-scale-dialog button{min-height:44px;padding:10px 14px;border:1px solid #b4c7d5;border-radius:8px;background:white;color:#193f5a;font-weight:700;cursor:pointer}#bc-scale-dialog .bcs-primary{background:#173f5d;color:white}#bc-scale-dialog .bcs-row{display:flex;gap:10px;align-items:end}#bc-scale-dialog .bcs-row>label{flex:1;min-width:0}#bc-scale-dialog .bcs-head{display:flex;justify-content:space-between;align-items:center;gap:12px}#bc-scale-dialog .bcs-help{font-size:13px;color:#53687b}#bc-scale-dialog .bcs-note{background:#fff4da;color:#69490f;padding:10px;border-radius:7px}#bc-scale-dialog [hidden]{display:none!important}#bc-scale-dialog button:focus-visible,#bcSheetScale:focus-visible{outline:3px solid #ba831e;outline-offset:2px}#bc-scale-status{min-height:24px}#bcSheetScale{max-width:230px;white-space:normal!important}#bc-scale-dialog input[type=checkbox]{width:20px;height:20px;vertical-align:middle}body.dw-fullpage #bcMeasureStatus{display:block;font-size:12px;min-height:18px}#bcMarkupCanvas{touch-action:none}body.bcs-measuring .df-pin-layer,body.bcs-measuring .df-pin{pointer-events:none!important}
</style>'''

DIALOG = '''<dialog id="bc-scale-dialog" aria-labelledby="bc-scale-title"><div class="bcs-head"><h2 id="bc-scale-title">Set this sheet's scale</h2><button type="button" id="bc-scale-close">Close</button></div>
<p id="bc-scale-sheet" class="bcs-help"></p>
<label>How do you want to set it?<select id="bc-scale-method"><option value="preset">Use the printed scale</option><option value="reference">Use a known dimension</option></select></label>
<div id="bc-scale-printed"><label>Printed scale<select id="bc-scale-preset"><optgroup label="Site / civil"><option value="1|in|40|ft">1 inch = 40 feet</option><option value="1|in|10|ft">1 inch = 10 feet</option><option value="1|in|20|ft">1 inch = 20 feet</option><option value="1|in|30|ft">1 inch = 30 feet</option><option value="1|in|50|ft">1 inch = 50 feet</option><option value="1|in|60|ft">1 inch = 60 feet</option><option value="1|in|100|ft">1 inch = 100 feet</option></optgroup><optgroup label="Architectural"><option value="1/16|in|1|ft">1/16 inch = 1 foot</option><option value="1/8|in|1|ft">1/8 inch = 1 foot</option><option value="3/16|in|1|ft">3/16 inch = 1 foot</option><option value="1/4|in|1|ft">1/4 inch = 1 foot</option><option value="3/8|in|1|ft">3/8 inch = 1 foot</option><option value="1/2|in|1|ft">1/2 inch = 1 foot</option><option value="3/4|in|1|ft">3/4 inch = 1 foot</option><option value="1|in|1|ft">1 inch = 1 foot</option><option value="1 1/2|in|1|ft">1 1/2 inches = 1 foot</option><option value="3|in|1|ft">3 inches = 1 foot</option></optgroup><optgroup label="Metric"><option value="1|mm|20|mm">1:20</option><option value="1|mm|50|mm">1:50</option><option value="1|mm|100|mm">1:100</option><option value="1|mm|200|mm">1:200</option><option value="1|mm|500|mm">1:500</option></optgroup><option value="custom">Custom scale</option></select></label>
<div class="bcs-row"><label>On the drawing<input id="bc-scale-paper" value="1" maxlength="24" inputmode="text"></label><label>Paper unit<select id="bc-scale-paper-unit"><option value="in">Inches</option><option value="mm">Millimeters</option></select></label></div>
<label class="bcs-help"><input id="bc-scale-confirm" type="checkbox"> The PDF is at the size stated on the plan.</label><p class="bcs-help">A reduced or resized PDF needs calibration from a known dimension. Screen zoom does not affect the scale.</p></div>
<div class="bcs-row"><label id="bc-scale-known-label">Actual length<input id="bc-scale-known" value="40" maxlength="24" inputmode="text"></label><label>Actual unit<select id="bc-scale-unit"><option value="ft">Feet</option><option value="in">Inches</option><option value="m">Meters</option><option value="mm">Millimeters</option></select></label></div>
<div id="bc-scale-reference" hidden><p class="bcs-help">Enter a dimension printed on this sheet, then drag exactly between its two endpoints. For 10 feet 6 inches, enter 10.5 feet or 126 inches.</p><button id="bc-scale-draw" type="button">Draw reference line</button><p id="bc-scale-reference-status">No reference selected.</p></div>
<p class="bcs-note">Applies only to this sheet revision. Details marked with another scale need a separate reference. Check a second known dimension before relying on measurements.</p>
<p id="bc-scale-status" role="status" aria-live="polite"></p><div class="bcs-row"><button type="button" id="bc-scale-save" class="bcs-primary">Save sheet scale</button><button type="button" id="bc-scale-clear">Not to scale / clear</button></div><p class="bcs-help">Scale saves immediately. Use Save markups to keep measurement lines. Existing measurement labels stay as recorded.</p></dialog>
'''

MATH_JS = r'''
window.BCSScaleMath=Object.freeze({
 number(value){const s=String(value).trim();if(!/^(?:\d+(?:\.\d+)?|\d+\/\d+|\d+ \d+\/\d+)$/.test(s))throw Error('Enter a positive number, such as 40, 1/4 or 1 1/2.');const terms=s.split(' ');const n=terms.reduce((sum,t)=>{const parts=t.split('/').map(Number);return sum+parts[0]/(parts.length===2?parts[1]:1);},0);if(!Number.isFinite(n)||n<=0||n>1e9)throw Error('Enter a positive length within the supported range.');return n;},
 distance(a,b,g){if(![...a,...b].every(v=>Number.isFinite(v)&&v>=0&&v<=1))throw Error('Keep both endpoints inside the drawing.');return Math.hypot((b[0]-a[0])*g.width,(b[1]-a[1])*g.height);},
 length(a,b,g,c){const n=this.distance(a,b,g)*c.factor;if(!Number.isFinite(n)||n<=0)throw Error('Choose two different measurement endpoints.');return n;},
 label(n,unit){if(unit==='ft'){const eighths=Math.round(n*96),feet=Math.floor(eighths/96),inch8=eighths%96,inches=Math.floor(inch8/8),part=inch8%8;const fractions=['','1/8','1/4','3/8','1/2','5/8','3/4','7/8'];return feet+"' "+inches+(part?' '+fractions[part]:'')+'" ('+n.toFixed(2)+' ft)';}return n.toFixed(unit==='mm'?1:2)+' '+unit;}
});
'''

CLIENT_JS = r'''
      let bcScaleState=window.BC_SHEET_SCALE,bcScalePoints=null,bcScaleSaving=false;
      const bcs=id=>document.getElementById(id),bcsMath=window.BCSScaleMath,bcsDialog=bcs('bc-scale-dialog');
      document.body.append(bcsDialog); // Keep this dialog outside the later history dialog.
      function bcScaleSync(){if(bcScaleState.profile)state.calibration[key()]={...bcScaleState.profile,scale_version:bcScaleState.version,sheet_id:bcScaleState.sheet_id};else delete state.calibration[key()];}
      function bcScaleStatus(){const c=bcScaleState.profile;const label=c?c.label:'Not calibrated';bcs('bcSheetScale').textContent=c?'Scale: '+label:'Set sheet scale';bcs('bcMeasureStatus').textContent='Sheet '+bcScaleState.sheet_number+' | '+label+(c?' | New measurements use this scale.':' | Set a scale before measuring.');}
      function bcScaleMode(){const ref=bcs('bc-scale-method').value==='reference';bcs('bc-scale-printed').hidden=ref;bcs('bc-scale-reference').hidden=!ref;}
      function bcScaleOpen(){window.dispatchEvent(new Event('bc-scale-mode'));setTool('pan');bcMenu?.classList.remove('open');bcs('bc-scale-status').textContent=bcScaleState.current?(bcScaleState.stale_source?'The source changed. Set a new scale.':''):'Previous revision: open the current sheet to change its scale.';bcs('bc-scale-save').disabled=!bcScaleState.current;bcs('bc-scale-clear').disabled=!bcScaleState.current;if(!bcsDialog.open)bcsDialog.showModal();}
      function bcScaleReference(a,b){setTool('pan');try{const distance=bcsMath.distance(a,b,bcScaleState.geometry);if(distance<1e-4)throw Error('Draw a longer reference line.');bcScalePoints=[a,b];bcs('bc-scale-reference-status').textContent='Reference selected. Save to apply the actual length.';bcScaleOpen();}catch(error){bcScaleOpen();bcs('bc-scale-status').textContent=error.message;}}
      function bcScaleMeasure(a,b,color){try{const c=bcScaleState.profile;if(!c){bcScaleOpen();return;}if(pageNum!==bcScaleState.page)throw Error('Open this page from Sheets before measuring it.');const real=bcsMath.length(a,b,bcScaleState.geometry,c);items().push({type:'measure',x1:a[0],y1:a[1],x2:b[0],y2:b[1],label:bcsMath.label(real,c.unit),color,measurement:{value:real,unit:c.unit,scale_version:bcScaleState.version,sheet_id:bcScaleState.sheet_id,method:c.method,label:c.label,factor:c.factor}});bcs('bcMeasureStatus').textContent='Measured '+bcsMath.label(real,c.unit)+' | Save markups to keep this line.';}catch(error){bcs('drawing-load-state').textContent=error.message;}}
      async function bcScaleSave(clear=false){if(bcScaleSaving)return;const status=bcs('bc-scale-status');try{const data={version:bcScaleState.version,source_key:bcScaleState.source_key,method:clear?'clear':bcs('bc-scale-method').value};if(!clear){data.known=bcsMath.number(bcs('bc-scale-known').value);data.unit=bcs('bc-scale-unit').value;if(data.method==='preset'){data.paper=bcsMath.number(bcs('bc-scale-paper').value);data.paper_unit=bcs('bc-scale-paper-unit').value;data.original_size_confirmed=bcs('bc-scale-confirm').checked;if(!data.original_size_confirmed)throw Error('Confirm the PDF size or choose a known dimension.');}else{if(!bcScalePoints)throw Error('Draw a reference across a known dimension first.');data.points=bcScalePoints;}}bcScaleSaving=true;bcs('bc-scale-save').disabled=true;bcs('bc-scale-clear').disabled=true;status.textContent='Saving scale...';const r=await fetch('/workspace/drawing-sheets/'+bcScaleState.sheet_id+'/scale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const type=r.headers.get('content-type')||'';const body=type.includes('application/json')?await r.json():{error:new DOMParser().parseFromString(await r.text(),'text/html').querySelector('p')?.textContent};if(!r.ok)throw Error(body.error||'The scale could not be saved. Reload to check your access.');bcScaleState=body;window.BC_SHEET_SCALE=body;bcScaleSync();bcScaleStatus();status.textContent='Saved for this sheet only.';bcsDialog.close();setTool(clear?'pan':'measure');bcs('drawing-load-state').textContent=clear?'Scale cleared. Existing measurement labels are unchanged.':'Scale saved. Check a second known dimension, then Save markups to keep measurement lines.';}catch(error){status.textContent=error.message||'Connection interrupted. Your values are still here; try again.';}finally{bcScaleSaving=false;bcs('bc-scale-save').disabled=!bcScaleState.current;bcs('bc-scale-clear').disabled=!bcScaleState.current;}}
      bcs('bc-scale-sheet').textContent='Sheet '+bcScaleState.sheet_number+' | Source page '+bcScaleState.page+(bcScaleState.geometry.kind==='pdf'?' | PDF size '+bcScaleState.geometry.paper_width_in.toFixed(2)+' x '+bcScaleState.geometry.paper_height_in.toFixed(2)+' inches':' | Image: use a known dimension');
      bcs('bcSheetScale').onclick=bcScaleOpen;bcs('bc-scale-close').onclick=()=>bcsDialog.close();bcs('bc-scale-method').onchange=bcScaleMode;
      bcs('bc-scale-preset').onchange=()=>{const v=bcs('bc-scale-preset').value;if(v==='custom')return;const p=v.split('|');bcs('bc-scale-paper').value=p[0];bcs('bc-scale-paper-unit').value=p[1];bcs('bc-scale-known').value=p[2];bcs('bc-scale-unit').value=p[3];bcs('bc-scale-confirm').checked=false;};
      for(const id of ['bc-scale-paper','bc-scale-paper-unit','bc-scale-known','bc-scale-unit'])bcs(id).addEventListener('input',()=>{bcs('bc-scale-preset').value='custom';bcs('bc-scale-confirm').checked=false;});
      bcs('bc-scale-draw').onclick=()=>{try{bcsMath.number(bcs('bc-scale-known').value);bcsDialog.close();bcMenu.classList.remove('open');setTool('calibrate');bcs('drawing-load-state').textContent='Drag from one end of the known dimension to the other. Press Escape to cancel.';}catch(error){bcs('bc-scale-status').textContent=error.message;}};
      bcs('bc-scale-save').onclick=()=>bcScaleSave();bcs('bc-scale-clear').onclick=()=>bcScaleSave(true);
      if(bcScaleState.profile){const c=bcScaleState.profile;bcs('bc-scale-method').value=c.method;bcs('bc-scale-known').value=c.known;bcs('bc-scale-unit').value=c.unit;bcs('bc-scale-preset').value='custom';if(c.method==='preset'){bcs('bc-scale-paper').value=c.paper;bcs('bc-scale-paper-unit').value=c.paper_unit;}else{bcScalePoints=c.points;bcs('bc-scale-reference-status').textContent='Saved reference selected. Draw again to replace it.';}}
      if(bcScaleState.geometry.kind!=='pdf'){bcs('bc-scale-method').value='reference';bcs('bc-scale-method').querySelector('[value=preset]').disabled=true;}
      bcScaleSync();bcScaleMode();
      canvas.addEventListener('pointermove',e=>{if(!down||!start||!['calibrate','measure'].includes(tool))return;const end=point(e);draw();ctx.save();ctx.strokeStyle='#e3a623';ctx.lineWidth=2;ctx.setLineDash([6,4]);arrow(start[0]*canvas.width,start[1]*canvas.height,end[0]*canvas.width,end[1]*canvas.height);ctx.restore();});
      canvas.addEventListener('pointercancel',()=>{down=false;start=null;current=null;draw();});
      document.addEventListener('keydown',e=>{if(e.key==='Escape'&&tool==='calibrate'){down=false;start=null;setTool('pan');draw();bcs('drawing-load-state').textContent='Reference cancelled. The saved scale is unchanged.';}});
'''
