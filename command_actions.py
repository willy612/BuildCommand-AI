"""8.11.0: reviewed in-app notices and dated internal briefing follow-ups.

Ask remains read-only. Preparing creates a private draft. Session-bound reviews
authorize an exact pair of local writes, committed with their audit and receipt
in one transaction. Replaying an approval returns its receipt. No email, model
tools, background delivery, or baseline schedule changes happen here.
"""
import json
import logging
import re
import secrets
from datetime import date, timedelta
from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from blueprint_field import esc, digest
from command_center import command_json

VERSION = '8.11.0'
RELEASE = 'Reviewed Command Actions'
log = logging.getLogger('buildcommand.command_actions')
SOURCE_TABLES = {'schedule': 'activities', 'rfi': 'project_issues',
                 'submittal': 'submittals', 'punch': 'punch_items'}
LABELS = {'DRAFT': 'Ready for your review', 'FAILED': 'Not completed — review and retry',
          'COMPLETED': 'Actions completed', 'CANCELLED': 'Draft cancelled'}
CSS = '''<style>
.bc-actions{max-width:1050px;margin:auto;overflow-wrap:anywhere}
.bc-actions .action-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.bc-actions .action-grid>section{min-width:0}.bc-actions label{display:block;font-weight:700;margin:16px 0 7px}
.bc-actions textarea,.bc-actions select,.bc-actions input:not([type=checkbox]):not([type=hidden]){width:100%;box-sizing:border-box;padding:12px;font:inherit;border:1px solid #aabace;border-radius:8px}
.bc-actions textarea{min-height:125px}.bc-actions .actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}
.bc-actions .exact{white-space:pre-wrap;line-height:1.65}.bc-actions .warning{padding:18px;border-radius:10px;background:#fff4dc}
.bc-actions .receipt{border-left:4px solid #e4ad3e;padding-left:18px}.bc-actions .small{font-size:14px;color:#53667a}
.bc-actions button,.bc-actions .bc860-button{min-height:46px}.bc-actions h1{font-size:clamp(28px,4vw,40px)}
@media(max-width:720px){.bc-actions .action-grid{grid-template-columns:1fr}.bc-actions .actions>*{width:100%;box-sizing:border-box}}
</style>'''


def install(ns):
    service = CommandActions(ns)
    ns['app'].state.command_actions = service
    ns['_BC850_SOURCES']['command_notice'] = ('Trade notice', 'bc_command_action_plans', 'id', None)
    service.register()
    return service


class CommandActions:
    def __init__(self, ns):
        self.ns = ns
        self.field = ns['app'].state.blueprint_field
        self.center = ns['app'].state.command_center
        self.db, self.require = self.field.db, self.field.require
        self.routes = []
        self.schema_ready = self.initialize()

    def initialize(self):
        try:
            key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            with self.db(True) as c:
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_command_action_plans(
                    id {key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,created_by BIGINT NOT NULL,
                    request_key TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'DRAFT',revision INTEGER NOT NULL DEFAULT 1,
                    question TEXT NOT NULL,answer TEXT NOT NULL,context_hash TEXT NOT NULL,context_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,approved_json TEXT NOT NULL DEFAULT '',share_id BIGINT,followup_id BIGINT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,approved_at TEXT,last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(company_id,created_by,request_key))''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_command_action_reviews(
                    id {key},plan_id BIGINT NOT NULL,revision INTEGER NOT NULL,company_id BIGINT NOT NULL,
                    project_id BIGINT NOT NULL,actor_user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,binding_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,consumed_at TEXT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_command_followups(
                    id {key},plan_id BIGINT NOT NULL UNIQUE,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    followup_date TEXT NOT NULL,title TEXT NOT NULL,note TEXT NOT NULL,created_by BIGINT NOT NULL,
                    created_at TEXT NOT NULL,closed_at TEXT,closed_by BIGINT)''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_action_project ON bc_command_action_plans(company_id,project_id,id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_followup_day ON bc_command_followups(company_id,project_id,closed_at,followup_date,id)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_action_review_expiry ON bc_command_action_reviews(expires_at)')
            return True
        except Exception:
            log.exception('Command action schema setup failed')
            return False

    def insert(self, c, table, columns, values):
        self.require(table in {'bc_command_action_plans', 'bc_command_action_reviews', 'bc_command_followups'}, 'Unsupported action.', 400)
        sql = f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:
            return int(c.execute(sql+' RETURNING id', tuple(values)).fetchone()['id'])
        return int(c.execute(sql, tuple(values)).lastrowid)

    def actor(self, c, pid, lock=False):
        self.require(self.schema_ready, 'Action setup is unavailable. Ask your administrator to check this release.', 503)
        return self.field.actor(c, pid, lock)

    def plan(self, c, plan_id, lock=False, own=False):
        user = self.ns['_bc850_actor'](c)
        row = c.execute('SELECT * FROM bc_command_action_plans WHERE id=? AND company_id=?', (plan_id, user['company_id'])).fetchone()
        self.require(row is not None, 'This action plan is unavailable.', 404)
        user, project = self.actor(c, row['project_id'], lock)
        if lock:
            row = c.execute('SELECT * FROM bc_command_action_plans WHERE id=? AND company_id=?'+self.field.lock, (plan_id, user['company_id'])).fetchone()
            self.require(row is not None, 'This action plan is unavailable.', 404)
        if own:
            self.require(row['created_by'] == user['id'], 'The superintendent who prepared this draft must review it from their account.', 403)
        return user, project, dict(row)

    def page(self, title, body, status=200):
        response = self.center.page(title, CSS+'<div class="bc-actions">'+body+'</div>')
        response.status_code = status
        response.headers['Cache-Control'] = 'no-store'
        return response

    def recipients(self, c, user, pid):
        table = self.ns['_bc850_member_table']()
        rows = c.execute(f'''SELECT DISTINCT u.id,u.display_name,u.email,u.role FROM users u
            JOIN {table} m ON m.user_id=u.id WHERE u.company_id=? AND m.project_id=? ORDER BY u.email''', (user['company_id'], pid)).fetchall()
        return [dict(r) for r in rows if self.ns['_bc840_tier'](dict(r)) == 'trade']

    def choices(self, c, user, project):
        pid = project['id']
        result = [{'value': f'project:{pid}', 'title': 'Project coordination — verify the situation on site', 'recipient': 0}]
        for kind in SOURCE_TABLES:
            for r in self.ns['_bc850_source'](c, user, pid, kind):
                result.append({'value': f'{kind}:{r["id"]}', 'title': self.ns['_BC850_SOURCES'][kind][0]+' · '+str(r['title'] or 'Untitled'), 'recipient': 0})
        for r in c.execute("SELECT id,title,recipient_user_id,kind,source_id FROM bc_shared_work WHERE company_id=? AND project_id=? AND revoked_at IS NULL AND kind<>'command_notice' ORDER BY id DESC LIMIT 100", (user['company_id'], pid)).fetchall():
            result.append({'value': f'shared:{r["id"]}', 'title': 'Shared work · '+r['title'], 'recipient': r['recipient_user_id'], 'origin': f'{r["kind"]}:{r["source_id"]}'})
        return result

    def source(self, c, user, project, value, lock=False):
        parts = value.split(':')
        self.require(len(parts) == 2 and parts[0] in {*SOURCE_TABLES, 'shared', 'project'} and parts[1].isdigit(), 'Choose the work this concerns.', 400)
        kind, sid = parts[0], int(parts[1])
        if kind == 'project':
            self.require(sid == project['id'], 'Choose work in this project.', 404)
            return {'kind': kind, 'id': sid, 'title': 'Project coordination', 'record': project}
        if kind == 'shared':
            share = self.ns['_bc850_share'](c, user, sid, True, lock)
            self.require(share['project_id'] == project['id'] and not share['revoked_at'] and share['kind'] != 'command_notice', 'Choose current shared work in this project.', 409)
            # Include the current source, not only the issued title or share version.
            if share['kind'] in SOURCE_TABLES:
                original = self.source(c,user,project,f'{share["kind"]}:{share["source_id"]}',lock)
            elif share['kind'] == 'scope':
                original = self.field.source(c,user,project['id'],share['source_id'],lock)
            elif share['kind'] == 'rfi_answer':
                original = self.ns['app'].state.rfi_field.source(c,user,project['id'],share['source_id'],lock)
            elif share['kind'] == 'photo_action':
                original = dict(c.execute('SELECT * FROM bc_photo_actions WHERE id=? AND company_id=? AND project_id=?'+(self.field.lock if lock else ''), (share['source_id'],user['company_id'],project['id'])).fetchone())
            else:
                original = self.ns['_bc850_source'](c,user,project['id'],share['kind'],share['source_id'])
            return {'kind': kind, 'id': sid, 'title': share['title'], 'record': share, 'original': original}
        item = self.ns['_bc850_source'](c, user, project['id'], kind, sid)
        row = c.execute(f'SELECT * FROM {SOURCE_TABLES[kind]} WHERE id=? AND project_id=?'+(self.field.lock if lock else ''), (sid, project['id'])).fetchone()
        self.require(row is not None, 'This source was removed. Review current project work.', 409)
        return {'kind': kind, 'id': sid, 'title': item['title'], 'record': dict(row)}

    def binding(self, c, user, project, payload):
        source = self.source(c, user, project, payload['source'], True)
        recipient = self.ns['_bc850_recipient'](c, user, project['id'], payload['recipient_user_id']) if payload['notice'] else None
        return {'source': source, 'recipient': recipient}

    def current(self, c, user, project, expected):
        context = self.center.context(c, user, project)
        self.require(secrets.compare_digest(digest(command_json(context)), expected),
                     'Project records changed. Open this draft and review the updated project records before approving.', 409)
        return context

    def entry_form(self, project_id, question, answer, context, source_hash):
        """No database write: the user must explicitly prepare a draft."""
        body = '<h3>Notice and briefing follow-up</h3><p>Carry this answer into one review. Choose the subcontractor and date there.</p>'
        values = {'request_key': secrets.token_urlsafe(24), 'source_hash': source_hash, 'question': question,
                  'answer': answer['answer'], 'message': answer.get('notice_draft', ''),
                  'briefing_note': answer.get('briefing_note') or answer['answer'],
                  'evidence_ids': ','.join(answer.get('evidence_ids', []))}
        body += f'<form method="post" action="/workspace/command/projects/{project_id}/actions/prepare">'
        body += ''.join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in values.items())
        return body+'<button>Prepare notice &amp; follow-up</button></form>'

    def prepare(self, project_id:int, request:Request, request_key:str=Form(...), source_hash:str=Form(...),
                question:str=Form(...), answer:str=Form(...), message:str=Form(''), briefing_note:str=Form(''), evidence_ids:str=Form('')):
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{24,80}', request_key)) and bool(re.fullmatch(r'[0-9a-f]{64}', source_hash)), 'Ask again to prepare this action.', 400)
        self.require(0 < len(question.strip()) <= 1500 and 0 < len(answer.strip()) <= 12000 and len(message) <= 6000 and len(briefing_note) <= 6000 and len(evidence_ids) <= 200, 'Keep this draft within the displayed text limits.', 400)
        with self.db(True) as c:
            user, project = self.actor(c, project_id, True)
            self.field.session_hash(request)
            previous = c.execute('SELECT id,project_id FROM bc_command_action_plans WHERE company_id=? AND created_by=? AND request_key=?', (user['company_id'], user['id'], request_key)).fetchone()
            if previous:
                self.require(previous['project_id'] == project_id, 'Ask again to prepare this project action.', 409)
                return RedirectResponse(f'/workspace/command/actions/{previous["id"]}', 303)
            context = self.current(c, user, project, source_hash)
            choices = self.choices(c, user, project)
            selected = [e for e in context['evidence'] if e['key'] in evidence_ids.split(',')]
            kind_map = {'Schedule': 'schedule', 'RFI': 'rfi', 'Submittal': 'submittal', 'Shared work': 'shared', 'Issued trade scope': 'shared', 'Issued RFI direction': 'shared'}
            candidates = [f'{kind_map[e["kind"]]}:{e["record_id"]}' for e in selected if e['kind'] in kind_map]
            # A linked source is a visible suggestion. Never guess a person from a trade name.
            chosen = next((r for r in choices if r['value'] in candidates), choices[0])
            assigned = [r for r in choices if r.get('origin') == chosen['value']]
            if len(assigned) == 1: chosen = assigned[0]
            recipients = self.recipients(c, user, project_id)
            uid = chosen['recipient'] if any(r['id'] == chosen['recipient'] for r in recipients) else recipients[0]['id'] if len(recipients) == 1 else 0
            payload = {'notice': bool(message.strip()), 'brief': True, 'source': chosen['value'], 'recipient_user_id': uid,
                       'title': str(chosen['title'])[:240], 'message': message.strip(), 'briefing_note': briefing_note.strip(),
                       'followup_date': (self.field.now().date()+timedelta(days=1)).isoformat(), 'channel': 'in_app'}
            now = self.field.now().isoformat()
            plan_id = self.insert(c, 'bc_command_action_plans', 'company_id,project_id,created_by,request_key,question,answer,context_hash,context_json,payload_json,created_at,updated_at',
                                  (user['company_id'], project_id, user['id'], request_key, question.strip(), answer.strip(), source_hash, command_json(context), command_json(payload), now, now))
            self.ns['_bc850_event'](c, user, project_id, None, 'COMMAND_DRAFT_PREPARED:'+str(plan_id))
        return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)

    def validate(self, payload):
        self.require(payload['notice'] or payload['brief'], 'Choose a trade notice, a briefing follow-up, or both.', 400)
        self.require(payload['channel'] == 'in_app', 'Notices use the subcontractor’s BuildCommand shared-work area.', 400)
        self.require(0 < len(payload['title']) <= 240, 'Enter a title within 240 characters.', 400)
        self.require(len(payload['source']) <= 80, 'Choose the source work.', 400)
        self.require(len(payload['message']) <= 6000 and len(payload['briefing_note']) <= 6000, 'Keep each note within 6,000 characters.', 400)
        if payload['notice']:
            self.require(payload['recipient_user_id'] > 0 and bool(payload['message']), 'Choose a subcontractor and enter their notice.', 400)
        if payload['brief']:
            self.require(bool(payload['briefing_note']), 'Enter the internal briefing follow-up.', 400)
            try:
                when = date.fromisoformat(payload['followup_date'])
                valid = when.isoformat() == payload['followup_date'] and self.field.now().date() <= when <= self.field.now().date()+timedelta(days=365)
            except (ValueError, TypeError):
                valid = False
            self.require(valid, 'Choose a follow-up date from today through the next year (UTC).', 400)

    def detail(self, plan_id:int):
        with self.db() as c:
            user, project, plan = self.plan(c, plan_id)
            if plan['state'] == 'COMPLETED':
                return self.receipt(c, user, project, plan)
            choices = self.choices(c, user, project)
            recipients = self.recipients(c, user, project['id'])
            current_context = self.center.context(c, user, project)
        payload = json.loads(plan['payload_json'])
        body = '<h1>Review your next steps</h1><p>'+esc(project['name'])+' · '+LABELS.get(plan['state'], 'Draft')+'</p>'
        body += '<p>Choose what the subcontractor receives and what your team needs to follow up.</p>'
        editable = plan['created_by'] == user['id'] and plan['state'] in {'DRAFT', 'FAILED'}
        stale = digest(command_json(current_context)) != plan['context_hash']
        if plan['state'] == 'FAILED':
            body += '<p role="alert" class="warning">The actions were not completed. Your draft is kept. Review it and try again. Reference '+esc(plan['last_error'])+'</p>'
        if stale:
            body += '<section class="warning"><h2>Project records have changed</h2><p>Check the current records below. Your prepared text will stay here for you to update.</p>'
            if editable:
                body += f'<form method="post" action="/workspace/command/actions/{plan_id}/refresh"><input type="hidden" name="revision" value="{plan["revision"]}"><button>Use these current records for review</button></form>'
            body += '</section>'
        body += '<details class="bc860-panel"'+(' open' if stale else '')+'><summary>Question, answer and current project records</summary><p>'+esc(plan['question'])+'</p><p class="exact">'+esc(plan['answer'])+'</p>'
        if stale or digest(plan['context_json']) != plan['context_hash']:
            body += '<p class="warning">This is the original Ask answer. Records have changed since it was prepared. Review its recommendations and draft text again.</p>'
        body += ''.join('<p><a href="'+esc(e['path'])+'">'+esc(e['kind']+' · '+e['title'])+'</a><br><span class="small">'+esc(e['detail'])+'</span></p>' for e in current_context['evidence'])+'</details>'
        if not editable:
            body += '<p>This draft can be changed only by the person who prepared it.</p>' if plan['state'] != 'CANCELLED' else '<p>No actions were published from this cancelled draft.</p>'
            return self.page('Command action', body+self.back(project['id']))
        body += f'<form method="post" action="/workspace/command/actions/{plan_id}/review"><input type="hidden" name="revision" value="{plan["revision"]}"><input type="hidden" name="channel" value="in_app">'
        body += '<section class="bc860-panel"><label for="action-source">Which work is this about?</label><select id="action-source" name="source" required>'
        body += ''.join('<option value="'+esc(r['value'])+'"'+(' selected' if r['value'] == payload['source'] else '')+'>'+esc(r['title'])+'</option>' for r in choices)
        body += '</select><label for="action-title">Title</label><input id="action-title" name="title" maxlength="240" required value="'+esc(payload['title'])+'"></section><div class="action-grid">'
        body += '<section class="bc860-panel"><h2>1. Trade notice</h2><label><input type="checkbox" name="notice" value="yes"'+(' checked' if payload['notice'] else '')+'> Publish a notice in BuildCommand</label><p class="small">The selected subcontractor will see it in My shared work and can reply.</p><label for="action-recipient">Subcontractor</label><select id="action-recipient" name="recipient_user_id"><option value="0">Choose a subcontractor</option>'
        body += ''.join(f'<option value="{r["id"]}"'+(' selected' if r['id'] == payload['recipient_user_id'] else '')+'>'+esc((r['display_name'] or r['email'])+' · '+r['email'])+'</option>' for r in recipients)+'</select>'
        if not recipients: body += f'<p class="warning">Assign a subcontractor before publishing a notice. <a href="/workspace/sharing/projects/{project["id"]}/team">Open project team</a>. You can still queue an internal follow-up.</p>'
        body += '<label for="action-message">Message they will receive</label><textarea id="action-message" name="message" maxlength="6000">'+esc(payload['message'])+'</textarea></section>'
        body += '<section class="bc860-panel"><h2>2. Briefing follow-up</h2><label><input type="checkbox" name="brief" value="yes"'+(' checked' if payload['brief'] else '')+'> Add an internal follow-up</label><label for="action-date">Bring this into the briefing on</label><input type="date" id="action-date" name="followup_date" value="'+esc(payload['followup_date'])+'"><p class="small">UTC calendar. It appears on this date and stays in later briefings until marked handled.</p><label for="action-note">Note for the superintendent</label><textarea id="action-note" name="briefing_note" maxlength="6000">'+esc(payload['briefing_note'])+'</textarea></section></div>'
        body += '<p class="small">Your next screen shows the exact notice and follow-up for approval. The project schedule stays as recorded.</p><button'+(' disabled' if stale else '')+'>Preview these actions</button></form>'
        body += f'<details><summary>Discard this draft</summary><form method="post" action="/workspace/command/actions/{plan_id}/cancel"><input type="hidden" name="revision" value="{plan["revision"]}"><button class="secondary">Cancel draft</button></form></details>'
        return self.page('Review next steps', body+self.back(project['id']))

    def review(self, plan_id:int, request:Request, revision:int=Form(...), source:str=Form(...), title:str=Form(...),
               notice:str=Form(''), brief:str=Form(''), recipient_user_id:int=Form(0), message:str=Form(''),
               briefing_note:str=Form(''), followup_date:str=Form(''), channel:str=Form('in_app')):
        self.require(notice in {'', 'yes'} and brief in {'', 'yes'}, 'Choose the actions to prepare.', 400)
        payload = {'notice': notice == 'yes', 'brief': brief == 'yes', 'source': source.strip(), 'title': title.strip(),
                   'recipient_user_id': recipient_user_id, 'message': message.strip(), 'briefing_note': briefing_note.strip(),
                   'followup_date': followup_date.strip(), 'channel': channel}
        self.validate(payload)
        with self.db(True) as c:
            user, project, plan = self.plan(c, plan_id, True, True)
            self.require(plan['state'] in {'DRAFT', 'FAILED'} and plan['revision'] == revision, 'This draft changed or was completed. Open it again.', 409)
            self.current(c, user, project, plan['context_hash'])
            binding = self.binding(c, user, project, payload)
            now = self.field.now()
            token = secrets.token_urlsafe(32)
            c.execute("UPDATE bc_command_action_plans SET payload_json=?,state='DRAFT',revision=revision+1,updated_at=?,last_error='' WHERE id=?", (command_json(payload), now.isoformat(), plan_id))
            self.insert(c, 'bc_command_action_reviews', 'plan_id,revision,company_id,project_id,actor_user_id,session_hash,token_hash,payload_json,binding_hash,expires_at',
                        (plan_id, revision+1, user['company_id'], project['id'], user['id'], self.field.session_hash(request), digest(token), command_json(payload), digest(command_json(binding)), (now+timedelta(minutes=15)).isoformat()))
        return self.preview_page(project, plan_id, payload, binding, token)

    def preview_page(self, project, plan_id, payload, binding, token, failure=''):
        body = '<h1>Approve these actions</h1><p>'+esc(project['name'])+'</p>'
        if failure: body += '<p role="alert" class="warning">The actions could not be saved. Neither action was completed. Try again while this review is current. Reference '+esc(failure)+'</p>'
        body += '<p>Source work: <strong>'+esc(binding['source']['title'])+'</strong></p><div class="action-grid">'
        if payload['notice']:
            r = binding['recipient']
            body += '<section class="bc860-panel"><h2>Publish this trade notice</h2><p><strong>'+esc(r['display_name'] or r['email'])+'</strong><br>'+esc(r['email'])+'</p><p class="small">Destination: their BuildCommand My shared work. Replies enabled.</p><h3>'+esc(payload['title'])+'</h3><p class="exact">'+esc(payload['message'])+'</p></section>'
        if payload['brief']:
            body += '<section class="bc860-panel"><h2>Queue this briefing follow-up</h2><p><strong>'+esc(payload['followup_date'])+' UTC</strong> · Internal project briefing</p><h3>'+esc(payload['title'])+'</h3><p class="exact">'+esc(payload['briefing_note'])+'</p><p class="small">Available in the briefing on this date. You will still review and save the day’s complete briefing.</p></section>'
        label = 'Approve & publish notice + queue follow-up' if payload['notice'] and payload['brief'] else 'Approve & publish notice' if payload['notice'] else 'Approve & queue follow-up'
        body += f'</div><form method="post" action="/workspace/command/actions/{plan_id}/approve"><input type="hidden" name="review_token" value="{token}"><input type="hidden" name="confirmed" value="yes"><p>Approve the exact content, person and date shown above.</p><button>{esc(label)}</button></form><p><a href="/workspace/command/actions/{plan_id}">Edit before approving</a></p>'
        return self.page('Approve command actions', body, 503 if failure else 200)

    def approve(self, plan_id:int, request:Request, review_token:str=Form(...), confirmed:str=Form('')):
        self.require(confirmed == 'yes' and bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}', review_token)), 'Open and approve the action preview.', 400)
        ready = False
        try:
            with self.db(True) as c:
                user, project, plan = self.plan(c, plan_id, True, True)
                row = c.execute('SELECT * FROM bc_command_action_reviews WHERE token_hash=? AND plan_id=?'+self.field.lock, (digest(review_token), plan_id)).fetchone()
                self.require(row is not None, 'This review is unavailable. Preview the draft again.', 409)
                preview = dict(row)
                self.require(preview['actor_user_id'] == user['id'] and preview['company_id'] == user['company_id'] and preview['project_id'] == project['id'] and secrets.compare_digest(preview['session_hash'], self.field.session_hash(request)), 'Approve from the same signed-in session that reviewed this draft.', 403)
                if plan['state'] == 'COMPLETED' and preview['consumed_at']:
                    return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)
                self.require(plan['state'] in {'DRAFT', 'FAILED'} and not preview['consumed_at'] and preview['revision'] == plan['revision'] and preview['expires_at'] > self.field.now().isoformat(), 'This review changed, expired or was cancelled. Preview the draft again.', 409)
                payload = json.loads(preview['payload_json'])
                self.validate(payload)
                self.current(c, user, project, plan['context_hash'])
                binding = self.binding(c, user, project, payload)
                self.require(secrets.compare_digest(digest(command_json(binding)), preview['binding_hash']), 'The source work or recipient changed. Review this draft again.', 409)
                ready = True
                now = self.field.now().isoformat()
                sid = fid = None
                if payload['notice']:
                    sid = self.ns['_bc850_insert'](c, 'bc_shared_work', 'company_id,project_id,kind,source_id,recipient_user_id,title,message,due_date,allow_response,created_by,created_at,updated_at',
                        (user['company_id'], project['id'], 'command_notice', plan_id, payload['recipient_user_id'], payload['title'], payload['message'], '', 1, user['id'], now, now))
                    self.ns['_bc850_event'](c, user, project['id'], sid, 'COMMAND_NOTICE_PUBLISHED:'+str(plan_id))
                if payload['brief']:
                    fid = self.insert(c, 'bc_command_followups', 'plan_id,company_id,project_id,followup_date,title,note,created_by,created_at',
                        (plan_id, user['company_id'], project['id'], payload['followup_date'], payload['title'], payload['briefing_note'], user['id'], now))
                    self.ns['_bc850_event'](c, user, project['id'], sid, 'COMMAND_FOLLOWUP_QUEUED:'+str(fid))
                receipt = {'source': {'kind': binding['source']['kind'], 'id': binding['source']['id'], 'title': binding['source']['title']},
                           'recipient': binding['recipient'], 'binding_hash': preview['binding_hash']}
                c.execute("UPDATE bc_command_action_plans SET state='COMPLETED',share_id=?,followup_id=?,approved_json=?,approved_at=?,updated_at=?,last_error='' WHERE id=?", (sid, fid, command_json(receipt), now, now, plan_id))
                c.execute('UPDATE bc_command_action_reviews SET consumed_at=? WHERE id=?', (now, preview['id']))
                self.ns['_bc850_event'](c, user, project['id'], sid, 'COMMAND_ACTIONS_COMPLETED:'+str(plan_id))
            return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)
        except self.ns['_BC850_Problem']:
            raise
        except Exception:
            # The local database transaction rolled back. Keep a safe failure
            # reference; never store/log draft contents or database exceptions.
            reference = 'ACTION-'+secrets.token_hex(4).upper()
            log.error('COMMAND_ACTION_FAILURE reference=%s plan_id=%s', reference, plan_id)
            self.require(ready, 'The action review is temporarily unavailable. Open your draft and try again. Reference '+reference, 503)
            with self.db(True) as c:
                fresh, project, current = self.plan(c, plan_id, True, True)
                if current['state'] == 'COMPLETED':
                    return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)
                self.require(current['state'] in {'DRAFT', 'FAILED'} and current['revision'] == preview['revision'], 'This draft changed. Open it again.', 409)
                c.execute("UPDATE bc_command_action_plans SET state='FAILED',last_error=?,updated_at=? WHERE id=?", (reference, self.field.now().isoformat(), plan_id))
            return self.preview_page(project, plan_id, payload, binding, review_token, reference)

    def refresh(self, plan_id:int, revision:int=Form(...)):
        with self.db(True) as c:
            user, project, plan = self.plan(c, plan_id, True, True)
            self.require(plan['state'] in {'DRAFT', 'FAILED'} and plan['revision'] == revision, 'Open the current draft before refreshing it.', 409)
            context = self.center.context(c, user, project)
            raw = command_json(context)
            # Preserve the original Ask evidence snapshot for the action history.
            # Only the current, explicitly reviewed fingerprint is refreshed.
            c.execute("UPDATE bc_command_action_plans SET context_hash=?,revision=revision+1,state='DRAFT',last_error='',updated_at=? WHERE id=?", (digest(raw), self.field.now().isoformat(), plan_id))
            self.ns['_bc850_event'](c, user, project['id'], None, 'COMMAND_DRAFT_REFRESHED:'+str(plan_id))
        return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)

    def cancel(self, plan_id:int, revision:int=Form(...)):
        with self.db(True) as c:
            user, project, plan = self.plan(c, plan_id, True, True)
            if plan['state'] != 'CANCELLED':
                self.require(plan['state'] in {'DRAFT', 'FAILED'} and plan['revision'] == revision, 'This draft changed or was already completed.', 409)
                c.execute("UPDATE bc_command_action_plans SET state='CANCELLED',revision=revision+1,updated_at=? WHERE id=?", (self.field.now().isoformat(), plan_id))
                self.ns['_bc850_event'](c, user, project['id'], None, 'COMMAND_DRAFT_CANCELLED:'+str(plan_id))
        return RedirectResponse(f'/workspace/command/actions/{plan_id}', 303)

    def receipt(self, c, user, project, plan):
        payload = json.loads(plan['payload_json'])
        approved = json.loads(plan['approved_json'])
        body = '<h1>Actions completed</h1><p>'+esc(project['name'])+' · '+esc(plan['approved_at'])+' UTC</p><section class="bc860-panel receipt"><h2>'+esc(payload['title'])+'</h2>'
        body += '<p>Source work: '+esc(approved['source']['title'])+'</p>'
        if plan['share_id']:
            row = c.execute('SELECT revoked_at,state FROM bc_shared_work WHERE id=? AND company_id=?', (plan['share_id'], user['company_id'])).fetchone()
            state = 'Access later revoked' if row and row['revoked_at'] else 'Responses closed' if row and row['state'] == 'CLOSED' else 'Published in My shared work' if row else 'Published notice is no longer available'
            body += '<h3>Trade notice · '+state+'</h3><p class="exact">'+esc(payload['message'])+'</p>'
            r = approved['recipient']
            body += '<p>Approved recipient: '+esc((r['display_name'] or r['email'])+' · '+r['email'])+'</p>'
            if row: body += f'<p><a href="/workspace/sharing/{plan["share_id"]}">Open notice and trade replies</a></p>'
        if plan['followup_id']:
            row = c.execute('SELECT closed_at FROM bc_command_followups WHERE id=? AND company_id=?', (plan['followup_id'], user['company_id'])).fetchone()
            body += '<h3>Briefing follow-up · '+('Handled' if row and row['closed_at'] else 'Queued')+'</h3><p>'+esc(payload['followup_date'])+' UTC</p><p class="exact">'+esc(payload['briefing_note'])+'</p>'
            if row and not row['closed_at']: body += self.close_form(plan['followup_id'])
        body += f'</section><div class="actions"><a class="bc860-button" href="/workspace/command?project_id={project["id"]}">Back to Command</a><a class="bc860-button secondary" href="/workspace/command/projects/{project["id"]}/actions">View action history</a></div>'
        return self.page('Actions completed', body)

    def close_form(self, fid):
        return f'<form method="post" action="/workspace/command/followups/{fid}/close"><button class="secondary">Mark follow-up handled</button></form>'

    def close(self, followup_id:int):
        with self.db(True) as c:
            user = self.ns['_bc850_actor'](c)
            row = c.execute('SELECT * FROM bc_command_followups WHERE id=? AND company_id=?', (followup_id, user['company_id'])).fetchone()
            self.require(row is not None, 'This follow-up is unavailable.', 404)
            user, project = self.actor(c, row['project_id'], True)
            row = c.execute('SELECT * FROM bc_command_followups WHERE id=?'+self.field.lock, (followup_id,)).fetchone()
            if not row['closed_at']:
                c.execute('UPDATE bc_command_followups SET closed_at=?,closed_by=? WHERE id=?', (self.field.now().isoformat(), user['id'], followup_id))
                self.ns['_bc850_event'](c, user, project['id'], None, 'COMMAND_FOLLOWUP_HANDLED:'+str(followup_id))
        return RedirectResponse(f'/workspace/command/actions/{row["plan_id"]}', 303)

    def brief_data(self, c, user, project):
        self.require(self.schema_ready, 'Command follow-up setup is unavailable.', 503)
        args = (user['company_id'], project['id'], self.field.now().date().isoformat())
        where = 'company_id=? AND project_id=? AND closed_at IS NULL AND followup_date<=?'
        total = int(c.execute('SELECT COUNT(*) AS n FROM bc_command_followups WHERE '+where, args).fetchone()['n'])
        rows = c.execute('SELECT id,plan_id,followup_date,title,note FROM bc_command_followups WHERE '+where+' ORDER BY followup_date,id LIMIT 100', args).fetchall()
        return {'total': total, 'items': [dict(r) for r in rows]}

    @staticmethod
    def brief_html(data, links=False):
        body = '<section class="card"><h2>Planned follow-ups</h2><p>'+str(data['total'])+' due today or carried forward.</p>'
        for r in data['items']:
            title = esc(r['title'])
            if links: title = f'<a href="/workspace/command/actions/{r["plan_id"]}">'+title+'</a>'
            body += '<article><h3>'+title+'</h3><p>'+esc(r['followup_date'])+' UTC</p><blockquote>'+esc(r['note'])+'</blockquote></article>'
        if not data['items']: body += '<p>No planned follow-ups are due.</p>'
        if data['total'] > len(data['items']): body += '<p>Showing the first 100. Open Command action history for the remaining follow-ups.</p>'
        return body+'</section>'

    def panel(self, c, user, project):
        data = self.brief_data(c, user, project)
        pid = project['id']
        drafts = int(c.execute("SELECT COUNT(*) AS n FROM bc_command_action_plans WHERE company_id=? AND project_id=? AND created_by=? AND state IN ('DRAFT','FAILED')", (user['company_id'], pid, user['id'])).fetchone()['n'])
        body = '<div class="planned-followups"><h3>Planned follow-ups</h3>'
        for r in data['items'][:3]:
            body += f'<p><a href="/workspace/command/actions/{r["plan_id"]}">'+esc(r['title'])+'</a> · '+esc(r['followup_date'])+'</p>'
        body += f'<p class="muted">{data["total"]} due · {drafts} of your drafts awaiting review</p><a href="/workspace/command/projects/{pid}/actions">Open follow-ups &amp; action history</a></div>'
        return body

    def history(self, project_id:int, before_id:int=0):
        self.require(before_id >= 0, 'Choose a valid page.', 400)
        with self.db() as c:
            user, project = self.actor(c, project_id)
            plans = [dict(r) for r in c.execute('SELECT id,state,payload_json,created_at FROM bc_command_action_plans WHERE company_id=? AND project_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 26', (user['company_id'], project_id, before_id, before_id)).fetchall()]
            followups = [dict(r) for r in c.execute('SELECT id,plan_id,followup_date,title FROM bc_command_followups WHERE company_id=? AND project_id=? AND closed_at IS NULL ORDER BY followup_date,id LIMIT 100', (user['company_id'], project_id)).fetchall()]
        body = '<h1>Follow-ups &amp; action history</h1><p>'+esc(project['name'])+'</p><section class="bc860-panel"><h2>Upcoming and carried forward</h2>'
        for r in followups:
            body += f'<article><h3><a href="/workspace/command/actions/{r["plan_id"]}">'+esc(r['title'])+'</a></h3><p>'+esc(r['followup_date'])+' UTC</p>'+self.close_form(r['id'])+'</article>'
        if not followups: body += '<p>No open follow-ups. Ask BuildCommand to prepare the next notice or briefing item.</p>'
        if len(followups) == 100: body += '<p>Showing the first 100 open follow-ups. Every action remains available in the history below.</p>'
        body += '</section><h2>Prepared and completed actions</h2>'
        for row in plans[:25]:
            payload = json.loads(row['payload_json'])
            body += f'<article class="bc860-panel"><h3><a href="/workspace/command/actions/{row["id"]}">'+esc(payload['title'])+'</a></h3><p>'+LABELS.get(row['state'], 'Draft')+' · '+esc(row['created_at'])+' UTC</p></article>'
        if not plans: body += '<p>No action plans yet.</p>'
        if len(plans) > 25: body += f'<p><a href="/workspace/command/projects/{project_id}/actions?before_id={plans[24]["id"]}">Older actions</a></p>'
        return self.page('Command action history', body+self.back(project_id))

    def list_sources(self, c, user, pid, source_id=None, query=''):
        # The generic sharing form must never republish or edit approved notices.
        if source_id is None: return []
        row = c.execute("SELECT id,payload_json FROM bc_command_action_plans WHERE id=? AND company_id=? AND project_id=? AND state='COMPLETED'", (source_id, user['company_id'], pid)).fetchone()
        self.require(row is not None, 'This published notice is unavailable.', 404)
        return {'id': row['id'], 'title': json.loads(row['payload_json'])['title'], 'due_date': ''}

    def notice(self, c, share):
        row = c.execute("SELECT share_id,payload_json FROM bc_command_action_plans WHERE id=? AND company_id=? AND project_id=? AND state='COMPLETED'", (share['source_id'], share['company_id'], share['project_id'])).fetchone()
        self.require(row is not None and row['share_id'] == share['id'], 'This published notice is unavailable.', 404)
        payload = json.loads(row['payload_json'])
        self.require(payload['notice'] and payload['recipient_user_id'] == share['recipient_user_id'] and payload['title'] == share['title'] and payload['message'] == share['message'], 'This notice needs a project leader’s review.', 409)

    def back(self, pid):
        return f'<p><a href="/workspace/command?project_id={pid}">Back to Command</a></p>'

    def health(self):
        checks = {'action_schema_initialized': self.schema_ready}
        for method, path, endpoint in self.routes:
            routes = [r for r in self.ns['app'].routes if getattr(r, 'path', '') == path and method in (getattr(r, 'methods', None) or set())]
            checks[method+' '+path] = len(routes) == 1 and routes[0].endpoint is endpoint
        checks.update(command_actions_installed=getattr(self.ns['app'].state, 'command_actions', None) is self,
                      notice_sharing_supported='command_notice' in self.ns['_BC850_SOURCES'],
                      daily_briefing_preserved=self.ns['app'].state.daily_command.schema_ready,
                      ask_project_data_fix_preserved=all(self.center.data_health()['checks'].values()),
                      form_origin_guard_preserved=callable(self.ns.get('_bc861_same_origin')))
        try:
            with self.db() as c:
                c.execute('SELECT revision,share_id,followup_id,state FROM bc_command_action_plans WHERE 1=0')
                c.execute('SELECT token_hash,session_hash,binding_hash,consumed_at FROM bc_command_action_reviews WHERE 1=0')
                c.execute('SELECT followup_date,closed_at FROM bc_command_followups WHERE 1=0')
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        return {'app': 'BuildCommand AI', 'version': VERSION, 'release': RELEASE, 'status': 'ok' if all(checks.values()) else 'degraded',
                'checks': checks, 'passed': sum(checks.values()), 'total': len(checks), 'data_reset': False,
                'scope': 'Schema and active route checks only. Test Ask, exact review, trade-only notice access, retry, dated follow-ups and saved briefings on staging.'}

    def register(self):
        for method, path, fn in [
            ('POST', '/workspace/command/projects/{project_id}/actions/prepare', self.prepare),
            ('GET', '/workspace/command/projects/{project_id}/actions', self.history),
            ('GET', '/workspace/command/actions/{plan_id}', self.detail),
            ('POST', '/workspace/command/actions/{plan_id}/review', self.review),
            ('POST', '/workspace/command/actions/{plan_id}/approve', self.approve),
            ('POST', '/workspace/command/actions/{plan_id}/refresh', self.refresh),
            ('POST', '/workspace/command/actions/{plan_id}/cancel', self.cancel),
            ('POST', '/workspace/command/followups/{followup_id}/close', self.close)]:
            endpoint = self.ns['_bc850_endpoint'](fn)
            self.ns['app'].add_api_route(path, endpoint, methods=[method])
            self.routes.append((method, path, endpoint))
        self.ns['app'].add_api_route('/health/reviewed-actions-8-11-0', self.health, methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add('/health/reviewed-actions-8-11-0')
