"""
BuildCommand AI — Owner Service Entry Point
Phase 1.1 Corrected Owner Landing

Purpose:
- Keeps the live main BuildCommand app untouched.
- Reuses the existing BuildCommand runtime/database/auth for now.
- Gives the separate Owner Render service its own root landing.
- Redirects "/" directly to the Owner Business Console.
- Filters out customer construction routes from the Owner service.
- Safe bridge before Phase 2 extracts shared auth/database/runtime.

Render start command:
    uvicorn owner_app:app --host 0.0.0.0 --port $PORT
"""

from fastapi.responses import RedirectResponse
from full_app import app as _full_app

app = _full_app

_KEEP_EXACT = {
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

# Remove the existing "/" route and all main customer/construction routes.
_filtered = []
for _route in list(app.router.routes):
    _path = getattr(_route, "path", None)
    if _path is None:
        _filtered.append(_route)
        continue
    if _path == "/":
        continue
    if _keep_route(_route):
        _filtered.append(_route)

app.router.routes[:] = _filtered

# Owner service root should always go to the Owner Console.
@app.get("/", include_in_schema=False)
def owner_root():
    return RedirectResponse(url="/owner", status_code=307)

try:
    app.title = "BuildCommand AI Owner"
    app.version = "6.6.4-owner-phase-1.1"
except Exception:
    pass


@app.get("/health/owner-service")
def owner_service_health():
    paths = {str(getattr(r, "path", "") or "") for r in app.routes}
    required = {
        "/",
        "/owner",
        "/owner/customers",
        "/owner/api/summary",
    }
    return {
        "status": "ok" if required.issubset(paths) else "degraded",
        "service": "BuildCommand AI Owner",
        "version": "6.6.4-owner-phase-1.1",
        "root_redirects_to_owner": "/" in paths,
        "owner_console_present": "/owner" in paths,
        "customers_present": "/owner/customers" in paths,
        "owner_api_present": "/owner/api/summary" in paths,
        "main_customer_routes_filtered": True,
        "shared_existing_postgres_runtime": True,
        "phase": "1.1",
        "next_phase": "extract shared database/auth/runtime so owner service no longer imports full_app.py",
    }


if __name__ == "__main__":
    print("BuildCommand AI Owner Service 6.6.4-owner-phase-1.1")
    print("Run: uvicorn owner_app:app --host 0.0.0.0 --port $PORT")
