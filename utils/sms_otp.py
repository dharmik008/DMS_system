"""
utils/sms_otp.py
────────────────────────────────────────────────────────────────────────────
Sends the SAME 6-digit OTP that already goes out by email (see
utils/mailer.py) as an SMS too, using 2Factor.in's free-tier "SMS OTP" API.

Why 2Factor.in:
  - Free trial signup, no credit card required, gives a working API key
    instantly with a starting SMS credit balance — good enough for dev/
    testing and low-volume production use.
  - India-only numbers (10-digit), which matches how phone numbers are
    already stored/validated in auth/routes.py.
  - One HTTP GET call, no SDK needed (we already have `requests` in
    requirements.txt).

Setup:
  1. Sign up free at https://2factor.in  (Email OTP / SMS OTP product)
  2. Copy your API key from the dashboard.
  3. Add to your .env / environment:
        SMS_OTP_API_KEY=your_2factor_api_key_here
  4. If SMS_OTP_API_KEY is not set, send_sms_otp() raises RuntimeError —
     callers should catch this and treat SMS as best-effort (email OTP
     remains the required channel; see auth/routes.py usage).

This module never invents or stores its own OTP — the caller (auth/routes.py)
generates one `otp` value and passes it to BOTH send_otp_email() and
send_sms_otp() so the same code works on whichever channel the user checks.
"""

import os
import requests

SMS_API_BASE = "https://2factor.in/API/V1"
REQUEST_TIMEOUT = 10  # seconds


def _api_key():
    key = os.environ.get("SMS_OTP_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SMS is not configured. Set SMS_OTP_API_KEY in your environment "
            "(free key from https://2factor.in — see utils/sms_otp.py docstring)."
        )
    return key


def send_sms_otp(phone_digits: str, otp: str) -> dict:
    """
    Send `otp` via SMS to a 10-digit Indian mobile number using 2Factor's
    templated OTP SMS endpoint. Returns the parsed JSON response on success.
    Raises RuntimeError (missing key) or requests.RequestException /
    ValueError on network / API failure — callers should catch and decide
    whether SMS failure should block the flow or just be logged.
    """
    phone_digits = "".join(c for c in phone_digits if c.isdigit())[-10:]
    if len(phone_digits) != 10:
        raise ValueError(f"Expected a 10-digit Indian mobile number, got: {phone_digits!r}")

    api_key = _api_key()
    # OTP1 is 2Factor's built-in generic OTP template — no separate template
    # registration needed for basic use.
    url = f"{SMS_API_BASE}/{api_key}/SMS/{phone_digits}/{otp}/OTP1"

    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("Status") != "Success":
        raise RuntimeError(f"2Factor SMS API returned failure: {data}")
    return data
