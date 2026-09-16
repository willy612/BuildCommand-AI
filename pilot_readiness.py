"""8.25: evidence-based setup, access explanations and reviewed mail recovery.

No provider calls, permission changes, sharing or sending from GET/health routes.
"""
import json
import re
import secrets
from datetime import timedelta
from fastapi import Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from blueprint_field import esc
from command_center import command_json
from delivery_worker import VERSION as WORKER_VERSION, configuration_status, eligible

VERSION = '8.25.0'
RELEASE = 'GC Pilot Readiness & Reliable Delivery'
ROOT = '/workspace/delivery'
STATUS = {
    'queued': ('Waiting to send', 'The approved email is queued. The separately configured worker must process it.'),
    'sending': ('Sending / outcome pending', 'Do not resend. If this remains unchanged, ask your administrator to check the worker and mail service.'),
    'accepted_by_smtp': ('Accepted by mail service', 'This does not prove inbox delivery or that the recipient read it. Check the separate package receipt.'),
    'failed': ('Not sent', 'The attempt failed before the mail service accepted it. Fix the cause, then review a retry.'),
    'uncertain': ('Delivery unconfirmed', 'The email may have been accepted. Check the mail service using its message reference before doing anything else. No automatic retry is allowed.'),
    'skipped': ('Not sent — no longer eligible', 'Access, the recipient or the package receipt changed before sending. Review the current package.'),
    'cancelled': ('Cancelled', 'This queued attempt will not be sent. The package and its access are unchanged.'),
    'replaced': ('Retry prepared', 'A separate, reviewed attempt replaced this failed attempt. Its history remains here.'),
}
ERRORS = {
    'connection_failed': 'The mail connection or secure handshake failed. Ask your administrator to check the mail settings.',
    'authentication_failed': 'The mail service rejected sign-in. Ask your administrator to check the sender credentials.',
    'recipient_rejected': 'The mail service rejected the recipient address. Verify it before reviewing a retry.',
    'sender_rejected': 'The mail service rejected the sending address. Ask your administrator to verify that sender.',
    'message_rejected': 'The mail service rejected this message. Ask your administrator to check the provider log.',
    'message_invalid': 'The notification could not be prepared. Ask your administrator to check the sender and recipient.',
}
TASKS = {
    'find_next_step': 'Find the next step on the job home',
    'setup_team': 'Invite or assign a teammate',
    'share_work': 'Review and issue a package',
    'trade_open': 'Have the recipient open the right files',
    'trade_reply': 'Have the recipient acknowledge or reply',
    'briefing': 'Prepare and save a daily briefing',
}
CSS = '''<style>.pilot{max-width:1080px;margin:auto}.pilot h1{font-size:clamp(27px,4vw,40px);line-height:1.2}.pilot .pilot-next{background:#122b40;color:white;padding:24px;border-radius:14px;margin:20px 0}.pilot-next p{color:#e1eaf3}.pilot-next a{background:#f4bd49;color:#122b40}.pilot .pilot-step{padding:20px;border:1px solid #dce5ee;border-radius:12px;margin:14px 0;background:#fff}.pilot .pilot-step h2{font-size:21px;margin:0 0 12px}.pilot .pilot-ready{background:#e3f3e8;color:#21533a}.pilot .pilot-wait{background:#fff0cf;color:#744e0a}.pilot .pilot-status{display:inline-block;padding:5px 10px;border-radius:6px;font-weight:700}.pilot summary{cursor:pointer;font-weight:700;padding:14px 0}.pilot :focus-visible{outline:3px solid #b97d0c;outline-offset:3px}.pilot .field-actions a,.pilot button{min-height:46px}.pilot pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:620px){.pilot .field-actions>*{width:100%;box-sizing:border-box}.pilot .pilot-step{padding:16px}}</style>'''


def install(ns):
    service = PilotReadiness(ns)
    ns['app'].state.pilot_readiness = service
    return service


class PilotReadiness:
    def __init__(self, ns):
        self.ns = ns
        self.b = b = ns['app'].state.connected_field
        self.delivery = ns['app'].state.field_delivery
        self.setup = ns['app'].state.project_setup
        b.schema('bc824_worker_status', 'started TEXT NOT NULL,finished TEXT NOT NULL,worker_version TEXT NOT NULL',
                 ',UNIQUE(company_id,project_id)')
        b.schema('bc824_pilot_observations', '''actor_id BIGINT NOT NULL,request_key TEXT NOT NULL,task TEXT NOT NULL,
            participant_role TEXT NOT NULL,outcome TEXT NOT NULL,seconds INTEGER NOT NULL,notes TEXT NOT NULL,created TEXT NOT NULL''',
                 ',UNIQUE(company_id,actor_id,request_key)')
        b.action('mail_recovery', self.recovery_binding, self.apply_recovery)
        self.routes = []
        for path, method, fn in [
            ('/workspace/setup/projects/{project_id}/ready', 'GET', self.ready),
            ('/workspace/setup/projects/{project_id}/access/{user_id}', 'GET', self.access),
            ('/workspace/setup/projects/{project_id}/walkthrough', 'GET', self.walkthrough),
            ('/workspace/setup/projects/{project_id}/walkthrough', 'POST', self.observe),
            (ROOT, 'GET', self.deliveries),
            (ROOT+'/{job_id}', 'GET', self.job),
            (ROOT+'/{job_id}/review', 'POST', self.review_recovery),
        ]:
            b.route(path, method, fn)
            self.routes.append(b.routes[-1])
        ns['_bc840_replace']('/health/pilot-readiness-8-25-0', 'GET', self.health)
        ns['_runtime'].PUBLIC_PATHS.add('/health/pilot-readiness-8-25-0')

    def page(self, title, body):
        return self.b.page(title, CSS+'<div class="pilot">'+body+'</div>')

    def base(self, pid):
        return '/workspace/setup/projects/'+str(pid)

    def setup_link(self, pid):
        return '<div class="field-actions">'+self.b.link(self.base(pid)+'/ready', 'Check job readiness')+self.b.link(ROOT+'?project_id='+str(pid), 'Delivery & receipts')+'</div>'

    def count(self, c, table, cid, pid, extra=''):
        # Table names are internal constants, never accepted from a request.
        scoped = {'activities', 'subs'}
        sql = 'SELECT count(*) AS n FROM '+table+' WHERE project_id=?'
        args = [pid]
        if table not in scoped:
            sql += ' AND company_id=?'
            args.append(cid)
        return int(c.execute(sql+extra, args).fetchone()['n'])

    def ready(self, project_id: int):
        b = self.b
        with b.db() as c:
            user, p = b.actor(c, project_id)
            data = self.setup.records(c, user, project_id)
            cid, pid = user['company_id'], p['id']
            sheets = self.count(c, 'bc_drawing_heads', cid, pid)
            activities = self.count(c, 'activities', cid, pid)
            directory = self.count(c, 'subs', cid, pid)
            members = [m for m in data['members'] if m['id'] != user['id']]
            shares = self.count(c, 'bc_shared_work', cid, pid, ' AND revoked_at IS NULL')
            packages = self.count(c, 'bc824_packages', cid, pid)
            acks = self.count(c, 'bc824_packages', cid, pid, ' AND acknowledged_at IS NOT NULL')
            plans = len(data['plans'])
        base = self.base(pid)
        steps = [
            ('Plans and drawings', bool(plans or sheets), f'{plans} plan files saved; {sheets} current drawing sheets.', '/workspace/drawings?project_id='+str(pid), 'Open drawings'),
            ('Project schedule', bool(activities), f'{activities} activities saved. Check their dates and trade assignments before issuing a look-ahead.', '/workspace/readiness?project_id='+str(pid) if activities else '/workspace/tools?project_id='+str(pid), 'Check trade readiness' if activities else 'Open tools to import the schedule'),
            ('Subcontractor directory', bool(directory), f'{directory} company profiles on this job. Contact details do not grant account access.', '/workspace/directory?project_id='+str(pid), 'Open directory'),
            ('Team accounts', bool(members), f'{len(members)} other accounts assigned; {len(data["pending"])} invitations still awaiting acceptance.', base+'/team', 'Review the team'),
            ('Work issued to the team', bool(shares), f'{shares} non-revoked shared records; {packages} packages issued. Check each recipient and any package expiry.', '/workspace/sharing?project_id='+str(pid), 'Review shared work'),
        ]
        next_step = next((s for s in steps if not s[1]), None)
        body = '<div class="hero"><p>JOB READINESS · '+esc(p.get('number') or '')+'</p><h1>'+esc(p['name'])+'</h1><p>See what is saved, then take the next useful step. You can run Command while setup continues.</p></div>'
        if next_step:
            body += '<section class="pilot-next"><p>NEXT STEP</p><h2>'+esc(next_step[0])+'</h2><p>'+esc(next_step[2])+'</p>'+b.link(next_step[3], next_step[4])+'</section>'
        else:
            body += '<section class="pilot-next"><p>NEXT STEP</p><h2>Check the job with your team</h2><p>The setup records are present. Confirm a real teammate can open and respond to the correct work.</p>'+b.link(base+'/walkthrough', 'Run the field walkthrough')+'</section>'
        body += '<div class="field-actions">'+b.link('/workspace/command?project_id='+str(pid), 'Open Command')+b.link(ROOT+'?project_id='+str(pid), 'Delivery & receipts')+'</div>'
        for title, done, note, path, label in steps:
            body += '<section class="pilot-step"><span class="pilot-status '+('pilot-ready' if done else 'pilot-wait')+'">'+('Records present' if done else 'Next setup task')+'</span><h2>'+esc(title)+'</h2><p>'+esc(note)+'</p>'+b.link(path, label)+'</section>'
        body += '<section class="card"><h2>Did the trade receive the work?</h2><p>'+str(acks)+' package receipts recorded. An invitation, a shared record, an email attempt and an acknowledgment are different events.</p><p>After sharing, have the recipient use their own account to open My work. Check their files and record a reply.</p>'+b.link(base+'/team', 'Explain a teammate’s access')+'</section>'
        body += '<details class="card"><summary>First-time field walkthrough</summary><p>Record where someone needs help. The 30-second rule is a real-user goal; saved setup records do not prove usability.</p>'+b.link(base+'/walkthrough', 'Record a walkthrough')+'</details>'+b.link(base, 'Back to job setup')
        return self.page('Job readiness', body)

    def access(self, project_id: int, user_id: int):
        b = self.b
        with b.db() as c:
            viewer, p = b.actor(c, project_id)
            row = c.execute('SELECT id,company_id,email,display_name,role FROM users WHERE id=? AND company_id=?', (user_id, viewer['company_id'])).fetchone()
            b.require(row is not None, 'This person is unavailable in your company.', 404)
            person = dict(row)
            table = self.ns['_bc850_member_table']()
            assigned = bool(c.execute(f'SELECT user_id FROM {table} WHERE user_id=? AND project_id=?', (user_id, project_id)).fetchone())
            tier = self.ns['_bc840_tier'](person)
            b.require(self.setup.admin(viewer) or assigned or tier == 'trade', 'Your administrator manages this account.', 403)
            rows = [dict(r) for r in c.execute('SELECT * FROM bc_shared_work WHERE company_id=? AND project_id=? AND recipient_user_id=? ORDER BY id DESC LIMIT 51', (viewer['company_id'], project_id, user_id)).fetchall()]
            more = len(rows) > 50
            records = []
            for share in rows[:50]:
                reason = 'Available under sharing permissions'
                if not assigned:
                    reason = 'Project assignment is missing'
                elif tier != 'trade':
                    reason = 'This recipient no longer has a subcontractor role'
                elif share['revoked_at']:
                    reason = 'Access was revoked'
                else:
                    try:
                        self.ns['_bc850_share'](c, person, share['id'], False)
                    except self.ns['_BC850_Problem']:
                        reason = 'Source or sharing permission needs review'
                    if share['kind'] == 'field_package':
                        q = c.execute('SELECT * FROM bc824_packages WHERE id=? AND company_id=? AND project_id=?', (share['source_id'], viewer['company_id'], project_id)).fetchone()
                        if not q:
                            reason = 'The package source is unavailable'
                        elif q['expires'] <= b.now().isoformat():
                            reason = 'Package access expired'
                        elif json.loads(q['snapshot_json'])['recipient']['email'] != person['email']:
                            reason = 'Email changed since this package was reviewed'
                records.append((share, reason))
        base = self.base(project_id)
        body = '<div class="hero"><p>'+esc(p['name'])+'</p><h1>Check this person’s access</h1><p>'+esc(person['display_name'] or person['email'])+' · '+esc(person['email'])+'</p></div>'
        body += '<section class="card"><h2>Account and project access</h2><p>Role: <strong>'+esc(self.ns['_bc840_role_label'](person['role']))+'</strong></p>'
        if tier in {'owner', 'admin'}:
            body += '<p>This role has company-level project management access. Platform-owner access is separate from team subscription access.</p>'
        elif not assigned:
            body += '<p class="field-warning">This person is not assigned to this project. Review the team assignment first.</p>'
        elif tier == 'trade':
            body += '<p>Assigned to this project. They can open only the work specifically shared with their account.</p>'
        elif self.ns['_bc850_manager'](person):
            body += '<p>Assigned project leader. They can run Command and manage shared work on this job.</p>'
        else:
            body += '<p>Assigned to the project. Their role has limited tools; a company administrator controls role changes.</p>'
        body += '<p>Company activation and sign-in still apply. This is a read-only explanation of assignments and sharing, not a sign-in as this person or a guarantee that every file opens.</p><p>If they see “Waiting for company access,” ask the company administrator to review Company → My access.</p>'+b.link(base+'/team', 'Review project assignments')+'</section>'
        if tier == 'trade' or records:
            body += '<section class="card"><h2>Work shared with this account</h2>'
            if not records:
                body += '<p>No work has been shared with this account on this job. An invitation or directory contact does not share drawings or documents.</p>'+b.link('/workspace/sharing?project_id='+str(project_id), 'Choose work to share')
            for share, reason in records:
                body += '<div class="field-row"><strong>'+esc(share['title'])+'</strong><p>'+esc(reason)+'</p>'+b.link('/workspace/sharing/'+str(share['id']), 'Review this shared record')+'</div>'
            if more:
                body += '<p>Showing the latest 50 records. Use Trade sharing for the full history.</p>'
            body += '</section>'
        body += '<section class="card"><h2>Check together</h2><p>Ask them to sign in with the exact email above, open this project, then open My work. Review one shared file and a reply using their account.</p></section>'+b.link(base+'/team', 'Back to the team')
        return self.page('Explain project access', body)

    def job_context(self, c, job_id, write=False):
        user, p, row = self.b.scope(c, 'bc824_mail', job_id, write)
        if write:
            row = dict(c.execute('SELECT * FROM bc824_mail WHERE id=?'+self.b.field.lock, (job_id,)).fetchone())
        return user, p, row

    def environment_note(self, c, user):
        status = configuration_status()
        heartbeat = c.execute('SELECT started,finished,worker_version FROM bc824_worker_status WHERE company_id=0 AND project_id=0').fetchone()
        body = '<details class="card"><summary>Email service readiness</summary><p>'+('Settings present.' if status['configured'] else 'Email setup needs attention.')+'</p>'
        if self.setup.admin(user):
            body += '<p>'+esc(status['message'])+'</p>'
        else:
            body += '<p>Your company administrator or service operator manages email setup.</p>'
        if heartbeat:
            body += '<p>Worker last reported (UTC): '+esc(heartbeat['finished'] or heartbeat['started'])+'. Worker version: '+esc(heartbeat['worker_version'])+'.</p>'
        else:
            body += '<p>No worker check-in has been recorded. Approved notifications may remain queued until the delivery worker runs.</p>'
        body += '<p>Settings and check-ins do not prove inbox delivery. The app and worker may have different environments. Use an approved package to a consenting test recipient to verify the complete flow.</p></details>'
        return body

    def deliveries(self, project_id: int = 0, before_id: int = 0):
        b = self.b
        b.require(before_id >= 0, 'Invalid page.', 400)
        with b.db() as c:
            user, p, body = b.chooser(c, project_id, 'Delivery & receipts', ROOT)
            if not p:
                return self.page('Delivery & receipts', body+'<p>Choose a project.</p>')
            body += '<p>Check notification attempts and the separate package receipt. Sending email does not grant new access.</p>'+self.environment_note(c, user)
            rows = [dict(r) for r in c.execute('''SELECT m.*,p.title,p.opened_at,p.acknowledged_at FROM bc824_mail m
                JOIN bc824_packages p ON p.id=m.package_id AND p.company_id=m.company_id AND p.project_id=m.project_id
                WHERE m.company_id=? AND m.project_id=? AND (?=0 OR m.id<?) ORDER BY m.id DESC LIMIT 51''', (user['company_id'], p['id'], before_id, before_id)).fetchall()]
        body += '<div class="field-actions">'+b.link('/workspace/transmittals?project_id='+str(p['id']), 'Open packages')+b.link(self.base(p['id'])+'/ready', 'Job readiness')+'</div>'
        if not rows:
            body += '<section class="card"><h2>No email notifications queued</h2><p>Packages can still be shared in the app. To notify a recipient by email, select email notification while preparing the package, then review and approve.</p></section>'
        for job in rows[:50]:
            label, note = STATUS.get(job['state'], ('Needs review', 'Ask your administrator to check this attempt.'))
            body += '<section class="pilot-step"><span class="field-pill">'+esc(label)+'</span><h2>'+esc(job['title'])+'</h2><p>To: '+esc(job['email'])+'</p><p>'+esc(note)+'</p><p>Opened in app: '+esc(job['opened_at'] or 'Not recorded')+'<br>Acknowledged: '+esc(job['acknowledged_at'] or 'Not recorded')+'</p>'+b.link(ROOT+'/'+str(job['id']), 'Review notification')+'</section>'
        if len(rows) > 50:
            body += b.link(ROOT+'?project_id='+str(p['id'])+'&before_id='+str(rows[49]['id']), 'Older attempts')
        return self.page('Delivery & receipts', body)

    def job(self, job_id: int):
        b = self.b
        with b.db() as c:
            user, p, job = self.job_context(c, job_id)
            allowed = eligible(b, c, job)
            events = c.execute("SELECT action,snapshot_json,created FROM bc824_events WHERE company_id=? AND project_id=? AND record_id=? AND action IN ('Notification cancelled','Notification retry approved') ORDER BY id DESC LIMIT 20", (user['company_id'], p['id'], job_id)).fetchall()
        label, note = STATUS.get(job['state'], ('Needs review', 'Ask your administrator to check this attempt.'))
        body = '<div class="hero"><p>'+esc(p['name'])+'</p><h1>'+esc(label)+'</h1><p>'+esc(note)+'</p></div><section class="card"><h2>'+esc(job['subject'])+'</h2><p>To: '+esc(job['email'])+'</p><p class="field-exact">'+esc(job['body'])+'</p><p>Queued for (UTC): '+esc(job['send_after'])+'<br>Attempt started: '+esc(job['attempted_at'] or 'Not started')+'<br>Mail-service acceptance: '+esc(job['sent_at'] or 'Not recorded')+'</p>'
        if job['error_code'] in ERRORS:
            body += '<p class="field-warning">'+esc(ERRORS[job['error_code']])+'</p>'
        body += '<p>Mail reference: <code>'+esc(job['message_key'])+'</code></p>'+b.link('/workspace/transmittals/'+str(job['package_id']), 'Open package & receipt')+'</section>'
        if not allowed:
            body += '<p class="field-warning">This notification is no longer eligible to send. The package was received, access changed, or the reviewed recipient changed. Review the package before preparing any new notification.</p>'
        actions = ['cancel'] if job['state'] == 'queued' else ['cancel', 'retry'] if job['state'] == 'failed' else []
        if actions:
            body += '<form class="card field-form" method="post" action="'+ROOT+'/'+str(job_id)+'/review"><h2>Next action</h2><label for="mail-action">Choose what to do</label><select id="mail-action" name="action">'
            for action in actions:
                if action == 'retry' and not allowed:
                    continue
                body += '<option value="'+action+'">'+('Cancel this unsent attempt' if action == 'cancel' else 'Review one new attempt after fixing the cause')+'</option>'
            body += '</select>'+b.area('reason', 'What did you check or change?', maximum=2000)+'<button>Review this action</button><p>Nothing is sent or changed until you approve the review.</p></form>'
        for event in events:
            detail = json.loads(event['snapshot_json'])
            body += '<section class="card"><h2>'+esc(event['action'])+'</h2><p>'+esc(event['created'])+'</p><p class="field-exact">'+esc(detail.get('reason', ''))+'</p>'
            if detail.get('replacement_id'):
                body += b.link(ROOT+'/'+str(detail['replacement_id']), 'Open the reviewed retry')
            body += '</section>'
        body += b.link(ROOT+'?project_id='+str(p['id']), 'Back to delivery & receipts')
        return self.page('Notification review', body)

    def recovery_binding(self, c, user, p, data):
        _, current_p, job = self.job_context(c, data['job_id'], True)
        b = self.b
        b.require(current_p['id'] == p['id'], 'Notification unavailable.', 404)
        b.require(data['action'] in {'cancel', 'retry'}, 'Choose a supported action.', 400)
        b.require(job['state'] in ({'queued', 'failed'} if data['action'] == 'cancel' else {'failed'}), 'This attempt changed or its delivery is uncertain. Reopen it before proceeding.', 409)
        source = dict(job=job)
        if data['action'] == 'retry':
            b.require(eligible(b, c, job), 'The recipient, access or receipt changed. Review the package instead.', 409)
            pending = c.execute("SELECT id FROM bc824_mail WHERE package_id=? AND company_id=? AND state IN ('queued','sending','uncertain') LIMIT 1", (job['package_id'], user['company_id'])).fetchone()
            b.require(not pending, 'Another attempt is pending or unconfirmed. Check it before preparing a retry.', 409)
            source['package'] = dict(c.execute('SELECT * FROM bc824_packages WHERE id=? AND company_id=?', (job['package_id'], user['company_id'])).fetchone())
            source['share'] = dict(c.execute('SELECT * FROM bc_shared_work WHERE id=? AND company_id=?', (source['package']['share_id'], user['company_id'])).fetchone())
        return source

    def review_recovery(self, job_id: int, request: Request, action: str = Form(...), reason: str = Form(...)):
        b = self.b
        b.origin(request)
        data = dict(job_id=job_id, action=action, reason=b.text(reason, 2000, 'what you checked or changed', True))
        with b.db(True) as c:
            user, p, job = self.job_context(c, job_id, True)
            self.recovery_binding(c, user, p, data)
            body = '<section class="card"><h2>'+('Cancel this unsent attempt' if action == 'cancel' else 'Queue one reviewed retry')+'</h2><p>To: '+esc(job['email'])+'</p><p>Subject: '+esc(job['subject'])+'</p><p class="field-exact">'+esc(job['body'])+'</p><p>Package reference: '+str(job['package_id'])+'</p><p class="field-exact">Reason: '+esc(data['reason'])+'</p><p>'+('Only this unsent attempt is cancelled; package access stays unchanged.' if action == 'cancel' else 'The old failed attempt remains in history. One new attempt will be queued for the configured worker. No automatic further retries.')+'</p></section>'
            return b.prepare(c, user, p, request, 'mail_recovery', data, 'Review notification action', body, ROOT+'/'+str(job_id))

    def apply_recovery(self, c, user, p, data):
        b = self.b
        job = self.recovery_binding(c, user, p, data)['job']
        result_id = job['id']
        if data['action'] == 'retry':
            values = {key: job[key] for key in ('company_id', 'project_id', 'package_id', 'recipient_id', 'email', 'subject', 'body')}
            values.update(send_after=b.now().isoformat(), state='queued', attempted_at=None, sent_at=None, error_code='', message_key=secrets.token_hex(24))
            result_id = b.insert(c, 'bc824_mail', values)
        state = 'replaced' if data['action'] == 'retry' else 'cancelled'
        c.execute('UPDATE bc824_mail SET state=? WHERE id=?', (state, job['id']))
        b.event(c, user, p['id'], 'Notification retry approved' if data['action'] == 'retry' else 'Notification cancelled', job['id'], dict(reason=data['reason'], replacement_id=result_id if data['action'] == 'retry' else None))
        return ROOT+'/'+str(result_id)

    def walkthrough(self, project_id: int):
        b = self.b
        with b.db() as c:
            user, p = b.actor(c, project_id)
            rows = c.execute('SELECT task,participant_role,outcome,seconds,notes,created FROM bc824_pilot_observations WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 30', (user['company_id'], project_id)).fetchall()
        path = self.base(project_id)+'/walkthrough'
        body = '<div class="hero"><p>'+esc(p['name'])+'</p><h1>Can the team use this without help?</h1><p>Ask a first-time user to complete one task. Watch before explaining. For a key screen, aim for them to identify the next action within 30 seconds.</p></div>'
        body += '<form class="card field-form" method="post" action="'+path+'">'+b.hidden('request_key', secrets.token_hex(20))+'<label for="pilot-task">Task observed</label><select id="pilot-task" name="task">'+''.join('<option value="'+k+'">'+esc(v)+'</option>' for k, v in TASKS.items())+'</select><label for="pilot-role">Participant role</label><select id="pilot-role" name="participant_role"><option value="superintendent">Superintendent</option><option value="subcontractor">Subcontractor</option><option value="administrator">Administrator</option></select><label for="pilot-outcome">What happened?</label><select id="pilot-outcome" name="outcome"><option value="without_help">Completed without help</option><option value="needed_help">Needed help</option><option value="blocked">Could not complete</option></select>'
        body += b.input('seconds', 'Observed time in seconds', '', 'number', 'min="1" max="7200" required')+b.area('notes', 'Where did they hesitate or get stuck? Avoid personal or sensitive information.', maximum=2000)+'<button>Save observation</button><p>This records your observation. It does not automatically certify the project or release as ready.</p></form>'
        if not rows:
            body += '<section class="card"><h2>No observations recorded yet</h2><p>Start with finding the next action, then try sharing and opening actual work.</p></section>'
        for row in rows:
            body += '<section class="pilot-step"><h2>'+esc(TASKS.get(row['task'], row['task']))+'</h2><p>'+esc(row['participant_role'].title())+' · '+esc(row['outcome'].replace('_', ' '))+' · '+str(row['seconds'])+' seconds · '+esc(row['created'])+'</p><p class="field-exact">'+esc(row['notes'])+'</p></section>'
        return self.page('Field walkthrough', body+b.link(self.base(project_id)+'/ready', 'Back to job readiness'))

    def observe(self, project_id: int, request: Request, request_key: str = Form(...), task: str = Form(...), participant_role: str = Form(...), outcome: str = Form(...), seconds: int = Form(...), notes: str = Form('')):
        b = self.b
        b.origin(request)
        b.require(bool(re.fullmatch('[a-f0-9]{40}', request_key)), 'Reopen the observation form.', 400)
        b.require(task in TASKS and participant_role in {'superintendent', 'subcontractor', 'administrator'} and outcome in {'without_help', 'needed_help', 'blocked'}, 'Choose an observation from this form.', 400)
        b.require(1 <= seconds <= 7200, 'Enter the observed time from 1 to 7200 seconds.', 400)
        notes = b.text(notes, 2000)
        with b.db(True) as c:
            user, p = b.actor(c, project_id, True)
            existing = c.execute('SELECT * FROM bc824_pilot_observations WHERE company_id=? AND actor_id=? AND request_key=?', (user['company_id'], user['id'], request_key)).fetchone()
            if existing:
                b.require(all(existing[k] == v for k, v in dict(project_id=project_id, task=task, participant_role=participant_role, outcome=outcome, seconds=seconds, notes=notes).items()), 'This form already saved a different observation. Reopen it.', 409)
            else:
                rid = b.insert(c, 'bc824_pilot_observations', dict(company_id=user['company_id'], project_id=project_id, actor_id=user['id'], request_key=request_key, task=task, participant_role=participant_role, outcome=outcome, seconds=seconds, notes=notes, created=b.now().isoformat()))
                b.event(c, user, project_id, 'Field walkthrough observed', rid, dict(task=task, outcome=outcome, seconds=seconds))
        return RedirectResponse(self.base(project_id)+'/walkthrough', 303)

    def health(self):
        b = self.b
        checks = {method+' '+path: sum(getattr(r, 'path', '') == path and method in (getattr(r, 'methods', None) or set()) and r.endpoint is endpoint for r in b.app.routes) == 1 for path, method, endpoint in self.routes}
        checks.update(pilot_schema_initialized=b.schema_ready, pilot_service_installed=getattr(b.app.state, 'pilot_readiness', None) is self,
                      reviewed_mail_recovery=b.actions.get('mail_recovery') == (self.recovery_binding, self.apply_recovery),
                      safe_failure_classification=WORKER_VERSION == VERSION,
                      setup_guidance_connected=getattr(self.setup, 'pilot_guidance_version', '') == VERSION,
                      delivery_panel_connected=getattr(self.delivery, 'pilot_delivery_version', '') == VERSION,
                      existing_connected_field_preserved=getattr(b.app.state, 'connected_field', None) is b,
                      form_origin_guard_preserved=bool(self.ns.get('_bc840_same_origin')),
                      ask_preserved=hasattr(b.app.state.command_center, 'run_answer'))
        try:
            with b.db() as c:
                for table in ('bc824_worker_status', 'bc824_pilot_observations', 'bc824_mail', 'bc824_packages', 'bc824_reviews'):
                    c.execute('SELECT id FROM '+table+' LIMIT 1').fetchone()
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        return JSONResponse(dict(app='BuildCommand AI', version=VERSION, release=RELEASE, status='ok' if all(checks.values()) else 'attention', checks=checks, passed=sum(checks.values()), total=len(checks), data_reset=False,
            scope='Installation and schema checks only. No email is sent. Verify live mail delivery, real user walkthroughs, production PostgreSQL concurrency and database/file restore separately.'), status_code=200 if all(checks.values()) else 503)
