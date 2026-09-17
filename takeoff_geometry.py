"""Deterministic source-coordinate takeoff math; no screen pixels or AI quantities."""
import math
from drawing_scale import UNITS

EPS = 1e-10
OUTPUT = {'length': {'LF': .3048, 'M': 1.0}, 'area': {'SF': .3048**2, 'M2': 1.0}, 'count': {'EA': 1.0}}

def points(value, minimum):
    if not isinstance(value, list) or not minimum <= len(value) <= 256:
        raise ValueError(f'Use {minimum} to 256 points per outline.')
    if any(not isinstance(p, list) or len(p) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in p) for p in value):
        raise ValueError('Keep every point inside the drawing.')
    if any(a == b for a, b in zip(value, value[1:])):
        raise ValueError('Choose distinct consecutive points.')
    return value

def cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

def on(a,b,p):
    return abs(cross(a,b,p)) <= EPS and min(a[0],b[0])-EPS <= p[0] <= max(a[0],b[0])+EPS and min(a[1],b[1])-EPS <= p[1] <= max(a[1],b[1])+EPS

def intersects(a,b,c,d):
    return (cross(a,b,c)*cross(a,b,d) < -EPS**2 and cross(c,d,a)*cross(c,d,b) < -EPS**2) or any((on(a,b,c),on(a,b,d),on(c,d,a),on(c,d,b)))

def edges(poly):
    return list(zip(poly, poly[1:]+poly[:1]))

def inside(poly,p):
    if any(on(a,b,p) for a,b in edges(poly)): return False  # strict containment
    odd=False
    for a,b in edges(poly):
        if (a[1]>p[1]) != (b[1]>p[1]) and p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]: odd=not odd
    return odd

def area(poly):
    return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in edges(poly)))/2

def polygon(value):
    p=points(value,3); es=edges(p)
    if area(p) <= EPS: raise ValueError('Outline a nonzero area.')
    for i,(a,b) in enumerate(es):
        for j,(c,d) in enumerate(es):
            if j <= i or j==i+1 or (i==0 and j==len(es)-1):continue
            if intersects(a,b,c,d):raise ValueError('Area outlines cannot cross or touch themselves.')
    if len({tuple(x) for x in p}) != len(p):raise ValueError('Do not repeat area corners; Finish closes the outline.')
    # Backtracking collinear edges share more than their common endpoint.
    for i,b in enumerate(p):
        a,c=p[i-1],p[(i+1)%len(p)]
        if abs(cross(a,b,c))<=EPS and (on(a,b,c) or on(b,c,a)):raise ValueError('Area edges cannot double back.')
    return p

def touching(a,b):
    return any(intersects(x,y,z,w) for x,y in edges(a) for z,w in edges(b))

def calculate(shape,state,kind,unit):
    if kind not in OUTPUT or unit not in OUTPUT[kind]:raise ValueError('Choose a compatible takeoff unit.')
    if not isinstance(shape,dict):raise ValueError('Choose the measurement points.')
    p=polygon(shape.get('points')) if kind=='area' else points(shape.get('points'),2 if kind=='length' else 1)
    holes=shape.get('holes',[])
    if not isinstance(holes,list) or len(holes)>16 or (kind!='area' and holes):raise ValueError('Only areas support cutouts, up to 16 per outline.')
    holes=[polygon(h) for h in holes]
    if len(p)+sum(len(h) for h in holes)>1024:raise ValueError('Split this area into smaller outlines; each measurement supports 1,024 total points.')
    for i,h in enumerate(holes):
        if touching(p,h) or not all(inside(p,x) for x in h):raise ValueError('Keep cutouts completely inside their parent area.')
        for prev in holes[:i]:
            if touching(prev,h) or inside(prev,h[0]) or inside(h,prev[0]):raise ValueError('Cutouts cannot overlap or contain each other.')
    clean={'points':p,'holes':holes}
    if kind=='count' and len({tuple(q) for q in p})!=len(p):raise ValueError('Do not count the same point twice.')
    if kind=='count':return dict(shape=clean,gross=len(p),deductions=0,net=len(p),unit=unit,calibration=None)
    segments=edges(p) if kind=='area' else list(zip(p,p[1:]))
    hits=[]
    for detail in state.get('areas',[]):
        x,y,w,h=detail['rect']; rect=[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
        within=lambda q: x-EPS<=q[0]<=x+w+EPS and y-EPS<=q[1]<=y+h+EPS
        if any(within(q) for q in p) or any(intersects(a,b,c,d) for a,b in segments for c,d in edges(rect)) or (kind=='area' and any(inside(p,q) for q in rect)):
            hits.append(detail)
    if len(hits)>1:raise ValueError('Split this measurement at detail scale boundaries.')
    if hits:
        chosen=hits[0];x,y,w,h=chosen['rect']
        if not all(x-EPS<=q[0]<=x+w+EPS and y-EPS<=q[1]<=y+h+EPS for q in p):raise ValueError('Keep each measurement inside one scale area, or split it.')
        calibration=chosen.get('profile')
        if chosen.get('stale_source'):calibration=None
        label=chosen['name']
    else:
        calibration=state.get('profile');label='Sheet scale'
    if not calibration:raise ValueError('Set a scale for this sheet or detail before measuring it.')
    g=state['geometry']; factor=calibration['factor']*UNITS[calibration['unit']]
    if kind=='length':
        gross=sum(math.hypot((a[0]-b[0])*g['width'],(a[1]-b[1])*g['height']) for a,b in segments)*factor/OUTPUT[kind][unit];deduction=0
    else:
        multiplier=g['width']*g['height']*factor**2/OUTPUT[kind][unit]
        gross=area(p)*multiplier;deduction=sum(area(h) for h in holes)*multiplier
    if not all(math.isfinite(v) and 0<=v<=1e12 for v in (gross,deduction)) or gross-deduction<=1e-9:raise ValueError('Check the outline and scale; the quantity is outside the supported range.')
    return dict(shape=clean,gross=gross,deductions=deduction,net=gross-deduction,unit=unit,calibration={'name':label,'profile':calibration,'version':hits[0]['version'] if hits else state['version'],'area_id':hits[0]['id'] if hits else None})

def scale_stamp(state,kind):
    result={k:state[k] for k in ('sheet_id','source_key')}
    if kind!='count':result.update(version=state['version'],areas_revision=state.get('areas_revision',0))
    return result

def totals(rows,waste):
    gross=math.fsum(r['gross'] for r in rows);deductions=math.fsum(r['deductions'] for r in rows);net=gross-deductions
    return dict(gross=gross,deductions=deductions,net=net,waste_pct=waste,waste=net*waste/100,quantity=round(net*(1+waste/100),6))
