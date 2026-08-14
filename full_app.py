from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
import sqlite3

try:
    import openpyxl
except Exception:
    openpyxl = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None
import os
import io
import csv
import json
import base64
import re
import secrets
import hashlib
import hmac
import mimetypes
import zipfile
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None

app=FastAPI(title="BuildCommand AI",version="32.0")
DB="construction_ai_web.db"
DEFAULT_UPLOAD_DIR="/var/data/buildcommand_uploads" if os.path.isdir("/var/data") else "/tmp/buildcommand_uploads"
UPLOAD_DIR=os.environ.get("UPLOAD_DIR",DEFAULT_UPLOAD_DIR)
os.makedirs(UPLOAD_DIR,exist_ok=True)
_current_user_id=ContextVar("buildcommand_user_id",default=None)
_current_company_id=ContextVar("buildcommand_company_id",default=None)
MAX_UPLOAD_BYTES=45*1024*1024
ALLOWED_UPLOAD_EXTENSIONS={".pdf",".png",".jpg",".jpeg",".webp",".doc",".docx",".xls",".xlsx",".csv",".txt"}

DATABASE_URL=os.environ.get("DATABASE_URL","").strip()
DATABASE_KIND="postgres" if DATABASE_URL.startswith(("postgres://","postgresql://")) else "sqlite"

class PgCompatConnection:
    def __init__(self,conn):
        self.conn=conn
        self.last_insert_id=None
    def _sql(self,sql):
        if sql.strip().lower().startswith("select last_insert_rowid()"):
            return None
        sql=sql.replace("?","%s")
        sql=re.sub(r"INSERT OR REPLACE INTO user_state\s*\(user_id,selected_project_id\)\s*VALUES\(%s,%s\)","INSERT INTO user_state(user_id,selected_project_id) VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=EXCLUDED.selected_project_id",sql,flags=re.I|re.S)
        sql=re.sub(r"INSERT OR REPLACE INTO app_state\s*\(id,selected_project_id\)\s*VALUES\(%s,%s\)","INSERT INTO app_state(id,selected_project_id) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET selected_project_id=EXCLUDED.selected_project_id",sql,flags=re.I|re.S)
        return sql
    def execute(self,sql,params=()):
        translated=self._sql(sql)
        if translated is None:
            class OneRow:
                def __init__(self,v): self.v=v
                def fetchone(self): return {"id":self.v}
                def fetchall(self): return [{"id":self.v}]
                def __iter__(self): return iter([{"id":self.v}])
            return OneRow(self.last_insert_id)
        cur=self.conn.cursor()
        m=re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)",translated,re.I)
        if m and "RETURNING" not in translated.upper() and m.group(1).lower() not in {"user_state"}:
            try:
                cur.execute(translated.rstrip().rstrip(";")+" RETURNING id",params)
                row=cur.fetchone()
                if row and "id" in row: self.last_insert_id=row["id"]
                return cur
            except Exception:
                self.conn.rollback(); cur=self.conn.cursor()
        cur.execute(translated,params)
        return cur
    def executescript(self,script):
        ddl=re.sub(r"\bid INTEGER PRIMARY KEY\b","id BIGSERIAL PRIMARY KEY",script,flags=re.I)
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]: self.execute(stmt)
        return self
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

def db():
    if DATABASE_KIND=="postgres":
        if psycopg is None: raise RuntimeError("PostgreSQL DATABASE_URL is set but psycopg is not installed")
        return PgCompatConnection(psycopg.connect(DATABASE_URL,row_factory=dict_row))
    conn=sqlite3.connect(DB)
    conn.row_factory=sqlite3.Row
    return conn

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY,name TEXT NOT NULL,logo_url TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,email TEXT NOT NULL UNIQUE,display_name TEXT,password_hash TEXT NOT NULL,role TEXT DEFAULT 'MEMBER',created TEXT);
    CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,token_hash TEXT NOT NULL UNIQUE,expires TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS user_state(user_id INTEGER PRIMARY KEY,selected_project_id INTEGER);
    CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER,category TEXT,title TEXT,original_name TEXT,stored_name TEXT,mime_type TEXT,size_bytes INTEGER,created_by INTEGER,created TEXT);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER,severity TEXT,title TEXT,detail TEXT,source TEXT,status TEXT DEFAULT 'UNREAD',created TEXT);
    CREATE TABLE IF NOT EXISTS morning_briefs(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,brief_date TEXT,brief_text TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS beta_feedback(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,user_id INTEGER,project_id INTEGER,rating INTEGER,category TEXT,feedback TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS user_favorites(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,user_id INTEGER NOT NULL,tool_name TEXT,tool_url TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS project_archive_state(project_id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,archived INTEGER DEFAULT 0,archived_at TEXT);
    CREATE TABLE IF NOT EXISTS attachment_tags(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,attachment_id INTEGER NOT NULL,tag TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS notification_rules(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,rule_name TEXT,enabled INTEGER DEFAULT 1,severity TEXT DEFAULT 'HIGH',threshold_value REAL DEFAULT 0,created TEXT);
    CREATE TABLE IF NOT EXISTS health_history(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,snapshot_date TEXT,overall REAL,schedule REAL,readiness REAL,procurement REAL,risk REAL,field REAL,created TEXT);
    CREATE TABLE IF NOT EXISTS admin_audit_log(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,user_id INTEGER,project_id INTEGER,action TEXT,detail TEXT,created TEXT);

    CREATE TABLE IF NOT EXISTS invitations(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,email TEXT NOT NULL,role TEXT DEFAULT 'FIELD_USER',token_hash TEXT NOT NULL UNIQUE,expires TEXT,accepted INTEGER DEFAULT 0,created_by INTEGER,created TEXT);
    CREATE TABLE IF NOT EXISTS company_settings(company_id INTEGER PRIMARY KEY,auto_ai_brief INTEGER DEFAULT 1,email_alerts INTEGER DEFAULT 0,beta_mode INTEGER DEFAULT 1,onboarding_complete INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS weekly_ai_reports(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,week_ending TEXT,report_text TEXT,created TEXT);
    
    CREATE TABLE IF NOT EXISTS weather_impacts(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,activity_id INTEGER,impact_date TEXT,weather_type TEXT,lost_hours REAL DEFAULT 0,description TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS schedule_import_batches(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,file_name TEXT,row_count INTEGER DEFAULT 0,imported_count INTEGER DEFAULT 0,created TEXT);
    CREATE TABLE IF NOT EXISTS photo_observations(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,attachment_id INTEGER,activity_id INTEGER,observation TEXT,severity TEXT DEFAULT 'WATCH',created TEXT);
    CREATE TABLE IF NOT EXISTS communication_drafts(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,sub_id INTEGER,draft_type TEXT,subject TEXT,body TEXT,status TEXT DEFAULT 'DRAFT',created TEXT);
    CREATE TABLE IF NOT EXISTS meeting_ai_summaries(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,source_text TEXT,summary_text TEXT,created TEXT);

    CREATE TABLE IF NOT EXISTS document_ai_chunks(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,attachment_id INTEGER NOT NULL,chunk_index INTEGER,text_content TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS blueprint_runs(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,status TEXT DEFAULT 'COMPLETE',source_files TEXT,project_summary TEXT,detected_disciplines TEXT,cross_discipline_flags TEXT,rfi_candidates TEXT,review_notes TEXT,model_name TEXT,created_by INTEGER,created TEXT);
    CREATE TABLE IF NOT EXISTS blueprint_trade_scopes(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,run_id INTEGER NOT NULL,trade TEXT NOT NULL,division TEXT,summary TEXT,scope_text TEXT,item_count INTEGER DEFAULT 0,created TEXT);
    CREATE TABLE IF NOT EXISTS blueprint_scope_items(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,run_id INTEGER NOT NULL,trade_scope_id INTEGER,trade TEXT NOT NULL,requirement TEXT NOT NULL,source_sheet TEXT,source_detail TEXT,source_spec TEXT,source_note TEXT,related_trade TEXT,confidence TEXT DEFAULT 'MEDIUM',item_type TEXT DEFAULT 'SCOPE',status TEXT DEFAULT 'NOT_STARTED',created TEXT);
    CREATE TABLE IF NOT EXISTS schedule_import_sources(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,source_type TEXT,file_name TEXT,imported_count INTEGER DEFAULT 0,created TEXT);
    CREATE TABLE IF NOT EXISTS forecast_snapshots(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,snapshot_date TEXT,health_score REAL,projected_delay_days REAL,confidence REAL,explanation TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS sub_risk_snapshots(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,sub_id INTEGER NOT NULL,risk_score REAL,risk_band TEXT,explanation TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS change_packages(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,change_event_id INTEGER,package_text TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS quick_entries(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,user_id INTEGER,entry_type TEXT,text TEXT,routed_to TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS daily_reports(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        report_date TEXT,
        weather TEXT,
        manpower INTEGER DEFAULT 0,
        work_completed TEXT,
        delays TEXT,
        deliveries TEXT,
        inspections TEXT,
        safety TEXT,
        tomorrow_plan TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS daily_report_analysis(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        report_id INTEGER,
        analysis_text TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS action_items(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        title TEXT,
        owner TEXT,
        priority TEXT,
        due TEXT,
        status TEXT DEFAULT 'OPEN',
        notes TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS activity_readiness(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        drawings INTEGER DEFAULT 0,
        material INTEGER DEFAULT 0,
        manpower INTEGER DEFAULT 0,
        predecessor INTEGER DEFAULT 0,
        access_ready INTEGER DEFAULT 0,
        inspection INTEGER DEFAULT 0,
        equipment INTEGER DEFAULT 0,
        notes TEXT,
        updated TEXT,
        UNIQUE(project_id, activity_id)
    );

    CREATE TABLE IF NOT EXISTS procurement(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        item TEXT,
        vendor TEXT,
        required_on_site TEXT,
        promised_date TEXT,
        status TEXT,
        notes TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS project_issues(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        issue_type TEXT,
        title TEXT,
        owner TEXT,
        due TEXT,
        priority TEXT,
        status TEXT DEFAULT 'OPEN',
        description TEXT,
        response TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS punch_items(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        title TEXT,
        location TEXT,
        trade TEXT,
        owner TEXT,
        priority TEXT,
        due TEXT,
        status TEXT DEFAULT 'OPEN',
        description TEXT,
        resolution TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS inspections_tracker(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        inspection_type TEXT,
        authority TEXT,
        scheduled_date TEXT,
        result TEXT DEFAULT 'PENDING',
        reinspection_date TEXT,
        notes TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS submittals(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        title TEXT,
        spec_section TEXT,
        responsible_party TEXT,
        sent_date TEXT,
        due_date TEXT,
        status TEXT DEFAULT 'PENDING',
        notes TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS safety_items(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        event_date TEXT,
        item_type TEXT,
        title TEXT,
        location TEXT,
        responsible_party TEXT,
        severity TEXT,
        status TEXT DEFAULT 'OPEN',
        description TEXT,
        corrective_action TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS change_events(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        activity_id INTEGER,
        event_type TEXT,
        title TEXT,
        responsible_party TEXT,
        estimated_cost REAL DEFAULT 0,
        schedule_days REAL DEFAULT 0,
        status TEXT DEFAULT 'OPEN',
        description TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS meeting_notes(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        meeting_date TEXT,
        meeting_type TEXT,
        title TEXT,
        attendees TEXT,
        decisions TEXT,
        commitments TEXT,
        follow_up TEXT,
        created TEXT
    );

    CREATE TABLE IF NOT EXISTS app_state(
        id INTEGER PRIMARY KEY,
        selected_project_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT,number TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY,project_id INTEGER,external_id TEXT,name TEXT,trade TEXT,start TEXT,finish TEXT,pct REAL DEFAULT 0,status TEXT DEFAULT 'NOT_STARTED');
    CREATE TABLE IF NOT EXISTS make_ready(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,title TEXT,reason TEXT,due TEXT,priority TEXT,status TEXT DEFAULT 'OPEN');
    CREATE TABLE IF NOT EXISTS risks(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,score REAL,band TEXT,explanation TEXT);
    CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,trade TEXT);

    CREATE TABLE IF NOT EXISTS subcontractor_updates(
        id INTEGER PRIMARY KEY,
        project_id INTEGER,
        sub_id INTEGER,
        update_date TEXT,
        manpower INTEGER DEFAULT 0,
        commitment TEXT,
        issue TEXT,
        status TEXT,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS production(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,work_date TEXT,crew INTEGER,qty REAL,planned_qty REAL,unit TEXT);
    CREATE TABLE IF NOT EXISTS field_updates(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,update_type TEXT,text TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY,project_id INTEGER,category TEXT,insight TEXT,confidence REAL);
    CREATE TABLE IF NOT EXISTS recovery(id INTEGER PRIMARY KEY,project_id INTEGER,activity_id INTEGER,scenario TEXT,days_recovered REAL,est_cost REAL,status TEXT);
    """)

    if c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"] == 0:
        c.execute(
            "INSERT INTO projects(name,number,status) VALUES(?,?,?)",
            ("Canyon Medical Office", "CMO-024", "ACTIVE")
        )
        pid = c.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]

        acts = [
            ("A100", "Footings & Foundations", "Concrete", "2026-08-10", "2026-08-18", 80, "IN_PROGRESS"),
            ("A200", "Structural Steel / Deck", "Structural", "2026-08-19", "2026-09-04", 15, "NOT_STARTED"),
            ("A300", "MEP Underground / Rough", "MEP", "2026-08-24", "2026-09-11", 5, "NOT_STARTED"),
            ("A400", "Interior Framing", "Framing", "2026-09-08", "2026-09-25", 0, "NOT_STARTED"),
            ("A500", "Drywall Close-In", "Drywall", "2026-09-22", "2026-10-09", 0, "NOT_STARTED"),
        ]
        for x in acts:
            c.execute(
                "INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,?,?)",
                (pid, *x)
            )

        ids = {
            r["external_id"]: r["id"]
            for r in c.execute(
                "SELECT id,external_id FROM activities WHERE project_id=?",
                (pid,)
            )
        }

        c.execute(
            "INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",
            (pid, ids["A300"], "Clear MEP rough-in access", "Material laydown is blocking east-side access.", "2026-08-12", "CRITICAL")
        )
        c.execute(
            "INSERT INTO make_ready(project_id,activity_id,title,reason,due,priority) VALUES(?,?,?,?,?,?)",
            (pid, ids["A200"], "Confirm steel delivery", "Fabricator delivery confirmation has not been received.", "2026-08-13", "HIGH")
        )

        for aid, score, band, why in [
            (ids["A300"], 82, "CRITICAL", "MEP rough-in is behind the field plan and access remains unresolved."),
            (ids["A200"], 68, "HIGH", "Steel delivery confirmation is unresolved; downstream starts are exposed."),
            (ids["A400"], 44, "WATCH", "Framing depends on MEP rough-in and inspection clearance."),
        ]:
            c.execute(
                "INSERT INTO risks(project_id,activity_id,score,band,explanation) VALUES(?,?,?,?,?)",
                (pid, aid, score, band, why)
            )

        for name, trade in [
            ("Apex Concrete", "Concrete"),
            ("Metro Steel", "Structural"),
            ("Summit MEP", "MEP"),
        ]:
            c.execute(
                "INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)",
                (pid, name, trade)
            )

        c.execute(
            "INSERT INTO memory(project_id,category,insight,confidence) VALUES(?,?,?,?)",
            (pid, "Company Memory", "Early access constraints on MEP rough-in should be cleared before manpower is increased.", .72)
        )

    if c.execute("SELECT COUNT(*) n FROM app_state").fetchone()["n"] == 0:
        first_project = c.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if first_project:
            c.execute(
                "INSERT INTO app_state(id, selected_project_id) VALUES(1, ?)",
                (first_project["id"],)
            )

    if DATABASE_KIND=="postgres":
        project_columns={row["column_name"] for row in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='projects'").fetchall()}
    else:
        project_columns={row["name"] for row in c.execute("PRAGMA table_info(projects)").fetchall()}
    if "company_id" not in project_columns:
        c.execute("ALTER TABLE projects ADD COLUMN company_id INTEGER")

    c.commit()
    c.close()


init()

def hash_password(password):
    salt = secrets.token_bytes(16)
    rounds = 210000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)).hex()
        return hmac.compare_digest(calc, digest_hex)
    except Exception:
        return False


def current_user_id():
    return _current_user_id.get()


def current_company_id():
    return _current_company_id.get()


def current_user():
    uid = current_user_id()
    if not uid:
        return None
    c = db()
    row = c.execute("""
        SELECT u.*, c.name AS company_name, c.logo_url
        FROM users u JOIN companies c ON c.id=u.company_id
        WHERE u.id=?
    """, (uid,)).fetchone()
    c.close()
    return row


ROLE_ORDER={"READ_ONLY":0,"FIELD_USER":1,"MEMBER":1,"SUPERINTENDENT":2,"PROJECT_MANAGER":3,"ADMIN":4,"OWNER":5}

def role_level(role):
    return ROLE_ORDER.get((role or "").upper(),0)

def require_role(min_role):
    u=current_user()
    return bool(u and role_level(u["role"])>=role_level(min_role))

def ensure_company_settings(company_id):
    if not company_id: return
    c=db()
    row=c.execute("SELECT company_id FROM company_settings WHERE company_id=?",(company_id,)).fetchone()
    if not row:
        c.execute("INSERT INTO company_settings(company_id,auto_ai_brief,email_alerts,beta_mode,onboarding_complete) VALUES(?,?,?,?,?)",(company_id,1,0,1,0))
        c.commit()
    c.close()

def company_setting(name,default=0):
    cid=current_company_id()
    if not cid: return default
    ensure_company_settings(cid)
    c=db(); row=c.execute("SELECT * FROM company_settings WHERE company_id=?",(cid,)).fetchone(); c.close()
    try: return row[name]
    except Exception: return default

def create_session(user_id):
    raw = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
    c = db()
    c.execute("INSERT INTO sessions(user_id,token_hash,expires,created) VALUES(?,?,?,?)",
              (user_id, token_hash, expires, datetime.utcnow().isoformat()))
    c.commit(); c.close()
    return raw


def user_from_session(raw_token):
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    c = db()
    row = c.execute("""
        SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND (s.expires IS NULL OR s.expires>?)
    """, (token_hash, datetime.utcnow().isoformat())).fetchone()
    c.close()
    return row


PUBLIC_PATHS = {"/login", "/register", "/health"}


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    raw_token = request.cookies.get("bc_session")
    user = user_from_session(raw_token)
    t1 = _current_user_id.set(user["id"] if user else None)
    t2 = _current_company_id.set(user["company_id"] if user else None)
    try:
        if not user and request.url.path not in PUBLIC_PATHS:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)
    finally:
        _current_user_id.reset(t1)
        _current_company_id.reset(t2)


def login_page(message=""):
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>BuildCommand AI Login</title>
    <style>body{{margin:0;background:#0a1017;color:#eef4fb;font-family:Inter,system-ui,sans-serif;padding:28px}}.box{{max-width:460px;margin:6vh auto;background:#111923;border:1px solid #213042;border-radius:16px;padding:26px}}input{{width:100%;box-sizing:border-box;background:#0d1620;color:#eef4fb;border:1px solid #213042;border-radius:9px;padding:12px;margin:8px 0 16px}}button{{background:#f0b44d;border:0;border-radius:9px;padding:11px 16px;font-weight:800}}a{{color:#f0b44d}}.muted{{color:#8fa2b5}}.error{{color:#ff9b9b}}</style></head><body>
    <div class="box"><div class="muted">Construction Intelligence Platform</div><h1>BuildCommand AI</h1><p class="error">{esc(message)}</p>
    <form method="post" action="/login"><label>Email</label><input type="email" name="email" required><label>Password</label><input type="password" name="password" required><button type="submit">Sign In</button></form>
    <p><a href="/register">Create a company account</a></p><p class="muted">Built by Wilson LaHood · © 2026 Wilson LaHood</p></div></body></html>'''


@app.get("/login", response_class=HTMLResponse)
def login_get():
    return login_page()


@app.post("/login", response_class=HTMLResponse)
def login_post(email: str = Form(...), password: str = Form(...)):
    c = db(); user = c.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone(); c.close()
    if not user or not verify_password(password, user["password_hash"]):
        return HTMLResponse(login_page("Email or password is incorrect."), status_code=401)
    raw = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("bc_session", raw, httponly=True, secure=os.environ.get("COOKIE_SECURE", "1") == "1", samesite="lax", max_age=2592000)
    return response


@app.get("/register", response_class=HTMLResponse)
def register_get():
    return '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create BuildCommand Account</title>
    <style>body{margin:0;background:#0a1017;color:#eef4fb;font-family:Inter,system-ui,sans-serif;padding:28px}.box{max-width:520px;margin:4vh auto;background:#111923;border:1px solid #213042;border-radius:16px;padding:26px}input{width:100%;box-sizing:border-box;background:#0d1620;color:#eef4fb;border:1px solid #213042;border-radius:9px;padding:12px;margin:8px 0 16px}button{background:#f0b44d;border:0;border-radius:9px;padding:11px 16px;font-weight:800}a{color:#f0b44d}.muted{color:#8fa2b5}</style></head><body>
    <div class="box"><div class="muted">BuildCommand AI</div><h1>Create Company Account</h1><form method="post" action="/register"><label>Company Name</label><input name="company_name" required><label>Your Name</label><input name="display_name" required><label>Email</label><input type="email" name="email" required><label>Password</label><input type="password" name="password" minlength="8" required><button type="submit">Create Account</button></form><p><a href="/login">Back to login</a></p></div></body></html>'''


@app.post("/register")
def register_post(company_name: str = Form(...), display_name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if len(password) < 8:
        return HTMLResponse("Password must be at least 8 characters.", status_code=400)
    c = db()
    if c.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone():
        c.close(); return HTMLResponse("That email is already registered.", status_code=400)
    c.execute("INSERT INTO companies(name,created) VALUES(?,?)", (company_name.strip(), date.today().isoformat()))
    company_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO users(company_id,email,display_name,password_hash,role,created) VALUES(?,?,?,?,?,?)",
              (company_id,email.strip().lower(),display_name.strip(),hash_password(password),"OWNER",date.today().isoformat()))
    user_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("UPDATE projects SET company_id=? WHERE company_id IS NULL", (company_id,))
    first_project = c.execute("SELECT id FROM projects WHERE company_id=? ORDER BY id LIMIT 1", (company_id,)).fetchone()
    if first_project:
        c.execute("INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id", (user_id, first_project["id"]))
    c.commit(); c.close()
    raw = create_session(user_id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("bc_session", raw, httponly=True, secure=os.environ.get("COOKIE_SECURE", "1") == "1", samesite="lax", max_age=2592000)
    return response


@app.post("/logout")
def logout(request: Request):
    raw = request.cookies.get("bc_session")
    if raw:
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest(); c = db(); c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,)); c.commit(); c.close()
    response = RedirectResponse("/login", status_code=303); response.delete_cookie("bc_session"); return response


@app.get("/health")
def health():
    return {"status":"ok","app":"BuildCommand AI","version":"31.1"}

@app.get("/projects/new", response_class=HTMLResponse)
def new_project_form():
    return """
    <html>
    <head>
        <title>Add Project</title>
        <style>
            body {
                background:#0a1017;
                color:white;
                font-family:Arial,sans-serif;
                padding:40px;
            }
            .box {
                max-width:500px;
                margin:auto;
                background:#111923;
                padding:30px;
                border-radius:15px;
            }
            input, select {
                width:100%;
                padding:12px;
                margin:8px 0 18px;
                box-sizing:border-box;
                border-radius:8px;
                border:1px solid #213042;
                background:#0d1620;
                color:white;
            }
            button {
                background:#f0b44d;
                border:none;
                padding:12px 20px;
                border-radius:8px;
                font-weight:bold;
                cursor:pointer;
            }
            a {
                color:#f0b44d;
            }
        </style>
    </head>

    <body>
        <div class="box">
            <h1>Add New Project</h1>

            <form method="post" action="/projects/new">

                <label>Project Name</label>
                <input
                    type="text"
                    name="name"
                    placeholder="Example: Phoenix Medical Center"
                    required
                >

                <label>Project Number</label>
                <input
                    type="text"
                    name="number"
                    placeholder="Example: PMC-001"
                    required
                >

                <label>Status</label>
                <select name="status">
                    <option value="ACTIVE">Active</option>
                    <option value="PLANNING">Planning</option>
                    <option value="ON_HOLD">On Hold</option>
                    <option value="COMPLETE">Complete</option>
                </select>

                <button type="submit">
                    Save Project
                </button>

            </form>

            <p>
                <a href="/">← Back to Dashboard</a>
            </p>
        </div>
    </body>
    </html>
    """


@app.post("/projects/new")
def create_project(name: str = Form(...), number: str = Form(...), status: str = Form(...)):
    c=db(); c.execute("INSERT INTO projects(name,number,status,company_id) VALUES(?,?,?,?)",(name.strip(),number.strip(),status,current_company_id())); pid=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]; c.execute("INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id",(current_user_id(),pid)); c.commit(); c.close(); return RedirectResponse(url="/",status_code=303)
CSS="""
:root{--bg:#0a1017;--panel:#111923;--line:#213042;--text:#eef4fb;--muted:#8fa2b5;--gold:#f0b44d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.side{background:#0c141d;border-right:1px solid var(--line);padding:22px 16px}.brand{font-size:20px;font-weight:800}.company{font-size:12px;color:var(--muted);margin:5px 0 20px}.nav a{display:block;color:#cbd7e3;text-decoration:none;padding:10px;border-radius:9px;margin:2px 0}.nav a:hover{background:#162333}.creator-footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:11px;line-height:1.6}.main{padding:26px;max-width:1400px}
.hero,.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:12px}.hero h1{margin:4px 0}.eyebrow{color:var(--gold);font-size:11px;text-transform:uppercase;letter-spacing:.13em}.muted,.small{color:var(--muted)}.small{font-size:12px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kpi{font-size:28px;font-weight:800}.label{font-size:11px;color:var(--muted);text-transform:uppercase}.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-weight:800;font-size:10px}.CRITICAL,.HOLD{background:#492324;color:#ff9b9b}.HIGH,.WATCH{background:#43381b;color:#ffd779}.READY,.LOW,.COMPLETE{background:#18392c;color:#82e4b5}.OPEN{background:#1d2e44;color:#99c9ff}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}input,textarea,select{width:100%;background:#0d1620;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px}textarea{min-height:90px}button{background:var(--gold);border:0;border-radius:9px;padding:10px 14px;font-weight:800}.action{padding:12px 0;border-bottom:1px solid var(--line)}
@media(max-width:850px){.app{grid-template-columns:1fr}.grid4,.grid3,.grid2{grid-template-columns:1fr}.main{padding:14px}}

.mobile-menu-btn{display:none}
@media(max-width:900px){.app{grid-template-columns:1fr}.side{position:relative;border-right:0;border-bottom:1px solid var(--line)}.grid4,.grid3,.grid2{grid-template-columns:1fr}.mobile-menu-btn{display:block;width:100%;margin:12px 0}.nav{display:none}.nav.mobile-open{display:block}}
"""

NAV=[("Daily Command","/"),("AI Command","/ai-command"),("Blueprint Brain","/plans-specs-ai"),("Deep Document AI","/document-ai"),("Schedule Import","/schedule-import"),("Advanced Schedule Import","/advanced-schedule-import"),("Photo Intelligence","/photo-intelligence"),("AI Photo Analysis","/photo-ai"),("3-Week Lookahead","/lookahead-intelligence"),("Project Health","/project-health"),("Predictive Forecast","/predictive-forecast"),("Morning Brief","/morning-brief"),("Action Center","/actions"),("RFIs / Issues","/issues"),("Punch List","/punch"),("Inspections","/inspections"),("Submittals","/submittals"),("Safety","/safety"),("Change Events","/changes"),("Meetings","/meetings"),("Documents","/documents"),("Notifications","/notifications"),("AI Assistant","/assistant"),("AI Analysis","/ai-analysis"),("Daily Report","/daily-report"),("Auto Daily Report","/auto-daily-report"),("Schedule","/schedule"),("Schedule Health","/schedule-health"),("Procurement","/procurement"),("Readiness","/readiness"),("Make Ready","/make-ready"),("Field","/field"),("Subcontractors","/subcontractors"),("Production","/production"),("Predictive Risk","/risk"),("Recovery","/recovery"),("Company Memory","/memory"),("Playbooks","/playbooks"),("Portfolio","/portfolio"),("Owner Dashboard","/owner-dashboard"),("Portfolio Intelligence","/portfolio-intelligence"),("Exports","/exports"),("Team","/team"),("Company Settings","/company-settings"),("Project Settings","/project-settings"),("System Check","/system-check"),("Beta Feedback","/beta-feedback"),("Setup","/setup"),("Invitations","/invitations"),("Production Settings","/production-settings"),("Sub Scorecards","/sub-scorecards"),("Sub Risk","/sub-risk"),("RFI Impact","/rfi-impact"),("Procurement Warning","/procurement-warning"),("Recovery Planner","/ai-recovery"),("Quick Entry","/quick-entry"),("Weekly AI Report","/weekly-report"),("RFI Drafting","/rfi-drafting"),("Sub Communications","/sub-communications"),("AI Meeting Minutes","/meeting-minutes-ai"),("Weather Impacts","/weather-impacts"),("PDF Reports","/pdf-reports"),("Cost Intelligence","/cost-intelligence"),("Change Package","/change-package"),("Mobile Home","/mobile-home"),("Mobile Field+","/mobile-field-plus"),("Beta Checklist","/beta-checklist")]


NAV_GROUPS=[
    ('🏠 Home',[
        ('BuildCommand Brain','/brain'),('Estimator Intelligence','/brain/estimator'),('Takeoff Intelligence','/brain/takeoff'),('Daily Command','/'),('AI Command','/ai-command'),('Morning Brief','/morning-brief'),('Action Center','/actions'),
        ('Global Search','/global-search'),('Favorites','/favorites'),('Recent Activity','/recent-activity'),('Mobile Field+','/mobile-field-plus')
    ]),
    ('📅 Schedule & Production',[
        ('Schedule','/schedule'),('Schedule Import','/schedule-import'),('Advanced Schedule Import','/advanced-schedule-import'),
        ('3-Week Lookahead','/lookahead-intelligence'),('Schedule Health','/schedule-health'),('Predictive Forecast','/predictive-forecast'),
        ('Production','/production'),('Readiness','/readiness'),('Make Ready','/make-ready'),('Procurement','/procurement'),
        ('Procurement Warning','/procurement-warning'),('Recovery','/recovery'),('Recovery Planner','/ai-recovery'),('Predictive Risk','/risk')
    ]),
    ('👷 Field Operations',[
        ('Field','/field'),('Quick Entry','/quick-entry'),('Daily Report','/daily-report'),('Auto Daily Report','/auto-daily-report'),
        ('Photo Intelligence','/photo-intelligence'),('AI Photo Analysis','/photo-ai'),('Weather Impacts','/weather-impacts'),
        ('Safety','/safety'),('Inspections','/inspections'),('Punch List','/punch')
    ]),
    ('📄 Documents & AI',[
        ('Documents','/documents'),('Document Tags','/document-tags'),('Plan Intake','/plans-specs-ai'),('Deep Document AI','/document-ai'),
        ('RFIs / Issues','/issues'),('RFI Drafting','/rfi-drafting'),('RFI Impact','/rfi-impact'),('Submittals','/submittals'),
        ('Meetings','/meetings'),('AI Meeting Minutes','/meeting-minutes-ai'),('AI Assistant','/assistant'),('AI Analysis','/ai-analysis'),
        ('Weekly AI Report','/weekly-report'),('PDF Reports','/pdf-reports')
    ]),
    ('🤝 Subs & Communication',[
        ('Subcontractors','/subcontractors'),('Sub Scorecards','/sub-scorecards'),('Sub Risk','/sub-risk'),
        ('Sub Communications','/sub-communications'),('Notifications','/notifications'),('Notification Rules','/notification-rules')
    ]),
    ('💰 Management & Executive',[
        ('Project Health','/project-health'),('Health History','/health-history'),('Change Events','/changes'),('Cost Intelligence','/cost-intelligence'),
        ('Change Package','/change-package'),('Owner Dashboard','/owner-dashboard'),('Portfolio','/portfolio'),('Portfolio Intelligence','/portfolio-intelligence'),
        ('Company Memory','/memory'),('Playbooks','/playbooks'),('Bulk Export','/bulk-export')
    ]),
    ('⚙️ Admin',[
        ('Team','/team'),('Invitations','/invitations'),('Company Settings','/company-settings'),('Project Settings','/project-settings'),
        ('Clone Project','/project-clone'),('Archive Projects','/project-archive'),('Storage Status','/storage-status'),('Audit Log','/audit-log'),
        ('Setup','/setup'),('System Check','/system-check'),('Beta Feedback','/beta-feedback'),('Beta Checklist','/beta-checklist')
    ])
]

def _v37_esc(value):
    import html
    return html.escape(str(value or ""), quote=True)

# Global compatibility helper used by legacy BuildCommand pages.
def esc(value):
    return _v37_esc(value)



# ============================================================

def categorized_nav():
    items=[
        ("Projects","/"),
        ("Build","/build"),
        ("Estimate","/estimate"),
        ("Manage","/manage"),
        ("Ask BuildCommand","/ask-buildcommand"),
    ]
    return "".join(
        f'<a href="{href}" style="display:block;padding:12px 10px;margin:4px 0;border-radius:9px;">{_v37_esc(label)}</a>'
        for label,href in items
    )

def shell(title, body):
    current_pid = project_id()
    company_id = current_company_id()
    user = current_user()
    c = db()
    projects = c.execute("SELECT p.id,p.name,p.number,p.status FROM projects p LEFT JOIN project_archive_state a ON a.project_id=p.id WHERE p.company_id=? AND COALESCE(a.archived,0)=0 ORDER BY p.name", (company_id,)).fetchall()
    current = c.execute("SELECT * FROM projects WHERE id=? AND company_id=?", (current_pid, company_id)).fetchone() if current_pid else None
    c.close()
    nav = categorized_nav()
    project_options = "".join(f'<option value="{p["id"]}" {"selected" if p["id"]==current_pid else ""}>{_v37_esc(p["number"])} - {_v37_esc(p["name"])}</option>' for p in projects)
    current_name = _v37_esc(current["name"]) if current else "No Project Selected"
    company_name = _v37_esc(user["company_name"]) if user else "BuildCommand Company"
    display_name = _v37_esc(user["display_name"]) if user else ""
    selector = f'''<div style="margin-bottom:20px;"><div class="small" style="margin-bottom:6px;">CURRENT PROJECT</div><form method="post" action="/projects/select"><select name="project_id" style="margin-bottom:8px;">{project_options}</select><button type="submit" style="width:100%;">Switch Project</button></form><div style="margin-top:10px;"><a href="/projects/new" style="color:#f0b44d;text-decoration:none;font-weight:700;">+ Add Project</a></div></div>'''
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_v37_esc(title)} · BuildCommand AI</title><style>{CSS}
.nav-group{{border-bottom:1px solid rgba(255,255,255,.08);padding:2px 0}}.nav-group summary{{cursor:pointer;padding:11px 10px;font-weight:800;color:#f4f4f4;list-style:none;border-radius:8px}}.nav-group summary::-webkit-details-marker{{display:none}}.nav-group summary:after{{content:"▾";float:right;opacity:.7}}.nav-group[open] summary:after{{content:"▴"}}.nav-items{{padding:0 0 8px 8px}}.nav-items a{{display:block;padding:8px 10px;font-size:13px}}.search-result{{padding:12px 0;border-bottom:1px solid var(--line)}}
</style></head><body><div class="app"><aside class="side"><div class="brand">BuildCommand AI</div><div class="company">{company_name}<br>{current_name}</div>{selector}<button type="button" class="mobile-menu-btn" onclick="document.getElementById('bcnav').classList.toggle('mobile-open')">☰ Menu</button><nav class="nav" id="bcnav">{nav}</nav><div class="creator-footer">{display_name}<br>Built by Wilson LaHood<br>© 2026 Wilson LaHood<form method="post" action="/logout" style="margin-top:10px;"><button type="submit" style="width:100%;">Sign Out</button></form></div></aside><main class="main">{body}</main></div></body></html>'''

def project_id():
    user_id = current_user_id(); company_id = current_company_id()
    if not user_id or not company_id:
        return None
    c = db()
    state = c.execute("""SELECT us.selected_project_id FROM user_state us JOIN projects p ON p.id=us.selected_project_id WHERE us.user_id=? AND p.company_id=?""", (user_id, company_id)).fetchone()
    if state and state["selected_project_id"]:
        pid = state["selected_project_id"]
    else:
        first = c.execute("SELECT p.id FROM projects p LEFT JOIN project_archive_state a ON a.project_id=p.id WHERE p.company_id=? AND COALESCE(a.archived,0)=0 ORDER BY p.id LIMIT 1", (company_id,)).fetchone()
        pid = first["id"] if first else None
        if pid:
            c.execute("INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id", (user_id,pid)); c.commit()
    c.close(); return pid


@app.post("/projects/select")
def select_project(project_id: int = Form(...)):
    company_id = current_company_id(); user_id = current_user_id(); c = db()
    exists = c.execute("SELECT id FROM projects WHERE id=? AND company_id=?", (project_id, company_id)).fetchone()
    if exists:
        c.execute("INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id", (user_id,project_id)); c.commit()
    c.close(); return RedirectResponse("/", status_code=303)


# ============================================================
# v37.2 UNIFIED BUILDCOMMAND INTERFACE FIX
# ============================================================


def _v37_count(sql,args=()):
    """
    PostgreSQL/SQLite-safe scalar counter for unified dashboard.
    Always aliases COUNT(*) as n so dict_row and sqlite Row behave the same.
    """
    c=db()
    try:
        r=c.execute(sql,args).fetchone()
        value=int((r["n"] if r and "n" in r.keys() else 0) or 0)
        c.close()
        return value
    except Exception:
        try: c.rollback()
        except Exception: pass
        try: c.close()
        except Exception: pass
        return 0

def _v37_snapshot(pid):
    if not pid:
        return dict(scope=0,estimate=0,review=0,issues=0,submittals=0,actions=0,inspections=0)
    co=current_company_id()
    return {
        "scope":_v37_count(
            "SELECT COUNT(*) AS n FROM blueprint_scope_items WHERE company_id=? AND project_id=?",
            (co,pid)
        ),
        "estimate":_v37_count(
            "SELECT COUNT(*) AS n FROM estimator_items WHERE company_id=? AND project_id=?",
            (co,pid)
        ),
        "review":_v37_count(
            "SELECT COUNT(*) AS n FROM estimator_items WHERE company_id=? AND project_id=? AND COALESCE(verified,0)=0",
            (co,pid)
        ),
        "issues":_v37_count(
            "SELECT COUNT(*) AS n FROM project_issues WHERE project_id=? AND COALESCE(status,'OPEN')!='CLOSED'",
            (pid,)
        ),
        "submittals":_v37_count(
            "SELECT COUNT(*) AS n FROM submittals WHERE project_id=? AND COALESCE(status,'PENDING') NOT IN ('APPROVED','APPROVED_AS_NOTED','CLOSED','COMPLETE')",
            (pid,)
        ),
        "actions":_v37_count(
            "SELECT COUNT(*) AS n FROM action_items WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('CLOSED','COMPLETE')",
            (pid,)
        ),
        "inspections":_v37_count(
            "SELECT COUNT(*) AS n FROM inspections_tracker WHERE project_id=? AND COALESCE(result,'PENDING')!='PASSED'",
            (pid,)
        )
    }

def _v37_link_card(title,desc,href,label="Open"):
    return ('<div class="card"><h2>'+_v37_esc(title)+'</h2><p class="muted">'+_v37_esc(desc)+'</p>'
            '<a href="'+href+'" style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:10px 14px;border-radius:9px;font-weight:800;">'
            +_v37_esc(label)+' →</a></div>')

@app.get("/",response_class=HTMLResponse)
def unified_projects_home():
    pid=project_id(); s=_v37_snapshot(pid)
    attention=s["issues"]+s["submittals"]+s["actions"]+s["inspections"]
    c=db(); current=c.execute("SELECT * FROM projects WHERE id=? AND company_id=?",(pid,current_company_id())).fetchone() if pid else None; c.close()
    name=_v37_esc(current["name"]) if current else "Select or create a project"
    body=(
      '<div class="hero"><div class="eyebrow">BuildCommand AI · Construction Operations Intelligence System</div><h1>'+name+'</h1>'
      '<p class="muted">What is happening? What needs attention? What should happen next?</p></div>'
      '<div class="grid4">'
      f'<div class="card"><div class="label">Things That Need You</div><div class="kpi">{attention}</div><a href="/actions">Review →</a></div>'
      f'<div class="card"><div class="label">Scope Items</div><div class="kpi">{s["scope"]}</div><a href="/build">Build →</a></div>'
      f'<div class="card"><div class="label">Estimate Review</div><div class="kpi">{s["review"]}</div><a href="/estimate">Estimate →</a></div>'
      f'<div class="card"><div class="label">Open Issues</div><div class="kpi">{s["issues"]}</div><a href="/manage">Manage →</a></div></div>'
      '<div class="grid3">'
      +_v37_link_card("BUILD","Plans, specifications, project scope and construction intelligence.","/build")
      +_v37_link_card("ESTIMATE","Takeoff, estimator review, pricing and cost intelligence.","/estimate")
      +_v37_link_card("MANAGE","Schedule, field, RFIs, submittals, inspections and subcontractors.","/manage")
      +'</div>'
      +_v37_link_card("Ask BuildCommand","Do not hunt through menus. Ask the project what you need.","/ask-buildcommand","Ask")
      +'<details class="card"><summary><b>Advanced tools</b></summary><p class="muted">Nothing was removed.</p><p><a href="/legacy-dashboard">Open legacy dashboard →</a></p></details>'
    )
    return shell("Projects",body)

# ============================================================
# v38 UNIFIED PROJECT INTELLIGENCE PIPELINE
# ============================================================

def _v38_selected_docs(pid, attachment_ids):
    ids=list(dict.fromkeys(attachment_ids or []))
    if not ids: return []
    c=db(); docs=[]
    for aid in ids:
        d=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?",(aid,current_company_id(),pid)).fetchone()
        if d: docs.append(d)
    c.close(); return docs

def _v38_run_blueprint(pid, docs, focus=""):
    if not os.environ.get("OPENAI_API_KEY"): raise RuntimeError("OPENAI_API_KEY is not configured.")
    if not docs: raise RuntimeError("No project documents were selected.")
    total=sum(int(d["size_bytes"] or 0) for d in docs)
    if total>=50*1024*1024: raise RuntimeError(f"Selected files total {total/1024/1024:.1f} MB. Keep each Analyze Project batch under 50 MB.")
    model=os.environ.get("OPENAI_MODEL","gpt-5.6")
    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    uploaded=[]; input_content=[]; text_fallback=[]
    try:
        for d in docs:
            path=os.path.join(UPLOAD_DIR,d["stored_name"])
            if not os.path.isfile(path): continue
            ext=Path(d["original_name"] or "").suffix.lower()
            if ext==".pdf":
                with open(path,"rb") as fh:
                    remote=client.files.create(file=fh,purpose="user_data")
                uploaded.append(remote.id); input_content.append({"type":"input_file","file_id":remote.id,"detail":"high"})
            else:
                extracted=_attachment_text(d)
                if extracted: text_fallback.append(f"\n--- FILE: {d['original_name']} ---\n{extracted[:120000]}")
        if not input_content and not text_fallback: raise RuntimeError("No readable selected files were available on the server.")
        names="\n".join(f"- {d['original_name']}" for d in docs)
        prompt=_blueprint_prompt(names)
        if focus.strip(): prompt += "\n\nGC ANALYSIS FOCUS:\n"+focus.strip()
        if text_fallback: prompt += "\n\nEXTRACTED NON-PDF DOCUMENT CONTENT:\n"+"\n".join(text_fallback)
        input_content.append({"type":"input_text","text":prompt})
        response=client.responses.create(model=model,input=[{"role":"user","content":input_content}])
        data=_blueprint_json(response.output_text)
        if not isinstance(data.get("trade_scopes"),list): raise RuntimeError("Plan Intelligence response did not contain trade scopes.")
        run_id=_save_blueprint_result(pid,docs,data,model)
        return {"run_id":run_id,"trades":len(data.get("trade_scopes") or [])}
    finally:
        for fid in uploaded:
            try: client.files.delete(fid)
            except Exception: pass

def _v38_run_component_split(pid):
    _seed_estimator_from_latest(pid); _ensure_takeoff_component_tables()
    run=_latest_blueprint_run(pid)
    if not run: return {"created":0,"skipped":"No plan intelligence run"}
    c=db()
    rows=c.execute("SELECT e.* FROM estimator_items e JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id WHERE e.company_id=? AND e.project_id=? AND b.run_id=? ORDER BY e.trade,e.id LIMIT 180",(current_company_id(),pid,run["id"])).fetchall()
    c.close()
    if not rows or not os.environ.get("OPENAI_API_KEY"): return {"created":0,"skipped":"No estimator items or AI key"}
    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),input=_component_split_prompt(rows))
    data=_v36_parse_json(resp.output_text); valid={int(r["id"]):r for r in rows}; now=datetime.utcnow().isoformat()
    c=db(); created=0
    for result in data.get("results") or []:
        try: eid=int(result.get("estimator_id"))
        except Exception: continue
        if eid not in valid: continue
        comps=result.get("components") or []
        if len(comps)<=1: continue
        c.execute("DELETE FROM takeoff_components WHERE company_id=? AND project_id=? AND estimator_item_id=? AND status='PROPOSED'",(current_company_id(),pid,eid))
        for comp in comps[:25]:
            name=str(comp.get("name") or "").strip(); desc=str(comp.get("description") or name).strip(); unit=str(comp.get("unit") or "").upper()
            if not name or unit not in {"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}: continue
            source=str(comp.get("source") or valid[eid]["source_ref"] or "")
            c.execute("INSERT INTO takeoff_components(company_id,project_id,estimator_item_id,component_name,description,unit,quantity,confidence,basis,source_ref,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(current_company_id(),pid,eid,name,desc,unit,None,"VERIFY","Component split from cleaned parent scope; quantity not yet verified.",source,"PROPOSED",now,now))
            created+=1
    c.commit(); c.close(); return {"created":created}

def _v38_run_auto_takeoff(pid):
    _seed_estimator_from_latest(pid)
    run,docs=_v36_latest_plan_docs(pid)
    if not run or not docs or not os.environ.get("OPENAI_API_KEY"): return {"reviewed":0,"proposed":0,"verify":0,"skipped":"No run, docs, or AI key"}
    targets=_v36_scope_targets(pid,run["id"])
    if not targets: return {"reviewed":0,"proposed":0,"verify":0,"skipped":"No estimator targets"}
    total=sum(int(d["size_bytes"] or 0) for d in docs if Path(d["original_name"] or "").suffix.lower()==".pdf")
    if total>=50*1024*1024: return {"reviewed":0,"proposed":0,"verify":0,"skipped":"Plan set over 50 MB"}
    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"]); uploaded=[]; content=[]
    try:
        for d in docs:
            path=os.path.join(UPLOAD_DIR,d["stored_name"])
            if not os.path.isfile(path): continue
            if Path(d["original_name"] or "").suffix.lower()==".pdf":
                with open(path,"rb") as fh: remote=client.files.create(file=fh,purpose="user_data")
                uploaded.append(remote.id); content.append({"type":"input_file","file_id":remote.id,"detail":"high"})
        if not content: return {"reviewed":0,"proposed":0,"verify":0,"skipped":"No PDF plan files"}
        content.append({"type":"input_text","text":_v36_takeoff_prompt(targets)})
        response=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),input=[{"role":"user","content":content}])
        data=_v36_parse_json(response.output_text); results=data.get("results") or []; valid_ids={int(r["estimator_id"]) for r in targets}; now=datetime.utcnow().isoformat()
        c=db(); saved=0; proposed=0; verify=0
        for item in results:
            try: eid=int(item.get("estimator_id"))
            except Exception: continue
            if eid not in valid_ids: continue
            q=item.get("quantity")
            try: q=float(q) if q is not None else None
            except Exception: q=None
            unit=str(item.get("unit") or "").upper().strip()
            if unit not in {"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}: unit=""
            confidence=str(item.get("confidence") or "VERIFY").upper()
            if confidence not in {"HIGH","MEDIUM","LOW","VERIFY"}: confidence="VERIFY"
            basis=str(item.get("basis") or "")[:4000]; source=str(item.get("source") or "")[:1500]
            c.execute("UPDATE estimator_items SET ai_quantity=?,ai_unit=?,ai_confidence=?,ai_basis=?,ai_source=?,ai_updated=? WHERE id=? AND company_id=? AND project_id=?",(q,unit,confidence,basis,source,now,eid,current_company_id(),pid))
            saved+=1
            if q is not None and confidence in {"HIGH","MEDIUM"}: proposed+=1
            else: verify+=1
        c.commit(); c.close(); return {"reviewed":saved,"proposed":proposed,"verify":verify}
    finally:
        for fid in uploaded:
            try: client.files.delete(fid)
            except Exception: pass

@app.get("/build/analyze-project",response_class=HTMLResponse)
def unified_analyze_project_page():
    pid=project_id(); c=db()
    docs=c.execute("SELECT * FROM attachments WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),pid)).fetchall(); c.close()
    eligible=[d for d in docs if Path(d["original_name"] or "").suffix.lower() in {".pdf",".txt",".csv",".xlsx",".xlsm"}]
    checks="".join(f'<label style="display:flex;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.08)"><input type="checkbox" name="attachment_ids" value="{d["id"]}" style="width:auto"><span><b>{esc(d["original_name"])}</b><br><span class="small">{(int(d["size_bytes"] or 0)/1024/1024):.1f} MB</span></span></label>' for d in eligible) or '<div class="muted">No supported project documents are uploaded yet.</div>'
    body='<div class="hero"><div class="eyebrow">BuildCommand · Unified Project Intelligence · v38</div><h1>Analyze the project once.</h1><p class="muted">BuildCommand runs Plan Intelligence, trade scope cleanup, estimator sync, takeoff splitting and quantity review.</p></div>'
    body+='<div class="card"><form method="post" action="/build/analyze-project"><h2>Select project documents</h2>'+checks+'<label style="margin-top:16px">Optional analysis focus</label><textarea name="focus" placeholder="Example: Full bid/scope review"></textarea><button type="submit">Analyze Project</button></form><p class="small">Selected files must total less than 50 MB. AI quantity proposals never overwrite estimator-entered quantities.</p></div>'
    return shell("Analyze Project",body)

@app.post("/build/analyze-project",response_class=HTMLResponse)
def unified_analyze_project_run(attachment_ids:list[int] | None=Form(None),focus:str=Form("")):
    pid=project_id(); docs=_v38_selected_docs(pid,attachment_ids)
    if not docs: return shell("Analyze Project",'<div class="card"><h2>Select at least one project document.</h2><p><a href="/build/analyze-project">← Back</a></p></div>')
    stages=[]
    try:
        bp=_v38_run_blueprint(pid,docs,focus); stages.append(("Plan Intelligence","COMPLETE",f'{bp["trades"]} trade scopes generated'))
        est=_seed_estimator_from_latest(pid); stages.append(("Estimator Sync","COMPLETE",f'{est["added"]} new · {est["updated"]} refreshed'))
        comp=_v38_run_component_split(pid)
        stages.append(("Takeoff Component Split","REVIEW" if comp.get("skipped") else "COMPLETE",comp.get("skipped") or f'{comp["created"]} measurable components created'))
        auto=_v38_run_auto_takeoff(pid)
        stages.append(("Automatic Quantity Review","REVIEW" if auto.get("skipped") else "COMPLETE",auto.get("skipped") or f'{auto["proposed"]} quantity proposals · {auto["verify"]} need verification'))
        rows="".join(f'<div class="action"><span class="badge {"READY" if status=="COMPLETE" else "WATCH"}">{status}</span> <b>{esc(name)}</b><div class="small">{esc(detail)}</div></div>' for name,status,detail in stages)
        body='<div class="hero"><div class="eyebrow">Unified Project Intelligence · v38</div><h1>Project intelligence ready.</h1><p class="muted">The brains ran as one pipeline. Review only the items that need human judgment.</p></div><div class="card"><h2>Pipeline Results</h2>'+rows+'</div><div class="grid3">'
        body+=_v37_link_card("Review BUILD","Review cleaned trade scope and project intelligence.","/build","Open BUILD")+_v37_link_card("Review ESTIMATE","Review takeoff proposals and estimator data.","/estimate","Open ESTIMATE")+_v37_link_card("Things That Need You","Review human-decision items.","/actions","Review")+'</div>'
        return shell("Project Intelligence Ready",body)
    except Exception as exc:
        rows="".join(f'<div class="action"><span class="badge READY">{status}</span> <b>{esc(name)}</b><div class="small">{esc(detail)}</div></div>' for name,status,detail in stages)
        body='<div class="hero"><h1>Analyze Project stopped.</h1></div><div class="card"><p>'+esc(str(exc))+'</p></div>'
        if rows: body+='<div class="card"><h2>Completed before stop</h2>'+rows+'</div>'
        body+='<div class="card"><p><a href="/build/analyze-project">← Back to Analyze Project</a></p></div>'
        return shell("Analyze Project",body)


# ============================================================
# v39 NEXT 10 CONSTRUCTION INTELLIGENCE
# ============================================================

def _v39_rows(sql,args=()):
    c=db()
    try:
        return c.execute(sql,args).fetchall()
    finally:
        c.close()

def _v39_safe_date(v):
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except Exception:
        return None

def _v39_priority(due,priority=""):
    p=str(priority or "").upper()
    d=_v39_safe_date(due)
    today=datetime.utcnow().date()
    if p in {"CRITICAL","URGENT"} or (d and d<today): return "CRITICAL"
    if d==today or p=="HIGH": return "TODAY"
    if d and 0 < (d-today).days <= 7: return "THIS WEEK"
    return "REVIEW"

def _v39_attention(pid):
    items=[]
    for r in _v39_rows("SELECT * FROM action_items WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('CLOSED','COMPLETE')",(pid,)):
        items.append((_v39_priority(r["due"],r["priority"]),"ACTION",r["title"],r["due"] or "",r["owner"] or ""))
    for r in _v39_rows("SELECT * FROM project_issues WHERE project_id=? AND COALESCE(status,'OPEN')!='CLOSED'",(pid,)):
        items.append((_v39_priority(r["due"],r["priority"]),"ISSUE",r["title"],r["due"] or "",r["owner"] or ""))
    for r in _v39_rows("SELECT * FROM submittals WHERE project_id=? AND COALESCE(status,'PENDING') NOT IN ('APPROVED','CLOSED','COMPLETE')",(pid,)):
        items.append((_v39_priority(r["due_date"],"HIGH" if r["due_date"] else ""),"SUBMITTAL",r["title"],r["due_date"] or "",r["responsible_party"] or ""))
    for r in _v39_rows("SELECT * FROM inspections_tracker WHERE project_id=? AND COALESCE(result,'PENDING')!='PASSED'",(pid,)):
        items.append((_v39_priority(r["scheduled_date"],"HIGH" if r["scheduled_date"] else ""),"INSPECTION",r["inspection_type"],r["scheduled_date"] or "",r["authority"] or ""))
    for r in _v39_rows("SELECT * FROM estimator_items WHERE project_id=? AND COALESCE(verified,0)=0 AND COALESCE(ai_confidence,'') IN ('LOW','VERIFY')",(pid,)):
        items.append(("REVIEW","ESTIMATE",r["description"],"",r["trade"] or ""))
    rank={"CRITICAL":0,"TODAY":1,"THIS WEEK":2,"REVIEW":3,"FYI":4}
    return sorted(items,key=lambda x:rank.get(x[0],9))

def _v39_memory_rules(pid):
    return _v39_rows("SELECT trade,requirement,confidence FROM blueprint_scope_items WHERE project_id=? AND COALESCE(confidence,'')='HIGH' ORDER BY id DESC LIMIT 100",(pid,))

def _v39_conflicts(pid):
    return _v39_rows("SELECT requirement,COUNT(DISTINCT trade) AS n,MIN(trade) AS trade FROM blueprint_scope_items WHERE project_id=? GROUP BY requirement HAVING COUNT(DISTINCT trade)>1 ORDER BY n DESC LIMIT 50",(pid,))

def _v39_schedule_risks(pid):
    today=datetime.utcnow().date()
    rows=_v39_rows("SELECT * FROM activities WHERE project_id=? AND COALESCE(status,'NOT_STARTED')!='COMPLETE' ORDER BY start LIMIT 100",(pid,))
    out=[]
    for r in rows:
        s=_v39_safe_date(r["start"]); f=_v39_safe_date(r["finish"]); pct=float(r["pct"] or 0)
        if f and f<today and pct<100: out.append(("CRITICAL",r["name"],"Past finish date",r["finish"]))
        elif s and 0 <= (s-today).days <= 14 and pct==0: out.append(("THIS WEEK",r["name"],"Upcoming / make-ready",r["start"]))
    return out

def _v39_procurement(pid):
    return _v39_rows("SELECT * FROM procurement WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('DELIVERED','COMPLETE','CLOSED') ORDER BY required_on_site LIMIT 100",(pid,))

def _v39_changes(pid):
    return _v39_rows("SELECT * FROM change_events WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('CLOSED','COMPLETE') ORDER BY id DESC LIMIT 100",(pid,))

@app.get("/intelligence",response_class=HTMLResponse)
def v39_intelligence_center():
    pid=project_id(); attention=_v39_attention(pid); conflicts=_v39_conflicts(pid); sched=_v39_schedule_risks(pid); proc=_v39_procurement(pid); changes=_v39_changes(pid)
    cards=[
        ("Things That Need You",len(attention),"/intelligence/attention","Prioritized decisions across the job."),
        ("Project Memory",len(_v39_memory_rules(pid)),"/intelligence/memory","High-confidence construction knowledge retained from this project."),
        ("Conflict Brain",len(conflicts),"/intelligence/conflicts","Cross-scope conflicts that deserve review."),
        ("RFI Intelligence",len(conflicts),"/intelligence/rfis","Draft RFIs from detected conflicts."),
        ("Schedule Intelligence",len(sched),"/intelligence/schedule","Late and upcoming work requiring make-ready."),
        ("Look-Ahead Brain",len(sched),"/intelligence/lookahead","2, 3 and 6 week field outlook."),
        ("Procurement Brain",len(proc),"/intelligence/procurement","Long-lead and required-on-site tracking."),
        ("Bid Scope Brain",_v37_snapshot(pid)["scope"],"/intelligence/bids","Trade scope package intelligence."),
        ("Cost & Change",len(changes),"/intelligence/changes","Open cost and schedule exposure."),
        ("Daily Superintendent Command",len(attention)+len(sched),"/intelligence/daily-command","Today's project command brief.")
    ]
    body='<div class="hero"><div class="eyebrow">BuildCommand v40</div><h1>Construction Intelligence Center</h1><p class="muted">Ten brains. One project operating system. The main menu stays simple.</p></div><div class="grid3">'
    for name,count,href,desc in cards:
        body+=f'<div class="card"><div class="label">{esc(name)}</div><div class="kpi">{count}</div><p class="muted">{esc(desc)}</p><a href="{href}">Open →</a></div>'
    body+='</div>'
    return shell("Intelligence",body)

@app.get("/intelligence/attention",response_class=HTMLResponse)
def v39_attention_page():
    items=_v39_attention(project_id())
    rows="".join(f'<div class="action"><span class="badge">{esc(level)}</span> <b>{esc(kind)}</b> — {esc(title)}<div class="small">{esc(due)} {esc(owner)}</div></div>' for level,kind,title,due,owner in items)
    return shell("Things That Need You",'<div class="hero"><h1>Things That Need You</h1><p class="muted">Critical → Today → This Week → Review.</p></div><div class="card">'+(rows or '<p class="muted">Nothing needs attention.</p>')+'</div>')

@app.get("/intelligence/memory",response_class=HTMLResponse)
def v39_memory_page():
    rows=_v39_memory_rules(project_id())
    h="".join(f'<div class="action"><b>{esc(r["trade"])}</b><div>{esc(r["requirement"])}</div><span class="small">Confidence: {esc(r["confidence"])}</span></div>' for r in rows)
    return shell("Project Memory",'<div class="hero"><h1>Project Memory Brain</h1><p class="muted">High-confidence project construction knowledge retained for future reasoning.</p></div><div class="card">'+(h or '<p class="muted">Memory grows as project intelligence is reviewed.</p>')+'</div>')

@app.get("/intelligence/conflicts",response_class=HTMLResponse)
def v39_conflicts_page():
    rows=_v39_conflicts(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">REVIEW</span> {esc(r["requirement"])}<div class="small">Appears under {r["n"]} trades.</div></div>' for r in rows)
    return shell("Conflict Brain",'<div class="hero"><h1>Cross-Document Conflict Brain</h1></div><div class="card">'+(h or '<p class="muted">No duplicate cross-trade requirements detected.</p>')+'</div>')

@app.get("/intelligence/rfis",response_class=HTMLResponse)
def v39_rfi_page():
    rows=_v39_conflicts(project_id())
    h="".join(f'<div class="card"><span class="badge WATCH">DRAFT RFI</span><h3>{esc(r["requirement"])}</h3><p>Please clarify the governing scope/document requirement. BuildCommand detected this requirement across multiple trade assignments.</p><p class="small">Human approval required before issue.</p></div>' for r in rows)
    return shell("RFI Intelligence",'<div class="hero"><h1>Automatic RFI Intelligence</h1><p class="muted">Proposals only. Nothing is sent automatically.</p></div>'+(h or '<div class="card">No RFI candidates detected.</div>'))

@app.get("/intelligence/schedule",response_class=HTMLResponse)
def v39_schedule_page():
    rows=_v39_schedule_risks(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(level)}</span> <b>{esc(name)}</b><div class="small">{esc(reason)} · {esc(date)}</div></div>' for level,name,reason,date in rows)
    return shell("Schedule Intelligence",'<div class="hero"><h1>Schedule Intelligence Brain</h1></div><div class="card">'+(h or '<p class="muted">No immediate schedule exceptions detected.</p>')+'</div>')

@app.get("/intelligence/lookahead",response_class=HTMLResponse)
def v39_lookahead_page():
    pid=project_id(); today=datetime.utcnow().date(); rows=_v39_rows("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,))
    body='<div class="hero"><h1>Look-Ahead Brain</h1><p class="muted">2, 3 and 6 week production outlook.</p></div>'
    for weeks in (2,3,6):
        end=today+timedelta(days=weeks*7)
        subset=[r for r in rows if _v39_safe_date(r["start"]) and today <= _v39_safe_date(r["start"]) <= end]
        h="".join(f'<div class="action"><b>{esc(r["name"])}</b><div class="small">{esc(r["trade"])} · {esc(r["start"])} → {esc(r["finish"])}</div></div>' for r in subset)
        body+=f'<div class="card"><h2>{weeks}-Week Look-Ahead</h2>{h or "<p class=muted>No scheduled starts in this window.</p>"}</div>'
    return shell("Look-Ahead",body)

@app.get("/intelligence/procurement",response_class=HTMLResponse)
def v39_procurement_page():
    rows=_v39_procurement(project_id())
    h="".join(f'<div class="action"><b>{esc(r["item"])}</b><div class="small">Required: {esc(r["required_on_site"])} · Promised: {esc(r["promised_date"])} · {esc(r["status"])}</div></div>' for r in rows)
    return shell("Procurement Intelligence",'<div class="hero"><h1>Procurement & Long-Lead Brain</h1></div><div class="card">'+(h or '<p class="muted">No open procurement items.</p>')+'</div>')

@app.get("/intelligence/bids",response_class=HTMLResponse)
def v39_bid_page():
    rows=_v39_rows("SELECT trade,COUNT(*) AS n FROM blueprint_scope_items WHERE project_id=? GROUP BY trade ORDER BY trade",(project_id(),))
    h="".join(f'<div class="action"><b>{esc(r["trade"])}</b><div class="small">{r["n"]} source-backed scope items ready for bid-package review.</div></div>' for r in rows)
    return shell("Bid Scope Intelligence",'<div class="hero"><h1>Subcontractor Scope & Bid Brain</h1><p class="muted">Trade packages originate from the cleaned Blueprint Brain scope.</p></div><div class="card">'+(h or '<p class="muted">Analyze project documents first.</p>')+'</div>')

@app.get("/intelligence/changes",response_class=HTMLResponse)
def v39_change_page():
    rows=_v39_changes(project_id()); total=sum(float(r["estimated_cost"] or 0) for r in rows); days=sum(float(r["schedule_days"] or 0) for r in rows)
    h="".join(f'<div class="action"><b>{esc(r["title"])}</b><div class="small">${float(r["estimated_cost"] or 0):,.0f} · {float(r["schedule_days"] or 0):g} days · {esc(r["status"])}</div></div>' for r in rows)
    return shell("Cost & Change",f'<div class="hero"><h1>Cost & Change Intelligence</h1><p class="muted">Open exposure: ${total:,.0f} · {days:g} schedule days.</p></div><div class="card">'+(h or '<p class="muted">No open change exposure.</p>')+'</div>')

@app.get("/intelligence/daily-command",response_class=HTMLResponse)
def v39_daily_command():
    pid=project_id(); attention=_v39_attention(pid); sched=_v39_schedule_risks(pid); proc=_v39_procurement(pid)
    critical=sum(1 for x in attention if x[0]=="CRITICAL"); today=sum(1 for x in attention if x[0]=="TODAY")
    body=f'<div class="hero"><div class="eyebrow">Daily Superintendent Command</div><h1>Run the job from here.</h1><p class="muted">{critical} critical · {today} today · {len(sched)} schedule exceptions · {len(proc)} procurement items.</p></div>'
    body+='<div class="grid3">'+_v37_link_card("Decisions","Prioritized items requiring human attention.","/intelligence/attention","Review")+_v37_link_card("Look-Ahead","Upcoming production and make-ready.","/intelligence/lookahead","Open")+_v37_link_card("Ask BuildCommand","Ask the project what matters now.","/ask-buildcommand","Ask")+'</div>'
    return shell("Daily Command",body)


# ============================================================
# v40 PROJECT AUTOPILOT
# ============================================================

def _v40_score(pid):
    att=_v39_attention(pid); sched=_v39_schedule_risks(pid); proc=_v39_procurement(pid); changes=_v39_changes(pid)
    critical=sum(1 for x in att if x[0]=="CRITICAL")+sum(1 for x in sched if x[0]=="CRITICAL")
    high=sum(1 for x in att if x[0] in {"TODAY","THIS WEEK"})
    score=min(100,critical*20+high*7+len(proc)*4+len(changes)*5)
    return {"risk":score,"health":max(0,100-score),"level":"CRITICAL" if score>=70 else "HIGH" if score>=45 else "MEDIUM" if score>=20 else "LOW"}

def _v40_revision_candidates(pid):
    rows=_v39_rows("SELECT * FROM attachments WHERE project_id=? ORDER BY id DESC LIMIT 100",(pid,))
    seen={}; pairs=[]
    for r in rows:
        name=str(r["original_name"] or "")
        base=re.sub(r'(?i)(rev(?:ision)?[ _-]*[A-Z0-9]+|addendum[ _-]*[A-Z0-9]+|bulletin[ _-]*[A-Z0-9]+)','',name)
        base=re.sub(r'[^a-z0-9]+',' ',base.lower()).strip()
        if base in seen: pairs.append((r,seen[base]))
        else: seen[base]=r
    return pairs[:25]

def _v40_gap_summary(pid):
    scopes=_v39_rows("SELECT trade,COUNT(*) AS n FROM blueprint_scope_items WHERE project_id=? GROUP BY trade ORDER BY trade",(pid,))
    subs=_v39_rows("SELECT trade,COUNT(*) AS n FROM subcontractors WHERE project_id=? GROUP BY trade",(pid,))
    covered={str(r["trade"] or "").lower() for r in subs}
    return [(r["trade"],r["n"],str(r["trade"] or "").lower() in covered) for r in scopes]

def _v40_inspection_ready(pid):
    inspections=_v39_rows("SELECT * FROM inspections_tracker WHERE project_id=? AND COALESCE(result,'PENDING')!='PASSED' ORDER BY scheduled_date LIMIT 50",(pid,))
    issues=len(_v39_rows("SELECT id FROM project_issues WHERE project_id=? AND COALESCE(status,'OPEN')!='CLOSED'",(pid,)))
    subs=len(_v39_rows("SELECT id FROM submittals WHERE project_id=? AND COALESCE(status,'PENDING') NOT IN ('APPROVED','CLOSED','COMPLETE')",(pid,)))
    return inspections,issues,subs

@app.get("/autopilot",response_class=HTMLResponse)
def v40_autopilot():
    pid=project_id(); s=_v40_score(pid)
    body=f'<div class="hero"><div class="eyebrow">BuildCommand v40 - Project Autopilot</div><h1>Your project is {s["level"]} risk.</h1><p class="muted">Health {s["health"]}/100 - Risk {s["risk"]}/100. Autopilot connects project decisions to downstream construction impact.</p></div><div class="grid3">'
    cards=[
      ("Risk Prediction","Score and prioritize project threats.","/autopilot/risks"),
      ("Revision Brain","Identify drawing and addendum revision candidates.","/autopilot/revisions"),
      ("Change Detection","Surface potential cost and time exposure.","/autopilot/change-detection"),
      ("Subcontractor Gaps","Compare cleaned scopes against subcontractor coverage.","/autopilot/gaps"),
      ("Inspection Readiness","Preflight upcoming inspections.","/autopilot/inspection-readiness"),
      ("Quality Control","Source-backed QC verification.","/autopilot/qc"),
      ("Field Photo Intelligence","Route photos into reviewed field intelligence.","/autopilot/photos"),
      ("Project Forecast","Forecast health, completion pressure and decisions.","/autopilot/forecast"),
      ("Executive Command","See project health across the company.","/autopilot/executive")
    ]
    for name,desc,href in cards: body+=_v37_link_card(name,desc,href,"Open")
    body+='</div>'
    return shell("Project Autopilot",body)

@app.get("/autopilot/risks",response_class=HTMLResponse)
def v40_risks():
    pid=project_id(); s=_v40_score(pid); items=_v39_attention(pid); sched=_v39_schedule_risks(pid)
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(x[0])}</span> <b>{esc(x[1])}</b> - {esc(x[2])}<div class="small">{esc(x[3])}</div></div>' for x in items[:60])
    h+="".join(f'<div class="action"><span class="badge WATCH">{esc(x[0])}</span> <b>SCHEDULE</b> - {esc(x[1])}<div class="small">{esc(x[2])} - {esc(x[3])}</div></div>' for x in sched)
    return shell("Risk Prediction",f'<div class="hero"><h1>Risk Prediction Engine</h1><p class="muted">{s["level"]} - Risk score {s["risk"]}/100</p></div><div class="card">{h or "No major risk signals detected."}</div>')

@app.get("/autopilot/revisions",response_class=HTMLResponse)
def v40_revisions():
    pairs=_v40_revision_candidates(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">COMPARE</span> <b>{esc(a["original_name"])}</b><div class="small">Possible revision of {esc(b["original_name"])}</div></div>' for a,b in pairs)
    return shell("Revision Brain",'<div class="hero"><h1>Drawing Revision / Addendum Brain</h1><p class="muted">Revision candidates are flagged for review. Contract scope is never changed automatically.</p></div><div class="card">'+(h or '<p class="muted">No likely revision pairs detected.</p>')+'</div>')

@app.get("/autopilot/change-detection",response_class=HTMLResponse)
def v40_change_detection():
    rows=_v39_conflicts(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">POTENTIAL CHANGE</span> {esc(r["requirement"])}<div class="small">Detected across {r["n"]} trade assignments. Review contract scope.</div></div>' for r in rows)
    return shell("Change Detection",'<div class="hero"><h1>Automatic Change-Order Detection</h1><p class="muted">Potential exposure only. Human approval required.</p></div><div class="card">'+(h or '<p class="muted">No current change candidates.</p>')+'</div>')

@app.get("/autopilot/gaps",response_class=HTMLResponse)
def v40_gaps():
    rows=_v40_gap_summary(project_id())
    h="".join(f'<div class="action"><span class="badge">{"COVERED" if ok else "GAP"}</span> <b>{esc(trade)}</b><div class="small">{n} cleaned scope items</div></div>' for trade,n,ok in rows)
    return shell("Subcontractor Gaps",'<div class="hero"><h1>Subcontractor Gap Analyzer</h1></div><div class="card">'+(h or '<p class="muted">Analyze project scope first.</p>')+'</div>')

@app.get("/autopilot/inspection-readiness",response_class=HTMLResponse)
def v40_inspection_readiness():
    rows,issues,subs=_v40_inspection_ready(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{"NOT READY" if issues or subs else "READY"}</span> <b>{esc(r["inspection_type"])}</b><div class="small">{esc(r["scheduled_date"])} - {issues} open issues - {subs} open submittals</div></div>' for r in rows)
    return shell("Inspection Readiness",'<div class="hero"><h1>Inspection Readiness Brain</h1></div><div class="card">'+(h or '<p class="muted">No pending inspections.</p>')+'</div>')

@app.get("/autopilot/qc",response_class=HTMLResponse)
def v40_qc():
    rows=_v39_rows("SELECT trade,requirement,source_ref FROM blueprint_scope_items WHERE project_id=? AND COALESCE(confidence,'')='HIGH' ORDER BY trade,id LIMIT 120",(project_id(),))
    h="".join(f'<div class="action"><input type="checkbox" style="width:auto"> <b>{esc(r["trade"])}</b> - {esc(r["requirement"])}<div class="small">{esc(r["source_ref"])}</div></div>' for r in rows)
    return shell("Quality Control",'<div class="hero"><h1>Quality-Control Brain</h1><p class="muted">Source-backed verification checklist.</p></div><div class="card">'+(h or '<p class="muted">Analyze plans first.</p>')+'</div>')

@app.get("/autopilot/photos",response_class=HTMLResponse)
def v40_photos():
    rows=_v39_rows("SELECT * FROM attachments WHERE project_id=? ORDER BY id DESC LIMIT 100",(project_id(),))
    photos=[r for r in rows if Path(r["original_name"] or "").suffix.lower() in {".jpg",".jpeg",".png",".webp"}]
    h="".join(f'<div class="action"><span class="badge WATCH">REVIEW PHOTO</span> <b>{esc(r["original_name"])}</b><div class="small">Associate with location, trade, QC, punch or schedule impact.</div></div>' for r in photos)
    return shell("Field Photo Intelligence",'<div class="hero"><h1>Field Photo Intelligence</h1><p class="muted">Human review required before formal issue creation.</p></div><div class="card">'+(h or '<p class="muted">No project photos uploaded.</p>')+'</div>')

@app.get("/autopilot/forecast",response_class=HTMLResponse)
def v40_forecast():
    pid=project_id(); s=_v40_score(pid); acts=_v39_rows("SELECT * FROM activities WHERE project_id=? ORDER BY finish DESC LIMIT 1",(pid,)); finish=acts[0]["finish"] if acts else ""; open_items=len(_v39_attention(pid))
    return shell("Project Forecast",f'<div class="hero"><h1>Project Forecast Brain</h1><p class="muted">Current planned finish: {esc(finish or "Not scheduled")}</p></div><div class="grid3"><div class="card"><div class="label">Health</div><div class="kpi">{s["health"]}</div></div><div class="card"><div class="label">Risk</div><div class="kpi">{s["risk"]}</div></div><div class="card"><div class="label">Open Decisions</div><div class="kpi">{open_items}</div></div></div>')

@app.get("/autopilot/executive",response_class=HTMLResponse)
def v40_executive():
    rows=_v39_rows("SELECT * FROM projects WHERE company_id=? ORDER BY id DESC",(current_company_id(),)); h=""
    for p in rows:
        s=_v40_score(p["id"]); h+=f'<div class="action"><span class="badge">{esc(s["level"])}</span> <b>{esc(p["number"])} - {esc(p["name"])}</b><div class="small">Health {s["health"]}/100 - Risk {s["risk"]}/100</div></div>'
    return shell("Executive Command",'<div class="hero"><h1>Executive Command Center</h1><p class="muted">Which projects need leadership attention and why.</p></div><div class="card">'+(h or '<p class="muted">No projects found.</p>')+'</div>')


# ============================================================
# v41 FIELD EXECUTION INTELLIGENCE
# ============================================================

def _v41_ensure_tables():
    c=db()
    if DATABASE_KIND=="postgres":
        c.execute("CREATE TABLE IF NOT EXISTS closeout_items(id BIGSERIAL PRIMARY KEY,company_id BIGINT,project_id BIGINT,category TEXT,item TEXT,responsible_party TEXT,due_date TEXT,status TEXT DEFAULT 'OPEN',notes TEXT DEFAULT '',created TEXT,updated TEXT)")
    else:
        c.execute("CREATE TABLE IF NOT EXISTS closeout_items(id INTEGER PRIMARY KEY,company_id INTEGER,project_id INTEGER,category TEXT,item TEXT,responsible_party TEXT,due_date TEXT,status TEXT DEFAULT 'OPEN',notes TEXT DEFAULT '',created TEXT,updated TEXT)")
    c.commit(); c.close()

def _v41_make_ready(pid):
    rows=_v39_rows("SELECT a.*,r.drawings,r.material,r.manpower,r.predecessor,r.access_ready,r.inspection,r.equipment,r.notes readiness_notes FROM activities a LEFT JOIN activity_readiness r ON r.activity_id=a.id AND r.project_id=a.project_id WHERE a.project_id=? AND COALESCE(a.status,'NOT_STARTED')!='COMPLETE' ORDER BY a.start LIMIT 80",(pid,))
    out=[]
    for r in rows:
        checks=[r["drawings"],r["material"],r["manpower"],r["predecessor"],r["access_ready"],r["inspection"],r["equipment"]]
        missing=7-sum(1 for x in checks if int(x or 0)==1)
        status="READY" if missing==0 else "AT RISK" if missing<=2 else "NOT READY"
        out.append((r,status,missing))
    return out

def _v41_sub_center(pid):
    return (
        _v39_rows("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(pid,)),
        _v39_rows("SELECT * FROM activities WHERE project_id=? AND COALESCE(status,'NOT_STARTED')!='COMPLETE' ORDER BY start",(pid,)),
        _v39_rows("SELECT * FROM subcontractor_updates WHERE project_id=? ORDER BY id DESC LIMIT 100",(pid,))
    )

def _v41_delivery_risks(pid):
    today=datetime.utcnow().date()
    rows=_v39_rows("SELECT * FROM procurement WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('DELIVERED','COMPLETE','CLOSED') ORDER BY required_on_site",(pid,))
    out=[]
    for r in rows:
        need=_v39_safe_date(r["required_on_site"]); promised=_v39_safe_date(r["promised_date"])
        exposure=(promised-need).days if need and promised else None
        level="CRITICAL" if exposure is not None and exposure>=7 else "HIGH" if exposure is not None and exposure>0 else "TODAY" if need and need<=today else "WATCH"
        out.append((r,level,exposure))
    return out

def _v41_inspection_countdown(pid):
    today=datetime.utcnow().date()
    rows=_v39_rows("SELECT * FROM inspections_tracker WHERE project_id=? AND COALESCE(result,'PENDING')!='PASSED' ORDER BY scheduled_date",(pid,))
    out=[]
    for r in rows:
        d=_v39_safe_date(r["scheduled_date"]); days=(d-today).days if d else None
        label="UNSCHEDULED" if days is None else "OVERDUE" if days<0 else "TODAY" if days==0 else "TOMORROW" if days==1 else f"{days} DAYS" if days<=7 else "UPCOMING"
        out.append((r,label,days))
    return out

def _v41_delay_candidates(pid):
    rows=_v39_rows("SELECT * FROM project_issues WHERE project_id=? AND COALESCE(status,'OPEN')!='CLOSED' ORDER BY id DESC",(pid,))
    out=[]
    for r in rows:
        txt=(str(r["title"] or "")+" "+str(r["description"] or "")+" "+str(r["issue_type"] or "")).lower()
        if any(x in txt for x in ["delay","late","waiting","hold","blocked","impact","shutdown","unavailable"]):
            out.append({"title":r["title"],"description":r["description"] or "","due":r["due"] or "","owner":r["owner"] or ""})
    for level,name,reason,datev in _v39_schedule_risks(pid):
        if level=="CRITICAL":
            out.append({"title":name,"description":reason,"due":datev,"owner":""})
    return out

def _v41_closeout_seed(pid):
    _v41_ensure_tables()
    c=db()
    r=c.execute("SELECT COUNT(*) AS n FROM closeout_items WHERE company_id=? AND project_id=?",(current_company_id(),pid)).fetchone()
    if int(r["n"] or 0)==0:
        now=datetime.utcnow().isoformat()
        defaults=[
            ("As-Builts","Record drawings / as-builts"),
            ("O&M","Operations and maintenance manuals"),
            ("Warranties","Warranty documentation"),
            ("Training","Owner training completion"),
            ("Attic Stock","Required attic stock / spare materials"),
            ("Inspections","Final inspections and certificates"),
            ("Testing","Final testing and commissioning"),
            ("Lien Releases","Final lien releases"),
            ("Turnover","Keys, access, owner turnover documents")
        ]
        for category,item in defaults:
            c.execute("INSERT INTO closeout_items(company_id,project_id,category,item,responsible_party,due_date,status,notes,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",(current_company_id(),pid,category,item,"","","OPEN","",now,now))
        c.commit()
    c.close()

@app.get("/field-command",response_class=HTMLResponse)
def v41_field_command():
    pid=project_id()
    ready=_v41_make_ready(pid); deliveries=_v41_delivery_risks(pid); inspections=_v41_inspection_countdown(pid)
    subs,acts,updates=_v41_sub_center(pid); delays=_v41_delay_candidates(pid); attention=_v39_attention(pid)
    not_ready=sum(1 for _,s,_ in ready if s=="NOT READY")
    today_ins=sum(1 for _,label,_ in inspections if label in {"TODAY","TOMORROW"})
    delivery_risk=sum(1 for _,level,_ in deliveries if level in {"CRITICAL","HIGH","TODAY"})
    body=f'<div class="hero"><div class="eyebrow">BuildCommand v41 - Superintendent Field Command</div><h1>Run today&#39;s job.</h1><p class="muted">{len(subs)} subcontractors - {today_ins} inspections due now/next - {delivery_risk} delivery risks - {not_ready} activities not ready - {len(delays)} delay signals - {len(attention)} decisions need review.</p></div>'
    body+='<div class="grid3">'
    body+=_v37_link_card("Make-Ready","See what can actually start and why.","/field-command/make-ready","Open")
    body+=_v37_link_card("Subcontractors","Crews, commitments and upcoming work.","/field-command/subs","Open")
    body+=_v37_link_card("Sub Alerts","Prepare schedule/field alerts for human approval.","/field-command/sub-alerts","Open")
    body+=_v37_link_card("Deliveries","Required-on-site vs promised-date exposure.","/field-command/deliveries","Open")
    body+=_v37_link_card("Inspections","Countdown and readiness before the inspector arrives.","/field-command/inspections","Open")
    body+=_v37_link_card("Daily Report","Build today's superintendent report from project data.","/field-command/daily-report","Open")
    body+=_v37_link_card("Delay Documentation","Capture possible delay events before the record is lost.","/field-command/delays","Open")
    body+=_v37_link_card("Punch & Completion","Open punch organized by location/trade.","/field-command/punch","Open")
    body+=_v37_link_card("Closeout","Track turnover requirements to completion.","/field-command/closeout","Open")
    body+='</div>'
    return shell("Field Command",body)

@app.get("/field-command/make-ready",response_class=HTMLResponse)
def v41_make_ready_page():
    rows=_v41_make_ready(project_id())
    h="".join(f'<div class="action"><span class="badge">{esc(status)}</span> <b>{esc(r["name"])}</b><div class="small">{esc(r["trade"])} - {esc(r["start"])} - {missing} readiness checks incomplete</div></div>' for r,status,missing in rows)
    return shell("Make-Ready Brain",'<div class="hero"><h1>Make-Ready Brain</h1><p class="muted">Drawings - material - manpower - predecessor - access - inspection - equipment.</p></div><div class="card">'+(h or '<p class="muted">No active activities.</p>')+'</div>')

@app.get("/field-command/subs",response_class=HTMLResponse)
def v41_subs_page():
    subs,acts,updates=_v41_sub_center(project_id()); h=""
    for s in subs:
        related=[a for a in acts if str(a["trade"] or "").lower()==str(s["trade"] or "").lower()]
        nxt=related[0] if related else None
        next_text=("Next: "+esc(nxt["name"])+" - "+esc(nxt["start"])) if nxt else "No upcoming activity matched."
        h+=f'<div class="card"><h3>{esc(s["name"])}</h3><div class="small">{esc(s["trade"])}</div><p>{next_text}</p></div>'
    return shell("Subcontractor Command",'<div class="hero"><h1>Subcontractor Command Center</h1></div><div class="grid3">'+(h or '<div class="card">No subcontractors loaded.</div>')+'</div>')

@app.get("/field-command/sub-alerts",response_class=HTMLResponse)
def v41_sub_alerts():
    subs,acts,updates=_v41_sub_center(project_id()); risks=_v39_schedule_risks(project_id()); h=""
    for s in subs:
        matching=[r for r in risks if any(str(a["trade"] or "").lower()==str(s["trade"] or "").lower() and a["name"]==r[1] for a in acts)]
        if matching:
            msg=f'BuildCommand draft: Please review upcoming {s["trade"]} schedule requirements and confirm manpower/material readiness. Human approval required before sending.'
            h+=f'<div class="card"><span class="badge WATCH">DRAFT ALERT</span><h3>{esc(s["name"])}</h3><p>{esc(msg)}</p></div>'
    return shell("Sub Alerts",'<div class="hero"><h1>Automatic Sub Alerts</h1><p class="muted">Prepared only. BuildCommand does not send external messages without approval.</p></div>'+(h or '<div class="card">No current alert candidates.</div>'))

@app.get("/field-command/deliveries",response_class=HTMLResponse)
def v41_deliveries_page():
    rows=_v41_delivery_risks(project_id()); h=""
    for r,level,exposure in rows:
        ex=(f' - {exposure} day exposure' if exposure is not None and exposure>0 else '')
        h+=f'<div class="action"><span class="badge WATCH">{esc(level)}</span> <b>{esc(r["item"])}</b><div class="small">Need {esc(r["required_on_site"])} - Promised {esc(r["promised_date"])}{ex}</div></div>'
    return shell("Delivery Intelligence",'<div class="hero"><h1>Delivery Intelligence</h1></div><div class="card">'+(h or '<p class="muted">No open deliveries.</p>')+'</div>')

@app.get("/field-command/inspections",response_class=HTMLResponse)
def v41_inspections_page():
    rows=_v41_inspection_countdown(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(label)}</span> <b>{esc(r["inspection_type"])}</b><div class="small">{esc(r["scheduled_date"])} - {esc(r["authority"])}</div></div>' for r,label,days in rows)
    return shell("Inspection Countdown",'<div class="hero"><h1>Inspection Countdown</h1><p class="muted">7 days - 3 days - tomorrow - today.</p></div><div class="card">'+(h or '<p class="muted">No pending inspections.</p>')+'</div>')

@app.get("/field-command/daily-report",response_class=HTMLResponse)
def v41_daily_report_page():
    pid=project_id()
    acts=_v39_rows("SELECT * FROM activities WHERE project_id=? AND COALESCE(status,'NOT_STARTED')!='COMPLETE' ORDER BY start LIMIT 20",(pid,))
    deliveries=_v41_delivery_risks(pid); inspections=_v41_inspection_countdown(pid); delays=_v41_delay_candidates(pid)
    work=", ".join(str(a["name"]) for a in acts[:8])
    body=f'<div class="hero"><h1>Daily Report Brain</h1><p class="muted">Draft from live project information; superintendent reviews before saving.</p></div><div class="card"><h2>Draft</h2><p><b>Work / upcoming:</b> {esc(work or "No active activities")}</p><p><b>Deliveries:</b> {len(deliveries)} open</p><p><b>Inspections:</b> {len(inspections)} pending</p><p><b>Potential delays:</b> {len(delays)}</p><p><a href="/daily-report">Open Daily Report editor -&gt;</a></p></div>'
    return shell("Daily Report Brain",body)

@app.get("/field-command/delays",response_class=HTMLResponse)
def v41_delays_page():
    rows=_v41_delay_candidates(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">DOCUMENT</span> <b>{esc(r["title"])}</b><div class="small">{esc(r["description"])}</div></div>' for r in rows)
    return shell("Delay Documentation",'<div class="hero"><h1>Delay Documentation Brain</h1><p class="muted">Capture dates, affected activity, responsible party, photos, RFIs and schedule impact.</p></div><div class="card">'+(h or '<p class="muted">No obvious delay candidates detected.</p>')+'</div>')

@app.get("/field-command/punch",response_class=HTMLResponse)
def v41_punch_page():
    rows=_v39_rows("SELECT * FROM punch_items WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('VERIFIED','CLOSED','COMPLETE') ORDER BY location,trade,due",(project_id(),))
    h="".join(f'<div class="action"><b>{esc(r["location"] or "No location")} - {esc(r["title"])}</b><div class="small">{esc(r["trade"])} - {esc(r["owner"])} - Due {esc(r["due"])}</div></div>' for r in rows)
    return shell("Punch & Completion",'<div class="hero"><h1>Punch & Completion Brain</h1></div><div class="card">'+(h or '<p class="muted">No open punch items.</p>')+'<p><a href="/punch">Open full Punch List -&gt;</a></p></div>')

@app.get("/field-command/closeout",response_class=HTMLResponse)
def v41_closeout_page():
    pid=project_id(); _v41_closeout_seed(pid)
    rows=_v39_rows("SELECT * FROM closeout_items WHERE company_id=? AND project_id=? ORDER BY category,id",(current_company_id(),pid))
    complete=sum(1 for r in rows if str(r["status"] or "").upper() in {"COMPLETE","CLOSED","RECEIVED"})
    h="".join(f'<div class="action"><span class="badge">{esc(r["status"])}</span> <b>{esc(r["category"])}</b> - {esc(r["item"])}<div class="small">{esc(r["responsible_party"])} {esc(r["due_date"])}</div></div>' for r in rows)
    return shell("Closeout Brain",f'<div class="hero"><h1>Closeout Brain</h1><p class="muted">{complete}/{len(rows)} core turnover requirements complete.</p></div><div class="card">'+h+'</div>')


# ============================================================
# v42 PROJECT CONTROL INTELLIGENCE
# ============================================================

def _v42_ensure_tables():
    c=db()
    if DATABASE_KIND=="postgres":
        pk="BIGSERIAL PRIMARY KEY"; num="DOUBLE PRECISION"
    else:
        pk="INTEGER PRIMARY KEY"; num="REAL"
    stmts=[
      f"CREATE TABLE IF NOT EXISTS rfi_control(id {pk},company_id BIGINT,project_id BIGINT,number TEXT,title TEXT,question TEXT,responsible_party TEXT,due_date TEXT,status TEXT DEFAULT 'DRAFT',answer TEXT,cost_impact {num} DEFAULT 0,schedule_days {num} DEFAULT 0,source_ref TEXT,created TEXT,updated TEXT)",
      f"CREATE TABLE IF NOT EXISTS budget_commitments(id {pk},company_id BIGINT,project_id BIGINT,trade TEXT,vendor TEXT,original_budget {num} DEFAULT 0,commitment {num} DEFAULT 0,approved_changes {num} DEFAULT 0,pending_exposure {num} DEFAULT 0,notes TEXT,created TEXT,updated TEXT)",
      f"CREATE TABLE IF NOT EXISTS contract_scopes(id {pk},company_id BIGINT,project_id BIGINT,sub_name TEXT,trade TEXT,scope_text TEXT,exclusions TEXT,allowances TEXT,alternates TEXT,status TEXT DEFAULT 'REVIEW',created TEXT,updated TEXT)",
      f"CREATE TABLE IF NOT EXISTS correspondence_control(id {pk},company_id BIGINT,project_id BIGINT,correspondence_date TEXT,subject TEXT,party TEXT,trade TEXT,related_type TEXT,related_ref TEXT,summary TEXT,status TEXT DEFAULT 'OPEN',created TEXT)",
      f"CREATE TABLE IF NOT EXISTS owner_decisions(id {pk},company_id BIGINT,project_id BIGINT,title TEXT,decision_needed TEXT,due_date TEXT,cost_impact {num} DEFAULT 0,schedule_days {num} DEFAULT 0,status TEXT DEFAULT 'OPEN',source_ref TEXT,created TEXT,updated TEXT)",
      f"CREATE TABLE IF NOT EXISTS project_audit_log(id {pk},company_id BIGINT,project_id BIGINT,event_time TEXT,event_type TEXT,title TEXT,source_ref TEXT,decision TEXT,downstream_impact TEXT,reviewed_by TEXT,created TEXT)"
    ]
    for s in stmts: c.execute(s)
    c.commit(); c.close()

def _v42_rfis(pid):
    _v42_ensure_tables()
    return _v39_rows("SELECT * FROM rfi_control WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),pid))

def _v42_budget(pid):
    _v42_ensure_tables()
    return _v39_rows("SELECT * FROM budget_commitments WHERE company_id=? AND project_id=? ORDER BY trade,vendor",(current_company_id(),pid))

def _v42_contract_gaps(pid):
    _v42_ensure_tables()
    scopes=_v39_rows("SELECT trade,COUNT(*) n FROM blueprint_scope_items WHERE project_id=? GROUP BY trade ORDER BY trade",(pid,))
    contracts=_v39_rows("SELECT * FROM contract_scopes WHERE company_id=? AND project_id=?",(current_company_id(),pid))
    covered={str(r["trade"] or "").lower():r for r in contracts}
    return [(r["trade"],r["n"],covered.get(str(r["trade"] or "").lower())) for r in scopes]

def _v42_meeting_actions(pid):
    meetings=_v39_rows("SELECT * FROM meeting_notes WHERE project_id=? ORDER BY meeting_date DESC,id DESC LIMIT 50",(pid,))
    actions=_v39_rows("SELECT * FROM action_items WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('CLOSED','COMPLETE') ORDER BY due",(pid,))
    return meetings,actions

def _v42_correspondence(pid):
    _v42_ensure_tables()
    return _v39_rows("SELECT * FROM correspondence_control WHERE company_id=? AND project_id=? ORDER BY correspondence_date DESC,id DESC",(current_company_id(),pid))

def _v42_owner(pid):
    _v42_ensure_tables()
    return _v39_rows("SELECT * FROM owner_decisions WHERE company_id=? AND project_id=? AND COALESCE(status,'OPEN') NOT IN ('CLOSED','COMPLETE') ORDER BY due_date",(current_company_id(),pid))

def _v42_audit(pid):
    _v42_ensure_tables()
    return _v39_rows("SELECT * FROM project_audit_log WHERE company_id=? AND project_id=? ORDER BY event_time DESC,id DESC LIMIT 200",(current_company_id(),pid))

@app.get("/project-control",response_class=HTMLResponse)
def v42_project_control():
    pid=project_id(); _v42_ensure_tables()
    rfis=_v42_rfis(pid); subs=_v39_rows("SELECT * FROM submittals WHERE project_id=? AND COALESCE(status,'PENDING') NOT IN ('APPROVED','CLOSED','COMPLETE')",(pid,))
    budget=_v42_budget(pid); changes=_v39_changes(pid); owners=_v42_owner(pid); gaps=_v42_contract_gaps(pid)
    meetings,actions=_v42_meeting_actions(pid); corr=_v42_correspondence(pid); audit=_v42_audit(pid)
    pending=sum(float(r["pending_exposure"] or 0) for r in budget)+sum(float(r["estimated_cost"] or 0) for r in changes)
    gap_count=sum(1 for _,_,c in gaps if c is None)
    body=f'<div class="hero"><div class="eyebrow">BuildCommand v42 - Project Control</div><h1>Control cost, schedule and decisions.</h1><p class="muted">{len(rfis)} RFIs - {len(subs)} open submittals - ${pending:,.0f} pending exposure - {len(owners)} owner decisions - {gap_count} contract scope gaps - {len(actions)} open actions.</p></div><div class="grid3">'
    cards=[
      ("RFI Command","Lifecycle, cost/schedule impact and field verification.","/project-control/rfis"),
      ("Submittal Command","Track required, submitted, review and release status.","/project-control/submittals"),
      ("Budget & Commitments","Budget, commitments, approved changes and exposure.","/project-control/budget"),
      ("Change Management","Trace potential changes through cost and schedule.","/project-control/changes"),
      ("Contract Scope","Compare subcontract coverage against Blueprint Brain.","/project-control/contracts"),
      ("Meeting Intelligence","Decisions, commitments and open actions.","/project-control/meetings"),
      ("Correspondence","Organize important communication around project issues.","/project-control/correspondence"),
      ("Owner Decisions","Decisions required and their consequence.","/project-control/owner-decisions"),
      ("Audit Trail","Chronological project intelligence record.","/project-control/audit")
    ]
    for name,desc,href in cards: body+=_v37_link_card(name,desc,href,"Open")
    body+='</div>'
    return shell("Project Control",body)

@app.get("/project-control/rfis",response_class=HTMLResponse)
def v42_rfi_page():
    rows=_v42_rfis(project_id())
    h="".join(f'<div class="action"><span class="badge">{esc(r["status"])}</span> <b>{esc(r["number"] or "DRAFT")} - {esc(r["title"])}</b><div class="small">Due {esc(r["due_date"])} - Cost ${float(r["cost_impact"] or 0):,.0f} - {float(r["schedule_days"] or 0):g} days</div></div>' for r in rows)
    return shell("RFI Command",'<div class="hero"><h1>RFI Command Brain</h1><p class="muted">Detected - draft - submitted - answered - cost/schedule impact - field verification.</p></div><div class="card">'+(h or '<p class="muted">No controlled RFIs yet. Conflict Brain proposals remain available for human review.</p>')+'</div>')

@app.get("/project-control/submittals",response_class=HTMLResponse)
def v42_submittal_page():
    rows=_v39_rows("SELECT * FROM submittals WHERE project_id=? ORDER BY due_date,id",(project_id(),))
    h="".join(f'<div class="action"><span class="badge">{esc(r["status"])}</span> <b>{esc(r["title"])}</b><div class="small">Spec {esc(r["spec_section"])} - {esc(r["responsible_party"])} - Due {esc(r["due_date"])}</div></div>' for r in rows)
    return shell("Submittal Command",'<div class="hero"><h1>Submittal Command Brain</h1></div><div class="card">'+(h or '<p class="muted">No submittals loaded.</p>')+'</div>')

@app.get("/project-control/budget",response_class=HTMLResponse)
def v42_budget_page():
    rows=_v42_budget(project_id()); h=""; tb=tc=ta=tp=0
    for r in rows:
        b=float(r["original_budget"] or 0); c=float(r["commitment"] or 0); a=float(r["approved_changes"] or 0); p=float(r["pending_exposure"] or 0)
        tb+=b; tc+=c; ta+=a; tp+=p; forecast=c+a+p; level="WATCH" if b and forecast>b else "READY"
        h+=f'<div class="action"><span class="badge {level}">{level}</span> <b>{esc(r["trade"])}</b><div class="small">Budget ${b:,.0f} - Committed ${c:,.0f} - Approved ${a:,.0f} - Pending ${p:,.0f} - Forecast ${forecast:,.0f}</div></div>'
    head=f'<div class="hero"><h1>Budget & Commitment Brain</h1><p class="muted">Budget ${tb:,.0f} - Commitments ${tc:,.0f} - Approved changes ${ta:,.0f} - Pending exposure ${tp:,.0f}</p></div>'
    return shell("Budget & Commitments",head+'<div class="card">'+(h or '<p class="muted">No budget commitments loaded yet.</p>')+'</div>')

@app.get("/project-control/changes",response_class=HTMLResponse)
def v42_changes_page():
    rows=_v39_changes(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(r["status"])}</span> <b>{esc(r["event_type"])} - {esc(r["title"])}</b><div class="small">${float(r["estimated_cost"] or 0):,.0f} - {float(r["schedule_days"] or 0):g} days - {esc(r["responsible_party"])}</div></div>' for r in rows)
    return shell("Change Management",'<div class="hero"><h1>Change Management Brain</h1><p class="muted">RFI / ASI / bulletin / owner request / field condition to cost and schedule exposure.</p></div><div class="card">'+(h or '<p class="muted">No open change events.</p>')+'</div>')

@app.get("/project-control/contracts",response_class=HTMLResponse)
def v42_contract_page():
    rows=_v42_contract_gaps(project_id()); h=""
    for trade,n,c in rows:
        if c is None:
            h+=f'<div class="action"><span class="badge WATCH">GAP</span> <b>{esc(trade)}</b><div class="small">{n} Blueprint Brain scope items - no contract scope record loaded.</div></div>'
        else:
            exclusions=str(c["exclusions"] or "")
            h+=f'<div class="action"><span class="badge READY">REVIEWED</span> <b>{esc(trade)} - {esc(c["sub_name"])}</b><div class="small">{n} source-backed items - exclusions: {esc(exclusions[:300])}</div></div>'
    return shell("Contract Scope",'<div class="hero"><h1>Contract Scope Brain</h1><p class="muted">Scope gaps, exclusions, overlaps, allowances and unclear responsibility.</p></div><div class="card">'+(h or '<p class="muted">Analyze Blueprint Brain scope first.</p>')+'</div>')

@app.get("/project-control/meetings",response_class=HTMLResponse)
def v42_meeting_page():
    meetings,actions=_v42_meeting_actions(project_id()); h=""
    for m in meetings:
        h+=f'<div class="card"><span class="badge">{esc(m["meeting_type"])}</span><h3>{esc(m["title"])}</h3><div class="small">{esc(m["meeting_date"])} - {esc(m["attendees"])}</div><p><b>Decisions:</b> {esc(m["decisions"])}</p><p><b>Commitments:</b> {esc(m["commitments"])}</p></div>'
    ah="".join(f'<div class="action"><b>{esc(a["title"])}</b><div class="small">{esc(a["owner"])} - Due {esc(a["due"])} - {esc(a["priority"])}</div></div>' for a in actions)
    return shell("Meeting Intelligence",'<div class="hero"><h1>Meeting Intelligence</h1><p class="muted">Keep decisions and commitments from disappearing.</p></div>'+(h or '<div class="card">No meeting notes loaded.</div>')+'<div class="card"><h2>Open Actions</h2>'+ (ah or '<p class="muted">No open actions.</p>')+'</div>')

@app.get("/project-control/correspondence",response_class=HTMLResponse)
def v42_correspondence_page():
    rows=_v42_correspondence(project_id())
    h="".join(f'<div class="action"><span class="badge">{esc(r["related_type"])}</span> <b>{esc(r["subject"])}</b><div class="small">{esc(r["correspondence_date"])} - {esc(r["party"])} - {esc(r["trade"])} - {esc(r["related_ref"])}</div><p>{esc(r["summary"])}</p></div>' for r in rows)
    return shell("Correspondence",'<div class="hero"><h1>Correspondence Brain</h1><p class="muted">Communication organized around trade, issue, RFI, submittal, schedule and change.</p></div><div class="card">'+(h or '<p class="muted">No controlled correspondence loaded yet.</p>')+'</div>')

@app.get("/project-control/owner-decisions",response_class=HTMLResponse)
def v42_owner_page():
    rows=_v42_owner(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">DECISION</span> <b>{esc(r["title"])}</b><div>{esc(r["decision_needed"])}</div><div class="small">Due {esc(r["due_date"])} - ${float(r["cost_impact"] or 0):,.0f} exposure - {float(r["schedule_days"] or 0):g} days at risk - {esc(r["source_ref"])}</div></div>' for r in rows)
    return shell("Owner Decisions",'<div class="hero"><h1>Owner Decision Tracker</h1><p class="muted">Decision needed - deadline - downstream cost/schedule consequence.</p></div><div class="card">'+(h or '<p class="muted">No owner decisions currently tracked.</p>')+'</div>')

@app.get("/project-control/audit",response_class=HTMLResponse)
def v42_audit_page():
    rows=_v42_audit(project_id())
    h="".join(f'<div class="action"><span class="badge">{esc(r["event_type"])}</span> <b>{esc(r["title"])}</b><div class="small">{esc(r["event_time"])} - {esc(r["source_ref"])} - reviewed by {esc(r["reviewed_by"])}</div><p>{esc(r["downstream_impact"])}</p></div>' for r in rows)
    return shell("Project Audit Trail",'<div class="hero"><h1>Project Audit Trail</h1><p class="muted">What changed, source, decision, downstream impact and review history.</p></div><div class="card">'+(h or '<p class="muted">Audit events will accumulate as controlled project decisions are recorded.</p>')+'</div>')


# ============================================================
# v43 CONSTRUCTION LEARNING INTELLIGENCE
# Human-approved learning only: AI output never teaches itself.
# ============================================================

def _v43_ensure_tables():
    c=db()
    if DATABASE_KIND=="postgres":
        pk="BIGSERIAL PRIMARY KEY"; num="DOUBLE PRECISION"
    else:
        pk="INTEGER PRIMARY KEY"; num="REAL"
    stmts=[
      f"""CREATE TABLE IF NOT EXISTS learning_rules(
        id {pk},company_id BIGINT,project_id BIGINT,rule_type TEXT,subject TEXT,
        learned_rule TEXT,source_ref TEXT,scope_level TEXT DEFAULT 'PROJECT ONLY',
        approval_status TEXT DEFAULT 'PROPOSED',approved_by TEXT,confidence TEXT DEFAULT 'HIGH',
        created TEXT,updated TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS estimator_learning(
        id {pk},company_id BIGINT,project_id BIGINT,estimator_item_id BIGINT,trade TEXT,
        description TEXT,ai_quantity {num},accepted_quantity {num},unit TEXT,
        decision TEXT,reason TEXT,approval_status TEXT DEFAULT 'PROPOSED',
        approved_by TEXT,created TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS historical_costs(
        id {pk},company_id BIGINT,project_id BIGINT,trade TEXT,scope TEXT,unit TEXT,
        quantity {num},unit_cost {num},total_cost {num},building_type TEXT,location TEXT,
        vendor TEXT,cost_date TEXT,approval_status TEXT DEFAULT 'APPROVED',created TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS schedule_learning(
        id {pk},company_id BIGINT,project_id BIGINT,activity_id BIGINT,trade TEXT,
        activity_name TEXT,planned_start TEXT,planned_finish TEXT,actual_start TEXT,actual_finish TEXT,
        planned_days {num},actual_days {num},production_note TEXT,approval_status TEXT DEFAULT 'PROPOSED',created TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS subcontractor_performance(
        id {pk},company_id BIGINT,project_id BIGINT,sub_name TEXT,trade TEXT,
        schedule_score {num} DEFAULT 0,quality_score {num} DEFAULT 0,safety_score {num} DEFAULT 0,
        responsiveness_score {num} DEFAULT 0,change_score {num} DEFAULT 0,closeout_score {num} DEFAULT 0,
        notes TEXT,approval_status TEXT DEFAULT 'PROPOSED',created TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS inspection_learning(
        id {pk},company_id BIGINT,project_id BIGINT,inspection_type TEXT,authority TEXT,trade TEXT,
        result TEXT,failure_reason TEXT,correction TEXT,lesson TEXT,
        approval_status TEXT DEFAULT 'PROPOSED',created TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS lessons_learned(
        id {pk},company_id BIGINT,project_id BIGINT,category TEXT,title TEXT,lesson TEXT,
        recommendation TEXT,source_ref TEXT,scope_level TEXT DEFAULT 'PROJECT ONLY',
        approval_status TEXT DEFAULT 'PROPOSED',approved_by TEXT,created TEXT)"""
    ]
    for s in stmts: c.execute(s)
    c.commit(); c.close()

def _v43_rules(pid=None):
    _v43_ensure_tables()
    if pid:
        return _v39_rows("SELECT * FROM learning_rules WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),pid))
    return _v39_rows("SELECT * FROM learning_rules WHERE company_id=? ORDER BY id DESC LIMIT 300",(current_company_id(),))

def _v43_trade_proposals(pid):
    # Current high-confidence reviewed scope becomes a learning candidate only.
    rows=_v39_rows("SELECT * FROM blueprint_scope_items WHERE project_id=? AND COALESCE(confidence,'')='HIGH' ORDER BY id DESC LIMIT 150",(pid,))
    return rows

def _v43_estimator_signals(pid):
    return _v39_rows("SELECT * FROM estimator_items WHERE project_id=? AND (COALESCE(verified,0)=1 OR ai_quantity IS NOT NULL) ORDER BY id DESC LIMIT 150",(pid,))

def _v43_schedule_signals(pid):
    return _v39_rows("SELECT * FROM activities WHERE project_id=? ORDER BY id DESC LIMIT 150",(pid,))

def _v43_inspection_signals(pid):
    return _v39_rows("SELECT * FROM inspections_tracker WHERE project_id=? AND COALESCE(result,'PENDING') NOT IN ('PENDING','PASSED') ORDER BY id DESC LIMIT 100",(pid,))

def _v43_rfi_change_patterns(pid):
    _v42_ensure_tables()
    rfis=_v39_rows("SELECT * FROM rfi_control WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),pid))
    changes=_v39_rows("SELECT * FROM change_events WHERE project_id=? ORDER BY id DESC",(pid,))
    return rfis,changes

@app.get("/learning",response_class=HTMLResponse)
def v43_learning_center():
    pid=project_id(); _v43_ensure_tables()
    rules=_v43_rules(pid); trade=_v43_trade_proposals(pid); est=_v43_estimator_signals(pid)
    inspections=_v43_inspection_signals(pid); rfis,changes=_v43_rfi_change_patterns(pid)
    approved=sum(1 for r in rules if str(r["approval_status"] or "").upper()=="APPROVED")
    body=f'<div class="hero"><div class="eyebrow">BuildCommand v43 - Construction Learning Intelligence</div><h1>Every approved lesson makes the next project smarter.</h1><p class="muted">{approved} approved learned rules - {len(trade)} trade-learning candidates - {len(est)} estimator signals - {len(inspections)} inspection lessons - {len(rfis)+len(changes)} RFI/change patterns.</p><p><b>Learning firewall:</b> AI proposals never become permanent construction knowledge without human approval.</p></div><div class="grid3">'
    cards=[
      ("Trade Assignment Learning","Turn reviewed trade corrections into reusable rules.","/learning/trades"),
      ("Scope Boundary Brain","Learn where one trade stops and another starts.","/learning/boundaries"),
      ("Estimator Learning","Learn from accepted, rejected and adjusted takeoff proposals.","/learning/estimator"),
      ("Historical Cost Brain","Private cost history by trade, scope, unit and project.","/learning/costs"),
      ("Schedule Learning","Compare planned durations with actual field performance.","/learning/schedule"),
      ("Sub Performance","Learn which subcontractors actually perform.","/learning/subs"),
      ("Inspection Learning","Remember failures, corrections and AHJ patterns.","/learning/inspections"),
      ("RFI / Change Patterns","Find recurring drawing conflicts and cost exposure.","/learning/patterns"),
      ("Lessons Learned","Turn project experience into approved future guidance.","/learning/lessons"),
      ("Knowledge Core","Controlled project/company/global construction knowledge.","/learning/core")
    ]
    for name,desc,href in cards: body+=_v37_link_card(name,desc,href,"Open")
    body+='</div>'
    return shell("Construction Learning",body)

@app.get("/learning/trades",response_class=HTMLResponse)
def v43_trade_learning():
    rows=_v43_trade_proposals(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">PROPOSAL</span> <b>{esc(r["trade"])}</b> - {esc(r["requirement"])}<div class="small">{esc(r["source_sheet"])} {esc(r["source_detail"])} - Human approval required before learning.</div></div>' for r in rows)
    return shell("Trade Assignment Learning",'<div class="hero"><h1>Trade Assignment Learning Brain</h1><p class="muted">Reviewed construction corrections become candidates for reusable knowledge, never automatic rules.</p></div><div class="card">'+(h or '<p class="muted">No high-confidence trade candidates yet.</p>')+'</div>')

@app.get("/learning/boundaries",response_class=HTMLResponse)
def v43_boundaries():
    rows=_v43_rules()
    rows=[r for r in rows if str(r["rule_type"] or "").upper() in {"SCOPE BOUNDARY","TRADE ASSIGNMENT"}]
    h="".join(f'<div class="action"><span class="badge">{esc(r["scope_level"])}</span> <b>{esc(r["subject"])}</b><div>{esc(r["learned_rule"])}</div><div class="small">{esc(r["approval_status"])} - {esc(r["source_ref"])}</div></div>' for r in rows)
    return shell("Scope Boundary Brain",'<div class="hero"><h1>Scope Boundary Brain</h1><p class="muted">Responsibility rules can be PROJECT ONLY, COMPANY STANDARD, or controlled GLOBAL BUILDCOMMAND RULE.</p></div><div class="card">'+(h or '<p class="muted">No approved boundary rules recorded yet.</p>')+'</div>')

@app.get("/learning/estimator",response_class=HTMLResponse)
def v43_estimator_learning():
    rows=_v43_estimator_signals(project_id()); h=""
    for r in rows:
        ai=r["ai_quantity"]; accepted=r["quantity"]; delta=(float(accepted or 0)-float(ai or 0)) if ai is not None else None
        h+=f'<div class="action"><span class="badge {"READY" if r["verified"] else "WATCH"}">{"VERIFIED" if r["verified"] else "REVIEW"}</span> <b>{esc(r["trade"])} - {esc(r["description"])}</b><div class="small">AI {esc(ai)} {esc(r["ai_unit"])} - Current {esc(accepted)} {esc(r["unit"])}'+(f' - Delta {delta:g}' if delta is not None else '')+'</div></div>'
    return shell("Estimator Learning",'<div class="hero"><h1>Estimator Learning Brain</h1><p class="muted">Quantity decisions remain proposals until reviewed; estimator quantities are never silently overwritten.</p></div><div class="card">'+(h or '<p class="muted">No estimator learning signals yet.</p>')+'</div>')

@app.get("/learning/costs",response_class=HTMLResponse)
def v43_costs():
    _v43_ensure_tables()
    rows=_v39_rows("SELECT * FROM historical_costs WHERE company_id=? ORDER BY cost_date DESC,id DESC LIMIT 200",(current_company_id(),))
    h="".join(f'<div class="action"><b>{esc(r["trade"])} - {esc(r["scope"])}</b><div class="small">{float(r["quantity"] or 0):g} {esc(r["unit"])} @ ${float(r["unit_cost"] or 0):,.2f} - Total ${float(r["total_cost"] or 0):,.0f} - {esc(r["vendor"])}</div></div>' for r in rows)
    return shell("Historical Cost Brain",'<div class="hero"><h1>Historical Cost Brain</h1><p class="muted">Private company cost intelligence by trade, scope, unit, project, vendor and date.</p></div><div class="card">'+(h or '<p class="muted">Historical costs will build from reviewed project actuals.</p>')+'</div>')

@app.get("/learning/schedule",response_class=HTMLResponse)
def v43_schedule_learning():
    rows=_v43_schedule_signals(project_id())
    h="".join(f'<div class="action"><b>{esc(r["trade"])} - {esc(r["name"])}</b><div class="small">Planned {esc(r["start"])} to {esc(r["finish"])} - {float(r["pct"] or 0):g}% complete - {esc(r["status"])}</div></div>' for r in rows)
    return shell("Schedule Learning",'<div class="hero"><h1>Schedule Learning Brain</h1><p class="muted">Build realistic production intelligence by comparing planned work against actual field performance.</p></div><div class="card">'+(h or '<p class="muted">No schedule activities loaded.</p>')+'</div>')

@app.get("/learning/subs",response_class=HTMLResponse)
def v43_sub_learning():
    _v43_ensure_tables()
    rows=_v39_rows("SELECT * FROM subcontractor_performance WHERE company_id=? ORDER BY id DESC LIMIT 200",(current_company_id(),))
    current=_v39_rows("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(project_id(),))
    h="".join(f'<div class="action"><b>{esc(r["sub_name"])} - {esc(r["trade"])}</b><div class="small">Schedule {float(r["schedule_score"] or 0):g} - Quality {float(r["quality_score"] or 0):g} - Safety {float(r["safety_score"] or 0):g} - Responsiveness {float(r["responsiveness_score"] or 0):g} - Closeout {float(r["closeout_score"] or 0):g}</div></div>' for r in rows)
    if not h:
        h="".join(f'<div class="action"><span class="badge WATCH">NEEDS HISTORY</span> <b>{esc(r["name"])} - {esc(r["trade"])}</b></div>' for r in current)
    return shell("Subcontractor Performance",'<div class="hero"><h1>Subcontractor Performance Brain</h1><p class="muted">Performance history should inform future selection, not price alone.</p></div><div class="card">'+(h or '<p class="muted">No subcontractors loaded.</p>')+'</div>')

@app.get("/learning/inspections",response_class=HTMLResponse)
def v43_inspection_learning():
    rows=_v43_inspection_signals(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(r["result"])}</span> <b>{esc(r["inspection_type"])}</b><div class="small">{esc(r["authority"])} - {esc(r["notes"])}</div></div>' for r in rows)
    return shell("Inspection Learning",'<div class="hero"><h1>Inspection Learning Brain</h1><p class="muted">Remember failed inspections and corrections by jurisdiction, trade and inspection type.</p></div><div class="card">'+(h or '<p class="muted">No failed/rejected inspection signals.</p>')+'</div>')

@app.get("/learning/patterns",response_class=HTMLResponse)
def v43_patterns():
    rfis,changes=_v43_rfi_change_patterns(project_id()); h=""
    for r in rfis:
        h+=f'<div class="action"><span class="badge">RFI</span> <b>{esc(r["title"])}</b><div class="small">{esc(r["source_ref"])} - ${float(r["cost_impact"] or 0):,.0f} - {float(r["schedule_days"] or 0):g} days</div></div>'
    for r in changes:
        h+=f'<div class="action"><span class="badge WATCH">CHANGE</span> <b>{esc(r["title"])}</b><div class="small">{esc(r["event_type"])} - ${float(r["estimated_cost"] or 0):,.0f} - {float(r["schedule_days"] or 0):g} days</div></div>'
    return shell("RFI / Change Patterns",'<div class="hero"><h1>RFI / Change Pattern Brain</h1><p class="muted">Recurring conflicts become preconstruction warnings after review.</p></div><div class="card">'+(h or '<p class="muted">No RFI/change history yet.</p>')+'</div>')

@app.get("/learning/lessons",response_class=HTMLResponse)
def v43_lessons():
    _v43_ensure_tables()
    rows=_v39_rows("SELECT * FROM lessons_learned WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),project_id()))
    h="".join(f'<div class="action"><span class="badge">{esc(r["approval_status"])}</span> <b>{esc(r["category"])} - {esc(r["title"])}</b><div>{esc(r["lesson"])}</div><div class="small">Recommendation: {esc(r["recommendation"])} - {esc(r["scope_level"])}</div></div>' for r in rows)
    return shell("Lessons Learned",'<div class="hero"><h1>Project Lessons-Learned Brain</h1><p class="muted">What worked, what failed, what cost money, what delayed the job, and what the next project should do differently.</p></div><div class="card">'+(h or '<p class="muted">Lessons will accumulate through reviewed project closeout.</p>')+'</div>')

@app.get("/learning/core",response_class=HTMLResponse)
def v43_knowledge_core():
    rows=_v43_rules(); project=sum(1 for r in rows if r["scope_level"]=="PROJECT ONLY"); company=sum(1 for r in rows if r["scope_level"]=="COMPANY STANDARD"); globaln=sum(1 for r in rows if r["scope_level"]=="GLOBAL BUILDCOMMAND RULE"); approved=sum(1 for r in rows if str(r["approval_status"]).upper()=="APPROVED")
    h="".join(f'<div class="action"><span class="badge">{esc(r["scope_level"])}</span> <b>{esc(r["rule_type"])} - {esc(r["subject"])}</b><div>{esc(r["learned_rule"])}</div><div class="small">{esc(r["approval_status"])} - source {esc(r["source_ref"])}</div></div>' for r in rows[:150])
    head=f'<div class="hero"><h1>BuildCommand Construction Knowledge Core</h1><p class="muted">{approved} approved - {project} project-only - {company} company standards - {globaln} controlled global rules.</p><p><b>Knowledge firewall:</b> PROJECT ONLY can never silently become COMPANY STANDARD or GLOBAL. Promotion requires human approval.</p></div>'
    return shell("Knowledge Core",head+'<div class="card">'+(h or '<p class="muted">The controlled knowledge core is ready to learn from approved construction decisions.</p>')+'</div>')


# ============================================================
# v44 PRECONSTRUCTION & BID INTELLIGENCE
# ============================================================

def _v44_ensure_tables():
    c=db()
    if DATABASE_KIND=="postgres":
        pk="BIGSERIAL PRIMARY KEY"; num="DOUBLE PRECISION"
    else:
        pk="INTEGER PRIMARY KEY"; num="REAL"
    stmts=[
      f"""CREATE TABLE IF NOT EXISTS pursuit_intelligence(
        id {pk},company_id BIGINT,project_id BIGINT,client TEXT,project_type TEXT,
        location TEXT,bid_due TEXT,estimated_value {num} DEFAULT 0,competition TEXT,
        strategic_fit {num} DEFAULT 0,risk_score {num} DEFAULT 0,win_score {num} DEFAULT 0,
        recommendation TEXT DEFAULT 'REVIEW',notes TEXT,created TEXT,updated TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS bid_invites(
        id {pk},company_id BIGINT,project_id BIGINT,trade TEXT,sub_name TEXT,
        contact TEXT,invite_date TEXT,due_date TEXT,status TEXT DEFAULT 'PLANNED',
        response TEXT,notes TEXT,created TEXT,updated TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS bid_proposals(
        id {pk},company_id BIGINT,project_id BIGINT,trade TEXT,sub_name TEXT,
        base_bid {num} DEFAULT 0,alternates {num} DEFAULT 0,allowances {num} DEFAULT 0,
        exclusions TEXT,qualifications TEXT,scope_coverage {num} DEFAULT 0,
        risk_score {num} DEFAULT 0,status TEXT DEFAULT 'REVIEW',received_date TEXT,
        notes TEXT,created TEXT,updated TEXT)""",
      f"""CREATE TABLE IF NOT EXISTS bid_packages(
        id {pk},company_id BIGINT,project_id BIGINT,trade TEXT,title TEXT,
        scope_text TEXT,source_count INTEGER DEFAULT 0,status TEXT DEFAULT 'DRAFT',
        issued_date TEXT,due_date TEXT,created TEXT,updated TEXT)"""
    ]
    for s in stmts:
        c.execute(s)
    c.commit(); c.close()

def _v44_scope_by_trade(pid):
    return _v39_rows(
        "SELECT trade,COUNT(*) AS n FROM blueprint_scope_items WHERE project_id=? GROUP BY trade ORDER BY trade",
        (pid,)
    )

def _v44_estimate_by_trade(pid):
    rows=_v39_rows("""
        SELECT trade,
               SUM(COALESCE(quantity,0)*COALESCE(material_unit_cost,0)) AS material,
               SUM(COALESCE(quantity,0)*COALESCE(labor_unit_cost,0)) AS labor,
               SUM(COALESCE(subcontract_quote,0)) AS subquote,
               SUM(COALESCE(allowance,0)) AS allowance
        FROM estimator_items
        WHERE project_id=?
        GROUP BY trade ORDER BY trade
    """,(pid,))
    return rows

def _v44_historical_cost_by_trade():
    _v43_ensure_tables()
    return _v39_rows("""
        SELECT trade,AVG(COALESCE(unit_cost,0)) AS avg_unit_cost,
               COUNT(*) AS samples,SUM(COALESCE(total_cost,0)) AS total_history
        FROM historical_costs
        WHERE company_id=? AND COALESCE(approval_status,'APPROVED')='APPROVED'
        GROUP BY trade ORDER BY trade
    """,(current_company_id(),))

def _v44_bid_coverage(pid):
    _v44_ensure_tables()
    scopes=_v44_scope_by_trade(pid)
    invites=_v39_rows("SELECT * FROM bid_invites WHERE company_id=? AND project_id=?",(current_company_id(),pid))
    proposals=_v39_rows("SELECT * FROM bid_proposals WHERE company_id=? AND project_id=?",(current_company_id(),pid))
    out=[]
    for r in scopes:
        trade=str(r["trade"] or "")
        ti=[x for x in invites if str(x["trade"] or "").lower()==trade.lower()]
        tp=[x for x in proposals if str(x["trade"] or "").lower()==trade.lower()]
        status="PRICED" if tp else "INVITED" if ti else "NO COVERAGE"
        out.append((trade,int(r["n"] or 0),len(ti),len(tp),status))
    return out

def _v44_scope_gaps(pid):
    rows=_v39_rows("""
        SELECT * FROM blueprint_scope_items
        WHERE project_id=? AND (
            COALESCE(confidence,'MEDIUM') IN ('LOW','MEDIUM')
            OR COALESCE(related_trade,'')!=''
        )
        ORDER BY trade,id LIMIT 150
    """,(pid,))
    return rows

def _v44_allowances(pid):
    rows=_v39_rows("""
        SELECT * FROM estimator_items
        WHERE project_id=? AND (
            COALESCE(allowance,0)>0
            OR LOWER(COALESCE(description,'')) LIKE '%allowance%'
            OR LOWER(COALESCE(description,'')) LIKE '%alternate%'
            OR LOWER(COALESCE(description,'')) LIKE '%owner furnished%'
        )
        ORDER BY trade,id
    """,(pid,))
    return rows

def _v44_long_leads(pid):
    rows=_v39_rows("""
        SELECT * FROM procurement
        WHERE project_id=? AND COALESCE(status,'OPEN') NOT IN ('DELIVERED','COMPLETE','CLOSED')
        ORDER BY required_on_site
    """,(pid,))
    return rows

def _v44_precon_risk(pid):
    coverage=_v44_bid_coverage(pid)
    gaps=_v44_scope_gaps(pid)
    est=_v44_estimate_by_trade(pid)
    longleads=_v44_long_leads(pid)
    uncovered=sum(1 for x in coverage if x[4]=="NO COVERAGE")
    unverified=_v37_snapshot(pid)["review"]
    score=min(100,uncovered*10+len(gaps)*2+unverified*2+len(longleads)*4)
    level="CRITICAL" if score>=70 else "HIGH" if score>=45 else "MEDIUM" if score>=20 else "LOW"
    return {"score":score,"level":level,"uncovered":uncovered,"gaps":len(gaps),"unverified":unverified,"longleads":len(longleads)}

def _v44_pursuit(pid):
    _v44_ensure_tables()
    rows=_v39_rows("SELECT * FROM pursuit_intelligence WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1",(current_company_id(),pid))
    return rows[0] if rows else None

@app.get("/preconstruction",response_class=HTMLResponse)
def v44_preconstruction():
    pid=project_id(); _v44_ensure_tables()
    risk=_v44_precon_risk(pid); coverage=_v44_bid_coverage(pid); gaps=_v44_scope_gaps(pid)
    proposals=_v39_rows("SELECT * FROM bid_proposals WHERE company_id=? AND project_id=?",(current_company_id(),pid))
    allowances=_v44_allowances(pid); longleads=_v44_long_leads(pid)
    body=f'<div class="hero"><div class="eyebrow">BuildCommand v44 - Preconstruction & Bid Intelligence</div><h1>Decide what to bid, what it should cost, and where the risk is.</h1><p class="muted">Preconstruction risk {risk["level"]} ({risk["score"]}/100) - {risk["uncovered"]} trades without bid coverage - {len(proposals)} proposals - {len(gaps)} scope review items - {len(allowances)} allowance/alternate items - {len(longleads)} long-lead signals.</p></div><div class="grid3">'
    cards=[
      ("Pursuit / Go-No-Go","Should we chase this project? Score fit, risk, competition and win potential.","/preconstruction/pursuit"),
      ("Scope Completeness","What is in the job, what is unclear, and what might be missing.","/preconstruction/scope"),
      ("Estimate Benchmark","Compare current estimate structure with approved historical company costs.","/preconstruction/benchmark"),
      ("Bid Coverage","Which trades are covered, invited, priced or completely exposed.","/preconstruction/coverage"),
      ("Bidder Strategy","Build the subcontractor pursuit list by trade and performance history.","/preconstruction/bidders"),
      ("Bid Packages","Turn cleaned Blueprint Brain scopes into trade bid-package readiness.","/preconstruction/packages"),
      ("Bid Leveling","Compare subcontractor pricing, coverage, exclusions and qualifications.","/preconstruction/leveling"),
      ("Allowances & Alternates","Separate uncertain money before it disappears into the estimate.","/preconstruction/allowances"),
      ("Long-Lead / Schedule Risk","Find procurement items that can hurt the proposed schedule.","/preconstruction/long-lead"),
      ("Preconstruction Risk","One consolidated view of scope, pricing, coverage and schedule exposure.","/preconstruction/risk")
    ]
    for name,desc,href in cards:
        body+=_v37_link_card(name,desc,href,"Open")
    body+='</div>'
    return shell("Preconstruction",body)

@app.get("/preconstruction/pursuit",response_class=HTMLResponse)
def v44_pursuit_page():
    pid=project_id(); p=_v44_pursuit(pid); risk=_v44_precon_risk(pid)
    if p:
        win=float(p["win_score"] or 0); fit=float(p["strategic_fit"] or 0)
        rec=p["recommendation"] or "REVIEW"
        detail=f'<div class="grid3"><div class="card"><div class="label">Win Score</div><div class="kpi">{win:g}</div></div><div class="card"><div class="label">Strategic Fit</div><div class="kpi">{fit:g}</div></div><div class="card"><div class="label">Recommendation</div><div class="kpi">{esc(rec)}</div></div></div><div class="card"><p>{esc(p["notes"])}</p></div>'
    else:
        detail=f'<div class="card"><span class="badge WATCH">REVIEW</span><h2>Go / No-Go candidate</h2><p>Current preconstruction risk: <b>{risk["level"]}</b> ({risk["score"]}/100).</p><p class="muted">Add client, competition, strategic fit, project value and win probability before making a pursuit decision.</p></div>'
    return shell("Pursuit Intelligence",'<div class="hero"><h1>Pursuit / Go-No-Go Brain</h1><p class="muted">Do not burn estimating time on every opportunity. Score whether the project is worth chasing.</p></div>'+detail)

@app.get("/preconstruction/scope",response_class=HTMLResponse)
def v44_scope_page():
    rows=_v44_scope_gaps(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(r["confidence"])}</span> <b>{esc(r["trade"])}</b> - {esc(r["requirement"])}<div class="small">{esc(r["source_sheet"])} {esc(r["source_detail"])} {esc(r["source_spec"])}'+(f' - Related trade: {esc(r["related_trade"])}' if r["related_trade"] else '')+'</div></div>' for r in rows)
    return shell("Scope Completeness",'<div class="hero"><h1>Scope Completeness Brain</h1><p class="muted">Focus estimating attention on ambiguous, cross-trade and lower-confidence requirements.</p></div><div class="card">'+(h or '<p class="muted">No obvious scope-completeness exceptions detected.</p>')+'</div>')

@app.get("/preconstruction/benchmark",response_class=HTMLResponse)
def v44_benchmark_page():
    current=_v44_estimate_by_trade(project_id()); hist=_v44_historical_cost_by_trade()
    hm={str(r["trade"] or "").lower():r for r in hist}; h=""
    for r in current:
        trade=str(r["trade"] or ""); current_total=float(r["material"] or 0)+float(r["labor"] or 0)+float(r["subquote"] or 0)+float(r["allowance"] or 0)
        old=hm.get(trade.lower())
        hist_text=f'{int(old["samples"] or 0)} historical samples - avg unit cost ${float(old["avg_unit_cost"] or 0):,.2f}' if old else 'No approved historical benchmark'
        h+=f'<div class="action"><b>{esc(trade)}</b><div class="small">Current estimator total ${current_total:,.0f} - {esc(hist_text)}</div></div>'
    return shell("Estimate Benchmark",'<div class="hero"><h1>Estimate Benchmark Brain</h1><p class="muted">Historical costs inform review; they never replace current project takeoff or subcontractor pricing.</p></div><div class="card">'+(h or '<p class="muted">No current estimate data.</p>')+'</div>')

@app.get("/preconstruction/coverage",response_class=HTMLResponse)
def v44_coverage_page():
    rows=_v44_bid_coverage(project_id())
    h="".join(f'<div class="action"><span class="badge {"READY" if status=="PRICED" else "WATCH"}">{esc(status)}</span> <b>{esc(trade)}</b><div class="small">{n} scope items - {invites} invite(s) - {proposals} proposal(s)</div></div>' for trade,n,invites,proposals,status in rows)
    return shell("Bid Coverage",'<div class="hero"><h1>Bid Coverage Brain</h1><p class="muted">Every trade should have deliberate pricing coverage before bid day.</p></div><div class="card">'+(h or '<p class="muted">Analyze project scope first.</p>')+'</div>')

@app.get("/preconstruction/bidders",response_class=HTMLResponse)
def v44_bidders_page():
    pid=project_id()
    subs=_v39_rows("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(pid,))
    perf=_v39_rows("SELECT * FROM subcontractor_performance WHERE company_id=? AND COALESCE(approval_status,'PROPOSED')='APPROVED' ORDER BY trade,sub_name",(current_company_id(),))
    pm={(str(r["sub_name"] or "").lower(),str(r["trade"] or "").lower()):r for r in perf}
    h=""
    for s in subs:
        p=pm.get((str(s["name"] or "").lower(),str(s["trade"] or "").lower()))
        score=""
        if p:
            vals=[float(p[k] or 0) for k in ["schedule_score","quality_score","safety_score","responsiveness_score","closeout_score"]]
            score=f' - Performance avg {sum(vals)/len(vals):.1f}'
        h+=f'<div class="action"><b>{esc(s["name"])} - {esc(s["trade"])}</b><div class="small">Bidder candidate{esc(score)}</div></div>'
    return shell("Bidder Strategy",'<div class="hero"><h1>Bidder Strategy Brain</h1><p class="muted">Build the bidder list using trade coverage and approved performance history, not price alone.</p></div><div class="card">'+(h or '<p class="muted">No subcontractors loaded for this project.</p>')+'</div>')

@app.get("/preconstruction/packages",response_class=HTMLResponse)
def v44_packages_page():
    rows=_v44_scope_by_trade(project_id())
    h="".join(f'<div class="action"><span class="badge READY">READY TO BUILD</span> <b>{esc(r["trade"])}</b><div class="small">{int(r["n"] or 0)} source-backed scope items can feed a trade bid package.</div></div>' for r in rows)
    return shell("Bid Packages",'<div class="hero"><h1>Bid Package Brain</h1><p class="muted">Clean Blueprint Brain scope becomes the foundation of each subcontractor bid package.</p></div><div class="card">'+(h or '<p class="muted">Analyze project documents first.</p>')+'</div>')

@app.get("/preconstruction/leveling",response_class=HTMLResponse)
def v44_leveling_page():
    _v44_ensure_tables()
    rows=_v39_rows("SELECT * FROM bid_proposals WHERE company_id=? AND project_id=? ORDER BY trade,base_bid",(current_company_id(),project_id()))
    h="".join(f'<div class="action"><span class="badge {"READY" if float(r["scope_coverage"] or 0)>=90 else "WATCH"}">{float(r["scope_coverage"] or 0):g}% COVERAGE</span> <b>{esc(r["trade"])} - {esc(r["sub_name"])}</b><div class="small">Base ${float(r["base_bid"] or 0):,.0f} - Alternates ${float(r["alternates"] or 0):,.0f} - Allowances ${float(r["allowances"] or 0):,.0f} - Risk {float(r["risk_score"] or 0):g}</div><p><b>Exclusions:</b> {esc(r["exclusions"])}</p><p><b>Qualifications:</b> {esc(r["qualifications"])}</p></div>' for r in rows)
    return shell("Bid Leveling",'<div class="hero"><h1>Subcontractor Bid Leveling Brain</h1><p class="muted">Lowest number does not mean lowest risk. Compare scope coverage, exclusions, qualifications and price.</p></div><div class="card">'+(h or '<p class="muted">No subcontractor proposals loaded yet.</p>')+'</div>')

@app.get("/preconstruction/allowances",response_class=HTMLResponse)
def v44_allowance_page():
    rows=_v44_allowances(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">REVIEW</span> <b>{esc(r["trade"])}</b> - {esc(r["description"])}<div class="small">Allowance ${float(r["allowance"] or 0):,.0f} - {esc(r["source_ref"])}</div></div>' for r in rows)
    return shell("Allowances & Alternates",'<div class="hero"><h1>Allowances & Alternates Brain</h1><p class="muted">Keep uncertain money, owner-furnished items and alternates visible before final bid.</p></div><div class="card">'+(h or '<p class="muted">No allowance/alternate signals detected.</p>')+'</div>')

@app.get("/preconstruction/long-lead",response_class=HTMLResponse)
def v44_longlead_page():
    rows=_v44_long_leads(project_id())
    h="".join(f'<div class="action"><span class="badge WATCH">{esc(r["status"])}</span> <b>{esc(r["item"])}</b><div class="small">Required onsite {esc(r["required_on_site"])} - Promised {esc(r["promised_date"])} - {esc(r["vendor"])}</div></div>' for r in rows)
    return shell("Long Lead",'<div class="hero"><h1>Long-Lead / Schedule Risk Brain</h1><p class="muted">Preconstruction should identify procurement threats before the baseline schedule is committed.</p></div><div class="card">'+(h or '<p class="muted">No long-lead procurement items loaded yet.</p>')+'</div>')

@app.get("/preconstruction/risk",response_class=HTMLResponse)
def v44_risk_page():
    r=_v44_precon_risk(project_id())
    body=f'<div class="hero"><h1>Preconstruction Risk Brain</h1><p class="muted">{r["level"]} risk - Score {r["score"]}/100.</p></div><div class="grid4"><div class="card"><div class="label">Uncovered Trades</div><div class="kpi">{r["uncovered"]}</div></div><div class="card"><div class="label">Scope Review</div><div class="kpi">{r["gaps"]}</div></div><div class="card"><div class="label">Unverified Estimate</div><div class="kpi">{r["unverified"]}</div></div><div class="card"><div class="label">Long-Lead Signals</div><div class="kpi">{r["longleads"]}</div></div></div>'
    return shell("Preconstruction Risk",body)


# ============================================================
# v44.2 EXISTING BLUEPRINT DATA NORMALIZATION
# ============================================================

def _v442_normalize_existing_blueprint(pid):
    """Normalize duplicate trade labels and re-run ownership on existing saved Blueprint Brain items."""
    c=db()
    items=c.execute("SELECT * FROM blueprint_scope_items WHERE company_id=? AND project_id=? ORDER BY run_id,id",
                    (current_company_id(),pid)).fetchall()
    changed=0
    for item in items:
        req=str(item["requirement"] or "")
        if _v442_exclude_scope_item(req):
            c.execute("DELETE FROM blueprint_scope_items WHERE id=?",(item["id"],))
            changed+=1
            continue
        target=_v441_primary_trade(req,_v33_normalize_trade(item["trade"]))
        target=_v441_apply_approved_learning(pid,req,target)
        if target != item["trade"]:
            run_id=item["run_id"]
            parent=c.execute("SELECT * FROM blueprint_trade_scopes WHERE run_id=? AND company_id=? AND project_id=? AND trade=? LIMIT 1",
                             (run_id,current_company_id(),pid,target)).fetchone()
            if parent:
                target_id=parent["id"]
            else:
                div={"Demolition":"02","Concrete":"03","Roofing":"07","Doors / Frames / Hardware":"08",
                     "Storefront / Glazing":"08","Framing / Drywall":"09","Ceilings":"09",
                     "Flooring / Tile":"09","Painting":"09","Toilet / Bath Accessories":"10",
                     "Specialties":"10","Fire Sprinkler":"21","Plumbing":"22",
                     "HVAC / Mechanical":"23","Controls":"23","Electrical":"26",
                     "Low Voltage":"27","Fire Alarm":"28"}.get(target,"")
                now=datetime.utcnow().isoformat()
                c.execute("INSERT INTO blueprint_trade_scopes(company_id,project_id,run_id,trade,division,summary,scope_text,item_count,created) VALUES(?,?,?,?,?,?,?,?,?)",
                          (current_company_id(),pid,run_id,target,div,f"BuildCommand source-backed scope for {target}.","",0,now))
                target_id=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.execute("UPDATE blueprint_scope_items SET trade=?,trade_scope_id=? WHERE id=?",
                      (target,target_id,item["id"]))
            changed+=1

    # Canonicalize parent labels and remove empty/duplicate parents safely.
    scopes=c.execute("SELECT * FROM blueprint_trade_scopes WHERE company_id=? AND project_id=? ORDER BY run_id,id",
                     (current_company_id(),pid)).fetchall()
    for sc in scopes:
        canonical=_v33_normalize_trade(sc["trade"])
        if canonical != sc["trade"]:
            other=c.execute("SELECT * FROM blueprint_trade_scopes WHERE run_id=? AND company_id=? AND project_id=? AND trade=? AND id<>? LIMIT 1",
                            (sc["run_id"],current_company_id(),pid,canonical,sc["id"])).fetchone()
            if other:
                c.execute("UPDATE blueprint_scope_items SET trade_scope_id=?,trade=? WHERE trade_scope_id=?",
                          (other["id"],canonical,sc["id"]))
                c.execute("DELETE FROM blueprint_trade_scopes WHERE id=?",(sc["id"],))
            else:
                c.execute("UPDATE blueprint_trade_scopes SET trade=? WHERE id=?",(canonical,sc["id"]))
                c.execute("UPDATE blueprint_scope_items SET trade=? WHERE trade_scope_id=?",(canonical,sc["id"]))

    scopes=c.execute("SELECT id FROM blueprint_trade_scopes WHERE company_id=? AND project_id=?",
                     (current_company_id(),pid)).fetchall()
    for sc in scopes:
        n=c.execute("SELECT COUNT(*) n FROM blueprint_scope_items WHERE trade_scope_id=?",(sc["id"],)).fetchone()["n"]
        if n==0:
            c.execute("DELETE FROM blueprint_trade_scopes WHERE id=?",(sc["id"],))
        else:
            c.execute("UPDATE blueprint_trade_scopes SET item_count=? WHERE id=?",(n,sc["id"]))
    c.commit(); c.close()
    return changed

@app.post("/blueprint-brain/final-cleanup")
def v442_run_final_cleanup():
    pid=project_id()
    _v442_normalize_existing_blueprint(pid)
    return RedirectResponse("/blueprint-brain",status_code=303)

@app.get("/build",response_class=HTMLResponse)
def unified_build():
    s=_v37_snapshot(project_id())
    body=(
      '<div class="hero"><div class="eyebrow">BUILD</div><h1>Understand the project.</h1><p class="muted">One place for plans, specs and scope intelligence.</p></div>'
      f'<div class="card"><div class="label">Source-backed Scope Items</div><div class="kpi">{s["scope"]}</div></div>'
      '<div class="grid2">'
      +_v37_link_card("Analyze Project","Upload once. BuildCommand runs plan intelligence, estimator sync, takeoff splitting and quantity review automatically.","/build/analyze-project","Analyze")
      +_v37_link_card("Review Project Scope","Review unified source-backed construction intelligence.","/brain","Review")
      +'</div><details class="card"><summary><b>More Build tools</b></summary><p><a href="/preconstruction">Preconstruction & Bid Intelligence</a> · <a href="/documents">Documents</a> · <a href="/document-ai">Deep Document AI</a></p></details>'
    )
    return shell("Build",body)

@app.get("/estimate",response_class=HTMLResponse)
def unified_estimate():
    s=_v37_snapshot(project_id())
    body=(
      '<div class="hero"><div class="eyebrow">ESTIMATE</div><h1>Scope to price.</h1><p class="muted">The takeoff brains work underneath; review what needs estimator judgment.</p></div>'
      f'<div class="grid2"><div class="card"><div class="label">Estimator Items</div><div class="kpi">{s["estimate"]}</div></div>'
      f'<div class="card"><div class="label">Need Review</div><div class="kpi">{s["review"]}</div></div></div>'
      '<div class="grid2">'
      +_v37_link_card("Estimator Workspace","Scope, quantity, unit, labor, material, subcontract quote and markup.","/brain/estimator","Open Estimate")
      +_v37_link_card("Takeoff Review","Review AI quantity proposals and verification items.","/brain/takeoff","Review")
      +'</div><details class="card"><summary><b>Advanced estimating tools</b></summary><p><a href="/brain/takeoff/components">Takeoff Components</a> · <a href="/cost-intelligence">Cost Intelligence</a></p></details>'
    )
    return shell("Estimate",body)

@app.get("/manage",response_class=HTMLResponse)
def unified_manage():
    s=_v37_snapshot(project_id())
    attention=s["issues"]+s["submittals"]+s["actions"]+s["inspections"]
    body=(
      '<div class="hero"><div class="eyebrow">MANAGE</div><h1>Run the job.</h1><p class="muted">Schedule, field, RFIs, submittals, inspections and subcontractors.</p></div>'
      f'<div class="grid4"><div class="card"><div class="label">Need Attention</div><div class="kpi">{attention}</div></div>'
      f'<div class="card"><div class="label">Issues</div><div class="kpi">{s["issues"]}</div></div>'
      f'<div class="card"><div class="label">Submittals</div><div class="kpi">{s["submittals"]}</div></div>'
      f'<div class="card"><div class="label">Inspections</div><div class="kpi">{s["inspections"]}</div></div></div>'
      '<div class="grid3">'
      +_v37_link_card("Today","Superintendent Field Command: readiness, crews, deliveries, inspections and decisions.","/field-command")
      +_v37_link_card("Schedule","Schedule and production planning.","/schedule")
      +_v37_link_card("Things That Need You","One queue for human decisions.","/actions")
      +_v37_link_card("RFIs / Issues","Questions, conflicts and issues.","/issues")
      +_v37_link_card("Submittals","Submittal workflow.","/submittals")
      +_v37_link_card("Field","Field execution and reporting.","/field")
      +'</div><details class="card"><summary><b>More management tools</b></summary><p><a href="/learning">Learning Intelligence</a> · <a href="/project-control">Project Control</a> · <a href="/intelligence">Intelligence Center</a> · <a href="/inspections">Inspections</a> · <a href="/safety">Safety</a> · <a href="/subcontractors">Subcontractors</a> · <a href="/procurement">Procurement</a> · <a href="/punch">Punch</a></p></details>'
    )
    return shell("Manage",body)

@app.get("/ask-buildcommand",response_class=HTMLResponse)
def unified_ask_buildcommand():
    body=(
      '<div class="hero"><div class="eyebrow">ASK BUILDCOMMAND</div><h1>Ask the project.</h1><p class="muted">Use natural language instead of hunting through menus.</p></div>'
      '<div class="grid2">'
      +_v37_link_card("Project Command","Ask about scope, schedule, risks, trades and project status.","/ai-command","Ask")
      +_v37_link_card("Search Everything","Find information across the project.","/global-search","Search")
      +'</div><div class="card"><h2>Try asking</h2><div class="action">What am I missing in electrical?</div><div class="action">What needs my attention today?</div><div class="action">Show me everything affecting doors.</div><div class="action">What could delay this week?</div></div>'
    )
    return shell("Ask BuildCommand",body)

@app.get("/legacy-dashboard",response_class=HTMLResponse)
def home():
    pid = project_id()
    ensure_today_morning_brief(pid)
    c = db()
    today = date.today().isoformat()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    risks = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    make_ready_items = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY m.due
        """,
        (pid,)
    ).fetchall()

    actions = c.execute(
        """
        SELECT *
        FROM action_items
        WHERE project_id=? AND status='OPEN'
        ORDER BY due, id
        """,
        (pid,)
    ).fetchall()

    issues = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM project_issues i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=? AND i.status!='CLOSED'
        ORDER BY i.due, i.id
        """,
        (pid,)
    ).fetchall()

    procurement_rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM procurement p
        LEFT JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=? AND p.status!='DELIVERED'
        ORDER BY p.required_on_site, p.id
        """,
        (pid,)
    ).fetchall()

    inspection_rows = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM inspections_tracker i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=? AND i.result!='PASSED'
        ORDER BY i.scheduled_date, i.id
        """,
        (pid,)
    ).fetchall()

    submittal_rows = c.execute(
        """
        SELECT s.*, a.external_id, a.name activity
        FROM submittals s
        LEFT JOIN activities a ON a.id=s.activity_id
        WHERE s.project_id=? AND s.status NOT IN ('APPROVED','APPROVED_AS_NOTED')
        ORDER BY s.due_date, s.id
        """,
        (pid,)
    ).fetchall()

    safety_rows = c.execute(
        """
        SELECT *
        FROM safety_items
        WHERE project_id=? AND status!='CLOSED'
        ORDER BY id DESC
        """,
        (pid,)
    ).fetchall()

    change_rows = c.execute(
        """
        SELECT *
        FROM change_events
        WHERE project_id=? AND status NOT IN ('APPROVED','REJECTED')
        ORDER BY id DESC
        """,
        (pid,)
    ).fetchall()

    latest_report = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC,id DESC
        LIMIT 1
        """,
        (pid,)
    ).fetchone()

    c.close()

    critical_risks = [r for r in risks if r["band"] == "CRITICAL"]
    high_risks = [r for r in risks if r["band"] == "HIGH"]

    overdue_actions = [
        a for a in actions
        if a["due"] and a["due"] < today
    ]

    overdue_issues = [
        i for i in issues
        if i["due"] and i["due"] < today
    ]

    procurement_critical = []
    for p in procurement_rows:
        badge, risk_text = procurement_risk(
            p["required_on_site"],
            p["promised_date"],
            p["status"]
        )
        if badge == "CRITICAL":
            procurement_critical.append((p, risk_text))

    failed_inspections = [
        i for i in inspection_rows
        if i["result"] == "FAILED"
    ]

    overdue_inspections = [
        i for i in inspection_rows
        if i["scheduled_date"]
        and i["scheduled_date"] < today
        and i["result"] in ["PENDING", "SCHEDULED"]
    ]

    rejected_submittals = [
        s for s in submittal_rows
        if s["status"] == "REJECTED"
    ]

    overdue_submittals = [
        s for s in submittal_rows
        if s["due_date"]
        and s["due_date"] < today
        and s["status"] in ["PENDING", "SUBMITTED"]
    ]

    critical_safety = [
        s for s in safety_rows
        if s["severity"] == "CRITICAL"
    ]

    open_change_cost = sum((r["estimated_cost"] or 0) for r in change_rows)
    open_change_days = sum((r["schedule_days"] or 0) for r in change_rows)

    attention_score = min(
        100,
        len(critical_risks) * 20
        + len(high_risks) * 10
        + len(overdue_actions) * 8
        + len(overdue_issues) * 8
        + len(procurement_critical) * 12
        + len(failed_inspections) * 15
        + len(overdue_inspections) * 8
        + len(rejected_submittals) * 10
        + len(overdue_submittals) * 6
        + len(critical_safety) * 20
    )

    if attention_score >= 70:
        overall_badge = "CRITICAL"
        overall_text = "Immediate Attention"
    elif attention_score >= 40:
        overall_badge = "HIGH"
        overall_text = "Needs Attention"
    elif attention_score >= 15:
        overall_badge = "WATCH"
        overall_text = "Watch"
    else:
        overall_badge = "READY"
        overall_text = "Stable"

    priority_items = []

    for s in critical_safety[:2]:
        priority_items.append(
            ("CRITICAL", "Safety", s["title"], f'{s["location"] or "No location"}')
        )

    for r in critical_risks[:3]:
        priority_items.append(
            ("CRITICAL", "Risk", r["activity"], r["explanation"])
        )

    for i in failed_inspections[:2]:
        priority_items.append(
            ("CRITICAL", "Inspection", i["inspection_type"], i["notes"] or "Failed inspection")
        )

    for p, risk_text in procurement_critical[:2]:
        priority_items.append(
            ("CRITICAL", "Procurement", p["item"], risk_text)
        )

    for a in overdue_actions[:3]:
        priority_items.append(
            (
                a["priority"] if a["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "HIGH",
                "Action",
                a["title"],
                f'Owner: {a["owner"] or "Unassigned"} · Due {a["due"]}'
            )
        )

    for i in overdue_issues[:2]:
        priority_items.append(
            (
                i["priority"] if i["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "HIGH",
                i["issue_type"],
                i["title"],
                f'Owner: {i["owner"] or "Unassigned"} · Due {i["due"]}'
            )
        )

    for s in rejected_submittals[:2]:
        priority_items.append(
            ("HIGH", "Submittal", s["title"], "Rejected / revise and resubmit")
        )

    for m in make_ready_items[:3]:
        priority_items.append(
            (
                m["priority"] if m["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "HIGH",
                "Make Ready",
                m["title"],
                f'Due {m["due"]}'
            )
        )

    if not priority_items:
        priority_items.append(
            ("READY", "Command", "No urgent issues detected", "Review today’s plan and upcoming work.")
        )

    priority_html = "".join(
        f"""
        <div class="action">
            <span class="badge {badge}">{esc(category)}</span>
            <b>{esc(title)}</b>
            <div class="small">{esc(detail)}</div>
        </div>
        """
        for badge, category, title, detail in priority_items[:10]
    )

    latest_report_html = (
        f"""
        <div class="small">Latest Daily Report: {esc(latest_report["report_date"])}</div>
        <p><b>Manpower:</b> {latest_report["manpower"] or 0}</p>
        <p><b>Work Completed:</b> {esc(latest_report["work_completed"]) or "—"}</p>
        <p><b>Delays:</b> {esc(latest_report["delays"]) or "—"}</p>
        <p><b>Tomorrow:</b> {esc(latest_report["tomorrow_plan"]) or "—"}</p>
        """
        if latest_report
        else '<div class="muted">No daily report submitted yet.</div>'
    )

    project_name = esc(project["name"]) if project else "Current Project"
    project_number = esc(project["number"]) if project else ""

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;gap:18px;align-items:flex-start;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Morning Command Center</div>
                <h1>What needs attention today?</h1>
                <div class="muted">
                    {project_number} - {project_name}
                </div>
            </div>

            <div style="text-align:right;">
                <span class="badge {overall_badge}">{overall_text}</span>
                <div class="kpi" style="margin-top:8px;">{attention_score}</div>
                <div class="small">Project attention score</div>
            </div>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Critical Risks</div>
            <div class="kpi">{len(critical_risks)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue Actions</div>
            <div class="kpi">{len(overdue_actions)}</div>
        </div>

        <div class="card">
            <div class="label">Open Make-Ready</div>
            <div class="kpi">{len(make_ready_items)}</div>
        </div>

        <div class="card">
            <div class="label">Failed Inspections</div>
            <div class="kpi">{len(failed_inspections)}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Handle First</h2>
            {priority_html}
        </div>

        <div class="card">
            <h2>Latest Field Signal</h2>
            {latest_report_html}

            <div style="margin-top:14px;">
                <a href="/assistant"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Ask BuildCommand AI →
                </a>
            </div>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Procurement Critical</div>
            <div class="kpi">{len(procurement_critical)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue RFIs / Issues</div>
            <div class="kpi">{len(overdue_issues)}</div>
        </div>

        <div class="card">
            <div class="label">Submittal Problems</div>
            <div class="kpi">{len(rejected_submittals) + len(overdue_submittals)}</div>
        </div>

        <div class="card">
            <div class="label">Critical Safety</div>
            <div class="kpi">{len(critical_safety)}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Change Exposure</h2>

            <div class="grid2">
                <div>
                    <div class="label">Potential Cost</div>
                    <div class="kpi">${open_change_cost:,.0f}</div>
                </div>

                <div>
                    <div class="label">Potential Days</div>
                    <div class="kpi">{open_change_days:.1f}</div>
                </div>
            </div>

            <div style="margin-top:12px;">
                <a href="/changes"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Review Change Events →
                </a>
            </div>
        </div>

        <div class="card">
            <h2>Quick Command</h2>

            <div class="action">
                <a href="/daily-report" style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Daily Report →
                </a>
            </div>

            <div class="action">
                <a href="/schedule-health" style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Schedule Health →
                </a>
            </div>

            <div class="action">
                <a href="/readiness" style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Lookahead Readiness →
                </a>
            </div>

            <div class="action">
                <a href="/actions" style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Action Center →
                </a>
            </div>
        </div>
    </div>
    """

    return shell("Daily Command", body)


@app.get("/schedule",response_class=HTMLResponse)
def schedule():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); c.close()
    tr="".join(
        f'<tr>'
        f'<td>{esc(r["external_id"])}</td>'
        f'<td><b>{esc(r["name"])}</b></td>'
        f'<td>{esc(r["trade"])}</td>'
        f'<td>{r["start"]}</td>'
        f'<td>{r["finish"]}</td>'
        f'<td>{r["pct"]:.0f}%</td>'
        f'<td>{esc(r["status"])}</td>'
        f'<td><a href="/activities/{r["id"]}/edit" style="color:#f0b44d;text-decoration:none;font-weight:700;">Edit</a></td>'
        f'</tr>'
        for r in rows
    )
    return shell("Schedule",f'<div class="hero"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;"><div><div class="eyebrow">Schedule + Lookahead</div><h1>Field execution plan</h1></div><a href="/activities/new" style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">+ Add Activity</a></div></div><div class="card"><table><tr><th>ID</th><th>Activity</th><th>Trade</th><th>Start</th><th>Finish</th><th>%</th><th>Status</th><th>Action</th></tr>{tr}</table></div>')

@app.get("/make-ready",response_class=HTMLResponse)
def make_ready():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY
            CASE m.priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                ELSE 4
            END,
            m.due
        """,
        (pid,)
    ).fetchall()

    closed = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='COMPLETE'
        ORDER BY m.id DESC
        LIMIT 10
        """,
        (pid,)
    ).fetchall()

    c.close()

    open_html = "".join(
        f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {r["priority"]}">{r["priority"]}</span>
                    <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
                </div>

                <form method="post" action="/make-ready/{r["id"]}/close">
                    <button type="submit">Mark Cleared</button>
                </form>
            </div>

            <h3>{esc(r["title"])}</h3>
            <p>{esc(r["reason"])}</p>
            <div class="small">Clear by {esc(r["due"])}</div>
        </div>
        """
        for r in rows
    ) or '<div class="card"><div class="muted">No open make-ready items. Nice work.</div></div>'

    closed_html = "".join(
        f"""
        <div class="action">
            <span class="badge COMPLETE">CLEARED</span>
            <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
            <div>{esc(r["title"])}</div>
        </div>
        """
        for r in closed
    ) or '<div class="muted">No recently cleared items.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Predictive Make-Ready</div>
                <h1>Clear tomorrow's blockers before they hit the field.</h1>
            </div>

            <a href="/make-ready/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Make-Ready
            </a>
        </div>
    </div>

    <div class="grid2">
        <div>
            <h2>Open Blockers</h2>
            {open_html}
        </div>

        <div class="card">
            <h2>Recently Cleared</h2>
            {closed_html}
        </div>
    </div>
    """

    return shell("Make Ready", body)

@app.get("/field",response_class=HTMLResponse)
def field():
    pid=project_id(); c=db(); acts=c.execute("SELECT id,external_id,name FROM activities WHERE project_id=?",(pid,)).fetchall(); updates=c.execute("SELECT f.*,a.name activity FROM field_updates f JOIN activities a ON a.id=f.activity_id WHERE f.project_id=? ORDER BY f.id DESC LIMIT 15",(pid,)).fetchall(); c.close()
    opts="".join(f'<option value="{a["id"]}">{a["external_id"]} - {esc(a["name"])}</option>' for a in acts)
    recent="".join(f'<div class="action"><b>{u["update_type"]} - {esc(u["activity"])}</b><div>{esc(u["text"])}</div><div class="small">{u["created"]}</div></div>' for u in updates) or '<div class="muted">No updates yet.</div>'
    body=f'<div class="hero"><div class="eyebrow">Field Execution</div><h1>Capture what changed today.</h1></div><div class="grid2"><div class="card"><form method="post" action="/field/add"><select name="activity_id">{opts}</select><br><br><select name="update_type"><option>PROGRESS</option><option>BLOCKER</option><option>QUALITY</option><option>INSPECTION</option><option>DELIVERY</option></select><br><br><textarea name="text" placeholder="What changed in the field?"></textarea><br><br><button>Save field update</button></form></div><div class="card"><h2>Recent field updates</h2>{recent}</div></div>'
    return shell("Field",body)

@app.post("/field/add")
def add_field(activity_id:int=Form(...),update_type:str=Form(...),text:str=Form(...)):
    pid=project_id(); c=db(); c.execute("INSERT INTO field_updates(project_id,activity_id,update_type,text,created) VALUES(?,?,?,?,?)",(pid,activity_id,update_type,text,date.today().isoformat())); c.commit(); c.close()
    return RedirectResponse("/field",303)

@app.get("/subcontractors",response_class=HTMLResponse)
def subcontractors():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    updates = c.execute(
        """
        SELECT u.*, s.name AS sub_name, s.trade AS sub_trade
        FROM subcontractor_updates u
        JOIN subs s ON s.id=u.sub_id
        WHERE u.project_id=?
        ORDER BY u.update_date DESC, u.id DESC
        LIMIT 25
        """,
        (pid,)
    ).fetchall()

    c.close()

    latest_by_sub = {}
    for u in updates:
        if u["sub_id"] not in latest_by_sub:
            latest_by_sub[u["sub_id"]] = u

    cards = ""

    for r in rows:
        latest = latest_by_sub.get(r["id"])

        if latest:
            status = latest["status"] or "WATCH"
            status_badge = status if status in ["CRITICAL", "HIGH", "WATCH", "READY", "LOW"] else "OPEN"
            detail = (
                f'<div class="small">Latest Update: {esc(latest["update_date"])}</div>'
                f'<p><b>Manpower:</b> {latest["manpower"] or 0}</p>'
                f'<p><b>Commitment:</b> {esc(latest["commitment"]) or "—"}</p>'
                f'<p><b>Issue:</b> {esc(latest["issue"]) or "—"}</p>'
                f'<span class="badge {status_badge}">{esc(status)}</span>'
            )
        else:
            detail = '<div class="muted">No field update recorded yet.</div>'

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    <h3 style="margin-top:0;">{esc(r["name"])}</h3>
                    <div class="muted">{esc(r["trade"])}</div>
                </div>

                <a href="/subcontractors/{r["id"]}/update"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Log Update
                </a>
            </div>

            <div style="margin-top:14px;">
                {detail}
            </div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No subcontractors added yet.</div></div>'

    recent_html = ""

    for u in updates[:12]:
        status = u["status"] or "WATCH"
        badge = status if status in ["CRITICAL", "HIGH", "WATCH", "READY", "LOW"] else "OPEN"

        recent_html += (
            f'<div class="action">'
            f'<span class="badge {badge}">{esc(status)}</span> '
            f'<b>{esc(u["sub_name"])}</b> · {esc(u["sub_trade"])}'
            f'<div class="small">{esc(u["update_date"])} · Manpower {u["manpower"] or 0}</div>'
            f'<div>{esc(u["commitment"]) or "No commitment entered."}</div>'
            f'<div class="small">{esc(u["issue"]) or "No issue entered."}</div>'
            f'</div>'
        )

    if not recent_html:
        recent_html = '<div class="muted">No subcontractor updates yet.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Subcontractor Intelligence</div>
                <h1>Know who is committed, who is staffed, and who needs attention.</h1>
            </div>

            <a href="/subcontractors/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Subcontractor
            </a>
        </div>
    </div>

    <div class="grid3">
        {cards}
    </div>

    <div class="card">
        <h2>Recent Trade Partner Updates</h2>
        {recent_html}
    </div>
    """

    return shell("Subcontractors", body)


@app.get("/subcontractors/{sub_id}/update", response_class=HTMLResponse)
def subcontractor_update_form(sub_id: int):
    pid = project_id()
    c = db()

    sub = c.execute(
        """
        SELECT *
        FROM subs
        WHERE id=? AND project_id=?
        """,
        (sub_id, pid)
    ).fetchone()

    c.close()

    if not sub:
        return RedirectResponse(url="/subcontractors", status_code=303)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Subcontractor Intelligence</div>
        <h1>Log Trade Partner Update</h1>
        <div class="muted">{esc(sub["name"])} · {esc(sub["trade"])}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/subcontractors/{sub_id}/update">
            <label>Update Date</label>
            <input type="date" name="update_date" value="{date.today().isoformat()}" required>

            <label>Manpower</label>
            <input type="number" name="manpower" min="0" value="0">

            <label>Commitment</label>
            <textarea name="commitment" placeholder="Example: Complete level 2 overhead rough-in by Friday."></textarea>

            <label>Issue / Constraint</label>
            <textarea name="issue" placeholder="Example: Waiting on sleeves and access above ceiling."></textarea>

            <label>Status</label>
            <select name="status">
                <option value="READY">Ready / On Track</option>
                <option value="WATCH">Watch</option>
                <option value="HIGH">High Concern</option>
                <option value="CRITICAL">Critical</option>
                <option value="LOW">Low Concern</option>
            </select>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Update</button>
                <a href="/subcontractors"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Subcontractor Update", body)


@app.post("/subcontractors/{sub_id}/update")
def save_subcontractor_update(
    sub_id: int,
    update_date: str = Form(...),
    manpower: int = Form(0),
    commitment: str = Form(""),
    issue: str = Form(""),
    status: str = Form("WATCH")
):
    pid = project_id()
    c = db()

    sub = c.execute(
        "SELECT id FROM subs WHERE id=? AND project_id=?",
        (sub_id, pid)
    ).fetchone()

    if sub:
        c.execute(
            """
            INSERT INTO subcontractor_updates(
                project_id,
                sub_id,
                update_date,
                manpower,
                commitment,
                issue,
                status,
                created
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                pid,
                sub_id,
                update_date,
                manpower,
                commitment.strip(),
                issue.strip(),
                status,
                date.today().isoformat()
            )
        )
        c.commit()

    c.close()
    return RedirectResponse(url="/subcontractors", status_code=303)


@app.get("/production",response_class=HTMLResponse)
def production():
    pid = project_id()
    c = db()

    acts = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    rows = c.execute(
        """
        SELECT p.*,a.external_id,a.name activity
        FROM production p
        JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY p.work_date DESC,p.id DESC
        LIMIT 30
        """,
        (pid,)
    ).fetchall()

    c.close()

    opts = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in acts
    )

    total_qty = sum((r["qty"] or 0) for r in rows)
    total_plan = sum((r["planned_qty"] or 0) for r in rows)

    overall_pct = 0
    if total_plan > 0:
        overall_pct = (total_qty / total_plan) * 100

    behind_count = 0
    onplan_count = 0

    tr = ""

    for r in rows:
        qty = r["qty"] or 0
        plan = r["planned_qty"] or 0

        if plan > 0:
            pct = (qty / plan) * 100
            variance = qty - plan

            if pct < 90:
                status = "BEHIND"
                badge = "CRITICAL"
                behind_count += 1
            elif pct < 100:
                status = "WATCH"
                badge = "WATCH"
            else:
                status = "ON PLAN"
                badge = "READY"
                onplan_count += 1
        else:
            pct = 0
            variance = qty
            status = "NO PLAN"
            badge = "OPEN"

        tr += f"""
        <tr>
            <td>{esc(r["work_date"])}</td>
            <td><b>{esc(r["external_id"])} - {esc(r["activity"])}</b></td>
            <td>{r["crew"] or 0}</td>
            <td>{qty:g}</td>
            <td>{plan:g}</td>
            <td>{esc(r["unit"])}</td>
            <td>{pct:.0f}%</td>
            <td>{variance:+g}</td>
            <td><span class="badge {badge}">{status}</span></td>
        </tr>
        """

    if not tr:
        tr = '<tr><td colspan="9" class="muted">No production records yet.</td></tr>'

    if not opts:
        form_html = """
        <div class="muted">
            Add a schedule activity before recording production.
        </div>
        <div style="margin-top:12px;">
            <a href="/activities/new"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                + Add Activity
            </a>
        </div>
        """
    else:
        form_html = f"""
        <form method="post" action="/production/add">
            <label>Activity</label>
            <select name="activity_id" required>{opts}</select>

            <label>Crew Size</label>
            <input name="crew" type="number" min="0" placeholder="Example: 6">

            <label>Installed Quantity</label>
            <input name="qty" type="number" step="0.1" placeholder="Example: 420">

            <label>Planned Quantity</label>
            <input name="planned_qty" type="number" step="0.1" placeholder="Example: 500">

            <label>Unit</label>
            <input name="unit" placeholder="LF / SF / CY / EA">

            <button type="submit">Record Production</button>
        </form>
        """

    body = f"""
    <div class="hero">
        <div class="eyebrow">Production Intelligence</div>
        <h1>Measure the job before the schedule update does.</h1>
        <div class="muted">
            Track actual production against plan and flag underperformance early.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Production Records</div>
            <div class="kpi">{len(rows)}</div>
        </div>

        <div class="card">
            <div class="label">Overall To Plan</div>
            <div class="kpi">{overall_pct:.0f}%</div>
        </div>

        <div class="card">
            <div class="label">Behind Plan</div>
            <div class="kpi">{behind_count}</div>
        </div>

        <div class="card">
            <div class="label">On / Above Plan</div>
            <div class="kpi">{onplan_count}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Record Production</h2>
            {form_html}
        </div>

        <div class="card">
            <h2>How BuildCommand Reads It</h2>
            <p>
                Below 90% of planned production is flagged as <b>Behind</b>.
                Between 90% and 99% is flagged as <b>Watch</b>.
                At or above plan is shown as <b>On Plan</b>.
            </p>
            <div class="small">
                These records can be used by the AI Assistant when evaluating field performance and schedule risk.
            </div>
        </div>
    </div>

    <div class="card">
        <h2>Production History</h2>

        <table>
            <tr>
                <th>Date</th>
                <th>Activity</th>
                <th>Crew</th>
                <th>Actual</th>
                <th>Plan</th>
                <th>Unit</th>
                <th>% Plan</th>
                <th>Variance</th>
                <th>Status</th>
            </tr>

            {tr}
        </table>
    </div>
    """

    return shell("Production", body)


@app.post("/production/add")
def add_prod(activity_id:int=Form(...),crew:int=Form(0),qty:float=Form(0),planned_qty:float=Form(0),unit:str=Form("")):
    pid=project_id(); c=db(); c.execute("INSERT INTO production(project_id,activity_id,work_date,crew,qty,planned_qty,unit) VALUES(?,?,?,?,?,?,?)",(pid,activity_id,date.today().isoformat(),crew,qty,planned_qty,unit)); c.commit(); c.close()
    return RedirectResponse("/production",303)

@app.get("/risk",response_class=HTMLResponse)
def risk():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    html = "".join(
        f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {r["band"]}">{r["band"]}</span>
                    <b>{esc(r["external_id"])} - {esc(r["activity"])}</b>
                </div>

                <a href="/risk/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <h3>{r["score"]:.0f}/100</h3>
            <p>{esc(r["explanation"])}</p>
        </div>
        """
        for r in rows
    ) or '<div class="card"><div class="muted">No risks entered for this project yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Predictive Risk</div>
                <h1>What is most likely to hurt the job next?</h1>
                <div class="muted">
                    Track field and schedule risk with an explainable score.
                </div>
            </div>

            <a href="/risk/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Risk
            </a>
        </div>
    </div>

    {html}
    """

    return shell("Predictive Risk", body)


@app.get("/risk/new", response_class=HTMLResponse)
def new_risk_form():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    options = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    if not options:
        body = """
        <div class="hero">
            <div class="eyebrow">Predictive Risk</div>
            <h1>Add Risk</h1>
        </div>

        <div class="card">
            <p>This project has no schedule activities yet. Add an activity first.</p>
            <a href="/activities/new"
               style="color:#f0b44d;font-weight:700;text-decoration:none;">
                + Add Activity
            </a>
        </div>
        """
        return shell("Add Risk", body)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Predictive Risk</div>
        <h1>Add Risk</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/risk/new">

            <label>Activity</label>
            <select name="activity_id" required>
                {options}
            </select>

            <div class="grid2">
                <div>
                    <label>Risk Score</label>
                    <input
                        type="number"
                        name="score"
                        min="0"
                        max="100"
                        step="1"
                        value="50"
                        required
                    >
                </div>

                <div>
                    <label>Risk Band</label>
                    <select name="band">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>
            </div>

            <label>Why Is This a Risk?</label>
            <textarea
                name="explanation"
                placeholder="Example: Material release is late and threatens the rough-in start."
                required
            ></textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Risk</button>

                <a href="/risk"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Risk", body)


@app.post("/risk/new")
def create_risk(
    activity_id: int = Form(...),
    score: float = Form(...),
    band: str = Form(...),
    explanation: str = Form(...)
):
    pid = project_id()
    score = max(0.0, min(100.0, score))

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO risks(
                project_id,
                activity_id,
                score,
                band,
                explanation
            )
            VALUES(?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                score,
                band,
                explanation.strip()
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.get("/risk/{risk_id}/edit", response_class=HTMLResponse)
def edit_risk_form(risk_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.id=? AND r.project_id=?
        """,
        (risk_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/risk", status_code=303)

    bands = ["CRITICAL", "HIGH", "WATCH", "LOW"]
    band_options = "".join(
        f'<option value="{b}" {"selected" if item["band"] == b else ""}>{b.title()}</option>'
        for b in bands
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Predictive Risk</div>
        <h1>Edit Risk</h1>
        <div class="muted">
            {esc(item["external_id"])} - {esc(item["activity"])}
        </div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/risk/{risk_id}/edit">

            <div class="grid2">
                <div>
                    <label>Risk Score</label>
                    <input
                        type="number"
                        name="score"
                        min="0"
                        max="100"
                        step="1"
                        value="{item["score"]:.0f}"
                        required
                    >
                </div>

                <div>
                    <label>Risk Band</label>
                    <select name="band">
                        {band_options}
                    </select>
                </div>
            </div>

            <label>Explanation</label>
            <textarea name="explanation" required>{esc(item["explanation"])}</textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>

                <a href="/risk"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>

        <form method="post"
              action="/risk/{risk_id}/delete"
              style="margin-top:22px;padding-top:18px;border-top:1px solid #213042;">
            <button type="submit"
                    style="background:#492324;color:#ffb0b0;">
                Delete Risk
            </button>
        </form>
    </div>
    """

    return shell("Edit Risk", body)


@app.post("/risk/{risk_id}/edit")
def edit_risk(
    risk_id: int,
    score: float = Form(...),
    band: str = Form(...),
    explanation: str = Form(...)
):
    pid = project_id()
    score = max(0.0, min(100.0, score))

    c = db()
    c.execute(
        """
        UPDATE risks
        SET score=?, band=?, explanation=?
        WHERE id=? AND project_id=?
        """,
        (
            score,
            band,
            explanation.strip(),
            risk_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.post("/risk/{risk_id}/delete")
def delete_risk(risk_id: int):
    pid = project_id()

    c = db()
    c.execute(
        "DELETE FROM risks WHERE id=? AND project_id=?",
        (risk_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/risk", status_code=303)


@app.get("/recovery",response_class=HTMLResponse)
def recovery():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM recovery r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY
            CASE r.status
                WHEN 'APPROVED' THEN 1
                WHEN 'PROPOSED' THEN 2
                WHEN 'REJECTED' THEN 3
                ELSE 4
            END,
            r.days_recovered DESC,
            r.est_cost ASC
        """,
        (pid,)
    ).fetchall()

    acts = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    opts = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in acts
    )

    total_scenarios = len(rows)
    approved = sum(1 for r in rows if r["status"] == "APPROVED")
    proposed = sum(1 for r in rows if r["status"] == "PROPOSED")

    best_value = None
    best_value_score = None

    scenario_cards = ""

    for r in rows:
        days = r["days_recovered"] or 0
        cost = r["est_cost"] or 0

        if days > 0:
            cost_per_day = cost / days
            value_text = f'${cost_per_day:,.0f} / recovered day'

            if best_value_score is None or cost_per_day < best_value_score:
                best_value_score = cost_per_day
                best_value = r["id"]
        else:
            cost_per_day = None
            value_text = "No recovered days entered"

        status_badge = (
            "READY" if r["status"] == "APPROVED"
            else "HIGH" if r["status"] == "REJECTED"
            else "OPEN"
        )

        best_badge = (
            '<span class="badge READY">BEST VALUE</span>'
            if best_value == r["id"]
            else ""
        )

        scenario_cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {status_badge}">{esc(r["status"])}</span>
                    {best_badge}
                    <h3 style="margin:10px 0 4px;">{esc(r["scenario"]).replace("_"," ").title()}</h3>
                    <div class="muted">{esc(r["external_id"])} - {esc(r["activity"])}</div>
                </div>

                <form method="post" action="/recovery/{r["id"]}/delete">
                    <button type="submit" style="background:#492324;color:#ffb0b0;">
                        Delete
                    </button>
                </form>
            </div>

            <div class="grid3" style="margin-top:14px;">
                <div>
                    <div class="label">Days Recovered</div>
                    <div class="kpi">{days:.1f}</div>
                </div>

                <div>
                    <div class="label">Estimated Cost</div>
                    <div class="kpi">${cost:,.0f}</div>
                </div>

                <div>
                    <div class="label">Cost / Day</div>
                    <div style="font-size:20px;font-weight:800;">{value_text}</div>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;">
                <form method="post" action="/recovery/{r["id"]}/approve">
                    <button type="submit">Approve</button>
                </form>

                <form method="post" action="/recovery/{r["id"]}/reject">
                    <button type="submit" style="background:#43381b;color:#ffd779;">
                        Reject
                    </button>
                </form>
            </div>
        </div>
        """

    if not scenario_cards:
        scenario_cards = '<div class="card"><div class="muted">No recovery scenarios saved yet.</div></div>'

    if opts:
        form = f"""
        <div class="card">
            <h2>Test Recovery Idea</h2>

            <form method="post" action="/recovery/add">
                <label>Activity</label>
                <select name="activity_id" required>
                    {opts}
                </select>

                <label>Scenario</label>
                <select name="scenario">
                    <option value="ADD_CREW">Add Crew</option>
                    <option value="OVERTIME">Overtime</option>
                    <option value="WORK_SATURDAY">Work Saturday</option>
                    <option value="RESEQUENCE">Resequence Work</option>
                    <option value="CLEAR_CONSTRAINT">Clear Constraint</option>
                    <option value="SECOND_SHIFT">Second Shift</option>
                </select>

                <label>Modeled Days Recovered</label>
                <input
                    name="days_recovered"
                    type="number"
                    step="0.1"
                    min="0"
                    placeholder="Example: 2.5"
                >

                <label>Estimated Cost</label>
                <input
                    name="est_cost"
                    type="number"
                    step="100"
                    min="0"
                    placeholder="Example: 7500"
                >

                <button type="submit">Save Recovery Scenario</button>
            </form>
        </div>
        """
    else:
        form = """
        <div class="card">
            <h2>Test Recovery Idea</h2>
            <p>Add a schedule activity before creating a recovery scenario.</p>
            <a href="/activities/new"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                + Add Activity
            </a>
        </div>
        """

    body = f"""
    <div class="hero">
        <div class="eyebrow">Recovery Intelligence</div>
        <h1>Compare recovery before spending money.</h1>
        <div class="muted">
            Model time recovered, cost, and decision status before committing resources.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Scenarios</div>
            <div class="kpi">{total_scenarios}</div>
        </div>

        <div class="card">
            <div class="label">Proposed</div>
            <div class="kpi">{proposed}</div>
        </div>

        <div class="card">
            <div class="label">Approved</div>
            <div class="kpi">{approved}</div>
        </div>

        <div class="card">
            <div class="label">Best Cost / Day</div>
            <div class="kpi">
                {"$"+format(best_value_score,",.0f") if best_value_score is not None else "—"}
            </div>
        </div>
    </div>

    <div class="grid2">
        {form}

        <div class="card">
            <h2>How To Use Recovery Intelligence</h2>
            <p>
                Compare options by how many schedule days they recover and what each recovered day costs.
                Approve the option you intend to execute and reject scenarios you no longer want considered.
            </p>
            <div class="small">
                BuildCommand AI can use these recovery scenarios when evaluating project risk and next actions.
            </div>
        </div>
    </div>

    <div>
        <h2>Recovery Scenarios</h2>
        {scenario_cards}
    </div>
    """

    return shell("Recovery", body)


@app.post("/recovery/add")
def add_recovery(
    activity_id: int = Form(...),
    scenario: str = Form(...),
    days_recovered: float = Form(0),
    est_cost: float = Form(0)
):
    pid = project_id()

    days_recovered = max(0.0, days_recovered)
    est_cost = max(0.0, est_cost)

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO recovery(
                project_id,
                activity_id,
                scenario,
                days_recovered,
                est_cost,
                status
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                scenario,
                days_recovered,
                est_cost,
                "PROPOSED"
            )
        )
        c.commit()

    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/approve")
def approve_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE recovery
        SET status='APPROVED'
        WHERE id=? AND project_id=?
        """,
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/reject")
def reject_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE recovery
        SET status='REJECTED'
        WHERE id=? AND project_id=?
        """,
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.post("/recovery/{scenario_id}/delete")
def delete_recovery(scenario_id: int):
    pid = project_id()

    c = db()
    c.execute(
        "DELETE FROM recovery WHERE id=? AND project_id=?",
        (scenario_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse("/recovery", 303)


@app.get("/memory",response_class=HTMLResponse)
def memory():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM memory WHERE project_id=? ORDER BY confidence DESC",(pid,)).fetchall(); c.close()
    html="".join(f'<div class="card"><h3>{esc(r["category"])}</h3><p>{esc(r["insight"])}</p><div class="small">Confidence {r["confidence"]:.0%}</div></div>' for r in rows)
    return shell("Company Memory",'<div class="hero"><div class="eyebrow">Company Construction Memory</div><h1>Every project makes the next one smarter.</h1></div>'+html)

@app.get("/playbooks",response_class=HTMLResponse)
def playbooks():
    body='<div class="hero"><div class="eyebrow">Company Playbooks</div><h1>Turn repeated evidence into how your company runs work.</h1></div><div class="card"><h3>MEP Start Readiness</h3><p>Confirm manpower, material, predecessor completion, access, and inspection prerequisites. Escalate unresolved items before start.</p><div class="small">Evidence-backed company operating routine.</div></div><div class="card"><h3>Schedule Drift Response</h3><p>Verify field status, identify the controlling constraint, compare the field plan with the master schedule, test recovery, and document the agreed plan.</p></div>'
    return shell("Playbooks",body)

@app.get("/portfolio",response_class=HTMLResponse)
def portfolio():
    current_pid = project_id()
    c = db()

    projects = c.execute(
        """
        SELECT *
        FROM projects
        WHERE company_id=?
        ORDER BY
            CASE status
                WHEN 'ACTIVE' THEN 1
                WHEN 'PLANNING' THEN 2
                WHEN 'ON_HOLD' THEN 3
                WHEN 'COMPLETE' THEN 4
                ELSE 5
            END,
            name
        """,
        (current_company_id(),)
    ).fetchall()

    cards = []
    portfolio_scores = []

    for p in projects:
        pid = p["id"]

        risks = c.execute(
            "SELECT * FROM risks WHERE project_id=?",
            (pid,)
        ).fetchall()

        mr = c.execute(
            """
            SELECT COUNT(*) n
            FROM make_ready
            WHERE project_id=? AND status='OPEN'
            """,
            (pid,)
        ).fetchone()["n"]

        activities = c.execute(
            """
            SELECT COUNT(*) n
            FROM activities
            WHERE project_id=?
            """,
            (pid,)
        ).fetchone()["n"]

        active_activities = c.execute(
            """
            SELECT COUNT(*) n
            FROM activities
            WHERE project_id=? AND status='IN_PROGRESS'
            """,
            (pid,)
        ).fetchone()["n"]

        report = c.execute(
            """
            SELECT report_date, manpower, delays
            FROM daily_reports
            WHERE project_id=?
            ORDER BY report_date DESC, id DESC
            LIMIT 1
            """,
            (pid,)
        ).fetchone()

        crit = sum(r["band"] == "CRITICAL" for r in risks)
        high = sum(r["band"] == "HIGH" for r in risks)
        watch = sum(r["band"] == "WATCH" for r in risks)

        score = min(
            100,
            crit * 30 +
            high * 15 +
            watch * 5 +
            mr * 5
        )

        portfolio_scores.append(score)

        if score >= 70:
            attention_band = "CRITICAL"
            attention_text = "Immediate Attention"
        elif score >= 40:
            attention_band = "HIGH"
            attention_text = "Needs Attention"
        elif score >= 15:
            attention_band = "WATCH"
            attention_text = "Watch"
        else:
            attention_band = "READY"
            attention_text = "Stable"

        selected_badge = (
            '<span class="badge READY">CURRENT</span>'
            if pid == current_pid
            else ""
        )

        latest_report = esc(report["report_date"]) if report else "No report"
        latest_manpower = report["manpower"] if report else 0
        latest_delay = esc(report["delays"]) if report and report["delays"] else "No delay noted"

        card = f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <div class="eyebrow">{esc(p["number"])}</div>
                    <h2 style="margin:5px 0;">{esc(p["name"])}</h2>
                    <div class="muted">{esc(p["status"]).replace("_"," ").title()}</div>
                </div>

                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    {selected_badge}
                    <span class="badge {attention_band}">{attention_text}</span>
                </div>
            </div>

            <div class="grid4" style="margin-top:16px;">
                <div>
                    <div class="label">Attention</div>
                    <div class="kpi">{score}</div>
                </div>
                <div>
                    <div class="label">Critical</div>
                    <div class="kpi">{crit}</div>
                </div>
                <div>
                    <div class="label">High</div>
                    <div class="kpi">{high}</div>
                </div>
                <div>
                    <div class="label">Make Ready</div>
                    <div class="kpi">{mr}</div>
                </div>
            </div>

            <div style="margin-top:16px;">
                <div class="small">
                    Activities: {activities} · Active: {active_activities}
                </div>
                <div class="small">
                    Latest report: {latest_report} · Manpower: {latest_manpower or 0}
                </div>
                <div class="small">
                    Latest delay signal: {latest_delay}
                </div>
            </div>

            <form method="post" action="/projects/select" style="margin-top:16px;">
                <input type="hidden" name="project_id" value="{pid}">
                <button type="submit">Open Project</button>
            </form>
        </div>
        """

        cards.append((score, card))

    c.close()

    cards.sort(key=lambda x: x[0], reverse=True)
    cards_html = "".join(card for _, card in cards)

    total_projects = len(projects)
    active_projects = sum(p["status"] == "ACTIVE" for p in projects)
    high_attention = sum(score >= 40 for score in portfolio_scores)
    avg_score = (
        sum(portfolio_scores) / len(portfolio_scores)
        if portfolio_scores
        else 0
    )

    if not cards_html:
        cards_html = '<div class="card"><div class="muted">No projects created yet.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Executive Intelligence</div>
        <h1>Which projects need intervention?</h1>
        <div class="muted">
            Portfolio-wide risk, make-ready, field reporting, and schedule attention in one view.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Projects</div>
            <div class="kpi">{total_projects}</div>
        </div>

        <div class="card">
            <div class="label">Active Projects</div>
            <div class="kpi">{active_projects}</div>
        </div>

        <div class="card">
            <div class="label">Need Attention</div>
            <div class="kpi">{high_attention}</div>
        </div>

        <div class="card">
            <div class="label">Avg Attention</div>
            <div class="kpi">{avg_score:.0f}</div>
        </div>
    </div>

    <div class="grid2">
        {cards_html}
    </div>
    """

    return shell("Portfolio", body)

@app.get("/activities/new", response_class=HTMLResponse)
def new_activity_form():
    pid = project_id()
    c = db()
    project = c.execute("SELECT name,number FROM projects WHERE id=?", (pid,)).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Schedule</div>
        <h1>Add Activity</h1>
        <div class="muted">Add a schedule activity to {project_label}.</div>
    </div>
    <div class="card" style="max-width:760px;">
        <form method="post" action="/activities/new">
            <div class="grid2">
                <div><label>Activity ID</label><input type="text" name="external_id" placeholder="Example: A600" required></div>
                <div><label>Trade</label><input type="text" name="trade" placeholder="Example: Electrical" required></div>
            </div>
            <label>Activity Name</label>
            <input type="text" name="name" placeholder="Example: Electrical Rough-In" required>
            <div class="grid2">
                <div><label>Start Date</label><input type="date" name="start" required></div>
                <div><label>Finish Date</label><input type="date" name="finish" required></div>
            </div>
            <div class="grid2">
                <div><label>Percent Complete</label><input type="number" name="pct" min="0" max="100" step="1" value="0" required></div>
                <div>
                    <label>Status</label>
                    <select name="status">
                        <option value="NOT_STARTED">Not Started</option>
                        <option value="IN_PROGRESS">In Progress</option>
                        <option value="COMPLETE">Complete</option>
                        <option value="HOLD">Hold</option>
                    </select>
                </div>
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Activity</button>
                <a href="/schedule" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>
    </div>
    """
    return shell("Add Activity", body)


@app.post("/activities/new")
def create_activity(
    external_id: str = Form(...),
    name: str = Form(...),
    trade: str = Form(...),
    start: str = Form(...),
    finish: str = Form(...),
    pct: float = Form(0),
    status: str = Form("NOT_STARTED")
):
    pid = project_id()
    pct = max(0.0, min(100.0, pct))
    c = db()
    c.execute(
        """
        INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (pid, external_id.strip(), name.strip(), trade.strip(), start, finish, pct, status)
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.get("/activities/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity_form(activity_id: int):
    pid = project_id()
    c = db()
    activity = c.execute(
        "SELECT * FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()
    c.close()

    if not activity:
        return RedirectResponse(url="/schedule", status_code=303)

    statuses = ["NOT_STARTED", "IN_PROGRESS", "COMPLETE", "HOLD"]
    options = "".join(
        f'<option value="{s}" {"selected" if activity["status"] == s else ""}>{s.replace("_"," ").title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Schedule</div>
        <h1>Edit Activity</h1>
        <div class="muted">Update schedule information for this activity.</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/activities/{activity_id}/edit">
            <div class="grid2">
                <div>
                    <label>Activity ID</label>
                    <input type="text" name="external_id" value="{esc(activity["external_id"])}" required>
                </div>
                <div>
                    <label>Trade</label>
                    <input type="text" name="trade" value="{esc(activity["trade"])}" required>
                </div>
            </div>

            <label>Activity Name</label>
            <input type="text" name="name" value="{esc(activity["name"])}" required>

            <div class="grid2">
                <div>
                    <label>Start Date</label>
                    <input type="date" name="start" value="{activity["start"]}" required>
                </div>
                <div>
                    <label>Finish Date</label>
                    <input type="date" name="finish" value="{activity["finish"]}" required>
                </div>
            </div>

            <div class="grid2">
                <div>
                    <label>Percent Complete</label>
                    <input type="number" name="pct" min="0" max="100" step="1" value="{activity["pct"]:.0f}" required>
                </div>
                <div>
                    <label>Status</label>
                    <select name="status">{options}</select>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>
                <a href="/schedule" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>

        <form method="post" action="/activities/{activity_id}/delete" style="margin-top:22px;padding-top:18px;border-top:1px solid #213042;">
            <button type="submit" style="background:#492324;color:#ffb0b0;">Delete Activity</button>
        </form>
    </div>
    """
    return shell("Edit Activity", body)


@app.post("/activities/{activity_id}/edit")
def edit_activity(
    activity_id: int,
    external_id: str = Form(...),
    name: str = Form(...),
    trade: str = Form(...),
    start: str = Form(...),
    finish: str = Form(...),
    pct: float = Form(0),
    status: str = Form("NOT_STARTED")
):
    pid = project_id()
    pct = max(0.0, min(100.0, pct))
    c = db()
    c.execute(
        """
        UPDATE activities
        SET external_id=?, name=?, trade=?, start=?, finish=?, pct=?, status=?
        WHERE id=? AND project_id=?
        """,
        (external_id.strip(), name.strip(), trade.strip(), start, finish, pct, status, activity_id, pid)
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.post("/activities/{activity_id}/delete")
def delete_activity(activity_id: int):
    pid = project_id()
    c = db()

    c.execute("DELETE FROM field_updates WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM production WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM make_ready WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM risks WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM recovery WHERE activity_id=? AND project_id=?", (activity_id, pid))
    c.execute("DELETE FROM activities WHERE id=? AND project_id=?", (activity_id, pid))

    c.commit()
    c.close()
    return RedirectResponse(url="/schedule", status_code=303)


@app.get("/subcontractors/new", response_class=HTMLResponse)
def new_subcontractor_form():
    pid = project_id()
    c = db()
    project = c.execute("SELECT name,number FROM projects WHERE id=?", (pid,)).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Subcontractors</div>
        <h1>Add Subcontractor</h1>
        <div class="muted">Add a trade partner to {project_label}.</div>
    </div>

    <div class="card" style="max-width:680px;">
        <form method="post" action="/subcontractors/new">
            <label>Company Name</label>
            <input type="text" name="name" placeholder="Example: Valley Electric" required>

            <label>Trade</label>
            <input type="text" name="trade" placeholder="Example: Electrical" required>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Subcontractor</button>
                <a href="/subcontractors" style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">Cancel</a>
            </div>
        </form>
    </div>
    """
    return shell("Add Subcontractor", body)


@app.post("/subcontractors/new")
def create_subcontractor(
    name: str = Form(...),
    trade: str = Form(...)
):
    pid = project_id()
    c = db()
    c.execute(
        "INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)",
        (pid, name.strip(), trade.strip())
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/subcontractors", status_code=303)


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    reports = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 10
        """,
        (pid,)
    ).fetchall()

    analyses = c.execute(
        """
        SELECT *
        FROM daily_report_analysis
        WHERE project_id=?
        ORDER BY id DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    analysis_by_report = {}
    for a in analyses:
        if a["report_id"] not in analysis_by_report:
            analysis_by_report[a["report_id"]] = a

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    report_cards = ""

    for r in reports:
        analysis = analysis_by_report.get(r["id"])

        if analysis:
            analysis_html = (
                '<div style="margin-top:16px;padding-top:14px;border-top:1px solid #213042;">'
                '<div class="small">BUILDCOMMAND ANALYSIS</div>'
                f'<div style="margin-top:8px;line-height:1.6;">{esc(analysis["analysis_text"]).replace(chr(10), "<br>")}</div>'
                '</div>'
            )
            analyze_button = (
                f'<form method="post" action="/daily-report/{r["id"]}/analyze">'
                '<button type="submit">Analyze Again</button>'
                '</form>'
            )
        else:
            analysis_html = ""
            analyze_button = (
                f'<form method="post" action="/daily-report/{r["id"]}/analyze">'
                '<button type="submit">Analyze Report</button>'
                '</form>'
            )

        report_cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    <div class="eyebrow">Daily Report</div>
                    <h3 style="margin:6px 0;">{esc(r["report_date"])}</h3>
                    <div class="badge OPEN">{r["manpower"] or 0} workers</div>
                </div>
                <div>{analyze_button}</div>
            </div>

            <div class="small" style="margin-top:14px;">Weather</div>
            <p>{esc(r["weather"]) or "—"}</p>

            <div class="small">Work Completed</div>
            <p>{esc(r["work_completed"]) or "—"}</p>

            <div class="small">Delays / Constraints</div>
            <p>{esc(r["delays"]) or "—"}</p>

            <div class="small">Deliveries</div>
            <p>{esc(r["deliveries"]) or "—"}</p>

            <div class="small">Inspections</div>
            <p>{esc(r["inspections"]) or "—"}</p>

            <div class="small">Safety</div>
            <p>{esc(r["safety"]) or "—"}</p>

            <div class="small">Tomorrow's Plan</div>
            <p>{esc(r["tomorrow_plan"]) or "—"}</p>

            {analysis_html}
        </div>
        """

    if not report_cards:
        report_cards = '<div class="card"><div class="muted">No daily reports yet.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Daily Superintendent Report</div>
        <h1>Capture the job today so BuildCommand can analyze tomorrow.</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>New Daily Report</h2>

            <form method="post" action="/daily-report">
                <label>Report Date</label>
                <input type="date" name="report_date" value="{date.today().isoformat()}" required>

                <label>Weather</label>
                <input type="text" name="weather" placeholder="Example: Clear, 96°F">

                <label>Total Manpower</label>
                <input type="number" name="manpower" min="0" value="0">

                <label>Work Completed</label>
                <textarea name="work_completed" placeholder="What got completed today?"></textarea>

                <label>Delays / Constraints</label>
                <textarea name="delays" placeholder="Access issues, missing material, RFIs, manpower problems, etc."></textarea>

                <label>Deliveries</label>
                <textarea name="deliveries" placeholder="What arrived or failed to arrive?"></textarea>

                <label>Inspections</label>
                <textarea name="inspections" placeholder="Passed, failed, scheduled, or pending inspections."></textarea>

                <label>Safety Notes</label>
                <textarea name="safety" placeholder="Incidents, observations, toolbox talks, corrective actions."></textarea>

                <label>Tomorrow's Plan</label>
                <textarea name="tomorrow_plan" placeholder="What needs to happen tomorrow?"></textarea>

                <button type="submit">Save Daily Report</button>
            </form>
        </div>

        <div>
            <h2 style="margin-top:0;">Recent Reports</h2>
            {report_cards}
        </div>
    </div>
    """

    return shell("Daily Report", body)


@app.post("/daily-report")
def save_daily_report(
    report_date: str = Form(...),
    weather: str = Form(""),
    manpower: int = Form(0),
    work_completed: str = Form(""),
    delays: str = Form(""),
    deliveries: str = Form(""),
    inspections: str = Form(""),
    safety: str = Form(""),
    tomorrow_plan: str = Form("")
):
    pid = project_id()
    c = db()
    c.execute(
        """
        INSERT INTO daily_reports(
            project_id, report_date, weather, manpower, work_completed,
            delays, deliveries, inspections, safety, tomorrow_plan, created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            report_date,
            weather.strip(),
            manpower,
            work_completed.strip(),
            delays.strip(),
            deliveries.strip(),
            inspections.strip(),
            safety.strip(),
            tomorrow_plan.strip(),
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()
    return RedirectResponse(url="/daily-report", status_code=303)


@app.get("/ai-analysis", response_class=HTMLResponse)
def ai_analysis():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    risks = c.execute(
        """
        SELECT r.*, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    make_ready_items = c.execute(
        """
        SELECT m.*, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY due
        """,
        (pid,)
    ).fetchall()

    report = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 1
        """,
        (pid,)
    ).fetchone()

    c.close()

    project_name = esc(project["name"]) if project else "Current Project"

    critical = [r for r in risks if r["band"] == "CRITICAL"]
    high = [r for r in risks if r["band"] == "HIGH"]
    in_progress = [a for a in activities if a["status"] == "IN_PROGRESS"]

    top_actions = []

    for r in critical[:3]:
        top_actions.append(
            f'CRITICAL: {esc(r["activity"])} — {esc(r["explanation"])}'
        )

    for item in make_ready_items[:3]:
        top_actions.append(
            f'MAKE READY: {esc(item["title"])} — due {esc(item["due"])}'
        )

    if report:
        delays = (report["delays"] or "").strip()
        inspections = (report["inspections"] or "").strip()
        tomorrow = (report["tomorrow_plan"] or "").strip()

        if delays:
            top_actions.append(f'FIELD DELAY: {esc(delays)}')

        if inspections:
            top_actions.append(f'INSPECTION: {esc(inspections)}')

        if tomorrow:
            top_actions.append(f'TOMORROW: {esc(tomorrow)}')

    if not top_actions:
        top_actions.append(
            "No immediate high-priority issues were found in the current project data."
        )

    action_html = "".join(
        f'<div class="action">{item}</div>'
        for item in top_actions[:7]
    )

    if report:
        report_summary = (
            f'<div class="small">Latest Report: {esc(report["report_date"])}</div>'
            f'<p><b>Manpower:</b> {report["manpower"] or 0}</p>'
            f'<p><b>Work Completed:</b> {esc(report["work_completed"]) or "—"}</p>'
            f'<p><b>Delays:</b> {esc(report["delays"]) or "—"}</p>'
            f'<p><b>Tomorrow:</b> {esc(report["tomorrow_plan"]) or "—"}</p>'
        )
    else:
        report_summary = '<div class="muted">No daily report has been submitted yet.</div>'

    risk_html = "".join(
        (
            f'<div class="action">'
            f'<span class="badge {r["band"]}">{r["band"]}</span> '
            f'<b>{esc(r["activity"])}</b> · {r["score"]:.0f}/100'
            f'<div class="small">{esc(r["explanation"])}</div>'
            f'</div>'
        )
        for r in risks[:6]
    ) or '<div class="muted">No risk records yet.</div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand Intelligence</div>
        <h1>What needs attention now?</h1>
        <div class="muted">
            Current project analysis for {project_name}.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Critical Risks</div>
            <div class="kpi">{len(critical)}</div>
        </div>
        <div class="card">
            <div class="label">High Risks</div>
            <div class="kpi">{len(high)}</div>
        </div>
        <div class="card">
            <div class="label">Open Make-Ready</div>
            <div class="kpi">{len(make_ready_items)}</div>
        </div>
        <div class="card">
            <div class="label">Active Activities</div>
            <div class="kpi">{len(in_progress)}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Handle First</h2>
            {action_html}
        </div>

        <div class="card">
            <h2>Latest Field Signal</h2>
            {report_summary}
        </div>
    </div>

    <div class="card">
        <h2>Risk Intelligence</h2>
        {risk_html}
    </div>

    <div class="card">
        <h2>BuildCommand Recommendation</h2>
        <p>
            Focus first on critical risks and open make-ready items, then verify field delays,
            inspections, and tomorrow's plan before committing additional manpower or recovery cost.
        </p>
        <div class="small">
            This is the first intelligence layer. The next version can connect a live AI model
            so BuildCommand can answer natural-language questions about the project.
        </div>
    </div>
    """

    return shell("AI Analysis", body)


def build_project_context(pid):
    c = db()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    risks = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM risks r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.score DESC
        """,
        (pid,)
    ).fetchall()

    make_ready_items = c.execute(
        """
        SELECT m.*, a.external_id, a.name activity
        FROM make_ready m
        JOIN activities a ON a.id=m.activity_id
        WHERE m.project_id=? AND m.status='OPEN'
        ORDER BY due
        """,
        (pid,)
    ).fetchall()

    reports = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE project_id=?
        ORDER BY report_date DESC, id DESC
        LIMIT 5
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        "SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",
        (pid,)
    ).fetchall()

    production_rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM production p
        JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY p.work_date DESC, p.id DESC
        LIMIT 12
        """,
        (pid,)
    ).fetchall()

    sub_updates = c.execute(
        """
        SELECT u.*, s.name sub_name, s.trade
        FROM subcontractor_updates u
        JOIN subs s ON s.id=u.sub_id
        WHERE u.project_id=?
        ORDER BY u.update_date DESC, u.id DESC
        LIMIT 15
        """,
        (pid,)
    ).fetchall()

    recovery_rows = c.execute(
        """
        SELECT r.*, a.external_id, a.name activity
        FROM recovery r
        JOIN activities a ON a.id=r.activity_id
        WHERE r.project_id=?
        ORDER BY r.id DESC
        LIMIT 12
        """,
        (pid,)
    ).fetchall()

    action_rows = c.execute(
        """
        SELECT *
        FROM action_items
        WHERE project_id=? AND status='OPEN'
        ORDER BY due, id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    readiness_rows = c.execute(
        """
        SELECT ar.*, a.external_id, a.name activity, a.start
        FROM activity_readiness ar
        JOIN activities a ON a.id=ar.activity_id
        WHERE ar.project_id=?
        ORDER BY a.start
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    procurement_rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM procurement p
        LEFT JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY p.required_on_site, p.id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    issue_rows = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM project_issues i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=? AND i.status!='CLOSED'
        ORDER BY i.due, i.id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    punch_rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM punch_items p
        LEFT JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=? AND p.status!='VERIFIED'
        ORDER BY p.due, p.id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    inspection_rows = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM inspections_tracker i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=? AND i.result!='PASSED'
        ORDER BY i.scheduled_date, i.id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    submittal_rows = c.execute(
        """
        SELECT s.*, a.external_id, a.name activity
        FROM submittals s
        LEFT JOIN activities a ON a.id=s.activity_id
        WHERE s.project_id=? AND s.status NOT IN ('APPROVED','APPROVED_AS_NOTED')
        ORDER BY s.due_date, s.id
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    safety_rows = c.execute(
        """
        SELECT s.*, a.external_id, a.name activity
        FROM safety_items s
        LEFT JOIN activities a ON a.id=s.activity_id
        WHERE s.project_id=? AND s.status!='CLOSED'
        ORDER BY s.event_date DESC, s.id DESC
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    change_rows = c.execute(
        """
        SELECT ce.*, a.external_id, a.name activity
        FROM change_events ce
        LEFT JOIN activities a ON a.id=ce.activity_id
        WHERE ce.project_id=? AND ce.status NOT IN ('APPROVED','REJECTED')
        ORDER BY ce.id DESC
        LIMIT 20
        """,
        (pid,)
    ).fetchall()

    meeting_rows = c.execute(
        """
        SELECT *
        FROM meeting_notes
        WHERE project_id=?
        ORDER BY meeting_date DESC, id DESC
        LIMIT 10
        """,
        (pid,)
    ).fetchall()

    c.close()

    lines = []

    if project:
        lines.append(
            f'PROJECT: {project["number"]} - {project["name"]} | Status: {project["status"]}'
        )

    lines.append("\nSCHEDULE ACTIVITIES:")
    for a in activities:
        lines.append(
            f'- {a["external_id"]}: {a["name"]} | Trade: {a["trade"]} | '
            f'{a["start"]} to {a["finish"]} | {a["pct"]:.0f}% | {a["status"]}'
        )

    lines.append("\nRISKS:")
    for r in risks:
        lines.append(
            f'- {r["band"]} {r["score"]:.0f}/100 | {r["external_id"]} {r["activity"]}: '
            f'{r["explanation"]}'
        )

    lines.append("\nOPEN MAKE-READY ITEMS:")
    for m in make_ready_items:
        lines.append(
            f'- {m["priority"]} | {m["external_id"]} {m["activity"]} | '
            f'{m["title"]} | Reason: {m["reason"]} | Due: {m["due"]}'
        )

    lines.append("\nSUBCONTRACTORS:")
    for s in subs:
        lines.append(f'- {s["name"]} | Trade: {s["trade"]}')

    lines.append("\nRECENT PRODUCTION:")
    for p in production_rows:
        qty = p["qty"] or 0
        plan = p["planned_qty"] or 0
        pct = (qty / plan * 100) if plan > 0 else 0
        lines.append(
            f'- {p["work_date"]} | {p["external_id"]} {p["activity"]} | '
            f'Crew {p["crew"] or 0} | Actual {qty:g} {p["unit"] or ""} | '
            f'Plan {plan:g} | {pct:.0f}% of plan'
        )

    lines.append("\nRECENT SUBCONTRACTOR UPDATES:")
    for u in sub_updates:
        lines.append(
            f'- {u["update_date"]} | {u["sub_name"]} | Trade: {u["trade"]} | '
            f'Manpower: {u["manpower"] or 0} | Status: {u["status"] or "N/A"} | '
            f'Commitment: {u["commitment"] or "N/A"} | Issue: {u["issue"] or "N/A"}'
        )

    lines.append("\nRECOVERY SCENARIOS:")
    for r in recovery_rows:
        days = r["days_recovered"] or 0
        cost = r["est_cost"] or 0
        cpd = (cost / days) if days > 0 else 0
        lines.append(
            f'- {r["external_id"]} {r["activity"]} | '
            f'{r["scenario"]} | Days recovered: {days:.1f} | '
            f'Cost: ${cost:,.0f} | Cost/day: ${cpd:,.0f} | Status: {r["status"]}'
        )

    lines.append("\nOPEN ACTION ITEMS:")
    for a in action_rows:
        lines.append(
            f'- {a["priority"]} | {a["title"]} | Owner: {a["owner"] or "Unassigned"} | '
            f'Due: {a["due"]} | Notes: {a["notes"] or "N/A"}'
        )

    lines.append("\nACTIVITY READINESS:")
    for r in readiness_rows:
        pct, status, _ = readiness_result(r)
        lines.append(
            f'- {r["external_id"]} {r["activity"]} | Starts {r["start"]} | '
            f'Readiness {pct}% | Status: {status} | Notes: {r["notes"] or "N/A"}'
        )

    lines.append("\nSCHEDULE HEALTH:")
    readiness_map = {r["activity_id"]: r for r in readiness_rows}

    for a in activities:
        score, status, _, reasons = schedule_health_status(
            a,
            readiness_map.get(a["id"]),
            production_rows,
            risks
        )
        lines.append(
            f'- {a["external_id"]} {a["name"]} | Health score {score} | '
            f'Status: {status} | Reasons: {"; ".join(reasons)}'
        )

    lines.append("\nPROCUREMENT / LONG LEAD:")
    for p in procurement_rows:
        badge, risk_text = procurement_risk(
            p["required_on_site"],
            p["promised_date"],
            p["status"]
        )
        activity_text = (
            f'{p["external_id"]} {p["activity"]}'
            if p["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {p["item"]} | {activity_text} | Vendor: {p["vendor"] or "N/A"} | '
            f'Required: {p["required_on_site"] or "N/A"} | '
            f'Promised: {p["promised_date"] or "N/A"} | '
            f'Status: {p["status"] or "N/A"} | Risk: {risk_text} | '
            f'Notes: {p["notes"] or "N/A"}'
        )

    lines.append("\nOPEN RFIS / ISSUES:")
    for i in issue_rows:
        activity_text = (
            f'{i["external_id"]} {i["activity"]}'
            if i["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {i["issue_type"]} | {i["priority"]} | {i["title"]} | '
            f'{activity_text} | Owner: {i["owner"] or "Unassigned"} | '
            f'Due: {i["due"] or "N/A"} | Status: {i["status"]} | '
            f'Description: {i["description"] or "N/A"} | '
            f'Response: {i["response"] or "N/A"}'
        )

    lines.append("\nOPEN QUALITY / PUNCH ITEMS:")
    for p in punch_rows:
        activity_text = (
            f'{p["external_id"]} {p["activity"]}'
            if p["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {p["priority"]} | {p["title"]} | Location: {p["location"] or "N/A"} | '
            f'Trade: {p["trade"] or "N/A"} | {activity_text} | '
            f'Owner: {p["owner"] or "Unassigned"} | Due: {p["due"] or "N/A"} | '
            f'Status: {p["status"]} | Description: {p["description"] or "N/A"} | '
            f'Resolution: {p["resolution"] or "N/A"}'
        )

    lines.append("\nOPEN / FAILED INSPECTIONS:")
    for i in inspection_rows:
        activity_text = (
            f'{i["external_id"]} {i["activity"]}'
            if i["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {i["inspection_type"]} | {activity_text} | '
            f'Authority: {i["authority"] or "N/A"} | '
            f'Scheduled: {i["scheduled_date"] or "N/A"} | '
            f'Result: {i["result"]} | '
            f'Reinspection: {i["reinspection_date"] or "N/A"} | '
            f'Notes: {i["notes"] or "N/A"}'
        )

    lines.append("\nOPEN SUBMITTALS:")
    for s in submittal_rows:
        activity_text = (
            f'{s["external_id"]} {s["activity"]}'
            if s["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {s["title"]} | Spec: {s["spec_section"] or "N/A"} | '
            f'{activity_text} | Responsible: {s["responsible_party"] or "Unassigned"} | '
            f'Sent: {s["sent_date"] or "N/A"} | Due: {s["due_date"] or "N/A"} | '
            f'Status: {s["status"]} | Notes: {s["notes"] or "N/A"}'
        )

    lines.append("\nOPEN SAFETY ITEMS:")
    for s in safety_rows:
        activity_text = (
            f'{s["external_id"]} {s["activity"]}'
            if s["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {s["severity"]} | {s["item_type"]} | {s["title"]} | '
            f'Location: {s["location"] or "N/A"} | {activity_text} | '
            f'Responsible: {s["responsible_party"] or "Unassigned"} | '
            f'Status: {s["status"]} | Description: {s["description"] or "N/A"} | '
            f'Corrective action: {s["corrective_action"] or "N/A"}'
        )

    lines.append("\nOPEN CHANGE EVENTS:")
    for ce in change_rows:
        activity_text = (
            f'{ce["external_id"]} {ce["activity"]}'
            if ce["external_id"]
            else "No linked activity"
        )
        lines.append(
            f'- {ce["event_type"]} | {ce["title"]} | {activity_text} | '
            f'Responsible: {ce["responsible_party"] or "Unassigned"} | '
            f'Estimated cost: ${ce["estimated_cost"] or 0:,.0f} | '
            f'Schedule impact: {ce["schedule_days"] or 0:.1f} days | '
            f'Status: {ce["status"]} | Description: {ce["description"] or "N/A"}'
        )

    lines.append("\nRECENT MEETING NOTES:")
    for m in meeting_rows:
        lines.append(
            f'- {m["meeting_date"]} | {m["meeting_type"]} | {m["title"]} | '
            f'Attendees: {m["attendees"] or "N/A"} | '
            f'Decisions: {m["decisions"] or "N/A"} | '
            f'Commitments: {m["commitments"] or "N/A"} | '
            f'Follow-up: {m["follow_up"] or "N/A"}'
        )

    lines.append("\nRECENT DAILY REPORTS:")
    for r in reports:
        lines.append(
            f'- Date: {r["report_date"]} | Weather: {r["weather"] or "N/A"} | '
            f'Manpower: {r["manpower"] or 0}\n'
            f'  Work completed: {r["work_completed"] or "N/A"}\n'
            f'  Delays: {r["delays"] or "N/A"}\n'
            f'  Deliveries: {r["deliveries"] or "N/A"}\n'
            f'  Inspections: {r["inspections"] or "N/A"}\n'
            f'  Safety: {r["safety"] or "N/A"}\n'
            f'  Tomorrow plan: {r["tomorrow_plan"] or "N/A"}'
        )

    return "\n".join(lines)


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page():
    pid = project_id()
    c = db()
    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    api_ready = bool(os.environ.get("OPENAI_API_KEY"))

    status_html = (
        '<span class="badge READY">AI CONNECTED</span>'
        if api_ready
        else '<span class="badge HIGH">API KEY NEEDED</span>'
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand AI Copilot</div>
        <h1>Ask the project.</h1>
        <div class="muted">{project_label}</div>
        <div style="margin-top:10px;">{status_html}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Ask BuildCommand</h2>

            <form method="post" action="/assistant">
                <label>Your Question</label>
                <textarea
                    name="question"
                    placeholder="Example: What should I worry about today?"
                    required
                ></textarea>

                <button type="submit">Analyze Project</button>
            </form>

            <div class="small" style="margin-top:14px;">
                Try: What should I handle first? Which activity could delay us?
                What should I ask the MEP subcontractor? What should tomorrow's plan include?
            </div>
        </div>

        <div class="card">
            <h2>How it works</h2>
            <p>
                BuildCommand sends the selected project's schedule, risks, open make-ready items,
                subcontractors, and recent daily reports to the AI with your question.
            </p>
            <p class="small">
                The AI is instructed to stay grounded in project data and clearly identify when
                information is missing instead of inventing jobsite facts.
            </p>
        </div>
    </div>
    """

    return shell("AI Assistant", body)


@app.post("/assistant", response_class=HTMLResponse)
def assistant_answer(question: str = Form(...)):
    pid = project_id()

    c = db()
    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()
    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        answer = """
        OPENAI_API_KEY is not configured yet.

        Add the key in Render under your service's Environment settings,
        then redeploy the service. Do not paste the API key into full_app.py.
        """
    else:
        context = build_project_context(pid)

        instructions = """
You are BuildCommand AI, a construction superintendent and project-execution copilot.

Use only the project information supplied in the prompt as job-specific facts.
Do not invent schedule status, subcontractor commitments, inspections, deliveries,
manpower, safety conditions, or field progress.

Prioritize:
1. immediate field risk,
2. schedule impact,
3. make-ready constraints,
4. inspections and deliveries,
5. subcontractor follow-up,
6. practical next actions.

When useful, structure the answer as:
- Handle first
- Why it matters
- Who to follow up with
- Next action

Keep answers concise, practical, and written for a working superintendent.
If the project data is insufficient, clearly say what information is missing.
"""

        user_input = f"""
PROJECT DATA
------------
{context}

SUPERINTENDENT QUESTION
-----------------------
{question}
"""

        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model="gpt-5.6",
                instructions=instructions,
                input=user_input
            )
            answer = response.output_text
        except Exception as exc:
            answer = (
                "BuildCommand could not reach the AI service. "
                f"Error: {str(exc)}"
            )

    answer_html = esc(answer).replace("\n", "<br>")

    body = f"""
    <div class="hero">
        <div class="eyebrow">BuildCommand AI Copilot</div>
        <h1>Project Answer</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="grid2">
        <div class="card">
            <div class="small">YOUR QUESTION</div>
            <h3>{esc(question)}</h3>

            <a href="/assistant"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                ← Ask another question
            </a>
        </div>

        <div class="card">
            <div class="small">BUILDCOMMAND AI</div>
            <div style="margin-top:12px;line-height:1.65;">
                {answer_html}
            </div>
        </div>
    </div>
    """

    return shell("AI Assistant", body)


@app.get("/make-ready/new", response_class=HTMLResponse)
def new_make_ready_form():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    options = "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    if not options:
        body = """
        <div class="hero">
            <div class="eyebrow">Make Ready</div>
            <h1>Add Make-Ready Item</h1>
        </div>
        <div class="card">
            <p>This project has no activities yet. Add a schedule activity first.</p>
            <a href="/activities/new" style="color:#f0b44d;font-weight:700;text-decoration:none;">
                + Add Activity
            </a>
        </div>
        """
        return shell("Add Make-Ready", body)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Make Ready</div>
        <h1>Add Make-Ready Item</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/make-ready/new">

            <label>Activity</label>
            <select name="activity_id" required>
                {options}
            </select>

            <label>Blocker / Action Title</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Confirm electrical gear delivery"
                required
            >

            <label>Why It Matters</label>
            <textarea
                name="reason"
                placeholder="Describe the constraint, dependency, or field impact."
                required
            ></textarea>

            <div class="grid2">
                <div>
                    <label>Clear By</label>
                    <input type="date" name="due" required>
                </div>

                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>
            </div>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Make-Ready Item</button>

                <a href="/make-ready"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Make-Ready", body)


@app.post("/make-ready/new")
def create_make_ready(
    activity_id: int = Form(...),
    title: str = Form(...),
    reason: str = Form(...),
    due: str = Form(...),
    priority: str = Form("HIGH")
):
    pid = project_id()

    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO make_ready(
                project_id,
                activity_id,
                title,
                reason,
                due,
                priority,
                status
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                pid,
                activity_id,
                title.strip(),
                reason.strip(),
                due,
                priority,
                "OPEN"
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/make-ready", status_code=303)


@app.post("/make-ready/{item_id}/close")
def close_make_ready(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE make_ready
        SET status='COMPLETE'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/make-ready", status_code=303)


@app.post("/daily-report/{report_id}/analyze")
def analyze_daily_report(report_id: int):
    pid = project_id()
    c = db()

    report = c.execute(
        """
        SELECT *
        FROM daily_reports
        WHERE id=? AND project_id=?
        """,
        (report_id, pid)
    ).fetchone()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    activities = c.execute(
        "SELECT * FROM activities WHERE project_id=? ORDER BY start",
        (pid,)
    ).fetchall()

    c.close()

    if not report:
        return RedirectResponse(url="/daily-report", status_code=303)

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        analysis_text = (
            "OPENAI_API_KEY is not configured. Add the key in Render Environment settings "
            "to enable automatic report analysis."
        )
    else:
        schedule_lines = []
        for a in activities:
            schedule_lines.append(
                f'{a["external_id"]} | {a["name"]} | {a["trade"]} | '
                f'{a["start"]} to {a["finish"]} | {a["pct"]:.0f}% | {a["status"]}'
            )

        schedule_text = "\n".join(schedule_lines) or "No schedule activities entered."

        prompt = f"""
PROJECT
{project["number"] if project else ""} - {project["name"] if project else ""}

SCHEDULE
{schedule_text}

DAILY REPORT
Date: {report["report_date"]}
Weather: {report["weather"] or "N/A"}
Manpower: {report["manpower"] or 0}
Work completed: {report["work_completed"] or "N/A"}
Delays / constraints: {report["delays"] or "N/A"}
Deliveries: {report["deliveries"] or "N/A"}
Inspections: {report["inspections"] or "N/A"}
Safety: {report["safety"] or "N/A"}
Tomorrow plan: {report["tomorrow_plan"] or "N/A"}
"""

        instructions = """
You are BuildCommand AI, a construction superintendent field-intelligence copilot.

Analyze the daily report against the schedule information provided.
Do not invent facts that are not present.

Return a concise superintendent review with these headings:
HANDLE FIRST
SCHEDULE IMPACT
SUBCONTRACTOR / FOLLOW-UP
TOMORROW READINESS
MISSING INFORMATION

Call out specific delays, inspection issues, delivery risks, manpower concerns,
or tomorrow-plan gaps when supported by the report.
"""

        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(
                model="gpt-5.6",
                instructions=instructions,
                input=prompt
            )
            analysis_text = response.output_text
        except Exception as exc:
            analysis_text = f"AI analysis failed: {str(exc)}"

    c = db()
    c.execute(
        "DELETE FROM daily_report_analysis WHERE project_id=? AND report_id=?",
        (pid, report_id)
    )
    c.execute(
        """
        INSERT INTO daily_report_analysis(
            project_id,
            report_id,
            analysis_text,
            created
        )
        VALUES(?,?,?,?)
        """,
        (
            pid,
            report_id,
            analysis_text,
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/daily-report", status_code=303)


@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM action_items
        WHERE project_id=?
        ORDER BY
            CASE status
                WHEN 'OPEN' THEN 1
                WHEN 'COMPLETE' THEN 2
                ELSE 3
            END,
            CASE priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                WHEN 'LOW' THEN 4
                ELSE 5
            END,
            due
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()

    open_items = [r for r in rows if r["status"] == "OPEN"]
    complete_items = [r for r in rows if r["status"] == "COMPLETE"]
    overdue_items = [
        r for r in open_items
        if r["due"] and r["due"] < today
    ]

    open_html = ""

    for r in open_items:
        overdue = bool(r["due"] and r["due"] < today)
        due_text = f'Due {esc(r["due"])}'

        if overdue:
            due_text += " · OVERDUE"

        badge = r["priority"] if r["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "OPEN"

        open_html += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["priority"])}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">Owner: {esc(r["owner"]) or "Unassigned"}</div>
                </div>

                <form method="post" action="/actions/{r["id"]}/complete">
                    <button type="submit">Mark Complete</button>
                </form>
            </div>

            <p>{esc(r["notes"]) or "No notes entered."}</p>
            <div class="small">{due_text}</div>
        </div>
        """

    if not open_html:
        open_html = '<div class="card"><div class="muted">No open action items.</div></div>'

    complete_html = "".join(
        f"""
        <div class="action">
            <span class="badge COMPLETE">COMPLETE</span>
            <b>{esc(r["title"])}</b>
            <div class="small">
                Owner: {esc(r["owner"]) or "Unassigned"} · Due {esc(r["due"]) or "—"}
            </div>
        </div>
        """
        for r in complete_items[:12]
    ) or '<div class="muted">No completed actions yet.</div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Action Center</div>
                <h1>Turn project intelligence into accountable action.</h1>
                <div class="muted">
                    Assign ownership, due dates, and priority to the work that cannot fall through the cracks.
                </div>
            </div>

            <a href="/actions/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Action
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open Actions</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue_items)}</div>
        </div>

        <div class="card">
            <div class="label">Critical</div>
            <div class="kpi">{sum(r["priority"] == "CRITICAL" for r in open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Completed</div>
            <div class="kpi">{len(complete_items)}</div>
        </div>
    </div>

    <div class="grid2">
        <div>
            <h2>Open Actions</h2>
            {open_html}
        </div>

        <div class="card">
            <h2>Recently Completed</h2>
            {complete_html}
        </div>
    </div>
    """

    return shell("Action Center", body)


@app.get("/actions/new", response_class=HTMLResponse)
def new_action_form():
    pid = project_id()
    c = db()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    project = c.execute(
        "SELECT name,number FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    c.close()

    owner_options = '<option value="">Unassigned</option>'
    owner_options += '<option value="Superintendent">Superintendent</option>'
    owner_options += '<option value="Project Manager">Project Manager</option>'

    for s in subs:
        owner_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    project_label = "Current Project"
    if project:
        project_label = f'{esc(project["number"])} - {esc(project["name"])}'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Action Center</div>
        <h1>Add Action Item</h1>
        <div class="muted">{project_label}</div>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/actions/new">

            <label>Action</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Confirm switchgear delivery date"
                required
            >

            <label>Owner</label>
            <select name="owner">
                {owner_options}
            </select>

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" required>
                </div>
            </div>

            <label>Notes</label>
            <textarea
                name="notes"
                placeholder="What specifically needs to happen?"
            ></textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Action</button>

                <a href="/actions"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Action", body)


@app.post("/actions/new")
def create_action(
    title: str = Form(...),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    notes: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        INSERT INTO action_items(
            project_id,
            title,
            owner,
            priority,
            due,
            status,
            notes,
            created
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            title.strip(),
            owner.strip(),
            priority,
            due,
            "OPEN",
            notes.strip(),
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/actions", status_code=303)


@app.post("/actions/{action_id}/complete")
def complete_action(action_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE action_items
        SET status='COMPLETE'
        WHERE id=? AND project_id=?
        """,
        (action_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/actions", status_code=303)


def readiness_result(row):
    checks = [
        row["drawings"],
        row["material"],
        row["manpower"],
        row["predecessor"],
        row["access_ready"],
        row["inspection"],
        row["equipment"],
    ]

    complete = sum(1 for x in checks if x)
    pct = round((complete / len(checks)) * 100)

    critical_ready = bool(
        row["drawings"]
        and row["material"]
        and row["manpower"]
        and row["predecessor"]
        and row["access_ready"]
    )

    if complete == len(checks):
        status = "READY"
        badge = "READY"
    elif critical_ready and complete >= 5:
        status = "AT RISK"
        badge = "WATCH"
    else:
        status = "NOT READY"
        badge = "CRITICAL"

    return pct, status, badge


@app.get("/readiness", response_class=HTMLResponse)
def readiness_page():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT *
        FROM activities
        WHERE project_id=? AND status!='COMPLETE'
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    readiness_rows = c.execute(
        """
        SELECT *
        FROM activity_readiness
        WHERE project_id=?
        """,
        (pid,)
    ).fetchall()

    c.close()

    readiness_by_activity = {
        r["activity_id"]: r
        for r in readiness_rows
    }

    cards = ""
    ready_count = 0
    risk_count = 0
    not_ready_count = 0

    for a in activities:
        r = readiness_by_activity.get(a["id"])

        if r:
            pct, status, badge = readiness_result(r)
            notes = esc(r["notes"]) or "No readiness notes."
        else:
            pct, status, badge = 0, "NOT READY", "CRITICAL"
            notes = "Readiness has not been reviewed."

        if status == "READY":
            ready_count += 1
        elif status == "AT RISK":
            risk_count += 1
        else:
            not_ready_count += 1

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <div class="eyebrow">{esc(a["external_id"])}</div>
                    <h3 style="margin:6px 0;">{esc(a["name"])}</h3>
                    <div class="muted">{esc(a["trade"])} · Starts {esc(a["start"])}</div>
                </div>

                <span class="badge {badge}">{status}</span>
            </div>

            <div style="margin-top:14px;">
                <div class="label">Readiness</div>
                <div class="kpi">{pct}%</div>
            </div>

            <p>{notes}</p>

            <a href="/readiness/{a["id"]}"
               style="color:#f0b44d;text-decoration:none;font-weight:700;">
                Review Readiness →
            </a>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No incomplete activities found.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Lookahead Readiness</div>
        <h1>Is upcoming work actually ready to start?</h1>
        <div class="muted">
            Verify the conditions that must be true before crews hit the field.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Activities Reviewed</div>
            <div class="kpi">{len(activities)}</div>
        </div>

        <div class="card">
            <div class="label">Ready</div>
            <div class="kpi">{ready_count}</div>
        </div>

        <div class="card">
            <div class="label">At Risk</div>
            <div class="kpi">{risk_count}</div>
        </div>

        <div class="card">
            <div class="label">Not Ready</div>
            <div class="kpi">{not_ready_count}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Readiness", body)


@app.get("/readiness/{activity_id}", response_class=HTMLResponse)
def readiness_form(activity_id: int):
    pid = project_id()
    c = db()

    activity = c.execute(
        """
        SELECT *
        FROM activities
        WHERE id=? AND project_id=?
        """,
        (activity_id, pid)
    ).fetchone()

    row = c.execute(
        """
        SELECT *
        FROM activity_readiness
        WHERE activity_id=? AND project_id=?
        """,
        (activity_id, pid)
    ).fetchone()

    c.close()

    if not activity:
        return RedirectResponse(url="/readiness", status_code=303)

    def checked(field):
        return "checked" if row and row[field] else ""

    notes = esc(row["notes"]) if row else ""

    body = f"""
    <div class="hero">
        <div class="eyebrow">Lookahead Readiness</div>
        <h1>{esc(activity["external_id"])} - {esc(activity["name"])}</h1>
        <div class="muted">
            {esc(activity["trade"])} · {esc(activity["start"])} to {esc(activity["finish"])}
        </div>
    </div>

    <div class="card" style="max-width:820px;">
        <h2>Start Readiness Checklist</h2>

        <form method="post" action="/readiness/{activity_id}">
            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="drawings" value="1" {checked("drawings")}>
                <span class="form-check-label">Drawings / approved information are available</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="material" value="1" {checked("material")}>
                <span class="form-check-label">Required material is on site or confirmed</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="manpower" value="1" {checked("manpower")}>
                <span class="form-check-label">Manpower is committed</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="predecessor" value="1" {checked("predecessor")}>
                <span class="form-check-label">Predecessor work is complete</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="access_ready" value="1" {checked("access_ready")}>
                <span class="form-check-label">Work area and access are ready</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="inspection" value="1" {checked("inspection")}>
                <span class="form-check-label">Required inspections / prerequisites are cleared</span>
            </label>

            <label class="form-check">
                <input class="form-check-input" type="checkbox" name="equipment" value="1" {checked("equipment")}>
                <span class="form-check-label">Required equipment / tools are available</span>
            </label>

            <label style="display:block;margin-top:18px;">Readiness Notes</label>
            <textarea
                name="notes"
                placeholder="What is missing, who owns it, or what needs to happen before start?"
            >{notes}</textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Readiness</button>

                <a href="/readiness"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Back
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Readiness Review", body)


@app.post("/readiness/{activity_id}")
def save_readiness(
    activity_id: int,
    drawings: int = Form(0),
    material: int = Form(0),
    manpower: int = Form(0),
    predecessor: int = Form(0),
    access_ready: int = Form(0),
    inspection: int = Form(0),
    equipment: int = Form(0),
    notes: str = Form("")
):
    pid = project_id()
    c = db()

    activity = c.execute(
        "SELECT id FROM activities WHERE id=? AND project_id=?",
        (activity_id, pid)
    ).fetchone()

    if activity:
        c.execute(
            """
            INSERT INTO activity_readiness(
                project_id,
                activity_id,
                drawings,
                material,
                manpower,
                predecessor,
                access_ready,
                inspection,
                equipment,
                notes,
                updated
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id, activity_id)
            DO UPDATE SET
                drawings=excluded.drawings,
                material=excluded.material,
                manpower=excluded.manpower,
                predecessor=excluded.predecessor,
                access_ready=excluded.access_ready,
                inspection=excluded.inspection,
                equipment=excluded.equipment,
                notes=excluded.notes,
                updated=excluded.updated
            """,
            (
                pid,
                activity_id,
                1 if drawings else 0,
                1 if material else 0,
                1 if manpower else 0,
                1 if predecessor else 0,
                1 if access_ready else 0,
                1 if inspection else 0,
                1 if equipment else 0,
                notes.strip(),
                date.today().isoformat()
            )
        )
        c.commit()

    c.close()

    return RedirectResponse(url="/readiness", status_code=303)


def schedule_health_status(activity, readiness_row, production_rows, risk_rows):
    today = date.today().isoformat()

    pct = activity["pct"] or 0
    start = activity["start"] or ""
    finish = activity["finish"] or ""

    reasons = []
    score = 0

    if finish and finish < today and pct < 100:
        score += 40
        reasons.append("Finish date has passed and activity is incomplete.")

    if start and start <= today and pct == 0 and activity["status"] != "COMPLETE":
        score += 20
        reasons.append("Activity has started by date but shows 0% complete.")

    if readiness_row:
        readiness_pct, readiness_status, _ = readiness_result(readiness_row)

        if readiness_status == "NOT READY":
            score += 25
            reasons.append(f"Readiness is only {readiness_pct}% and activity is NOT READY.")
        elif readiness_status == "AT RISK":
            score += 12
            reasons.append(f"Readiness is {readiness_pct}% and activity is AT RISK.")

    related_risks = [
        r for r in risk_rows
        if r["activity_id"] == activity["id"]
    ]

    for r in related_risks:
        if r["band"] == "CRITICAL":
            score += 25
            reasons.append("Critical risk is open.")
        elif r["band"] == "HIGH":
            score += 15
            reasons.append("High risk is open.")

    related_prod = [
        p for p in production_rows
        if p["activity_id"] == activity["id"]
    ]

    if related_prod:
        latest = related_prod[0]
        qty = latest["qty"] or 0
        plan = latest["planned_qty"] or 0

        if plan > 0:
            prod_pct = (qty / plan) * 100

            if prod_pct < 90:
                score += 20
                reasons.append(f"Latest production is {prod_pct:.0f}% of plan.")
            elif prod_pct < 100:
                score += 8
                reasons.append(f"Latest production is {prod_pct:.0f}% of plan.")

    score = min(100, score)

    if score >= 60:
        status = "CRITICAL"
        badge = "CRITICAL"
    elif score >= 35:
        status = "HIGH"
        badge = "HIGH"
    elif score >= 15:
        status = "WATCH"
        badge = "WATCH"
    else:
        status = "STABLE"
        badge = "READY"

    if not reasons:
        reasons.append("No significant schedule-health warning signals found.")

    return score, status, badge, reasons


@app.get("/schedule-health", response_class=HTMLResponse)
def schedule_health_page():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT *
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    readiness_rows = c.execute(
        """
        SELECT *
        FROM activity_readiness
        WHERE project_id=?
        """,
        (pid,)
    ).fetchall()

    production_rows = c.execute(
        """
        SELECT *
        FROM production
        WHERE project_id=?
        ORDER BY work_date DESC,id DESC
        """,
        (pid,)
    ).fetchall()

    risk_rows = c.execute(
        """
        SELECT *
        FROM risks
        WHERE project_id=?
        """,
        (pid,)
    ).fetchall()

    c.close()

    readiness_by_activity = {
        r["activity_id"]: r
        for r in readiness_rows
    }

    health_rows = []

    for a in activities:
        score, status, badge, reasons = schedule_health_status(
            a,
            readiness_by_activity.get(a["id"]),
            production_rows,
            risk_rows
        )

        health_rows.append(
            (score, a, status, badge, reasons)
        )

    health_rows.sort(key=lambda x: x[0], reverse=True)

    critical_count = sum(1 for x in health_rows if x[2] == "CRITICAL")
    high_count = sum(1 for x in health_rows if x[2] == "HIGH")
    watch_count = sum(1 for x in health_rows if x[2] == "WATCH")
    stable_count = sum(1 for x in health_rows if x[2] == "STABLE")

    cards = ""

    for score, a, status, badge, reasons in health_rows:
        reason_html = "".join(
            f'<div class="small">• {esc(reason)}</div>'
            for reason in reasons
        )

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <div class="eyebrow">{esc(a["external_id"])}</div>
                    <h3 style="margin:6px 0;">{esc(a["name"])}</h3>
                    <div class="muted">
                        {esc(a["trade"])} · {esc(a["start"])} to {esc(a["finish"])}
                    </div>
                </div>

                <span class="badge {badge}">{status}</span>
            </div>

            <div class="grid3" style="margin-top:16px;">
                <div>
                    <div class="label">Health Score</div>
                    <div class="kpi">{score}</div>
                </div>

                <div>
                    <div class="label">Complete</div>
                    <div class="kpi">{(a["pct"] or 0):.0f}%</div>
                </div>

                <div>
                    <div class="label">Status</div>
                    <div style="font-size:18px;font-weight:800;">
                        {esc(a["status"]).replace("_"," ").title()}
                    </div>
                </div>
            </div>

            <div style="margin-top:14px;">
                {reason_html}
            </div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No schedule activities found.</div></div>'

    body = f"""
    <div class="hero">
        <div class="eyebrow">Schedule Health</div>
        <h1>Which activities are drifting before the schedule update catches it?</h1>
        <div class="muted">
            Combines schedule dates, progress, readiness, production, and risk signals.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Critical</div>
            <div class="kpi">{critical_count}</div>
        </div>

        <div class="card">
            <div class="label">High</div>
            <div class="kpi">{high_count}</div>
        </div>

        <div class="card">
            <div class="label">Watch</div>
            <div class="kpi">{watch_count}</div>
        </div>

        <div class="card">
            <div class="label">Stable</div>
            <div class="kpi">{stable_count}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Schedule Health", body)


@app.get("/project-settings", response_class=HTMLResponse)
def project_settings_page():
    pid = project_id()
    c = db()

    project = c.execute(
        "SELECT * FROM projects WHERE id=?",
        (pid,)
    ).fetchone()

    counts = {
        "activities": c.execute(
            "SELECT COUNT(*) n FROM activities WHERE project_id=?",
            (pid,)
        ).fetchone()["n"],
        "risks": c.execute(
            "SELECT COUNT(*) n FROM risks WHERE project_id=?",
            (pid,)
        ).fetchone()["n"],
        "make_ready": c.execute(
            "SELECT COUNT(*) n FROM make_ready WHERE project_id=?",
            (pid,)
        ).fetchone()["n"],
        "daily_reports": c.execute(
            "SELECT COUNT(*) n FROM daily_reports WHERE project_id=?",
            (pid,)
        ).fetchone()["n"],
    }

    c.close()

    if not project:
        return RedirectResponse(url="/", status_code=303)

    statuses = ["ACTIVE", "PLANNING", "ON_HOLD", "COMPLETE"]
    status_options = "".join(
        f'<option value="{s}" {"selected" if project["status"] == s else ""}>{s.replace("_"," ").title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Project Settings</div>
        <h1>{esc(project["number"])} - {esc(project["name"])}</h1>
        <div class="muted">
            Manage the selected project's identity and lifecycle.
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Activities</div>
            <div class="kpi">{counts["activities"]}</div>
        </div>

        <div class="card">
            <div class="label">Risks</div>
            <div class="kpi">{counts["risks"]}</div>
        </div>

        <div class="card">
            <div class="label">Make Ready</div>
            <div class="kpi">{counts["make_ready"]}</div>
        </div>

        <div class="card">
            <div class="label">Daily Reports</div>
            <div class="kpi">{counts["daily_reports"]}</div>
        </div>
    </div>

    <div class="grid2">
        <div class="card">
            <h2>Edit Project</h2>

            <form method="post" action="/project-settings/edit">
                <label>Project Name</label>
                <input
                    type="text"
                    name="name"
                    value="{esc(project["name"])}"
                    required
                >

                <label>Project Number</label>
                <input
                    type="text"
                    name="number"
                    value="{esc(project["number"])}"
                    required
                >

                <label>Status</label>
                <select name="status">
                    {status_options}
                </select>

                <button type="submit">Save Project Changes</button>
            </form>
        </div>

        <div class="card">
            <h2>Delete Project</h2>

            <p>
                Deleting a project removes its schedule activities, risks, make-ready items,
                field updates, production records, daily reports, subcontractor updates,
                actions, readiness records, recovery scenarios, and saved analyses.
            </p>

            <div class="small">
                This cannot be undone.
            </div>

            <form method="post"
                  action="/project-settings/delete"
                  style="margin-top:18px;">
                <label>Type DELETE to confirm</label>
                <input
                    type="text"
                    name="confirm_text"
                    placeholder="DELETE"
                    required
                >

                <button type="submit"
                        style="background:#492324;color:#ffb0b0;">
                    Delete Project
                </button>
            </form>
        </div>
    </div>
    """

    return shell("Project Settings", body)


@app.post("/project-settings/edit")
def edit_project_settings(
    name: str = Form(...),
    number: str = Form(...),
    status: str = Form(...)
):
    pid = project_id()

    allowed_statuses = {"ACTIVE", "PLANNING", "ON_HOLD", "COMPLETE"}
    if status not in allowed_statuses:
        status = "ACTIVE"

    c = db()
    c.execute(
        """
        UPDATE projects
        SET name=?, number=?, status=?
        WHERE id=?
        """,
        (
            name.strip(),
            number.strip(),
            status,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/project-settings", status_code=303)


@app.post("/project-settings/delete")
def delete_project_settings(confirm_text: str = Form(...)):
    pid = project_id()

    if confirm_text.strip().upper() != "DELETE":
        return RedirectResponse(url="/project-settings", status_code=303)

    c = db()

    activity_ids = [
        r["id"]
        for r in c.execute(
            "SELECT id FROM activities WHERE project_id=?",
            (pid,)
        ).fetchall()
    ]

    # Delete project-scoped tables first.
    tables = [
        "daily_report_analysis",
        "daily_reports",
        "subcontractor_updates",
        "action_items",
        "activity_readiness",
        "procurement",
        "project_issues",
        "punch_items",
        "inspections_tracker",
        "submittals",
        "safety_items",
        "change_events",
        "meeting_notes",
        "field_updates",
        "production",
        "make_ready",
        "risks",
        "recovery",
        "memory",
        "subs",
    ]

    for table in tables:
        c.execute(
            f"DELETE FROM {table} WHERE project_id=?",
            (pid,)
        )

    c.execute(
        "DELETE FROM activities WHERE project_id=?",
        (pid,)
    )

    c.execute(
        "DELETE FROM projects WHERE id=?",
        (pid,)
    )

    next_project = c.execute(
        "SELECT id FROM projects ORDER BY id LIMIT 1"
    ).fetchone()

    if next_project:
        c.execute(
            """
            INSERT INTO app_state(id, selected_project_id)
            VALUES(1, ?)
            ON CONFLICT(id)
            DO UPDATE SET selected_project_id=excluded.selected_project_id
            """,
            (next_project["id"],)
        )
    else:
        c.execute(
            "DELETE FROM app_state WHERE id=1"
        )

    c.commit()
    c.close()

    if next_project:
        return RedirectResponse(url="/", status_code=303)

    return RedirectResponse(url="/projects/new", status_code=303)


def procurement_risk(required_on_site, promised_date, status):
    if status == "DELIVERED":
        return "READY", "DELIVERED"

    if not required_on_site:
        return "WATCH", "NO REQUIRED DATE"

    if not promised_date:
        return "CRITICAL", "NO PROMISED DATE"

    if promised_date > required_on_site:
        return "CRITICAL", "LATE"

    if status in ["RELEASED", "FABRICATION", "SHIPPED"]:
        return "WATCH", status

    return "HIGH", status or "OPEN"


@app.get("/procurement", response_class=HTMLResponse)
def procurement_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM procurement p
        LEFT JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY
            CASE p.status
                WHEN 'DELIVERED' THEN 5
                WHEN 'SHIPPED' THEN 4
                WHEN 'FABRICATION' THEN 3
                WHEN 'RELEASED' THEN 2
                ELSE 1
            END,
            p.required_on_site
        """,
        (pid,)
    ).fetchall()

    c.close()

    critical = 0
    watch = 0
    delivered = 0

    cards = ""

    for r in rows:
        badge, risk_text = procurement_risk(
            r["required_on_site"],
            r["promised_date"],
            r["status"]
        )

        if badge == "CRITICAL":
            critical += 1
        elif badge == "WATCH":
            watch += 1

        if r["status"] == "DELIVERED":
            delivered += 1

        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{risk_text}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["item"])}</h3>
                    <div class="muted">{activity_text}</div>
                </div>

                <a href="/procurement/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <div class="grid3" style="margin-top:16px;">
                <div>
                    <div class="label">Vendor / Sub</div>
                    <div>{esc(r["vendor"]) or "—"}</div>
                </div>

                <div>
                    <div class="label">Required On Site</div>
                    <div>{esc(r["required_on_site"]) or "—"}</div>
                </div>

                <div>
                    <div class="label">Promised</div>
                    <div>{esc(r["promised_date"]) or "—"}</div>
                </div>
            </div>

            <p>{esc(r["notes"]) or "No notes entered."}</p>
            <div class="small">Status: {esc(r["status"]).replace("_"," ").title()}</div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No procurement items added yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Procurement Intelligence</div>
                <h1>Know what material can hurt the schedule before it arrives late.</h1>
                <div class="muted">
                    Track required-on-site dates, vendor commitments, and long-lead exposure.
                </div>
            </div>

            <a href="/procurement/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Procurement Item
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Items</div>
            <div class="kpi">{len(rows)}</div>
        </div>

        <div class="card">
            <div class="label">Critical</div>
            <div class="kpi">{critical}</div>
        </div>

        <div class="card">
            <div class="label">Watch</div>
            <div class="kpi">{watch}</div>
        </div>

        <div class="card">
            <div class="label">Delivered</div>
            <div class="kpi">{delivered}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Procurement", body)


@app.get("/procurement/new", response_class=HTMLResponse)
def new_procurement_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    options = '<option value="">No linked activity</option>'
    options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Procurement Intelligence</div>
        <h1>Add Procurement Item</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/procurement/new">

            <label>Material / Equipment</label>
            <input
                type="text"
                name="item"
                placeholder="Example: Main electrical switchgear"
                required
            >

            <label>Linked Activity</label>
            <select name="activity_id">
                {options}
            </select>

            <label>Vendor / Subcontractor</label>
            <input
                type="text"
                name="vendor"
                placeholder="Example: Valley Electric / Eaton"
            >

            <div class="grid2">
                <div>
                    <label>Required On Site</label>
                    <input type="date" name="required_on_site" required>
                </div>

                <div>
                    <label>Promised Date</label>
                    <input type="date" name="promised_date">
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                <option value="NOT_RELEASED">Not Released</option>
                <option value="RELEASED">Released</option>
                <option value="FABRICATION">Fabrication</option>
                <option value="SHIPPED">Shipped</option>
                <option value="DELIVERED">Delivered</option>
            </select>

            <label>Notes</label>
            <textarea
                name="notes"
                placeholder="Lead time, submittal status, release issue, delivery commitment, etc."
            ></textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Procurement Item</button>

                <a href="/procurement"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Add Procurement", body)


@app.post("/procurement/new")
def create_procurement(
    item: str = Form(...),
    activity_id: str = Form(""),
    vendor: str = Form(""),
    required_on_site: str = Form(...),
    promised_date: str = Form(""),
    status: str = Form("NOT_RELEASED"),
    notes: str = Form("")
):
    pid = project_id()

    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid_activity = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()

        if not valid_activity:
            linked_activity = None

    c.execute(
        """
        INSERT INTO procurement(
            project_id,
            activity_id,
            item,
            vendor,
            required_on_site,
            promised_date,
            status,
            notes,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            item.strip(),
            vendor.strip(),
            required_on_site,
            promised_date,
            status,
            notes.strip(),
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/procurement", status_code=303)


@app.get("/procurement/{item_id}/edit", response_class=HTMLResponse)
def edit_procurement_form(item_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT *
        FROM procurement
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    ).fetchone()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    if not item:
        return RedirectResponse(url="/procurement", status_code=303)

    activity_options = '<option value="">No linked activity</option>'

    for a in activities:
        selected = "selected" if item["activity_id"] == a["id"] else ""
        activity_options += (
            f'<option value="{a["id"]}" {selected}>'
            f'{esc(a["external_id"])} - {esc(a["name"])}</option>'
        )

    statuses = [
        "NOT_RELEASED",
        "RELEASED",
        "FABRICATION",
        "SHIPPED",
        "DELIVERED"
    ]

    status_options = "".join(
        f'<option value="{s}" {"selected" if item["status"] == s else ""}>'
        f'{s.replace("_"," ").title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Procurement Intelligence</div>
        <h1>Edit Procurement Item</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/procurement/{item_id}/edit">

            <label>Material / Equipment</label>
            <input type="text" name="item" value="{esc(item["item"])}" required>

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Vendor / Subcontractor</label>
            <input type="text" name="vendor" value="{esc(item["vendor"])}">

            <div class="grid2">
                <div>
                    <label>Required On Site</label>
                    <input type="date" name="required_on_site" value="{esc(item["required_on_site"])}" required>
                </div>

                <div>
                    <label>Promised Date</label>
                    <input type="date" name="promised_date" value="{esc(item["promised_date"])}">
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                {status_options}
            </select>

            <label>Notes</label>
            <textarea name="notes">{esc(item["notes"])}</textarea>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>

                <a href="/procurement"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>

        <form method="post"
              action="/procurement/{item_id}/delete"
              style="margin-top:22px;padding-top:18px;border-top:1px solid #213042;">
            <button type="submit"
                    style="background:#492324;color:#ffb0b0;">
                Delete Procurement Item
            </button>
        </form>
    </div>
    """

    return shell("Edit Procurement", body)


@app.post("/procurement/{item_id}/edit")
def edit_procurement(
    item_id: int,
    item: str = Form(...),
    activity_id: str = Form(""),
    vendor: str = Form(""),
    required_on_site: str = Form(...),
    promised_date: str = Form(""),
    status: str = Form("NOT_RELEASED"),
    notes: str = Form("")
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid_activity = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()

        if not valid_activity:
            linked_activity = None

    c.execute(
        """
        UPDATE procurement
        SET activity_id=?,
            item=?,
            vendor=?,
            required_on_site=?,
            promised_date=?,
            status=?,
            notes=?
        WHERE id=? AND project_id=?
        """,
        (
            linked_activity,
            item.strip(),
            vendor.strip(),
            required_on_site,
            promised_date,
            status,
            notes.strip(),
            item_id,
            pid
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/procurement", status_code=303)


@app.post("/procurement/{item_id}/delete")
def delete_procurement(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        "DELETE FROM procurement WHERE id=? AND project_id=?",
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/procurement", status_code=303)


@app.get("/issues", response_class=HTMLResponse)
def issues_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM project_issues i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=?
        ORDER BY
            CASE i.status
                WHEN 'OPEN' THEN 1
                WHEN 'ANSWERED' THEN 2
                WHEN 'CLOSED' THEN 3
                ELSE 4
            END,
            CASE i.priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                ELSE 4
            END,
            i.due
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()
    open_items = [r for r in rows if r["status"] == "OPEN"]
    overdue = [r for r in open_items if r["due"] and r["due"] < today]
    answered = [r for r in rows if r["status"] == "ANSWERED"]

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        overdue_text = ""
        if r["status"] == "OPEN" and r["due"] and r["due"] < today:
            overdue_text = " · OVERDUE"

        badge = r["priority"] if r["priority"] in ["CRITICAL","HIGH","WATCH","LOW"] else "OPEN"

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["priority"])}</span>
                    <span class="badge OPEN">{esc(r["issue_type"])}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">{activity_text}</div>
                </div>

                <a href="/issues/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <p>{esc(r["description"]) or "No description entered."}</p>
            <div class="small">
                Owner: {esc(r["owner"]) or "Unassigned"} · Due {esc(r["due"]) or "—"}{overdue_text}
            </div>

            <div class="small" style="margin-top:8px;">
                Status: {esc(r["status"])}
            </div>

            {"<p><b>Response:</b> " + esc(r["response"]) + "</p>" if r["response"] else ""}
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No RFIs or project issues logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">RFIs / Issues</div>
                <h1>Track unanswered questions before they stop the field.</h1>
                <div class="muted">
                    Capture ownership, due dates, responses, and schedule exposure.
                </div>
            </div>

            <a href="/issues/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add RFI / Issue
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue)}</div>
        </div>

        <div class="card">
            <div class="label">Answered</div>
            <div class="kpi">{len(answered)}</div>
        </div>

        <div class="card">
            <div class="label">Total</div>
            <div class="kpi">{len(rows)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("RFIs / Issues", body)


@app.get("/issues/new", response_class=HTMLResponse)
def new_issue_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    owner_options = '<option value="">Unassigned</option>'
    owner_options += '<option value="Architect / Engineer">Architect / Engineer</option>'
    owner_options += '<option value="Owner">Owner</option>'
    owner_options += '<option value="Project Manager">Project Manager</option>'
    owner_options += '<option value="Superintendent">Superintendent</option>'

    for s in subs:
        owner_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    body = f"""
    <div class="hero">
        <div class="eyebrow">RFIs / Issues</div>
        <h1>Add RFI or Project Issue</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/issues/new">

            <label>Type</label>
            <select name="issue_type">
                <option value="RFI">RFI</option>
                <option value="FIELD_ISSUE">Field Issue</option>
                <option value="DESIGN">Design Issue</option>
                <option value="COORDINATION">Coordination</option>
                <option value="OWNER_DECISION">Owner Decision</option>
            </select>

            <label>Title</label>
            <input
                type="text"
                name="title"
                placeholder="Example: RFI - Beam conflict with duct route"
                required
            >

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Owner / Responsible Party</label>
            <select name="owner">
                {owner_options}
            </select>

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" required>
                </div>
            </div>

            <label>Description</label>
            <textarea
                name="description"
                placeholder="What is the question, conflict, or decision needed?"
                required
            ></textarea>

            <button type="submit">Save RFI / Issue</button>
        </form>
    </div>
    """

    return shell("Add RFI / Issue", body)


@app.post("/issues/new")
def create_issue(
    issue_type: str = Form(...),
    title: str = Form(...),
    activity_id: str = Form(""),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    description: str = Form(...)
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()

        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO project_issues(
            project_id,
            activity_id,
            issue_type,
            title,
            owner,
            due,
            priority,
            status,
            description,
            response,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            issue_type,
            title.strip(),
            owner.strip(),
            due,
            priority,
            "OPEN",
            description.strip(),
            "",
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/issues", status_code=303)


@app.get("/issues/{issue_id}/edit", response_class=HTMLResponse)
def edit_issue_form(issue_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT *
        FROM project_issues
        WHERE id=? AND project_id=?
        """,
        (issue_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/issues", status_code=303)

    statuses = ["OPEN", "ANSWERED", "CLOSED"]
    status_options = "".join(
        f'<option value="{s}" {"selected" if item["status"] == s else ""}>{s.title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">RFIs / Issues</div>
        <h1>Edit {esc(item["issue_type"])}</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/issues/{issue_id}/edit">

            <label>Title</label>
            <input type="text" name="title" value="{esc(item["title"])}" required>

            <label>Owner / Responsible Party</label>
            <input type="text" name="owner" value="{esc(item["owner"])}">

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL" {"selected" if item["priority"]=="CRITICAL" else ""}>Critical</option>
                        <option value="HIGH" {"selected" if item["priority"]=="HIGH" else ""}>High</option>
                        <option value="WATCH" {"selected" if item["priority"]=="WATCH" else ""}>Watch</option>
                        <option value="LOW" {"selected" if item["priority"]=="LOW" else ""}>Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" value="{esc(item["due"])}" required>
                </div>
            </div>

            <label>Description</label>
            <textarea name="description" required>{esc(item["description"])}</textarea>

            <label>Response / Resolution</label>
            <textarea name="response">{esc(item["response"])}</textarea>

            <label>Status</label>
            <select name="status">
                {status_options}
            </select>

            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;">
                <button type="submit">Save Changes</button>
                <a href="/issues"
                   style="display:inline-block;color:#f0b44d;text-decoration:none;padding:10px 4px;font-weight:700;">
                    Cancel
                </a>
            </div>
        </form>
    </div>
    """

    return shell("Edit RFI / Issue", body)


@app.post("/issues/{issue_id}/edit")
def edit_issue(
    issue_id: int,
    title: str = Form(...),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    description: str = Form(...),
    response: str = Form(""),
    status: str = Form("OPEN")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE project_issues
        SET title=?,
            owner=?,
            priority=?,
            due=?,
            description=?,
            response=?,
            status=?
        WHERE id=? AND project_id=?
        """,
        (
            title.strip(),
            owner.strip(),
            priority,
            due,
            description.strip(),
            response.strip(),
            status,
            issue_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/issues", status_code=303)


@app.get("/punch", response_class=HTMLResponse)
def punch_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT p.*, a.external_id, a.name activity
        FROM punch_items p
        LEFT JOIN activities a ON a.id=p.activity_id
        WHERE p.project_id=?
        ORDER BY
            CASE p.status
                WHEN 'OPEN' THEN 1
                WHEN 'CORRECTED' THEN 2
                WHEN 'VERIFIED' THEN 3
                ELSE 4
            END,
            CASE p.priority
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                ELSE 4
            END,
            p.due
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()

    open_items = [r for r in rows if r["status"] == "OPEN"]
    corrected_items = [r for r in rows if r["status"] == "CORRECTED"]
    verified_items = [r for r in rows if r["status"] == "VERIFIED"]
    overdue_items = [
        r for r in open_items
        if r["due"] and r["due"] < today
    ]

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        badge = (
            r["priority"]
            if r["priority"] in ["CRITICAL", "HIGH", "WATCH", "LOW"]
            else "OPEN"
        )

        overdue_text = ""
        if r["status"] == "OPEN" and r["due"] and r["due"] < today:
            overdue_text = " · OVERDUE"

        if r["status"] == "OPEN":
            action_buttons = f"""
            <form method="post" action="/punch/{r["id"]}/corrected">
                <button type="submit">Mark Corrected</button>
            </form>
            """
        elif r["status"] == "CORRECTED":
            action_buttons = f"""
            <form method="post" action="/punch/{r["id"]}/verified">
                <button type="submit">Verify Complete</button>
            </form>
            """
        else:
            action_buttons = '<span class="badge COMPLETE">VERIFIED</span>'

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["priority"])}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">
                        {esc(r["location"]) or "No location"} · {esc(r["trade"]) or "No trade"}
                    </div>
                </div>

                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    {action_buttons}
                    <a href="/punch/{r["id"]}/edit"
                       style="color:#f0b44d;text-decoration:none;font-weight:700;padding:10px 2px;">
                        Edit
                    </a>
                </div>
            </div>

            <p>{esc(r["description"]) or "No description entered."}</p>

            <div class="small">
                {activity_text}
            </div>

            <div class="small">
                Owner: {esc(r["owner"]) or "Unassigned"} ·
                Due {esc(r["due"]) or "—"}{overdue_text} ·
                Status: {esc(r["status"])}
            </div>

            {"<p><b>Resolution:</b> " + esc(r["resolution"]) + "</p>" if r["resolution"] else ""}
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No punch or quality items logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Quality / Punch List</div>
                <h1>Track deficiencies until they are corrected and verified.</h1>
                <div class="muted">
                    Assign ownership, due dates, location, trade, and final resolution.
                </div>
            </div>

            <a href="/punch/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Punch Item
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue_items)}</div>
        </div>

        <div class="card">
            <div class="label">Corrected</div>
            <div class="kpi">{len(corrected_items)}</div>
        </div>

        <div class="card">
            <div class="label">Verified</div>
            <div class="kpi">{len(verified_items)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Punch List", body)


@app.get("/punch/new", response_class=HTMLResponse)
def new_punch_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    owner_options = '<option value="">Unassigned</option>'
    owner_options += '<option value="Superintendent">Superintendent</option>'
    owner_options += '<option value="Project Manager">Project Manager</option>'

    for s in subs:
        owner_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Quality / Punch List</div>
        <h1>Add Punch Item</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/punch/new">

            <label>Item Title</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Repair damaged drywall at Room 214"
                required
            >

            <label>Location</label>
            <input
                type="text"
                name="location"
                placeholder="Example: Level 2 - Room 214"
            >

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Trade</label>
            <input
                type="text"
                name="trade"
                placeholder="Example: Drywall"
            >

            <label>Owner / Responsible Party</label>
            <select name="owner">
                {owner_options}
            </select>

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option>
                        <option value="WATCH">Watch</option>
                        <option value="LOW">Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" required>
                </div>
            </div>

            <label>Description</label>
            <textarea
                name="description"
                placeholder="Describe the deficiency and what acceptable correction looks like."
                required
            ></textarea>

            <button type="submit">Save Punch Item</button>
        </form>
    </div>
    """

    return shell("Add Punch Item", body)


@app.post("/punch/new")
def create_punch(
    title: str = Form(...),
    location: str = Form(""),
    activity_id: str = Form(""),
    trade: str = Form(""),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    description: str = Form(...)
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()

        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO punch_items(
            project_id,
            activity_id,
            title,
            location,
            trade,
            owner,
            priority,
            due,
            status,
            description,
            resolution,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            title.strip(),
            location.strip(),
            trade.strip(),
            owner.strip(),
            priority,
            due,
            "OPEN",
            description.strip(),
            "",
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/punch", status_code=303)


@app.get("/punch/{item_id}/edit", response_class=HTMLResponse)
def edit_punch_form(item_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT *
        FROM punch_items
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/punch", status_code=303)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Quality / Punch List</div>
        <h1>Edit Punch Item</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/punch/{item_id}/edit">

            <label>Item Title</label>
            <input type="text" name="title" value="{esc(item["title"])}" required>

            <label>Location</label>
            <input type="text" name="location" value="{esc(item["location"])}">

            <label>Trade</label>
            <input type="text" name="trade" value="{esc(item["trade"])}">

            <label>Owner</label>
            <input type="text" name="owner" value="{esc(item["owner"])}">

            <div class="grid2">
                <div>
                    <label>Priority</label>
                    <select name="priority">
                        <option value="CRITICAL" {"selected" if item["priority"]=="CRITICAL" else ""}>Critical</option>
                        <option value="HIGH" {"selected" if item["priority"]=="HIGH" else ""}>High</option>
                        <option value="WATCH" {"selected" if item["priority"]=="WATCH" else ""}>Watch</option>
                        <option value="LOW" {"selected" if item["priority"]=="LOW" else ""}>Low</option>
                    </select>
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due" value="{esc(item["due"])}" required>
                </div>
            </div>

            <label>Description</label>
            <textarea name="description" required>{esc(item["description"])}</textarea>

            <label>Resolution</label>
            <textarea name="resolution">{esc(item["resolution"])}</textarea>

            <button type="submit">Save Changes</button>
        </form>
    </div>
    """

    return shell("Edit Punch Item", body)


@app.post("/punch/{item_id}/edit")
def edit_punch(
    item_id: int,
    title: str = Form(...),
    location: str = Form(""),
    trade: str = Form(""),
    owner: str = Form(""),
    priority: str = Form("HIGH"),
    due: str = Form(...),
    description: str = Form(...),
    resolution: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE punch_items
        SET title=?,
            location=?,
            trade=?,
            owner=?,
            priority=?,
            due=?,
            description=?,
            resolution=?
        WHERE id=? AND project_id=?
        """,
        (
            title.strip(),
            location.strip(),
            trade.strip(),
            owner.strip(),
            priority,
            due,
            description.strip(),
            resolution.strip(),
            item_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/punch", status_code=303)


@app.post("/punch/{item_id}/corrected")
def mark_punch_corrected(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE punch_items
        SET status='CORRECTED'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/punch", status_code=303)


@app.post("/punch/{item_id}/verified")
def mark_punch_verified(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE punch_items
        SET status='VERIFIED'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/punch", status_code=303)


@app.get("/inspections", response_class=HTMLResponse)
def inspections_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT i.*, a.external_id, a.name activity
        FROM inspections_tracker i
        LEFT JOIN activities a ON a.id=i.activity_id
        WHERE i.project_id=?
        ORDER BY
            CASE i.result
                WHEN 'FAILED' THEN 1
                WHEN 'PENDING' THEN 2
                WHEN 'SCHEDULED' THEN 3
                WHEN 'PASSED' THEN 4
                ELSE 5
            END,
            i.scheduled_date
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()

    pending = [r for r in rows if r["result"] in ["PENDING", "SCHEDULED"]]
    failed = [r for r in rows if r["result"] == "FAILED"]
    passed = [r for r in rows if r["result"] == "PASSED"]
    overdue = [
        r for r in pending
        if r["scheduled_date"] and r["scheduled_date"] < today
    ]

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        if r["result"] == "PASSED":
            badge = "READY"
        elif r["result"] == "FAILED":
            badge = "CRITICAL"
        elif r["result"] == "SCHEDULED":
            badge = "WATCH"
        else:
            badge = "OPEN"

        reinspection = (
            f' · Reinspection {esc(r["reinspection_date"])}'
            if r["reinspection_date"]
            else ""
        )

        overdue_text = ""
        if r["result"] in ["PENDING", "SCHEDULED"] and r["scheduled_date"] and r["scheduled_date"] < today:
            overdue_text = " · OVERDUE"

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["result"])}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["inspection_type"])}</h3>
                    <div class="muted">{activity_text}</div>
                </div>

                <a href="/inspections/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <p>{esc(r["notes"]) or "No notes entered."}</p>

            <div class="small">
                Authority: {esc(r["authority"]) or "—"} ·
                Scheduled {esc(r["scheduled_date"]) or "—"}{overdue_text}{reinspection}
            </div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No inspections logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Inspection Intelligence</div>
                <h1>Know which inspection can stop the next activity.</h1>
                <div class="muted">
                    Track scheduled inspections, pass/fail results, authorities, and reinspections.
                </div>
            </div>

            <a href="/inspections/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Inspection
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Pending</div>
            <div class="kpi">{len(pending)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue)}</div>
        </div>

        <div class="card">
            <div class="label">Failed</div>
            <div class="kpi">{len(failed)}</div>
        </div>

        <div class="card">
            <div class="label">Passed</div>
            <div class="kpi">{len(passed)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Inspections", body)


@app.get("/inspections/new", response_class=HTMLResponse)
def new_inspection_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Inspection Intelligence</div>
        <h1>Add Inspection</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/inspections/new">

            <label>Inspection Type</label>
            <input
                type="text"
                name="inspection_type"
                placeholder="Example: Above Ceiling Inspection"
                required
            >

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Authority / Inspector</label>
            <input
                type="text"
                name="authority"
                placeholder="Example: City of Phoenix"
            >

            <label>Scheduled Date</label>
            <input type="date" name="scheduled_date" required>

            <label>Result</label>
            <select name="result">
                <option value="PENDING">Pending</option>
                <option value="SCHEDULED">Scheduled</option>
                <option value="PASSED">Passed</option>
                <option value="FAILED">Failed</option>
            </select>

            <label>Reinspection Date</label>
            <input type="date" name="reinspection_date">

            <label>Notes</label>
            <textarea
                name="notes"
                placeholder="Required corrections, prerequisites, inspector comments, etc."
            ></textarea>

            <button type="submit">Save Inspection</button>
        </form>
    </div>
    """

    return shell("Add Inspection", body)


@app.post("/inspections/new")
def create_inspection(
    inspection_type: str = Form(...),
    activity_id: str = Form(""),
    authority: str = Form(""),
    scheduled_date: str = Form(...),
    result: str = Form("PENDING"),
    reinspection_date: str = Form(""),
    notes: str = Form("")
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()
        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO inspections_tracker(
            project_id,
            activity_id,
            inspection_type,
            authority,
            scheduled_date,
            result,
            reinspection_date,
            notes,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            inspection_type.strip(),
            authority.strip(),
            scheduled_date,
            result,
            reinspection_date,
            notes.strip(),
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/inspections", status_code=303)


@app.get("/inspections/{inspection_id}/edit", response_class=HTMLResponse)
def edit_inspection_form(inspection_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        """
        SELECT *
        FROM inspections_tracker
        WHERE id=? AND project_id=?
        """,
        (inspection_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/inspections", status_code=303)

    results = ["PENDING", "SCHEDULED", "PASSED", "FAILED"]
    result_options = "".join(
        f'<option value="{r}" {"selected" if item["result"] == r else ""}>{r.title()}</option>'
        for r in results
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Inspection Intelligence</div>
        <h1>Edit Inspection</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/inspections/{inspection_id}/edit">

            <label>Inspection Type</label>
            <input type="text" name="inspection_type" value="{esc(item["inspection_type"])}" required>

            <label>Authority / Inspector</label>
            <input type="text" name="authority" value="{esc(item["authority"])}">

            <label>Scheduled Date</label>
            <input type="date" name="scheduled_date" value="{esc(item["scheduled_date"])}" required>

            <label>Result</label>
            <select name="result">
                {result_options}
            </select>

            <label>Reinspection Date</label>
            <input type="date" name="reinspection_date" value="{esc(item["reinspection_date"])}">

            <label>Notes</label>
            <textarea name="notes">{esc(item["notes"])}</textarea>

            <button type="submit">Save Changes</button>
        </form>
    </div>
    """

    return shell("Edit Inspection", body)


@app.post("/inspections/{inspection_id}/edit")
def edit_inspection(
    inspection_id: int,
    inspection_type: str = Form(...),
    authority: str = Form(""),
    scheduled_date: str = Form(...),
    result: str = Form("PENDING"),
    reinspection_date: str = Form(""),
    notes: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE inspections_tracker
        SET inspection_type=?,
            authority=?,
            scheduled_date=?,
            result=?,
            reinspection_date=?,
            notes=?
        WHERE id=? AND project_id=?
        """,
        (
            inspection_type.strip(),
            authority.strip(),
            scheduled_date,
            result,
            reinspection_date,
            notes.strip(),
            inspection_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/inspections", status_code=303)


@app.get("/submittals", response_class=HTMLResponse)
def submittals_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT s.*, a.external_id, a.name activity
        FROM submittals s
        LEFT JOIN activities a ON a.id=s.activity_id
        WHERE s.project_id=?
        ORDER BY
            CASE s.status
                WHEN 'REJECTED' THEN 1
                WHEN 'PENDING' THEN 2
                WHEN 'SUBMITTED' THEN 3
                WHEN 'APPROVED_AS_NOTED' THEN 4
                WHEN 'APPROVED' THEN 5
                ELSE 6
            END,
            s.due_date
        """,
        (pid,)
    ).fetchall()

    c.close()

    today = date.today().isoformat()

    pending = [r for r in rows if r["status"] in ["PENDING", "SUBMITTED"]]
    rejected = [r for r in rows if r["status"] == "REJECTED"]
    approved = [r for r in rows if r["status"] in ["APPROVED", "APPROVED_AS_NOTED"]]
    overdue = [
        r for r in pending
        if r["due_date"] and r["due_date"] < today
    ]

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        if r["status"] == "REJECTED":
            badge = "CRITICAL"
        elif r["status"] in ["PENDING", "SUBMITTED"]:
            badge = "WATCH"
        elif r["status"] in ["APPROVED", "APPROVED_AS_NOTED"]:
            badge = "READY"
        else:
            badge = "OPEN"

        overdue_text = ""
        if r["status"] in ["PENDING", "SUBMITTED"] and r["due_date"] and r["due_date"] < today:
            overdue_text = " · OVERDUE"

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["status"]).replace("_"," ")}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">{activity_text}</div>
                </div>

                <a href="/submittals/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <div class="grid3" style="margin-top:14px;">
                <div>
                    <div class="label">Spec Section</div>
                    <div>{esc(r["spec_section"]) or "—"}</div>
                </div>

                <div>
                    <div class="label">Responsible</div>
                    <div>{esc(r["responsible_party"]) or "—"}</div>
                </div>

                <div>
                    <div class="label">Due</div>
                    <div>{esc(r["due_date"]) or "—"}{overdue_text}</div>
                </div>
            </div>

            <p>{esc(r["notes"]) or "No notes entered."}</p>
            <div class="small">Sent: {esc(r["sent_date"]) or "—"}</div>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No submittals logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Document Control</div>
                <h1>Track submittals before approvals become schedule blockers.</h1>
                <div class="muted">
                    Monitor due dates, approval status, responsible party, and linked work.
                </div>
            </div>

            <a href="/submittals/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Submittal
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Pending</div>
            <div class="kpi">{len(pending)}</div>
        </div>

        <div class="card">
            <div class="label">Overdue</div>
            <div class="kpi">{len(overdue)}</div>
        </div>

        <div class="card">
            <div class="label">Rejected</div>
            <div class="kpi">{len(rejected)}</div>
        </div>

        <div class="card">
            <div class="label">Approved</div>
            <div class="kpi">{len(approved)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Submittals", body)


@app.get("/submittals/new", response_class=HTMLResponse)
def new_submittal_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    responsible_options = '<option value="">Unassigned</option>'
    responsible_options += '<option value="Project Manager">Project Manager</option>'
    responsible_options += '<option value="Architect / Engineer">Architect / Engineer</option>'

    for s in subs:
        responsible_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Document Control</div>
        <h1>Add Submittal</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/submittals/new">

            <label>Submittal Title</label>
            <input type="text" name="title" placeholder="Example: Electrical Switchgear Product Data" required>

            <label>Spec Section</label>
            <input type="text" name="spec_section" placeholder="Example: 26 24 16">

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Subcontractor / Responsible Party</label>
            <input
                type="text"
                name="responsible_party"
                list="responsible_parties"
                placeholder="Type or select a subcontractor"
            >
            <datalist id="responsible_parties">
                <option value="Project Manager">
                <option value="Architect / Engineer">
                {''.join(f'<option value="{esc(s["name"])}">{esc(s["trade"])}</option>' for s in subs)}
            </datalist>

            <div class="grid2">
                <div>
                    <label>Sent Date</label>
                    <input type="date" name="sent_date">
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due_date" required>
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                <option value="PENDING">Pending</option>
                <option value="SUBMITTED">Submitted</option>
                <option value="APPROVED">Approved</option>
                <option value="APPROVED_AS_NOTED">Approved As Noted</option>
                <option value="REJECTED">Rejected / Revise & Resubmit</option>
            </select>

            <label>Notes</label>
            <textarea name="notes" placeholder="Review comments, lead-time impact, release constraints, etc."></textarea>

            <button type="submit">Save Submittal</button>
        </form>
    </div>
    """

    return shell("Add Submittal", body)


@app.post("/submittals/new")
def create_submittal(
    title: str = Form(...),
    spec_section: str = Form(""),
    activity_id: str = Form(""),
    responsible_party: str = Form(""),
    sent_date: str = Form(""),
    due_date: str = Form(...),
    status: str = Form("PENDING"),
    notes: str = Form("")
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()
        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO submittals(
            project_id, activity_id, title, spec_section, responsible_party,
            sent_date, due_date, status, notes, created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            title.strip(),
            spec_section.strip(),
            responsible_party.strip(),
            sent_date,
            due_date,
            status,
            notes.strip(),
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/submittals", status_code=303)


@app.get("/submittals/{submittal_id}/edit", response_class=HTMLResponse)
def edit_submittal_form(submittal_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        "SELECT * FROM submittals WHERE id=? AND project_id=?",
        (submittal_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/submittals", status_code=303)

    statuses = ["PENDING", "SUBMITTED", "APPROVED", "APPROVED_AS_NOTED", "REJECTED"]
    status_options = "".join(
        f'<option value="{s}" {"selected" if item["status"] == s else ""}>{s.replace("_"," ").title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Document Control</div>
        <h1>Edit Submittal</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/submittals/{submittal_id}/edit">

            <label>Submittal Title</label>
            <input type="text" name="title" value="{esc(item["title"])}" required>

            <label>Spec Section</label>
            <input type="text" name="spec_section" value="{esc(item["spec_section"])}">

            <label>Subcontractor / Responsible Party</label>
            <input
                type="text"
                name="responsible_party"
                value="{esc(item["responsible_party"])}"
                placeholder="Type subcontractor or responsible party"
            >

            <div class="grid2">
                <div>
                    <label>Sent Date</label>
                    <input type="date" name="sent_date" value="{esc(item["sent_date"])}">
                </div>

                <div>
                    <label>Due Date</label>
                    <input type="date" name="due_date" value="{esc(item["due_date"])}" required>
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                {status_options}
            </select>

            <label>Notes</label>
            <textarea name="notes">{esc(item["notes"])}</textarea>

            <button type="submit">Save Changes</button>
        </form>
    </div>
    """

    return shell("Edit Submittal", body)


@app.post("/submittals/{submittal_id}/edit")
def edit_submittal(
    submittal_id: int,
    title: str = Form(...),
    spec_section: str = Form(""),
    responsible_party: str = Form(""),
    sent_date: str = Form(""),
    due_date: str = Form(...),
    status: str = Form("PENDING"),
    notes: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE submittals
        SET title=?,
            spec_section=?,
            responsible_party=?,
            sent_date=?,
            due_date=?,
            status=?,
            notes=?
        WHERE id=? AND project_id=?
        """,
        (
            title.strip(),
            spec_section.strip(),
            responsible_party.strip(),
            sent_date,
            due_date,
            status,
            notes.strip(),
            submittal_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/submittals", status_code=303)


@app.get("/safety", response_class=HTMLResponse)
def safety_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT s.*, a.external_id, a.name activity
        FROM safety_items s
        LEFT JOIN activities a ON a.id=s.activity_id
        WHERE s.project_id=?
        ORDER BY
            CASE s.status
                WHEN 'OPEN' THEN 1
                WHEN 'CORRECTED' THEN 2
                WHEN 'CLOSED' THEN 3
                ELSE 4
            END,
            CASE s.severity
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'WATCH' THEN 3
                ELSE 4
            END,
            s.event_date DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    open_items = [r for r in rows if r["status"] == "OPEN"]
    corrected = [r for r in rows if r["status"] == "CORRECTED"]
    closed = [r for r in rows if r["status"] == "CLOSED"]
    critical = [r for r in open_items if r["severity"] == "CRITICAL"]

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        badge = (
            r["severity"]
            if r["severity"] in ["CRITICAL","HIGH","WATCH","LOW"]
            else "OPEN"
        )

        if r["status"] == "OPEN":
            action_button = f"""
            <form method="post" action="/safety/{r["id"]}/corrected">
                <button type="submit">Mark Corrected</button>
            </form>
            """
        elif r["status"] == "CORRECTED":
            action_button = f"""
            <form method="post" action="/safety/{r["id"]}/closed">
                <button type="submit">Close Item</button>
            </form>
            """
        else:
            action_button = '<span class="badge COMPLETE">CLOSED</span>'

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["severity"])}</span>
                    <span class="badge OPEN">{esc(r["item_type"]).replace("_"," ")}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">{esc(r["location"]) or "No location"} · {activity_text}</div>
                </div>

                <div>{action_button}</div>
            </div>

            <p>{esc(r["description"]) or "No description entered."}</p>

            <div class="small">
                Date: {esc(r["event_date"])} ·
                Responsible: {esc(r["responsible_party"]) or "Unassigned"} ·
                Status: {esc(r["status"])}
            </div>

            {"<p><b>Corrective Action:</b> " + esc(r["corrective_action"]) + "</p>" if r["corrective_action"] else ""}
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No safety items logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Safety Intelligence</div>
                <h1>Track observations, near misses, and corrective actions.</h1>
                <div class="muted">
                    Keep safety issues visible until they are corrected and closed.
                </div>
            </div>

            <a href="/safety/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Safety Item
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Critical</div>
            <div class="kpi">{len(critical)}</div>
        </div>

        <div class="card">
            <div class="label">Corrected</div>
            <div class="kpi">{len(corrected)}</div>
        </div>

        <div class="card">
            <div class="label">Closed</div>
            <div class="kpi">{len(closed)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Safety", body)


@app.get("/safety/new", response_class=HTMLResponse)
def new_safety_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    responsible_options = '<option value="">Unassigned</option>'
    responsible_options += '<option value="Superintendent">Superintendent</option>'
    responsible_options += '<option value="Safety Manager">Safety Manager</option>'

    for s in subs:
        responsible_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Safety Intelligence</div>
        <h1>Add Safety Item</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/safety/new">

            <label>Type</label>
            <select name="item_type">
                <option value="OBSERVATION">Observation</option>
                <option value="NEAR_MISS">Near Miss</option>
                <option value="INCIDENT">Incident</option>
                <option value="CORRECTIVE_ACTION">Corrective Action</option>
            </select>

            <label>Title</label>
            <input type="text" name="title" placeholder="Example: Missing guardrail at stair opening" required>

            <label>Event Date</label>
            <input type="date" name="event_date" value="{date.today().isoformat()}" required>

            <label>Location</label>
            <input type="text" name="location" placeholder="Example: Level 3 west stair">

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Responsible Party</label>
            <select name="responsible_party">
                {responsible_options}
            </select>

            <label>Severity</label>
            <select name="severity">
                <option value="CRITICAL">Critical</option>
                <option value="HIGH">High</option>
                <option value="WATCH">Watch</option>
                <option value="LOW">Low</option>
            </select>

            <label>Description</label>
            <textarea name="description" placeholder="Describe the condition or event." required></textarea>

            <label>Corrective Action</label>
            <textarea name="corrective_action" placeholder="What must be done to correct or prevent recurrence?"></textarea>

            <button type="submit">Save Safety Item</button>
        </form>
    </div>
    """

    return shell("Add Safety Item", body)


@app.post("/safety/new")
def create_safety_item(
    item_type: str = Form(...),
    title: str = Form(...),
    event_date: str = Form(...),
    location: str = Form(""),
    activity_id: str = Form(""),
    responsible_party: str = Form(""),
    severity: str = Form("WATCH"),
    description: str = Form(...),
    corrective_action: str = Form("")
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()
        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO safety_items(
            project_id,
            activity_id,
            event_date,
            item_type,
            title,
            location,
            responsible_party,
            severity,
            status,
            description,
            corrective_action,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            event_date,
            item_type,
            title.strip(),
            location.strip(),
            responsible_party.strip(),
            severity,
            "OPEN",
            description.strip(),
            corrective_action.strip(),
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/safety", status_code=303)


@app.post("/safety/{item_id}/corrected")
def mark_safety_corrected(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE safety_items
        SET status='CORRECTED'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/safety", status_code=303)


@app.post("/safety/{item_id}/closed")
def mark_safety_closed(item_id: int):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE safety_items
        SET status='CLOSED'
        WHERE id=? AND project_id=?
        """,
        (item_id, pid)
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/safety", status_code=303)


@app.get("/changes", response_class=HTMLResponse)
def changes_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT ce.*, a.external_id, a.name activity
        FROM change_events ce
        LEFT JOIN activities a ON a.id=ce.activity_id
        WHERE ce.project_id=?
        ORDER BY
            CASE ce.status
                WHEN 'OPEN' THEN 1
                WHEN 'PRICING' THEN 2
                WHEN 'SUBMITTED' THEN 3
                WHEN 'APPROVED' THEN 4
                WHEN 'REJECTED' THEN 5
                ELSE 6
            END,
            ce.id DESC
        """,
        (pid,)
    ).fetchall()

    c.close()

    open_items = [r for r in rows if r["status"] in ["OPEN", "PRICING", "SUBMITTED"]]
    approved = [r for r in rows if r["status"] == "APPROVED"]
    rejected = [r for r in rows if r["status"] == "REJECTED"]

    open_cost = sum((r["estimated_cost"] or 0) for r in open_items)
    open_days = sum((r["schedule_days"] or 0) for r in open_items)

    cards = ""

    for r in rows:
        activity_text = (
            f'{esc(r["external_id"])} - {esc(r["activity"])}'
            if r["external_id"]
            else "No linked activity"
        )

        if r["status"] == "APPROVED":
            badge = "READY"
        elif r["status"] == "REJECTED":
            badge = "LOW"
        elif r["status"] == "SUBMITTED":
            badge = "WATCH"
        elif r["status"] == "PRICING":
            badge = "HIGH"
        else:
            badge = "OPEN"

        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge {badge}">{esc(r["status"])}</span>
                    <span class="badge OPEN">{esc(r["event_type"]).replace("_"," ")}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">{activity_text}</div>
                </div>

                <a href="/changes/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <div class="grid3" style="margin-top:16px;">
                <div>
                    <div class="label">Estimated Cost</div>
                    <div class="kpi">${(r["estimated_cost"] or 0):,.0f}</div>
                </div>

                <div>
                    <div class="label">Schedule Impact</div>
                    <div class="kpi">{(r["schedule_days"] or 0):.1f}</div>
                    <div class="small">days</div>
                </div>

                <div>
                    <div class="label">Responsible</div>
                    <div>{esc(r["responsible_party"]) or "Unassigned"}</div>
                </div>
            </div>

            <p>{esc(r["description"]) or "No description entered."}</p>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No change events logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Change Intelligence</div>
                <h1>Track field changes before cost and schedule impact get lost.</h1>
                <div class="muted">
                    Capture scope changes, owner decisions, design changes, and potential change orders.
                </div>
            </div>

            <a href="/changes/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Change Event
            </a>
        </div>
    </div>

    <div class="grid4">
        <div class="card">
            <div class="label">Open Events</div>
            <div class="kpi">{len(open_items)}</div>
        </div>

        <div class="card">
            <div class="label">Open Exposure</div>
            <div class="kpi">${open_cost:,.0f}</div>
        </div>

        <div class="card">
            <div class="label">Potential Days</div>
            <div class="kpi">{open_days:.1f}</div>
        </div>

        <div class="card">
            <div class="label">Approved</div>
            <div class="kpi">{len(approved)}</div>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Change Events", body)


@app.get("/changes/new", response_class=HTMLResponse)
def new_change_form():
    pid = project_id()
    c = db()

    activities = c.execute(
        """
        SELECT id,external_id,name
        FROM activities
        WHERE project_id=?
        ORDER BY start,name
        """,
        (pid,)
    ).fetchall()

    subs = c.execute(
        """
        SELECT name,trade
        FROM subs
        WHERE project_id=?
        ORDER BY trade,name
        """,
        (pid,)
    ).fetchall()

    c.close()

    activity_options = '<option value="">No linked activity</option>'
    activity_options += "".join(
        f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>'
        for a in activities
    )

    responsible_options = '<option value="">Unassigned</option>'
    responsible_options += '<option value="Owner">Owner</option>'
    responsible_options += '<option value="Architect / Engineer">Architect / Engineer</option>'
    responsible_options += '<option value="General Contractor">General Contractor</option>'

    for s in subs:
        responsible_options += (
            f'<option value="{esc(s["name"])}">{esc(s["name"])} - {esc(s["trade"])}</option>'
        )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Change Intelligence</div>
        <h1>Add Change Event</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/changes/new">

            <label>Change Type</label>
            <select name="event_type">
                <option value="OWNER_CHANGE">Owner Change</option>
                <option value="DESIGN_CHANGE">Design Change</option>
                <option value="SCOPE_GAP">Scope Gap</option>
                <option value="FIELD_CONDITION">Field Condition</option>
                <option value="RFI_IMPACT">RFI Impact</option>
                <option value="POTENTIAL_CHANGE_ORDER">Potential Change Order</option>
            </select>

            <label>Title</label>
            <input
                type="text"
                name="title"
                placeholder="Example: Added power for imaging equipment"
                required
            >

            <label>Linked Activity</label>
            <select name="activity_id">
                {activity_options}
            </select>

            <label>Responsible Party</label>
            <select name="responsible_party">
                {responsible_options}
            </select>

            <div class="grid2">
                <div>
                    <label>Estimated Cost Impact</label>
                    <input
                        type="number"
                        name="estimated_cost"
                        min="0"
                        step="100"
                        value="0"
                    >
                </div>

                <div>
                    <label>Schedule Impact (Days)</label>
                    <input
                        type="number"
                        name="schedule_days"
                        min="0"
                        step="0.5"
                        value="0"
                    >
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                <option value="OPEN">Open</option>
                <option value="PRICING">Pricing</option>
                <option value="SUBMITTED">Submitted</option>
                <option value="APPROVED">Approved</option>
                <option value="REJECTED">Rejected</option>
            </select>

            <label>Description</label>
            <textarea
                name="description"
                placeholder="Describe the change, cause, scope, cost exposure, and schedule impact."
                required
            ></textarea>

            <button type="submit">Save Change Event</button>
        </form>
    </div>
    """

    return shell("Add Change Event", body)


@app.post("/changes/new")
def create_change_event(
    event_type: str = Form(...),
    title: str = Form(...),
    activity_id: str = Form(""),
    responsible_party: str = Form(""),
    estimated_cost: float = Form(0),
    schedule_days: float = Form(0),
    status: str = Form("OPEN"),
    description: str = Form(...)
):
    pid = project_id()
    linked_activity = int(activity_id) if activity_id.strip() else None

    estimated_cost = max(0.0, estimated_cost)
    schedule_days = max(0.0, schedule_days)

    c = db()

    if linked_activity is not None:
        valid = c.execute(
            "SELECT id FROM activities WHERE id=? AND project_id=?",
            (linked_activity, pid)
        ).fetchone()
        if not valid:
            linked_activity = None

    c.execute(
        """
        INSERT INTO change_events(
            project_id,
            activity_id,
            event_type,
            title,
            responsible_party,
            estimated_cost,
            schedule_days,
            status,
            description,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            linked_activity,
            event_type,
            title.strip(),
            responsible_party.strip(),
            estimated_cost,
            schedule_days,
            status,
            description.strip(),
            date.today().isoformat()
        )
    )

    c.commit()
    c.close()

    return RedirectResponse(url="/changes", status_code=303)


@app.get("/changes/{change_id}/edit", response_class=HTMLResponse)
def edit_change_form(change_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        "SELECT * FROM change_events WHERE id=? AND project_id=?",
        (change_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/changes", status_code=303)

    statuses = ["OPEN", "PRICING", "SUBMITTED", "APPROVED", "REJECTED"]
    status_options = "".join(
        f'<option value="{s}" {"selected" if item["status"] == s else ""}>{s.title()}</option>'
        for s in statuses
    )

    body = f"""
    <div class="hero">
        <div class="eyebrow">Change Intelligence</div>
        <h1>Edit Change Event</h1>
    </div>

    <div class="card" style="max-width:760px;">
        <form method="post" action="/changes/{change_id}/edit">

            <label>Title</label>
            <input type="text" name="title" value="{esc(item["title"])}" required>

            <label>Responsible Party</label>
            <input type="text" name="responsible_party" value="{esc(item["responsible_party"])}">

            <div class="grid2">
                <div>
                    <label>Estimated Cost</label>
                    <input
                        type="number"
                        name="estimated_cost"
                        min="0"
                        step="100"
                        value="{item["estimated_cost"] or 0}"
                    >
                </div>

                <div>
                    <label>Schedule Impact (Days)</label>
                    <input
                        type="number"
                        name="schedule_days"
                        min="0"
                        step="0.5"
                        value="{item["schedule_days"] or 0}"
                    >
                </div>
            </div>

            <label>Status</label>
            <select name="status">
                {status_options}
            </select>

            <label>Description</label>
            <textarea name="description" required>{esc(item["description"])}</textarea>

            <button type="submit">Save Changes</button>
        </form>
    </div>
    """

    return shell("Edit Change Event", body)


@app.post("/changes/{change_id}/edit")
def edit_change_event(
    change_id: int,
    title: str = Form(...),
    responsible_party: str = Form(""),
    estimated_cost: float = Form(0),
    schedule_days: float = Form(0),
    status: str = Form("OPEN"),
    description: str = Form(...)
):
    pid = project_id()

    estimated_cost = max(0.0, estimated_cost)
    schedule_days = max(0.0, schedule_days)

    c = db()
    c.execute(
        """
        UPDATE change_events
        SET title=?,
            responsible_party=?,
            estimated_cost=?,
            schedule_days=?,
            status=?,
            description=?
        WHERE id=? AND project_id=?
        """,
        (
            title.strip(),
            responsible_party.strip(),
            estimated_cost,
            schedule_days,
            status,
            description.strip(),
            change_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/changes", status_code=303)


@app.get("/meetings", response_class=HTMLResponse)
def meetings_page():
    pid = project_id()
    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM meeting_notes
        WHERE project_id=?
        ORDER BY meeting_date DESC, id DESC
        LIMIT 30
        """,
        (pid,)
    ).fetchall()

    c.close()

    cards = ""

    for r in rows:
        cards += f"""
        <div class="card">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
                <div>
                    <span class="badge OPEN">{esc(r["meeting_type"]).replace("_"," ")}</span>
                    <h3 style="margin:10px 0 4px;">{esc(r["title"])}</h3>
                    <div class="muted">{esc(r["meeting_date"])}</div>
                </div>

                <a href="/meetings/{r["id"]}/edit"
                   style="color:#f0b44d;text-decoration:none;font-weight:700;">
                    Edit
                </a>
            </div>

            <div class="small">Attendees</div>
            <p>{esc(r["attendees"]) or "—"}</p>

            <div class="small">Decisions</div>
            <p>{esc(r["decisions"]) or "—"}</p>

            <div class="small">Commitments</div>
            <p>{esc(r["commitments"]) or "—"}</p>

            <div class="small">Follow-Up</div>
            <p>{esc(r["follow_up"]) or "—"}</p>
        </div>
        """

    if not cards:
        cards = '<div class="card"><div class="muted">No meeting notes logged yet.</div></div>'

    body = f"""
    <div class="hero">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;">
            <div>
                <div class="eyebrow">Meeting Intelligence</div>
                <h1>Capture decisions and commitments before they disappear into notes.</h1>
                <div class="muted">
                    Track OAC meetings, trade coordination, owner decisions, and follow-up.
                </div>
            </div>

            <a href="/meetings/new"
               style="display:inline-block;background:#f0b44d;color:#0a1017;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:800;">
                + Add Meeting Notes
            </a>
        </div>
    </div>

    <div class="grid2">
        {cards}
    </div>
    """

    return shell("Meetings", body)


@app.get("/meetings/new", response_class=HTMLResponse)
def new_meeting_form():
    body = f"""
    <div class="hero">
        <div class="eyebrow">Meeting Intelligence</div>
        <h1>Add Meeting Notes</h1>
    </div>

    <div class="card" style="max-width:820px;">
        <form method="post" action="/meetings/new">

            <label>Meeting Date</label>
            <input type="date" name="meeting_date" value="{date.today().isoformat()}" required>

            <label>Meeting Type</label>
            <select name="meeting_type">
                <option value="OAC">OAC</option>
                <option value="SUBCONTRACTOR_COORDINATION">Subcontractor Coordination</option>
                <option value="OWNER_MEETING">Owner Meeting</option>
                <option value="INTERNAL">Internal</option>
                <option value="SAFETY">Safety</option>
                <option value="PREINSTALL">Pre-Installation</option>
            </select>

            <label>Title</label>
            <input type="text" name="title" placeholder="Example: Weekly OAC Meeting" required>

            <label>Attendees</label>
            <textarea name="attendees" placeholder="Who attended?"></textarea>

            <label>Decisions Made</label>
            <textarea name="decisions" placeholder="What was decided?"></textarea>

            <label>Commitments</label>
            <textarea name="commitments" placeholder="Who committed to what and by when?"></textarea>

            <label>Follow-Up</label>
            <textarea name="follow_up" placeholder="What needs to happen next?"></textarea>

            <button type="submit">Save Meeting Notes</button>
        </form>
    </div>
    """

    return shell("Add Meeting Notes", body)


@app.post("/meetings/new")
def create_meeting(
    meeting_date: str = Form(...),
    meeting_type: str = Form(...),
    title: str = Form(...),
    attendees: str = Form(""),
    decisions: str = Form(""),
    commitments: str = Form(""),
    follow_up: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        INSERT INTO meeting_notes(
            project_id,
            meeting_date,
            meeting_type,
            title,
            attendees,
            decisions,
            commitments,
            follow_up,
            created
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            meeting_date,
            meeting_type,
            title.strip(),
            attendees.strip(),
            decisions.strip(),
            commitments.strip(),
            follow_up.strip(),
            date.today().isoformat()
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/meetings", status_code=303)


@app.get("/meetings/{meeting_id}/edit", response_class=HTMLResponse)
def edit_meeting_form(meeting_id: int):
    pid = project_id()
    c = db()

    item = c.execute(
        "SELECT * FROM meeting_notes WHERE id=? AND project_id=?",
        (meeting_id, pid)
    ).fetchone()

    c.close()

    if not item:
        return RedirectResponse(url="/meetings", status_code=303)

    body = f"""
    <div class="hero">
        <div class="eyebrow">Meeting Intelligence</div>
        <h1>Edit Meeting Notes</h1>
    </div>

    <div class="card" style="max-width:820px;">
        <form method="post" action="/meetings/{meeting_id}/edit">

            <label>Meeting Date</label>
            <input type="date" name="meeting_date" value="{esc(item["meeting_date"])}" required>

            <label>Title</label>
            <input type="text" name="title" value="{esc(item["title"])}" required>

            <label>Attendees</label>
            <textarea name="attendees">{esc(item["attendees"])}</textarea>

            <label>Decisions Made</label>
            <textarea name="decisions">{esc(item["decisions"])}</textarea>

            <label>Commitments</label>
            <textarea name="commitments">{esc(item["commitments"])}</textarea>

            <label>Follow-Up</label>
            <textarea name="follow_up">{esc(item["follow_up"])}</textarea>

            <button type="submit">Save Changes</button>
        </form>
    </div>
    """

    return shell("Edit Meeting Notes", body)


@app.post("/meetings/{meeting_id}/edit")
def edit_meeting(
    meeting_id: int,
    meeting_date: str = Form(...),
    title: str = Form(...),
    attendees: str = Form(""),
    decisions: str = Form(""),
    commitments: str = Form(""),
    follow_up: str = Form("")
):
    pid = project_id()

    c = db()
    c.execute(
        """
        UPDATE meeting_notes
        SET meeting_date=?,
            title=?,
            attendees=?,
            decisions=?,
            commitments=?,
            follow_up=?
        WHERE id=? AND project_id=?
        """,
        (
            meeting_date,
            title.strip(),
            attendees.strip(),
            decisions.strip(),
            commitments.strip(),
            follow_up.strip(),
            meeting_id,
            pid
        )
    )
    c.commit()
    c.close()

    return RedirectResponse(url="/meetings", status_code=303)


def safe_filename(name):
    base=os.path.basename(name or "upload"); cleaned="".join(ch for ch in base if ch.isalnum() or ch in "._- ")
    return cleaned[:120] or "upload"


def refresh_notifications(pid=None):
    cid=current_company_id()
    if not cid or not pid: return
    c=db(); c.execute("DELETE FROM notifications WHERE company_id=? AND project_id=? AND source='AUTO'",(cid,pid)); today=date.today().isoformat()
    def add(sev,title,detail):
        c.execute("INSERT INTO notifications(company_id,project_id,severity,title,detail,source,status,created) VALUES(?,?,?,?,?,'AUTO','UNREAD',?)",(cid,pid,sev,title,detail,datetime.utcnow().isoformat()))
    for r in c.execute("SELECT * FROM action_items WHERE project_id=? AND status='OPEN' AND due!='' AND due<?",(pid,today)).fetchall(): add(r["priority"] or "HIGH",f'Overdue action: {r["title"]}',f'Owner: {r["owner"] or "Unassigned"} · Due {r["due"]}')
    for r in c.execute("SELECT * FROM procurement WHERE project_id=? AND status!='DELIVERED' AND promised_date!='' AND required_on_site!='' AND promised_date>required_on_site",(pid,)).fetchall(): add("CRITICAL",f'Late procurement: {r["item"]}',f'Promised {r["promised_date"]}; required {r["required_on_site"]}')
    for r in c.execute("SELECT * FROM inspections_tracker WHERE project_id=? AND result='FAILED'",(pid,)).fetchall(): add("CRITICAL",f'Failed inspection: {r["inspection_type"]}',r["notes"] or "Correction/reinspection required.")
    for r in c.execute("SELECT * FROM submittals WHERE project_id=? AND status='REJECTED'",(pid,)).fetchall(): add("HIGH",f'Rejected submittal: {r["title"]}',r["notes"] or "Revise and resubmit.")
    for r in c.execute("SELECT * FROM safety_items WHERE project_id=? AND status!='CLOSED' AND severity='CRITICAL'",(pid,)).fetchall(): add("CRITICAL",f'Safety: {r["title"]}',r["description"] or "Critical safety item is open.")
    c.commit(); c.close()


@app.get("/notifications",response_class=HTMLResponse)
def notifications_page():
    pid=project_id(); refresh_notifications(pid); c=db(); rows=c.execute("SELECT * FROM notifications WHERE company_id=? AND (project_id=? OR project_id IS NULL) ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'WATCH' THEN 3 ELSE 4 END,id DESC LIMIT 100",(current_company_id(),pid)).fetchall(); c.close()
    cards="".join(f'<div class="card"><span class="badge {r["severity"] if r["severity"] in ["CRITICAL","HIGH","WATCH","LOW","READY"] else "OPEN"}">{esc(r["severity"])}</span><h3>{esc(r["title"])}</h3><p>{esc(r["detail"])}</p><div class="small">{esc(r["created"])}</div></div>' for r in rows) or '<div class="card"><div class="muted">No active notifications.</div></div>'
    return shell("Notifications",f'<div class="hero"><div class="eyebrow">Notifications</div><h1>What needs your attention?</h1></div><div class="card"><form method="post" action="/notifications/email-digest"><button type="submit">Email My Alert Digest</button></form><div class="small">Requires SMTP settings in Render.</div></div><div class="grid2">{cards}</div>')


@app.get("/documents",response_class=HTMLResponse)
def documents_page():
    pid=project_id(); c=db(); rows=c.execute("SELECT a.*,u.display_name FROM attachments a LEFT JOIN users u ON u.id=a.created_by WHERE a.company_id=? AND a.project_id=? ORDER BY a.id DESC",(current_company_id(),pid)).fetchall(); c.close()
    files_html="".join(f'<div class="card"><span class="badge OPEN">{esc(r["category"])}</span><h3>{esc(r["title"] or r["original_name"])}</h3><div class="small">{esc(r["original_name"])} · {(r["size_bytes"] or 0)/1024:.0f} KB</div><p><a href="/documents/{r["id"]}/download" style="color:#f0b44d;font-weight:700;">Download</a></p></div>' for r in rows) or '<div class="card"><div class="muted">No project documents uploaded yet.</div></div>'
    body=f'''<div class="hero"><div class="eyebrow">Document & Photo Center</div><h1>Keep field evidence with the project.</h1><div class="muted">Set UPLOAD_DIR to a Render persistent-disk path for durable storage.</div></div><div class="grid2"><div class="card"><h2>Upload</h2><form method="post" action="/documents/upload" enctype="multipart/form-data"><label>Category</label><select name="category"><option>PHOTO</option><option>DAILY_REPORT</option><option>RFI</option><option>PUNCH</option><option>SAFETY</option><option>SUBMITTAL</option><option>OTHER</option></select><label>Title</label><input name="title"><label>File</label><input type="file" name="file" required><button type="submit">Upload File</button></form></div><div class="card"><h2>Upload Rules</h2><p>Maximum 10 MB. PDF, image, Office, CSV, and text files are accepted.</p></div></div><div class="grid2">{files_html}</div>'''
    return shell("Documents",body)


@app.post("/documents/upload")
async def documents_upload(category:str=Form("OTHER"),title:str=Form(""),file:UploadFile=File(...)):
    pid=project_id(); original=safe_filename(file.filename); ext=Path(original).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS: return HTMLResponse("File type not allowed.",status_code=400)
    data=await file.read()
    if len(data)>MAX_UPLOAD_BYTES: return HTMLResponse("File exceeds the 10 MB limit.",status_code=400)
    stored=f"{secrets.token_hex(12)}{ext}"; path=os.path.join(UPLOAD_DIR,stored)
    with open(path,"wb") as f: f.write(data)
    c=db(); c.execute("INSERT INTO attachments(company_id,project_id,category,title,original_name,stored_name,mime_type,size_bytes,created_by,created) VALUES(?,?,?,?,?,?,?,?,?,?)",(current_company_id(),pid,category,title.strip(),original,stored,file.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream",len(data),current_user_id(),datetime.utcnow().isoformat())); c.commit(); c.close()
    return RedirectResponse("/documents",status_code=303)


@app.get("/documents/{attachment_id}/download")
def document_download(attachment_id:int):
    c=db(); row=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=?",(attachment_id,current_company_id())).fetchone(); c.close()
    if not row: return HTMLResponse("File not found.",status_code=404)
    path=os.path.join(UPLOAD_DIR,row["stored_name"])
    if not os.path.isfile(path): return HTMLResponse("Stored file is unavailable. Configure persistent storage.",status_code=404)
    return FileResponse(path,media_type=row["mime_type"] or "application/octet-stream",filename=row["original_name"])


@app.get("/morning-brief",response_class=HTMLResponse)
def morning_brief_page():
    pid=project_id(); c=db(); row=c.execute("SELECT * FROM morning_briefs WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1",(current_company_id(),pid)).fetchone(); c.close(); brief=esc(row["brief_text"]).replace("\n","<br>") if row else "No AI morning brief has been generated yet."
    return shell("Morning Brief",f'<div class="hero"><div class="eyebrow">Proactive AI</div><h1>Morning Superintendent Brief</h1></div><div class="card"><form method="post" action="/morning-brief/generate"><button type="submit">Generate Current Brief</button></form></div><div class="card"><div style="line-height:1.65;">{brief}</div></div>')


@app.post("/morning-brief/generate")
def generate_morning_brief():
    pid=project_id(); api_key=os.environ.get("OPENAI_API_KEY"); context=build_project_context(pid)
    if api_key and OpenAI is not None:
        try:
            client=OpenAI(api_key=api_key); response=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions="You are BuildCommand AI, a construction superintendent morning-command copilot. Use only supplied project facts. Return: HANDLE FIRST, SCHEDULE THREATS, WHO OWES WHAT, TODAY'S VERIFICATIONS, COST / CHANGE EXPOSURE, SAFETY / QUALITY. Do not invent facts.",input=context); brief=response.output_text
        except Exception as exc: brief=f"AI brief failed: {exc}"
    else: brief="OPENAI_API_KEY is not configured. The rule-based Daily Command dashboard remains available."
    c=db(); c.execute("INSERT INTO morning_briefs(company_id,project_id,brief_date,brief_text,created) VALUES(?,?,?,?,?)",(current_company_id(),pid,date.today().isoformat(),brief,datetime.utcnow().isoformat())); c.commit(); c.close(); return RedirectResponse("/morning-brief",status_code=303)


@app.get("/team",response_class=HTMLResponse)
def team_page():
    user=current_user(); c=db(); rows=c.execute("SELECT * FROM users WHERE company_id=? ORDER BY display_name,email",(current_company_id(),)).fetchall(); c.close(); members="".join(f'<div class="action"><b>{esc(r["display_name"] or r["email"])}</b><div class="small">{esc(r["email"])} · {esc(r["role"])}</div></div>' for r in rows)
    add=''
    if user and user["role"] in ["OWNER","ADMIN"]: add='''<div class="card"><h2>Add Team Member</h2><form method="post" action="/team/add"><label>Name</label><input name="display_name" required><label>Email</label><input type="email" name="email" required><label>Temporary Password</label><input type="password" name="password" minlength="8" required><label>Role</label><select name="role"><option>MEMBER</option><option>ADMIN</option></select><button type="submit">Create User</button></form></div>'''
    return shell("Team",f'<div class="hero"><div class="eyebrow">Accounts</div><h1>Company Team</h1></div><div class="grid2"><div class="card"><h2>Users</h2>{members}</div>{add}</div>')


@app.post("/team/add")
def add_team_member(display_name:str=Form(...),email:str=Form(...),password:str=Form(...),role:str=Form("MEMBER")):
    user=current_user()
    if not user or user["role"] not in ["OWNER","ADMIN"]: return HTMLResponse("Not authorized.",status_code=403)
    if role not in ["MEMBER","ADMIN"]: role="MEMBER"
    c=db()
    if c.execute("SELECT id FROM users WHERE lower(email)=lower(?)",(email.strip(),)).fetchone(): c.close(); return HTMLResponse("That email is already in use.",status_code=400)
    c.execute("INSERT INTO users(company_id,email,display_name,password_hash,role,created) VALUES(?,?,?,?,?,?)",(current_company_id(),email.strip().lower(),display_name.strip(),hash_password(password),role,date.today().isoformat())); c.commit(); c.close(); return RedirectResponse("/team",status_code=303)


@app.get("/company-settings",response_class=HTMLResponse)
def company_settings():
    u=current_user(); return shell("Company Settings",f'''<div class="hero"><div class="eyebrow">Branding</div><h1>Company Settings</h1></div><div class="card" style="max-width:720px;"><form method="post" action="/company-settings"><label>Company Name</label><input name="name" value="{esc(u["company_name"] if u else "")}" required><label>Logo URL (optional)</label><input name="logo_url" value="{esc(u["logo_url"] if u else "")}"><button type="submit">Save Company Settings</button></form></div>''')


@app.post("/company-settings")
def company_settings_save(name:str=Form(...),logo_url:str=Form("")):
    u=current_user()
    if not u or u["role"] not in ["OWNER","ADMIN"]: return HTMLResponse("Not authorized.",status_code=403)
    c=db(); c.execute("UPDATE companies SET name=?,logo_url=? WHERE id=?",(name.strip(),logo_url.strip(),current_company_id())); c.commit(); c.close(); return RedirectResponse("/company-settings",status_code=303)


@app.get("/exports",response_class=HTMLResponse)
def exports_page():
    return shell("Exports",'''<div class="hero"><div class="eyebrow">Reports & Exports</div><h1>Take BuildCommand data with you.</h1></div><div class="grid2"><div class="card"><h2>Project CSV</h2><a href="/exports/project.csv" style="color:#f0b44d;font-weight:700;">Download CSV</a></div><div class="card"><h2>Project JSON</h2><a href="/exports/project.json" style="color:#f0b44d;font-weight:700;">Download JSON</a></div><div class="card"><h2>Latest Daily Report PDF</h2><a href="/exports/daily-report.pdf" style="color:#f0b44d;font-weight:700;">Download PDF</a></div><div class="card"><h2>Backup</h2><a href="/backup/download" style="color:#f0b44d;font-weight:700;">Download Backup ZIP</a></div></div>''')


@app.get("/exports/project.csv")
def export_project_csv():
    pid=project_id(); c=db(); project=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); risks=c.execute("SELECT * FROM risks WHERE project_id=? ORDER BY score DESC",(pid,)).fetchall(); actions=c.execute("SELECT * FROM action_items WHERE project_id=? ORDER BY due",(pid,)).fetchall(); c.close(); out=io.StringIO(); w=csv.writer(out); w.writerow(["BuildCommand AI Project Export"]); w.writerow(["Project",project["number"] if project else "",project["name"] if project else ""]); w.writerow([]); w.writerow(["RISKS"]); w.writerow(["Score","Band","Explanation"]); [w.writerow([r["score"],r["band"],r["explanation"]]) for r in risks]; w.writerow([]); w.writerow(["ACTIONS"]); w.writerow(["Title","Owner","Priority","Due","Status","Notes"]); [w.writerow([r["title"],r["owner"],r["priority"],r["due"],r["status"],r["notes"]]) for r in actions]; return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":'attachment; filename="buildcommand_project.csv"'})


@app.get("/exports/project.json")
def export_project_json():
    pid=project_id(); c=db(); project=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); tables=["activities","risks","make_ready","action_items","project_issues","punch_items","inspections_tracker","submittals","safety_items","change_events","meeting_notes","procurement","production","daily_reports","subs","subcontractor_updates"]; payload={"project":dict(project) if project else None}; [payload.__setitem__(t,[dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE project_id=?",(pid,)).fetchall()]) for t in tables]; c.close(); return Response(json.dumps(payload,indent=2,default=str),media_type="application/json",headers={"Content-Disposition":'attachment; filename="buildcommand_project.json"'})


@app.get("/exports/daily-report.pdf")
def export_daily_report_pdf():
    pid=project_id(); c=db(); project=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone(); report=c.execute("SELECT * FROM daily_reports WHERE project_id=? ORDER BY report_date DESC,id DESC LIMIT 1",(pid,)).fetchone(); c.close()
    if not report: return HTMLResponse("No daily report is available to export.",status_code=404)
    if canvas is None: return HTMLResponse("PDF support is not installed. Ensure reportlab is in requirements.txt.",status_code=503)
    buffer=io.BytesIO(); pdf=canvas.Canvas(buffer); y=742; pdf.setFont("Helvetica-Bold",16); pdf.drawString(45,y,"BuildCommand AI - Daily Superintendent Report"); y-=24; pdf.setFont("Helvetica",10); pdf.drawString(45,y,f'{project["number"]} - {project["name"]}'); y-=18; pdf.drawString(45,y,f'Date: {report["report_date"]}   Manpower: {report["manpower"] or 0}'); y-=26
    for label,value in [("Weather",report["weather"]),("Work Completed",report["work_completed"]),("Delays / Constraints",report["delays"]),("Deliveries",report["deliveries"]),("Inspections",report["inspections"]),("Safety",report["safety"]),("Tomorrow's Plan",report["tomorrow_plan"])]:
        pdf.setFont("Helvetica-Bold",10); pdf.drawString(45,y,label); y-=14; pdf.setFont("Helvetica",9); words=str(value or "—").split(); line=""
        for word in words:
            test=(line+" "+word).strip()
            if pdf.stringWidth(test,"Helvetica",9)>510: pdf.drawString(55,y,line); y-=12; line=word
            else: line=test
            if y<60: pdf.showPage(); y=742
        if line: pdf.drawString(55,y,line); y-=12
        y-=10
    pdf.setFont("Helvetica",8); pdf.drawString(45,30,"Built by Wilson LaHood · © 2026 Wilson LaHood"); pdf.save(); buffer.seek(0); return Response(buffer.getvalue(),media_type="application/pdf",headers={"Content-Disposition":'attachment; filename="buildcommand_daily_report.pdf"'})


@app.get("/backup/download")
def backup_download():
    stamp=datetime.utcnow().strftime("%Y%m%d_%H%M%S"); tmp=f"/tmp/bc_{secrets.token_hex(5)}"; os.makedirs(tmp,exist_ok=True); dbcopy=os.path.join(tmp,"buildcommand.db"); s=sqlite3.connect(DB); t=sqlite3.connect(dbcopy); s.backup(t); t.close(); s.close(); zpath=f"/tmp/buildcommand_backup_{stamp}.zip"
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED) as z:
        z.write(dbcopy,arcname="buildcommand.db"); c=db(); files=c.execute("SELECT * FROM attachments WHERE company_id=?",(current_company_id(),)).fetchall(); c.close()
        for r in files:
            p=os.path.join(UPLOAD_DIR,r["stored_name"])
            if os.path.isfile(p): z.write(p,arcname=f'uploads/{r["stored_name"]}')
    return FileResponse(zpath,media_type="application/zip",filename=f"buildcommand_backup_{stamp}.zip")


@app.get("/system-check",response_class=HTMLResponse)
def system_check():
    checks=[]
    try:
        c=db(); tables=["projects","activities","subs","subcontractor_updates","daily_reports","risks","make_ready","action_items","project_issues","punch_items","inspections_tracker","submittals","safety_items","change_events","meeting_notes","procurement","attachments","users","companies"]
        for t in tables: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        c.close(); checks.append(("READY","Database schema","Core tables respond successfully."))
    except Exception as exc: checks.append(("CRITICAL","Database schema",str(exc)))
    checks.append(("READY" if os.environ.get("OPENAI_API_KEY") else "WATCH","OpenAI connection","OPENAI_API_KEY is configured." if os.environ.get("OPENAI_API_KEY") else "AI key is not configured.")); checks.append(("READY" if os.path.isdir(UPLOAD_DIR) and os.access(UPLOAD_DIR,os.W_OK) else "CRITICAL","Upload storage",f"Upload path: {UPLOAD_DIR}")); checks.append(("READY" if DATABASE_KIND=="postgres" else "WATCH","Database engine",f"Current engine: {DATABASE_KIND}.")); checks.append(("READY" if os.environ.get("UPLOAD_DIR") else "WATCH","Persistent files","UPLOAD_DIR is explicitly configured." if os.environ.get("UPLOAD_DIR") else "Set UPLOAD_DIR to a Render persistent-disk path for durable files.")); html="".join(f'<div class="card"><span class="badge {b}">{b}</span><h3>{esc(t)}</h3><p>{esc(d)}</p></div>' for b,t,d in checks); return shell("System Check",f'<div class="hero"><div class="eyebrow">Stability</div><h1>BuildCommand System Check</h1></div><div class="grid2">{html}</div>')


@app.get("/beta-feedback",response_class=HTMLResponse)
def beta_feedback_page():
    c=db(); rows=c.execute("SELECT b.*,u.display_name FROM beta_feedback b LEFT JOIN users u ON u.id=b.user_id WHERE b.company_id=? ORDER BY b.id DESC LIMIT 20",(current_company_id(),)).fetchall(); c.close(); recent="".join(f'<div class="action"><b>{esc(r["category"])}</b> · {r["rating"]}/5<div>{esc(r["feedback"])}</div><div class="small">{esc(r["display_name"] or "")} · {esc(r["created"])}</div></div>' for r in rows) or '<div class="muted">No beta feedback yet.</div>'; return shell("Beta Feedback",f'''<div class="hero"><div class="eyebrow">Beta Program</div><h1>What should BuildCommand improve next?</h1></div><div class="grid2"><div class="card"><form method="post" action="/beta-feedback"><label>Rating</label><select name="rating"><option value="5">5 - Excellent</option><option value="4">4</option><option value="3">3</option><option value="2">2</option><option value="1">1 - Poor</option></select><label>Category</label><select name="category"><option>FIELD_WORKFLOW</option><option>AI</option><option>SCHEDULE</option><option>MOBILE</option><option>REPORTING</option><option>BUG</option><option>OTHER</option></select><label>Feedback</label><textarea name="feedback" required></textarea><button type="submit">Save Feedback</button></form></div><div class="card"><h2>Recent Feedback</h2>{recent}</div></div>''')


@app.post("/beta-feedback")
def beta_feedback_save(rating:int=Form(...),category:str=Form(...),feedback:str=Form(...)):
    c=db(); c.execute("INSERT INTO beta_feedback(company_id,user_id,project_id,rating,category,feedback,created) VALUES(?,?,?,?,?,?,?)",(current_company_id(),current_user_id(),project_id(),max(1,min(5,rating)),category,feedback.strip(),datetime.utcnow().isoformat())); c.commit(); c.close(); return RedirectResponse("/beta-feedback",status_code=303)


@app.post("/notifications/email-digest")
def notifications_email_digest():
    import smtplib
    from email.message import EmailMessage
    user=current_user(); pid=project_id(); refresh_notifications(pid)
    host=os.environ.get("SMTP_HOST"); sender=os.environ.get("SMTP_FROM"); smtp_user=os.environ.get("SMTP_USER"); smtp_password=os.environ.get("SMTP_PASSWORD"); port=int(os.environ.get("SMTP_PORT","587"))
    if not host or not sender:
        return HTMLResponse(shell("Notifications",'<div class="card"><h2>Email alerts need setup</h2><p>Add SMTP_HOST and SMTP_FROM in Render. If your mail provider requires login, also add SMTP_USER and SMTP_PASSWORD.</p><p><a href="/notifications">Back to Notifications</a></p></div>'),status_code=503)
    c=db(); rows=c.execute("SELECT * FROM notifications WHERE company_id=? AND project_id=? ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'WATCH' THEN 3 ELSE 4 END,id DESC LIMIT 30",(current_company_id(),pid)).fetchall(); c.close()
    lines=[f'{r["severity"]}: {r["title"]} — {r["detail"]}' for r in rows]
    msg=EmailMessage(); msg["Subject"]="BuildCommand AI Alert Digest"; msg["From"]=sender; msg["To"]=user["email"]; msg.set_content("\n".join(lines) or "No active BuildCommand alerts.")
    with smtplib.SMTP(host,port,timeout=15) as smtp:
        smtp.starttls()
        if smtp_user: smtp.login(smtp_user,smtp_password or "")
        smtp.send_message(msg)
    return RedirectResponse("/notifications",status_code=303)


def ensure_today_morning_brief(pid):
    if not pid or not company_setting("auto_ai_brief", 1):
        return
    cid = current_company_id()
    c = db()
    existing = c.execute(
        "SELECT id FROM morning_briefs WHERE company_id=? AND project_id=? AND brief_date=? LIMIT 1",
        (cid, pid, date.today().isoformat()),
    ).fetchone()
    c.close()
    if existing or not os.environ.get("OPENAI_API_KEY") or OpenAI is None:
        return
    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        response = client.responses.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6"),
            instructions=(
                "You are BuildCommand AI. Create a concise superintendent morning brief grounded only in "
                "supplied project data. Use HANDLE FIRST, SCHEDULE THREATS, WHO OWES WHAT, TODAY'S "
                "VERIFICATIONS, COST / CHANGE EXPOSURE, SAFETY / QUALITY."
            ),
            input=build_project_context(pid),
        )
        brief = response.output_text
    except Exception:
        return
    c = db()
    c.execute(
        "INSERT INTO morning_briefs(company_id,project_id,brief_date,brief_text,created) VALUES(?,?,?,?,?)",
        (cid, pid, date.today().isoformat(), brief, datetime.utcnow().isoformat()),
    )
    c.commit()
    c.close()


@app.get("/setup", response_class=HTMLResponse)
def setup_wizard():
    cid = current_company_id()
    ensure_company_settings(cid)
    u = current_user()
    c = db()
    pc = c.execute("SELECT COUNT(*) n FROM projects WHERE company_id=?", (cid,)).fetchone()["n"]
    tc = c.execute("SELECT COUNT(*) n FROM users WHERE company_id=?", (cid,)).fetchone()["n"]
    c.close()
    body = f"""
    <div class="hero"><div class="eyebrow">Customer Setup</div><h1>Finish your BuildCommand workspace.</h1></div>
    <div class="grid2">
      <div class="card"><h2>1. Company</h2><p>{esc(u['company_name'])}</p><a href="/company-settings">Edit Company →</a></div>
      <div class="card"><h2>2. Project</h2><p>{pc} project(s)</p><a href="/projects/new">Add Project →</a></div>
      <div class="card"><h2>3. Team</h2><p>{tc} user(s)</p><a href="/invitations">Invite Team →</a></div>
      <div class="card"><h2>4. Field Setup</h2><a href="/subcontractors">Add Subcontractors →</a><br><a href="/activities/new">Add Schedule Activity →</a></div>
    </div>
    <div class="card"><form method="post" action="/setup/complete"><button type="submit">Finish Setup</button></form></div>
    """
    return shell("Setup", body)


@app.post("/setup/complete")
def setup_complete():
    cid = current_company_id()
    ensure_company_settings(cid)
    c = db()
    c.execute("UPDATE company_settings SET onboarding_complete=1 WHERE company_id=?", (cid,))
    c.commit()
    c.close()
    return RedirectResponse("/", 303)


@app.get("/invitations", response_class=HTMLResponse)
def invitations_page():
    if not require_role("ADMIN"):
        return HTMLResponse("Not authorized.", 403)
    c = db()
    rows = c.execute(
        "SELECT * FROM invitations WHERE company_id=? ORDER BY id DESC LIMIT 30",
        (current_company_id(),),
    ).fetchall()
    c.close()
    hist = "".join(
        f"<div class='action'><b>{esc(r['email'])}</b> · {esc(r['role'])}<div class='small'>{'Accepted' if r['accepted'] else 'Pending'} · Expires {esc(r['expires'])}</div></div>"
        for r in rows
    ) or "<div class='muted'>No invitations yet.</div>"
    form = """
    <div class='card'><h2>Create Invitation</h2><form method='post' action='/invitations'>
    <label>Email</label><input type='email' name='email' required>
    <label>Role</label><select name='role'>
      <option value='READ_ONLY'>Read Only</option><option value='FIELD_USER'>Field User</option>
      <option value='SUPERINTENDENT'>Superintendent</option><option value='PROJECT_MANAGER'>Project Manager</option>
      <option value='ADMIN'>Admin</option>
    </select><button type='submit'>Create Invite</button></form></div>
    """
    return shell(
        "Invitations",
        f"<div class='hero'><div class='eyebrow'>Team Invitations</div><h1>Invite the project team.</h1></div><div class='grid2'>{form}<div class='card'><h2>Recent</h2>{hist}</div></div>",
    )


@app.post("/invitations")
def create_invitation(email: str = Form(...), role: str = Form("FIELD_USER")):
    if not require_role("ADMIN"):
        return HTMLResponse("Not authorized.", 403)
    valid = {"READ_ONLY", "FIELD_USER", "SUPERINTENDENT", "PROJECT_MANAGER", "ADMIN"}
    role = role if role in valid else "FIELD_USER"
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires = (datetime.utcnow() + timedelta(days=7)).isoformat()
    c = db()
    c.execute(
        "INSERT INTO invitations(company_id,email,role,token_hash,expires,accepted,created_by,created) VALUES(?,?,?,?,?,?,?,?)",
        (current_company_id(), email.strip().lower(), role, token_hash, expires, 0, current_user_id(), datetime.utcnow().isoformat()),
    )
    c.commit()
    c.close()
    link = f"/accept-invite?token={raw}"
    return HTMLResponse(
        shell(
            "Invitation Created",
            f"<div class='card'><h2>Invitation created</h2><p>Send this link to {esc(email)}. It expires in 7 days.</p><input value='{esc(link)}' readonly><p><a href='/invitations'>Back</a></p></div>",
        )
    )


@app.get("/accept-invite", response_class=HTMLResponse)
def accept_invite_get(token: str):
    th = hashlib.sha256(token.encode()).hexdigest()
    c = db()
    inv = c.execute(
        "SELECT i.*,c.name company_name FROM invitations i JOIN companies c ON c.id=i.company_id WHERE i.token_hash=? AND i.accepted=0 AND i.expires>?",
        (th, datetime.utcnow().isoformat()),
    ).fetchone()
    c.close()
    if not inv:
        return HTMLResponse("Invitation invalid or expired.", 400)
    return f"""
    <!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}</style></head>
    <body><main class='main'><div class='card'><h1>Join {esc(inv['company_name'])}</h1>
    <form method='post' action='/accept-invite'><input type='hidden' name='token' value='{esc(token)}'>
    <label>Name</label><input name='display_name' required><label>Password</label><input type='password' name='password' minlength='8' required>
    <button type='submit'>Create Account</button></form></div></main></body></html>
    """


@app.post("/accept-invite")
def accept_invite_post(token: str = Form(...), display_name: str = Form(...), password: str = Form(...)):
    if len(password) < 8:
        return HTMLResponse("Password must be at least 8 characters.", 400)
    th = hashlib.sha256(token.encode()).hexdigest()
    c = db()
    inv = c.execute(
        "SELECT * FROM invitations WHERE token_hash=? AND accepted=0 AND expires>?",
        (th, datetime.utcnow().isoformat()),
    ).fetchone()
    if not inv:
        c.close()
        return HTMLResponse("Invitation invalid or expired.", 400)
    if c.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (inv["email"],)).fetchone():
        c.close()
        return HTMLResponse("Email already registered.", 400)
    c.execute(
        "INSERT INTO users(company_id,email,display_name,password_hash,role,created) VALUES(?,?,?,?,?,?)",
        (inv["company_id"], inv["email"], display_name.strip(), hash_password(password), inv["role"], date.today().isoformat()),
    )
    uid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("UPDATE invitations SET accepted=1 WHERE id=?", (inv["id"],))
    fp = c.execute("SELECT id FROM projects WHERE company_id=? ORDER BY id LIMIT 1", (inv["company_id"],)).fetchone()
    if fp:
        c.execute(
            "INSERT INTO user_state(user_id,selected_project_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_project_id=excluded.selected_project_id",
            (uid, fp["id"]),
        )
    c.commit()
    c.close()
    raw = create_session(uid)
    resp = RedirectResponse("/", 303)
    resp.set_cookie("bc_session", raw, httponly=True, secure=os.environ.get("COOKIE_SECURE", "1") == "1", samesite="lax", max_age=2592000)
    return resp


@app.get("/production-settings", response_class=HTMLResponse)
def production_settings_page():
    if not require_role("ADMIN"):
        return HTMLResponse("Not authorized.", 403)
    cid = current_company_id()
    ensure_company_settings(cid)
    c = db()
    s = c.execute("SELECT * FROM company_settings WHERE company_id=?", (cid,)).fetchone()
    c.close()
    body = f"""
    <div class='hero'><div class='eyebrow'>Production Controls</div><h1>Automation & beta settings</h1></div>
    <div class='card'><form method='post' action='/production-settings'>
    <label>Automatic AI Morning Brief</label><select name='auto_ai_brief'>
      <option value='1' {'selected' if s['auto_ai_brief'] else ''}>Enabled</option><option value='0' {'selected' if not s['auto_ai_brief'] else ''}>Disabled</option></select>
    <label>Email Alerts</label><select name='email_alerts'>
      <option value='1' {'selected' if s['email_alerts'] else ''}>Enabled</option><option value='0' {'selected' if not s['email_alerts'] else ''}>Disabled</option></select>
    <label>Beta Mode</label><select name='beta_mode'>
      <option value='1' {'selected' if s['beta_mode'] else ''}>Enabled</option><option value='0' {'selected' if not s['beta_mode'] else ''}>Disabled</option></select>
    <button type='submit'>Save Settings</button></form></div>
    """
    return shell("Production Settings", body)


@app.post("/production-settings")
def production_settings_save(auto_ai_brief: int = Form(1), email_alerts: int = Form(0), beta_mode: int = Form(1)):
    if not require_role("ADMIN"):
        return HTMLResponse("Not authorized.", 403)
    cid = current_company_id()
    ensure_company_settings(cid)
    c = db()
    c.execute(
        "UPDATE company_settings SET auto_ai_brief=?,email_alerts=?,beta_mode=? WHERE company_id=?",
        (1 if auto_ai_brief else 0, 1 if email_alerts else 0, 1 if beta_mode else 0, cid),
    )
    c.commit()
    c.close()
    return RedirectResponse("/production-settings", 303)


@app.get("/beta-checklist", response_class=HTMLResponse)
def beta_checklist():
    pid = project_id()
    c = db()
    checks = {
        "Project created": bool(pid),
        "Schedule activity entered": c.execute("SELECT COUNT(*) n FROM activities WHERE project_id=?", (pid,)).fetchone()["n"] > 0 if pid else False,
        "Subcontractor entered": c.execute("SELECT COUNT(*) n FROM subs WHERE project_id=?", (pid,)).fetchone()["n"] > 0 if pid else False,
        "Daily report submitted": c.execute("SELECT COUNT(*) n FROM daily_reports WHERE project_id=?", (pid,)).fetchone()["n"] > 0 if pid else False,
        "Risk entered": c.execute("SELECT COUNT(*) n FROM risks WHERE project_id=?", (pid,)).fetchone()["n"] > 0 if pid else False,
        "AI configured": bool(os.environ.get("OPENAI_API_KEY")),
        "Persistent upload path configured": bool(os.environ.get("UPLOAD_DIR")),
        "PostgreSQL connected": DATABASE_KIND == "postgres",
    }
    c.close()
    complete = sum(1 for v in checks.values() if v)
    html = "".join(
        f"<div class='action'><span class='badge {'READY' if ok else 'WATCH'}'>{'DONE' if ok else 'TODO'}</span> {esc(name)}</div>"
        for name, ok in checks.items()
    )
    return shell(
        "Beta Checklist",
        f"<div class='hero'><div class='eyebrow'>Beta Readiness</div><h1>{complete}/{len(checks)} launch checks complete</h1></div><div class='card'>{html}</div><div class='card'><a href='/beta-feedback'>Record Beta Feedback →</a></div>",
    )

# =========================
# BuildCommand AI v28 Intelligence Layer
# =========================

def parse_iso_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def activity_delay_signal(activity, today=None):
    today = today or date.today()
    start = parse_iso_date(activity["start"])
    finish = parse_iso_date(activity["finish"])
    pct = float(activity["pct"] or 0)
    status = activity["status"] or "NOT_STARTED"
    score, reasons = 0, []
    if finish and finish < today and pct < 100:
        score += 55; reasons.append("finish date has passed")
    if start and start <= today and pct <= 0 and status != "COMPLETE":
        score += 25; reasons.append("scheduled start has passed with 0% complete")
    if start and finish and start <= today <= finish:
        total = max(1, (finish-start).days+1)
        elapsed = max(0, (today-start).days+1)
        expected = min(100, elapsed/total*100)
        if pct + 15 < expected:
            variance = expected-pct
            score += min(35, 10 + variance*.6)
            reasons.append(f"progress trails time-elapsed plan by about {variance:.0f} points")
    score = min(100, int(round(score)))
    band = "CRITICAL" if score >= 70 else "HIGH" if score >= 45 else "WATCH" if score >= 20 else "READY"
    return score, band, reasons


def procurement_warning_signal(row):
    required, promised = parse_iso_date(row["required_on_site"]), parse_iso_date(row["promised_date"])
    status, today = (row["status"] or "NOT_RELEASED").upper(), date.today()
    score, reasons = 0, []
    if status == "DELIVERED": return 0, "READY", ["delivered"]
    if not required: score += 20; reasons.append("required-on-site date missing")
    if not promised: score += 45; reasons.append("promised date missing")
    elif required and promised > required:
        late=(promised-required).days; score += min(80,45+late*4); reasons.append(f"promised date is {late} day(s) after required date")
    if required:
        days=(required-today).days
        if days < 0: score += 30; reasons.append("required-on-site date has passed")
        elif days <= 7 and status not in ["SHIPPED","DELIVERED"]: score += 25; reasons.append("needed within 7 days and not shipped")
        elif days <= 21 and status in ["NOT_RELEASED","RELEASED"]: score += 12; reasons.append("needed within 3 weeks and not yet in fabrication/shipping")
    score=min(100,score)
    band="CRITICAL" if score>=70 else "HIGH" if score>=45 else "WATCH" if score>=20 else "READY"
    return score,band,reasons or ["no procurement warning detected"]


def project_health_snapshot(pid):
    c=db()
    activities=c.execute("SELECT * FROM activities WHERE project_id=?",(pid,)).fetchall()
    risks=c.execute("SELECT * FROM risks WHERE project_id=?",(pid,)).fetchall()
    actions=c.execute("SELECT * FROM action_items WHERE project_id=? AND status='OPEN'",(pid,)).fetchall()
    issues=c.execute("SELECT * FROM project_issues WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchall()
    procurement=c.execute("SELECT * FROM procurement WHERE project_id=? AND status!='DELIVERED'",(pid,)).fetchall()
    readiness=c.execute("SELECT * FROM activity_readiness WHERE project_id=?",(pid,)).fetchall()
    safety=c.execute("SELECT * FROM safety_items WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchall()
    inspections=c.execute("SELECT * FROM inspections_tracker WHERE project_id=? AND result!='PASSED'",(pid,)).fetchall(); c.close()
    today=date.today()
    overdue_actions=sum(1 for x in actions if parse_iso_date(x["due"]) and parse_iso_date(x["due"])<today)
    overdue_issues=sum(1 for x in issues if parse_iso_date(x["due"]) and parse_iso_date(x["due"])<today)
    delay_risk=sum(activity_delay_signal(a)[0] for a in activities)/len(activities) if activities else 0
    risk_exp=sum(float(r["score"] or 0) for r in risks)/len(risks) if risks else 0
    proc_exp=sum(procurement_warning_signal(p)[0] for p in procurement)/len(procurement) if procurement else 0
    ready_pct=sum(readiness_result(r)[0] for r in readiness)/len(readiness) if readiness else (50 if activities else 100)
    safety_penalty=sum(25 if s["severity"]=="CRITICAL" else 15 if s["severity"]=="HIGH" else 7 for s in safety)
    inspection_penalty=sum(18 if i["result"]=="FAILED" else 7 for i in inspections)
    scores={"schedule":max(0,100-delay_risk),"readiness":max(0,min(100,ready_pct)),"procurement":max(0,100-proc_exp),"risk":max(0,100-risk_exp),"field":max(0,100-min(45,overdue_actions*6+overdue_issues*7)-min(35,safety_penalty+inspection_penalty))}
    overall=round(scores["schedule"]*.28+scores["readiness"]*.22+scores["procurement"]*.18+scores["risk"]*.17+scores["field"]*.15)
    return {"overall":overall,**{k:round(v) for k,v in scores.items()},"overdue_actions":overdue_actions,"overdue_issues":overdue_issues}


@app.get("/project-health", response_class=HTMLResponse)
def project_health_page():
    h=project_health_snapshot(project_id())
    badge,label=("READY","Healthy") if h["overall"]>=85 else ("WATCH","Watch") if h["overall"]>=70 else ("HIGH","At Risk") if h["overall"]>=50 else ("CRITICAL","Critical")
    return shell("Project Health",f'<div class="hero"><div class="eyebrow">Executive Project Health</div><h1>{h["overall"]}/100 <span class="badge {badge}">{label}</span></h1></div><div class="grid4"><div class="card"><div class="label">Schedule</div><div class="kpi">{h["schedule"]}</div></div><div class="card"><div class="label">Readiness</div><div class="kpi">{h["readiness"]}</div></div><div class="card"><div class="label">Procurement</div><div class="kpi">{h["procurement"]}</div></div><div class="card"><div class="label">Risk</div><div class="kpi">{h["risk"]}</div></div></div><div class="card"><h2>Field Execution {h["field"]}</h2><p>Overdue actions: {h["overdue_actions"]} · Overdue RFIs/issues: {h["overdue_issues"]}</p></div>')


@app.get("/lookahead-intelligence", response_class=HTMLResponse)
def lookahead_intelligence_page():
    pid=project_id(); today=date.today(); horizon=today+timedelta(days=21); c=db()
    activities=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); readiness_rows=c.execute("SELECT * FROM activity_readiness WHERE project_id=?",(pid,)).fetchall(); procurement=c.execute("SELECT * FROM procurement WHERE project_id=? AND status!='DELIVERED'",(pid,)).fetchall(); inspections=c.execute("SELECT * FROM inspections_tracker WHERE project_id=? AND result!='PASSED'",(pid,)).fetchall(); issues=c.execute("SELECT * FROM project_issues WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchall(); c.close()
    rm={r["activity_id"]:r for r in readiness_rows}; pm={}; im={}; qm={}
    for p in procurement: pm.setdefault(p["activity_id"],[]).append(p)
    for i in inspections: im.setdefault(i["activity_id"],[]).append(i)
    for q in issues: qm.setdefault(q["activity_id"],[]).append(q)
    cards=''
    for a in activities:
        start,finish=parse_iso_date(a["start"]),parse_iso_date(a["finish"])
        if not start or (finish and finish<today) or start>horizon: continue
        rp,rs=(0,"NOT REVIEWED")
        if a["id"] in rm: rp,rs,_=readiness_result(rm[a["id"]])
        blockers=[]
        if rs in ["NOT READY","AT RISK","NOT REVIEWED"]: blockers.append(f"Readiness {rp}%")
        for p in pm.get(a["id"],[]):
            _,b,_=procurement_warning_signal(p)
            if b!="READY": blockers.append(f"Procurement: {p['item']} ({b})")
        for i in im.get(a["id"],[]): blockers.append(f"Inspection: {i['inspection_type']} ({i['result']})")
        for q in qm.get(a["id"],[]): blockers.append(f"{q['issue_type']}: {q['title']}")
        _,dband,_=activity_delay_signal(a)
        if dband!="READY": blockers.append(f"Schedule: {dband}")
        days=(start-today).days; timing="STARTED / DUE NOW" if days<=0 else f"Starts in {days} day(s)"; badge="READY" if not blockers else "CRITICAL" if any("CRITICAL" in b for b in blockers) else "WATCH"
        bh=''.join(f'<div class="small">• {esc(b)}</div>' for b in blockers) or '<div class="small">No major blocker detected.</div>'
        cards+=f'<div class="card"><span class="badge {badge}">{timing}</span><h3>{esc(a["external_id"])} - {esc(a["name"])}</h3><p>Readiness: {rp}%</p>{bh}</div>'
    return shell("3-Week Lookahead",f'<div class="hero"><div class="eyebrow">3-Week Lookahead Intelligence</div><h1>Upcoming work + blockers</h1><div class="muted">{today} through {horizon}</div></div><div class="grid2">{cards or "<div class=card>No activities in the next 21 days.</div>"}</div>')


@app.get("/sub-scorecards", response_class=HTMLResponse)
def subcontractor_scorecards_page():
    pid=project_id(); c=db(); subs=c.execute("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(pid,)).fetchall(); updates=c.execute("SELECT * FROM subcontractor_updates WHERE project_id=? ORDER BY update_date DESC,id DESC",(pid,)).fetchall(); punch=c.execute("SELECT * FROM punch_items WHERE project_id=? AND status!='VERIFIED'",(pid,)).fetchall(); safety=c.execute("SELECT * FROM safety_items WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchall(); actions=c.execute("SELECT * FROM action_items WHERE project_id=? AND status='OPEN'",(pid,)).fetchall(); c.close(); today=date.today(); cards=''
    for s in subs:
        su=[u for u in updates if u["sub_id"]==s["id"]]; latest=su[0] if su else None; score=100; reasons=[]
        if not latest: score-=20; reasons.append("no recent subcontractor update")
        else:
            st=(latest["status"] or "").upper(); score-=35 if st=="CRITICAL" else 22 if st=="HIGH" else 10 if st=="WATCH" else 0
            if st in ["CRITICAL","HIGH","WATCH"]: reasons.append(f"latest status: {st}")
            if (latest["manpower"] or 0)<=0: score-=12; reasons.append("latest manpower is zero")
        name=(s["name"] or "").lower(); trade=(s["trade"] or "").lower(); op=sum(1 for p in punch if (name and name in (p["owner"] or "").lower()) or (trade and trade in (p["trade"] or "").lower())); osf=sum(1 for x in safety if name and name in (x["responsible_party"] or "").lower()); oa=sum(1 for a in actions if name and name in (a["owner"] or "").lower() and parse_iso_date(a["due"]) and parse_iso_date(a["due"])<today)
        if op: score-=min(20,op*4); reasons.append(f"{op} open punch item(s)")
        if osf: score-=min(25,osf*8); reasons.append(f"{osf} open safety item(s)")
        if oa: score-=min(20,oa*6); reasons.append(f"{oa} overdue action(s)")
        score=max(0,score); grade="A" if score>=90 else "B" if score>=80 else "C" if score>=65 else "D" if score>=50 else "F"; badge="READY" if score>=80 else "WATCH" if score>=65 else "HIGH" if score>=50 else "CRITICAL"; rh=''.join(f'<div class="small">• {esc(r)}</div>' for r in reasons) or '<div class="small">No major negative signal detected.</div>'; cards+=f'<div class="card"><span class="badge {badge}">Grade {grade}</span><h3>{esc(s["name"])}</h3><div class="muted">{esc(s["trade"])}</div><div class="kpi">{score}</div>{rh}</div>'
    return shell("Subcontractor Scorecards",f'<div class="hero"><div class="eyebrow">Trade Partner Intelligence</div><h1>Subcontractor Scorecards</h1></div><div class="grid3">{cards or "<div class=card>Add subcontractors to generate scorecards.</div>"}</div>')


@app.get("/rfi-impact", response_class=HTMLResponse)
def rfi_impact_page():
    pid=project_id(); c=db(); issues=c.execute("""SELECT i.*,a.external_id,a.name activity,a.start activity_start FROM project_issues i LEFT JOIN activities a ON a.id=i.activity_id WHERE i.project_id=? AND i.status!='CLOSED' ORDER BY i.due,i.id""",(pid,)).fetchall(); activities=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); c.close(); cards=''; today=date.today()
    for i in issues:
        ls=parse_iso_date(i["activity_start"]) if i["activity_start"] else None; impacted=[a for a in activities if ls and parse_iso_date(a["start"]) and parse_iso_date(a["start"])>=ls and a["id"]!=i["activity_id"]]; due=parse_iso_date(i["due"]); score=20+(40 if i["priority"]=="CRITICAL" else 25 if i["priority"]=="HIGH" else 0)+(25 if due and due<today else 0)+(20 if ls and (ls-today).days<=14 else 0); score=min(100,score); badge="CRITICAL" if score>=70 else "HIGH" if score>=45 else "WATCH"; impact=', '.join(f'{a["external_id"]} {a["name"]}' for a in impacted[:4]) or 'No downstream activity inferred from date order.'; cards+=f'<div class="card"><span class="badge {badge}">Impact {score}</span><h3>{esc(i["title"])}</h3><p>Linked: {esc(i["external_id"] or "None")} {esc(i["activity"] or "")}</p><p>Possible downstream exposure: {esc(impact)}</p></div>'
    return shell("RFI Impact",f'<div class="hero"><div class="eyebrow">RFI Impact Intelligence</div><h1>Which questions can move the schedule?</h1></div><div class="grid2">{cards or "<div class=card>No open RFIs/issues.</div>"}</div>')


@app.get("/procurement-warning", response_class=HTMLResponse)
def procurement_warning_page():
    pid=project_id(); c=db(); rows=c.execute("""SELECT p.*,a.external_id,a.name activity FROM procurement p LEFT JOIN activities a ON a.id=p.activity_id WHERE p.project_id=? AND p.status!='DELIVERED' ORDER BY p.required_on_site,p.id""",(pid,)).fetchall(); c.close(); scored=sorted([(procurement_warning_signal(r)[0],procurement_warning_signal(r)[1],r,procurement_warning_signal(r)[2]) for r in rows],key=lambda x:x[0],reverse=True); cards=''
    for score,band,r,reasons in scored: cards+=f'<div class="card"><span class="badge {band}">{band} · {score}</span><h3>{esc(r["item"])}</h3><p>Required: {esc(r["required_on_site"] or "—")} · Promised: {esc(r["promised_date"] or "—")}</p>'+''.join(f'<div class="small">• {esc(x)}</div>' for x in reasons)+'</div>'
    return shell("Procurement Warning",f'<div class="hero"><div class="eyebrow">Procurement Early Warning</div><h1>Material threats ranked before installation.</h1></div><div class="grid2">{cards or "<div class=card>No open procurement items.</div>"}</div>')


def recovery_options_for_activity(activity,delay_score):
    if delay_score<20: return [("Protect Plan",0,0,"No major delay signal. Protect access, material, and inspection readiness.")]
    return [("Add targeted manpower",min(3.0,max(1.0,delay_score/30)),2500+delay_score*35,"Increase manpower only after constraints are cleared."),("Second shift / extended hours",min(5.0,max(1.5,delay_score/22)),4500+delay_score*55,"Use where supervision and site rules allow."),("Resequence downstream work",min(4.0,max(1.0,delay_score/25)),1500+delay_score*20,"Advance unaffected work while the constraint is removed.")]


@app.get("/ai-recovery", response_class=HTMLResponse)
def ai_recovery_page():
    pid=project_id(); c=db(); acts=c.execute("SELECT * FROM activities WHERE project_id=? AND status!='COMPLETE' ORDER BY start",(pid,)).fetchall(); c.close(); scored=sorted([(activity_delay_signal(a)[0],activity_delay_signal(a)[1],a) for a in acts],key=lambda x:x[0],reverse=True); cards=''
    for score,band,a in scored[:6]:
        opts=recovery_options_for_activity(a,score); oh=''.join(f'<div class="action"><b>{esc(n)}</b><div class="small">Estimated recovery: {d:.1f} day(s) · ROM cost: ${cst:,.0f}</div><div class="small">{esc(note)}</div></div>' for n,d,cst,note in opts); n,d,cst,_=opts[0]; cards+=f'<div class="card"><span class="badge {band}">Delay Signal {score}</span><h3>{esc(a["external_id"])} - {esc(a["name"])}</h3>{oh}<form method="post" action="/ai-recovery/save"><input type="hidden" name="activity_id" value="{a["id"]}"><input type="hidden" name="scenario" value="{esc(n)}"><input type="hidden" name="days_recovered" value="{d}"><input type="hidden" name="est_cost" value="{cst}"><button type="submit">Save Top Recovery Option</button></form></div>'
    return shell("AI Recovery Planner",f'<div class="hero"><div class="eyebrow">Recovery Intelligence</div><h1>What can we do when work drifts?</h1><div class="muted">ROM planning aid—field validation required.</div></div><div class="grid2">{cards}</div>')


@app.post("/ai-recovery/save")
def save_ai_recovery(activity_id:int=Form(...),scenario:str=Form(...),days_recovered:float=Form(0),est_cost:float=Form(0)):
    pid=project_id(); c=db(); c.execute("INSERT INTO recovery(project_id,activity_id,scenario,days_recovered,est_cost,status) VALUES(?,?,?,?,?,'PROPOSED')",(pid,activity_id,scenario,days_recovered,est_cost)); c.commit(); c.close(); return RedirectResponse('/ai-recovery',status_code=303)


@app.get("/quick-entry", response_class=HTMLResponse)
def quick_entry_page():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM quick_entries WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 15",(current_company_id(),pid)).fetchall(); c.close(); recent=''.join(f'<div class="action"><b>{esc(r["entry_type"])}</b> → {esc(r["routed_to"])}<div>{esc(r["text"])}</div></div>' for r in rows) or '<div class="muted">No quick entries yet.</div>'; body=f'''<div class="hero"><div class="eyebrow">Superintendent Quick Entry</div><h1>Say it once. Route it where it belongs.</h1></div><div class="grid2"><div class="card"><form method="post" action="/quick-entry"><label>Route As</label><select name="entry_type"><option value="AUTO">Auto Detect</option><option value="ACTION">Action</option><option value="FIELD_UPDATE">Field Update</option><option value="RFI">RFI / Issue</option><option value="SAFETY">Safety Observation</option></select><label>Field Note</label><textarea id="quickText" name="text" required></textarea><button type="button" onclick="startBuildCommandVoice()">Speak</button><button type="submit">Route Entry</button></form></div><div class="card"><h2>Recent Quick Entries</h2>{recent}</div></div><script>function startBuildCommandVoice(){{const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){{alert('Voice recognition is not supported by this browser.');return;}}const rec=new SR();rec.lang='en-US';rec.onresult=function(e){{document.getElementById('quickText').value=e.results[0][0].transcript;}};rec.start();}}</script>'''; return shell("Quick Entry",body)


@app.post("/quick-entry")
def save_quick_entry(entry_type:str=Form("AUTO"),text:str=Form(...)):
    pid=project_id(); raw=text.strip(); low=raw.lower(); detected=entry_type
    if detected=="AUTO": detected="SAFETY" if any(k in low for k in ["unsafe","safety","guardrail","near miss","incident"]) else "RFI" if any(k in low for k in ["rfi","question","clarify","design","drawing conflict"]) else "ACTION" if any(k in low for k in ["need to","follow up","confirm","call","send","by friday","by tomorrow"]) else "FIELD_UPDATE"
    c=db(); routed=detected
    if detected=="ACTION": c.execute("INSERT INTO action_items(project_id,title,owner,priority,due,status,notes,created) VALUES(?,?,?,?,?,'OPEN',?,?)",(pid,raw[:140],"Superintendent","WATCH",date.today().isoformat(),raw,date.today().isoformat())); routed="Action Center"
    elif detected=="RFI": c.execute("INSERT INTO project_issues(project_id,activity_id,issue_type,title,owner,due,priority,status,description,response,created) VALUES(?,NULL,'FIELD_ISSUE',?,'',?,'WATCH','OPEN',?,'',?)",(pid,raw[:140],date.today().isoformat(),raw,date.today().isoformat())); routed="RFIs / Issues"
    elif detected=="SAFETY": c.execute("INSERT INTO safety_items(project_id,activity_id,event_date,item_type,title,location,responsible_party,severity,status,description,corrective_action,created) VALUES(?,NULL,?,'OBSERVATION',?,'','','WATCH','OPEN',?,'',?)",(pid,date.today().isoformat(),raw[:140],raw,date.today().isoformat())); routed="Safety"
    else: c.execute("INSERT INTO field_updates(project_id,activity_id,update_type,text,created) VALUES(?,NULL,'QUICK_ENTRY',?,?)",(pid,raw,date.today().isoformat())); routed="Field Updates"
    c.execute("INSERT INTO quick_entries(company_id,project_id,user_id,entry_type,text,routed_to,created) VALUES(?,?,?,?,?,?,?)",(current_company_id(),pid,current_user_id(),detected,raw,routed,datetime.utcnow().isoformat())); c.commit(); c.close(); return RedirectResponse('/quick-entry',status_code=303)


@app.get("/weekly-report", response_class=HTMLResponse)
def weekly_report_page():
    pid=project_id(); c=db(); row=c.execute("SELECT * FROM weekly_ai_reports WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1",(current_company_id(),pid)).fetchone(); c.close(); report=esc(row["report_text"]).replace("\n","<br>") if row else "No weekly AI report generated yet."; return shell("Weekly AI Report",f'<div class="hero"><div class="eyebrow">Owner / PM Reporting</div><h1>AI Weekly Project Report</h1></div><div class="card"><form method="post" action="/weekly-report/generate"><button type="submit">Generate Weekly Report</button></form></div><div class="card">{report}</div>')


@app.post("/weekly-report/generate")
def generate_weekly_report():
    pid=project_id(); context=build_project_context(pid); health=project_health_snapshot(pid); key=os.environ.get("OPENAI_API_KEY")
    if key:
        instructions=f'''You are BuildCommand AI producing a professional weekly construction project report for an owner/project manager. Use only supplied project facts. Project health score: {health["overall"]}/100. Use sections: EXECUTIVE SUMMARY, SCHEDULE, FIELD PROGRESS, RFIs / DECISIONS, PROCUREMENT, SAFETY / QUALITY, CHANGE EXPOSURE, TOP PRIORITIES NEXT WEEK. Do not invent facts, commitments, costs, or dates.'''
        try: report=OpenAI(api_key=key).responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions=instructions,input=context).output_text
        except Exception as exc: report=f"AI weekly report failed: {exc}"
    else: report=f"Project Health: {health['overall']}/100\nOPENAI_API_KEY is not configured."
    c=db(); c.execute("INSERT INTO weekly_ai_reports(company_id,project_id,week_ending,report_text,created) VALUES(?,?,?,?,?)",(current_company_id(),pid,date.today().isoformat(),report,datetime.utcnow().isoformat())); c.commit(); c.close(); return RedirectResponse('/weekly-report',status_code=303)


@app.get("/ai-command", response_class=HTMLResponse)
def ai_command_page():
    pid=project_id(); h=project_health_snapshot(pid); c=db(); acts=c.execute("SELECT * FROM activities WHERE project_id=? AND status!='COMPLETE'",(pid,)).fetchall(); actions=c.execute("SELECT * FROM action_items WHERE project_id=? AND status='OPEN'",(pid,)).fetchall(); procs=c.execute("SELECT * FROM procurement WHERE project_id=? AND status!='DELIVERED'",(pid,)).fetchall(); issues=c.execute("SELECT * FROM project_issues WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchall(); c.close(); sig=[]; today=date.today()
    for a in acts:
        s,b,r=activity_delay_signal(a)
        if b!="READY": sig.append((s,b,"Schedule",f'{a["external_id"]} {a["name"]}',"; ".join(r)))
    for p in procs:
        s,b,r=procurement_warning_signal(p)
        if b!="READY": sig.append((s,b,"Procurement",p["item"],"; ".join(r)))
    for a in actions:
        d=parse_iso_date(a["due"])
        if d and d<today: sig.append((65,a["priority"] or "HIGH","Action",a["title"],f'Overdue · Owner {a["owner"] or "Unassigned"}'))
    for i in issues:
        d=parse_iso_date(i["due"])
        if d and d<today: sig.append((70,i["priority"] or "HIGH",i["issue_type"],i["title"],"Response/decision overdue"))
    sig.sort(key=lambda x:x[0],reverse=True); sh=''.join(f'<div class="action"><span class="badge {b if b in ["CRITICAL","HIGH","WATCH","READY","LOW"] else "HIGH"}">{esc(cat)}</span> <b>{esc(t)}</b><div class="small">{esc(d)}</div></div>' for _,b,cat,t,d in sig[:12]) or '<div class="muted">No major automated warning signals detected.</div>'; return shell("AI Command",f'<div class="hero"><div class="eyebrow">AI Daily Command Center</div><h1>Project Health {h["overall"]}/100</h1></div><div class="grid2"><div class="card"><h2>Handle First</h2>{sh}</div><div class="card"><h2>Command Tools</h2><p><a href="/lookahead-intelligence">3-Week Lookahead</a></p><p><a href="/ai-recovery">Recovery Planner</a></p><p><a href="/quick-entry">Quick / Voice Entry</a></p><p><a href="/weekly-report">Weekly AI Report</a></p></div></div>')

def _attachment_text(row):
    if not row:
        return ""
    path=os.path.join(UPLOAD_DIR,row["stored_name"])
    ext=Path(row["original_name"] or "").suffix.lower()
    if not os.path.isfile(path):
        return ""
    try:
        if ext in {".txt",".csv",".md"}:
            return Path(path).read_text(errors="ignore")[:250000]
        if ext==".pdf" and PdfReader is not None:
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages[:250])[:350000]
        if ext in {".xlsx",".xlsm"} and openpyxl is not None:
            wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
            lines=[]
            for ws in wb.worksheets[:30]:
                lines.append("SHEET: "+ws.title)
                for vals in ws.iter_rows(values_only=True):
                    lines.append(" | ".join("" if v is None else str(v) for v in vals))
                    if len(lines)>25000:
                        break
            return "\n".join(lines)[:350000]
    except Exception:
        return ""
    return ""


def _blueprint_json(text_value):
    raw=(text_value or "").strip()
    if raw.startswith("```"):
        raw=re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw=re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        m=re.search(r"\{.*\}",raw,re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("Blueprint Brain returned a response that was not valid JSON.")


def _blueprint_scope_text(trade_data):
    lines=[]
    summary=(trade_data.get("summary") or "").strip()
    if summary:
        lines.append(summary)
        lines.append("")
    for i,item in enumerate(trade_data.get("items") or [],1):
        req=(item.get("requirement") or "").strip()
        if not req:
            continue
        src=[]
        if item.get("source_sheet"): src.append("Sheet "+str(item.get("source_sheet")))
        if item.get("source_detail"): src.append("Detail "+str(item.get("source_detail")))
        if item.get("source_spec"): src.append("Spec "+str(item.get("source_spec")))
        conf=(item.get("confidence") or "MEDIUM").upper()
        line=f"{i}. {req}"
        if src: line += " ["+" · ".join(src)+"]"
        line += f" — Confidence: {conf}"
        lines.append(line)
    return "\n".join(lines).strip()


def _blueprint_prompt(source_names):
    return f"""
You are the BuildCommand AI Blueprint Brain, a construction-document scope intelligence engine.

SOURCE FILES:
{source_names}

MISSION:
Read the supplied project plans/specifications as one coordinated construction package. Build a trade-by-trade scope of work by finding requirements wherever they appear, including requirements that belong to one trade but are written on another discipline's sheets.

NON-NEGOTIABLE RULES:
1. Use only information supported by the supplied files. Never invent drawing numbers, details, spec sections, quantities, equipment, code requirements, or responsibilities.
2. A requirement may be assigned to a trade even when it is found on another discipline's sheet. Mark those as CROSS_DISCIPLINE.
3. Every scope item must preserve the best available source: sheet number, detail, spec section, note, or filename. If a source cannot be identified, use an empty string and lower the confidence.
4. Distinguish explicit requirements from coordination inferences. Use item_type values SCOPE, CROSS_DISCIPLINE, COORDINATION, EXCLUSION_REVIEW, or RFI_CANDIDATE.
5. Flag contradictions, missing information, equipment with unclear power/plumbing/HVAC connections, unclear trade responsibility, and likely scope gaps as RFI candidates. Do not resolve ambiguity by guessing.
6. Include all detected trades, not only MEP. Typical trades can include demolition, earthwork, concrete, masonry, structural steel, rough carpentry, millwork, waterproofing, roofing, doors/frames/hardware, glazing, framing/drywall, ceilings, flooring, tile, painting, specialties, equipment, furnishings, fire sprinkler, plumbing, HVAC, controls, electrical, fire alarm, low voltage, security, site utilities, paving, landscaping, and others actually supported by the documents.
7. Phrase each requirement as an actionable subcontractor scope item suitable for GC review. Do not claim the generated scope replaces the signed subcontract, specifications, addenda, RFIs, or professional design review.
8. Confidence must be HIGH, MEDIUM, or LOW based on source clarity.
9. Trade ownership rules: resilient/rubber/vinyl base and resilient flooring belong to Flooring, not Framing/Drywall.
10. Demolition is system-owned: electrical demolition belongs to Electrical, plumbing demolition to Plumbing, HVAC/mechanical demolition to HVAC/Mechanical, fire sprinkler demolition to Fire Sprinkler, fire alarm demolition to Fire Alarm, and low-voltage/security demolition to the applicable systems trade. Only non-system/general demolition belongs to Demolition.

Return ONLY valid JSON using exactly this top-level structure:
{{
  "project_summary": "short summary grounded in the documents",
  "detected_disciplines": ["Architectural", "Electrical"],
  "trade_scopes": [
    {{
      "trade": "Electrical",
      "division": "26",
      "summary": "short trade scope overview",
      "items": [
        {{
          "requirement": "actionable scope requirement",
          "source_sheet": "E2.1",
          "source_detail": "3/E5.2",
          "source_spec": "26 05 00",
          "source_note": "General Note 7",
          "related_trade": "Mechanical",
          "confidence": "HIGH",
          "item_type": "CROSS_DISCIPLINE"
        }}
      ]
    }}
  ],
  "cross_discipline_flags": ["plain-language flag with source"],
  "rfi_candidates": ["plain-language ambiguity/gap with source"],
  "review_notes": ["items the GC should verify before issuing scopes"]
}}

IMPORTANT TRADE OWNERSHIP:
Classify by the actual work/material/system, not by which drawing sheet contains the note.
If one note contains work for multiple trades, split it into separate scope items and keep the same source sheet/detail/spec reference on each.
Wall/gypsum/drywall patching = Framing / Drywall. Paint/refinish after patch = Painting.
Doors, door frames, hollow-metal/wood doors and hardware = Doors / Frames / Hardware.
Door rough openings, jamb studs and header framing = Framing / Drywall.
Tile materials/installation = Tile. Metal studs/framing = Framing / Drywall even when shown in a tile detail.
MEP demolition stays with the responsible MEP trade. General non-MEP demolition = Demolition.
""".strip()




V33_TRADES = [
    "Demolition","Earthwork","Concrete","Masonry","Structural Steel","Rough Carpentry",
    "Millwork","Waterproofing","Roofing","Doors / Frames / Hardware","Storefront / Glazing",
    "Framing / Drywall","Ceilings","Flooring / Tile","Painting","Toilet / Bath Accessories",
    "Specialties","Equipment","Furnishings","Fire Sprinkler","Plumbing","HVAC / Mechanical",
    "Controls","Electrical","Fire Alarm","Low Voltage","Security","Site Utilities",
    "Paving","Landscaping","Unassigned"
]

def _v33_normalize_trade(name):
    n=(name or "").strip().lower()
    aliases={
        "mechanical":"HVAC / Mechanical","hvac":"HVAC / Mechanical",
        "hvac/mechanical":"HVAC / Mechanical","mechanical / hvac":"HVAC / Mechanical",
        "drywall":"Framing / Drywall","framing":"Framing / Drywall","framing/drywall":"Framing / Drywall",
        "doors/frames/hardware":"Doors / Frames / Hardware","door hardware":"Doors / Frames / Hardware",
        "storefront":"Storefront / Glazing","glazing":"Storefront / Glazing",
        "glazing/storefront":"Storefront / Glazing","glazing / storefront":"Storefront / Glazing",
        "storefront/glazing":"Storefront / Glazing","storefront / glazing":"Storefront / Glazing",
        "tile":"Flooring / Tile","flooring":"Flooring / Tile","flooring/tile":"Flooring / Tile",
        "tile/flooring":"Flooring / Tile","flooring / tile":"Flooring / Tile","tile / flooring":"Flooring / Tile",
        "bath accessories":"Toilet / Bath Accessories","bathroom accessories":"Toilet / Bath Accessories",
        "toilet accessories":"Toilet / Bath Accessories","toilet room accessories":"Toilet / Bath Accessories",
        "toilet / bath accessories":"Toilet / Bath Accessories",
        "fire sprinkler":"Fire Sprinkler","sprinkler":"Fire Sprinkler","fire alarm":"Fire Alarm",
        "low voltage":"Low Voltage","low-voltage":"Low Voltage","electrical":"Electrical",
        "plumbing":"Plumbing","demolition":"Demolition","demo":"Demolition"
    }
    return aliases.get(n, (name or "Unassigned").strip() or "Unassigned")

def _v33_trade_for_item(item, proposed_trade):
    """v34.1 field-tested Blueprint Brain trade classifier."""
    req=str(item.get("requirement") or "").strip().lower()
    text=" ".join(str(item.get(k) or "") for k in
                  ["requirement","source_note","source_spec","related_trade"]).lower()
    proposed=_v33_normalize_trade(proposed_trade)

    def has(*terms): return any(x in text for x in terms)

    # ------------------------------------------------------------
    # 1) ACTION FIRST: demolition ownership
    # Demolition must be the work action, not merely context such as
    # "patching resulting from demolition."
    explicit_demo=(
        req.startswith("demo ") or req.startswith("demolish ") or
        req.startswith("remove existing ") or req.startswith("remove and dispose ") or
        " existing to be removed" in req or " shall be demolished" in req or
        " demolition of " in req
    )

    # Access/coordination removal is NOT demolition scope when it is explicitly remove-and-replace.
    access_remove_replace=has("remove and replace ceiling tile","remove and replace ceiling tiles",
                              "remove and replace ceiling grid","remove/reinstall ceiling",
                              "remove and reinstall ceiling","temporarily remove ceiling")

    if explicit_demo and not access_remove_replace:
        # If the primary requirement is clearly general architectural demo and merely says
        # MEP removals remain by their trades, keep the item in Demolition.
        general_demo_objects=has("restroom partition","toilet partition","accessories","finish flooring",
                                 "wall finish","ceiling finish","metal stud partition","gypsum partition",
                                 "drywall partition","door and frame","casework","millwork","storefront",
                                 "ceramic tile","flooring","ceiling tile","ceiling grid")
        exception_language=has("remain assigned to their respective trades","system-specific",
                               "mep removals remain","mep removal remains")
        if general_demo_objects and exception_language:
            return "Demolition"

        # Protected MEP/system demo.
        if has("fire alarm","smoke detector","horn strobe","pull station","notification appliance"):
            return "Fire Alarm"
        if has("card reader","card access","access control","electronic strike","electric strike",
               "electrified strike","request to exit","rex device","door contact","security contact",
               "camera","cctv","security system","intrusion alarm","data cabling","telecom cabling",
               "low voltage","low-voltage","intercom"):
            return "Low Voltage"
        if has("sprinkler","fire suppression","fire protection piping"):
            return "Fire Sprinkler"
        if has("receptacle","outlet","switch","panelboard","electrical panel","transformer","conduit",
               "wire","wiring","branch circuit","breaker","light fixture","lighting fixture","feeder"):
            return "Electrical"
        if has("water closet","urinal","lavatory","wash fountain","mop sink","floor sink","floor drain",
               "drinking fountain","plumbing fixture","sanitary piping","waste piping","vent piping",
               "domestic water","water heater","plumbing piping"):
            return "Plumbing"
        if has("ductwork","duct","diffuser","grille","register","vav","rtu","ahu","fan coil",
               "exhaust fan","air handler","mechanical equipment","hvac equipment","refrigerant piping"):
            return "HVAC / Mechanical"
        return "Demolition"

    # ------------------------------------------------------------
    # 2) GC / coordination responsibilities
    # ------------------------------------------------------------
    if has("coordinate installation and utility rough-ins","coordinate installation","coordinate utility rough-ins",
           "confirm which appliances","confirm which equipment","confirm owner-furnished",
           "owner furnished versus contractor furnished","owner-furnished versus contractor-furnished",
           "verify furnished by","confirm furnished by","confirm procurement responsibility"):
        return "GC / General Contractor"

    # ------------------------------------------------------------
    # 3) Explicit work-action overrides equipment/location words
    # ------------------------------------------------------------
    if has("electrical connections","electrical connection","disconnecting means","provide power",
           "power connection","branch circuit","electrical circuit","electrical feed","provide disconnect"):
        return "Electrical"

    # Plumbing fixture/equipment package. Location inside millwork never changes ownership.
    if has("plumbing fixture schedule","water closets","urinals","lavatories","wash fountains",
           "mop sink","floor sink","floor drains","drinking fountains","break-room sinks",
           "break room sinks","disposal connections","instantaneous electric water heater","iwh-1",
           "chronomite"):
        return "Plumbing"

    # HVAC equipment package. Roof-mounted/roof curb is location/accessory, not Roofing.
    if has("exhaust fan","ef-1","greenheck","upblast fan","upblast exhaust fan","full-size exhaust duct",
           "full size exhaust duct","backdraft damper"):
        return "HVAC / Mechanical"

    # Roof repair work specifically belongs to Roofing.
    if has("patch roof","roof patch","repair roof","roof repair","restore roof","roof membrane patch",
           "patch roofing","repair roof membrane","restore roof membrane","roof flashing",
           "flash roof penetration","watertight roof restoration"):
        return "Roofing"

    # ------------------------------------------------------------
    # 4) Protected specialty systems
    # ------------------------------------------------------------
    if has("card reader","card access","access control","electronic strike","electric strike",
           "electrified strike","request to exit","rex device","door contact","security contact",
           "camera","cctv","security system","intrusion alarm","data cabling","telecom cabling",
           "low voltage","low-voltage","intercom"):
        return "Low Voltage"

    if has("sprinkler head","sprinkler heads","sprinkler piping","fire sprinkler","fire suppression piping",
           "sprinkler branch","sprinkler drop"):
        return "Fire Sprinkler"

    if has("fire extinguisher cabinet","fire extinguisher cabinets","semi-recessed fire extinguisher",
           "larson or equal"):
        return "Specialties"

    # ------------------------------------------------------------
    # 5) Framing/Drywall primary assemblies and supports
    # ------------------------------------------------------------
    if has("concealed wood blocking","wood blocking","plywood backing","wood backing","in-wall backing",
           "in wall backing","metal backing","concealed backing","support framing","equivalent supports"):
        return "Framing / Drywall"

    if has("marlite","symmetrix","smart seam","frp","fiberglass reinforced panel",
           "fiberglass-reinforced panel","wainscot"):
        return "Framing / Drywall"

    if has("wall type b","gypsum-board ceiling","gypsum board ceiling","resilient channels",
           "resilient channel","acoustical sealant","taped joints","metal-stud jamb","metal stud jamb",
           "metal-stud header","metal stud header","rough openings","rough opening","stud framing",
           "metal studs","metal stud","steel studs","steel stud","gypsum board","gyp board","drywall",
           "shaftwall","shaft wall"):
        return "Framing / Drywall"

    if has("wall patch","patch wall","patch drywall","drywall patch","gypsum patch","patch gypsum",
           "repair drywall","repair gypsum","gyp board patch","patch gyp","patch and repair wall",
           "patch gypsum board","wall or ceiling openings resulting from demolition"):
        return "Framing / Drywall"

    # ------------------------------------------------------------
    # 6) Ceilings
    # ------------------------------------------------------------
    if has("suspended acoustical tile ceiling","suspended acoustical tile ceilings",
           "acoustical tile ceiling","acoustical tile ceilings","acoustic ceiling tile",
           "acoustical ceiling tile","ceiling tiles and grid","ceiling tile and grid",
           "ceiling tiles","ceiling grid","act ceiling","suspension system"):
        return "Ceilings"

    # ------------------------------------------------------------
    # 7) Doors vs Storefront/Glazing -- primary system first
    # ------------------------------------------------------------
    interior_door_system=has("interior doors and frames","interior door and frame","hollow-metal framed windows",
                             "hollow metal framed windows","interior hollow-metal","interior hollow metal",
                             "door schedule","door hardware sets","rotary white birch doors","hollow-metal doors",
                             "hollow metal doors")
    if interior_door_system:
        return "Doors / Frames / Hardware"

    # True storefront/glazing system only; generic 'glazing' is not enough.
    if has("aluminum storefront","storefront system","store front system","curtain wall","aluminum entrance",
           "exterior storefront","storefront doors","storefront frames"):
        return "Storefront / Glazing"

    if has("door frame","door frames","wood door","wood doors","hollow metal frame","hollow-metal frame",
           "door hardware","hardware set","door closer","panic hardware","exit device","lockset",
           "door threshold","door sweep","door hinges"):
        return "Doors / Frames / Hardware"

    # ------------------------------------------------------------
    # 8) Finishes / tile / flooring / paint
    # ------------------------------------------------------------
    if has("faux tile backsplash","tile backsplash","backsplash wt2","aspect ideas a9550",
           "ceramic tile","porcelain tile","wall tile","floor tile","tile base","tile grout",
           "tile mortar","tile adhesive","setting bed","tile setting","tile trim","schluter"):
        return "Tile"

    if has("rubber base","resilient base","vinyl base","cove base","lvt","luxury vinyl","vct",
           "carpet tile","sheet vinyl","resilient flooring","floor transition","transition strip"):
        return "Flooring"

    if has("paint or refinish","paint patched","paint patch","repaint","touch-up paint","touch up paint",
           "paint repair","refinish patched","finish paint","painting of patched surfaces"):
        return "Painting"

    if has("casework","millwork","cabinet","countertop"):
        return "Millwork / Casework"

    # Conservative fallbacks: do not allow location/source words to steal ownership.
    if has("electrical","receptacle","panelboard","conduit","circuit"):
        return "Electrical"
    if has("plumbing","sanitary","domestic water"):
        return "Plumbing"
    if has("hvac","mechanical","ductwork","diffuser","vav"):
        return "HVAC / Mechanical"
    if has("paint","painting","primer","finish coat"):
        return "Painting"

    return proposed

def _v33_reclassify_data(data):
    """Rebuild every parent trade scope from final item ownership."""
    grouped={}
    for td in data.get("trade_scopes") or []:
        proposed=_v33_normalize_trade(td.get("trade"))
        for item in td.get("items") or []:
            if not isinstance(item,dict):
                continue
            target=_v33_trade_for_item(item, proposed) or "Unassigned"
            clean=dict(item)
            clean["assigned_trade"]=target
            # Preserve source and original proposed trade for auditability.
            if proposed and proposed != target:
                clean["original_proposed_trade"]=proposed
            grouped.setdefault(target,[]).append(clean)

    division_defaults={
        "GC / General Contractor":"01","Demolition":"02","Concrete":"03","Masonry":"04",
        "Structural Steel":"05","Rough Carpentry":"06","Waterproofing":"07","Roofing":"07",
        "Doors / Frames / Hardware":"08","Storefront / Glazing":"08","Framing / Drywall":"09",
        "Ceilings":"09","Flooring":"09","Tile":"09","Painting":"09","Specialties":"10",
        "Fire Sprinkler":"21","Plumbing":"22","HVAC / Mechanical":"23","Controls":"23",
        "Electrical":"26","Low Voltage":"27","Fire Alarm":"28"
    }
    rebuilt=[]
    for trade in sorted(grouped):
        items=grouped[trade]
        rebuilt.append({
            "trade":trade,
            "division":division_defaults.get(trade,""),
            "summary":f"BuildCommand source-backed scope for {trade}.",
            "items":items
        })
    data["trade_scopes"]=rebuilt
    notes=data.setdefault("review_notes",[])
    notes.append("v34.1 rebuilt every parent trade scope from final action-first ownership; source sheet/location cannot override the primary work assembly.")
    return data



# ============================================================
# v44.1 BLUEPRINT OWNERSHIP GUARD
# ============================================================

_V441_TRADES=set(V33_TRADES)

def _v441_primary_trade(requirement, proposed):
    s=str(requirement or "").lower().strip()
    def has(*terms): return any(x in s for x in terms)

    # 1. PRIMARY ACTION FIRST.
    explicit_demo=(
        s.startswith("demo ") or s.startswith("demolish ") or
        s.startswith("remove existing ") or s.startswith("remove the existing ") or
        s.startswith("remove the architectural ") or s.startswith("remove architectural ") or
        s.startswith("remove and dispose ") or s.startswith("perform non-system") or
        "specifically marked for removal" in s or "identified for demolition" in s
    )
    if explicit_demo:
        return "Demolition"

    # Generic firestopping coordination/spec language is excluded later, not routed as a trade scope.
    # Finish actions beat substrate/material words.
    if has("paint ","paint gypsum","paint hollow","prime and refinish","prime and paint",
           "paint or refinish","repaint","touch-up paint","touch up paint",
           "refinish all wall","finish paint","painting of patched surfaces"):
        return "Painting"

    # Supporting construction work owns itself even when caused by MEP.
    if has("slab cutting","sawcut slab","saw cut slab","concrete cutting",
           "trenching and restoration","patch concrete","concrete patch",
           "restore concrete","slab restoration"):
        return "Concrete"

    # Ceiling coordination/layout belongs to ceilings; device names are interfaces only.
    if has("coordinate ceiling grid layout","ceiling grid layout and openings",
           "coordinate ceiling openings","suspended acoustical tile ceiling",
           "acoustical tile ceiling","acoustic ceiling tile","acoustical ceiling tile",
           "ceiling tile and grid","ceiling tiles and grid","ceiling grid","act ceiling",
           "suspension system"):
        return "Ceilings"

    # Duct smoke devices tied to air-conditioning units are HVAC in this BuildCommand ownership model.
    if has("duct smoke detector","duct smoke detectors"):
        return "HVAC / Mechanical"

    # Electronic accessible/automatic door operators and touch pads are Low Voltage.
    if has("electronic accessible door operator","automatic door operator",
           "accessible door operator","door operator with","touch pads","activation touch pad"):
        return "Low Voltage"

    # Toilet/bath accessories are not plumbing.
    if has("grab bar","grab bars","toilet partition","toilet partitions","urinal screen","urinal screens",
           "toilet tissue holder","paper towel/waste","paper towel dispenser","soap dispenser",
           "robe hook","framed mirror","toilet-seat-cover","toilet seat cover",
           "sanitary-napkin","sanitary napkin","bath accessory","bathroom accessory",
           "toilet room accessory"):
        return "Toilet / Bath Accessories"

    # Storefront hardware remains storefront when explicitly part of storefront/entrance assembly.
    if has("storefront door hardware","storefront entrance hardware","aluminum entrance hardware"):
        return "Storefront / Glazing"

    # Protected specialty systems.
    if has("fire alarm","horn strobe","pull station","notification appliance","smoke detector"):
        return "Fire Alarm"
    if has("card access","card reader","access control","electronic strike","electric strike",
           "electrified strike","request to exit","rex device","door contact","cctv","camera",
           "security system","intrusion alarm","data cabling","telecom cabling","intercom"):
        return "Low Voltage"
    if has("sprinkler head","sprinkler heads","sprinkler piping","fire sprinkler",
           "fire suppression piping","sprinkler branch","sprinkler drop"):
        return "Fire Sprinkler"

    if has("patch roof","roof patch","repair roof","roof repair","restore roof","roof membrane patch",
           "patch roofing","repair roof membrane","flash roof penetration","roof flashing",
           "watertight roof","roof penetration patch"):
        return "Roofing"

    if has("electrical connection","electrical connections","disconnecting means","provide power",
           "branch circuit","electrical feed","power connection","provide disconnect"):
        return "Electrical"

    if has("water closet","urinal","lavatory","wash fountain","mop sink","floor sink","floor drain",
           "drinking fountain","plumbing fixture","break-room sink","break room sink",
           "disposal connection","instantaneous electric water heater","electric domestic water heater",
           "water heater wh-","chronomite","iwh-1","expansion tank","recirculation pump"):
        return "Plumbing"

    if has("exhaust fan","upblast fan","greenheck","backdraft damper","full-size exhaust duct",
           "full size exhaust duct","rtu","ahu","air handler","vav","diffuser","grille","ductwork",
           "thermostat","thermostats","rooftop unit","air-conditioning unit","air conditioning unit"):
        return "HVAC / Mechanical"

    if has("concealed wood blocking","wood blocking","wood backing","plywood backing","metal backing",
           "in-wall backing","in wall backing","support framing","rough opening","metal-stud jamb",
           "metal stud jamb","metal-stud header","metal stud header","stud framing","metal studs",
           "steel studs","gypsum board","gyp board","drywall","shaftwall","shaft wall",
           "patch gypsum","patch drywall","wall patch","patch wall","repair drywall",
           "marlite","symmetrix","frp","wainscot"):
        return "Framing / Drywall"

    if has("interior doors and frames","interior door and frame","hollow-metal framed window",
           "hollow metal framed window","door schedule","door hardware set","hollow-metal door",
           "hollow metal door","door frame","door hardware","lockset","panic hardware","exit device"):
        return "Doors / Frames / Hardware"

    if has("aluminum storefront","storefront system","store front system","curtain wall",
           "aluminum entrance","exterior storefront","storefront door","storefront frame"):
        return "Storefront / Glazing"

    if has("faux tile backsplash","tile backsplash","ceramic tile","porcelain tile","wall tile",
           "floor tile","tile base","tile grout","tile mortar","tile adhesive","schluter",
           "rubber base","resilient base","vinyl base","cove base","lvt","luxury vinyl","vct",
           "carpet tile","sheet vinyl","resilient flooring","floor transition","transition strip"):
        return "Flooring / Tile"

    if has("fire extinguisher cabinet","fire extinguisher cabinets","semi-recessed fire extinguisher"):
        return "Specialties"

    if has("casework","millwork","countertop"):
        return "Millwork"

    return _v33_normalize_trade(proposed)

def _v442_exclude_scope_item(requirement):
    s=str(requirement or "").lower()
    # User-approved exclusion: generic firestop penetration specification/coordination statement.
    return (
        ("firestop" in s or "firestopping" in s) and
        ("penetration" in s or "penetrations" in s) and
        ("listed assembl" in s or "restore" in s or "original rating" in s)
    )

def _v441_apply_approved_learning(pid, requirement, trade):
    try:
        _v43_ensure_tables()
        c=db()
        rows=c.execute("""
            SELECT * FROM learning_rules
            WHERE company_id=? AND approval_status='APPROVED'
              AND (project_id=? OR scope_level='COMPANY STANDARD')
              AND rule_type IN ('TRADE ASSIGNMENT','SCOPE BOUNDARY')
            ORDER BY CASE WHEN project_id=? THEN 0 ELSE 1 END,id DESC
        """,(current_company_id(),pid,pid)).fetchall()
        c.close()
        low=str(requirement or "").lower()
        for r in rows:
            subject=str(r["subject"] or "").strip().lower()
            if subject and subject in low:
                learned=str(r["learned_rule"] or "").strip()
                target=learned.split("->")[-1].strip() if "->" in learned else learned
                target=_v33_normalize_trade(target)
                if target in _V441_TRADES:
                    return target
    except Exception:
        pass
    return trade

def _v441_reclassify_with_learning(pid,data):
    grouped={}
    for td in data.get("trade_scopes") or []:
        proposed=_v33_normalize_trade(td.get("trade"))
        for item in td.get("items") or []:
            if not isinstance(item,dict): continue
            req=str(item.get("requirement") or "").strip()
            if not req: continue
            if _v442_exclude_scope_item(req):
                continue
            target=_v33_trade_for_item(item,proposed) or proposed
            target=_v441_primary_trade(req,target)
            target=_v441_apply_approved_learning(pid,req,target)
            clean=dict(item)
            clean["assigned_trade"]=target
            if proposed != target:
                clean["original_proposed_trade"]=proposed
            grouped.setdefault(target,[]).append(clean)

    div={
        "GC / General Contractor":"01","Demolition":"02","Concrete":"03","Masonry":"04",
        "Structural Steel":"05","Rough Carpentry":"06","Waterproofing":"07","Roofing":"07",
        "Doors / Frames / Hardware":"08","Storefront / Glazing":"08","Framing / Drywall":"09",
        "Ceilings":"09","Flooring / Tile":"09","Painting":"09","Millwork":"12",
        "Toilet / Bath Accessories":"10","Specialties":"10","Fire Sprinkler":"21","Plumbing":"22","HVAC / Mechanical":"23",
        "Controls":"23","Electrical":"26","Low Voltage":"27","Fire Alarm":"28"
    }
    data["trade_scopes"]=[
        {"trade":trade,"division":div.get(trade,""),
         "summary":f"BuildCommand source-backed scope for {trade}.","items":items}
        for trade,items in sorted(grouped.items())
    ]
    data.setdefault("review_notes",[]).append(
        "v44.1 Blueprint Ownership Guard applied action/assembly routing and approved learning rules before save."
    )
    return data

def _save_blueprint_result(pid, docs, data, model_name):
    data=_v33_reclassify_data(data)
    data=_v441_reclassify_with_learning(pid,data)
    company_id=current_company_id(); user_id=current_user_id(); now=datetime.utcnow().isoformat()
    c=db()
    c.execute("INSERT INTO blueprint_runs(company_id,project_id,status,source_files,project_summary,detected_disciplines,cross_discipline_flags,rfi_candidates,review_notes,model_name,created_by,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(
        company_id,pid,"COMPLETE",json.dumps([d["original_name"] for d in docs]),str(data.get("project_summary") or ""),json.dumps(data.get("detected_disciplines") or []),json.dumps(data.get("cross_discipline_flags") or []),json.dumps(data.get("rfi_candidates") or []),json.dumps(data.get("review_notes") or []),model_name,user_id,now
    ))
    run_id=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    for trade_data in data.get("trade_scopes") or []:
        trade=(trade_data.get("trade") or "Unassigned").strip() or "Unassigned"
        division=str(trade_data.get("division") or "").strip()
        summary=str(trade_data.get("summary") or "").strip()
        items=trade_data.get("items") or []
        scope_text=_blueprint_scope_text(trade_data)
        c.execute("INSERT INTO blueprint_trade_scopes(company_id,project_id,run_id,trade,division,summary,scope_text,item_count,created) VALUES(?,?,?,?,?,?,?,?,?)",(company_id,pid,run_id,trade,division,summary,scope_text,len(items),now))
        trade_scope_id=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for item in items:
            req=str(item.get("requirement") or "").strip()
            if not req:
                continue
            confidence=str(item.get("confidence") or "MEDIUM").upper()
            if confidence not in {"HIGH","MEDIUM","LOW"}: confidence="MEDIUM"
            item_type=str(item.get("item_type") or "SCOPE").upper()
            c.execute("INSERT INTO blueprint_scope_items(company_id,project_id,run_id,trade_scope_id,trade,requirement,source_sheet,source_detail,source_spec,source_note,related_trade,confidence,item_type,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                company_id,pid,run_id,trade_scope_id,trade,req,str(item.get("source_sheet") or ""),str(item.get("source_detail") or ""),str(item.get("source_spec") or ""),str(item.get("source_note") or ""),str(item.get("related_trade") or ""),confidence,item_type,"NOT_STARTED",now
            ))
    c.commit(); c.close(); return run_id


def _blueprint_latest(pid):
    c=db(); run=c.execute("SELECT * FROM blueprint_runs WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1",(current_company_id(),pid)).fetchone()
    scopes=[]
    if run:
        scopes=c.execute("SELECT * FROM blueprint_trade_scopes WHERE company_id=? AND project_id=? AND run_id=? ORDER BY trade",(current_company_id(),pid,run["id"])).fetchall()
    c.close(); return run,scopes



# ============================================================
# v34 BUILDCOMMAND CORE BRAIN + ESTIMATOR INTELLIGENCE
# ============================================================

def _ensure_v34_estimator_tables():
    """
    v34.2 estimator schema repair.
    PostgreSQL needs a sequence/default for estimator_items.id. Earlier v34 used
    c.execute(CREATE TABLE...) instead of executescript(), so PgCompat did not
    translate INTEGER PRIMARY KEY to BIGSERIAL. Repair existing installs in place.
    """
    c=db()
    if DATABASE_KIND=="postgres":
        c.execute("""
            CREATE TABLE IF NOT EXISTS estimator_items(
                id BIGSERIAL PRIMARY KEY,
                company_id BIGINT,
                project_id BIGINT,
                blueprint_scope_item_id BIGINT,
                trade TEXT,
                description TEXT,
                source_ref TEXT,
                quantity DOUBLE PRECISION DEFAULT 0,
                unit TEXT DEFAULT '',
                material_unit_cost DOUBLE PRECISION DEFAULT 0,
                labor_unit_cost DOUBLE PRECISION DEFAULT 0,
                subcontract_quote DOUBLE PRECISION DEFAULT 0,
                allowance DOUBLE PRECISION DEFAULT 0,
                markup_pct DOUBLE PRECISION DEFAULT 0,
                notes TEXT DEFAULT '',
                verified INTEGER DEFAULT 0,

                ai_quantity DOUBLE PRECISION,
                ai_unit TEXT DEFAULT '',
                ai_confidence TEXT DEFAULT '',
                ai_basis TEXT DEFAULT '',
                ai_source TEXT DEFAULT '',
                ai_updated TEXT,
                created TEXT,
                updated TEXT
            )
        """)
        # Repair a v34 table that already exists with "id INTEGER PRIMARY KEY"
        # and therefore has no sequence/default.
        try:
            c.execute("CREATE SEQUENCE IF NOT EXISTS estimator_items_id_seq")
            c.execute("ALTER SEQUENCE estimator_items_id_seq OWNED BY estimator_items.id")
            c.execute("""
                ALTER TABLE estimator_items
                ALTER COLUMN id SET DEFAULT nextval('estimator_items_id_seq')
            """)
            c.execute("""
                SELECT setval(
                    'estimator_items_id_seq',
                    GREATEST(COALESCE((SELECT MAX(id) FROM estimator_items),0)+1,1),
                    false
                )
            """)
        except Exception:
            c.rollback()
            c=db()
            # If another deploy/request repaired it concurrently, normal use can continue.
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS estimator_items(
                id INTEGER PRIMARY KEY,
                company_id INTEGER,
                project_id INTEGER,
                blueprint_scope_item_id INTEGER,
                trade TEXT,
                description TEXT,
                source_ref TEXT,
                quantity REAL DEFAULT 0,
                unit TEXT DEFAULT '',
                material_unit_cost REAL DEFAULT 0,
                labor_unit_cost REAL DEFAULT 0,
                subcontract_quote REAL DEFAULT 0,
                allowance REAL DEFAULT 0,
                markup_pct REAL DEFAULT 0,
                notes TEXT DEFAULT '',
                verified INTEGER DEFAULT 0,

                ai_quantity REAL,
                ai_unit TEXT DEFAULT '',
                ai_confidence TEXT DEFAULT '',
                ai_basis TEXT DEFAULT '',
                ai_source TEXT DEFAULT '',
                ai_updated TEXT,

                created TEXT,
                updated TEXT
            )
        """)

    # v36 automatic takeoff proposal fields. Add safely to existing v34/v35 tables.
    v36_columns=[
        ("ai_quantity","DOUBLE PRECISION" if DATABASE_KIND=="postgres" else "REAL"),
        ("ai_unit","TEXT DEFAULT ''"),
        ("ai_confidence","TEXT DEFAULT ''"),
        ("ai_basis","TEXT DEFAULT ''"),
        ("ai_source","TEXT DEFAULT ''"),
        ("ai_updated","TEXT")
    ]
    for col,col_type in v36_columns:
        try:
            c.execute(f"ALTER TABLE estimator_items ADD COLUMN {col} {col_type}")
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass

    # Helpful lookup/index; duplicate historical rows are tolerated, so no UNIQUE migration.
    try:
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_estimator_project_scope
            ON estimator_items(company_id,project_id,blueprint_scope_item_id)
        """)
    except Exception:
        pass
    c.commit(); c.close()

def _latest_blueprint_run(pid):
    c=db()
    row=c.execute("""SELECT * FROM blueprint_runs
                     WHERE company_id=? AND project_id=?
                     ORDER BY id DESC LIMIT 1""",
                  (current_company_id(),pid)).fetchone()
    c.close(); return row

def _seed_estimator_from_latest(pid):
    """
    Sync estimator rows from the latest cleaned Blueprint/Plan Intelligence run.
    Existing user-entered pricing/quantities are preserved, while trade, description,
    and source references are refreshed from v34.1+ corrected scope ownership.
    """
    _ensure_v34_estimator_tables()
    run=_latest_blueprint_run(pid)
    if not run:
        return {"added":0,"updated":0,"run_id":None}

    c=db()
    rows=c.execute("""
        SELECT i.id,i.requirement,i.source_sheet,i.source_detail,i.source_spec,s.trade
        FROM blueprint_scope_items i
        JOIN blueprint_trade_scopes s ON s.id=i.trade_scope_id
        WHERE i.company_id=? AND i.project_id=? AND s.run_id=?
        ORDER BY s.trade,i.id
    """,(current_company_id(),pid,run["id"])).fetchall()

    added=0; updated=0; now=datetime.utcnow().isoformat()
    latest_scope_ids=set()

    for r in rows:
        scope_item_id=int(r["id"])
        latest_scope_ids.add(scope_item_id)
        refs=[str(r[x] or "").strip() for x in ["source_sheet","source_detail","source_spec"]
              if str(r[x] or "").strip()]
        source_ref=" · ".join(refs)

        existing=c.execute("""
            SELECT id,trade,description,source_ref
            FROM estimator_items
            WHERE company_id=? AND project_id=? AND blueprint_scope_item_id=?
            ORDER BY id LIMIT 1
        """,(current_company_id(),pid,scope_item_id)).fetchone()

        if existing:
            # Keep estimator-entered numbers/notes but refresh scope truth.
            if ((existing["trade"] or "") != (r["trade"] or "") or
                (existing["description"] or "") != (r["requirement"] or "") or
                (existing["source_ref"] or "") != source_ref):
                c.execute("""
                    UPDATE estimator_items
                    SET trade=?,description=?,source_ref=?,updated=?
                    WHERE id=? AND company_id=? AND project_id=?
                """,(r["trade"],r["requirement"],source_ref,now,
                     existing["id"],current_company_id(),pid))
                updated+=1
            continue

        c.execute("""
            INSERT INTO estimator_items(
                company_id,project_id,blueprint_scope_item_id,trade,description,source_ref,
                quantity,unit,material_unit_cost,labor_unit_cost,subcontract_quote,allowance,
                markup_pct,notes,verified,created,updated
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(current_company_id(),pid,scope_item_id,r["trade"],r["requirement"],
             source_ref,0,"",0,0,0,0,0,"",0,now,now))
        added+=1

    c.commit(); c.close()
    return {"added":added,"updated":updated,"run_id":run["id"]}

def _estimate_math(row):
    qty=float(row["quantity"] or 0); mat=float(row["material_unit_cost"] or 0)
    labor=float(row["labor_unit_cost"] or 0); sub=float(row["subcontract_quote"] or 0)
    allowance=float(row["allowance"] or 0); markup=float(row["markup_pct"] or 0)
    direct=(qty*(mat+labor))+sub+allowance
    return direct,direct*(1+(markup/100.0))

@app.get("/brain",response_class=HTMLResponse)
def buildcommand_core_brain():
    pid=project_id(); run=_latest_blueprint_run(pid); c=db()
    project=c.execute("SELECT name,number FROM projects WHERE id=?",(pid,)).fetchone()
    scope_count=item_count=cross_count=rfi_count=0
    if run:
        scope_count=c.execute("SELECT COUNT(*) n FROM blueprint_trade_scopes WHERE run_id=?",(run["id"],)).fetchone()["n"]
        item_count=c.execute("SELECT COUNT(*) n FROM blueprint_scope_items WHERE run_id=?",(run["id"],)).fetchone()["n"]
        try: cross_count=len(json.loads(run["cross_discipline_flags"] or "[]"))
        except: cross_count=0
        try: rfi_count=len(json.loads(run["rfi_candidates"] or "[]"))
        except: rfi_count=0
    open_issues=c.execute("SELECT COUNT(*) n FROM project_issues WHERE project_id=? AND status!='CLOSED'",(pid,)).fetchone()["n"]
    activities=c.execute("SELECT COUNT(*) n FROM activities WHERE project_id=?",(pid,)).fetchone()["n"]
    c.close()
    project_label=f'{esc(project["number"])} - {esc(project["name"])}' if project else "Current Project"
    latest=(f'<span class="badge READY">PLAN INTELLIGENCE READY</span><div class="small" style="margin-top:8px">Latest run #{run["id"]} · {esc(run["created"] or "")}</div>'
            if run else '<span class="badge WATCH">NO PLAN ANALYSIS YET</span>')
    body=f"""
    <div class="hero">
      <div class="eyebrow">BuildCommand Core Brain · v34.1</div>
      <h1>One construction brain. One project memory.</h1>
      <div class="muted">{project_label}</div><div style="margin-top:12px">{latest}</div>
    </div>
    <div class="grid4">
      <div class="card"><div class="label">Trade Scopes</div><div class="kpi">{scope_count}</div></div>
      <div class="card"><div class="label">Scope Items</div><div class="kpi">{item_count}</div></div>
      <div class="card"><div class="label">Cross-Discipline</div><div class="kpi">{cross_count}</div></div>
      <div class="card"><div class="label">RFI Candidates</div><div class="kpi">{rfi_count}</div></div>
    </div>
    <div class="grid2">
      <div class="card">
        <h2>Ask the BuildCommand Brain</h2>
        <form method="post" action="/brain">
          <textarea name="question" required placeholder="Example: What are the biggest scope and estimating risks on this project?"></textarea>
          <button type="submit">Ask BuildCommand</button>
        </form>
        <p class="small">Core Brain uses operations data plus the latest source-backed plan intelligence.</p>
      </div>
      <div class="card">
        <h2>One Brain Tools</h2>
        <p><a href="/plans-specs-ai"><b>Plan Intake</b></a> — read plans and create trade scopes.</p>
        <p><a href="/brain/estimator"><b>Estimator Intelligence</b></a> — estimate from those same scope items.</p>
        <p><a href="/issues"><b>RFIs / Issues</b></a> — manage scope gaps and coordination issues.</p>
        <p><a href="/schedule"><b>Schedule</b></a> — connect scope to execution.</p>
      </div>
    </div>
    <div class="grid2">
      <div class="card"><h2>Operations connected</h2><div class="kpi">{activities}</div><div class="muted">schedule activities</div></div>
      <div class="card"><h2>Open issues connected</h2><div class="kpi">{open_issues}</div><div class="muted">project issues</div></div>
    </div>"""
    return shell("BuildCommand Brain",body)

@app.post("/brain",response_class=HTMLResponse)
def buildcommand_core_brain_answer(question:str=Form(...)):
    pid=project_id(); run=_latest_blueprint_run(pid); c=db()
    scope_rows=[]
    if run:
        scope_rows=c.execute("""
            SELECT s.trade,i.requirement,i.source_sheet,i.source_detail,i.source_spec
            FROM blueprint_scope_items i
            JOIN blueprint_trade_scopes s ON s.id=i.trade_scope_id
            WHERE i.company_id=? AND i.project_id=? AND s.run_id=?
            ORDER BY s.trade,i.id LIMIT 500
        """,(current_company_id(),pid,run["id"])).fetchall()
    c.close()
    plan_context="\n".join(
        f'- {r["trade"]}: {r["requirement"]} | source: {r["source_sheet"] or ""} {r["source_detail"] or ""} {r["source_spec"] or ""}'
        for r in scope_rows
    ) or "No plan/scope intelligence available yet."
    api_key=os.environ.get("OPENAI_API_KEY")
    if not api_key:
        answer="OPENAI_API_KEY is not configured."
    else:
        try:
            operations=build_project_context(pid)
            client=OpenAI(api_key=api_key)
            resp=client.responses.create(
                model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
                instructions="""You are the BuildCommand Core Brain, a construction operations and estimating intelligence system.
Use only supplied project facts as job-specific facts.
Think across estimating, trade scope, drawings, schedule, field execution, RFIs, procurement, inspections and closeout.
Never invent quantities, dimensions, costs, field conditions, commitments or drawing requirements.
If quantity/cost is not supported, say ESTIMATOR VERIFICATION REQUIRED.
Classify by actual work, not by drawing discipline.
Non-MEP demolition belongs to Demolition; MEP/system demo stays with the system trade.
Blocking/backing belongs to Framing/Drywall. Roof patching belongs to Roofing.
Sprinkler heads/piping belong to Fire Sprinkler.
Card access/electronic strikes/cameras/security belong to Low Voltage.
Storefront/glazing is separate from standard Doors/Frames/Hardware.""",
                input=f"""PROJECT OPERATIONS DATA
{operations}

LATEST PLAN / TRADE SCOPE INTELLIGENCE
{plan_context}

USER QUESTION
{question}"""
            )
            answer=resp.output_text
        except Exception as exc:
            answer=f"BuildCommand Brain could not complete the analysis: {exc}"
    body=f"""<div class="hero"><div class="eyebrow">BuildCommand Core Brain</div><h1>Project Answer</h1></div>
    <div class="grid2"><div class="card"><div class="small">QUESTION</div><h3>{esc(question)}</h3><p><a href="/brain">← Back to Brain</a></p></div>
    <div class="card"><div class="small">BUILDCOMMAND BRAIN</div><div style="white-space:pre-wrap;line-height:1.6">{esc(answer)}</div></div></div>"""
    return shell("BuildCommand Brain",body)


# ============================================================
# v35 ESTIMATOR TAKEOFF INTELLIGENCE
# ============================================================

def _suggest_takeoff_unit(trade, description):
    t=(str(description or "")+" "+str(trade or "")).lower()

    # Counts / equipment
    if any(x in t for x in [
        "door","frame","window","sprinkler head","receptacle","outlet","switch",
        "fixture","water closet","urinal","lavatory","sink","floor drain","cleanout",
        "diffuser","grille","vav","fan","rtu","ahu","panel","transformer","disconnect",
        "card reader","camera","fire extinguisher cabinet","cabinet","appliance"
    ]):
        return "EA"

    # Linear measurements
    if any(x in t for x in [
        "rubber base","resilient base","cove base","base molding","curb","handrail",
        "guardrail","pipe","piping","conduit","duct","ductwork","gutter","flashing",
        "joint sealant","wall base"
    ]):
        return "LF"

    # Area measurements
    if any(x in t for x in [
        "flooring","carpet","lvt","vct","tile","wall tile","floor tile","ceiling",
        "ceiling tile","drywall","gypsum board","wallboard","paint","painting",
        "frp","marlite","wainscot","roof membrane","roofing","waterproofing",
        "storefront glazing","glazing","insulation"
    ]):
        return "SF"

    # Volume
    if any(x in t for x in ["concrete","slab","footing","grade beam","grout fill"]):
        return "CY"

    # Earthwork
    if any(x in t for x in ["excavation","backfill","import soil","export soil"]):
        return "CY"

    # Lump-sum / coordination / general conditions
    if any(x in t for x in [
        "coordinate","confirm","verify","allowance","general conditions","mobilization",
        "testing","commissioning","permit","closeout","temporary protection"
    ]):
        return "LS"

    return "EA"


def _takeoff_status(row):
    qty=float(row["quantity"] or 0)
    verified=bool(int(row["verified"] or 0))
    if verified:
        return "VERIFIED"
    if qty > 0:
        return "QUANTITY_ENTERED"
    return "NEEDS_TAKEOFF"




# ============================================================
# v36.1 TAKEOFF COMPONENT SPLITTER
# ============================================================

def _ensure_takeoff_component_tables():
    c=db()
    if DATABASE_KIND=="postgres":
        c.execute("""
            CREATE TABLE IF NOT EXISTS takeoff_components(
                id BIGSERIAL PRIMARY KEY,
                company_id BIGINT,
                project_id BIGINT,
                estimator_item_id BIGINT,
                component_name TEXT,
                description TEXT,
                unit TEXT,
                quantity DOUBLE PRECISION,
                confidence TEXT DEFAULT 'VERIFY',
                basis TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                status TEXT DEFAULT 'PROPOSED',
                created TEXT,
                updated TEXT
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS takeoff_components(
                id INTEGER PRIMARY KEY,
                company_id INTEGER,
                project_id INTEGER,
                estimator_item_id INTEGER,
                component_name TEXT,
                description TEXT,
                unit TEXT,
                quantity REAL,
                confidence TEXT DEFAULT 'VERIFY',
                basis TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                status TEXT DEFAULT 'PROPOSED',
                created TEXT,
                updated TEXT
            )
        """)
    try:
        c.execute("""CREATE INDEX IF NOT EXISTS idx_takeoff_components_parent
                     ON takeoff_components(company_id,project_id,estimator_item_id)""")
    except Exception:
        pass
    c.commit()
    c.close()


def _component_split_prompt(rows):
    lines=[]
    for r in rows:
        lines.append(
            f'ESTIMATOR_ID={r["id"]} | TRADE={r["trade"]} | '
            f'SCOPE={r["description"]} | SOURCE={r["source_ref"] or ""}'
        )
    return """You are BuildCommand Takeoff Component Splitter.

Break each construction estimator scope item into separate measurable takeoff components ONLY when the scope contains multiple distinct measurable objects or measurement types.

RULES:
- Preserve the parent trade. Do not reclassify.
- Do not invent quantities.
- One component should normally have one measurement unit.
- Use EA for countable fixtures/equipment/devices, LF for linear work, SF for area work, CY for volume, LS for coordination/non-measurable scope.
- Do not split a simple single-measurement scope unnecessarily.
- Example: "Provide exhaust fan, roof curb, backdraft damper and full-size duct" becomes exhaust fan EA; roof curb EA; backdraft damper EA; exhaust duct LF.
- Example: "Provide water closets, lavatories, floor drains and domestic water piping" becomes water closets EA; lavatories EA; floor drains EA; domestic water piping LF.
- Keep the same source reference unless a more specific source is obvious.
- Return JSON only.

Return:
{
  "results":[
    {
      "estimator_id":123,
      "components":[
        {"name":"Exhaust fan EF-1","description":"Provide EF-1 exhaust fan","unit":"EA","source":"M401"},
        {"name":"Exhaust duct","description":"Provide full-size exhaust duct","unit":"LF","source":"M401"}
      ]
    }
  ]
}

ESTIMATOR SCOPE ITEMS
---------------------
""" + "\n".join(lines)


@app.post("/brain/takeoff/split-components",response_class=HTMLResponse)
def split_takeoff_components():
    pid=project_id()
    _seed_estimator_from_latest(pid)
    _ensure_takeoff_component_tables()
    run=_latest_blueprint_run(pid)
    if not run:
        return shell("Takeoff Components",'<div class="card"><h2>No current plan analysis found.</h2></div>')

    c=db()
    rows=c.execute("""
        SELECT e.*
        FROM estimator_items e
        JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id
        WHERE e.company_id=? AND e.project_id=? AND b.run_id=?
        ORDER BY e.trade,e.id
        LIMIT 180
    """,(current_company_id(),pid,run["id"])).fetchall()
    c.close()

    if not rows:
        return shell("Takeoff Components",'<div class="card"><h2>No estimator items found.</h2></div>')
    if not os.environ.get("OPENAI_API_KEY"):
        return shell("Takeoff Components",'<div class="card"><h2>OPENAI_API_KEY is not configured.</h2></div>')

    try:
        client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp=client.responses.create(
            model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
            input=_component_split_prompt(rows)
        )
        data=_v36_parse_json(resp.output_text)
        valid={int(r["id"]):r for r in rows}
        now=datetime.utcnow().isoformat()
        c=db()
        created=0

        for result in data.get("results") or []:
            try:
                eid=int(result.get("estimator_id"))
            except Exception:
                continue
            if eid not in valid:
                continue

            comps=result.get("components") or []
            if len(comps)<=1:
                continue

            c.execute("""DELETE FROM takeoff_components
                         WHERE company_id=? AND project_id=? AND estimator_item_id=? AND status='PROPOSED'""",
                      (current_company_id(),pid,eid))

            for comp in comps[:25]:
                name=str(comp.get("name") or "").strip()
                desc=str(comp.get("description") or name).strip()
                unit=str(comp.get("unit") or "").upper()
                if not name or unit not in {"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}:
                    continue
                source=str(comp.get("source") or valid[eid]["source_ref"] or "")
                c.execute("""
                    INSERT INTO takeoff_components(
                        company_id,project_id,estimator_item_id,component_name,description,
                        unit,quantity,confidence,basis,source_ref,status,created,updated
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(current_company_id(),pid,eid,name,desc,unit,None,"VERIFY",
                     "Component split from cleaned parent scope; quantity not yet verified.",
                     source,"PROPOSED",now,now))
                created+=1

        c.commit()
        c.close()

        body = (
            '<div class="hero"><div class="eyebrow">BuildCommand · v36.1</div>'
            '<h1>Takeoff components created.</h1></div>'
            f'<div class="card"><div class="kpi">{created}</div>'
            '<div class="muted">measurable child components created without changing the parent scopes.</div>'
            '<p><a href="/brain/takeoff/components"><b>Review components →</b></a></p></div>'
        )
        return shell("Takeoff Components",body)

    except Exception as exc:
        return shell(
            "Takeoff Components",
            f'<div class="card"><h2>Component split did not complete.</h2><p>{esc(str(exc))}</p></div>'
        )


@app.get("/brain/takeoff/components",response_class=HTMLResponse)
def takeoff_components_page():
    pid=project_id()
    _ensure_takeoff_component_tables()
    c=db()
    rows=c.execute("""
        SELECT tc.*,e.trade,e.description parent_description
        FROM takeoff_components tc
        JOIN estimator_items e ON e.id=tc.estimator_item_id
        WHERE tc.company_id=? AND tc.project_id=?
        ORDER BY e.trade,tc.estimator_item_id,tc.id
    """,(current_company_id(),pid)).fetchall()
    c.close()

    cards=[]
    for r in rows:
        qty="" if r["quantity"] is None else r["quantity"]
        confidence_options=''.join(
            f'<option {"selected" if r["confidence"]==x else ""}>{x}</option>'
            for x in ["HIGH","MEDIUM","LOW","VERIFY"]
        )
        status_options=''.join(
            f'<option {"selected" if r["status"]==x else ""}>{x}</option>'
            for x in ["PROPOSED","ACCEPTED","VERIFIED"]
        )

        cards.append(
            f'<div class="card">'
            f'<div class="small">{esc(r["trade"])} · Parent estimator item #{r["estimator_item_id"]}</div>'
            f'<h3>{esc(r["component_name"])}</h3>'
            f'<div>{esc(r["description"])}</div>'
            f'<div class="small">Source: {esc(r["source_ref"] or "")}</div>'
            f'<form method="post" action="/brain/takeoff/component/{r["id"]}" style="margin-top:10px">'
            '<div class="grid4">'
            f'<div><label>Qty</label><input name="quantity" type="number" step="0.01" value="{qty}"></div>'
            f'<div><label>Unit</label><input name="unit" value="{esc(r["unit"] or "")}"></div>'
            f'<div><label>Confidence</label><select name="confidence">{confidence_options}</select></div>'
            f'<div><label>Status</label><select name="status">{status_options}</select></div>'
            '</div>'
            f'<label>Basis / note</label><input name="basis" value="{esc(r["basis"] or "")}">'
            '<button type="submit" style="margin-top:10px">Save Component</button>'
            '</form></div>'
        )

    body = (
        '<div class="hero"><div class="eyebrow">Takeoff Component Splitter · v36.1</div>'
        '<h1>One scope. Separate measurable pieces.</h1></div>'
        '<div class="card"><p><a href="/brain/takeoff">← Back to Takeoff Intelligence</a></p></div>'
        + ("".join(cards) if cards else '<div class="card"><h2>No split components yet.</h2></div>')
    )
    return shell("Takeoff Components",body)


@app.post("/brain/takeoff/component/{component_id}")
def update_takeoff_component(component_id:int,quantity:str=Form(""),unit:str=Form("EA"),
                             confidence:str=Form("VERIFY"),status:str=Form("PROPOSED"),
                             basis:str=Form("")):
    pid=project_id()
    try:
        q=float(quantity) if str(quantity).strip() else None
    except Exception:
        q=None

    unit=unit.upper().strip()
    if unit not in {"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}:
        unit="EA"
    if confidence not in {"HIGH","MEDIUM","LOW","VERIFY"}:
        confidence="VERIFY"
    if status not in {"PROPOSED","ACCEPTED","VERIFIED"}:
        status="PROPOSED"

    c=db()
    c.execute("""UPDATE takeoff_components SET quantity=?,unit=?,confidence=?,status=?,basis=?,updated=?
                 WHERE id=? AND company_id=? AND project_id=?""",
              (q,unit,confidence,status,basis,datetime.utcnow().isoformat(),
               component_id,current_company_id(),pid))
    c.commit()
    c.close()
    return RedirectResponse("/brain/takeoff/components",status_code=303)

# ============================================================
# v36 AUTOMATIC PLAN TAKEOFF BRAIN
# ============================================================

def _v36_latest_plan_docs(pid):
    run=_latest_blueprint_run(pid)
    if not run:
        return run,[]
    try:
        names=json.loads(run["source_files"] or "[]")
    except Exception:
        names=[]
    docs=[]
    c=db()
    for name in names:
        d=c.execute("""
            SELECT * FROM attachments
            WHERE company_id=? AND project_id=? AND original_name=?
            ORDER BY id DESC LIMIT 1
        """,(current_company_id(),pid,name)).fetchone()
        if d:
            docs.append(d)
    c.close()
    return run,docs


def _v36_scope_targets(pid,run_id):
    c=db()
    rows=c.execute("""
        SELECT e.id estimator_id,e.trade,e.description,e.source_ref,e.quantity,e.unit,e.verified,
               b.id scope_item_id,b.confidence scope_confidence
        FROM estimator_items e
        JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id
        WHERE e.company_id=? AND e.project_id=? AND b.run_id=?
        ORDER BY e.trade,e.id
        LIMIT 180
    """,(current_company_id(),pid,run_id)).fetchall()
    c.close()
    return rows


def _v36_takeoff_prompt(targets):
    lines=[]
    for r in targets:
        lines.append(
            f'ESTIMATOR_ID={r["estimator_id"]} | TRADE={r["trade"]} | '
            f'DESCRIPTION={r["description"]} | SOURCE={r["source_ref"] or ""} | '
            f'SUGGESTED_UNIT={_suggest_takeoff_unit(r["trade"],r["description"])}'
        )
    return """You are BuildCommand Automatic Plan Takeoff Brain.

Analyze the attached construction plan PDFs visually and textually against the TAKEOFF TARGETS below.

STRICT RULES:
1. Never guess a quantity.
2. Return a numeric quantity only when the documents provide enough evidence to count or calculate it with reasonable confidence.
3. Prefer explicit schedules, legends, tagged fixture/equipment counts, door/window schedules, device plans, room finish schedules, and clearly dimensioned/calculable information.
4. Do NOT infer scaled LF/SF/CY measurements from a drawing image unless a reliable explicit scale/dimension and geometry are sufficiently clear. If not, return quantity=null and confidence=VERIFY.
5. A scope statement that says "provide doors" is not itself proof of the number of doors. Use schedules/plans as evidence.
6. Avoid double-counting the same tagged object shown on multiple sheets/details.
7. Preserve trade ownership from the supplied target; this task is quantity extraction, not reclassification.
8. For each non-null quantity, cite the best supporting sheet/schedule/detail in source and briefly explain the basis.
9. Confidence must be HIGH, MEDIUM, LOW, or VERIFY.
10. Return JSON only.

Return:
{
  "results":[
    {
      "estimator_id":123,
      "quantity":12,
      "unit":"EA",
      "confidence":"HIGH",
      "basis":"12 unique door marks listed in the door schedule.",
      "source":"A601 Door Schedule"
    },
    {
      "estimator_id":124,
      "quantity":null,
      "unit":"SF",
      "confidence":"VERIFY",
      "basis":"Area requires scaled takeoff; no reliable explicit area found.",
      "source":"A201/A801"
    }
  ],
  "review_notes":[]
}

TAKEOFF TARGETS
----------------
"""+"\n".join(lines)


def _v36_parse_json(raw):
    s=(raw or "").strip()
    if s.startswith("```"):
        s=re.sub(r"^```(?:json)?\s*","",s,flags=re.I)
        s=re.sub(r"\s*```$","",s)
    start=s.find("{"); end=s.rfind("}")
    if start>=0 and end>start:
        s=s[start:end+1]
    return json.loads(s)


@app.post("/brain/takeoff/auto",response_class=HTMLResponse)
def automatic_plan_takeoff():
    pid=project_id()
    _seed_estimator_from_latest(pid)
    run,docs=_v36_latest_plan_docs(pid)
    if not run:
        return shell("Automatic Takeoff",'<div class="card"><h2>No Plan Intelligence run found.</h2><p>Run Plan Intake first.</p></div>')
    if not docs:
        return shell("Automatic Takeoff",'<div class="card"><h2>Source plan files are unavailable.</h2><p>BuildCommand found the scope run but could not locate its original project attachments.</p></div>')
    if not os.environ.get("OPENAI_API_KEY"):
        return shell("Automatic Takeoff",'<div class="card"><h2>OPENAI_API_KEY is not configured.</h2></div>')

    targets=_v36_scope_targets(pid,run["id"])
    if not targets:
        return shell("Automatic Takeoff",'<div class="card"><h2>No current estimator targets found.</h2></div>')

    total=sum(int(d["size_bytes"] or 0) for d in docs if Path(d["original_name"] or "").suffix.lower()==".pdf")
    if total>=50*1024*1024:
        return shell("Automatic Takeoff",f'<div class="card"><h2>Plan set too large for one automatic takeoff pass.</h2><p>Current PDF total: {total/1024/1024:.1f} MB. Run Plan Intake in smaller volumes before automatic takeoff.</p></div>')

    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    uploaded=[]; content=[]
    try:
        for d in docs:
            path=os.path.join(UPLOAD_DIR,d["stored_name"])
            if not os.path.isfile(path):
                continue
            if Path(d["original_name"] or "").suffix.lower()==".pdf":
                with open(path,"rb") as fh:
                    remote=client.files.create(file=fh,purpose="user_data")
                uploaded.append(remote.id)
                content.append({"type":"input_file","file_id":remote.id,"detail":"high"})
        if not content:
            return shell("Automatic Takeoff",'<div class="card"><h2>No PDF plan files were available for visual takeoff.</h2></div>')

        content.append({"type":"input_text","text":_v36_takeoff_prompt(targets)})
        response=client.responses.create(
            model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
            input=[{"role":"user","content":content}]
        )
        data=_v36_parse_json(response.output_text)
        results=data.get("results") or []
        valid_ids={int(r["estimator_id"]) for r in targets}
        now=datetime.utcnow().isoformat()
        saved=0; proposed=0; verify=0
        c=db()
        for item in results:
            try:
                eid=int(item.get("estimator_id"))
            except Exception:
                continue
            if eid not in valid_ids:
                continue
            q=item.get("quantity")
            try:
                q=float(q) if q is not None else None
            except Exception:
                q=None
            unit=str(item.get("unit") or "").upper().strip()
            if unit not in {"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}:
                unit=""
            confidence=str(item.get("confidence") or "VERIFY").upper()
            if confidence not in {"HIGH","MEDIUM","LOW","VERIFY"}:
                confidence="VERIFY"
            basis=str(item.get("basis") or "")[:4000]
            source=str(item.get("source") or "")[:1500]
            # Only present usable proposals; never overwrite estimator-entered quantity.
            c.execute("""
                UPDATE estimator_items
                SET ai_quantity=?,ai_unit=?,ai_confidence=?,ai_basis=?,ai_source=?,ai_updated=?
                WHERE id=? AND company_id=? AND project_id=?
            """,(q,unit,confidence,basis,source,now,eid,current_company_id(),pid))
            saved+=1
            if q is not None and confidence in {"HIGH","MEDIUM"}:
                proposed+=1
            else:
                verify+=1
        c.commit(); c.close()

        notes=data.get("review_notes") or []
        note_html="".join(f'<div class="action">{esc(n)}</div>' for n in notes)
        body=f"""
        <div class="hero">
          <div class="eyebrow">BuildCommand Brain · Automatic Plan Takeoff · v36</div>
          <h1>Automatic takeoff pass complete.</h1>
        </div>
        <div class="grid3">
          <div class="card"><div class="label">Targets reviewed</div><div class="kpi">{saved}</div></div>
          <div class="card"><div class="label">Quantity proposals</div><div class="kpi">{proposed}</div></div>
          <div class="card"><div class="label">Needs verification</div><div class="kpi">{verify}</div></div>
        </div>
        <div class="card">
          <h2>Important</h2>
          <p>No estimator quantity was overwritten. AI results are proposals only. Review them on Takeoff Intelligence and accept only the quantities you trust.</p>
          <p><a href="/brain/takeoff"><b>Review takeoff proposals →</b></a></p>
        </div>
        {f'<div class="card"><h2>AI Review Notes</h2>{note_html}</div>' if note_html else ''}
        """
        return shell("Automatic Plan Takeoff",body)
    except Exception as exc:
        return shell("Automatic Takeoff",f'<div class="hero"><h1>Automatic takeoff did not complete.</h1></div><div class="card"><p>{esc(str(exc))}</p><p><a href="/brain/takeoff">← Back to Takeoff Intelligence</a></p></div>')
    finally:
        for fid in uploaded:
            try: client.files.delete(fid)
            except Exception: pass


@app.post("/brain/takeoff/item/{item_id}/accept-ai")
def accept_ai_takeoff(item_id:int):
    pid=project_id(); c=db()
    row=c.execute("""
        SELECT * FROM estimator_items
        WHERE id=? AND company_id=? AND project_id=?
    """,(item_id,current_company_id(),pid)).fetchone()
    if not row:
        c.close(); return HTMLResponse("Estimator item not found.",404)
    if row["ai_quantity"] is None or (row["ai_confidence"] or "") not in {"HIGH","MEDIUM"}:
        c.close()
        return shell("Takeoff Intelligence",'<div class="card"><h2>This AI quantity is not eligible for acceptance.</h2><p>Only HIGH or MEDIUM confidence numeric proposals can be accepted. Verify the item manually.</p><p><a href="/brain/takeoff">← Back</a></p></div>')
    unit=(row["ai_unit"] or row["unit"] or "EA").upper()
    c.execute("""
        UPDATE estimator_items
        SET quantity=?,unit=?,verified=0,notes=?,updated=?
        WHERE id=? AND company_id=? AND project_id=?
    """,(row["ai_quantity"],unit,
         ((row["notes"] or "")+" | AI takeoff accepted for estimator verification").strip(" |"),
         datetime.utcnow().isoformat(),item_id,current_company_id(),pid))
    c.commit(); c.close()
    return RedirectResponse("/brain/takeoff",status_code=303)

@app.get("/brain/takeoff", response_class=HTMLResponse)
def estimator_takeoff_intelligence():
    pid=project_id()
    sync=_seed_estimator_from_latest(pid)
    c=db()
    project=c.execute("SELECT name,number FROM projects WHERE id=?",(pid,)).fetchone()
    run=_latest_blueprint_run(pid)

    if run:
        rows=c.execute("""
            SELECT e.*
            FROM estimator_items e
            JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id
            WHERE e.company_id=? AND e.project_id=? AND b.run_id=?
            ORDER BY e.trade,e.id
        """,(current_company_id(),pid,run["id"])).fetchall()
    else:
        rows=[]
    c.close()

    counts={"NEEDS_TAKEOFF":0,"QUANTITY_ENTERED":0,"VERIFIED":0}
    cards=[]
    for r in rows:
        status=_takeoff_status(r)
        counts[status]+=1
        suggested=_suggest_takeoff_unit(r["trade"],r["description"])
        current_unit=(r["unit"] or "").strip()
        unit=current_unit or suggested
        badge = "READY" if status=="VERIFIED" else ("WATCH" if status=="QUANTITY_ENTERED" else "HIGH")

        cards.append(f"""
        <div class="card">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
            <div><b>{esc(r["trade"])}</b></div>
            <span class="badge {badge}">{status.replace("_"," ")}</span>
          </div>
          <h3>{esc(r["description"])}</h3>
          <div class="small">Source: {esc(r["source_ref"] or "Source not identified")}</div>
          <div class="small" style="margin-top:6px">Suggested takeoff unit: <b>{esc(suggested)}</b></div>
          {(
            f'<div class="action" style="margin-top:10px"><b>AI PLAN TAKEOFF PROPOSAL:</b> '
            + (f'{r["ai_quantity"]:g} {esc(r["ai_unit"] or suggested)}' if r["ai_quantity"] is not None else 'VERIFY / no reliable quantity')
            + f' · Confidence: {esc(r["ai_confidence"] or "VERIFY")}<br>'
            + f'<span class="small">Basis: {esc(r["ai_basis"] or "")}<br>Source: {esc(r["ai_source"] or "")}</span>'
            + (f'<form method="post" action="/brain/takeoff/item/{r["id"]}/accept-ai" style="margin-top:8px"><button type="submit">Accept AI Quantity</button></form>'
               if r["ai_quantity"] is not None and (r["ai_confidence"] or "") in ["HIGH","MEDIUM"] else '')
            + '</div>'
          ) if (r["ai_confidence"] or r["ai_basis"] or r["ai_source"]) else ''}

          <form method="post" action="/brain/takeoff/item/{r["id"]}" style="margin-top:12px">
            <div class="grid4">
              <div>
                <label>Quantity</label>
                <input name="quantity" type="number" step="0.01" value="{r["quantity"] or 0}">
              </div>
              <div>
                <label>Unit</label>
                <select name="unit">
                  {''.join(f'<option value="{u}" {"selected" if unit==u else ""}>{u}</option>' for u in ["EA","LF","SF","CY","LS","HR","DAY","TON","GAL"])}
                </select>
              </div>
              <div>
                <label>Estimator verified</label>
                <select name="verified">
                  <option value="0" {"selected" if not r["verified"] else ""}>No</option>
                  <option value="1" {"selected" if r["verified"] else ""}>Yes</option>
                </select>
              </div>
              <div>
                <label>Takeoff note</label>
                <input name="notes" value="{esc(r["notes"] or "")}" placeholder="Counted on A201, verify addendum, etc.">
              </div>
            </div>
            <button type="submit" style="margin-top:10px">Save Takeoff</button>
          </form>
        </div>
        """)

    project_label=f'{esc(project["number"])} - {esc(project["name"])}' if project else "Current Project"
    body=f"""
    <div class="hero">
      <div class="eyebrow">BuildCommand Brain · Takeoff Intelligence · v36.1</div>
      <h1>Scope → measurable takeoff targets.</h1>
      <div class="muted">{project_label}</div>
    </div>

    <div class="grid4">
      <div class="card"><div class="label">Scope Items</div><div class="kpi">{len(rows)}</div></div>
      <div class="card"><div class="label">Needs Takeoff</div><div class="kpi">{counts["NEEDS_TAKEOFF"]}</div></div>
      <div class="card"><div class="label">Qty Entered</div><div class="kpi">{counts["QUANTITY_ENTERED"]}</div></div>
      <div class="card"><div class="label">Verified</div><div class="kpi">{counts["VERIFIED"]}</div></div>
    </div>

    <div class="card">
      <h2>How BuildCommand handles takeoffs</h2>
      <p>
        BuildCommand identifies what should be counted or measured and suggests the likely unit.
        It does <b>not invent a quantity</b>. Quantities remain estimator-entered or estimator-verified
        until a future scaled-drawing takeoff engine is connected.
      </p>
      <form method="post" action="/brain/takeoff/split-components" style="margin:14px 0">
        <button type="submit">Split Mixed Scopes into Takeoff Components</button>
      </form>
      <p><a href="/brain/takeoff/components"><b>Review Takeoff Components →</b></a></p>
      <form method="post" action="/brain/takeoff/auto" style="margin:14px 0">
        <button type="submit">Run Automatic Plan Takeoff</button>
      </form>
      <p class="small">AI proposals never overwrite estimator quantities. HIGH/MEDIUM proposals must still be accepted and then estimator-verified.</p>
      <p><a href="/brain/estimator"><b>← Back to Estimator Intelligence</b></a></p>
    </div>

    {"".join(cards) if cards else '<div class="card"><h2>No takeoff items yet</h2><p>Run Plan Intake first.</p></div>'}
    """
    return shell("Takeoff Intelligence",body)


@app.post("/brain/takeoff/item/{item_id}")
def estimator_takeoff_update(
    item_id:int,
    quantity:float=Form(0),
    unit:str=Form("EA"),
    verified:int=Form(0),
    notes:str=Form("")
):
    allowed={"EA","LF","SF","CY","LS","HR","DAY","TON","GAL"}
    unit=(unit or "EA").upper()
    if unit not in allowed:
        unit="EA"

    pid=project_id()
    c=db()
    c.execute("""
        UPDATE estimator_items
        SET quantity=?,unit=?,verified=?,notes=?,updated=?
        WHERE id=? AND company_id=? AND project_id=?
    """,(quantity,unit,verified,notes,datetime.utcnow().isoformat(),
         item_id,current_company_id(),pid))
    c.commit(); c.close()
    return RedirectResponse("/brain/takeoff",status_code=303)

@app.get("/brain/estimator",response_class=HTMLResponse)
def estimator_intelligence():
    pid=project_id(); sync=_seed_estimator_from_latest(pid); c=db()
    project=c.execute("SELECT name,number FROM projects WHERE id=?",(pid,)).fetchone()
    run=_latest_blueprint_run(pid)
    if run:
        rows=c.execute("""
            SELECT e.*
            FROM estimator_items e
            JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id
            WHERE e.company_id=? AND e.project_id=? AND b.run_id=?
            ORDER BY e.trade,e.id
        """,(current_company_id(),pid,run["id"])).fetchall()
    else:
        rows=[]
    c.close()
    trade_totals={}; grand=0.0; cards=[]
    for r in rows:
        direct,total=_estimate_math(r); grand+=total
        trade_totals[r["trade"]]=trade_totals.get(r["trade"],0)+total
        verify_label="✓ VERIFIED" if int(r["verified"] or 0) else "VERIFY"
        cards.append(f"""
        <div class="card">
          <div class="small">{esc(r["trade"])} · {verify_label}</div>
          <h3>{esc(r["description"])}</h3>
          <div class="small">Source: {esc(r["source_ref"] or "Source not identified")}</div>
          <form method="post" action="/brain/estimator/item/{r["id"]}">
            <div class="grid4" style="margin-top:12px">
              <div><label>Qty</label><input name="quantity" type="number" step="0.01" value="{r["quantity"] or 0}"></div>
              <div><label>Unit</label><input name="unit" value="{esc(r["unit"] or "")}" placeholder="EA, LF, SF"></div>
              <div><label>Material / unit</label><input name="material_unit_cost" type="number" step="0.01" value="{r["material_unit_cost"] or 0}"></div>
              <div><label>Labor / unit</label><input name="labor_unit_cost" type="number" step="0.01" value="{r["labor_unit_cost"] or 0}"></div>
            </div>
            <div class="grid4" style="margin-top:10px">
              <div><label>Sub quote</label><input name="subcontract_quote" type="number" step="0.01" value="{r["subcontract_quote"] or 0}"></div>
              <div><label>Allowance</label><input name="allowance" type="number" step="0.01" value="{r["allowance"] or 0}"></div>
              <div><label>Markup %</label><input name="markup_pct" type="number" step="0.01" value="{r["markup_pct"] or 0}"></div>
              <div><label>Verified</label><select name="verified"><option value="0" {"selected" if not r["verified"] else ""}>No</option><option value="1" {"selected" if r["verified"] else ""}>Yes</option></select></div>
            </div>
            <label style="margin-top:10px">Estimator notes</label><input name="notes" value="{esc(r["notes"] or "")}">
            <div style="margin-top:10px"><b>Extended total: ${total:,.2f}</b></div>
            <button type="submit" style="margin-top:10px">Save Estimate Item</button>
          </form>
        </div>""")
    totals="".join(f"<tr><td>{esc(k)}</td><td>${v:,.2f}</td></tr>" for k,v in sorted(trade_totals.items()))
    project_label=f'{esc(project["number"])} - {esc(project["name"])}' if project else "Current Project"
    body=f"""
    <div class="hero"><div class="eyebrow">BuildCommand Brain · Estimator Intelligence · v34.2</div>
      <h1>Plans → scope → estimate.</h1><div class="muted">{project_label}</div></div>
    <div class="grid2">
      <div class="card"><div class="label">Current Estimate</div><div class="kpi">${grand:,.2f}</div>
        <div class="small">{len(rows)} source-backed scope items · {sync["added"]} new · {sync["updated"]} refreshed</div></div>
      <div class="card"><h2>Estimator Brain Review</h2>
        <form method="post" action="/brain/estimator/review">
          <textarea name="focus" placeholder="Find bid gaps, allowances, long-lead items and takeoff verification needs."></textarea>
          <button type="submit">Analyze Estimate Risk</button>
        </form></div>
    </div>
    <div class="card"><h2>Takeoff Intelligence</h2><p>Turn cleaned scope items into quantity targets and verification status.</p><p><a href="/brain/takeoff"><b>Open Takeoff Intelligence →</b></a></p></div>
    <div class="card"><h2>Trade Totals</h2><table><tr><th>Trade</th><th>Current Total</th></tr>{totals}</table></div>
    <div class="card"><h2>Estimator rule</h2><p>BuildCommand organizes and identifies takeoff targets, but quantities and costs remain <b>unverified until an estimator confirms them</b>. Source references stay attached.</p></div>
    {"".join(cards) if cards else '<div class="card"><h2>No estimator items yet</h2><p>Run Plan Intake first, then return here.</p></div>'}"""
    return shell("Estimator Intelligence",body)

@app.post("/brain/estimator/item/{item_id}")
def estimator_item_update(item_id:int,quantity:float=Form(0),unit:str=Form(""),
    material_unit_cost:float=Form(0),labor_unit_cost:float=Form(0),
    subcontract_quote:float=Form(0),allowance:float=Form(0),
    markup_pct:float=Form(0),notes:str=Form(""),verified:int=Form(0)):
    pid=project_id(); c=db()
    c.execute("""UPDATE estimator_items SET quantity=?,unit=?,material_unit_cost=?,labor_unit_cost=?,
        subcontract_quote=?,allowance=?,markup_pct=?,notes=?,verified=?,updated=?
        WHERE id=? AND company_id=? AND project_id=?""",
        (quantity,unit,material_unit_cost,labor_unit_cost,subcontract_quote,allowance,
         markup_pct,notes,verified,datetime.utcnow().isoformat(),item_id,current_company_id(),pid))
    c.commit(); c.close()
    return RedirectResponse("/brain/estimator",status_code=303)

@app.post("/brain/estimator/review",response_class=HTMLResponse)
def estimator_brain_review(focus:str=Form("")):
    pid=project_id(); _seed_estimator_from_latest(pid); c=db()
    run=_latest_blueprint_run(pid)
    if run:
        rows=c.execute("""
            SELECT e.*
            FROM estimator_items e
            JOIN blueprint_scope_items b ON b.id=e.blueprint_scope_item_id
            WHERE e.company_id=? AND e.project_id=? AND b.run_id=?
            ORDER BY e.trade,e.id
        """,(current_company_id(),pid,run["id"])).fetchall()
    else:
        rows=[]
    c.close()
    lines=[]
    for r in rows:
        direct,total=_estimate_math(r)
        lines.append(f'{r["trade"]} | {r["description"]} | source={r["source_ref"]} | '
                     f'qty={r["quantity"]} {r["unit"]} | mat={r["material_unit_cost"]} | '
                     f'labor={r["labor_unit_cost"]} | sub={r["subcontract_quote"]} | '
                     f'allowance={r["allowance"]} | markup={r["markup_pct"]}% | total={total:.2f} | '
                     f'verified={bool(r["verified"])} | notes={r["notes"]}')
    api_key=os.environ.get("OPENAI_API_KEY")
    if not api_key:
        answer="OPENAI_API_KEY is not configured."
    else:
        try:
            client=OpenAI(api_key=api_key)
            resp=client.responses.create(
                model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
                instructions="""You are BuildCommand Estimator Intelligence.
Review source-backed scope items and estimator-entered values.
Do not invent quantities or prices. Treat zero/unverified entries as missing, not as confirmed zero scope.
Identify bid gaps, missing takeoffs, missing quotes, allowances, cross-trade coordination,
scope exclusions needing confirmation, long-lead/procurement concerns, and high-risk items.
Prioritize what an estimator must verify before bid submission.""",
                input=f"""ESTIMATE WORKSHEET
{chr(10).join(lines)}

ESTIMATOR FOCUS
{focus or "Full bid-risk review"}"""
            )
            answer=resp.output_text
        except Exception as exc:
            answer=f"Estimator Intelligence could not complete review: {exc}"
    body=f"""<div class="hero"><div class="eyebrow">Estimator Intelligence</div><h1>Bid Risk Review</h1></div>
    <div class="card"><div style="white-space:pre-wrap;line-height:1.6">{esc(answer)}</div>
    <p><a href="/brain/estimator">← Back to Estimate</a></p></div>"""
    return shell("Estimator Brain Review",body)

@app.get("/plans-specs-ai",response_class=HTMLResponse)
def plans_specs_ai():
    pid=project_id(); c=db()
    docs=c.execute("SELECT * FROM attachments WHERE company_id=? AND project_id=? ORDER BY id DESC",(current_company_id(),pid)).fetchall()
    c.close()
    eligible=[d for d in docs if Path(d["original_name"] or "").suffix.lower() in {".pdf",".txt",".csv",".xlsx",".xlsm"}]
    checks="".join(f'<label style="display:flex;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.08)"><input type="checkbox" name="attachment_ids" value="{d["id"]}" style="width:auto"><span><b>{esc(d["original_name"])}</b><br><span class="small">{(int(d["size_bytes"] or 0)/1024/1024):.1f} MB</span></span></label>' for d in eligible) or '<div class="muted">No PDF/TXT/CSV/Excel project documents are uploaded yet. Use Documents first.</div>'
    run,scopes=_blueprint_latest(pid)
    latest='<div class="muted">No Blueprint Brain analysis has been run yet.</div>'
    if run:
        scope_cards="".join(f'<div class="action"><a href="/blueprint-brain/trade/{s["id"]}" style="font-weight:800">{esc(s["trade"])}</a> <span class="badge READY">{s["item_count"]} ITEMS</span><div class="small">Division {esc(s["division"] or "—")} · {esc(s["summary"] or "")}</div></div>' for s in scopes)
        flags=json.loads(run["cross_discipline_flags"] or "[]")
        rfis=json.loads(run["rfi_candidates"] or "[]")
        latest=f'<div class="small">LATEST RUN · {esc(run["created"] or "")}</div><h3>{esc(run["project_summary"] or "")}</h3><div style="margin:14px 0"><span class="badge READY">{len(scopes)} TRADES</span> <span class="badge WATCH">{len(flags)} CROSS-DISCIPLINE</span> <span class="badge HIGH">{len(rfis)} RFI CANDIDATES</span></div>{scope_cards}<p><a href="/blueprint-brain/run/{run["id"]}">Open full intelligence report →</a></p>'
    body=f'<div class="hero"><div class="eyebrow">BuildCommand Plan Intelligence</div><h1>Plans → trade scopes → field execution.</h1><div class="muted">Reads PDF plan pages visually and textually, finds cross-discipline requirements, preserves sources, generates trade scopes, and flags gaps for GC review.</div></div><div class="grid2"><div class="card"><h2>Analyze Plan Set</h2><form method="post" action="/plans-specs-ai/analyze">{checks}<label style="margin-top:16px">Analysis focus (optional)</label><textarea name="focus" placeholder="Example: Full bid/scope review, or focus on MEP coordination"></textarea><button type="submit">Run Blueprint Brain</button></form><p class="small">Selected files must total less than 50 MB. PDF page images use high-detail analysis. AI-generated scopes require superintendent/PM review before contractual use.</p></div><div class="card"><h2>Latest Blueprint Intelligence</h2>{latest}</div></div>'
    return shell("Blueprint Brain",body)


@app.post("/plans-specs-ai",response_class=HTMLResponse)
def plans_specs_ai_answer(attachment_id:int=Form(...),question:str=Form(...)):
    pid=project_id(); c=db(); d=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?",(attachment_id,current_company_id(),pid)).fetchone(); c.close()
    if not d: return HTMLResponse("Document not found",404)
    content=_attachment_text(d)
    if not content: answer=f'BuildCommand can see "{d["original_name"]}", but no readable text was extracted. Use Blueprint Brain for PDF visual analysis.'
    elif not os.environ.get("OPENAI_API_KEY"): answer="OPENAI_API_KEY is not configured."
    else:
        try:
            client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            r=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions="Answer only from the supplied construction document. Say when unsupported and never invent drawing references.",input=f"DOCUMENT:\n{content[:96000]}\nQUESTION:\n{question}")
            answer=r.output_text
        except Exception as e: answer=f"Document AI failed: {e}"
    return shell("Blueprint Brain",f'<div class="hero"><h1>{esc(d["original_name"])}</h1></div><div class="card"><h2>Answer</h2><div style="white-space:pre-wrap">{esc(answer)}</div></div>')


@app.post("/plans-specs-ai/analyze",response_class=HTMLResponse)
def blueprint_analyze(attachment_ids:list[int] | None=Form(None),focus:str=Form("")):
    pid=project_id(); company_id=current_company_id()
    if not os.environ.get("OPENAI_API_KEY"):
        return shell("Blueprint Brain",'<div class="card"><h2>OPENAI_API_KEY is not configured.</h2><p>Add it in Render Environment settings and redeploy.</p></div>')
    ids=list(dict.fromkeys(attachment_ids or []))
    if not ids:
        return shell("Blueprint Brain",'<div class="hero"><div class="eyebrow">Blueprint Brain</div><h1>Select a plan set first.</h1></div><div class="card"><p>Choose at least one PDF or supported project document, then click <b>Run Blueprint Brain</b>.</p><p><a href="/plans-specs-ai">← Back to Blueprint Brain</a></p></div>')
    c=db(); docs=[]
    for aid in ids:
        d=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?",(aid,company_id,pid)).fetchone()
        if d: docs.append(d)
    c.close()
    if not docs: return HTMLResponse("No valid project documents were selected.",404)
    total=sum(int(d["size_bytes"] or 0) for d in docs)
    if total>=50*1024*1024:
        return shell("Blueprint Brain",f'<div class="card"><h2>Plan set is too large for one analysis request.</h2><p>Selected total: {total/1024/1024:.1f} MB. Keep each Blueprint Brain batch under 50 MB, then run the remaining volumes separately.</p></div>')
    model=os.environ.get("OPENAI_MODEL","gpt-5.6")
    client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    uploaded=[]; input_content=[]; text_fallback=[]
    try:
        for d in docs:
            path=os.path.join(UPLOAD_DIR,d["stored_name"])
            if not os.path.isfile(path):
                continue
            ext=Path(d["original_name"] or "").suffix.lower()
            if ext==".pdf":
                with open(path,"rb") as fh:
                    remote=client.files.create(file=fh,purpose="user_data")
                uploaded.append(remote.id)
                input_content.append({"type":"input_file","file_id":remote.id,"detail":"high"})
            else:
                extracted=_attachment_text(d)
                if extracted:
                    text_fallback.append(f"\n--- FILE: {d['original_name']} ---\n{extracted[:120000]}")
        if not input_content and not text_fallback:
            return HTMLResponse("No readable selected files were available on the server.",400)
        names="\n".join(f"- {d['original_name']}" for d in docs)
        prompt=_blueprint_prompt(names)
        if focus.strip(): prompt += "\n\nGC ANALYSIS FOCUS:\n"+focus.strip()
        if text_fallback: prompt += "\n\nEXTRACTED NON-PDF DOCUMENT CONTENT:\n"+"\n".join(text_fallback)
        input_content.append({"type":"input_text","text":prompt})
        response=client.responses.create(model=model,input=[{"role":"user","content":input_content}])
        data=_blueprint_json(response.output_text)
        if not isinstance(data.get("trade_scopes"),list):
            raise ValueError("Blueprint Brain response did not contain trade_scopes.")
        run_id=_save_blueprint_result(pid,docs,data,model)
        return RedirectResponse(f"/blueprint-brain/run/{run_id}",status_code=303)
    except Exception as exc:
        return shell("Blueprint Brain",f'<div class="hero"><div class="eyebrow">Blueprint Brain</div><h1>Analysis did not complete.</h1></div><div class="card"><p>{esc(str(exc))}</p><p><a href="/plans-specs-ai">← Back to Blueprint Brain</a></p></div>')
    finally:
        for fid in uploaded:
            try: client.files.delete(fid)
            except Exception: pass


@app.get("/blueprint-brain/run/{run_id}",response_class=HTMLResponse)
def blueprint_run_detail(run_id:int):
    pid=project_id(); c=db(); run=c.execute("SELECT * FROM blueprint_runs WHERE id=? AND company_id=? AND project_id=?",(run_id,current_company_id(),pid)).fetchone()
    if not run: c.close(); return HTMLResponse("Blueprint analysis not found.",404)
    scopes=c.execute("SELECT * FROM blueprint_trade_scopes WHERE run_id=? AND company_id=? AND project_id=? ORDER BY trade",(run_id,current_company_id(),pid)).fetchall(); c.close()
    def _arr(field):
        try: return json.loads(run[field] or "[]")
        except Exception: return []
    files=_arr("source_files"); disciplines=_arr("detected_disciplines"); flags=_arr("cross_discipline_flags"); rfis=_arr("rfi_candidates"); notes=_arr("review_notes")
    scope_cards="".join(f'<div class="card"><div class="small">DIVISION {esc(s["division"] or "—")}</div><h2>{esc(s["trade"])}</h2><p>{esc(s["summary"] or "")}</p><span class="badge READY">{s["item_count"]} SCOPE ITEMS</span><p><a href="/blueprint-brain/trade/{s["id"]}">Open trade scope →</a></p></div>' for s in scopes)
    def list_html(items,empty): return "".join(f'<div class="action">{esc(x)}</div>' for x in items) or f'<div class="muted">{esc(empty)}</div>'
    body=f'<div class="hero"><div class="eyebrow">Blueprint Intelligence Run #{run_id}</div><h1>{esc(run["project_summary"] or "Plan set analyzed")}</h1><div class="muted">Sources: {esc(", ".join(files))}</div></div><div class="grid3"><div class="card"><div class="label">Trades</div><div class="kpi">{len(scopes)}</div></div><div class="card"><div class="label">Cross-Discipline Flags</div><div class="kpi">{len(flags)}</div></div><div class="card"><div class="label">RFI Candidates</div><div class="kpi">{len(rfis)}</div></div></div><div class="card"><h2>Detected Disciplines</h2><p>{esc(" · ".join(disciplines) or "Not identified")}</p></div><div class="grid2"><div class="card"><h2>Cross-Discipline Requirements</h2>{list_html(flags,"No cross-discipline flags returned.")}</div><div class="card"><h2>Potential Scope Gaps / RFIs</h2>{list_html(rfis,"No RFI candidates returned.")}</div></div><div class="card"><h2>GC Review Notes</h2>{list_html(notes,"No additional review notes returned.")}</div><h2>Trade Scopes</h2><div class="grid2">{scope_cards}</div><div class="card"><p class="small"><b>BuildCommand review rule:</b> AI-generated scope intelligence is a coordination aid. Verify against the complete contract documents, addenda, RFIs, subcontract agreements, and design-professional direction before issuing contractual scope.</p></div>'
    return shell("Blueprint Intelligence",body)


@app.get("/blueprint-brain/trade/{scope_id}",response_class=HTMLResponse)
def blueprint_trade_scope(scope_id:int):
    pid=project_id(); c=db(); scope=c.execute("SELECT * FROM blueprint_trade_scopes WHERE id=? AND company_id=? AND project_id=?",(scope_id,current_company_id(),pid)).fetchone()
    if not scope: c.close(); return HTMLResponse("Trade scope not found.",404)
    items=c.execute("SELECT * FROM blueprint_scope_items WHERE trade_scope_id=? AND company_id=? AND project_id=? ORDER BY id",(scope_id,current_company_id(),pid)).fetchall(); c.close()
    cards=[]
    for item in items:
        refs=[]
        if item["source_sheet"]: refs.append("Sheet "+item["source_sheet"])
        if item["source_detail"]: refs.append("Detail "+item["source_detail"])
        if item["source_spec"]: refs.append("Spec "+item["source_spec"])
        if item["source_note"]: refs.append(item["source_note"])
        conf=item["confidence"] if item["confidence"] in {"HIGH","MEDIUM","LOW"} else "WATCH"
        typ=item["item_type"] or "SCOPE"
        confidence_badge="HIGH" if conf=="HIGH" else "WATCH"
        type_badge="HIGH" if typ=="RFI_CANDIDATE" else "WATCH" if typ in ["CROSS_DISCIPLINE","COORDINATION"] else "READY"
        related=(' · <b>Related trade:</b> '+esc(item["related_trade"])) if item["related_trade"] else ''
        options=''.join(f'<option {"selected" if item["status"]==st else ""}>{st}</option>' for st in ["NOT_STARTED","IN_PROGRESS","INSPECTION_REQUIRED","COMPLETE","VERIFIED"])
        trade_options=''.join(f'<option value="{esc(t)}" {"selected" if item["trade"]==t else ""}>{esc(t)}</option>' for t in V33_TRADES)
        cards.append(f'<div class="action"><span class="badge {confidence_badge}">{esc(item["confidence"])}</span> <span class="badge {type_badge}">{esc(typ)}</span><h3>{esc(item["requirement"])}</h3><div class="small"><b>Assigned trade:</b> {esc(item["trade"])} · <b>Source:</b> {esc(" · ".join(refs) or "Source not clearly identified")}{related}</div><div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><form method="post" action="/blueprint-brain/item/{item["id"]}/status"><select name="status">{options}</select><button type="submit">Update status</button></form><form method="post" action="/blueprint-brain/item/{item["id"]}/trade"><select name="trade">{trade_options}</select><select name="learn_scope"><option>PROJECT ONLY</option><option>COMPANY STANDARD</option></select><button type="submit">Move & Learn</button></form></div></div>')
    body=f'<div class="hero"><div class="eyebrow">Division {esc(scope["division"] or "—")} · Blueprint Brain</div><h1>{esc(scope["trade"])} Scope of Work</h1><div class="muted">{esc(scope["summary"] or "")}</div></div><div class="card"><p><a href="/blueprint-brain/run/{scope["run_id"]}">← Full Blueprint Intelligence</a> · <a href="/blueprint-brain/trade/{scope_id}/export.txt">Export scope text</a></p></div><div class="card"><h2>Scope Boiler</h2><div style="white-space:pre-wrap">{esc(scope["scope_text"] or "")}</div></div><div class="card"><h2>Execution Checklist</h2>{"".join(cards) or "<div class=muted>No scope items.</div>"}</div>'
    return shell(scope["trade"]+" Scope",body)


@app.post("/blueprint-brain/item/{item_id}/status")
def blueprint_item_status(item_id:int,status:str=Form(...)):
    allowed={"NOT_STARTED","IN_PROGRESS","INSPECTION_REQUIRED","COMPLETE","VERIFIED"}; status=status.upper()
    if status not in allowed: return HTMLResponse("Invalid status",400)
    pid=project_id(); c=db(); item=c.execute("SELECT trade_scope_id FROM blueprint_scope_items WHERE id=? AND company_id=? AND project_id=?",(item_id,current_company_id(),pid)).fetchone()
    if not item: c.close(); return HTMLResponse("Scope item not found",404)
    c.execute("UPDATE blueprint_scope_items SET status=? WHERE id=? AND company_id=? AND project_id=?",(status,item_id,current_company_id(),pid)); c.commit(); scope_id=item["trade_scope_id"]; c.close(); return RedirectResponse(f"/blueprint-brain/trade/{scope_id}",status_code=303)




@app.post("/blueprint-brain/item/{item_id}/trade")
def blueprint_item_trade(item_id:int,trade:str=Form(...),learn_scope:str=Form("PROJECT ONLY")):
    trade=_v33_normalize_trade(trade)
    if trade not in V33_TRADES:
        return HTMLResponse("Invalid trade",400)
    if learn_scope not in {"PROJECT ONLY","COMPANY STANDARD"}:
        learn_scope="PROJECT ONLY"

    pid=project_id(); company_id=current_company_id(); c=db()
    item=c.execute("SELECT * FROM blueprint_scope_items WHERE id=? AND company_id=? AND project_id=?",(item_id,company_id,pid)).fetchone()
    if not item:
        c.close(); return HTMLResponse("Scope item not found",404)

    old_trade=item["trade"]; old_scope_id=item["trade_scope_id"]; run_id=item["run_id"]
    target=c.execute("SELECT * FROM blueprint_trade_scopes WHERE run_id=? AND company_id=? AND project_id=? AND trade=?",(run_id,company_id,pid,trade)).fetchone()
    if not target:
        div={"GC / General Contractor":"01","Demolition":"02","Roofing":"07",
             "Doors / Frames / Hardware":"08","Storefront / Glazing":"08",
             "Flooring":"09","Tile":"09","Painting":"09","Framing / Drywall":"09",
             "Ceilings":"09","Specialties":"10","Plumbing":"22","HVAC / Mechanical":"23",
             "Electrical":"26","Fire Sprinkler":"21","Fire Alarm":"28","Low Voltage":"27"}.get(trade,"")
        now=datetime.utcnow().isoformat()
        c.execute("INSERT INTO blueprint_trade_scopes(company_id,project_id,run_id,trade,division,summary,scope_text,item_count,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (company_id,pid,run_id,trade,div,f"BuildCommand classified scope for {trade}.","",0,now))
        target_id=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    else:
        target_id=target["id"]

    c.execute("UPDATE blueprint_scope_items SET trade=?,trade_scope_id=? WHERE id=? AND company_id=? AND project_id=?",
              (trade,target_id,item_id,company_id,pid))
    for sid in {old_scope_id,target_id}:
        count=c.execute("SELECT COUNT(*) n FROM blueprint_scope_items WHERE trade_scope_id=? AND company_id=? AND project_id=?",(sid,company_id,pid)).fetchone()["n"]
        c.execute("UPDATE blueprint_trade_scopes SET item_count=? WHERE id=? AND company_id=? AND project_id=?",(count,sid,company_id,pid))
    c.commit(); c.close()

    if old_trade != trade:
        _v43_ensure_tables()
        c=db(); now=datetime.utcnow().isoformat()
        subject=str(item["requirement"] or "")[:220]
        c.execute("""INSERT INTO learning_rules(
            company_id,project_id,rule_type,subject,learned_rule,source_ref,scope_level,
            approval_status,approved_by,confidence,created,updated
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id,pid,"TRADE ASSIGNMENT",subject,f"{old_trade} -> {trade}",
         "Blueprint manual correction",learn_scope,"APPROVED",
         str(current_user_id()),"HIGH",now,now))
        c.commit(); c.close()

    return RedirectResponse(f"/blueprint-brain/trade/{target_id}",status_code=303)


@app.get("/blueprint-brain/trade/{scope_id}/export.txt")
def blueprint_trade_export(scope_id:int):
    pid=project_id(); c=db(); scope=c.execute("SELECT * FROM blueprint_trade_scopes WHERE id=? AND company_id=? AND project_id=?",(scope_id,current_company_id(),pid)).fetchone(); c.close()
    if not scope: return HTMLResponse("Trade scope not found.",404)
    header=f"BuildCommand AI - {scope['trade']} Scope of Work\nDivision {scope['division'] or '—'}\n\n"
    footer="\n\nGC REVIEW REQUIRED: Verify this AI-generated scope against the complete contract documents, addenda, RFIs, subcontract agreement, and design-professional direction before contractual use.\n"
    filename=re.sub(r"[^A-Za-z0-9_-]+","_",scope["trade"] or "trade")+"_scope.txt"
    return Response(content=header+(scope["scope_text"] or "")+footer,media_type="text/plain",headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/schedule-import",response_class=HTMLResponse)
def schedule_import_page():
    return shell("Schedule Import",'''<div class="hero"><div class="eyebrow">Schedule Import</div><h1>Import schedule activities from CSV.</h1><div class="muted">Headers: external_id,name,trade,start,finish,pct,status</div></div><div class="card"><form method="post" action="/schedule-import" enctype="multipart/form-data"><input type="file" name="file" accept=".csv" required><button>Import Schedule</button></form></div>''')


@app.post("/schedule-import")
async def schedule_import(file:UploadFile=File(...)):
    pid=project_id(); raw=await file.read()
    try: decoded=raw.decode("utf-8-sig")
    except Exception: return HTMLResponse("CSV must be UTF-8",400)
    reader=csv.DictReader(io.StringIO(decoded)); req={"external_id","name","trade","start","finish","pct","status"}
    if not req.issubset(set(reader.fieldnames or [])): return HTMLResponse("CSV missing required headers",400)
    rows=list(reader); c=db(); imported=0
    for row in rows:
        ext=(row.get("external_id") or "").strip(); name=(row.get("name") or "").strip()
        if not ext or not name: continue
        trade=(row.get("trade") or "").strip(); start=(row.get("start") or "").strip(); finish=(row.get("finish") or "").strip(); pct=float(row.get("pct") or 0); status=(row.get("status") or "NOT_STARTED").strip().upper()
        ex=c.execute("SELECT id FROM activities WHERE project_id=? AND external_id=?",(pid,ext)).fetchone()
        if ex:
            c.execute("UPDATE activities SET name=?,trade=?,start=?,finish=?,pct=?,status=? WHERE id=? AND project_id=?",(name,trade,start,finish,pct,status,ex["id"],pid))
        else:
            c.execute("INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,?,?)",(pid,ext,name,trade,start,finish,pct,status))
        imported+=1
    c.execute("INSERT INTO schedule_import_batches(company_id,project_id,file_name,row_count,imported_count,created) VALUES(?,?,?,?,?,?)",(current_company_id(),pid,safe_filename(file.filename),len(rows),imported,datetime.utcnow().isoformat()))
    c.commit(); c.close()
    return RedirectResponse("/schedule",303)




def ensure_v29_tables():
    c=db()
    c.execute("CREATE TABLE IF NOT EXISTS photo_observations(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,attachment_id INTEGER,activity_id INTEGER,observation TEXT,severity TEXT DEFAULT 'WATCH',created TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS weather_impacts(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,activity_id INTEGER,impact_date TEXT,weather_type TEXT,lost_hours REAL DEFAULT 0,description TEXT,created TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS schedule_import_batches(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,file_name TEXT,row_count INTEGER DEFAULT 0,imported_count INTEGER DEFAULT 0,created TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS communication_drafts(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,sub_id INTEGER,draft_type TEXT,subject TEXT,body TEXT,status TEXT DEFAULT 'DRAFT',created TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS meeting_ai_summaries(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,project_id INTEGER NOT NULL,source_text TEXT,summary_text TEXT,created TEXT)")
    c.commit(); c.close()

@app.get("/photo-intelligence",response_class=HTMLResponse)
def photo_intelligence():
    ensure_v29_tables()
    pid=project_id(); c=db()
    photos=c.execute(
        """
        SELECT *
        FROM attachments
        WHERE company_id=? AND project_id=?
          AND (
            lower(COALESCE(mime_type,'')) LIKE ?
            OR lower(COALESCE(original_name,'')) LIKE ?
            OR lower(COALESCE(original_name,'')) LIKE ?
            OR lower(COALESCE(original_name,'')) LIKE ?
            OR lower(COALESCE(original_name,'')) LIKE ?
          )
        ORDER BY id DESC
        """,
        (
            current_company_id(),
            pid,
            "image/%",
            "%.jpg",
            "%.jpeg",
            "%.png",
            "%.webp"
        )
    ).fetchall()
    obs=c.execute("SELECT p.*,a.original_name FROM photo_observations p LEFT JOIN attachments a ON a.id=p.attachment_id WHERE p.company_id=? AND p.project_id=? ORDER BY p.id DESC LIMIT 25",(current_company_id(),pid)).fetchall()
    c.close()
    opts="".join(f'<option value="{p["id"]}">{esc(p["original_name"])}</option>' for p in photos) or '<option value="">No photos uploaded</option>'
    recent="".join(f'<div class="action"><span class="badge {o["severity"]}">{esc(o["severity"])}</span> <b>{esc(o["original_name"] or "Photo")}</b><div>{esc(o["observation"])}</div></div>' for o in obs) or '<div class="muted">No observations yet.</div>'
    return shell("Photo Intelligence",f'''<div class="hero"><div class="eyebrow">Field Photo Intelligence</div><h1>Document photo observations.</h1></div><div class="grid2"><div class="card"><form method="post" action="/photo-intelligence"><select name="attachment_id">{opts}</select><textarea name="observation" required></textarea><select name="severity"><option>LOW</option><option selected>WATCH</option><option>HIGH</option><option>CRITICAL</option></select><button>Save Observation</button></form></div><div class="card">{recent}</div></div>''')


@app.post("/photo-intelligence")
def photo_intelligence_save(attachment_id:int=Form(...),observation:str=Form(...),severity:str=Form("WATCH")):
    ensure_v29_tables()
    pid=project_id(); c=db()
    c.execute("INSERT INTO photo_observations(company_id,project_id,attachment_id,activity_id,observation,severity,created) VALUES(?,?,?,NULL,?,?,?)",(current_company_id(),pid,attachment_id,observation.strip(),severity,datetime.utcnow().isoformat()))
    c.commit(); c.close(); return RedirectResponse("/photo-intelligence",303)


@app.get("/rfi-drafting",response_class=HTMLResponse)
def rfi_drafting():
    return shell("RFI Drafting",'''<div class="hero"><div class="eyebrow">AI RFI Drafting</div><h1>Turn a field issue into a professional RFI.</h1></div><div class="card"><form method="post" action="/rfi-drafting"><textarea name="field_issue" required></textarea><button>Draft RFI</button></form></div>''')


@app.post("/rfi-drafting",response_class=HTMLResponse)
def rfi_drafting_generate(field_issue:str=Form(...)):
    context=build_project_context(project_id())
    if os.environ.get("OPENAI_API_KEY"):
        try:
            client=OpenAI(api_key=os.environ["OPENAI_API_KEY"]); r=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions="Draft a professional construction RFI. Return SUBJECT, BACKGROUND, QUESTION, POTENTIAL IMPACT. Do not invent drawing/spec numbers, dates or costs.",input=f"{context}\nFIELD ISSUE:\n{field_issue}"); draft=r.output_text
        except Exception as e: draft=f"AI RFI drafting failed: {e}"
    else: draft="OPENAI_API_KEY is not configured."
    return shell("RFI Drafting",f'''<div class="hero"><h1>Draft Ready</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(draft)}</div></div><div class="card"><form method="post" action="/rfi-drafting/save"><input type="hidden" name="title" value="{esc(field_issue[:120])}"><textarea name="description">{esc(draft)}</textarea><button>Save to RFIs</button></form></div>''')


@app.post("/rfi-drafting/save")
def rfi_drafting_save(title:str=Form(...),description:str=Form(...)):
    pid=project_id(); c=db()
    c.execute("INSERT INTO project_issues(project_id,activity_id,issue_type,title,owner,due,priority,status,description,response,created) VALUES(?,NULL,'RFI',?,'',?,'WATCH','OPEN',?,'',?)",(pid,title.strip(),date.today().isoformat(),description.strip(),date.today().isoformat()))
    c.commit(); c.close(); return RedirectResponse("/issues",303)


@app.get("/sub-communications",response_class=HTMLResponse)
def sub_communications():
    pid=project_id(); c=db(); subs=c.execute("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(pid,)).fetchall(); drafts=c.execute("SELECT d.*,s.name sub_name FROM communication_drafts d LEFT JOIN subs s ON s.id=d.sub_id WHERE d.company_id=? AND d.project_id=? ORDER BY d.id DESC LIMIT 20",(current_company_id(),pid)).fetchall(); c.close()
    opts="".join(f'<option value="{s["id"]}">{esc(s["name"])} - {esc(s["trade"])}</option>' for s in subs)
    recent="".join(f'<div class="action"><b>{esc(d["draft_type"])}</b> · {esc(d["sub_name"] or "")}<div>{esc(d["subject"])}</div></div>' for d in drafts) or '<div class="muted">No drafts yet.</div>'
    return shell("Sub Communications",f'''<div class="hero"><div class="eyebrow">Subcontractor Communications</div><h1>Generate field follow-ups.</h1></div><div class="grid2"><div class="card"><form method="post" action="/sub-communications"><select name="sub_id">{opts}</select><select name="draft_type"><option>MANPOWER_REQUEST</option><option>DELAY_NOTICE</option><option>COORDINATION</option><option>FOLLOW_UP</option></select><textarea name="prompt" required></textarea><button>Generate Draft</button></form></div><div class="card">{recent}</div></div>''')


@app.post("/sub-communications",response_class=HTMLResponse)
def sub_communications_generate(sub_id:int=Form(...),draft_type:str=Form(...),prompt:str=Form(...)):
    pid=project_id(); c=db(); sub=c.execute("SELECT * FROM subs WHERE id=? AND project_id=?",(sub_id,pid)).fetchone(); c.close()
    if os.environ.get("OPENAI_API_KEY") and sub:
        try:
            client=OpenAI(api_key=os.environ["OPENAI_API_KEY"]); r=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions="Write a concise professional subcontractor communication. Firm, collaborative, no invented dates or contract terms.",input=f"SUB: {sub['name']} {sub['trade']}\nTYPE: {draft_type}\nNEED: {prompt}"); body=r.output_text
        except Exception as e: body=f"AI communication failed: {e}"
    else: body=prompt
    subject=f"{draft_type.replace('_',' ').title()} - {sub['name'] if sub else 'Subcontractor'}"
    c=db(); c.execute("INSERT INTO communication_drafts(company_id,project_id,sub_id,draft_type,subject,body,status,created) VALUES(?,?,?,?,?,?,'DRAFT',?)",(current_company_id(),pid,sub_id,draft_type,subject,body,datetime.utcnow().isoformat())); c.commit(); c.close()
    return shell("Sub Communications",f'<div class="hero"><h1>{esc(subject)}</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(body)}</div></div>')


@app.get("/meeting-minutes-ai",response_class=HTMLResponse)
def meeting_minutes_ai():
    return shell("AI Meeting Minutes",'''<div class="hero"><div class="eyebrow">Meeting Minutes AI</div><h1>Turn raw notes into decisions and actions.</h1></div><div class="card"><form method="post" action="/meeting-minutes-ai"><textarea name="source_text" required style="min-height:250px"></textarea><button>Generate Minutes</button></form></div>''')


@app.post("/meeting-minutes-ai",response_class=HTMLResponse)
def meeting_minutes_ai_generate(source_text:str=Form(...)):
    pid=project_id()
    if os.environ.get("OPENAI_API_KEY"):
        try:
            client=OpenAI(api_key=os.environ["OPENAI_API_KEY"]); r=client.responses.create(model=os.environ.get("OPENAI_MODEL","gpt-5.6"),instructions="Convert construction meeting notes into DECISIONS, COMMITMENTS, ACTION ITEMS, RISKS/ISSUES, FOLLOW-UP. Do not invent owners or dates.",input=source_text); summary=r.output_text
        except Exception as e: summary=f"AI minutes failed: {e}"
    else: summary=source_text
    c=db(); c.execute("INSERT INTO meeting_ai_summaries(company_id,project_id,source_text,summary_text,created) VALUES(?,?,?,?,?)",(current_company_id(),pid,source_text,summary,datetime.utcnow().isoformat())); c.commit(); c.close()
    return shell("AI Meeting Minutes",f'''<div class="hero"><h1>Minutes Ready</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(summary)}</div></div><div class="card"><form method="post" action="/meeting-minutes-ai/save"><textarea name="summary">{esc(summary)}</textarea><button>Save to Meetings</button></form></div>''')


@app.post("/meeting-minutes-ai/save")
def meeting_minutes_ai_save(summary:str=Form(...)):
    pid=project_id(); c=db(); c.execute("INSERT INTO meeting_notes(project_id,meeting_date,meeting_type,title,attendees,decisions,commitments,follow_up,created) VALUES(?,?,'COORDINATION','AI Meeting Minutes','',?,'','',?)",(pid,date.today().isoformat(),summary.strip(),date.today().isoformat())); c.commit(); c.close(); return RedirectResponse("/meetings",303)


@app.get("/weather-impacts",response_class=HTMLResponse)
def weather_impacts():
    pid=project_id(); c=db(); acts=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); rows=c.execute("SELECT w.*,a.external_id,a.name activity FROM weather_impacts w LEFT JOIN activities a ON a.id=w.activity_id WHERE w.company_id=? AND w.project_id=? ORDER BY w.impact_date DESC",(current_company_id(),pid)).fetchall(); c.close()
    opts='<option value="">No linked activity</option>'+''.join(f'<option value="{a["id"]}">{esc(a["external_id"])} - {esc(a["name"])}</option>' for a in acts)
    hist=''.join(f'<div class="action"><b>{esc(r["impact_date"])} · {esc(r["weather_type"])}</b><div>{r["lost_hours"] or 0} lost hour(s) · {esc(r["external_id"] or "")} {esc(r["activity"] or "")}</div><div class="small">{esc(r["description"])}</div></div>' for r in rows) or '<div class="muted">No impacts logged.</div>'
    return shell("Weather Impacts",f'''<div class="hero"><div class="eyebrow">Weather Impact Log</div><h1>Document weather-related delay.</h1></div><div class="grid2"><div class="card"><form method="post" action="/weather-impacts"><input type="date" name="impact_date" value="{date.today().isoformat()}" required><select name="weather_type"><option>RAIN</option><option>HEAT</option><option>WIND</option><option>LIGHTNING</option><option>COLD</option><option>OTHER</option></select><select name="activity_id">{opts}</select><input type="number" step="0.5" min="0" name="lost_hours" value="0"><textarea name="description"></textarea><button>Log Impact</button></form></div><div class="card">{hist}</div></div>''')


@app.post("/weather-impacts")
def weather_impacts_save(impact_date:str=Form(...),weather_type:str=Form(...),activity_id:str=Form(""),lost_hours:float=Form(0),description:str=Form("")):
    pid=project_id(); linked=int(activity_id) if str(activity_id).strip() else None; c=db(); c.execute("INSERT INTO weather_impacts(company_id,project_id,activity_id,impact_date,weather_type,lost_hours,description,created) VALUES(?,?,?,?,?,?,?,?)",(current_company_id(),pid,linked,impact_date,weather_type,max(0,lost_hours),description.strip(),datetime.utcnow().isoformat())); c.commit(); c.close(); return RedirectResponse("/weather-impacts",303)


def _simple_pdf(title,lines,filename):
    if canvas is None: return HTMLResponse("reportlab is not installed",500)
    buf=io.BytesIO(); pdf=canvas.Canvas(buf); y=744; pdf.setFont("Helvetica-Bold",16); pdf.drawString(42,y,title); y-=28; pdf.setFont("Helvetica",9)
    for line in lines:
        words=str(line).split(); cur=""
        for word in words:
            test=(cur+" "+word).strip()
            if pdf.stringWidth(test,"Helvetica",9)>520:
                pdf.drawString(48,y,cur); y-=12; cur=word
                if y<50: pdf.showPage(); y=744; pdf.setFont("Helvetica",9)
            else: cur=test
        if cur: pdf.drawString(48,y,cur); y-=14
    pdf.setFont("Helvetica",8); pdf.drawString(42,28,"BuildCommand AI · Built by Wilson LaHood"); pdf.save(); buf.seek(0)
    return Response(buf.getvalue(),media_type="application/pdf",headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/pdf-reports",response_class=HTMLResponse)
def pdf_reports():
    return shell("PDF Reports",'''<div class="hero"><div class="eyebrow">Professional Reports</div><h1>Export project reports.</h1></div><div class="grid2"><div class="card"><a href="/pdf-reports/project-health.pdf">Project Health PDF</a></div><div class="card"><a href="/pdf-reports/lookahead.pdf">3-Week Lookahead PDF</a></div><div class="card"><a href="/pdf-reports/weekly.pdf">Weekly AI Report PDF</a></div><div class="card"><a href="/exports/daily-report.pdf">Daily Report PDF</a></div></div>''')


@app.get("/pdf-reports/project-health.pdf")
def pdf_health():
    h=project_health_snapshot(project_id()); return _simple_pdf("BuildCommand AI - Project Health",[f'Overall: {h["overall"]}/100',f'Schedule: {h["schedule"]}',f'Readiness: {h["readiness"]}',f'Procurement: {h["procurement"]}',f'Risk: {h["risk"]}',f'Field: {h["field"]}'],"buildcommand_project_health.pdf")


@app.get("/pdf-reports/lookahead.pdf")
def pdf_lookahead():
    pid=project_id(); today=date.today(); horizon=today+timedelta(days=21); c=db(); acts=c.execute("SELECT * FROM activities WHERE project_id=? ORDER BY start",(pid,)).fetchall(); c.close(); lines=[f"Window: {today} through {horizon}"]
    for a in acts:
        s=parse_iso_date(a["start"])
        if s and today<=s<=horizon: lines.append(f'{a["external_id"]} - {a["name"]} | {a["trade"]} | {a["start"]} to {a["finish"]} | {a["pct"] or 0}%')
    return _simple_pdf("BuildCommand AI - 3 Week Lookahead",lines,"buildcommand_lookahead.pdf")


@app.get("/pdf-reports/weekly.pdf")
def pdf_weekly():
    pid=project_id(); c=db(); r=c.execute("SELECT * FROM weekly_ai_reports WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 1",(current_company_id(),pid)).fetchone(); c.close()
    if not r: return HTMLResponse("Generate a Weekly AI Report first.",404)
    return _simple_pdf("BuildCommand AI - Weekly Project Report",(r["report_text"] or "").splitlines(),"buildcommand_weekly_report.pdf")


@app.get("/cost-intelligence",response_class=HTMLResponse)
def cost_intelligence():
    pid=project_id(); c=db(); rows=c.execute("SELECT ce.*,a.external_id,a.name activity FROM change_events ce LEFT JOIN activities a ON a.id=ce.activity_id WHERE ce.project_id=? ORDER BY ce.id DESC",(pid,)).fetchall(); c.close()
    open_rows=[r for r in rows if r["status"] not in ["APPROVED","REJECTED"]]; approved=[r for r in rows if r["status"]=="APPROVED"]; open_cost=sum(float(r["estimated_cost"] or 0) for r in open_rows); approved_cost=sum(float(r["estimated_cost"] or 0) for r in approved); days=sum(float(r["schedule_days"] or 0) for r in open_rows)
    cards=''.join(f'<div class="card"><span class="badge {"READY" if r["status"]=="APPROVED" else "WATCH"}">{esc(r["status"])}</span><h3>{esc(r["title"])}</h3><p>${(r["estimated_cost"] or 0):,.0f} · {(r["schedule_days"] or 0):.1f} day(s)</p></div>' for r in rows) or '<div class="card">No change events.</div>'
    return shell("Cost Intelligence",f'''<div class="hero"><div class="eyebrow">Cost & Change Intelligence</div><h1>Change exposure in dollars and days.</h1></div><div class="grid3"><div class="card"><div class="label">Open Exposure</div><div class="kpi">${open_cost:,.0f}</div></div><div class="card"><div class="label">Approved</div><div class="kpi">${approved_cost:,.0f}</div></div><div class="card"><div class="label">Open Days</div><div class="kpi">{days:.1f}</div></div></div><div class="grid2">{cards}</div>''')


@app.get("/mobile-home",response_class=HTMLResponse)
def mobile_home():
    h=project_health_snapshot(project_id())
    return shell("Mobile Home",f'''<div class="hero"><div class="eyebrow">Superintendent Mobile</div><h1>Today in the Field</h1><div class="kpi">{h["overall"]}/100</div></div><div class="grid2"><div class="card"><a href="/quick-entry">🎤 Speak / Quick Note</a></div><div class="card"><a href="/documents">📷 Add Photo / Document</a></div><div class="card"><a href="/daily-report">📝 Daily Report</a></div><div class="card"><a href="/issues">⚠️ Issue / RFI</a></div><div class="card"><a href="/ai-command">📌 View Today</a></div><div class="card"><a href="/assistant">🤖 AI Assistant</a></div></div>''')

def v30_extract_text(row):
    if not row:
        return ""
    path=os.path.join(UPLOAD_DIR,row["stored_name"])
    ext=Path(row["original_name"] or "").suffix.lower()
    if not os.path.isfile(path):
        return ""
    try:
        if ext in {".txt",".csv"}:
            return Path(path).read_text(errors="ignore")[:250000]
        if ext==".pdf" and PdfReader is not None:
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages[:200])[:300000]
        if ext in {".xlsx",".xlsm"} and openpyxl is not None:
            wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
            lines=[]
            for ws in wb.worksheets[:20]:
                lines.append("SHEET: "+ws.title)
                for vals in ws.iter_rows(values_only=True):
                    lines.append(" | ".join("" if v is None else str(v) for v in vals))
                    if len(lines)>20000:
                        break
            return "\n".join(lines)[:300000]
    except Exception:
        return ""
    return ""

@app.get("/document-ai",response_class=HTMLResponse)
def v30_document_ai_page():
    pid=project_id(); c=db()
    docs=c.execute("SELECT * FROM attachments WHERE company_id=? AND project_id=? ORDER BY id DESC LIMIT 100",(current_company_id(),pid)).fetchall()
    c.close()
    options="".join(f'<option value="{d["id"]}">{esc(d["original_name"])}</option>' for d in docs) or '<option value="">No uploaded documents</option>'
    return shell("Deep Document AI",f"""
    <div class="hero"><div class="eyebrow">Deep Plans & Specs AI</div><h1>Read PDF, Excel, TXT, and CSV project documents.</h1></div>
    <div class="card"><form method="post" action="/document-ai"><select name="attachment_id">{options}</select>
    <textarea name="question" required placeholder="Ask a question about this document"></textarea>
    <button type="submit">Analyze Document</button></form></div>""")

@app.post("/document-ai",response_class=HTMLResponse)
def v30_document_ai_answer(attachment_id:int=Form(...),question:str=Form(...)):
    pid=project_id(); c=db()
    doc=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?",(attachment_id,current_company_id(),pid)).fetchone()
    c.close()
    if not doc:
        return HTMLResponse("Document not found.",status_code=404)
    extracted=v30_extract_text(doc)
    if not extracted:
        answer="No readable text could be extracted from this file."
    else:
        try:
            client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            response=client.responses.create(
                model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
                instructions="Answer only from the supplied construction document text. Do not invent drawing numbers or spec sections.",
                input=f"DOCUMENT: {doc['original_name']}\n\n{extracted[:96000]}\n\nQUESTION: {question}"
            )
            answer=response.output_text
        except Exception as exc:
            answer=f"Document AI failed: {exc}"
    return shell("Deep Document AI",f"""<div class="hero"><h1>{esc(doc["original_name"])}</h1></div>
    <div class="card"><div style="white-space:pre-wrap">{esc(answer)}</div></div>""")

@app.get("/advanced-schedule-import",response_class=HTMLResponse)
def v30_schedule_import_page():
    return shell("Advanced Schedule Import","""
    <div class="hero"><div class="eyebrow">Advanced Schedule Import</div><h1>Import CSV or Excel schedules.</h1></div>
    <div class="card"><form method="post" action="/advanced-schedule-import" enctype="multipart/form-data">
    <input type="file" name="file" accept=".csv,.xlsx" required><button type="submit">Import Schedule</button></form></div>""")

@app.post("/advanced-schedule-import")
async def v30_schedule_import(file:UploadFile=File(...)):
    pid=project_id(); filename=safe_filename(file.filename); ext=Path(filename).suffix.lower(); raw=await file.read(); rows=[]
    if ext==".csv":
        rows=list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    elif ext==".xlsx":
        if openpyxl is None:
            return HTMLResponse("Excel support is unavailable.",status_code=500)
        tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".xlsx"); tmp.write(raw); tmp.close()
        try:
            wb=openpyxl.load_workbook(tmp.name,read_only=True,data_only=True); ws=wb[wb.sheetnames[0]]
            vals=list(ws.iter_rows(values_only=True))
            if vals:
                headers=[str(v or "").strip() for v in vals[0]]
                rows=[{headers[i]:r[i] for i in range(min(len(headers),len(r)))} for r in vals[1:]]
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass
    else:
        return HTMLResponse("Use CSV or XLSX.",status_code=400)
    required={"external_id","name","trade","start","finish","pct","status"}
    if rows and not required.issubset(set(rows[0].keys())):
        return HTMLResponse("Schedule file is missing required headers.",status_code=400)
    c=db(); imported=0
    for row in rows:
        eid=str(row.get("external_id") or "").strip(); name=str(row.get("name") or "").strip()
        if not eid or not name: continue
        existing=c.execute("SELECT id FROM activities WHERE project_id=? AND external_id=?",(pid,eid)).fetchone()
        trade=str(row.get("trade") or "").strip(); start=str(row.get("start") or "").split(" ")[0]; finish=str(row.get("finish") or "").split(" ")[0]
        try: pct=float(row.get("pct") or 0)
        except Exception: pct=0
        status=str(row.get("status") or "NOT_STARTED").strip().upper()
        if existing:
            c.execute("UPDATE activities SET name=?,trade=?,start=?,finish=?,pct=?,status=? WHERE id=? AND project_id=?",(name,trade,start,finish,pct,status,existing["id"],pid))
        else:
            c.execute("INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,?,?)",(pid,eid,name,trade,start,finish,pct,status))
        imported+=1
    c.execute("INSERT INTO schedule_import_sources(company_id,project_id,source_type,file_name,imported_count,created) VALUES(?,?,?,?,?,?)",(current_company_id(),pid,ext.replace(".","").upper(),filename,imported,datetime.utcnow().isoformat()))
    c.commit(); c.close()
    return RedirectResponse("/schedule",status_code=303)

@app.get("/photo-ai",response_class=HTMLResponse)
def v30_photo_ai_page():
    pid=project_id(); c=db()
    photos=c.execute("""SELECT * FROM attachments WHERE company_id=? AND project_id=? AND
    (lower(COALESCE(mime_type,'')) LIKE ? OR lower(COALESCE(original_name,'')) LIKE ? OR lower(COALESCE(original_name,'')) LIKE ? OR lower(COALESCE(original_name,'')) LIKE ? OR lower(COALESCE(original_name,'')) LIKE ?)
    ORDER BY id DESC LIMIT 50""",(current_company_id(),pid,"image/%","%.jpg","%.jpeg","%.png","%.webp")).fetchall(); c.close()
    options="".join(f'<option value="{p["id"]}">{esc(p["original_name"])}</option>' for p in photos) or '<option value="">No photos uploaded</option>'
    return shell("AI Photo Analysis",f"""<div class="hero"><div class="eyebrow">AI Photo Analysis</div><h1>Analyze field photos.</h1></div>
    <div class="card"><form method="post" action="/photo-ai"><select name="attachment_id">{options}</select>
    <select name="focus"><option>GENERAL</option><option>SAFETY</option><option>QUALITY</option><option>PROGRESS</option></select>
    <button type="submit">Analyze Photo</button></form></div>""")

@app.post("/photo-ai",response_class=HTMLResponse)
def v30_photo_ai(attachment_id:int=Form(...),focus:str=Form("GENERAL")):
    pid=project_id(); c=db()
    row=c.execute("SELECT * FROM attachments WHERE id=? AND company_id=? AND project_id=?",(attachment_id,current_company_id(),pid)).fetchone(); c.close()
    if not row: return HTMLResponse("Photo not found.",status_code=404)
    path=os.path.join(UPLOAD_DIR,row["stored_name"])
    if not os.path.isfile(path): return HTMLResponse("Stored photo unavailable.",status_code=404)
    try:
        data=Path(path).read_bytes(); mime=row["mime_type"] or "image/jpeg"; b64=base64.b64encode(data).decode("ascii")
        client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response=client.responses.create(
            model=os.environ.get("OPENAI_VISION_MODEL",os.environ.get("OPENAI_MODEL","gpt-5.6")),
            instructions=f"Describe only visible construction facts. Focus: {focus}. Treat possible concerns as observations, not definitive code violations.",
            input=[{"role":"user","content":[{"type":"input_text","text":"Analyze this construction field photo."},{"type":"input_image","image_url":f"data:{mime};base64,{b64}"}]}]
        )
        result=response.output_text
    except Exception as exc:
        result=f"Photo AI failed: {exc}"
    return shell("AI Photo Analysis",f"""<div class="hero"><h1>{esc(row["original_name"])}</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(result)}</div></div>""")

@app.get("/auto-daily-report",response_class=HTMLResponse)
def v30_auto_daily_page():
    return shell("Auto Daily Report","""<div class="hero"><div class="eyebrow">Automatic Daily Report</div><h1>Generate today's report from BuildCommand activity.</h1></div>
    <div class="card"><form method="post" action="/auto-daily-report/generate"><button type="submit">Generate Draft Daily Report</button></form></div>""")

@app.post("/auto-daily-report/generate",response_class=HTMLResponse)
def v30_auto_daily_generate():
    context=build_project_context(project_id())
    try:
        client=OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response=client.responses.create(
            model=os.environ.get("OPENAI_MODEL","gpt-5.6"),
            instructions="Draft a superintendent daily report using only supplied project data. Sections: WORK COMPLETED, MANPOWER, DELAYS/CONSTRAINTS, DELIVERIES, INSPECTIONS, SAFETY, TOMORROW PLAN. Say Not documented when missing.",
            input=context
        )
        draft=response.output_text
    except Exception as exc: draft=f"Auto daily report failed: {exc}"
    return shell("Auto Daily Report",f"""<div class="hero"><h1>Draft Ready</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(draft)}</div></div>""")

def v30_forecast(pid):
    c=db(); acts=c.execute("SELECT * FROM activities WHERE project_id=? AND status!='COMPLETE'",(pid,)).fetchall(); c.close()
    if not acts: return 0.0,.5,"No active activities."
    scores=[]; notes=[]
    for a in acts:
        score,band,reasons=activity_delay_signal(a); scores.append(score)
        if score>=45: notes.append(f'{a["external_id"]} {a["name"]}: {band}')
    avg=sum(scores)/len(scores)
    return round(avg/18.0,1),round(min(.9,.45+len(acts)*.03),2),"; ".join(notes[:5]) or "No major delay signal detected."

@app.get("/predictive-forecast",response_class=HTMLResponse)
def v30_predictive_forecast():
    pid=project_id(); h=project_health_snapshot(pid); projected,confidence,explanation=v30_forecast(pid)
    c=db(); c.execute("INSERT INTO forecast_snapshots(company_id,project_id,snapshot_date,health_score,projected_delay_days,confidence,explanation,created) VALUES(?,?,?,?,?,?,?,?)",
    (current_company_id(),pid,date.today().isoformat(),h["overall"],projected,confidence,explanation,datetime.utcnow().isoformat())); c.commit(); c.close()
    band="READY" if projected<1 else "WATCH" if projected<3 else "HIGH" if projected<6 else "CRITICAL"
    return shell("Predictive Forecast",f"""<div class="hero"><div class="eyebrow">Predictive Forecast</div><h1>{projected:.1f} projected delay day(s)</h1>
    <span class="badge {band}">{band}</span><div class="muted">Confidence {confidence*100:.0f}%</div></div><div class="card"><p>{esc(explanation)}</p></div>""")

@app.get("/sub-risk",response_class=HTMLResponse)
def v30_sub_risk():
    pid=project_id(); c=db(); subs=c.execute("SELECT * FROM subs WHERE project_id=? ORDER BY trade,name",(pid,)).fetchall()
    updates=c.execute("SELECT * FROM subcontractor_updates WHERE project_id=? ORDER BY update_date DESC,id DESC",(pid,)).fetchall(); c.close()
    cards=""
    for s in subs:
        su=[u for u in updates if u["sub_id"]==s["id"]]; latest=su[0] if su else None; score=20; reasons=[]
        if not latest: score+=35; reasons.append("no recent field update")
        else:
            st=(latest["status"] or "").upper()
            if st=="CRITICAL": score+=55; reasons.append("latest status critical")
            elif st=="HIGH": score+=35; reasons.append("latest status high")
            elif st=="WATCH": score+=18; reasons.append("latest status watch")
            if (latest["manpower"] or 0)<=0: score+=20; reasons.append("latest manpower zero")
        score=min(100,score); band="CRITICAL" if score>=75 else "HIGH" if score>=50 else "WATCH" if score>=25 else "READY"; explanation="; ".join(reasons) or "No major risk signal."
        c2=db(); c2.execute("INSERT INTO sub_risk_snapshots(company_id,project_id,sub_id,risk_score,risk_band,explanation,created) VALUES(?,?,?,?,?,?,?)",(current_company_id(),pid,s["id"],score,band,explanation,datetime.utcnow().isoformat())); c2.commit(); c2.close()
        cards+=f'<div class="card"><span class="badge {band}">{band} · {score}</span><h3>{esc(s["name"])}</h3><p>{esc(explanation)}</p></div>'
    return shell("Sub Risk",f'<div class="hero"><h1>Subcontractor Risk</h1></div><div class="grid3">{cards}</div>')

@app.get("/change-package",response_class=HTMLResponse)
def v30_change_package_page():
    pid=project_id(); c=db(); rows=c.execute("SELECT * FROM change_events WHERE project_id=? ORDER BY id DESC",(pid,)).fetchall(); c.close()
    options="".join(f'<option value="{r["id"]}">{esc(r["title"])}</option>' for r in rows)
    return shell("Change Package",f"""<div class="hero"><div class="eyebrow">Change Order Documentation</div><h1>Build a change package.</h1></div>
    <div class="card"><form method="post" action="/change-package"><select name="change_event_id">{options}</select><button type="submit">Generate Change Package</button></form></div>""")

@app.post("/change-package",response_class=HTMLResponse)
def v30_change_package(change_event_id:int=Form(...)):
    pid=project_id(); c=db()
    ce=c.execute("""SELECT ce.*,a.external_id,a.name activity FROM change_events ce LEFT JOIN activities a ON a.id=ce.activity_id WHERE ce.id=? AND ce.project_id=?""",(change_event_id,pid)).fetchone(); c.close()
    if not ce: return HTMLResponse("Change event not found.",status_code=404)
    package="\n".join([
        "CHANGE EVENT", str(ce["title"] or ""), "",
        "TYPE", str(ce["event_type"] or ""), "",
        "LINKED ACTIVITY", f'{ce["external_id"] or ""} {ce["activity"] or ""}', "",
        "RESPONSIBLE PARTY", str(ce["responsible_party"] or "Unassigned"), "",
        "ESTIMATED COST IMPACT", f'${float(ce["estimated_cost"] or 0):,.0f}', "",
        "SCHEDULE IMPACT", f'{float(ce["schedule_days"] or 0):.1f} day(s)', "",
        "STATUS", str(ce["status"] or ""), "",
        "DESCRIPTION", str(ce["description"] or ""), "",
        "SUPPORTING DOCUMENTS", "Attach photos, RFIs, submittals, meeting notes, and pricing as applicable."
    ])
    c=db(); c.execute("INSERT INTO change_packages(company_id,project_id,change_event_id,package_text,created) VALUES(?,?,?,?,?)",(current_company_id(),pid,change_event_id,package,datetime.utcnow().isoformat())); c.commit(); c.close()
    return shell("Change Package",f'<div class="hero"><h1>{esc(ce["title"])}</h1></div><div class="card"><div style="white-space:pre-wrap">{esc(package)}</div></div>')

@app.get("/owner-dashboard",response_class=HTMLResponse)
def v30_owner_dashboard():
    pid=project_id(); h=project_health_snapshot(pid); c=db(); project=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
    changes=c.execute("SELECT * FROM change_events WHERE project_id=?",(pid,)).fetchall(); latest=c.execute("SELECT * FROM weekly_ai_reports WHERE project_id=? ORDER BY id DESC LIMIT 1",(pid,)).fetchone(); c.close()
    open_cost=sum(float(r["estimated_cost"] or 0) for r in changes if r["status"] not in ["APPROVED","REJECTED"]); approved=sum(float(r["estimated_cost"] or 0) for r in changes if r["status"]=="APPROVED")
    summary=esc(latest["report_text"][:1800]) if latest else "Generate a Weekly AI Report to populate the owner summary."
    return shell("Owner Dashboard",f"""<div class="hero"><div class="eyebrow">Owner / Executive View</div><h1>{esc(project["name"] if project else "Project")}</h1><div class="kpi">{h["overall"]}/100</div></div>
    <div class="grid4"><div class="card"><div class="label">Schedule</div><div class="kpi">{h["schedule"]}</div></div>
    <div class="card"><div class="label">Readiness</div><div class="kpi">{h["readiness"]}</div></div>
    <div class="card"><div class="label">Open Change</div><div class="kpi">${open_cost:,.0f}</div></div>
    <div class="card"><div class="label">Approved Changes</div><div class="kpi">${approved:,.0f}</div></div></div>
    <div class="card"><h2>Executive Summary</h2><div style="white-space:pre-wrap">{summary}</div></div>""")

@app.get("/portfolio-intelligence",response_class=HTMLResponse)
def v30_portfolio_intelligence():
    cid=current_company_id(); c=db(); projects=c.execute("SELECT * FROM projects WHERE company_id=? ORDER BY name",(cid,)).fetchall(); c.close()
    cards=""; scores=[]
    for p in projects:
        h=project_health_snapshot(p["id"]); scores.append(h["overall"]); band="READY" if h["overall"]>=85 else "WATCH" if h["overall"]>=70 else "HIGH" if h["overall"]>=50 else "CRITICAL"
        cards+=f'<div class="card"><span class="badge {band}">{h["overall"]}/100</span><h3>{esc(p["name"])}</h3><p>Schedule {h["schedule"]} · Readiness {h["readiness"]} · Procurement {h["procurement"]}</p></div>'
    avg=round(sum(scores)/len(scores)) if scores else 0
    return shell("Portfolio Intelligence",f'<div class="hero"><h1>Company Portfolio Health: {avg}/100</h1></div><div class="grid3">{cards}</div>')

@app.get("/mobile-field-plus",response_class=HTMLResponse)
def v30_mobile_field_plus():
    h=project_health_snapshot(project_id())
    return shell("Mobile Field+",f"""<div class="hero"><div class="eyebrow">Mobile Superintendent Workflow</div><h1>Field Mode</h1><div class="kpi">{h["overall"]}/100</div></div>
    <div class="grid2">
    <div class="card"><a href="/quick-entry">🎤 Speak Note</a></div>
    <div class="card"><a href="/photo-ai">📷 Analyze Photo</a></div>
    <div class="card"><a href="/auto-daily-report">📝 Auto Daily Report</a></div>
    <div class="card"><a href="/rfi-drafting">❓ Draft RFI</a></div>
    <div class="card"><a href="/lookahead-intelligence">📅 3-Week Lookahead</a></div>
    <div class="card"><a href="/predictive-forecast">📈 Forecast</a></div>
    <div class="card"><a href="/sub-risk">👷 Sub Risk</a></div>
    <div class="card"><a href="/assistant">🤖 Ask AI</a></div></div>""")

def audit_event(action,detail='',pid=None):
    try:
        c=db(); c.execute('INSERT INTO admin_audit_log(company_id,user_id,project_id,action,detail,created) VALUES(?,?,?,?,?,?)',(current_company_id(),current_user_id(),pid if pid is not None else project_id(),action,detail,datetime.utcnow().isoformat())); c.commit(); c.close()
    except Exception: pass

@app.get('/storage-status',response_class=HTMLResponse)
def storage_status_page():
    pid=project_id(); c=db(); rows=c.execute('SELECT * FROM attachments WHERE company_id=? AND project_id=? ORDER BY id DESC',(current_company_id(),pid)).fetchall(); c.close()
    present=sum(1 for r in rows if os.path.isfile(os.path.join(UPLOAD_DIR,r['stored_name']))); missing=len(rows)-present; persistent=not os.path.abspath(UPLOAD_DIR).startswith('/tmp/'); band='READY' if persistent and missing==0 else 'WATCH'
    return shell('Storage Status',f'''<div class="hero"><div class="eyebrow">Persistent Storage</div><h1><span class="badge {band}">{band}</span> {present}/{len(rows)} project file(s) present</h1></div><div class="grid2"><div class="card"><h2>Upload Path</h2><p>{esc(UPLOAD_DIR)}</p><p class="small">{'Persistent path detected.' if persistent else 'Temporary /tmp path detected.'}</p></div><div class="card"><h2>Missing Files</h2><div class="kpi">{missing}</div></div></div>''')

@app.get('/global-search',response_class=HTMLResponse)
def global_search_page(q:str=''):
    pid=project_id(); results=[]; q=q.strip()
    if q:
        like=f'%{q.lower()}%'; c=db(); specs=[('Schedule','activities','name','/schedule'),('RFI / Issue','project_issues','title','/issues'),('Action','action_items','title','/actions'),('Subcontractor','subs','name','/subcontractors'),('Document','attachments','original_name','/documents'),('Change','change_events','title','/changes')]
        for kind,table,column,url in specs:
            if table=='attachments': rows=c.execute(f'SELECT id,{column} label FROM {table} WHERE project_id=? AND company_id=? AND lower(COALESCE({column},'')) LIKE ? ORDER BY id DESC LIMIT 20',(pid,current_company_id(),like)).fetchall()
            else: rows=c.execute(f'SELECT id,{column} label FROM {table} WHERE project_id=? AND lower(COALESCE({column},'')) LIKE ? ORDER BY id DESC LIMIT 20',(pid,like)).fetchall()
            for r in rows: results.append((kind,r['label'],url))
        c.close()
    html=''.join(f'<div class="search-result"><b>{esc(k)}</b> · <a href="{u}" style="color:#f0b44d">{esc(l)}</a></div>' for k,l,u in results) or ('<div class="muted">No matching project records.</div>' if q else '')
    return shell('Global Search',f'''<div class="hero"><div class="eyebrow">Global Search</div><h1>Search this project.</h1></div><div class="card"><form method="get" action="/global-search"><input name="q" value="{esc(q)}" placeholder="Search schedule, RFIs, actions, subs, documents, changes"><button type="submit">Search</button></form></div><div class="card">{html}</div>''')

@app.get('/favorites',response_class=HTMLResponse)
def favorites_page():
    c=db(); rows=c.execute('SELECT * FROM user_favorites WHERE company_id=? AND user_id=? ORDER BY id DESC',(current_company_id(),current_user_id())).fetchall(); c.close()
    options=''.join(f'<option value="{esc(u)}|{esc(n)}">{esc(n)}</option>' for n,u in NAV)
    cards=''.join(f'<div class="action"><a href="{esc(r["tool_url"])}" style="color:#f0b44d;font-weight:700">{esc(r["tool_name"])}</a><form method="post" action="/favorites/remove"><input type="hidden" name="favorite_id" value="{r["id"]}"><button type="submit">Remove</button></form></div>' for r in rows) or '<div class="muted">No pinned tools yet.</div>'
    return shell('Favorites',f'''<div class="hero"><div class="eyebrow">Favorites</div><h1>Pin your most-used tools.</h1></div><div class="grid2"><div class="card"><form method="post" action="/favorites"><select name="tool">{options}</select><button type="submit">Pin Tool</button></form></div><div class="card">{cards}</div></div>''')

@app.post('/favorites')
def favorites_add(tool:str=Form(...)):
    try: url,name=tool.split('|',1)
    except Exception: return RedirectResponse('/favorites',303)
    c=db(); ex=c.execute('SELECT id FROM user_favorites WHERE company_id=? AND user_id=? AND tool_url=?',(current_company_id(),current_user_id(),url)).fetchone()
    if not ex: c.execute('INSERT INTO user_favorites(company_id,user_id,tool_name,tool_url,created) VALUES(?,?,?,?,?)',(current_company_id(),current_user_id(),name,url,datetime.utcnow().isoformat())); c.commit()
    c.close(); audit_event('PIN_TOOL',name); return RedirectResponse('/favorites',303)

@app.post('/favorites/remove')
def favorites_remove(favorite_id:int=Form(...)):
    c=db(); c.execute('DELETE FROM user_favorites WHERE id=? AND company_id=? AND user_id=?',(favorite_id,current_company_id(),current_user_id())); c.commit(); c.close(); audit_event('UNPIN_TOOL',str(favorite_id)); return RedirectResponse('/favorites',303)

@app.get('/recent-activity',response_class=HTMLResponse)
def recent_activity_page():
    pid=project_id(); c=db(); entries=[]; sources=[('Action','action_items','title','created','/actions'),('RFI / Issue','project_issues','title','created','/issues'),('Document','attachments','original_name','created','/documents'),('Change','change_events','title','created','/changes'),('Daily Report','daily_reports','report_date','created','/daily-report')]
    for kind,table,label_col,date_col,url in sources:
        if table=='attachments': rows=c.execute(f'SELECT {label_col} label,{date_col} stamp FROM {table} WHERE project_id=? AND company_id=? ORDER BY id DESC LIMIT 12',(pid,current_company_id())).fetchall()
        else: rows=c.execute(f'SELECT {label_col} label,{date_col} stamp FROM {table} WHERE project_id=? ORDER BY id DESC LIMIT 12',(pid,)).fetchall()
        for r in rows: entries.append((r['stamp'] or '',kind,r['label'],url))
    c.close(); entries.sort(key=lambda x:x[0],reverse=True); html=''.join(f'<div class="action"><b>{esc(k)}</b> · <a href="{u}" style="color:#f0b44d">{esc(l)}</a><div class="small">{esc(s)}</div></div>' for s,k,l,u in entries[:40]) or '<div class="muted">No recent project activity.</div>'
    return shell('Recent Activity',f'<div class="hero"><div class="eyebrow">Recent Activity</div><h1>What changed recently?</h1></div><div class="card">{html}</div>')

@app.get('/project-clone',response_class=HTMLResponse)
def project_clone_page():
    pid=project_id(); c=db(); p=c.execute('SELECT * FROM projects WHERE id=? AND company_id=?',(pid,current_company_id())).fetchone(); c.close()
    return shell('Clone Project',f'''<div class="hero"><div class="eyebrow">Project Template</div><h1>Clone {esc(p['name'] if p else 'current project')}</h1></div><div class="card"><form method="post" action="/project-clone"><input name="name" placeholder="New Project Name" required><input name="number" placeholder="New Project Number" required><button type="submit">Clone Project Setup</button></form><p class="small">Copies schedule activities and subcontractor list only.</p></div>''')

@app.post('/project-clone')
def project_clone(name:str=Form(...),number:str=Form(...)):
    source=project_id(); cid=current_company_id(); c=db(); c.execute('INSERT INTO projects(name,number,status,company_id) VALUES(?,?,?,?)',(name.strip(),number.strip(),'PLANNING',cid)); new_id=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
    for a in c.execute('SELECT * FROM activities WHERE project_id=? ORDER BY id',(source,)).fetchall(): c.execute("INSERT INTO activities(project_id,external_id,name,trade,start,finish,pct,status) VALUES(?,?,?,?,?,?,0,'NOT_STARTED')",(new_id,a['external_id'],a['name'],a['trade'],a['start'],a['finish']))
    for s in c.execute('SELECT * FROM subs WHERE project_id=? ORDER BY id',(source,)).fetchall(): c.execute('INSERT INTO subs(project_id,name,trade) VALUES(?,?,?)',(new_id,s['name'],s['trade']))
    c.commit(); c.close(); audit_event('CLONE_PROJECT',f'{source} -> {new_id}',new_id); return RedirectResponse('/',303)

@app.get('/project-archive',response_class=HTMLResponse)
def project_archive_page():
    cid=current_company_id(); c=db(); rows=c.execute('SELECT p.*,COALESCE(a.archived,0) archived FROM projects p LEFT JOIN project_archive_state a ON a.project_id=p.id WHERE p.company_id=? ORDER BY p.name',(cid,)).fetchall(); c.close()
    cards=''.join(f'''<div class="action"><b>{esc(r['number'])} - {esc(r['name'])}</b><div class="small">{'ARCHIVED' if r['archived'] else 'ACTIVE'}</div><form method="post" action="/project-archive"><input type="hidden" name="project_id_value" value="{r['id']}"><input type="hidden" name="archived" value="{0 if r['archived'] else 1}"><button type="submit">{'Restore' if r['archived'] else 'Archive'}</button></form></div>''' for r in rows)
    return shell('Archive Projects',f'<div class="hero"><div class="eyebrow">Project Lifecycle</div><h1>Archive or restore projects.</h1></div><div class="card">{cards}</div>')

@app.post('/project-archive')
def project_archive_save(project_id_value:int=Form(...),archived:int=Form(...)):
    cid=current_company_id(); c=db(); valid=c.execute('SELECT id FROM projects WHERE id=? AND company_id=?',(project_id_value,cid)).fetchone()
    if valid:
        ex=c.execute('SELECT project_id FROM project_archive_state WHERE project_id=?',(project_id_value,)).fetchone()
        if ex: c.execute('UPDATE project_archive_state SET archived=?,archived_at=? WHERE project_id=?',(1 if archived else 0,datetime.utcnow().isoformat() if archived else None,project_id_value))
        else: c.execute('INSERT INTO project_archive_state(project_id,company_id,archived,archived_at) VALUES(?,?,?,?)',(project_id_value,cid,1 if archived else 0,datetime.utcnow().isoformat() if archived else None))
        c.commit()
    c.close(); audit_event('ARCHIVE_PROJECT' if archived else 'RESTORE_PROJECT',str(project_id_value),project_id_value); return RedirectResponse('/project-archive',303)

@app.get('/document-tags',response_class=HTMLResponse)
def document_tags_page():
    pid=project_id(); c=db(); docs=c.execute('SELECT * FROM attachments WHERE company_id=? AND project_id=? ORDER BY id DESC',(current_company_id(),pid)).fetchall(); tags=c.execute('SELECT t.*,a.original_name FROM attachment_tags t JOIN attachments a ON a.id=t.attachment_id WHERE t.company_id=? AND a.project_id=? ORDER BY t.id DESC',(current_company_id(),pid)).fetchall(); c.close()
    options=''.join(f'<option value="{d["id"]}">{esc(d["original_name"])}</option>' for d in docs); recent=''.join(f'<div class="action"><b>{esc(t["tag"])}</b> · {esc(t["original_name"])}</div>' for t in tags) or '<div class="muted">No document tags yet.</div>'
    return shell('Document Tags',f'''<div class="hero"><div class="eyebrow">Document Organization</div><h1>Tag project files.</h1></div><div class="grid2"><div class="card"><form method="post" action="/document-tags"><select name="attachment_id">{options}</select><input name="tag" placeholder="electrical, permit, owner" required><button type="submit">Add Tag</button></form></div><div class="card">{recent}</div></div>''')

@app.post('/document-tags')
def document_tags_add(attachment_id:int=Form(...),tag:str=Form(...)):
    c=db(); valid=c.execute('SELECT id FROM attachments WHERE id=? AND company_id=?',(attachment_id,current_company_id())).fetchone()
    if valid: c.execute('INSERT INTO attachment_tags(company_id,attachment_id,tag,created) VALUES(?,?,?,?)',(current_company_id(),attachment_id,tag.strip().lower(),datetime.utcnow().isoformat())); c.commit()
    c.close(); audit_event('TAG_DOCUMENT',tag); return RedirectResponse('/document-tags',303)

@app.get('/notification-rules',response_class=HTMLResponse)
def notification_rules_page():
    c=db(); rows=c.execute('SELECT * FROM notification_rules WHERE company_id=? ORDER BY id DESC',(current_company_id(),)).fetchall(); c.close()
    recent=''.join(f'<div class="action"><span class="badge {esc(r["severity"])}">{esc(r["severity"])}</span> <b>{esc(r["rule_name"])}</b><div class="small">Threshold {r["threshold_value"]} - {"Enabled" if r["enabled"] else "Disabled"}</div></div>' for r in rows) or '<div class="muted">No custom notification rules yet.</div>'
    return shell('Notification Rules',f'''<div class="hero"><div class="eyebrow">Alert Rules</div><h1>Control what deserves attention.</h1></div><div class="grid2"><div class="card"><form method="post" action="/notification-rules"><input name="rule_name" placeholder="High schedule delay" required><select name="severity"><option>WATCH</option><option selected>HIGH</option><option>CRITICAL</option></select><input type="number" step="0.1" name="threshold_value" value="0"><button type="submit">Add Rule</button></form></div><div class="card">{recent}</div></div>''')

@app.post('/notification-rules')
def notification_rules_add(rule_name:str=Form(...),severity:str=Form('HIGH'),threshold_value:float=Form(0)):
    c=db(); c.execute('INSERT INTO notification_rules(company_id,rule_name,enabled,severity,threshold_value,created) VALUES(?,?,1,?,?,?)',(current_company_id(),rule_name.strip(),severity,threshold_value,datetime.utcnow().isoformat())); c.commit(); c.close(); audit_event('ADD_NOTIFICATION_RULE',rule_name); return RedirectResponse('/notification-rules',303)

@app.get('/health-history',response_class=HTMLResponse)
def health_history_page():
    pid=project_id(); h=project_health_snapshot(pid); c=db(); ex=c.execute('SELECT id FROM health_history WHERE company_id=? AND project_id=? AND snapshot_date=?',(current_company_id(),pid,date.today().isoformat())).fetchone()
    if not ex: c.execute('INSERT INTO health_history(company_id,project_id,snapshot_date,overall,schedule,readiness,procurement,risk,field,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(current_company_id(),pid,date.today().isoformat(),h['overall'],h['schedule'],h['readiness'],h['procurement'],h['risk'],h['field'],datetime.utcnow().isoformat())); c.commit()
    rows=c.execute('SELECT * FROM health_history WHERE company_id=? AND project_id=? ORDER BY snapshot_date DESC,id DESC LIMIT 30',(current_company_id(),pid)).fetchall(); c.close()
    history=''.join(f'<div class="action"><b>{esc(r["snapshot_date"])}</b> - Overall {r["overall"]:.0f}/100<div class="small">Schedule {r["schedule"]:.0f} · Readiness {r["readiness"]:.0f} · Procurement {r["procurement"]:.0f} · Risk {r["risk"]:.0f} · Field {r["field"]:.0f}</div></div>' for r in rows)
    return shell('Health History',f'<div class="hero"><div class="eyebrow">Project Trend</div><h1>Health history</h1></div><div class="card">{history}</div>')

@app.get('/bulk-export')
def bulk_export():
    pid=project_id(); cid=current_company_id(); stamp=datetime.utcnow().strftime('%Y%m%d_%H%M%S'); zpath=f'/tmp/buildcommand_project_export_{stamp}.zip'; tables=['projects','activities','subs','action_items','project_issues','daily_reports','procurement','risks','change_events','attachments']; c=db()
    with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
        for table in tables:
            if table=='projects': rows=c.execute('SELECT * FROM projects WHERE id=? AND company_id=?',(pid,cid)).fetchall()
            elif table=='attachments': rows=c.execute('SELECT * FROM attachments WHERE project_id=? AND company_id=?',(pid,cid)).fetchall()
            else: rows=c.execute(f'SELECT * FROM {table} WHERE project_id=?',(pid,)).fetchall()
            if not rows: continue
            headers=list(rows[0].keys()); sio=io.StringIO(); writer=csv.writer(sio); writer.writerow(headers)
            for r in rows: writer.writerow([r[h] for h in headers])
            z.writestr(f'{table}.csv',sio.getvalue())
    c.close(); audit_event('BULK_EXPORT',f'project {pid}',pid); return FileResponse(zpath,media_type='application/zip',filename=f'buildcommand_project_export_{stamp}.zip')

@app.get('/audit-log',response_class=HTMLResponse)
def audit_log_page():
    if not require_role('ADMIN'): return HTMLResponse('Not authorized.',status_code=403)
    c=db(); rows=c.execute('SELECT l.*,u.display_name FROM admin_audit_log l LEFT JOIN users u ON u.id=l.user_id WHERE l.company_id=? ORDER BY l.id DESC LIMIT 100',(current_company_id(),)).fetchall(); c.close()
    html=''.join(f'<div class="action"><b>{esc(r["action"])}</b> - {esc(r["display_name"] or "System")}<div>{esc(r["detail"])}</div><div class="small">{esc(r["created"])}</div></div>' for r in rows) or '<div class="muted">No v31 audit events yet.</div>'
    return shell('Audit Log',f'<div class="hero"><div class="eyebrow">Admin Audit Log</div><h1>Track important workspace changes.</h1></div><div class="card">{html}</div>')
