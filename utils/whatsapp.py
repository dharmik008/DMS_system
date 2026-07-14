"""
utils/whatsapp.py
────────────────────────────────────────────────────────────────────────────
Customer inquiry confirmation / auto-reply via the official Meta WhatsApp
Cloud API (https://developers.facebook.com/docs/whatsapp/cloud-api).

WHY A TEMPLATE MESSAGE (not a plain free-form text message)
──────────────────────────────────────────────────────────────────────────
WhatsApp only lets a business send free-form text to a customer INSIDE the
24-hour "customer service window" that opens once the customer has messaged
that WhatsApp number directly. The confirmation here is triggered by a WEB
FORM submission (a car inquiry / contact form on the site), not by the
customer messaging WhatsApp first — so this is a business-initiated first
contact, which Meta requires to go through a pre-approved Message Template
(HSM). Sending a plain-text message here would fail outside that window.

ONE-TIME SETUP REQUIRED (before any message actually goes out)
──────────────────────────────────────────────────────────────────────────
1. A Meta for Developers app with the "WhatsApp" product added:
   https://developers.facebook.com/apps
2. A WhatsApp Business phone number connected to that app. The free test
   number is fine for development, but it can only message phone numbers
   you've explicitly added as testers in the dashboard.
3. A permanent token (create a System User in Meta Business Suite and
   generate a long-lived token for it — the 24-hour "temporary token"
   shown on the app dashboard will expire and break this in a day) and the
   Phone Number ID. Set them as environment variables (e.g. in .env):
       WHATSAPP_ENABLED=True
       WHATSAPP_ACCESS_TOKEN=EAAG...              (permanent System User token)
       WHATSAPP_PHONE_NUMBER_ID=123456789012345
       WHATSAPP_API_VERSION=v20.0                  (optional, this is the default)
       WHATSAPP_COUNTRY_CODE=91                     (used when a phone has no country code)
4. A message template created & APPROVED in WhatsApp Manager → Message
   Templates (Business Manager). Example body text for the template named
   "inquiry_confirmation" (category: UTILITY):
       Hi {{1}}, thanks for your interest in {{2}} on Caryanams! Our team
       will contact you shortly on this number.
   Then set:
       WHATSAPP_INQUIRY_TEMPLATE_NAME=inquiry_confirmation
       WHATSAPP_TEMPLATE_LANGUAGE=en_US             (must match the template's language)
   Template review by Meta usually takes minutes to a few hours.

Until WHATSAPP_ENABLED=True and a real token/template are configured, every
call below is a safe no-op — it logs a "skipped" row and returns quietly.
It NEVER raises into, or blocks, the caller's inquiry-submission request
(the send always happens in a background thread).
"""

import re
import threading

import requests
from flask import current_app


def _normalize_phone(raw_phone: str, default_country_code: str = "91"):
    """Best-effort conversion to the digits-only, country-code-prefixed
    format the WhatsApp Cloud API expects (no leading '+' or '00')."""
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", raw_phone)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:                        # bare local mobile number
        digits = default_country_code + digits
    if len(digits) < 10 or len(digits) > 15:      # not a plausible MSISDN
        return None
    return digits


def _log_message(inquiry_id, to_number, template_name, status, error=None, provider_message_id=None):
    """Best-effort audit row in whatsapp_message_log — logging failures
    must never propagate and break the caller."""
    try:
        from extensions import db
        from models import WhatsAppMessageLog
        db.session.add(WhatsAppMessageLog(
            inquiry_id=inquiry_id,
            to_number=to_number or '',
            message_type='template',
            template_name=template_name,
            status=status,
            error=error,
            provider_message_id=provider_message_id,
        ))
        db.session.commit()
    except Exception:
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass


def _send_template_sync(app, to_number, template_name, language_code, body_params, inquiry_id=None):
    """Does the actual HTTP call. Always run inside app.app_context() since
    this executes on a background thread, not the request thread."""
    with app.app_context():
        access_token    = app.config.get('WHATSAPP_ACCESS_TOKEN')
        phone_number_id = app.config.get('WHATSAPP_PHONE_NUMBER_ID')
        api_version     = app.config.get('WHATSAPP_API_VERSION', 'v20.0')

        if not app.config.get('WHATSAPP_ENABLED') or not access_token or not phone_number_id:
            app.logger.info(f'[whatsapp] Skipped (not configured yet) → {to_number}')
            _log_message(inquiry_id, to_number, template_name, 'skipped', error='WhatsApp not configured')
            return

        normalized = _normalize_phone(to_number, app.config.get('WHATSAPP_COUNTRY_CODE', '91'))
        if not normalized:
            app.logger.warning(f'[whatsapp] Invalid phone number, skipped → {to_number!r}')
            _log_message(inquiry_id, to_number, template_name, 'failed', error='Invalid phone number')
            return

        url = f'https://graph.facebook.com/{api_version}/{phone_number_id}/messages'
        payload = {
            'messaging_product': 'whatsapp',
            'to': normalized,
            'type': 'template',
            'template': {
                'name': template_name,
                'language': {'code': language_code},
                'components': (
                    [{'type': 'body', 'parameters': [{'type': 'text', 'text': str(p)} for p in body_params]}]
                    if body_params else []
                ),
            },
        }
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get('messages'):
                msg_id = data['messages'][0].get('id')
                app.logger.info(f'[whatsapp] Sent to {normalized} (template={template_name}) → {msg_id}')
                _log_message(inquiry_id, normalized, template_name, 'sent', provider_message_id=msg_id)
            else:
                err = (data.get('error') or {}).get('message', f'HTTP {resp.status_code}: {resp.text[:200]}')
                app.logger.warning(f'[whatsapp] Failed to {normalized}: {err}')
                _log_message(inquiry_id, normalized, template_name, 'failed', error=err)
        except Exception as exc:
            app.logger.error(f'[whatsapp] Exception sending to {normalized}: {exc}')
            _log_message(inquiry_id, normalized, template_name, 'failed', error=str(exc))


def send_inquiry_confirmation(name, phone, vehicle_label=None, inquiry_id=None):
    """
    Fire-and-forget: sends the customer a WhatsApp confirmation that their
    inquiry was received, using the approved inquiry-confirmation template.
    Runs on a background thread so it can never add latency to — or break —
    the request that's saving the inquiry (call this AFTER db.session.commit()).

    Parameters
    ----------
    name : str            customer's name (used as template variable {{1}})
    phone : str            customer's phone, any reasonable format (10-digit
                            local, +91XXXXXXXXXX, 0091..., etc.)
    vehicle_label : str    e.g. "Maruti Swift 2021" — used as {{2}}; pass
                            None for a general (non-vehicle) inquiry
    inquiry_id : int       the Inquiry.id this confirmation relates to, for
                            the audit log (optional)
    """
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        # Called outside a request/app context — nothing sensible to do.
        return

    template_name = app.config.get('WHATSAPP_INQUIRY_TEMPLATE_NAME', 'inquiry_confirmation')
    language_code = app.config.get('WHATSAPP_TEMPLATE_LANGUAGE', 'en_US')
    body_params = [name or 'there', vehicle_label or 'your inquiry']

    threading.Thread(
        target=_send_template_sync,
        args=(app, phone, template_name, language_code, body_params, inquiry_id),
        daemon=True,
    ).start()
