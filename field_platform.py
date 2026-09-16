"""Shared, project-bound review primitives for the 8.24 field release."""
import hashlib
import inspect
import json
import logging
import re
import secrets
from datetime import date, timedelta
from functools import wraps
from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from blueprint_field import esc, digest
from command_center import command_json

VERSION = '8.24.0'
RELEASE = 'Connected Field Release'
STYLE = '''<style>.field-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:16px}.field-row{padding:16px 0;border-bottom:1px solid #d8e2eb}.field-muted{color:#52657b;font-size:14px}.field-actions{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}.field-table{width:100%;border-collapse:collapse}.field-table th,.field-table td{padding:12px;text-align:left;border-bottom:1px solid #dae3ec;vertical-align:top}.field-scroll{overflow:auto}.field-exact{white-space:pre-wrap;overflow-wrap:anywhere}.field-form label{display:block;font-weight:600;margin-top:14px}.field-form input:not([type=checkbox]):not([type=radio]),.field-form textarea,.field-form select{width:100%;max-width:700px;box-sizing:border-box;min-height:44px}.field-form textarea{min-height:85px}.field-form button{margin-top:16px}.field-pill{display:inline-block;padding:5px 10px;border-radius:8px;background:#edf2f7}.field-warning{padding:16px;background:#fff3d7;border-radius:10px}.field-form details{margin:18px 0}@media(max-width:650px){.field-table th,.field-table td{padding:8px}.field-actions>a,.field-actions>button{min-height:44px}.field-grid{grid-template-columns:1fr}}</style>'''
log = logging.getLogger('buildcommand.field_platform')


class FieldPlatform:
    def __init__(self, ns):
        self.ns = ns
        self.app = ns['app']
        self.docs = self.app.state.project_documents
        self.field = self.docs.field
        self.db, self.require = self.docs.db, self.docs.require
        self.routes, self.actions, self.tables = [], {}, set()
        self.key = 'BIGSERIAL PRIMARY KEY' if self.field.postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        self.schema_ready = True
        self.schema('bc824_reviews', '''token_hash TEXT NOT NULL UNIQUE,session_hash TEXT NOT NULL,actor_id BIGINT NOT NULL,
            action TEXT NOT NULL,payload_json TEXT NOT NULL,source_hash TEXT NOT NULL,expires TEXT NOT NULL,
            consumed TEXT,result_path TEXT NOT NULL''')
        self.schema('bc824_events', 'actor_id BIGINT NOT NULL,action TEXT NOT NULL,record_id BIGINT NOT NULL,snapshot_json TEXT NOT NULL,created TEXT NOT NULL')
        self.route('/workspace/reviewed-actions/approve', 'POST', self.approve)

    def now(self):
        return self.field.now()

    def schema(self, table, definition, unique=''):
        self.require(bool(re.fullmatch(r'bc824_[a-z_]+', table)), 'Invalid internal table.', 500)
        self.tables.add(table)
        try:
            with self.db(True) as c:
                c.execute(f'CREATE TABLE IF NOT EXISTS {table}(id {self.key},company_id BIGINT NOT NULL,project_id BIGINT NOT NULL,{definition}{unique})')
                c.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_scope ON {table}(company_id,project_id)')
        except Exception:
            self.schema_ready = False
            log.exception('Field release schema initialization failed table=%s', table)

    def insert(self, c, table, values):
        self.require(table in self.tables, 'Unsupported record.', 500)
        sql = f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})'
        if self.field.postgres:
            return int(c.execute(sql+' RETURNING id', tuple(values.values())).fetchone()['id'])
        return int(c.execute(sql, tuple(values.values())).lastrowid)

    def event(self, c, user, pid, action, rid, snapshot):
        return self.insert(c, 'bc824_events', dict(company_id=user['company_id'], project_id=pid,
            actor_id=user['id'], action=action, record_id=rid, snapshot_json=command_json(snapshot), created=self.now().isoformat()))

    def actor(self, c, pid, write=False):
        return self.field.actor(c, pid, write)

    def user(self, c):
        return self.ns['_bc850_actor'](c)

    def scope(self, c, table, rid, write=False):
        self.require(table in self.tables, 'Unsupported record.', 500)
        user = self.user(c)
        row = c.execute(f'SELECT * FROM {table} WHERE id=? AND company_id=?', (rid, user['company_id'])).fetchone()
        self.require(row is not None, 'This record is unavailable.', 404)
        user, project = self.actor(c, row['project_id'], write)
        row = c.execute(f'SELECT * FROM {table} WHERE id=? AND company_id=?', (rid, user['company_id'])).fetchone()
        return user, project, dict(row)

    def origin(self, request):
        self.require(self.schema_ready, 'The update needs an administrator installation check.', 503)
        self.docs.origin(request)

    def text(self, value, maximum=2000, label='text', required=False):
        return self.docs.text(value, maximum, label, required)

    def day(self, value, required=False):
        self.require(not required or bool(value), 'Choose a date.', 400)
        if not value:
            return ''
        try:
            parsed = date.fromisoformat(value)
            self.require(parsed.isoformat()==value, 'Use a valid date.', 400)
        except ValueError:
            self.require(False, 'Use a valid date.', 400)
        return value

    def page(self, title, body):
        return self.docs.page(title, STYLE+body)

    def link(self, path, label):
        return self.docs.link(path, label)

    def hidden(self, name, value):
        return self.docs.hidden(name, value)

    def input(self, name, label, value='', typ='text', extra=''):
        return self.docs.input(name, label, value, typ, extra)

    def area(self, name, label, value='', maximum=4000):
        return f'<label for="f-{name}">{esc(label)}</label><textarea id="f-{name}" name="{name}" maxlength="{maximum}">{esc(value)}</textarea>'

    def chooser(self, c, pid, title, path):
        user, project, projects = self.docs.hub.user(c, pid, True)
        body = '<div class="hero"><h1>'+esc(title)+'</h1></div>'+self.docs.hub.selector(projects, project, path)
        return user, project, body

    def route(self, path, method, fn):
        def failed(exc):
            if isinstance(exc,self.ns['_BC850_Problem']):raise exc
            reference='FIELD-'+secrets.token_hex(4).upper()
            log.error('Field action failed reference=%s handler=%s category=%s',reference,fn.__name__,type(exc).__name__)
            self.require(False,'This action could not finish. Reopen the record to check its current state. Support reference '+reference+'.',503)
        @wraps(fn)
        def checked(*args, **kwargs):
            try:
                self.require(self.schema_ready, 'The field update is unavailable. Check its installation.', 503)
                return fn(*args, **kwargs)
            except Exception as exc:return failed(exc)
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def checked(*args, **kwargs):
                try:
                    self.require(self.schema_ready, 'The field update is unavailable. Check its installation.', 503)
                    return await fn(*args, **kwargs)
                except Exception as exc:return failed(exc)
        endpoint = self.docs.endpoint(checked)
        self.ns['_bc840_replace'](path, method, endpoint)
        self.routes.append((path, method, endpoint))

    def action(self, name, binding, apply):
        self.require(name not in self.actions, 'Duplicate review action.', 500)
        self.actions[name] = (binding, apply)

    def prepare(self, c, user, project, request, action, payload, title, body, back):
        source = self.actions[action][0](c, user, project, payload)
        raw = secrets.token_urlsafe(32)
        self.insert(c, 'bc824_reviews', dict(company_id=user['company_id'], project_id=project['id'],
            actor_id=user['id'], token_hash=digest(raw), session_hash=self.field.session_hash(request),
            action=action, payload_json=command_json(payload), source_hash=digest(command_json(source)),
            expires=(self.now()+timedelta(minutes=15)).isoformat(), consumed=None, result_path=''))
        html = '<div class="hero"><h1>'+esc(title)+'</h1><p>'+esc(project['name'])+'</p></div>'+body
        html += '<form class="card field-form" method="post" action="/workspace/reviewed-actions/approve">'+self.hidden('review_token', raw)
        html += '<label><input type="checkbox" name="confirmed" value="yes" required> I reviewed the exact information, recipient and proposed action shown above.</label><button>Approve reviewed action</button><p class="field-muted">Review expires in 15 minutes. Changed source records require a new review.</p></form>'+self.link(back, 'Back to edit')
        return self.page(title, html)

    def approve(self, request:Request, review_token:str=Form(...), confirmed:str=Form('')):
        self.origin(request)
        self.require(confirmed=='yes', 'Review and confirm the proposed action first.', 400)
        self.require(bool(re.fullmatch(r'[A-Za-z0-9_-]{40,80}', review_token)), 'Reopen the review.', 403)
        with self.db(True) as c:
            user = self.user(c)
            row = c.execute('SELECT * FROM bc824_reviews WHERE token_hash=? AND company_id=?', (digest(review_token), user['company_id'])).fetchone()
            self.require(row is not None, 'This review is unavailable.', 403)
            user, project = self.actor(c, row['project_id'], True)
            row = dict(c.execute('SELECT * FROM bc824_reviews WHERE id=?'+self.field.lock, (row['id'],)).fetchone())
            self.require(row['actor_id']==user['id'] and secrets.compare_digest(row['session_hash'], self.field.session_hash(request)), 'Approve from the session that prepared the review.', 403)
            if row['consumed']:
                return RedirectResponse(row['result_path'], 303)
            self.require(row['expires']>self.now().isoformat(), 'This review expired. Review the current information again.', 409)
            payload = json.loads(row['payload_json'])
            binding, apply = self.actions[row['action']]
            self.require(digest(command_json(binding(c,user,project,payload)))==row['source_hash'], 'The source or recipient changed. Review the current information again.', 409)
            path = apply(c, user, project, payload)
            self.require(path.startswith('/workspace/'), 'Invalid result destination.', 500)
            self.event(c,user,project['id'],'Approved '+row['action'],row['id'],payload)
            c.execute('UPDATE bc824_reviews SET consumed=?,result_path=? WHERE id=?', (self.now().isoformat(), path, row['id']))
        return RedirectResponse(path, 303)
