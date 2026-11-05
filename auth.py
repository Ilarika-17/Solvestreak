"""
Google sign-in for Solvestreak.

Handles: the users database (one row per person who has ever signed in,
keyed by their stable Google account id), the "Sign in with Google" OAuth
web flow, the @login_required decorator every page route is wrapped in, and
the /settings page where someone sets or changes their LeetCode username.

Sessions are set to last a long time (see app.py's PERMANENT_SESSION_LIFETIME)
so nobody has to sign in again just because they closed the app window or
restarted their computer — only an explicit "Sign out" click clears it.

--- One-time setup before this works ---

credentials.json in this folder is currently a "Desktop app" OAuth client
(the kind used by solvestreak.py's Calendar feature, which opens a browser
and listens on a random local port). That type happens to also work for
local testing of this web sign-in flow, because Google treats any port on
http://localhost as the same registered redirect URI for Desktop clients.

It will NOT be the right client type once this app is actually deployed
somewhere other than your own machine. Before deploying:
  1. In Google Cloud Console -> APIs & Services -> Credentials, create a
     second OAuth client of type "Web application".
  2. Add your deployed URL's callback as an authorized redirect URI, e.g.
     https://your-deployed-domain.com/auth/google/callback
  3. Set the GOOGLE_CLIENT_SECRETS_FILE env var to point at that new
     client's downloaded JSON, and set OAUTH_REDIRECT_URI in .env to the
     deployed callback URL.
Until then, this reads credentials.json as-is and assumes it's running at
http://127.0.0.1:5000, which is only ever true on your own machine.
"""
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, g, redirect, render_template, request, session, url_for
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow
from google.auth.transport import requests as google_requests

import scheduler

DB_PATH = "users.db"

# The one account that sees the admin dashboard (see is_admin below).
# Configurable via .env so it can change without a code edit, but defaults
# to the owner's own address so it works out of the box.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "awesomeilarika@gmail.com").strip().lower()

# How often a signed-in person's last_seen_at actually gets written (see
# _maybe_touch_last_seen) -- the admin dashboard's "active now" count only
# needs roughly-current data, and throttling avoids a database write on
# every single page load for every signed-in person, which matters more as
# more people use the app.
LAST_SEEN_THROTTLE = timedelta(seconds=30)

CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "credentials.json")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://127.0.0.1:5000/auth/google/callback")
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    # Requested here, for everyone, so each signed-in person's Deadline
    # Plan reads THEIR OWN calendar -- see the calendar_token column below.
    # Previously this was a separate opt-in flow in solvestreak.py sharing
    # one token.json for the whole app, which meant whichever one person
    # connected their calendar first was the only calendar anyone ever saw.
    "https://www.googleapis.com/auth/calendar.readonly",
]

# oauthlib refuses to complete the OAuth exchange over plain http, since
# OAuth 2 normally requires https end-to-end. Google itself is fine with
# http://localhost for local testing, so this flag just tells oauthlib not
# to enforce that rule -- but ONLY while the callback is actually on
# localhost. Once OAUTH_REDIRECT_URI is set to a real https:// deployment
# URL (see the docstring above), this block does nothing, so the security
# check stays on in production exactly where it matters.
if REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# This OAuth client is shared with solvestreak.py's separate Calendar
# feature, which asks for a broader "calendar.readonly" scope under its own
# flow. If someone already approved that at some point, Google can fold it
# into the token it hands back here even though this sign-in flow only ever
# asks for the three scopes below -- and oauthlib treats any such mismatch
# between requested and returned scopes as a hard error by default. This is
# the officially documented way to tell it that's fine, not a sign anything
# is broken or that a token expired.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------- database

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT,
            name TEXT,
            leetcode_username TEXT,
            created_at TEXT,
            calendar_token TEXT
        )
    """)
    # calendar_token didn't exist before the per-user Calendar fix -- add it
    # to any users.db that was created before today so existing installs
    # don't break on startup. Same deal for last_seen_at, added for the
    # admin dashboard's "active now" count, and theme/palette, added so each
    # account keeps its own colour theme instead of everyone sharing
    # whatever was last picked in that browser (see settings_theme below).
    # Both are nullable: NULL means "this account has never picked one, use
    # the app-wide default" -- same meaning localStorage's absence used to
    # have, just moved into the account instead of the browser.
    cursor.execute("PRAGMA table_info(users)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "calendar_token" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN calendar_token TEXT")
    if "last_seen_at" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
    if "theme" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN theme TEXT")
    if "palette" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN palette TEXT")
    conn.commit()
    conn.close()


def _row_to_user(row):
    if row is None:
        return None
    return {
        "id": row[0],
        "google_sub": row[1],
        "email": row[2],
        "name": row[3],
        "leetcode_username": row[4],
        "created_at": row[5],
        "last_seen_at": row[6],
        "theme": row[7],
        "palette": row[8],
    }


def get_all_users():
    """Every account that has ever signed in -- used by the daily unattended
    run (daily_run.py), which has no browser session to ask "which user" so
    it loops over everyone with a LeetCode username set, and by the admin
    dashboard, which shows the full roster with when each person was last
    seen."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, google_sub, email, name, leetcode_username, created_at, last_seen_at, theme, palette FROM users ORDER BY id"
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_user(row) for row in rows]


def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, google_sub, email, name, leetcode_username, created_at, last_seen_at, theme, palette FROM users WHERE id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return _row_to_user(row)


def get_or_create_user(google_sub, email, name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, google_sub, email, name, leetcode_username, created_at FROM users WHERE google_sub = ?",
        (google_sub,),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO users (google_sub, email, name, leetcode_username, created_at) VALUES (?, ?, ?, NULL, ?)",
            (google_sub, email, name, datetime.now().isoformat()),
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return get_user(user_id)

    user_id = row[0]
    # Keep email/name fresh in case they change on the Google side.
    cursor.execute("UPDATE users SET email = ?, name = ? WHERE id = ?", (email, name, user_id))
    conn.commit()
    conn.close()
    return get_user(user_id)


def update_leetcode_username(user_id, leetcode_username):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET leetcode_username = ? WHERE id = ?",
        (leetcode_username.strip() or None, user_id),
    )
    conn.commit()
    conn.close()


def update_theme_palette(user_id, theme=None, palette=None):
    """Persists this account's colour theme choice(s). Either argument can
    be left as None to leave that one column untouched -- the light/dark
    toggle only ever changes theme, the Settings swatch grid only ever
    changes palette, and each is saved independently as soon as it changes
    (see the /settings/theme route and the fetch calls in app.js), not only
    when the full Settings form is submitted."""
    if theme is None and palette is None:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if theme is not None and palette is not None:
        cursor.execute("UPDATE users SET theme = ?, palette = ? WHERE id = ?", (theme, palette, user_id))
    elif theme is not None:
        cursor.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))
    else:
        cursor.execute("UPDATE users SET palette = ? WHERE id = ?", (palette, user_id))
    conn.commit()
    conn.close()


def save_calendar_credentials(user_id, creds_json):
    """Store this user's own Google Calendar credentials (as the JSON string
    from Credentials.to_json()), so solvestreak.py can read their calendar
    without touching anyone else's."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET calendar_token = ? WHERE id = ?",
        (creds_json, user_id),
    )
    conn.commit()
    conn.close()


def get_calendar_credentials(user_id):
    """Returns the stored credentials JSON string for this user, or None if
    they haven't granted Calendar access yet (e.g. they signed in before
    this feature existed and haven't signed in again since)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT calendar_token FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def touch_last_seen(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()


def _maybe_touch_last_seen(user):
    """Called on every authenticated request (see login_required) to keep
    last_seen_at roughly current for the admin dashboard's "active now"
    count -- throttled to LAST_SEEN_THROTTLE so a person clicking around
    the app doesn't cause a database write on every single page load.
    Never raises: a failure here is purely cosmetic (a slightly stale
    "active now" number) and should never break the actual page someone is
    trying to load."""
    try:
        last = user.get("last_seen_at")
        if last:
            last_dt = datetime.fromisoformat(last)
            if datetime.now() - last_dt < LAST_SEEN_THROTTLE:
                return
        touch_last_seen(user["id"])
    except Exception:
        pass


def is_admin(user):
    """True only for the one account configured as ADMIN_EMAIL -- gates the
    /admin dashboard and the sidebar link to it (see app.py and base.html).
    Case-insensitive, since Google emails are effectively case-insensitive
    and a slightly different capitalization shouldn't lock the admin out of
    their own dashboard. Every other account is unaffected either way --
    this never changes what a non-admin can see or do anywhere else in the
    app."""
    return bool(user and user.get("email") and user["email"].strip().lower() == ADMIN_EMAIL)


# ------------------------------------------------------- decorator & g

def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("auth.login_page"))
        user = get_user(user_id)
        if user is None:
            # Account vanished (e.g. db reset) but the browser still has an
            # old session cookie — treat it like a logged-out visitor.
            session.clear()
            return redirect(url_for("auth.login_page"))
        g.current_user = user
        _maybe_touch_last_seen(user)
        return view_func(*args, **kwargs)
    return wrapped


def current_user():
    return getattr(g, "current_user", None)


# ------------------------------------------------------------- the flow

def _build_flow(state=None, code_verifier=None):
    # google-auth-oauthlib generates a fresh random PKCE code_verifier by
    # default (autogenerate_code_verifier=True) whenever authorization_url()
    # is called on a Flow instance, and bakes its hash into the authorization
    # URL as code_challenge. Google's token endpoint then requires the exact
    # same code_verifier back during fetch_token(). Since the callback route
    # builds a brand new Flow object (a separate request from /auth/google/login),
    # we have to carry that value across ourselves via the session, then hand
    # it back in here -- otherwise fetch_token() sends no code_verifier at all
    # and Google rejects the exchange with "Missing code verifier".
    kwargs = {"code_verifier": code_verifier} if code_verifier else {}
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
        **kwargs,
    )


@bp.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("login.html")


@bp.route("/auth/google/login")
def google_login():
    flow = _build_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        # Deliberately NOT include_granted_scopes=True: this sign-in flow
        # asks for exactly the scopes in SCOPES above (now including
        # Calendar), and asking Google to also fold in anything separately
        # granted elsewhere is what caused it to return a wider scope than
        # requested in the first place. OAUTHLIB_RELAX_TOKEN_SCOPE above is
        # kept as a safety net in case Google still normalizes/reorders the
        # scope string anyway.
        #
        # "consent" forces Google to show the consent screen and issue a
        # refresh_token every time, instead of silently skipping it for a
        # returning user -- which matters here because calendar.readonly is
        # a new scope being added to an existing sign-in flow. Without this,
        # anyone who already has an active session/consent on file could be
        # sent straight through without ever being asked for Calendar access
        # or getting a refresh_token stored for it.
        prompt="consent select_account",
    )
    session["oauth_state"] = state
    session["oauth_code_verifier"] = flow.code_verifier
    return redirect(authorization_url)


@bp.route("/auth/google/callback")
def google_callback():
    expected_state = session.pop("oauth_state", None)
    code_verifier = session.pop("oauth_code_verifier", None)
    returned_state = request.args.get("state")
    if not expected_state or expected_state != returned_state:
        return render_template(
            "error.html",
            title="Sign-in didn't go through",
            message="That sign-in link had expired or didn't match. Please try signing in again.",
            back_href="/login",
            back_label="Back to sign in",
        ), 400

    flow = _build_flow(state=expected_state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials
    info = google_id_token.verify_oauth2_token(
        credentials.id_token,
        google_requests.Request(),
        credentials.client_id,
        # By default this allows ZERO clock drift between this machine and
        # Google's servers when checking the token's issued-at time -- so
        # even a one-second-slow system clock fails sign-in with
        # "Token used too early". A small allowance here is standard
        # practice; it doesn't loosen security, it just tolerates the kind
        # of tiny drift that happens on virtually every real computer.
        clock_skew_in_seconds=10,
    )

    user = get_or_create_user(
        google_sub=info["sub"],
        email=info.get("email"),
        name=info.get("name") or (info.get("email") or "").split("@")[0] or "there",
    )

    # Store THIS user's Calendar credentials under their own account, so the
    # Deadline Plan page only ever reads the calendar of whoever is signed
    # in -- not a single shared token file.
    save_calendar_credentials(user["id"], credentials.to_json())

    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return redirect(url_for("index"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


# --------------------------------------------------------- settings page

@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    saved = False
    if request.method == "POST":
        update_leetcode_username(user["id"], request.form.get("leetcode_username", ""))
        user = get_user(user["id"])
        g.current_user = user
        saved = True
    return render_template(
        "settings.html",
        active_step="settings",
        leetcode_username=user["leetcode_username"] or "",
        saved=saved,
        daily_run_time=scheduler.get_saved_time(),
    )


@bp.route("/settings/theme", methods=["POST"])
@login_required
def settings_theme():
    """Persists the light/dark toggle and/or colour-theme picker to THIS
    account (see update_theme_palette) instead of only to the browser's
    localStorage -- so switching which Google account is signed in on the
    same browser no longer carries one person's theme choice over to
    another's. Called via fetch() from app.js right when either control is
    used, not via a full page form submit, so it returns plain JSON rather
    than a redirect."""
    data = request.get_json(silent=True) or {}
    theme = data.get("theme")
    palette = data.get("palette")

    if theme is not None and theme not in ("light", "dark"):
        return {"ok": False, "error": "invalid theme"}, 400
    if palette is not None and (not isinstance(palette, str) or not palette.strip()):
        return {"ok": False, "error": "invalid palette"}, 400

    user = current_user()
    update_theme_palette(user["id"], theme=theme, palette=palette)
    g.current_user = get_user(user["id"])
    return {"ok": True}
