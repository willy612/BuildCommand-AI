"""Server-rendered, accessible UI for the 8.29 project lookahead."""
from datetime import timedelta
from urllib.parse import urlencode
import json
from blueprint_field import esc
from lookahead_calendar import WorkCalendar, day, completed

BASE='/workspace/schedule'
STYLE='''<style>
.la{--la-navy:#18324b;--la-blue:#1f66b0;--la-muted:#51657b;color:#18324b;min-width:0;max-width:100%}
.la *{box-sizing:border-box}.la-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin:6px 0 20px}.la h1{font-size:30px;line-height:1.2;margin:5px 0 10px}.la h2{font-size:20px;margin:0 0 12px}.la h3{font-size:17px;margin:6px 0}.la p{line-height:1.6;margin:7px 0}.la-kicker{font-size:12px;letter-spacing:1.5px;font-weight:800;text-transform:uppercase;color:#657789}.la-muted{font-size:14px;color:var(--la-muted)}
.la-actions{display:flex;flex-wrap:wrap;gap:9px;align-items:center}.la a.la-btn,.la button{display:inline-flex;min-height:44px;padding:10px 15px;align-items:center;justify-content:center;border:1px solid #cbd6e1;border-radius:8px;background:white;color:#18324b;text-decoration:none;font:inherit;font-size:14px;font-weight:700;cursor:pointer}.la a.la-primary,.la button.la-primary{background:#184f84;border-color:#184f84;color:white}.la a:focus-visible,.la button:focus-visible,.la input:focus-visible,.la select:focus-visible,.la textarea:focus-visible{outline:3px solid #368ad9;outline-offset:3px}.la a.la-active{background:#18324b;color:white;border-color:#18324b}
.la-card{background:white;border:1px solid #dce5ed;border-radius:12px;padding:20px;margin:16px 0;min-width:0}.la-warning{background:#fff8e7;border:1px solid #ead195;border-left:4px solid #b5760b;border-radius:8px;padding:16px;margin:16px 0}.la-error{background:#fff1f0;border:1px solid #ddada8;padding:16px;border-radius:8px}.la-error p{color:#842821}.la-state{padding:5px 9px;border-radius:5px;font-weight:700;font-size:12px;display:inline-block;background:#edf2f7}.la-state.blocked{background:#fde6e1;color:#873829}.la-state.draft{background:#fff0c7;color:#765018}.la-state.done{background:#d9f0e8;color:#23644f}
.la-counts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.la-counts>div{background:#fff;border:1px solid #dae3ec;border-radius:10px;padding:14px 16px}.la-counts strong{display:block;font-size:27px;margin-bottom:4px}.la-counts span{font-size:13px;color:#51657b}.la-toolbar{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}.la-toolbar label{display:block;font-size:12px;font-weight:700;margin-bottom:5px}.la input:not([type=checkbox]),.la select,.la textarea{font:inherit;color:#18324b;border:1px solid #b6c6d6;border-radius:7px;background:#fff;min-height:44px;padding:9px 11px;max-width:100%}.la textarea{min-height:105px;resize:vertical}.la-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.la-field{min-width:0}.la-field label{display:block;font-size:14px;font-weight:700;margin-bottom:6px}.la-field input,.la-field select,.la-field textarea{width:100%}.la-wide{grid-column:1/-1}.la form>button{margin-top:15px}.la details{margin:14px 0}.la summary{font-weight:700;cursor:pointer;padding:9px 0}.la-check{display:flex;align-items:flex-start;gap:9px;padding:8px 0;line-height:1.5}.la-check input{width:20px;height:20px;flex-shrink:0}.la-code{font-family:monospace;font-size:12px}.la-error a{font-weight:700}
.la-scroll{max-width:100%;overflow:auto;border:1px solid #d3dee8;border-radius:10px;background:#fff}.la-table{border-collapse:separate;border-spacing:0;table-layout:fixed;min-width:1078px;width:max-content;font-size:13px}.la-table th{padding:9px 7px;background:#18324b;color:white;border-right:1px solid #39546c;text-align:left;vertical-align:middle}.la-table td{padding:12px 9px;vertical-align:top;border-bottom:1px solid #dce5ed;border-right:1px solid #e5ebf0;background:white;overflow-wrap:anywhere}.la-table tr:nth-child(even)>td{background:#f5f8fb}.la-table .la-work{width:250px;min-width:250px;position:sticky;left:0;z-index:2}.la-table th.la-work{z-index:4}.la-table .la-trade{width:140px;min-width:140px}.la-table .la-dates{width:115px;min-width:115px}.la-table .la-progress{width:90px;min-width:90px}.la-table .la-day{width:23px;min-width:23px;text-align:center;padding:8px 0;vertical-align:middle;font-size:11px}.la-table td.la-day{padding:0}.la-table .la-week{text-align:center;font-size:12px;background:#2c4b66}.la-table .la-off{background:#e8edf2!important;color:#6b7887}.la-table .la-plan{background:#dcebf8!important;color:#154c7c;font-weight:800}.la-table .la-actual{background:#d7eee4!important;color:#215f49;font-weight:800}.la-table .la-milestone{background:#fff0c9!important;color:#845c16;font-size:18px}.la-table .la-today{box-shadow:inset 2px 0 #d66432}.la-table .la-row-title{font-weight:750;display:block;color:#173854;font-size:14px;margin-bottom:6px}.la-table .la-edit{display:inline-block;margin-top:8px;color:#155ca0;min-height:32px}.la-table progress{width:65px;height:7px;display:block;margin-top:6px}.la-table caption{text-align:left;padding:14px 18px;font-weight:700;background:#fff}.la-legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;margin:12px 0}.la-dot{width:12px;height:12px;display:inline-block;vertical-align:middle;margin-right:5px;border-radius:2px}.la-dot.blue{background:#dcebf8;border:1px solid #1f66b0}.la-dot.green{background:#d7eee4;border:1px solid #3b8165}.la-dot.grey{background:#e8edf2;border:1px solid #8a9ba9}.la-phone{display:none}.la-phone .la-card{border-left:4px solid #3275ad}.la-phone .la-card.overdue{border-left-color:#be7532}.la-phone .la-mini{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}.la-mini small{display:block;color:#60748a}.la-mini>div{min-width:0}.la-mini strong{display:block;font-size:14px;white-space:normal;overflow-wrap:anywhere}.la-pred-list{max-height:270px;overflow:auto;padding:8px;border:1px solid #dce5ed;border-radius:8px}.la-review-table{width:100%;border-collapse:collapse;font-size:14px}.la-review-table td,.la-review-table th{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #dce4ec;overflow-wrap:anywhere}.la-review-table th{background:#edf3f8}.la-note{white-space:pre-wrap;overflow-wrap:anywhere}.la-footer{font-size:12px;color:#61758a;margin:24px 0 8px}.la-print-brand{display:none}
@media(max-width:850px){.la-head{display:block}.la-head>.la-actions{margin-top:15px}.la-counts{grid-template-columns:repeat(2,1fr)}.la-desktop{display:none}.la-phone{display:block}.la-form-grid{grid-template-columns:1fr}.la-wide{grid-column:auto}.la h1{font-size:26px}.la-card{padding:16px}.la-review-table{font-size:12px}.la-toolbar{gap:10px}.la-toolbar>div{flex:1 1 145px}.la-toolbar input,.la-toolbar select{width:100%}}
@media print{@page{size:landscape;margin:9mm}.bc840-sidebar,.bc8102-sidebar,aside,header,.hub-back,.la-no-print,.la-phone,.la-footer{display:none!important}body,main,.bc840-main,.bc8102-main{margin:0!important;padding:0!important;max-width:none!important;width:100%!important;display:block!important;background:white!important}.la-print-brand{display:block;font-weight:800;font-size:18px;margin-bottom:8px}.la-desktop{display:block}.la-scroll{overflow:visible;border:0}.la-table{width:100%;min-width:0;font-size:8px}.la-table .la-work{position:static;width:22%;min-width:0}.la-table .la-trade{width:12%;min-width:0}.la-table .la-dates{width:12%;min-width:0}.la-table .la-progress{width:8%;min-width:0}.la-table .la-day{width:auto;min-width:0;font-size:7px}.la-table td,.la-table th{font-size:8px!important;line-height:1.4}.la-table td{padding:5px}.la-table td.la-dates{overflow-wrap:normal}.la-table .la-muted,.la-table p{font-size:8px!important;line-height:1.4}.la-table .la-state{font-size:7px;padding:3px}.la-table progress{width:100%;max-width:40px}.la-table .la-row-title{font-size:9px}.la-table th{padding:5px 2px}.la-table tr{break-inside:avoid}.la-table thead{display:table-header-group}.la-counts>div{padding:7px}.la-counts strong{font-size:18px}.la-table .la-edit{display:none}.la *{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
</style>'''

def hidden(name,value):return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
def link(path,label,primary=False,active=False):return '<a class="la-btn'+(' la-primary' if primary else '')+(' la-active' if active else '')+'" href="'+esc(path)+'"'+(' aria-current="page"' if active else '')+'>'+esc(label)+'</a>'
def route(pid,suffix=''):return BASE+'/projects/'+str(pid)+suffix

def field(name,label,value='',typ='text',extra='',wide=False):
    return '<div class="la-field'+(' la-wide' if wide else '')+'"><label for="la-'+name+'">'+esc(label)+'</label><input id="la-'+name+'" name="'+name+'" type="'+typ+'" value="'+esc(value)+'" '+extra+'></div>'
def area(name,label,value='',maximum=4000):return '<div class="la-field la-wide"><label for="la-'+name+'">'+esc(label)+'</label><textarea id="la-'+name+'" name="'+name+'" maxlength="'+str(maximum)+'">'+esc(value)+'</textarea></div>'
def date_label(v):
    try:return day(v,True).strftime('%b %d, %Y')
    except ValueError:return 'Not scheduled'
def short_date(v):
    try:return day(v,True).strftime('%m/%d/%Y')
    except ValueError:return 'Not scheduled'

def label_status(r):return 'Complete' if completed(r) else 'Overdue' if r.get('overdue') else 'Carryover' if r.get('carryover') else 'In progress' if r.get('actual_start') or r['pct']>0 else 'Planned'

def start(title,subtitle=''):return STYLE+'<div class="la"><div class="la-print-brand">BuildCommand AI</div><div class="la-head"><div><span class="la-kicker">BuildCommand AI · Project schedule</span><h1>'+esc(title)+'</h1><p class="la-muted">'+esc(subtitle)+'</p></div>'
def finish():return '<p class="la-footer">Built By Willy LaHood © 2026 · Dates are a plan, not work authorization. Confirm readiness before crews start.</p></div>'

def index(s,m):
    pid=m['p']['id'];cal=WorkCalendar.load(m['payload']['calendar']);draft=m['draft'];manager=m['manager']
    q={k:m[k] for k in ('weeks','week','as_of','mode','view')}
    def here(**kw):return s_url(pid,**dict(q,**kw))
    body=start(str(m['weeks'])+'-week lookahead',m['p']['name']+' · '+date_label(m['week'])+' – '+date_label(m['end']))
    body+='<div class="la-actions la-no-print">'
    if manager:body+=link(route(pid,'/activity'),'Add activity',True)
    body+='</div></div>'
    if not m['settings']['configured']:body+='<div class="la-warning la-no-print"><strong>Confirm this job’s work calendar.</strong> The default is '+', '.join(['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][d] for d in cal.weekdays)+'; timezone '+esc(cal.timezone)+'. No holidays are assumed. '+(link(route(pid,'/calendar'),'Set work calendar') if manager else 'Your project leader can confirm these settings.')+'</div>'
    if draft and manager:
        body+='<div class="la-warning la-no-print"><strong>Draft '+str(draft['version'])+' is saved—not published.</strong><p>'+('The live schedule changed after this draft began. Compare both versions, then discard and reapply the proposed changes. No automatic overwrite is allowed.' if m['stale'] else 'Dates and progress below change only after a reviewed publication. Nothing has been sent to subcontractors.')+'</p><div class="la-actions">'+link(here(mode='current'),'Current schedule',active=m['mode']=='current')+link(here(mode='draft'),'Saved draft',active=m['mode']=='draft')+'</div></div>'
    body+='<div class="la-counts"><div><strong>'+str(m['counts']['lookahead'])+'</strong><span>In this lookahead</span></div><div><strong>'+str(m['counts']['carryover'])+'</strong><span>Unfinished carryover</span></div><div><strong>'+str(m['counts']['unscheduled'])+'</strong><span>Need dates</span></div><div><strong>'+str(m['counts']['all'])+'</strong><span>Total project activities</span></div></div>'
    body+='<section class="la-card la-no-print"><div class="la-actions" aria-label="Lookahead length">'+link(here(weeks=3),'3 weeks',active=m['weeks']==3)+link(here(weeks=6),'6 weeks',active=m['weeks']==6)+'</div><form class="la-toolbar" method="get" action="'+BASE+'">'+hidden('project_id',pid)+hidden('weeks',m['weeks'])+hidden('mode',m['mode'])
    for name,label,value in [('week','Week beginning',m['week']),('as_of','Progress as of',m['as_of'])]:body+='<div><label for="la-'+name+'">'+label+'</label><input type="date" name="'+name+'" id="la-'+name+'" value="'+esc(value)+'" required></div>'
    body+='<div><label for="la-view">Show activities</label><select id="la-view" name="view">'+''.join('<option value="'+key+'"'+(' selected' if m['view']==key else '')+'>'+label+'</option>' for key,label in [('lookahead','Lookahead + unfinished carryover'),('carryover','Unfinished carryover'),('unscheduled','Need dates'),('all','All project activities'),('complete','Completed work')])+'</select></div><button>Update view</button></form><p class="la-muted">Changing this window does not move any activity dates.</p></section>'
    body+='<div class="la-actions la-no-print">'
    if manager:body+=link(route(pid,'/calendar'),'Work calendar')+link(route(pid,'/share')+'?'+urlencode(dict(weeks=m['weeks'],week=m['week'])),'Share with a trade')
    body+=link(route(pid,'/history'),'Revision history')+link(route(pid,'/export.csv')+'?'+urlencode({k:m[k] for k in ['weeks','week','as_of','view']}),'Export current CSV')+'<button type="button" onclick="window.print()">Print this page</button></div>'
    body+='<p class="la-muted">'+('DRAFT PREVIEW — not issued. CSV exports the current saved schedule, not the draft.' if m['mode']=='draft' else 'Current saved schedule · Readiness is never assumed from a date or an empty blocker list.')+'</p>'
    if m['rows']:
        body+='<p class="la-muted la-no-print">On a wide schedule, scroll sideways inside the timeline to see every day.</p><div class="la-legend"><span><i class="la-dot blue"></i>P · planned working day</span><span><i class="la-dot green"></i>A · actual date range</span><span>◆ · milestone</span><span><i class="la-dot grey"></i>Nonworking day</span></div><div class="la-desktop">'+timeline(m,cal)+'</div><div class="la-phone">'
        for r in m['rows']:
            state=m['states'][r['key']];sub=m['subs'].get(r['sub_id'],{}).get('name','')
            body+='<article class="la-card'+(' overdue' if r['overdue'] else '')+'"><span class="la-kicker">'+esc(r['reference'])+'</span><h2>'+esc(r['name'])+'</h2><p>'+esc(' · '.join(v for v in [sub,r['trade'],r['area']] if v) or 'Trade and area not assigned')+'</p><span class="la-state">'+label_status(r)+'</span><div class="la-mini"><div><small>Start</small><strong>'+date_label(r['start'])+'</strong></div><div><small>Finish</small><strong>'+date_label(r['finish'])+'</strong></div><div><small>Workdays</small><strong>'+esc(r['duration'] if r['duration'] is not None else '—')+'</strong></div><div><small>Complete</small><strong>'+str(r['pct'])+'%</strong></div></div>'
            body+='<p><span class="la-state'+(' blocked' if state['reasons'] else '')+'">'+esc(state['label'])+'</span></p>'
            for reason in state['reasons'][:2]:body+='<p class="la-muted">'+esc(reason)+'</p>'
            if manager:body+=link(route(pid,'/activity')+'?'+urlencode({'key':r['key']}),'Update activity')
            body+='</article>'
        body+='</div>'
    else:
        body+='<section class="la-card"><h2>'+('Your schedule starts here' if not m['counts']['all'] else 'No activities in this view')+'</h2><p>'+('Add the first activity for this job. The reusable template supplies the layout—not invented dates or another project’s work.' if not m['counts']['all'] else 'Choose All project activities or Need dates to find work outside these weeks.')+'</p>'
        if manager:body+=link(route(pid,'/activity'),'Add an activity',True)
        body+='</section>'
    body+='<div class="la-actions la-no-print"><span class="la-muted">Page '+str(m['page'])+' of '+str(m['pages'])+' · Up to 50 activities per page. CSV includes all matching activities.</span>'
    if m['page']>1:body+=link(here(page=m['page']-1),'Previous')
    if m['page']<m['pages']:body+=link(here(page=m['page']+1),'Next')
    body+='</div>'
    if draft and manager:
        body+='<section class="la-card la-no-print" id="review-draft"><h2>Review and publish</h2><p>Only a published revision updates the live project schedule. Publishing does not notify trades.</p>'
        for note in m['payload'].get('notes',[]):body+='<p class="la-warning">'+esc(note)+'</p>'
        if not m['stale']:
            body+='<form method="post" action="'+route(pid,'/review')+'">'+hidden('draft_version',draft['version'])+area('reason','Revision description / why are dates changing?','',2000)+'<button class="la-primary">Review exact changes</button></form>'
        body+='<details><summary>Discard this draft</summary><p>Only unpublished changes will be removed. Current activities and issued revisions remain intact.</p><form method="post" action="'+route(pid,'/discard')+'">'+hidden('draft_version',draft['version'])+'<label class="la-check"><input type="checkbox" name="confirmed" value="yes" required> Discard this saved draft and keep the current schedule.</label><button>Discard draft</button></form></details></section>'
    body+='<details class="la-no-print"><summary>Readiness, imports and earlier tools</summary><div class="la-actions">'+link('/workspace/readiness?project_id='+str(pid),'Get trades ready')+'</div><p>Existing import and schedule tools still read and update the same activity records. Finish or discard a draft before editing in those tools.</p>'+s.hub.open_form(pid,'/advanced-schedule-import','Advanced schedule import')+s.hub.open_form(pid,'/schedule','Earlier schedule tools')+s.hub.open_form(pid,'/lookahead-intelligence','Detailed make-ready checks')+'</details>'
    return body+finish()

def s_url(pid,**kw):return BASE+'?'+urlencode(dict(project_id=pid,**kw))

def timeline(m,cal):
    first=day(m['week'],True);dates=[first+timedelta(days=i) for i in range(m['weeks']*7)];pid=m['p']['id']
    body='<div class="la-scroll" tabindex="0" role="region" aria-label="Scrollable schedule timeline"><table class="la-table"><caption>'+('DRAFT — ' if m['mode']=='draft' else '')+esc(m['p']['name'])+' · '+esc(m['week'])+' through '+esc(m['end'])+'</caption><thead><tr><th class="la-work" rowspan="2" scope="col">Activity / area</th><th class="la-trade" rowspan="2" scope="col">Subcontractor / trade</th><th class="la-dates" rowspan="2" scope="col">Planned dates</th><th class="la-progress" rowspan="2" scope="col">Progress</th>'
    for n in range(m['weeks']):body+='<th class="la-week" colspan="7" scope="colgroup">Week '+str(n+1)+' · '+dates[n*7].strftime('%b %d')+'</th>'
    body+='</tr><tr>'
    for d in dates:body+='<th scope="col" class="la-day'+(' la-off' if not cal.working(d) else '')+'" title="'+d.isoformat()+'">'+d.strftime('%d')+'<br>'+d.strftime('%a')[:2]+'</th>'
    body+='</tr></thead><tbody>'
    for r in m['rows']:
        state=m['states'][r['key']];sub=m['subs'].get(r['sub_id'],{}).get('name','Not assigned')
        body+='<tr><td class="la-work"><span class="la-code">'+esc(r['reference'])+'</span><span class="la-row-title">'+esc(r['name'])+'</span><span class="la-muted">'+esc(r['area'] or 'Area not set')+'</span><br><span class="la-state'+(' blocked' if state['reasons'] else '')+'" title="'+esc('\n'.join(state['reasons']))+'">'+esc(state['label'])+'</span>'
        if m['manager']:body+='<br><a class="la-edit" href="'+route(pid,'/activity')+'?'+urlencode({'key':r['key']})+'">Update activity</a>'
        body+='</td><td class="la-trade"><strong>'+esc(sub)+'</strong><br>'+esc(r['trade'])+'</td><td class="la-dates">'+short_date(r['start'])+'<br>to '+short_date(r['finish'])+'<br><span class="la-muted">'+esc(r['duration'] if r['duration'] is not None else '—')+' workdays</span></td><td class="la-progress"><strong>'+esc(r['pct'])+'%</strong><progress max="100" value="'+str(r['pct'])+'" aria-label="Completion"></progress><p>'+label_status(r)+'</p></td>'
        for d in dates:
            text='';classes='la-day';key=d.isoformat();tooltip=key
            if not cal.working(d):classes+=' la-off';text='·'
            if r['start'] and r['finish'] and r['start']<=key<=r['finish']:
                if r['duration']==0:classes+=' la-milestone';text='◆';tooltip+=' · milestone'
                elif cal.working(d):classes+=' la-plan';text='P';tooltip+=' · planned work'
            end=r['actual_finish'] or m['as_of']
            if r['actual_start'] and r['actual_start']<=key<=end:
                classes+=' la-actual';text='A';tooltip+=' · within reported actual date range; not a daily labor record'
            if key==m['as_of']:classes+=' la-today'
            body+='<td class="'+classes+'" title="'+esc(tooltip)+'">'+text+'</td>'
        body+='</tr>'
    return body+'</tbody></table></div>'

def editor(s,m):
    r=m['row'];pid=m['p']['id'];new=not r['id']
    body=start('Add activity' if new else 'Update activity',m['p']['name'])+'</div>'+link(s_url(pid,mode='draft'),'← Back to lookahead')
    if m['error']:body+='<div class="la-error" role="alert"><strong>The activity was not saved.</strong><p>'+esc(m['error'])+'</p><p>Your entries remain in the form. A stale draft requires reopening the latest saved version; copy your notes before reloading.</p></div>'
    if m['stale']:body+='<div class="la-warning">The live schedule changed after this draft began. This draft is preserved for comparison; discard and reapply it before publishing.</div>'
    body+='<section class="la-card"><p class="la-muted">Save to a draft first. Review and publish when ready. Linked, not-started successors may receive proposed dates; actual dates never shift automatically.</p><form method="post" action="'+route(pid,'/activity')+'">'+hidden('key',r['key'])+''.join(hidden(k,v) for k,v in m['tokens'].items())+'<div class="la-form-grid">'
    body+=field('name','Activity description',r['name'],extra='required maxlength="240"',wide=True)
    body+='<div class="la-field"><label for="la-sub_id">Subcontractor on this job</label><select name="sub_id" id="la-sub_id"><option value="0">Not assigned yet</option>'
    for sid,sub in m['subs'].items():body+='<option value="'+str(sid)+'"'+(' selected' if str(sid)==str(r['sub_id']) else '')+'>'+esc(sub['name'])+'</option>'
    body+='</select></div>'+field('trade','Trade',r['trade'],extra='maxlength="100"')+field('area','Area of work',r['area'],extra='maxlength="140"')+field('reference','Reference',r['reference'],extra='maxlength="60"')+field('requested_start','Requested start (leave blank for unscheduled)',r.get('requested_start') or '','date')+field('duration','Duration in workdays (0 = milestone)',r.get('duration') if r.get('duration') is not None else '','number','min="0" max="1000" step="1" required')+field('pct','Percent complete',r.get('pct',0),'number','min="0" max="100" step="0.01" required')+area('blocker','What is blocking this activity? (internal)',r.get('blocker',''),2000)
    body+='</div><details><summary>Actual dates, predecessor links and notes</summary><div class="la-form-grid">'+field('actual_start','Actual start',r.get('actual_start',''),'date')+field('actual_finish','Actual finish',r.get('actual_finish',''),'date')
    predecessors=r.get('predecessors',[])
    body+=field('lag','Wait after predecessors finish (workdays)',r.get('lag',0),'number','min="0" max="365" step="1"')+area('notes','Internal notes / delay reason',r.get('notes',''),4000)+'</div><p class="la-muted">Finish-to-start links only. Zero wait means the next working day after the earlier activity finishes. Loops and missing links are rejected.</p><details><summary>Choose work that must finish first</summary><p class="la-muted">Select the earlier activities. Leave unchecked for independent work.</p><div class="la-pred-list">'+hidden('predecessors','')
    for other in m['rows']:
        if other['key']!=r['key']:
            body+='<label class="la-check"><input type="checkbox" name="predecessors" value="'+esc(other['key'])+'"'+(' checked' if str(other['key']) in [str(x) for x in predecessors] else '')+'><span>'+esc(other['reference']+' · '+other['name'])+'<br><small>Planned finish: '+date_label(other['finish'])+'</small></span></label>'
    body+='</div></details></details><p class="la-muted">An actual finish marks the activity 100% complete. Reporting 100% does not invent an actual finish date. Actual date ranges are observations, not daily crew attendance.</p><button class="la-primary">Save draft</button></form></section>'
    try:
        cal=WorkCalendar.load(m['calendar'])
        if r.get('actual_start'):
            actual=cal.count(r['actual_start'],r['actual_finish']) if r.get('actual_finish') else 'Awaiting actual finish'
            sv=cal.variance(r['start'],r['actual_start']) if r.get('start') else 'No planned start'
            fv=cal.variance(r['finish'],r['actual_finish']) if r.get('finish') and r.get('actual_finish') else 'Awaiting actual finish'
            body+='<section class="la-card"><h2>Actuals and variance</h2><p>Actual workdays: '+esc(actual)+'</p><p>Start variance: '+esc(sv)+' · Finish variance: '+esc(fv)+'</p><p class="la-muted">Signed workdays against the current plan/calendar; positive means later. Historical approved dates and calendars remain in Revision history.</p></section>'
    except (ValueError,TypeError):pass
    return body+finish()

def calendar(s,m):
    pid=m['p']['id'];v=m['values'];body=start('Project work calendar',m['p']['name'])+'</div>'+link(s_url(pid,mode='draft'),'← Back to lookahead')
    if m['error']:body+='<div class="la-error" role="alert"><strong>The calendar was not saved.</strong><p>'+esc(m['error'])+'</p><p>Your entries remain below.</p></div>'
    body+='<section class="la-card"><h2>When does this job work?</h2><p>Calendar changes create proposed dates for unstarted work. Started/completed activities and actual dates are not automatically moved. Review all changes before publishing.</p><form method="post" action="'+route(pid,'/calendar')+'">'+''.join(hidden(k,val) for k,val in m['tokens'].items())+'<fieldset><legend>Normal working days</legend><div class="la-actions">'
    for n,label in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']):body+='<label class="la-check"><input type="checkbox" name="weekdays" value="'+str(n)+'"'+(' checked' if str(n) in [str(x) for x in v['weekdays']] else '')+'> '+label+'</label>'
    body+='</div></fieldset><div class="la-form-grid">'+field('timezone','Project timezone',v['timezone'],extra='required maxlength="80"',wide=True)+area('holidays','Holidays / shutdowns — one YYYY-MM-DD date per line',v['holidays'],5000)+area('extra_workdays','Extra working days — one YYYY-MM-DD date per line',v['extra_workdays'],5000)+'</div><p class="la-muted">Examples of timezones: America/Phoenix, America/Los_Angeles, America/Denver, America/Chicago, America/New_York, UTC. No public holidays are assumed or fetched automatically.</p>'
    if m['admin']:body+='<details><summary>Company default for future job schedules</summary><label class="la-check"><input type="checkbox" name="use_company_default" value="yes"'+(' checked' if v.get('use_company_default')=='yes' else '')+'> Also use this working week and timezone as the company default for future schedules.</label><p class="la-muted">Requires the same review and approval. Existing initialized jobs keep their own calendar. This job’s holidays, shutdowns, activities and dates are never copied into other projects.</p></details>'
    body+='<button class="la-primary">Save calendar draft</button></form></section>'
    return body+finish()

def review(s,p,before,after,reason,changes):
    body=STYLE+'<div class="la"><section class="la-card"><h2>Publish to '+esc(p['name'])+'</h2><p class="la-note">'+esc(reason)+'</p><p><strong>'+str(len(changes))+' activities changed.</strong> This updates the existing live project schedule after approval. No emails, automatic procurement releases or subcontractor instructions are sent.</p>'
    if before['calendar']!=after['calendar'] or not before['configured']:
        def desc(cal):return ', '.join(['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][i] for i in cal['weekdays'])+' | '+cal['timezone']+' | Nonworking: '+(', '.join(cal['holidays']) or 'None')+' | Extra work: '+(', '.join(cal['extra_workdays']) or 'None')
        body+='<h3>Calendar</h3><p class="la-note">Before: '+esc(desc(before['calendar']))+'</p><p class="la-note">After: '+esc(desc(after['calendar']))+'</p>'
    if after.get('use_company_default'):body+='<p class="la-warning">The working week and timezone also become this company’s default for future initialized job schedules. Existing jobs and project exceptions are not changed.</p>'
    body+='</section>'
    fields=[('reference','Reference'),('name','Activity'),('trade','Trade'),('sub_id','Subcontractor ID'),('area','Area'),('requested_start','Requested start'),('duration','Workdays'),('start','Planned start'),('finish','Planned finish'),('pct','Complete %'),('status','Progress status'),('actual_start','Actual start'),('actual_finish','Actual finish'),('blocker','Internal blocker'),('notes','Internal notes'),('predecessors','Predecessor IDs'),('lag','Lag workdays')]
    for change in changes:
        old=change['before'] or {};new=change['after'];body+='<section class="la-card"><h3>'+esc(new['reference']+' · '+new['name'])+'</h3>'+('<p>New activity</p>' if not old else '')+'<div class="la-scroll"><table class="la-review-table"><thead><tr><th>Field</th><th>Current</th><th>Proposed</th></tr></thead><tbody>'
        for key,label in fields:
            if old.get(key)!=new.get(key):body+='<tr><th>'+label+'</th><td class="la-note">'+esc(old.get(key,''))+'</td><td class="la-note">'+esc(new.get(key,''))+'</td></tr>'
        body+='</tbody></table></div></section>'
    for note in after.get('notes',[]):body+='<p class="la-warning">'+esc(note)+'</p>'
    return body+'</div>'

def history(s,p,rows,record):
    pid=p['id'];body=start('Schedule revision history',p['name'])+'</div>'+link(s_url(pid),'← Current schedule')
    if record:
        body+='<section class="la-card"><h2>Revision '+str(record['revision'])+' published</h2><p>Approved by '+esc(record['approved_name'])+' · '+esc(record['created'])+'</p><p class="la-note">'+esc(record['reason'])+'</p><p>This is an immutable internal schedule revision, not evidence of contractual schedule approval. Trades receive nothing until you separately review and share.</p></section>'
        before=json.loads(record['before_json']);after=json.loads(record['after_json'])
        body+=review(s,p,before,after,record['reason'],s.changes(before,after)).replace('Publish to '+esc(p['name']),'Recorded revision').replace('This updates the existing live project schedule after approval.','These are the stored before/after values for this revision.')
    body+='<section class="la-card"><h2>Published revisions</h2>'
    for row in rows:body+='<p>'+link(route(pid,'/revisions/'+str(row['id'])),'Revision '+str(row['revision']))+' '+esc(row['created'])+' · '+esc(row['approved_name'])+'</p><p>'+esc(row['reason'])+'</p>'
    if not rows:body+='<p>No revisions published through this workspace yet. Existing activity records remain intact.</p>'
    body+='<p class="la-muted">The latest 100 revisions are listed. Earlier records remain stored. Changes made in legacy tools are not automatically added to this revision list.</p></section>'
    return body+finish()

def share(s,m):
    pid=m['p']['id'];body=start('Share a trade lookahead',m['p']['name'])+'</div>'+link(s_url(pid),'← Current schedule')
    if m['draft']:return body+'<div class="la-warning"><h2>Finish the pending schedule draft first</h2><p>Publish or discard it before issuing a trade lookahead.</p>'+link(s_url(pid,mode='draft'),'Open draft')+'</div>'+finish()
    body+='<section class="la-card"><h2>Select exactly what this subcontractor should see</h2><p>'+str(m['weeks'])+' weeks · '+esc(m['week'])+' through '+esc(m['end'])+'. Only checked activities and your reviewed message are shared. Internal blockers, notes and the full project schedule are not included.</p>'
    if not m['recipients']:return body+'<p>No subcontractor account is assigned to this project. Add the trade to the job in Team first.</p>'+link('/workspace/team?project_id='+str(pid),'Open Team')+'</section>'+finish()
    body+='<form method="post" action="'+route(pid,'/share')+'">'+hidden('weeks',m['weeks'])+hidden('week',m['week'])+'<div class="la-form-grid"><div class="la-field la-wide"><label for="la-recipient">Recipient on this project</label><select id="la-recipient" name="recipient_id" required><option value="">Choose one recipient</option>'
    for r in m['recipients']:body+='<option value="'+str(r['id'])+'">'+esc((r['display_name'] or r['email'])+' · '+r['email'])+'</option>'
    body+='</select></div>'+field('due_date','Response due','','date')+'</div><fieldset><legend>Activities to include — choose up to 100</legend>'
    for r in m['rows']:body+='<label class="la-check"><input type="checkbox" name="activity_ids" value="'+str(r['id'])+'"><span><strong>'+esc(r['reference']+' · '+r['name'])+'</strong><br>'+esc(r['trade']+' · '+r['area'])+' · '+date_label(r['start'])+' to '+date_label(r['finish'])+'</span></label>'
    if not m['rows']:body+='<p>No activities are in this window. Return to Schedule and choose a different week.</p>'
    body+='</fieldset>'+area('message','Instructions to include for this recipient only','',2000)+'<p class="la-muted">The next screen shows the exact recipient and message. Approval makes an immutable package available in the app for 30 days. No email is queued by this action.</p><button class="la-primary">Review trade lookahead</button></form></section>'
    return body+finish()
