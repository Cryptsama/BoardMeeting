# agegate_routes.py — simple DOB age gate (no accounts, no uploads, no queues)
from __future__ import annotations

import os
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

router = APIRouter()

COOKIE_NAME = "bm_age"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days

# IMPORTANT: set AGEGATE_SECRET in .env for stable, secure cookies
_SECRET = os.getenv("AGEGATE_SECRET") or os.getenv("SECRET_KEY") or "dev-insecure-change-me"
_serializer = URLSafeTimedSerializer(_SECRET, salt="boardmeeting-agegate")


def _age_years(dob: date, today: date | None = None) -> int:
    today = today or date.today()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def _set_age_cookie(resp: RedirectResponse, ok: bool) -> None:
    if ok:
        token = _serializer.dumps({"ok": True, "ts": int(datetime.utcnow().timestamp())})
        resp.set_cookie(
            COOKIE_NAME,
            token,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
    else:
        resp.delete_cookie(COOKIE_NAME)


def is_age_verified(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        data = _serializer.loads(token, max_age=COOKIE_MAX_AGE_SECONDS)
        return bool(data.get("ok")) is True
    except (BadSignature, SignatureExpired):
        return False


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0b0f14; color:#e7eef7; }}
    .wrap {{ max-width: 560px; margin: 7vh auto; padding: 22px; border: 1px solid #1d2a3a; border-radius: 14px; background:#0f1720; }}
    input, button {{ width:100%; padding: 12px; border-radius: 10px; border: 1px solid #233246; background:#0b0f14; color:#e7eef7; }}
    button {{ cursor:pointer; background:#1c2b3d; }}
    .muted {{ color:#a9b7c7; font-size: 13px; margin-top: 10px; }}
    .err {{ color:#ffb3b3; margin: 10px 0; }}
    a {{ color:#9dd1ff; text-decoration:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    {body}
  </div>
</body>
</html>"""
    )


@router.get("/age-gate", response_class=HTMLResponse)
def age_gate_get(request: Request, next: str | None = None):
    if is_age_verified(request):
        return RedirectResponse(url=next or "/", status_code=302)

    next_q = quote(next or "/")
    return _page(
        "Age Verification",
        f"""
        <h2>Age Verification</h2>
        <p class="muted">Enter your date of birth to continue.</p>
        <form method="post" action="/age-gate">
          <input type="hidden" name="next" value="{next_q}">
          <div style="margin:12px 0;">
            <input name="dob" type="date" required>
          </div>
          <button type="submit">Continue</button>
        </form>
        <p class="muted">Cookie lasts 30 days (local only). If you clear cookies, you'll be asked again.</p>
        """,
    )


@router.post("/age-gate", response_class=HTMLResponse)
def age_gate_post(dob: str = Form(...), next: str = Form("/")):
    # dob expected: YYYY-MM-DD
    try:
        y, m, d = [int(x) for x in dob.split("-")]
        dob_date = date(y, m, d)
    except Exception:
        return _page("Age Verification", '<div class="err">Invalid date.</div><a href="/age-gate">Back</a>')

    if _age_years(dob_date) < 18:
        return _page("Age Verification", '<div class="err">You must be 18+ to use this site.</div>')

    resp = RedirectResponse(url=next or "/", status_code=302)
    _set_age_cookie(resp, True)
    return resp


@router.get("/age-clear")
def age_clear():
    resp = RedirectResponse(url="/age-gate", status_code=302)
    _set_age_cookie(resp, False)
    return resp