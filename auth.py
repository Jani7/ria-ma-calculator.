"""
Email + 6-digit-OTP sign-in for the RIA M&A Calculator, via Supabase Auth.

Flow (all in the sidebar, all session keys _auth_-prefixed):
    signed_out  -> email input + "Send code"   (sign_in_with_otp)
    code_sent   -> 6-digit input + "Verify"    (verify_otp, type="email")
    verified    -> "Signed in as x@y" caption + sign-out

Deliberately OTP codes, not magic-link redirects — a redirect bounces the
user out of the Streamlit session and lands them in a fresh one, losing all
widget state. A typed code keeps the session intact.

When st.secrets lacks SUPABASE_URL / SUPABASE_KEY this module renders
NOTHING and returns None, so the app degrades gracefully: share links keep
working, saving is simply absent. Mirrors app.py's guarded st.secrets
pattern (secrets access may raise entirely in some local-dev contexts).

NOTE for the owner: in the Supabase dashboard the "Magic Link" and "Confirm
signup" email templates must include {{ .Token }} (the 6-digit code) —
otherwise Supabase emails a link instead of a code. See the integration
notes checklist.
"""

import re
import time

import streamlit as st

_RESEND_COOLDOWN_SEC = 60
# Loose sanity check only — Supabase does the real validation. This just
# catches obvious typos before we burn a rate-limited email send.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _supabase_creds() -> "tuple[str, str] | None":
    """Graceful-absence secrets access, same style as app.py's ADV_DATA_*
    handling: missing secrets (or no secrets file at all) must never raise."""
    try:
        if "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
            url = str(st.secrets["SUPABASE_URL"]).strip()
            key = str(st.secrets["SUPABASE_KEY"]).strip()
            if url and key:
                return url, key
    except Exception:
        pass
    return None


def auth_available() -> bool:
    """True when Supabase secrets are configured (auth UI should exist)."""
    return _supabase_creds() is not None


@st.cache_resource(show_spinner=False)
def _get_client():
    """Cached Supabase client, or None if secrets/package are absent.
    Lazy import so the app runs fine without `supabase` installed."""
    creds = _supabase_creds()
    if creds is None:
        return None
    try:
        from supabase import create_client

        return create_client(*creds)
    except Exception:
        return None


def get_verified_email() -> "str | None":
    """The OTP-verified email for this session, or None."""
    if st.session_state.get("_auth_verified"):
        return st.session_state.get("_auth_email") or None
    return None


def _send_code(client, email: str) -> bool:
    """Fire the OTP email. should_create_user means first-time visitors get
    an account implicitly — no separate sign-up step."""
    try:
        client.auth.sign_in_with_otp(
            {"email": email, "options": {"should_create_user": True}}
        )
    except Exception:
        st.sidebar.error(
            "Couldn't send the code — double-check the address and try again."
        )
        return False
    st.session_state["_auth_last_send_ts"] = time.time()
    return True


def render_auth_widget() -> "str | None":
    """Compact sidebar auth flow. Renders nothing (and returns None) when
    Supabase isn't configured. Returns the verified email once signed in."""
    if not auth_available():
        return None
    client = _get_client()
    if client is None:
        # Secrets exist but the client couldn't be built (package missing or
        # bad URL). Stay quiet rather than shout at every visitor.
        return None

    stage = st.session_state.get("_auth_stage", "signed_out")

    # -- Signed in ---------------------------------------------------------
    if stage == "verified" and st.session_state.get("_auth_verified"):
        email = st.session_state.get("_auth_email", "")
        st.sidebar.caption(f"Signed in as {email}")
        if st.sidebar.button("Sign out", key="_auth_signout_btn", use_container_width=True):
            try:
                client.auth.sign_out()
            except Exception:
                pass  # local session teardown is what actually matters
            for k in ("_auth_stage", "_auth_verified", "_auth_email", "_auth_last_send_ts"):
                st.session_state.pop(k, None)
            # Saved-deals cache belongs to the signed-out user — drop it.
            st.session_state.pop("_persist_deals_cache", None)
            st.session_state.pop("_persist_deals_cache_email", None)
            st.rerun()
        return email or None

    # -- Waiting on the 6-digit code ----------------------------------------
    if stage == "code_sent":
        email = st.session_state.get("_auth_email", "")
        st.sidebar.caption(f"We emailed a 6-digit code to {email}.")
        code = st.sidebar.text_input(
            "6-digit code", key="_auth_code_input", max_chars=6, placeholder="123456"
        )
        if st.sidebar.button("Verify", key="_auth_verify_btn", type="primary",
                             use_container_width=True):
            ok = False
            try:
                res = client.auth.verify_otp(
                    {"email": email, "token": (code or "").strip(), "type": "email"}
                )
                ok = getattr(res, "user", None) is not None
            except Exception:
                ok = False
            if ok:
                st.session_state["_auth_verified"] = True
                st.session_state["_auth_stage"] = "verified"
                st.rerun()
            else:
                st.sidebar.error("That code didn't match — try again or resend.")

        # Resend, softly rate-limited. Not a security control (Supabase has
        # its own server-side limits) — just keeps trigger-happy clicking
        # from tripping those limits and confusing the user.
        elapsed = time.time() - st.session_state.get("_auth_last_send_ts", 0.0)
        remaining = int(_RESEND_COOLDOWN_SEC - elapsed)
        if remaining > 0:
            st.sidebar.caption(f"Didn't get it? You can resend in {remaining}s.")
        elif st.sidebar.button("Resend code", key="_auth_resend_btn", use_container_width=True):
            if _send_code(client, email):
                st.sidebar.caption("Sent — check your inbox (and spam).")
        if st.sidebar.button("Use a different email", key="_auth_change_btn",
                             use_container_width=True):
            st.session_state["_auth_stage"] = "signed_out"
            st.rerun()
        return None

    # -- Signed out ----------------------------------------------------------
    email = st.sidebar.text_input(
        "Email",
        key="_auth_email_input",
        value=st.session_state.get("_auth_email", ""),
        placeholder="you@firm.com",
        help="Sign in to save deals. We email you a 6-digit code — no password.",
    )
    if st.sidebar.button("Send code", key="_auth_send_btn", use_container_width=True):
        cleaned = (email or "").strip().lower()
        if not _EMAIL_RE.match(cleaned):
            st.sidebar.error("That doesn't look like an email address — try again.")
        elif _send_code(client, cleaned):
            st.session_state["_auth_email"] = cleaned
            st.session_state["_auth_stage"] = "code_sent"
            st.rerun()
    return None
