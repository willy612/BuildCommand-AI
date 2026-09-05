"""
BuildCommand AI — Owner Service Entry Point
Phase 1 Owner/Main App Separation

Purpose:
- Gives the Owner Business Console its own Render entry point.
- Reuses the EXISTING BuildCommand runtime, authentication, PostgreSQL connection,
  and owner_console.py implementation.
- Filters the exposed routes to Owner Console + auth/health/static essentials.
- Does NOT modify full_app.py.
- Safe bridge before Phase 2 extracts the shared runtime into small modules.

Render start command for the NEW owner service:
    uvicorn owner_app:app --host 0.0.0.0 --port $PORT
"""

from full_app import app as _full_app

app = _full_app

# Keep only routes required by the private Owner Console service.
# We intentionally retain authentication/session entry points and static/health
# surfaces so the existing BuildCommand auth behavior remains intact.
_KEEP_EXACT = {
    "/",
    "/login",
    "/logout",
    "/health",
    "/favicon.ico",
}

_KEEP_PREFIXES = (
    "/owner",
    "/login",
    "/logout",
    "/auth",
    "/oauth",
    "/static",
    "/assets",
    "/favicon",
    "/health",
)

def _keep_route(route):
    path = str(getattr(route, "path", "") or "")
    if path in _KEEP_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _KEEP_PREFIXES)

# Preserve framework/internal mounts with no normal path; filter normal HTTP routes.
_filtered = []
for _route in list(app.router.routes):
    _path = getattr(_route, "path", None)
    if _path is None or _keep_route(_route):
        _filtered.append(_route)

app.router.routes[:] = _filtered

# Owner service identity.
try:
    app.title = "BuildCommand AI Owner"
    app.version = "6.6.4-owner-phase-1"
except Exception:
    pass


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
        "version": "6.6.4-owner-phase-1",
        "owner_console_present": "/owner" in paths,
        "customers_present": "/owner/customers" in paths,
        "owner_api_present": "/owner/api/summary" in paths,
        "main_customer_routes_filtered": True,
        "shared_existing_postgres_runtime": True,
        "phase": 1,
        "next_phase": "extract shared database/auth runtime so owner service no longer imports full_app.py",
    }


if __name__ == "__main__":
    print("BuildCommand AI Owner Service 6.6.4-owner-phase-1")
    print("Run: uvicorn owner_app:app --host 0.0.0.0 --port $PORT")
