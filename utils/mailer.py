"""
utils/mailer.py
────────────────────────────────────────────────────────────────────────────
Minimal SMTP email sender used for real password-reset OTP emails.

Configure via environment variables (put these in your .env / server env,
never hard-code them in source):

    MAIL_SMTP_HOST   e.g. smtp.gmail.com
    MAIL_SMTP_PORT   e.g. 587
    MAIL_USERNAME    the Gmail address you are sending from
    MAIL_APP_PASSWORD  a 16-character Gmail "App Password"
                        (NOT your normal Gmail password — generate one at
                        https://myaccount.google.com/apppasswords, requires
                        2-Step Verification to be turned on)
    MAIL_FROM_NAME   display name shown to the recipient, e.g. "Caryanams DMS"

If MAIL_USERNAME / MAIL_APP_PASSWORD are not set, send_email() will raise
RuntimeError so the calling route can fail loudly instead of silently
pretending an email went out.
"""

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _smtp_config():
    return {
        'host': os.environ.get('MAIL_SMTP_HOST', 'smtp.gmail.com'),
        'port': int(os.environ.get('MAIL_SMTP_PORT', 587)),
        'username': os.environ.get('MAIL_USERNAME', ''),
        'password': os.environ.get('MAIL_APP_PASSWORD', ''),
        'from_name': os.environ.get('MAIL_FROM_NAME', 'Caryanams DMS'),
    }


def send_email(to_email, subject, html_body, text_body=None):
    """
    Send a real email over SMTP (TLS). Raises RuntimeError with a clear
    message if SMTP credentials are not configured, or smtplib.SMTPException
    subclasses if sending itself fails (bad login, network, etc).
    """
    cfg = _smtp_config()
    if not cfg['username'] or not cfg['password']:
        raise RuntimeError(
            'Email is not configured. Set MAIL_USERNAME and MAIL_APP_PASSWORD '
            'environment variables (see utils/mailer.py docstring).'
        )

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"{cfg['from_name']} <{cfg['username']}>"
    msg['To'] = to_email

    if text_body:
        msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg['host'], cfg['port']) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(cfg['username'], cfg['password'])
        server.sendmail(cfg['username'], [to_email], msg.as_string())


def send_otp_email(to_email, otp, user_name=None, valid_minutes=10):
    """Convenience wrapper: sends a formatted password-reset OTP email."""
    greeting = f"Hi {user_name}," if user_name else "Hi,"
    subject = "Your Caryanams DMS password reset code"

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto">
      <h2 style="color:#002366">Password Reset Code</h2>
      <p>{greeting}</p>
      <p>Use the code below to reset your Caryanams DMS password. This code
         is valid for {valid_minutes} minutes.</p>
      <div style="font-size:32px;font-weight:700;letter-spacing:6px;
                  background:#f1f5f9;padding:16px 24px;border-radius:10px;
                  text-align:center;color:#0f172a">{otp}</div>
      <p style="color:#64748b;font-size:13px;margin-top:20px">
        If you did not request this, you can safely ignore this email —
        your password will not be changed.
      </p>
    </div>
    """
    text_body = (
        f"{greeting}\n\n"
        f"Your Caryanams DMS password reset code is: {otp}\n"
        f"This code is valid for {valid_minutes} minutes.\n\n"
        f"If you did not request this, ignore this email."
    )
    send_email(to_email, subject, html_body, text_body)
