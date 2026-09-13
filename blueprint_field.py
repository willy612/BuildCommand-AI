"""BuildCommand AI 8.7.0: reviewed Blueprint-to-Field publications.

Publications are immutable, explicitly selected requirements, not links into
live internal scopes. The existing sharing service owns recipient permissions,
progress updates, revocation and review. This module owns scope preparation,
review tokens, source checks, issued snapshots and the Command summary.
"""
import hashlib
import html
import json
import logging
import re
import secrets
from collections import Counter
from datetime import datetime, timedelta

from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response

VERSION = '8.7.0'
RELEASE = 'Blueprint to Field'
log = logging.getLogger('buildcommand.blueprint_field')
ITEM_FIELDS = ('id', 'trade', 'requirement', 'source_sheet', 'source_detail',
               'source_spec', 'source_note', 'related_trade', 'confidence', 'item_type')
REFERENCE_FIELDS = ('source_sheet', 'source_detail', 'source_spec', 'source_note')


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def install(namespace):
    service = BlueprintField(namespace)
    namespace['app'].state.blueprint_field = service
    service.register()
    return service


class BlueprintField:
    def __init__(self, ns):
        self.ns = ns
        self.rt = ns['_runtime']
        self.version = VERSION
        self.db = ns['_bc850_db']
        self.require = ns['_bc850_require']
        self.schema_ready = self.initialize()
        self.routes = []

    def initialize(self):
        try:
            with self.db(True) as c:
                key = 'BIGSERIAL PRIMARY KEY' if self.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_scope_reviews(
                    id {key},token_hash TEXT NOT NULL UNIQUE,session_hash TEXT NOT NULL,
                    actor_user_id BIGINT NOT NULL,company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,
                    scope_id BIGINT NOT NULL,source_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,consumed_at TEXT)''')
                c.execute(f'''CREATE TABLE IF NOT EXISTS bc_scope_publications(
                    id {key},share_id BIGINT NOT NULL,share_version INTEGER NOT NULL,
                    company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,scope_id BIGINT NOT NULL,
                    run_id BIGINT NOT NULL,source_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                    created_by BIGINT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(share_id,share_version))''')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_scope_reviews_expiry ON bc_scope_reviews(expires_at)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_bc_scope_publications_project ON bc_scope_publications(company_id,project_id,scope_id)')
                c.execute('SELECT share_id,share_version,snapshot_json,source_hash FROM bc_scope_publications WHERE 1=0')
            return True
        except Exception:
            log.exception('Blueprint-to-Field schema initialization failed')
            return False

    @property
    def postgres(self):
        return getattr(self.rt, 'DATABASE_KIND', 'sqlite') == 'postgres'

    @property
    def lock(self):
        return ' FOR UPDATE' if self.postgres else ''

    def now(self):
        return self.ns['_bc830b_now']()

    def actor(self, c, pid, lock=False):
        self.require(self.schema_ready, 'Trade scope setup is unavailable. Ask your administrator to check the release installation.', 503)
        user = self.ns['_bc850_actor'](c)
        project = self.ns['_bc850_project'](c, user, pid, lock)
        # Refresh after the company/project lock and keep the actor stable.
        row = c.execute('SELECT id,company_id,email,role,display_name FROM users WHERE id=?' + (self.lock if lock else ''), (user['id'],)).fetchone()
        self.require(row is not None, 'Sign in again to continue.', 401)
        user = dict(row)
        self.ns['_bc850_project'](c, user, pid)
        return user, project

    def session_hash(self, request):
        cookie = request.cookies.get('bc_session', '')
        self.require(bool(cookie), 'Sign in again to review this scope.', 401)
        return digest(cookie)

    def insert(self, c, table, columns, values):
        self.require(table in {'bc_scope_reviews', 'bc_scope_publications'}, 'Unsupported scope operation.', 400)
        sql = f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})'
        if self.postgres:
            return int(c.execute(sql + ' RETURNING id', tuple(values)).fetchone()['id'])
        return int(c.execute(sql, tuple(values)).lastrowid)

    def source(self, c, user, pid, scope_id, lock=False):
        cid = int(user['company_id'])
        row = c.execute('SELECT * FROM blueprint_trade_scopes WHERE id=? AND company_id=? AND project_id=?' + (self.lock if lock else ''), (scope_id, cid, pid)).fetchone()
        self.require(row is not None, 'This trade scope is unavailable in this project.', 404)
        scope = dict(row)
        run = c.execute('SELECT id,status,created FROM blueprint_runs WHERE id=? AND company_id=? AND project_id=?' + (self.lock if lock else ''), (scope['run_id'], cid, pid)).fetchone()
        self.require(run is not None and str(run['status'] or '').upper() in {'COMPLETE', 'COMPLETED', 'SUCCESS'}, 'Choose a completed Blueprint Brain analysis.', 409)
        rows = c.execute('SELECT * FROM blueprint_scope_items WHERE trade_scope_id=? AND run_id=? AND company_id=? AND project_id=? ORDER BY id' + (self.lock if lock else ''), (scope_id, scope['run_id'], cid, pid)).fetchall()
        items = [{key: dict(row).get(key) for key in ITEM_FIELDS} for row in rows]
        return {'scope_id': int(scope_id), 'run_id': int(scope['run_id']), 'trade': scope['trade'],
                'division': scope['division'] or '', 'analysis_date': run['created'] or '', 'items': items}

    def list_sources(self, c, user, pid, scope_id=None, query=''):
        cid = int(user['company_id'])
        sql = '''SELECT s.id,s.trade AS title,'' AS due_date,s.run_id,s.item_count,r.created
                 FROM blueprint_trade_scopes s JOIN blueprint_runs r ON r.id=s.run_id
                 AND r.company_id=s.company_id AND r.project_id=s.project_id
                 WHERE s.company_id=? AND s.project_id=? AND UPPER(r.status) IN ('COMPLETE','COMPLETED','SUCCESS')'''
        args = [cid, pid]
        if scope_id is not None:
            sql += ' AND s.id=?'; args.append(scope_id)
        elif query:
            sql += ' AND LOWER(s.trade) LIKE LOWER(?)'; args.append('%' + query[:120] + '%')
        rows = [dict(r) for r in c.execute(sql + ' ORDER BY s.run_id DESC,s.trade,s.id LIMIT 100', tuple(args)).fetchall()]
        if scope_id is not None:
            self.require(bool(rows), 'This scope is unavailable in this project.', 404)
            return rows[0]
        return rows

    def warnings(self, source, items):
        counts = Counter(' '.join(str(i['requirement'] or '').lower().split()) for i in items)
        warnings = []
        if any(not any(str(i.get(k) or '').strip() for k in REFERENCE_FIELDS) for i in items):
            warnings.append('Some requirements have no source reference. Check the plans before issuing them.')
        if any(str(i.get('confidence') or '').upper() != 'HIGH' for i in items):
            warnings.append('Some assignments need closer review. AI confidence does not replace your scope check.')
        if any(str(i.get('trade') or '').strip().casefold() != str(source['trade']).strip().casefold() for i in items):
            warnings.append('Some requirements name a different trade. Correct ownership in Blueprint Brain or exclude those items.')
        if any(n > 1 for n in counts.values()):
            warnings.append('Repeated requirement text was found. Confirm that each included item is necessary.')
        return warnings

    def snapshot_html(self, snapshot):
        body = '<section class="card bc870-scope"><h2>' + esc(snapshot['trade']) + ' · Issued requirements</h2>'
        body += '<p class="small">Blueprint analysis #' + str(snapshot['run_id']) + ' · ' + esc(snapshot['analysis_date']) + '</p><ol>'
        for item in snapshot['items']:
            refs = ' · '.join(str(item.get(k) or '').strip() for k in REFERENCE_FIELDS if str(item.get(k) or '').strip())
            body += '<li style="margin:18px 0;overflow-wrap:anywhere"><p style="white-space:pre-wrap">' + esc(item['requirement']) + '</p><p class="small"><strong>Source:</strong> ' + esc(refs or 'Not recorded; reviewed by the project leader') + '</p></li>'
        return body + '</ol><p class="small">This is the reviewed copy issued to you. Later analysis changes do not alter it. Drawing files are shared separately.</p></section>'

    def home(self, project_id: int = 0, q: str = ''):
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            projects = self.ns['_bc850_projects'](c, user)
            if not project_id:
                selected = c.execute('SELECT selected_project_id FROM user_state WHERE user_id=?', (user['id'],)).fetchone()
                project_id = int(selected['selected_project_id'] or 0) if selected else 0
                if project_id not in {p['id'] for p in projects}:
                    project_id = projects[0]['id'] if len(projects) == 1 else 0
            if not project_id:
                links = ''.join(f'<p><a href="/workspace/scopes?project_id={p["id"]}">{esc(p["name"])}</a></p>' for p in projects)
                return self.page('Trade scopes', '<div class="card"><h1>Choose a project</h1>' + (links or '<p>Ask your company administrator to assign you to a project.</p>') + '</div>')
            user, project = self.actor(c, project_id)
            sources = self.list_sources(c, user, project_id, query=q)
            published = c.execute("SELECT source_id,COUNT(*) AS n FROM bc_shared_work WHERE company_id=? AND project_id=? AND kind='scope' AND revoked_at IS NULL GROUP BY source_id", (user['company_id'], project_id)).fetchall()
            counts = {r['source_id']: r['n'] for r in published}
        body = '<div class="hero"><h1>Trade scopes</h1><p>' + esc(project['name']) + ' · Review the work. Choose the trade partner. Publish the issued copy.</p></div>'
        body += f'<div class="card"><form method="get"><input type="hidden" name="project_id" value="{project_id}"><label for="scope-search">Find a trade</label> <input id="scope-search" name="q" maxlength="120" value="{esc(q[:120])}"> <button>Find</button></form><p>Showing up to 100 scopes, newest analyses first.</p></div><div class="grid2">'
        for s in sources:
            body += '<article class="card"><span class="bc840-pill">Analysis #' + str(s['run_id']) + '</span><h2>' + esc(s['title']) + '</h2><p>' + esc(s['created']) + ' · ' + str(counts.get(s['id'], 0)) + ' active publication(s)</p>'
            body += f'<a class="bc840-button" href="/workspace/scopes/{s["id"]}/prepare?project_id={project_id}">Review &amp; publish scope</a></article>'
        if not sources:
            body += '<div class="card"><h2>No completed trade scopes found</h2><p>Analyze the project plans in Blueprint Brain, then return here to review the trade requirements.</p>' + self.tool_form(project_id, 'blueprint', 'Open Blueprint Brain') + '</div>'
        body += f'</div><p><a href="/workspace/command?project_id={project_id}">Back to Superintendent Command</a></p>'
        return self.page('Trade scopes', body)

    def prepare(self, scope_id: int, project_id: int):
        with self.db() as c:
            user, project = self.actor(c, project_id)
            source = self.source(c, user, project_id, scope_id)
            table = self.ns['_bc850_member_table']()
            rows = c.execute(f'SELECT u.id,u.email,u.display_name,u.role FROM users u JOIN {table} m ON m.user_id=u.id WHERE u.company_id=? AND m.project_id=? ORDER BY u.email', (user['company_id'], project_id)).fetchall()
            recipients = [dict(r) for r in rows if self.ns['_bc840_tier'](dict(r)) == 'trade']
        body = '<div class="hero"><h1>Review ' + esc(source['trade']) + '</h1><p>' + esc(project['name']) + ' · Blueprint analysis #' + str(source['run_id']) + '</p></div>'
        body += self.tool_form(project_id, 'scope-' + str(scope_id), 'Open Blueprint Brain to correct this scope')
        body += '<div class="card"><h2>Before you issue this scope</h2><p>Choose the requirements this subcontractor should receive. Review trade ownership and drawing references.</p>'
        for warning in self.warnings(source, source['items']):
            body += '<p role="note">' + esc(warning) + '</p>'
        body += '</div>'
        if not recipients or not source['items']:
            body += '<div class="card"><p>' + ('Assign a subcontractor to this project first.' if not recipients else 'This scope has no requirements to publish.') + f'</p><a href="/workspace/sharing/projects/{project_id}/team">Manage project subcontractors</a></div>'
            return self.page('Review trade scope', body)
        body += f'<form class="card" method="post" action="/workspace/scopes/{scope_id}/review"><input type="hidden" name="project_id" value="{project_id}"><input type="hidden" name="source_hash" value="{digest(packed(source))}"><fieldset><legend>Included requirements</legend>'
        for item in source['items']:
            refs = ' · '.join(str(item.get(k) or '').strip() for k in REFERENCE_FIELDS if str(item.get(k) or '').strip())
            body += f'<div style="padding:14px 0;border-bottom:1px solid #dce3ec"><label><input type="checkbox" name="item_ids" value="{item["id"]}" checked> {esc(item["requirement"])}</label><p class="small">Trade: {esc(item["trade"])} · Confidence: {esc(item["confidence"])}<br>Source: {esc(refs or "Not recorded")}</p></div>'
        options = ''.join(f'<option value="{r["id"]}">{esc(r["display_name"] or r["email"])} · {esc(r["email"])}</option>' for r in recipients)
        body += '</fieldset><p><label for="scope-recipient">Assigned subcontractor</label><br><select id="scope-recipient" name="recipient_user_id" required><option value="">Choose a person</option>' + options + '</select></p>'
        body += '<p><label for="scope-title">Shared title</label><br><input id="scope-title" name="title" maxlength="240" required value="' + esc(str(source['trade']) + ' scope of work') + '" style="width:100%"></p>'
        body += '<p><label for="scope-message">Instructions</label><br><textarea id="scope-message" name="message" maxlength="6000" rows="4" required style="width:100%">Please review the issued requirements, acknowledge this scope, and report progress or blockers here.</textarea></p>'
        body += '<p><label for="scope-due">Due date (optional)</label><br><input id="scope-due" type="date" name="due_date"></p><p><label><input type="checkbox" name="allow_response" value="1" checked> Allow progress updates and replies</label></p>'
        body += '<button>Preview for this subcontractor</button></form>'
        return self.page('Review trade scope', body)

    def review(self, scope_id: int, request: Request, project_id: int = Form(...),
               source_hash: str = Form(...), recipient_user_id: int = Form(...),
               item_ids: list[int] = Form(...), title: str = Form(...), message: str = Form(...),
               due_date: str = Form(''), allow_response: int = Form(0)):
        title, message, due_date = title.strip(), message.strip(), due_date.strip()
        self.require(bool(re.fullmatch(r'[0-9a-f]{64}', source_hash)), 'Reopen the scope review form and try again.', 400)
        self.require(0 < len(title) <= 240 and 0 < len(message) <= 6000, 'Enter a title and instructions within the displayed limits.', 400)
        self.require(allow_response in {0, 1}, 'Choose whether replies are allowed.', 400)
        self.require(0 < len(item_ids) <= 2000 and len(set(item_ids)) == len(item_ids), 'Select a valid set of requirements.', 400)
        if due_date:
            try:
                self.require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', due_date)), 'Enter a valid due date.', 400)
                datetime.strptime(due_date, '%Y-%m-%d')
            except ValueError:
                self.require(False, 'Enter a valid due date.', 400)
        with self.db(True) as c:
            user, project = self.actor(c, project_id, True)
            recipient = self.ns['_bc850_recipient'](c, user, project_id, recipient_user_id)
            source = self.source(c, user, project_id, scope_id, True)
            self.require(secrets.compare_digest(digest(packed(source)), source_hash), 'The scope changed. Reopen the scope and review its current requirements.', 409)
            selected = [i for i in source['items'] if i['id'] in set(item_ids)]
            self.require(len(selected) == len(item_ids), 'A selected requirement is not in this scope.', 400)
            self.require(all(str(i['requirement'] or '').strip() for i in selected), 'Remove or correct empty requirements before publishing.', 400)
            snapshot = {**source, 'items': selected}
            previous = c.execute("SELECT id,version,revoked_at FROM bc_shared_work WHERE company_id=? AND project_id=? AND kind='scope' AND source_id=? AND recipient_user_id=?", (user['company_id'], project_id, scope_id, recipient_user_id)).fetchone()
            self.require(previous is None or previous['revoked_at'] is not None, 'This scope is already shared with that person. Revoke it before issuing a replacement.', 409)
            payload = {'snapshot': snapshot, 'recipient': recipient, 'title': title, 'message': message, 'due_date': due_date, 'allow_response': allow_response,
                       'previous': dict(previous) if previous else None}
            token = secrets.token_urlsafe(32)
            now = self.now()
            c.execute('DELETE FROM bc_scope_reviews WHERE expires_at<?', (now.isoformat(),))
            self.insert(c, 'bc_scope_reviews', 'token_hash,session_hash,actor_user_id,company_id,project_id,scope_id,source_hash,payload_json,expires_at',
                        (digest(token), self.session_hash(request), user['id'], user['company_id'], project_id, scope_id, source_hash, packed(payload), (now + timedelta(minutes=15)).isoformat()))
        body = '<div class="hero"><h1>Preview before publishing</h1><p>' + esc(project['name']) + '</p></div><div class="card"><h2>' + esc(title) + '</h2><p><strong>Recipient:</strong> ' + esc(recipient['display_name'] or recipient['email']) + ' · ' + esc(recipient['email']) + '</p><p><strong>Due:</strong> ' + esc(due_date or 'Not set') + '</p><p style="white-space:pre-wrap">' + esc(message) + '</p><p>Replies: ' + ('Allowed' if allow_response else 'Disabled') + '</p></div>'
        body += self.snapshot_html(snapshot)
        for warning in self.warnings(source, selected):
            body += '<p class="card">' + esc(warning) + '</p>'
        body += '<form class="card" method="post" action="/workspace/scopes/publish"><input type="hidden" name="review_token" value="' + token + '"><label><input type="checkbox" name="confirmed" value="yes" required> I reviewed the recipient, included requirements, trade ownership and source references.</label><p>This publishes the copy shown above. The preview expires in 15 minutes.</p><button>Publish reviewed scope</button></form>'
        body += f'<p><a href="/workspace/scopes/{scope_id}/prepare?project_id={project_id}">Back to edit</a></p>'
        return self.page('Preview trade scope', body)

    def publish(self, request: Request, review_token: str = Form(...), confirmed: str = Form('')):
        self.require(confirmed == 'yes', 'Review the preview and confirm before publishing.', 400)
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}', review_token)), 'This preview is invalid. Prepare the scope again.', 403)
        with self.db(True) as c:
            row = c.execute('SELECT * FROM bc_scope_reviews WHERE token_hash=?', (digest(review_token),)).fetchone()
            self.require(row is not None, 'This preview is unavailable. Prepare the scope again.', 409)
            review = dict(row)
            user, project = self.actor(c, review['project_id'], True)
            self.require(user['id'] == review['actor_user_id'] and user['company_id'] == review['company_id'] and secrets.compare_digest(self.session_hash(request), review['session_hash']), 'Prepare this scope from your own signed-in session.', 403)
            review = dict(c.execute('SELECT * FROM bc_scope_reviews WHERE id=?' + self.lock, (review['id'],)).fetchone())
            self.require(not review['consumed_at'] and review['expires_at'] > self.now().isoformat(), 'This preview expired or was already used. Prepare the scope again.', 409)
            payload = json.loads(review['payload_json'])
            recipient = payload['recipient']
            c.execute('SELECT id FROM users WHERE id=?' + self.lock, (recipient['id'],)).fetchone()
            live_recipient = self.ns['_bc850_recipient'](c, user, project['id'], recipient['id'])
            self.require(live_recipient == recipient, 'The recipient account changed. Review the scope again.', 409)
            source = self.source(c, user, project['id'], review['scope_id'], True)
            self.require(secrets.compare_digest(digest(packed(source)), review['source_hash']), 'The scope changed after your preview. Review the current requirements before publishing.', 409)
            previous = c.execute("SELECT id,version,revoked_at FROM bc_shared_work WHERE company_id=? AND project_id=? AND kind='scope' AND source_id=? AND recipient_user_id=?" + self.lock, (user['company_id'], project['id'], review['scope_id'], recipient['id'])).fetchone()
            self.require((dict(previous) if previous else None) == payload['previous'] and (previous is None or previous['revoked_at'] is not None), 'The sharing state changed. Review this scope again.', 409)
            now = self.now().isoformat()
            if previous:
                sid, version = previous['id'], previous['version'] + 1
                c.execute("UPDATE bc_shared_work SET title=?,message=?,due_date=?,allow_response=?,version=?,revoked_at=NULL,state='OPEN',created_by=?,updated_at=? WHERE id=?", (payload['title'], payload['message'], payload['due_date'], payload['allow_response'], version, user['id'], now, sid))
            else:
                version = 1
                sid = self.ns['_bc850_insert'](c, 'bc_shared_work', 'company_id,project_id,kind,source_id,recipient_user_id,title,message,due_date,allow_response,created_by,created_at,updated_at',
                    (user['company_id'], project['id'], 'scope', review['scope_id'], recipient['id'], payload['title'], payload['message'], payload['due_date'], payload['allow_response'], user['id'], now, now))
            snapshot = {**payload['snapshot'], 'title': payload['title'], 'message': payload['message'], 'due_date': payload['due_date'], 'recipient_name': recipient['display_name'] or recipient['email']}
            self.insert(c, 'bc_scope_publications', 'share_id,share_version,company_id,project_id,scope_id,run_id,source_hash,snapshot_json,created_by,created_at',
                        (sid, version, user['company_id'], project['id'], review['scope_id'], source['run_id'], review['source_hash'], packed(snapshot), user['id'], now))
            self.ns['_bc850_event'](c, user, project['id'], sid, 'SCOPE_PUBLISHED')
            c.execute('UPDATE bc_scope_reviews SET consumed_at=? WHERE id=?', (now, review['id']))
        return RedirectResponse(f'/workspace/sharing/{sid}', status_code=303)

    def publication(self, c, share):
        row = c.execute('''SELECT * FROM bc_scope_publications WHERE share_id=? AND company_id=? AND project_id=? AND scope_id=? AND share_version<=? ORDER BY share_version DESC LIMIT 1''', (share['id'], share['company_id'], share['project_id'], share['source_id'], share['version'])).fetchone()
        self.require(row is not None, 'This issued scope is unavailable. Ask your project leader to review it.', 409)
        return dict(row)

    def issued_html(self, c, share, manager=False):
        publication = self.publication(c, share)
        body = self.snapshot_html(json.loads(publication['snapshot_json']))
        body += '<div class="card"><p>Issued version ' + str(publication['share_version']) + ' · ' + esc(publication['created_at']) + ' UTC</p>'
        prefix = '/workspace/sharing/' if manager else '/workspace/shared/'
        if not share['revoked_at']:
            body += f'<a href="{prefix}{share["id"]}/scope.txt">Download issued scope</a>'
        if manager:
            rows = c.execute('SELECT share_version,created_at FROM bc_scope_publications WHERE share_id=? AND company_id=? AND project_id=? ORDER BY share_version DESC', (share['id'], share['company_id'], share['project_id'])).fetchall()
            body += '<details><summary>Publication history</summary>' + ''.join(f'<p><a href="/workspace/sharing/{share["id"]}/scope-history/{r["share_version"]}">Issued version {r["share_version"]}</a> · {esc(r["created_at"])}</p>' for r in rows) + '</details>'
        return body + '</div>'

    def history(self, share_id: int, version: int):
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            share = self.ns['_bc850_share'](c, user, share_id, True)
            self.require(share['kind'] == 'scope', 'This item has no issued scope history.', 404)
            row = c.execute('SELECT snapshot_json,created_at FROM bc_scope_publications WHERE share_id=? AND share_version=? AND company_id=? AND project_id=?', (share_id, version, user['company_id'], share['project_id'])).fetchone()
            self.require(row is not None, 'Issued version not found.', 404)
        snapshot = json.loads(row['snapshot_json'])
        return self.page('Issued scope history', '<div class="hero"><h1>Issued version ' + str(version) + '</h1><p>' + esc(row['created_at']) + '</p></div><div class="card"><h2>' + esc(snapshot['title']) + '</h2><p>Recipient: ' + esc(snapshot['recipient_name']) + '</p><p style="white-space:pre-wrap">' + esc(snapshot['message']) + '</p><p>Due: ' + esc(snapshot['due_date'] or 'Not set') + '</p></div>' + self.snapshot_html(snapshot))

    def download(self, share_id, manager=False):
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            share = self.ns['_bc850_share'](c, user, share_id, manager)
            self.require(share['kind'] == 'scope' and not share['revoked_at'], 'This issued scope is unavailable.', 403)
            publication = self.publication(c, share)
        data = json.loads(publication['snapshot_json'])
        lines = ['BuildCommand AI — Issued trade scope', data['title'], 'Trade: ' + str(data['trade']),
                 f'Issued version: {publication["share_version"]}', 'Issued: ' + publication['created_at'] + ' UTC',
                 'Recipient: ' + data['recipient_name'], 'Due: ' + (data['due_date'] or 'Not set'), '', data['message'], '']
        for n, item in enumerate(data['items'], 1):
            lines += [f'{n}. {item["requirement"]}', 'Source: ' + (' · '.join(str(item.get(k) or '') for k in REFERENCE_FIELDS if item.get(k)) or 'Not recorded'), '']
        lines.append('Fixed issued copy. Drawing files are shared separately.')
        return Response('\n'.join(lines), media_type='text/plain; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="issued-scope-{share_id}-v{publication["share_version"]}.txt"', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    def trade_download(self, share_id: int):
        return self.download(share_id)

    def manager_download(self, share_id: int):
        return self.download(share_id, True)

    def tool_form(self, pid, tool, label):
        return f'<form method="post" action="/workspace/command/projects/{pid}/open-tool" style="display:inline-block;margin:4px"><button class="secondary" name="tool" value="{esc(tool)}">{esc(label)}</button></form>'

    def tool_destination(self, project_id, tool):
        rfis = getattr(self.ns['app'].state, 'rfi_field', None)
        if rfis is not None and tool.startswith('rfi-source-'):
            return rfis.tool_destination(project_id,tool)
        choices = {'blueprint': '/blueprint-brain', 'photo': '/photo-ai', 'brief': '/morning-brief', 'daily': '/daily-report', 'photo-rfis': '/issues'}
        if tool in choices:
            return choices[tool]
        if re.fullmatch(r'scope-\d+', tool):
            scope_id = int(tool.split('-')[1])
            with self.db() as c:
                user, _ = self.actor(c, project_id)
                self.source(c, user, project_id, scope_id)
            return '/blueprint-brain/trade/' + str(scope_id)
        return None

    def page(self, title, body):
        response = self.ns['_bc840_page'](title, body)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    def health(self):
        checks = {'scope_schema_initialized': self.schema_ready}
        for method, path, endpoint in self.routes:
            routes = [r for r in self.ns['app'].routes if getattr(r, 'path', '') == path and method in (getattr(r, 'methods', None) or set())]
            checks[method + ' ' + path] = len(routes) == 1 and routes[0].endpoint is endpoint
        checks['scope_sharing_supported'] = 'scope' in self.ns['_BC850_SOURCES']
        checks['command_preserved'] = callable(self.ns.get('bc860_command'))
        checks['form_origin_guard_preserved'] = callable(self.ns.get('_bc861_same_origin'))
        try:
            with self.db() as c:
                c.execute('SELECT token_hash,session_hash,consumed_at FROM bc_scope_reviews WHERE 1=0')
                c.execute('SELECT share_id,share_version,snapshot_json FROM bc_scope_publications WHERE 1=0')
                c.execute('SELECT id,company_id,project_id,run_id,trade FROM blueprint_trade_scopes WHERE 1=0')
                c.execute('SELECT id,trade_scope_id,source_sheet,source_spec FROM blueprint_scope_items WHERE 1=0')
            checks['schema_readable'] = True
        except Exception:
            log.exception('Blueprint-to-Field health schema check failed')
            checks['schema_readable'] = False
        return {'app': 'BuildCommand AI', 'version': VERSION, 'release': RELEASE,
                'status': 'ok' if all(checks.values()) else 'degraded', 'checks': checks,
                'passed': sum(checks.values()), 'total': len(checks), 'data_reset': False,
                'scope': 'Schema and active route checks only. Test reviewed publishing, real company roles and subcontractor access on staging.'}

    def register(self):
        for method, path, handler in [
            ('GET', '/workspace/scopes', self.home),
            ('GET', '/workspace/scopes/{scope_id}/prepare', self.prepare),
            ('POST', '/workspace/scopes/{scope_id}/review', self.review),
            ('POST', '/workspace/scopes/publish', self.publish),
            ('GET', '/workspace/sharing/{share_id}/scope-history/{version}', self.history),
            ('GET', '/workspace/shared/{share_id}/scope.txt', self.trade_download),
            ('GET', '/workspace/sharing/{share_id}/scope.txt', self.manager_download)]:
            endpoint = self.ns['_bc850_endpoint'](handler)
            self.ns['app'].add_api_route(path, endpoint, methods=[method])
            self.routes.append((method, path, endpoint))
        self.ns['app'].add_api_route('/health/blueprint-to-field-8-7-0', self.health, methods=['GET'])
        self.rt.PUBLIC_PATHS.add('/health/blueprint-to-field-8-7-0')
