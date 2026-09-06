"""
BuildCommand AI — Main Service Branding Wrapper
6.6.2 baseline, Owner Console separated.

This avoids editing the 16+ MB full_app.py.
It imports the existing main app and swaps ONLY:
1) the main header logo
2) the text/social link preview image

Render start:
    uvicorn main_app:app --host 0.0.0.0 --port $PORT
"""

from pathlib import Path
import base64

from fastapi.responses import FileResponse
import full_app as _full

app = _full.app

_ASSET_DIR = Path(__file__).resolve().parent / "branding"
_HEADER = _ASSET_DIR / "approved_main_logo.jpg"
_SOCIAL = _ASSET_DIR / "approved_social_preview.png"

# 6.6.2's header reads this variable at request time.
# Point it at our clean asset instead of the old embedded logo.
_full._BC659_LOGO = "/brand/approved-main-logo.jpg"

# 6.6.0/6.6.2's existing social-share route decodes this variable.
# Feed it the approved 1200x630 image.
if _SOCIAL.exists():
    _full._BC660_SHARE = base64.b64encode(_SOCIAL.read_bytes()).decode("ascii")


@app.get("/brand/approved-main-logo.jpg")
def approved_main_logo():
    return FileResponse(
        _HEADER,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/health/6-6-2-approved-brand-wrapper")
def approved_brand_wrapper_health():
    return {
        "status": "ok" if _HEADER.exists() and _SOCIAL.exists() else "degraded",
        "baseline": "6.6.2",
        "main_logo_replaced": _HEADER.exists(),
        "social_preview_replaced": _SOCIAL.exists(),
        "owner_console_separate": True,
        "full_app_edited": False,
    }
