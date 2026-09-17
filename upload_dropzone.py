"""8.26.2: file selection only; existing upload handlers remain authoritative."""
import re
from fastapi.responses import JSONResponse

VERSION = '8.26.2'
RELEASE = 'Drag & Drop Uploads'
HEALTH = '/health/drag-drop-uploads-8-26-2'
MARKER = 'data-bc-file-drop="8.26.2"'

CSS = '''<style data-bc-file-drop="8.26.2">
.bc-file-drop{display:block;box-sizing:border-box;width:100%;min-width:0;margin:10px 0 16px;padding:24px;border:2px dashed #9aafc4;border-radius:12px;background:#f5f8fc;color:#17324a;transition:background .12s,border-color .12s;text-align:left;line-height:1.5;grid-column:1/-1}
.bc-file-drop.is-over,.bc-file-drop:focus-within{border-color:#174875;background:#e8f1fb;outline:2px solid #edb444;outline-offset:2px}
.bc-file-drop .bc-drop-title{display:block;font-size:19px;font-weight:750;margin:0 0 4px;color:#17324a}
.bc-file-drop .bc-drop-help{display:block;font-size:14px;font-weight:400;margin:0 0 12px;color:#435b73}
.bc-file-drop input[type=file]{display:block!important;box-sizing:border-box;width:100%!important;max-width:100%;min-width:0;padding:10px!important;margin:8px 0!important;border:1px solid #bdcddd!important;border-radius:8px;background:white!important;color:#17324a!important;font:inherit!important}
.bc-file-drop input[type=file]::file-selector-button{background:#183e64;color:#fff;border:0;border-radius:6px;padding:12px 16px;margin-right:12px;font:600 15px/1.2 Arial,sans-serif;cursor:pointer}
.bc-file-drop .bc-drop-selected{display:block;overflow-wrap:anywhere;font-size:14px;font-weight:600;white-space:pre-line;color:#27465f}
.bc-file-drop .bc-drop-message{display:block;overflow-wrap:anywhere;font-size:14px;font-weight:600;color:#9a2b1d;margin-top:8px}
.bc-file-drop .bc-drop-message:empty,.bc-file-drop .bc-drop-selected:empty{display:none}
.bc-drop-page-message{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:10000;max-width:calc(100vw - 32px);box-sizing:border-box;padding:14px 20px;border:1px solid #b6c9dd;border-radius:10px;background:#17324a;color:white;font:600 15px/1.5 Arial,sans-serif;box-shadow:0 6px 25px #0003}
@media(max-width:600px){.bc-file-drop{padding:16px}.bc-file-drop .bc-drop-title{font-size:18px}}
</style>'''

SCRIPT = r'''<script data-bc-file-drop="8.26.2">
(()=>{
  'use strict';
  if(window.bcFileDrop8262)return;
  window.bcFileDrop8262=true;
  let serial=0;
  function size(bytes){
    if(bytes<1024)return bytes+' B';
    if(bytes<1024*1024)return (bytes/1024).toFixed(1)+' KB';
    return (bytes/1024/1024).toFixed(1)+' MB';
  }
  function maxBytes(input){
    // Mirror only known limits. All server validation remains in place.
    const path=new URL(input.form.action,location.href).pathname;
    if(/^\/workspace\/documents\/\d+\/files$/.test(path))return 100*1024*1024;
    if(path==='/documents/upload'||/^\/workspace\/(?:drawing-sets|drawings)\/projects\/\d+\/upload$/.test(path))return 500*1024*1024;
    return 0;
  }
  function accepted(file,input){
    const rules=(input.accept||'').toLowerCase().split(',').map(x=>x.trim()).filter(Boolean);
    if(!rules.length)return true;
    const name=file.name.toLowerCase(),type=(file.type||'').toLowerCase();
    // Extension rules also work when the OS supplies no MIME type.
    return rules.some(rule=>rule[0]==='.'?name.endsWith(rule):
      rule.endsWith('/*')?type.startsWith(rule.slice(0,-1)):type===rule);
  }
  function enhance(input){
    if(input.dataset.bcDropReady||!input.form||input.hidden||input.closest('[data-no-file-drop]'))return;
    const zone=document.createElement('span');zone.className='bc-file-drop';
    zone.setAttribute('role','group');zone.setAttribute('aria-label','Select files to upload');
    const title=document.createElement('span');title.className='bc-drop-title';
    title.textContent=input.multiple?'Drop files here':'Drop a file here';
    const help=document.createElement('span');help.className='bc-drop-help';
    help.id='bc-drop-help-'+(++serial);
    const limit=maxBytes(input);
    help.textContent='Or use Choose File below. '+(input.multiple?'':'One file at a time. ')+(limit?'Up to '+Math.round(limit/1024/1024)+' MB. ':'')+'Select a file, then use the form\'s upload or save button.';
    const selected=document.createElement('span');selected.className='bc-drop-selected';
    selected.setAttribute('role','status');selected.setAttribute('aria-live','polite');
    const message=document.createElement('span');message.className='bc-drop-message';
    message.id='bc-drop-message-'+serial;message.setAttribute('role','alert');
    input.parentNode.insertBefore(zone,input);
    zone.append(title,help,input,selected,message);
    input.dataset.bcDropReady='true';
    input.setAttribute('aria-describedby',((input.getAttribute('aria-describedby')||'')+' '+help.id+' '+message.id).trim());
    const labels=Array.from(input.labels||[]).some(label=>label.textContent.trim());
    if(!labels&&!input.getAttribute('aria-label')&&!input.getAttribute('aria-labelledby'))input.setAttribute('aria-label','Choose file to upload');
    let submitted=false;
    input.form.addEventListener('submit',()=>{submitted=true;});
    const busy=()=>input.disabled||input.matches(':disabled')||input.form.getAttribute('aria-busy')==='true'||
      (submitted&&Array.from(input.form.querySelectorAll('button:not([type="button"]):not([type="reset"]),input[type="submit"]')).some(b=>b.disabled));
    function error(files){
      if(!input.multiple&&files.length>1)return 'This form takes one file at a time. Drop one file, upload it, then add the next.';
      for(const file of files){
        if(!file.size)return file.name+' is empty. Choose a file that contains data.';
        if(!accepted(file,input))return 'This file type is not accepted here. Choose '+input.accept+'.';
        if(limit&&file.size>limit)return file.name+' exceeds the '+Math.round(limit/1024/1024)+' MB limit for this form.';
      }
      return '';
    }
    function describe(){
      const files=Array.from(input.files||[]);
      const problem=error(files);
      input.setCustomValidity(problem);message.textContent=problem;
      selected.textContent=files.slice(0,20).map(f=>f.name+' - '+size(f.size)).join('\n')+(files.length>20?'\nAnd '+(files.length-20)+' more files.':'');
    }
    input.addEventListener('change',describe);
    input.addEventListener('click',event=>{
      if(busy()){event.preventDefault();message.textContent='Wait for this upload to finish before choosing another file.';}
    });
    input.form.addEventListener('reset',()=>{submitted=false;setTimeout(describe,0);});
    let depth=0;
    const isFiles=event=>Array.from(event.dataTransfer?.types||[]).includes('Files');
    zone.addEventListener('dragenter',event=>{
      if(!isFiles(event))return;event.preventDefault();depth++;if(!busy())zone.classList.add('is-over');
    });
    zone.addEventListener('dragover',event=>{
      if(!isFiles(event))return;event.preventDefault();event.dataTransfer.dropEffect=busy()?'none':'copy';
    });
    zone.addEventListener('dragleave',()=>{if(--depth<=0){depth=0;zone.classList.remove('is-over');}});
    zone.addEventListener('drop',event=>{
      if(!isFiles(event))return;
      event.preventDefault();event.stopPropagation();depth=0;zone.classList.remove('is-over');
      if(busy()){message.textContent='Wait for this upload to finish before choosing another file.';return;}
      const items=Array.from(event.dataTransfer.items||[]);
      if(items.some(item=>item.webkitGetAsEntry?.()?.isDirectory)){
        message.textContent='Open the folder and drop the file itself. Folders cannot be uploaded here.';return;
      }
      const files=Array.from(event.dataTransfer.files||[]);
      if(!files.length){message.textContent='Drop a file from your computer, or use Choose File.';return;}
      const problem=error(files);
      if(problem){message.textContent=problem;return;} // Keep the previous selection on rejected drops.
      const previous=input.files;
      try{
        const transfer=new DataTransfer();files.forEach(file=>transfer.items.add(file));
        input.files=transfer.files;
        if(input.files.length!==files.length)throw new Error('unsupported');
      }catch(_){
        try{input.files=previous;}catch(_){}
        message.textContent='Your browser needs Choose File for this upload. Click it to select the file.';return;
      }
      // The same native input feeds multipart forms and existing chunk upload scripts.
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
      input.focus({preventScroll:true});
    });
    describe();
  }
  function boot(){
    document.querySelectorAll('input[type="file"]').forEach(enhance);
    if(!document.querySelector('.bc-file-drop'))return;
    // Keep an accidental file drop from navigating away and discarding form edits.
    const isFiles=event=>Array.from(event.dataTransfer?.types||[]).includes('Files');
    document.addEventListener('dragover',event=>{if(isFiles(event))event.preventDefault();});
    let timer;
    document.addEventListener('drop',event=>{
      if(!isFiles(event))return;event.preventDefault();
      let note=document.querySelector('.bc-drop-page-message');
      if(!note){note=document.createElement('div');note.className='bc-drop-page-message';note.setAttribute('role','alert');document.body.append(note);}
      note.textContent='Drop the file inside an upload box, or use Choose File.';
      clearTimeout(timer);timer=setTimeout(()=>note.remove(),7000);
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
</script>'''


def enhance_upload_html(body):
    """Enhance rendered upload forms without buffering responses or changing routes."""
    if not isinstance(body, str) or MARKER in body:
        return body
    if not re.search(r'<input\b[^>]*\btype\s*=\s*(?:"file"|\'file\'|file\b)', body, re.I):
        return body
    return body + CSS + SCRIPT


def install(ns):
    app = ns['app']
    paths = (
        '/documents/upload',
        '/workspace/drawing-sets/projects/{project_id}/upload',
        '/workspace/drawings/projects/{project_id}/upload',
        '/workspace/documents/{record_id}/files',
        '/api/uploads/init',
        '/api/uploads/{upload_token}/chunk',
        '/api/uploads/{upload_token}/complete',
    )
    def handlers():
        return {path: next((r.endpoint for r in app.routes if getattr(r, 'path', '') == path and
                           ('POST' in (getattr(r, 'methods', None) or ()) or 'PUT' in (getattr(r, 'methods', None) or ()))), None)
                for path in paths}
    original = handlers()

    def health():
        active = handlers()
        checks = {
            'upload_ui_installed': ns.get('_bc8262_upload_ui') is enhance_upload_html,
            'private_shell_connected': '_bc8262_upload_ui' in ns['_bc840_shell'].__code__.co_names and ns['_runtime'].shell is ns['_bc840_shell'],
            'drawing_uploads_preserved': all(active[p] is original[p] and active[p] is not None for p in paths[1:3]),
            'document_uploads_preserved': all(active[p] is original[p] and active[p] is not None for p in (paths[0], paths[3])),
            'chunk_upload_handlers_preserved': all(active[p] is original[p] and active[p] is not None for p in paths[4:]),
            'large_plan_analysis_preserved': getattr(app.state, 'blueprint_batches', None) is not None,
            'native_file_input_preserved': 'input.files=transfer.files' in SCRIPT,
            'file_type_and_size_checks_configured': 'function accepted(' in SCRIPT and 'function maxBytes(' in SCRIPT,
            'single_file_forms_respected': '!input.multiple&&files.length>1' in SCRIPT,
            'form_origin_guard_preserved': ns.get('_bc840_same_origin') is ns.get('_bc861_same_origin') and ns.get('_BC862_FORM_REFERRER_POLICY') == 'same-origin',
        }
        ok = all(checks.values())
        return JSONResponse(dict(app='BuildCommand AI', version=VERSION, release=RELEASE,
            status='ok' if ok else 'degraded', checks=checks, passed=sum(checks.values()), total=len(checks),
            data_reset=False, scope='Installation and handler checks only. Test a real file drop, Choose File, upload completion and project access on staging. No file upload or AI request occurs in this check.'), status_code=200 if ok else 503)

    app.add_api_route(HEALTH, health, methods=['GET'])
    ns['_runtime'].PUBLIC_PATHS.add(HEALTH)
    app.state.upload_dropzone = enhance_upload_html
