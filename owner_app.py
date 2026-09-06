"""
BuildCommand AI — Standalone Owner Service
Version 6.6.5 Phase 2A

IMPORTANT:
This Owner service NO LONGER imports full_app.py.

It uses:
- shared_core.py for database/auth/runtime
- owner_console.py for Owner Business Console features

Render:
    uvicorn owner_app:app --host 0.0.0.0 --port $PORT
"""

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import shared_core as runtime
from owner_console import register_owner_console


app = FastAPI(
    title="BuildCommand AI Owner",
    version="6.6.5-owner-phase-2A",
)


PUBLIC_PATHS = {
    "/login",
    "/health",
    "/health/owner-service",
}


@app.middleware("http")
async def owner_authentication_middleware(request: Request, call_next):
    raw_token = request.cookies.get("bc_session")
    user = runtime.user_from_session(raw_token)
    tokens = runtime.set_request_user(user)

    try:
        if not user and request.url.path not in PUBLIC_PATHS:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)
    finally:
        runtime.reset_request_user(tokens)


def login_page(message=""):
    msg = f'<p class="error">{message}</p>' if message else ""
    return f"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BuildCommand AI Owner · Sign In</title>
<style>
body{{margin:0;background:#081018;color:#edf4fb;font-family:Inter,system-ui,sans-serif;padding:28px}}
.box{{max-width:460px;margin:9vh auto;background:#0e1823;border:1px solid #203247;border-radius:18px;padding:28px}}
.eyebrow{{color:#f0b44d;font-size:12px;font-weight:900;letter-spacing:.13em;text-transform:uppercase}}
h1{{margin:8px 0 6px}}
.muted{{color:#8fa5bb}}
.error{{color:#ff9696}}
label{{display:block;font-weight:800;margin-top:17px}}
input{{width:100%;box-sizing:border-box;background:#0a141e;color:#edf4fb;border:1px solid #2a4056;border-radius:9px;padding:12px;margin-top:7px}}
button{{margin-top:20px;width:100%;border:0;border-radius:9px;background:#f0b44d;color:#071018;padding:12px;font-weight:900;cursor:pointer}}
</style>
</head>
<body>
<div class="box">
<div class="eyebrow">Private Platform Control</div>
<h1>BuildCommand AI · Owner</h1>
<p class="muted">Sign in with your BuildCommand platform-owner account.</p>
{msg}
<form method="post" action="/login">
<label>Email</label>
<input type="email" name="email" required>
<label>Password</label>
<input type="password" name="password" required>
<button type="submit">Sign In to Owner Console</button>
</form>
</div>
</body>
</html>"""


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/owner", status_code=307)


@app.get("/login", response_class=HTMLResponse)
def login_get():
    if runtime.current_user():
        return RedirectResponse("/owner", status_code=303)
    return HTMLResponse(login_page())


@app.post("/login", response_class=HTMLResponse)
def login_post(email: str = Form(...), password: str = Form(...)):
    c = runtime.db()
    user = c.execute(
        "SELECT * FROM users WHERE lower(email)=lower(?)",
        (email.strip(),),
    ).fetchone()
    c.close()

    if not user or not runtime.verify_password(password, user["password_hash"]):
        return HTMLResponse(login_page("Email or password is incorrect."), status_code=401)

    if not runtime._bc174_is_platform_owner(user):
        return HTMLResponse(
            login_page("This account is not authorized for the Owner Console."),
            status_code=403,
        )

    raw = runtime.create_session(user["id"])
    response = RedirectResponse("/owner", status_code=303)
    response.set_cookie(
        "bc_session",
        raw,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="lax",
        max_age=2592000,
    )
    return response


@app.post("/logout")
def logout(request: Request):
    raw = request.cookies.get("bc_session")

    if raw:
        import hashlib
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        c = runtime.db()
        c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        c.commit()
        c.close()

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("bc_session")
    return response


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "BuildCommand AI Owner",
        "version": "6.6.5-owner-phase-2A",
    }


# Register the existing 674-line Owner Business Console unchanged.
register_owner_console(app, runtime)


@app.get("/health/owner-service")
def owner_service_health():
    paths = {str(getattr(r, "path", "") or "") for r in app.routes}
    required = {
        "/owner",
        "/owner/customers",
        "/owner/api/summary",
    }

    return {
        "status": "ok" if required.issubset(paths) else "degraded",
        "service": "BuildCommand AI Owner",
        "version": "6.6.5-owner-phase-2A",
        "imports_full_app": False,
        "shared_core_version": runtime.SHARED_CORE_VERSION,
        "database_kind": runtime.DATABASE_KIND,
        "owner_console_present": "/owner" in paths,
        "customers_present": "/owner/customers" in paths,
        "owner_api_present": "/owner/api/summary" in paths,
        "same_database_via_DATABASE_URL": bool(runtime.DATABASE_URL),
        "phase": "2A",
    }


if __name__ == "__main__":
    print("BuildCommand AI Owner 6.6.5 Phase 2A")
    print("full_app.py dependency: REMOVED")
    print("Run: uvicorn owner_app:app --host 0.0.0.0 --port $PORT")
