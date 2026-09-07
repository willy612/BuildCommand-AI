
from pathlib import Path
import base64
import re

from fastapi.responses import FileResponse, HTMLResponse
import full_app as _full

app = _full.app

_ASSET_DIR = Path(__file__).resolve().parent / "branding"
_HEADER = _ASSET_DIR / "approved_main_logo.jpg"
_SOCIAL = _ASSET_DIR / "approved_social_preview.png"

if _SOCIAL.exists() and hasattr(_full, "_BC660_SHARE"):
    _full._BC660_SHARE = base64.b64encode(_SOCIAL.read_bytes()).decode("ascii")

@app.get("/brand/approved-main-logo.jpg")
def approved_main_logo():
    return FileResponse(
        _HEADER,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, max-age=0"},
    )

def _approved_brand_html():
    return (
        '<div class="bc-brand-wrap" id="bc-approved-main-brand">'
        '<a href="/" aria-label="BuildCommand AI home">'
        '<img src="/brand/approved-main-logo.jpg?v=20260907" '
        'alt="BuildCommand AI — Construction Intelligence">'
        '</a>'
        '</div>'
    )

_BRAND_CSS = '''
<style id="bc-approved-main-brand-style">
#bc650-brand-wrap,
#bc652-app-logo,
#bc653-approved-logo,
#bc654-brand-header,
#bc657-dashboard-logo,
#bc658-dashboard-logo,
#bc659-approved-logo,
#bc661-approved-header-logo,
#bc662-native-brand,
.bc-brand-wrap:not(#bc-approved-main-brand){
    display:none!important;
    width:0!important;
    min-width:0!important;
    max-width:0!important;
    height:0!important;
    margin:0!important;
    padding:0!important;
    overflow:hidden!important;
    opacity:0!important;
    pointer-events:none!important;
}
#bc-approved-main-brand{
    display:flex!important;
    flex:0 0 auto!important;
    align-items:center!important;
    justify-content:flex-start!important;
    width:390px!important;
    max-width:34vw!important;
    height:105px!important;
    margin:0 16px 0 0!important;
    padding:0!important;
    overflow:hidden!important;
    opacity:1!important;
}
#bc-approved-main-brand a{
    display:flex!important;
    width:100%!important;
    height:100%!important;
    align-items:center!important;
    text-decoration:none!important;
    line-height:0!important;
}
#bc-approved-main-brand img{
    display:block!important;
    width:100%!important;
    height:100%!important;
    object-fit:contain!important;
    object-position:left center!important;
    border:0!important;
    margin:0!important;
    padding:0!important;
}
@media(max-width:900px){
    #bc-approved-main-brand{
        width:300px!important;
        max-width:36vw!important;
        height:88px!important;
    }
}
@media(max-width:650px){
    #bc-approved-main-brand{
        width:235px!important;
        max-width:47vw!important;
        height:72px!important;
        margin-right:8px!important;
    }
}
</style>
'''

def _rewrite_final_html(html: str) -> str:
    if not isinstance(html, str):
        return html

    approved = _approved_brand_html()

    html, count = re.subn(
        r'<div\b[^>]*id=["\']bc662-native-brand["\'][^>]*>.*?</div>',
        approved,
        html,
        count=1,
        flags=re.I | re.S,
    )

    if count == 0:
        html, count = re.subn(
            r'<div\b[^>]*class=["\'][^"\']*\bbc-brand-wrap\b[^"\']*["\'][^>]*>.*?</div>',
            approved,
            html,
            count=1,
            flags=re.I | re.S,
        )

    if count == 0 and 'id="bc-approved-main-brand"' not in html:
        marker = '<form class="v117r-project"'
        pos = html.find(marker)
        if pos >= 0:
            html = html[:pos] + approved + html[pos:]

    if 'id="bc-approved-main-brand-style"' not in html:
        idx = html.lower().find("</head>")
        if idx >= 0:
            html = html[:idx] + _BRAND_CSS + html[idx:]
        else:
            html = _BRAND_CSS + html

    social = "https://buildcommandai.com/brand/social-share-1200x630.png?v=20260907"

    html = re.sub(
        r'<meta\s+property=["\']og:image["\'][^>]*>',
        f'<meta property="og:image" content="{social}">',
        html,
        flags=re.I,
    )
    html = re.sub(
        r'<meta\s+property=["\']og:title["\'][^>]*>',
        '<meta property="og:title" content="BuildCommand AI — Construction Intelligence">',
        html,
        flags=re.I,
    )
    html = re.sub(
        r'<meta\s+property=["\']og:description["\'][^>]*>',
        '<meta property="og:description" content="Construction Intelligence for America’s Builders.">',
        html,
        flags=re.I,
    )
    html = re.sub(
        r'<meta\s+name=["\']twitter:image["\'][^>]*>',
        f'<meta name="twitter:image" content="{social}">',
        html,
        flags=re.I,
    )

    return html

@app.middleware("http")
async def approved_main_brand_finalizer(request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path or "/"
        if path.startswith(("/brand/", "/health/", "/api/")):
            return response

        ctype = str(response.headers.get("content-type") or "").lower()
        if "text/html" not in ctype:
            return response

        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk

        html = raw.decode("utf-8", errors="replace")
        html = _rewrite_final_html(html)

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return HTMLResponse(
            html,
            status_code=response.status_code,
            headers=headers,
        )
    except Exception:
        return response

@app.get("/health/approved-main-brand-final")
def approved_main_brand_health():
    return {
        "status": "ok" if _HEADER.exists() and _SOCIAL.exists() else "degraded",
        "baseline": "6.6.2",
        "full_app_edited": False,
        "old_header_layers_forced_hidden": True,
        "approved_logo_route": "/brand/approved-main-logo.jpg?v=20260907",
        "social_preview_route": "/brand/social-share-1200x630.png?v=20260907",
        "owner_console_separate": True,
    }
