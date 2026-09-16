"""Opt-in device drafts and user-initiated dictation. Never auto-submits a form."""
from fastapi.responses import HTMLResponse
from drawing_register import js

SCRIPT=r'''(()=>{const cfg=window.BC_FIELD_DEVICE;if(!cfg)return;const prefix='bc-field-draft:'+cfg.user+':'+cfg.session+':';const expired=Date.now()-8*3600*1000;try{for(const key of Object.keys(localStorage)){if(!key.startsWith('bc-field-draft:'))continue;try{if(JSON.parse(localStorage.getItem(key)).at<expired)localStorage.removeItem(key);}catch(e){localStorage.removeItem(key);}}}catch(e){}
document.querySelectorAll('form[action="/logout"]').forEach(f=>f.addEventListener('submit',()=>{try{for(const key of Object.keys(localStorage))if(key.startsWith('bc-field-draft:'))localStorage.removeItem(key);}catch(e){}}));
for(const [i,form] of [...document.forms].entries()){if(form.method.toLowerCase()!=='post'||/approve|publish|review|logout|login|invitation|join/.test(form.action))continue;const fields=[...form.querySelectorAll('textarea[name]')];if(!fields.length)continue;const key=prefix+location.pathname+location.search+':'+new URL(form.action).pathname+':'+i;
const tools=document.createElement('details'),title=document.createElement('summary');title.textContent='Keep a draft on this device';tools.append(title);const help=document.createElement('p');help.textContent='Only use on your own device. Draft text expires after 8 hours and is cleared at sign out. Attachments and approvals are not saved. Restoring text does not submit it.';tools.append(help);const status=document.createElement('p');status.setAttribute('role','status');let saving=false;
const button=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;tools.append(b);};function save(){if(!saving)return;try{const values={};for(const f of fields)values[f.name]=f.value;localStorage.setItem(key,JSON.stringify({at:Date.now(),values}));status.textContent='Draft text saved on this device. Review and submit when online.';}catch(e){status.textContent='This browser could not store the draft. Keep this page open and copy your notes.';}}
button('Save draft here',()=>{saving=true;save();});button('Restore saved text',()=>{try{const data=JSON.parse(localStorage.getItem(key)||'null');if(!data||typeof data.at!=='number'||data.at<Date.now()-8*3600*1000)throw Error();for(const f of fields)if(typeof data.values[f.name]==='string')f.value=data.values[f.name];status.textContent='Draft restored. Check current record details before submitting.';}catch(e){status.textContent='No current draft is saved for this form and account.';}});button('Forget draft',()=>{saving=false;try{localStorage.removeItem(key);}catch(e){}status.textContent='Device draft removed.';});tools.append(status);form.append(tools);for(const f of fields)f.addEventListener('input',save);
const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;if(Recognition){for(const f of fields){const b=document.createElement('button');b.type='button';b.textContent='Dictate notes';f.insertAdjacentElement('afterend',b);let active; b.onclick=()=>{if(active){active.stop();return;}const r=new Recognition();active=r;r.lang=document.documentElement.lang||'en-US';r.interimResults=false;r.continuous=false;r.onresult=e=>{const text=e.results[0][0].transcript;f.value=(f.value+' '+text).trim();f.dispatchEvent(new Event('input'));};r.onerror=()=>{status.textContent='Dictation did not finish. You can type your notes.';};r.onend=()=>{active=null;b.textContent='Dictate notes';};b.textContent='Stop dictation';status.textContent='Using your browser voice service. Review the text before saving.';try{r.start();}catch(e){r.onend();}};window.addEventListener('pagehide',()=>active?.stop());}}
}
window.addEventListener('offline',()=>{let p=document.getElementById('bc-offline-status');if(!p){p=document.createElement('p');p.id='bc-offline-status';p.setAttribute('role','status');p.style.cssText='position:fixed;bottom:8px;left:8px;right:8px;padding:14px;background:#fff0c8;color:#162d42;z-index:9999';document.body.append(p);}p.textContent='Connection lost. Keep this page open. Device drafts stay on this device; reconnect before submitting or uploading.';});window.addEventListener('online',()=>{const p=document.getElementById('bc-offline-status');if(p)p.textContent='Connection restored. Review your draft and submit when ready.';});})();'''

def install(b):
    original=b.ns['_bc840_page']
    def page(*args,**kwargs):
        response=original(*args,**kwargs)
        if not isinstance(response,HTMLResponse) or response.status_code!=200:return response
        with b.db() as c:user=b.user(c)
        # Session identifier is deliberately not persisted. User namespace and page
        # identity prevent accidental cross-user restore; sign out removes drafts.
        config=dict(user=str(user['company_id'])+'-'+str(user['id']),session='device-v1')
        html=response.body.decode('utf-8')
        if b.ns['_bc840_tier'](user)=='trade' and args and args[0]=='My shared work':
            html=html.replace('<h1>My shared work</h1>','<h1>My shared work</h1><p><a href="/workspace/inbox">Document packages &amp; trade look-aheads</a></p>',1)
        addition='<script>window.BC_FIELD_DEVICE='+js(config)+';</script><script>'+SCRIPT+'</script>'
        return HTMLResponse(html.replace('</body>',addition+'</body>',1),status_code=response.status_code,headers={k:v for k,v in response.headers.items() if k.lower() not in {'content-length','content-type'}})
    b.ns['_bc840_page']=page
