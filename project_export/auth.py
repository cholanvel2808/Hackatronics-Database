"""
Login/session for the merged dispatcher + driver app.

driverweb.py had no real authentication at all (just an st.radio toggle
between views, and a driver picked from a dropdown to "view as"). This
replaces that with a real login against the app_users table
(schema/001_dispatch_integration.sql), backed by bcrypt password hashing
and a plain st.session_state session (no cookies/JWT — fine for a
hackathon demo; it just means a hard refresh/new tab logs you out again,
which is a known, accepted limitation, not solved here).

Import-safe with no Postgres/bcrypt present, same posture as db.py: every
function degrades to returning None/False rather than crashing the app.
"""

import streamlit as st

try:
    import bcrypt

    _BCRYPT_AVAILABLE = True
except ImportError:
    _BCRYPT_AVAILABLE = False

import db

SESSION_KEY = "auth_user"  # st.session_state[SESSION_KEY] = {"user_id","username","role","driver_id"}


def hash_password(plain_password):
    if not _BCRYPT_AVAILABLE:
        return None
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password, password_hash):
    if not _BCRYPT_AVAILABLE:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def authenticate(username, password):
    """Looks up app_users, verifies the password, and returns
    {"user_id","username","role","driver_id"} on success, else None.
    Never raises — a DB or bcrypt problem just means login fails."""
    if not _BCRYPT_AVAILABLE:
        return None
    conn = db._get_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, username, password_hash, role, driver_id "
                "FROM app_users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        return None
    if row is None:
        return None
    user_id, uname, password_hash, role, driver_id = row
    if not verify_password(password, password_hash):
        return None
    return {"user_id": user_id, "username": uname, "role": role, "driver_id": driver_id}


def current_user():
    return st.session_state.get(SESSION_KEY)


def log_in(user):
    st.session_state[SESSION_KEY] = user


def log_out():
    st.session_state.pop(SESSION_KEY, None)


def login_gate():
    """Call at the very top of the app, before rendering anything else.
    Renders a login form and stops the script if nobody's logged in yet;
    otherwise just returns the logged-in user dict — the "logged in as ...
    / log out" chrome lives in test.py now (a settings-gear popover next
    to the page title), not a sidebar, so this function no longer renders
    anything for the already-logged-in case."""
    user = current_user()
    if user is not None:
        return user

    st.markdown(
        "<h1 style='text-align: center; font-size: 2rem; margin-top: 14vh;'>Sign In</h1>",
        unsafe_allow_html=True,
    )
    if not _BCRYPT_AVAILABLE or not db.db_available():
        st.info(
            "Login is disabled — this needs both `bcrypt` installed and Postgres "
            "reachable (DB_INTEGRATION_ENABLED / DATABASE_URL), neither of which "
            "is set up in this dev environment yet."
        )
        st.stop()

    _, col_mid, _ = st.columns([1, 1.2, 1])
    with col_mid:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", type="primary")
        if submitted:
            user = authenticate(username, password)
            if user is None:
                st.error("Invalid username or password.")
            else:
                log_in(user)
                st.rerun()
    st.stop()


def require_role(role):
    """Call at the top of a role-gated section. Shows an error and stops
    if the logged-in user isn't that role — use after login_gate()."""
    user = current_user()
    if user is None or user["role"] != role:
        st.error(f"This section is only available to {role.lower()}s.")
        st.stop()
    return user
