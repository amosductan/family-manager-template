"""Authentication for Family Manager — Google Sign-In + an email allowlist.

Three modes:

- ``AUTH_MODE=dev`` (default, LOCAL ONLY) — no login; every request is
  ``FM_DEV_EMAIL`` (defaults to the household's own mailbox). Lets the app run
  offline with no OAuth client. NEVER expose the app beyond your machine in this mode.
- ``AUTH_MODE=passcode`` — a shared family passcode (``APP_PASSCODE``),
  constant-time compare + 0.5s sleep on mismatch. The simple option when the app
  is reachable from other devices and there's no OAuth client yet.
- ``AUTH_MODE=google`` — "Sign in with Google" via OAuth/OIDC (Authlib). On
  callback the verified email is checked against the allowlist; only allowlisted
  users get a session. This is how a second parent signs in from their own phone.

Allowlist: ``FM_ALLOWLIST`` — comma-separated emails, e.g.
"sam@example.com,jordan@example.com". No domain allowlist on purpose — a family
tool is for named people only.

The OAuth client lives in your own Google Cloud project; add this app's redirect
URI to it. Sessions are signed with ``FLASK_SECRET``.
"""
from __future__ import annotations

import os

from flask import redirect, request, session, url_for

import family

AUTH_MODE = os.environ.get("AUTH_MODE", "dev").strip().lower()
DEV_EMAIL = (os.environ.get("FM_DEV_EMAIL") or family.SELF_EMAIL
             or "parent@family.local").strip().lower()

# Paths reachable without a session (OAuth dance itself + health check + static)
PUBLIC_PREFIXES = ("/auth/", "/healthz", "/static/")

_oauth = None


def _allowlist() -> set[str]:
    raw = os.environ.get("FM_ALLOWLIST", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_allowed(email: str) -> bool:
    if AUTH_MODE == "dev":
        return True
    return bool(email) and email.strip().lower() in _allowlist()


def current_email() -> str | None:
    """Verified + allowlisted user's email, or None. Single source of identity."""
    if AUTH_MODE == "dev":
        return DEV_EMAIL
    if AUTH_MODE == "passcode":
        return "family@passcode" if session.get("authed") else None
    email = (session.get("email") or "").strip().lower()
    return email if email and is_allowed(email) else None


def install(app) -> None:
    """Wire sessions, the before-request gate, and (in google mode) /auth/* routes."""
    secret = os.environ.get("FLASK_SECRET", "")
    if AUTH_MODE in ("google", "passcode") and not secret:
        raise RuntimeError(f"AUTH_MODE={AUTH_MODE} requires FLASK_SECRET (random hex).")
    app.secret_key = secret or "dev-insecure-secret-change-me"
    app.config.update(
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(AUTH_MODE in ("google", "passcode")),
        SESSION_COOKIE_HTTPONLY=True,
    )

    @app.context_processor
    def _inject_identity():
        return {"auth_mode": AUTH_MODE, "user_email": current_email()}

    @app.before_request
    def _gate():
        if AUTH_MODE == "dev":
            return None
        if request.path.startswith(PUBLIC_PREFIXES):
            return None
        if current_email():
            return None
        return redirect(url_for("auth_login"))

    if AUTH_MODE == "passcode":
        import secrets as _secrets
        import time as _time

        passcode = os.environ.get("APP_PASSCODE", "").strip()
        if not passcode:
            raise RuntimeError("AUTH_MODE=passcode but APP_PASSCODE is not set.")

        FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Family Manager</title>
<body style="font-family:Georgia,serif;display:flex;min-height:90vh;align-items:center;justify-content:center;background:#fdfcfa">
<form method=post style="text-align:center">
<h1 style="font-weight:300">Family<span style="color:#C4974C">Manager</span></h1>
<p style="font-family:sans-serif;font-size:13px;color:#777">{msg}</p>
<input name=passcode type=password autofocus style="padding:10px;font-size:16px;border:1px solid #ddd;border-radius:4px">
<button style="padding:10px 18px;font-size:12px;letter-spacing:1.5px;text-transform:uppercase;background:#C4974C;color:#fff;border:0;border-radius:4px;cursor:pointer">Enter</button>
</form></body>"""

        @app.route("/auth/login", methods=["GET", "POST"])
        def auth_login():
            if request.method == "POST":
                entered = (request.form.get("passcode") or "").strip()
                if entered and _secrets.compare_digest(entered, passcode):
                    session["authed"] = True
                    return redirect("/")
                _time.sleep(0.5)  # blunt brute force
                return FORM.format(msg="Wrong passcode — try again."), 401
            return FORM.format(msg="Enter the family passcode.")

        @app.get("/auth/logout")
        def auth_logout():
            session.pop("authed", None)
            return redirect("/auth/login")

        return

    if AUTH_MODE != "google":
        @app.get("/auth/login")
        def auth_login():
            return redirect("/")

        @app.get("/auth/logout")
        def auth_logout():
            return redirect("/")

        return

    # ── google mode ──────────────────────────────────────────────────────────
    from authlib.integrations.flask_client import OAuth

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "AUTH_MODE=google but GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not "
            "set. Create an OAuth client in your Google Cloud project and set both."
        )
    if not _allowlist():
        raise RuntimeError("AUTH_MODE=google but FM_ALLOWLIST is empty — nobody "
                           "could sign in. Set it (comma-separated emails).")

    global _oauth
    _oauth = OAuth(app)
    _oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    @app.get("/auth/login")
    def auth_login():
        # Behind a TLS-terminating proxy url_for() can emit http:// which then
        # mismatches the https:// URI registered in Google. FM_OAUTH_REDIRECT pins
        # the exact registered value.
        redirect_uri = os.environ.get("FM_OAUTH_REDIRECT", "").strip() \
            or url_for("auth_callback", _external=True)
        return _oauth.google.authorize_redirect(redirect_uri)

    @app.get("/auth/callback")
    def auth_callback():
        from authlib.integrations.base_client.errors import OAuthError
        try:
            token = _oauth.google.authorize_access_token()
        except OAuthError:
            return "Sign-in failed. Try again.", 401
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").strip().lower()
        if not email or not info.get("email_verified", True):
            return "Could not verify your Google email.", 401
        if not is_allowed(email):
            return (f"<h2>Not on the guest list</h2><p><b>{email}</b> isn't "
                    "allowed yet. Ask whoever runs this app to add you.</p>"), 403
        session["email"] = email
        return redirect("/")

    @app.get("/auth/logout")
    def auth_logout():
        session.pop("email", None)
        return redirect("/auth/login")
