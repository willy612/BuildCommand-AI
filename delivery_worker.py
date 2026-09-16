"""Send only human-approved queued package notifications. Run explicitly.

No scheduler is started by importing this module or opening the app. A send that
may have reached SMTP is marked uncertain and is never retried automatically.
"""
import argparse
import importlib
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

def configuration(env=os.environ):
    origin=env.get('BUILDCOMMAND_PUBLIC_URL','').rstrip('/');url=urlsplit(origin)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:raise ValueError('Set BUILDCOMMAND_PUBLIC_URL to the HTTPS origin of the construction app.')
    sender=env.get('BC_MAIL_FROM','');host=env.get('BC_SMTP_HOST','')
    if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',sender) or not host:raise ValueError('Set BC_MAIL_FROM and BC_SMTP_HOST.')
    port=int(env.get('BC_SMTP_PORT','465'))
    if port not in {465,587}:raise ValueError('Use TLS SMTP port 465 or STARTTLS port 587.')
    return dict(origin=origin,sender=sender,host=host,port=port,user=env.get('BC_SMTP_USER',''),password=env.get('BC_SMTP_PASSWORD',''))

def smtp_send(config,job):
    message=EmailMessage();message['From']=config['sender'];message['To']=job['email'];message['Subject']=job['subject'];message['Message-ID']='<'+job['message_key']+'@'+config['sender'].split('@',1)[1]+'>'
    message.set_content(job['body']+'\n\n'+config['origin']+'/workspace/transmittals/'+str(job['package_id'])+'\n\nBuildCommand AI\nBuilt By Willy LaHood ©2026')
    if config['port']==465:connection=smtplib.SMTP_SSL(config['host'],config['port'],context=ssl.create_default_context(),timeout=20)
    else:
        connection=smtplib.SMTP(config['host'],config['port'],timeout=20);connection.ehlo();connection.starttls(context=ssl.create_default_context());connection.ehlo()
    with connection:
        if config['user']:connection.login(config['user'],config['password'])
        refused=connection.send_message(message)
        if refused:raise RuntimeError('Mail server refused a recipient.')

def eligible(b,c,job):
    row=c.execute('SELECT * FROM bc824_packages WHERE id=? AND company_id=? AND project_id=?',(job['package_id'],job['company_id'],job['project_id'])).fetchone()
    if not row or row['acknowledged_at'] or row['expires']<=b.now().isoformat():return False
    share=c.execute('SELECT * FROM bc_shared_work WHERE id=? AND company_id=? AND project_id=?',(row['share_id'],job['company_id'],job['project_id'])).fetchone()
    if not share or share['revoked_at'] or share['state']!='OPEN' or share['recipient_user_id']!=job['recipient_id']:return False
    actor=c.execute('SELECT id,company_id,email,role,display_name FROM users WHERE id=? AND company_id=?',(row['created_by'],job['company_id'])).fetchone()
    if not actor:return False
    try:
        b.ns['_bc850_project'](c,dict(actor),job['project_id'])
        recipient=b.ns['_bc850_recipient'](c,dict(actor),job['project_id'],job['recipient_id'])
    except b.ns['_BC850_Problem']:return False
    return recipient['email']==job['email']==json.loads(row['snapshot_json'])['recipient']['email']

def run_once(b,config,send=smtp_send,limit=20):
    result={'accepted_by_smtp':0,'skipped':0,'uncertain':0}
    for _ in range(min(100,max(1,limit))):
        with b.db(True) as c:
            row=c.execute("SELECT * FROM bc824_mail WHERE state='queued' AND send_after<=? ORDER BY id LIMIT 1"+b.field.lock,(b.now().isoformat(),)).fetchone()
            if not row:break
            job=dict(row)
            if not eligible(b,c,job):
                c.execute("UPDATE bc824_mail SET state='skipped',error_code='access_or_receipt_changed' WHERE id=?",(job['id'],));result['skipped']+=1;continue
            c.execute("UPDATE bc824_mail SET state='sending',attempted_at=? WHERE id=?",(b.now().isoformat(),job['id']))
        try:
            # Recheck after committing the claim and immediately before external I/O.
            with b.db() as c:allowed=eligible(b,c,job)
            if not allowed:
                with b.db(True) as c:c.execute("UPDATE bc824_mail SET state='skipped',error_code='access_changed' WHERE id=?",(job['id'],))
                result['skipped']+=1;continue
            send(config,job)
        except Exception:
            with b.db(True) as c:c.execute("UPDATE bc824_mail SET state='uncertain',error_code='delivery_not_confirmed' WHERE id=?",(job['id'],))
            result['uncertain']+=1
        else:
            with b.db(True) as c:c.execute("UPDATE bc824_mail SET state='accepted_by_smtp',sent_at=? WHERE id=?",(b.now().isoformat(),job['id']))
            result['accepted_by_smtp']+=1
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--send-approved',action='store_true');args=parser.parse_args()
    if not args.send_approved:parser.error('Use --send-approved only after configuring and testing your mail service.')
    config=configuration();app=importlib.import_module('full_app');print(json.dumps(run_once(app.app.state.connected_field,config)))
