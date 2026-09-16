"""8.12.0: record-based first-job setup using the established project workflows.

Reads never select projects, invite people, run AI or publish work. Assignments
are additive and use the same company/project locking as trade sharing.
"""
from pathlib import PurePath
from fastapi import Form
from fastapi.responses import RedirectResponse
from blueprint_field import esc

VERSION = '8.12.0'
RELEASE = 'First Job Setup'
ROOT = '/workspace/setup'
CSS = '''<style>
.bc-setup{max-width:1050px;margin:auto;overflow-wrap:anywhere}.bc-setup h1{font-size:clamp(28px,4vw,40px)}
.bc-setup .setup-next{background:#112638;color:white;padding:26px;border-radius:14px;margin:22px 0}
.bc-setup .setup-next p{color:#dbe5ef}.bc-setup .setup-next a,.bc-setup .setup-next button{background:#f4bc49;color:#13283b}
.bc-setup .setup-steps{list-style:none;padding:0;counter-reset:step}.bc-setup .setup-step{counter-increment:step;padding:22px;border:1px solid #d7e1eb;border-radius:12px;background:white;margin:12px 0}
.bc-setup .setup-step h2{font-size:21px;margin:0}.bc-setup .setup-step h2:before{content:counter(step) '. ';color:#986715}
.bc-setup .setup-status{display:inline-block;font-weight:700;font-size:14px;margin:10px 0;padding:5px 10px;border-radius:6px;background:#eef3f8;color:#253f56}
.bc-setup .done{background:#e5f4eb;color:#245238}.bc-setup .waiting{background:#fff1d6;color:#714600}
.bc-setup .setup-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:18px 0}
.bc-setup label{display:block;font-weight:700;margin:18px 0 7px}.bc-setup input:not([type=hidden]),.bc-setup select{box-sizing:border-box;width:100%;padding:12px;border:1px solid #a5b7cb;border-radius:8px;font:inherit}
.bc-setup button,.bc-setup .bc840-button{min-height:46px;white-space:normal}.bc-setup .setup-person{border-top:1px solid #d7e1eb;padding:16px 0}
.bc-setup .setup-note{padding:16px;background:#eef3f8;border-radius:8px}.bc-setup summary{cursor:pointer;font-weight:700;padding:12px 0}
.bc-setup :focus-visible{outline:3px solid #b16b00;outline-offset:4px}
@media(max-width:640px){.bc-setup .setup-actions>*{width:100%;box-sizing:border-box}.bc-setup .setup-step{padding:18px}.bc-setup .setup-next{padding:20px}}
</style>'''


def install(ns):
    service = ProjectSetup(ns)
    ns['app'].state.project_setup = service
    service.register()
    return service


class ProjectSetup:
    def __init__(self, ns):
        self.ns = ns
        self.field = ns['app'].state.blueprint_field
        self.db, self.require = self.field.db, self.field.require
        self.routes = []
        self.pilot_guidance_version = "8.25.0"

    def admin(self, user):
        return self.ns['_bc840_tier'](user) in {'owner', 'admin'}

    def actor(self, c, pid, lock=False):
        return self.field.actor(c, pid, lock)

    def page(self, title, body):
        return self.ns['_bc840_page'](title, CSS+'<div class="bc-setup">'+body+'</div>')

    def link(self, path, label):
        return f'<a class="bc840-button" href="{esc(path)}">{esc(label)}</a>'

    def open_form(self, pid, tool, label):
        return f'<form method="post" action="{ROOT}/projects/{pid}/open"><input type="hidden" name="tool" value="{esc(tool)}"><button>{esc(label)}</button></form>'

    def entry(self, user, pid=None):
        """Small workspace entry; no progress queries on ordinary workspace pages."""
        if not self.ns['_bc850_manager'](user):
            return ''
        if pid is not None:
            with self.db() as c:
                fresh = self.ns['_bc850_actor'](c)
                if not any(p['id'] == pid for p in self.ns['_bc850_projects'](c, fresh)):
                    return ''
        target = f'{ROOT}/projects/{pid}' if pid is not None else ROOT
        return '<div class="card"><h2>Get your job ready</h2><p>Plans, people and project access — see what is ready and what to do next.</p>'+self.link(target, 'Open job setup')+'</div>'

    def return_link(self, user, pid, path):
        if not self.ns['_bc850_manager'](user):
            return ''
        with self.db() as c:
            fresh = self.ns['_bc850_actor'](c)
            if path.startswith('/blueprint-brain/run/'):
                run_id = path.rsplit('/', 1)[-1]
                if not run_id.isdigit(): return ''
                run = c.execute('SELECT project_id FROM blueprint_runs WHERE id=? AND company_id=?', (int(run_id), fresh['company_id'])).fetchone()
                if not run: return ''
                pid = run['project_id']
            if not any(p['id'] == pid for p in self.ns['_bc850_projects'](c, fresh)):
                return ''
        return f'<p><a href="{ROOT}/projects/{pid}">Back to job setup</a></p>'

    def index(self):
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            projects = self.ns['_bc850_projects'](c, user)
        body = '<div class="hero"><div class="eyebrow">FIRST JOB SETUP</div><h1>Get your job ready</h1><p>Create a project, upload plans and bring in your team. Pick a job to see its next step.</p></div>'
        if self.admin(user):
            body += '<div class="setup-actions">'+self.link(ROOT+'/new', 'Create a project')+'</div>'
        if not projects:
            body += '<div class="card"><h2>No projects yet</h2><p>'+('Start with the project name and number.' if self.admin(user) else 'Ask your company administrator to create a project and assign you as its superintendent or project manager.')+'</p></div>'
        for p in projects:
            body += '<div class="card"><span class="small">'+esc(p.get('number') or '')+'</span><h2>'+esc(p['name'])+'</h2>'+self.link(f'{ROOT}/projects/{p["id"]}', 'Continue setup')+'</div>'
        return self.page('Job setup', body)

    def new(self):
        with self.db() as c:
            user = self.ns['_bc850_actor'](c)
            self.require(self.admin(user), 'Your company administrator creates projects.')
        body = '<div class="hero"><div class="eyebrow">FIRST JOB SETUP</div><h1>Create your project</h1><p>Use the name and number your team already knows.</p></div>'
        body += '''<section class="card"><form method="post" action="/projects/new">
            <input type="hidden" name="setup_flow" value="1"><input type="hidden" name="status" value="ACTIVE">
            <label for="setup-name">Project name</label><input id="setup-name" name="name" maxlength="200" required placeholder="Service Wire Canopy">
            <label for="setup-number">Project number</label><input id="setup-number" name="number" maxlength="80" required placeholder="2603">
            <p>Your company's existing project limits apply. The project starts as Active.</p>
            <div class="setup-actions"><button>Create project &amp; continue</button><a href="/workspace/setup">Back</a></div>
            </form></section>'''
        return self.page('Create project', body)

    def records(self, c, user, pid):
        cid = user['company_id']
        table = self.ns['_bc850_member_table']()
        members = [dict(r) for r in c.execute(f'''SELECT DISTINCT u.id,u.email,u.display_name,u.role FROM users u
            JOIN {table} m ON m.user_id=u.id WHERE u.company_id=? AND m.project_id=? ORDER BY u.email''', (cid, pid)).fetchall()]
        pending = [dict(r) for r in c.execute('''SELECT DISTINCT i.id,i.email,i.role,i.expires_at,i.status FROM bc_user_invitations i
            JOIN bc_user_invitation_projects ip ON ip.invitation_id=i.id WHERE i.company_id=? AND ip.project_id=? AND i.status='PENDING'
            ORDER BY i.id DESC''', (cid, pid)).fetchall() if not self.ns['_bc830b_expired'](r)]
        # Completed attachment records only. Unfinished upload sessions and photo
        # evidence are not a substitute for uploaded plans.
        plans = [dict(r) for r in c.execute('SELECT * FROM attachments WHERE company_id=? AND project_id=?', (cid, pid)).fetchall()
                 if str(dict(r).get('category') or '').upper() in {'PLANS', 'PLAN', 'DRAWINGS', 'SPECS', 'SPECIFICATIONS'}
                 and PurePath(str(dict(r).get('original_name') or '')).suffix.lower() in {'.pdf', '.txt', '.csv', '.xlsx', '.xlsm'}]
        run = c.execute('SELECT id,status,created FROM blueprint_runs WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1', (cid, pid)).fetchone()
        return {'members': members, 'pending': pending, 'plans': plans, 'run': dict(run) if run else None}

    def detail(self, project_id:int):
        with self.db() as c:
            user, project = self.actor(c, project_id)
            data = self.records(c, user, project_id)
        pid, base = project_id, f'{ROOT}/projects/{project_id}'
        members, pending, plans, run = (data[k] for k in ('members', 'pending', 'plans', 'run'))
        others = [p for p in members if p['id'] != user['id']]
        leaders = [p for p in members if self.ns['_bc850_manager'](p)]
        if not plans:
            title, message, action = 'Upload your plans', 'Start with the drawings and specifications you want BuildCommand to review.', self.open_form(pid, 'blueprint', 'Upload plans')
        elif not others:
            title, message = 'Bring your team onto the job', 'Invite someone new or assign an existing company teammate. Pending invitations still need to be accepted.'
            action = self.link(base+'/team', 'Set up the team')
        elif not leaders and self.admin(user):
            title, message, action = 'Choose who runs the day', 'You can run Command as a company administrator. Assign a superintendent or project manager when they are ready.', self.link(base+'/team', 'Review project access')
        else:
            title, message, action = 'Start running the day', 'Open Command for priorities and questions. Review the plan analysis before issuing a trade scope.', self.open_form(pid, 'command', 'Open Command')
        body = '<div class="hero"><div class="eyebrow">JOB SETUP · '+esc(project.get('number') or '')+'</div><h1>'+esc(project['name'])+'</h1><p>Your saved project records show what is ready. You can use Command at any time, including while you work alone.</p></div>'
        pilot = getattr(self.ns['app'].state, 'pilot_readiness', None)
        if pilot:
            body += pilot.setup_link(project_id)
        body += '<section class="setup-next" aria-labelledby="setup-next-title"><div>NEXT STEP</div><h2 id="setup-next-title">'+title+'</h2><p>'+message+'</p>'+action+'</section>'
        body += '<ol class="setup-steps">'
        def step(title, status, note, action='', state=''):
            return '<li class="setup-step"><h2>'+title+'</h2><span class="setup-status '+state+'">'+esc(status)+'</span><p>'+note+'</p><div class="setup-actions">'+action+'</div></li>'
        body += step('Create the project', 'Created', esc(project['name'])+' · '+esc(project.get('number') or ''), state='done')
        analysis = 'Analysis has not been run. Uploading a file does not analyze it.'
        if run:
            status = str(run['status'] or '').upper()
            if status in {'COMPLETE', 'COMPLETED'}:
                analysis = f'Latest analysis #{run["id"]} is complete. Open it to check which files were analyzed and review the findings before using them.'
            elif status in {'FAILED', 'ERROR'}:
                analysis = 'The latest analysis did not finish. Open your plans to review the error and try again.'
            else:
                analysis = 'Latest analysis status: '+esc(status.replace('_', ' ').lower() or 'not available')+'. Open your plans to check its progress.'
        body += step('Upload plans', f'{len(plans)} plan / specification files saved' if plans else 'Plans needed', analysis,
                     self.open_form(pid, 'blueprint', 'Open plans & analysis'), 'done' if plans else 'waiting')
        team_note = f'{len(others)} other people assigned to this job; {len(pending)} invitations awaiting acceptance.'
        body += step('Invite the team', 'Teammates joined' if others else ('Waiting for acceptance' if pending else 'Team not added yet'), team_note,
                     self.link(base+'/team', 'Manage this team'), 'done' if others else 'waiting')
        access_note = ('Company administrators can run this job. ' if self.admin(user) else '')+f'{len(leaders)} appointed project leaders; {sum(self.ns["_bc840_tier"](p)=="trade" for p in members)} assigned subcontractors.'
        access_note += ' Subcontractors see only work you review and share with their account. Assignment alone does not share plans or scopes.'
        body += step('Set project access', 'Review who can do what', access_note, self.link(base+'/team', 'Review project access'))
        body += step('Run the day', 'Command is available', 'See today’s priorities, ask about the job and prepare actions for your review.', self.open_form(pid, 'command', 'Open Command'))
        body += '</ol><details class="card"><summary>Next: put the plans to work</summary><p>Run the plan analysis, review each trade’s scope, then publish the approved work to a named subcontractor. Inviting someone does not publish anything.</p><div class="setup-actions">'+self.open_form(pid, 'scopes', 'Review trade scopes')+self.open_form(pid, 'sharing', 'Open trade sharing')+'</div></details>'
        body += '<p><a href="'+ROOT+'">Choose another job</a> · <a href="/workspace">My workspace</a></p>'
        return self.page('Job setup', body)

    def open(self, project_id:int, tool:str=Form(...)):
        destinations = {'blueprint': '/blueprint-brain', 'command': f'/workspace/command?project_id={project_id}',
                        'scopes': f'/workspace/scopes?project_id={project_id}', 'sharing': f'/workspace/sharing?project_id={project_id}'}
        self.require(tool in destinations, 'Choose an action on the job setup page.', 400)
        with self.db(True) as c:
            user, project = self.actor(c, project_id, True)
            sql = 'INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id'
            if self.field.postgres: sql += ' RETURNING user_id'
            c.execute(sql, (user['id'], project['id']))
        return RedirectResponse(destinations[tool], 303)

    def team(self, project_id:int):
        with self.db() as c:
            user, project = self.actor(c, project_id)
            table = self.ns['_bc850_member_table']()
            data = self.records(c, user, project_id)
            people = [dict(r) for r in c.execute('SELECT id,email,display_name,role FROM users WHERE company_id=? ORDER BY email', (user['company_id'],)).fetchall()]
        assigned = {p['id'] for p in data['members']}
        base = f'{ROOT}/projects/{project_id}'
        body = '<div class="hero"><div class="eyebrow">'+esc(project['name'])+'</div><h1>Who is on this job?</h1><p>Invite new people or assign an existing teammate. Their company role stays the same.</p></div>'
        if self.admin(user):
            body += '<div class="setup-actions">'+self.link(base+'/invite', 'Invite a new person')+'</div>'
        else:
            body += '<p class="setup-note">Your company administrator invites new accounts and appoints project leaders. You can assign existing company subcontractors here.</p>'
        body += '<section class="card"><h2>Assigned people</h2>'
        for p in data['members']:
            body += '<div class="setup-person"><strong>'+esc(p['display_name'] or p['email'])+'</strong><br>'+esc(p['email'])+' · '+esc(self.ns['_bc840_role_label'](p['role']))+'</div>'
            if getattr(self.ns['app'].state, 'pilot_readiness', None):
                body += self.link(base+'/access/'+str(p['id']), 'Explain this person’s access')
        if not data['members']: body += '<p>No people explicitly assigned yet. Company administrators retain their company access.</p>'
        body += '</section><section class="card"><h2>Assign an existing teammate</h2><p>Choose a named person. This adds access to this project only.</p>'
        candidates = [p for p in people if p['id'] not in assigned and (self.ns['_bc840_tier'](p) == 'trade' or (self.admin(user) and p['role'] in {'SUPERINTENDENT', 'PROJECT_MANAGER', 'COMPANY_ADMIN', 'ADMIN'}))]
        if candidates:
            body += f'<form method="post" action="{base}/assign"><label for="setup-person">Person and existing role</label><select name="user_id" id="setup-person" required><option value="" selected disabled>Choose a person</option>'
            for p in candidates:
                body += f'<option value="{p["id"]}">'+esc((p['display_name'] or p['email'])+' · '+p['email']+' · '+self.ns['_bc840_role_label'](p['role']))+'</option>'
            body += '</select><div class="setup-actions"><button>Add to this project</button></div></form>'
        else:
            body += '<p>No additional teammates are available to assign. '+('Invite a new person above.' if self.admin(user) else 'Ask your company administrator to invite the missing person.')+'</p>'
        body += '</section><section class="card"><h2>Waiting for acceptance</h2>'
        for p in data['pending']:
            body += '<div class="setup-person">'+esc(p['email'])+' · '+esc(self.ns['_bc840_role_label'](p['role']))+'<br><span class="small">Expires '+esc(str(p['expires_at'])[:10])+'</span></div>'
        if not data['pending']: body += '<p>No current invitations awaiting acceptance for this job.</p>'
        body += '</section><section class="card"><h2>What each role can do</h2><p><strong>Superintendent / project manager:</strong> run Command and review work for their assigned jobs.</p><p><strong>Subcontractor:</strong> open assigned jobs and respond to the work specifically shared with them.</p><p><strong>Company administrator:</strong> manage company people, projects and subscriptions.</p><p>Assigning someone does not send a notice, publish a scope or restore revoked work.</p>'
        if self.admin(user): body += '<details><summary>Change roles or remove access</summary><p><a href="/company/users">Open People &amp; access</a> to review the person’s full access before making changes.</p></details>'
        else: body += f'<p><a href="/workspace/sharing/projects/{project_id}/team">Manage or remove subcontractor assignments</a></p>'
        body += '</section>'+self.link(base, 'Back to job setup')
        return self.page('Project team', body)

    def assign(self, project_id:int, user_id:int=Form(...)):
        with self.db(True) as c:
            user, project = self.actor(c, project_id, True)
            person = c.execute('SELECT id,email,role,company_id FROM users WHERE id=? AND company_id=?'+self.field.lock, (user_id, user['company_id'])).fetchone()
            self.require(person is not None, 'Choose an existing person in your company.', 403)
            person = dict(person)
            allowed = self.ns['_bc840_tier'](person) == 'trade' or (self.admin(user) and person['role'] in {'SUPERINTENDENT', 'PROJECT_MANAGER', 'COMPANY_ADMIN', 'ADMIN'})
            self.require(allowed, 'Your company administrator manages project leaders and company roles.', 403)
            table = self.ns['_bc850_member_table']()
            if not c.execute(f'SELECT user_id FROM {table} WHERE project_id=? AND user_id=?', (project_id, user_id)).fetchone():
                self.ns['_bc830b_link_insert'](c, table, 'user_id,project_id', (user_id, project_id))
                self.ns['_bc850_event'](c, user, project_id, None, 'SETUP_ASSIGN:'+str(user_id))
        return RedirectResponse(f'{ROOT}/projects/{project_id}/team', 303)

    def invite(self, project_id:int):
        with self.db() as c:
            user, project = self.actor(c, project_id)
            self.require(self.admin(user), 'Your company administrator invites new accounts.', 403)
        base = f'{ROOT}/projects/{project_id}'
        body = '<div class="hero"><div class="eyebrow">'+esc(project['name'])+'</div><h1>Invite someone to this job</h1><p>Choose their role, create the invitation and copy the link to send to them.</p></div>'
        body += f'''<section class="card"><form method="post" action="/company/invitations/create">
            <input type="hidden" name="project_ids" value="{project_id}"><input type="hidden" name="setup_project_id" value="{project_id}">
            <label for="setup-email">Their email</label><input id="setup-email" type="email" name="email" autocomplete="email" maxlength="254" required>
            <label for="setup-role">What will they do?</label><select id="setup-role" name="role" required>{self.ns['_bc840_role_options']('SUBCONTRACTOR')}</select>
            <p><strong>Subcontractor:</strong> receives only work you share. <strong>Superintendent / project manager:</strong> runs this job and reviews field actions. <strong>Company administrator:</strong> manages people and subscriptions across the company.</p>
            <p>Project: <strong>{esc(project['name'])}</strong>. The invitation expires in 7 days. Email is not sent automatically.</p>
            <div class="setup-actions"><button>Create invitation link</button><a href="{base}/team">Back to the team</a></div>
            </form></section>'''
        return self.page('Invite to project', body)

    def health(self):
        checks = {f'{method} {path}': next((r.endpoint is endpoint for r in self.ns['app'].routes if getattr(r, 'path', '') == path and method in (getattr(r, 'methods', None) or set())), False) for method, path, endpoint in self.routes}
        checks.update(project_setup_installed=getattr(self.ns['app'].state, 'project_setup', None) is self,
                      reviewed_actions_preserved=getattr(self.ns['app'].state, 'command_actions', None) is not None,
                      ask_project_data_fix_preserved=all(self.ns['app'].state.command_center.data_health()['checks'].values()),
                      form_origin_guard_preserved=self.ns['_bc840_same_origin'] is self.ns['_bc861_same_origin'])
        try:
            with self.db() as c:
                c.execute('SELECT id,company_id,name,number FROM projects WHERE 1=0')
                c.execute('SELECT company_id,project_id,category,original_name FROM attachments WHERE 1=0')
                c.execute('SELECT id,company_id,project_id,status,created FROM blueprint_runs WHERE 1=0')
                c.execute('SELECT company_id,status,expires_at FROM bc_user_invitations WHERE 1=0')
                c.execute('SELECT invitation_id,project_id FROM bc_user_invitation_projects WHERE 1=0')
                c.execute(f'SELECT project_id,user_id FROM {self.ns["_bc850_member_table"]()} WHERE 1=0')
            checks['schema_readable'] = True
        except Exception:
            checks['schema_readable'] = False
        return {'app': 'BuildCommand AI', 'version': VERSION, 'release': RELEASE, 'status': 'ok' if all(checks.values()) else 'degraded', 'checks': checks,
                'passed': sum(checks.values()), 'total': len(checks), 'data_reset': False,
                'scope': 'Schema and active route checks only. Verify project creation, real uploads, invitation acceptance, appointed roles and a first-time GC walkthrough on staging.'}

    def register(self):
        for method, path, fn in [('GET', ROOT, self.index), ('GET', ROOT+'/new', self.new),
                                 ('GET', ROOT+'/projects/{project_id}', self.detail),
                                 ('POST', ROOT+'/projects/{project_id}/open', self.open),
                                 ('GET', ROOT+'/projects/{project_id}/team', self.team),
                                 ('POST', ROOT+'/projects/{project_id}/assign', self.assign),
                                 ('GET', ROOT+'/projects/{project_id}/invite', self.invite)]:
            endpoint = self.ns['_bc850_endpoint'](fn)
            self.ns['app'].add_api_route(path, endpoint, methods=[method])
            self.routes.append((method, path, endpoint))
        self.ns['app'].add_api_route('/health/first-job-setup-8-12-0', self.health, methods=['GET'])
        self.ns['_runtime'].PUBLIC_PATHS.add('/health/first-job-setup-8-12-0')
