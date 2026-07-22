"""
chatbot/routes.py — Caryanams Support Chatbot
────────────────────────────────────────────────────────────────────────────
Flask blueprint powering the floating chat widget
(static/js/chatbot.js + templates/_chatbot_widget.html).

Response policy (enforced via system prompt + server-side grounding):
  • Answer only the user's question. Keep replies concise; one sentence when
    one sentence will do. Give detail only when the user asks for detail.
  • No unnecessary explanations, suggestions, or unrelated information.

Answer priority (highest first):
  1. Application DATABASE / APIs
       - A logged-in DEALER's OWN inventory ("my car", "my mileage",
         "my registration number", etc.) — looked up from Vehicle where
         dealer_id == the logged-in dealer's id.
       - Public, approved, available inventory (browsing visitors) via
         db.vehicles_public — unchanged from the original widget.
  2. Verified application knowledge (how Caryanams works: approval, dealers,
     contacting via inquiry button, etc.).
  3. General knowledge (e.g. "What is ABS?", "How does an EV work?") — answered
     freely from the model's own knowledge.
  4. If none of the above can answer it, direct the user to support with the
     exact unknown-answer template.

Notes:
  • KYC handling is intentionally NOT part of the chatbot — the existing
    utils/kyc_engine + DealerKYC pipeline continues to own that.
  • Uses Groq's OpenAI-compatible /chat/completions endpoint via `requests`.
  • Stateless on the server: the client sends recent turn history each request.

Setup (.env):
    GROQ_API_KEY=gsk_your_key_here
    GROQ_MODEL=llama-3.3-70b-versatile     (optional, this is the default)
    SUPPORT_PHONE_NUMBER=+91-XXXXXXXXXX     (shown in unknown-answer replies)

Registered in app.py:
    from chatbot.routes import chatbot_bp
    app.register_blueprint(chatbot_bp, url_prefix='/chatbot')
"""

import os
import re
import requests
from flask import Blueprint, request, jsonify, current_app, g

from db import vehicles_public

chatbot_bp = Blueprint('chatbot', __name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_SUPPORT_PHONE = "+91-XXXXXXXXXX"

FALLBACK_MAKES = [
    'Maruti', 'Suzuki', 'Hyundai', 'Tata', 'Mahindra', 'Honda', 'Toyota',
    'Kia', 'Ford', 'Volkswagen', 'Skoda', 'Renault', 'Nissan', 'MG',
    'BMW', 'Audi', 'Mercedes', 'Jeep', 'Datsun', 'Chevrolet',
]

# Words that signal the user WANTS a detailed answer.
DETAIL_TRIGGERS = (
    'why', 'how', 'explain', 'guide', 'step by step', 'step-by-step',
    'detail', 'details', 'elaborate', 'describe', 'tell me more',
    'more information', 'more info', 'in detail',
)

# Words/phrases that signal the user EXPLICITLY wants previous conversation
# used (continuation, comparison, or reference to an earlier answer). Only when
# one of these is present do we keep prior turns; otherwise every message is
# treated as a brand-new, independent request (stateless behavior).
CONTINUATION_TRIGGERS = (
    'continue', 'carry on', 'go on', 'keep going',
    'previous', 'previously', 'earlier', 'before',
    'as discussed', 'as mentioned', 'as you said', 'you said',
    'last answer', 'last response', 'last question', 'last time',
    'above answer', 'above response', 'your previous', 'that answer',
    'build on', 'building on', 'based on what you said', 'based on the above',
    'compare', 'comparison', 'compared to', 'versus', ' vs ', 'vs.',
    'difference between', 'summarize our', 'summarise our',
    'summary of our', 'recap', 'refer to', 'referring to',
    # Common Hindi/Hinglish equivalents
    'pehle', 'pichhle', 'pichle', 'jaisa btaya', 'jaisa kaha',
    'aage batao', 'continue karo', 'jaari rakho',
)


def _wants_continuation(message):
    m = ' ' + message.lower() + ' '
    return any(t in m for t in CONTINUATION_TRIGGERS)


# Words that signal the user is asking about their OWN records.
PERSONAL_TRIGGERS = (
    ' my ', 'my car', 'my vehicle', 'my mileage', 'my registration',
    'my rc', 'my listing', 'my inventory', 'my price', 'my stock',
    'mera ', 'meri ', 'apni gaadi', 'meri gaadi',
)


def _wants_detail(message):
    m = message.lower()
    return any(t in m for t in DETAIL_TRIGGERS)


def _is_personal(message):
    m = ' ' + message.lower() + ' '
    return any(t in m for t in PERSONAL_TRIGGERS)


def _current_user():
    """Return the logged-in user object if available, else None.

    This app sets g.user per-request (see app.py context processor); we also
    fall back to flask_login.current_user if present. Either may be a dealer.
    """
    u = getattr(g, 'user', None)
    if u is not None:
        return u
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            return current_user
    except Exception:
        pass
    return None


def _is_dealer(user):
    return bool(user) and getattr(user, 'role', None) == 'dealer'


def _build_system_prompt(support_phone, context_block, wants_detail, is_dealer):
    unknown_template = (
        "I'm sorry, but I couldn't find that information. Please contact our "
        f"support team at {support_phone} for further assistance."
    )

    length_rule = (
        "The user asked for details / an explanation, so a fuller answer is "
        "appropriate here. Stay on-topic; do not pad with fluff."
        if wants_detail else
        "Keep the reply concise. If the question can be answered in one "
        "sentence, answer in one sentence. Do NOT add explanations, "
        "suggestions, or unrelated information the user did not ask for."
    )

    who = (
        "The person chatting is a LOGGED-IN DEALER. \"My car\", \"my mileage\", "
        "\"my registration number\", \"my listings\" etc. refer to THEIR OWN "
        "inventory, provided below under \"YOUR INVENTORY\"."
        if is_dealer else
        "The person chatting is a website visitor browsing dealer inventory. "
        "They do not own any car in the system; \"my car\" style questions do "
        "not apply — help them browse the public inventory below instead."
    )

    return f"""You are the AI customer-support assistant for Caryanams, an Indian used & new
car marketplace and dealer management platform. Answer using the application's
data first, then verified app knowledge, then general knowledge.

WHO YOU ARE TALKING TO
{who}

GENERAL RULES
- Answer only the user's CURRENT question. Treat it as a brand-new, independent
  request. Do NOT mix in, reuse, or merge information from earlier questions.
- If the topic has changed from a previous message, ignore the earlier
  conversation completely.
- Do NOT reference earlier messages — never say "as discussed earlier",
  "previously", "as mentioned before", "based on our previous conversation",
  or anything similar — UNLESS the user explicitly asked you to continue,
  compare, summarize, or refer back to an earlier answer.
- If the current question is ambiguous because it seems to depend on an earlier
  message (e.g. "What is its mileage?" with no vehicle named), ask a short
  clarification question instead of assuming it refers to a previous topic.
- If the same question is asked again, give the same appropriate answer.
- {length_rule}
- Do NOT repeat information or add anything that was not requested.
- Reply in the SAME language and script the user's latest message uses
  (English, Hindi/Devanagari, Hinglish, Gujarati, Tamil, etc.). If they mix
  languages, mix the same natural way. Do not default to English.

ANSWER PRIORITY (follow in order every time)
1. APPLICATION DATA — use the records in the context block below. If the
   answer is there, give it accurately. Never invent or guess customer or
   vehicle data. Prices are in Indian Rupees (₹); large amounts may also be
   given in lakhs (1 lakh = ₹100,000).
2. VERIFIED APP KNOWLEDGE — how Caryanams works: dealers list their own
   inventory, listings go through admin approval, buyers use the
   "Contact Dealer"/inquiry button on a listing to reach a dealer. You cannot
   book test drives, place holds, or take payments yourself.
3. GENERAL KNOWLEDGE — for general questions not specific to this app
   (e.g. "What is ABS?", "How does an EV work?", "What is engine oil?"),
   answer from your own knowledge.
4. If the answer is not in the data, cannot be determined, and is not
   something general knowledge can answer, reply with EXACTLY this and nothing
   else: "{unknown_template}"

Never fabricate customer data or vehicle information. Never answer "I don't
know" before checking the data provided below.

{context_block}
"""


def _get_makes():
    try:
        from models import Vehicle
        rows = Vehicle.query.with_entities(Vehicle.make).distinct().all()
        makes = [r[0] for r in rows if r[0]]
        return makes or FALLBACK_MAKES
    except Exception:
        return FALLBACK_MAKES


def _detect_make(text, makes):
    text_l = text.lower()
    for make in makes:
        if make.lower() in text_l:
            return make
    return ''


def _detect_fuel(text):
    text_l = text.lower()
    mapping = {
        'petrol': 'Petrol', 'diesel': 'Diesel', 'electric': 'Electric',
        'ev': 'Electric', 'cng': 'CNG', 'hybrid': 'Hybrid',
    }
    for k, v in mapping.items():
        if k in text_l:
            return v
    return ''


def _detect_transmission(text):
    text_l = text.lower()
    if 'automatic' in text_l or ' amt' in text_l or 'auto ' in text_l:
        return 'Automatic'
    if 'manual' in text_l:
        return 'Manual'
    return ''


def _detect_condition(text):
    text_l = text.lower()
    if 'new car' in text_l or 'brand new' in text_l:
        return 'new'
    if 'used' in text_l or 'second hand' in text_l or 'pre-owned' in text_l or 'preowned' in text_l:
        return 'used'
    return ''


def _rupees(match_num, unit):
    n = float(match_num)
    if unit and unit.startswith('lakh'):
        n *= 100000
    elif unit and unit.startswith('cr'):
        n *= 10000000
    elif unit == 'k':
        n *= 1000
    return n


_PRICE_RE = re.compile(
    r'(?:under|below|less than|within|budget of|up to)\s*(?:₹|rs\.?|inr)?\s*'
    r'(\d+(?:\.\d+)?)\s*(lakh|lakhs|lac|crore|cr|k)?',
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(lakh|lakhs|lac|crore|cr|k)?\s*(?:-|to)\s*'
    r'(\d+(?:\.\d+)?)\s*(lakh|lakhs|lac|crore|cr|k)?',
    re.IGNORECASE,
)


def _detect_price_range(text):
    m = _RANGE_RE.search(text)
    if m:
        lo = _rupees(m.group(1), (m.group(2) or '').lower())
        hi = _rupees(m.group(3), (m.group(4) or m.group(2) or '').lower())
        return (min(lo, hi), max(lo, hi))
    m = _PRICE_RE.search(text)
    if m:
        hi = _rupees(m.group(1), (m.group(2) or '').lower())
        return (None, hi)
    return (None, None)


def _detect_year(text):
    m = re.search(r'\b(20[0-3]\d)\b', text)
    return int(m.group(1)) if m else None


def _format_vehicle_line(v):
    price = f"₹{v['price']:,.0f}" if v.get('price') else 'Price on request'
    bits = [f"{v['year']} {v['make']} {v['model']}"]
    if v.get('variant'):
        bits.append(v['variant'])
    line = " ".join(bits)
    extras = []
    if v.get('fuel_type'):
        extras.append(v['fuel_type'])
    if v.get('transmission'):
        extras.append(v['transmission'])
    if v.get('mileage') is not None:
        extras.append(f"{v['mileage']:,} km driven")
    extra_str = f" ({', '.join(extras)})" if extras else ""
    return f"- {line}{extra_str} — {price} [listing id {v['id']}]"


def _format_dealer_vehicle_line(v):
    """Fuller line for a dealer's own car — includes RC / registration etc."""
    price = f"₹{v['price']:,.0f}" if v.get('price') else 'Price on request'
    bits = [f"{v['year']} {v['make']} {v['model']}"]
    if v.get('variant'):
        bits.append(v['variant'])
    head = " ".join(bits)
    fields = []
    if v.get('registration_number'):
        fields.append(f"reg no {v['registration_number']}")
    if v.get('mileage') is not None:
        fields.append(f"{v['mileage']:,} km")
    if v.get('fuel_type'):
        fields.append(v['fuel_type'])
    if v.get('transmission'):
        fields.append(v['transmission'])
    if v.get('color'):
        fields.append(v['color'])
    if v.get('status'):
        fields.append(f"status: {v['status']}")
    if v.get('approval_status'):
        fields.append(f"approval: {v['approval_status']}")
    detail = "; ".join(fields)
    return f"- {head} — {price}" + (f" ({detail})" if detail else "") + f" [listing id {v['id']}]"


def _find_matching_vehicles(message):
    """Public inventory matches (browsing visitors)."""
    makes = _get_makes()
    make = _detect_make(message, makes)
    fuel = _detect_fuel(message)
    transmission = _detect_transmission(message)
    condition = _detect_condition(message)
    min_price, max_price = _detect_price_range(message)
    year = _detect_year(message)

    result = vehicles_public(
        make=make, fuel=fuel, transmission=transmission, condition=condition,
        min_price=min_price, max_price=max_price,
        min_year=year, max_year=None,
        sort='newest', page=1, per_page=5,
    )
    return result['items'], result['total']


def _find_dealer_vehicles(dealer_id, message, limit=15):
    """A dealer's OWN vehicles, optionally narrowed by hints in the message."""
    try:
        from models import Vehicle
    except Exception:
        return [], 0

    q = Vehicle.query.filter(Vehicle.dealer_id == dealer_id)

    makes = _get_makes()
    make = _detect_make(message, makes)
    fuel = _detect_fuel(message)
    transmission = _detect_transmission(message)
    year = _detect_year(message)

    # Registration-number lookup (e.g. "MH12AB1234")
    reg = re.search(r'\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{1,4})\b', message.upper())

    if make:
        q = q.filter(Vehicle.make == make)
    if fuel:
        q = q.filter(Vehicle.fuel_type == fuel)
    if transmission:
        q = q.filter(Vehicle.transmission == transmission)
    if year:
        q = q.filter(Vehicle.year == year)
    if reg:
        reg_clean = reg.group(1).replace(' ', '')
        q = q.filter(Vehicle.registration_number.ilike(f"%{reg_clean}%"))

    total = q.count()
    rows = q.order_by(Vehicle.created_at.desc()).limit(limit).all()
    items = [r.to_dict() for r in rows]
    return items, total


def _call_groq(messages, api_key, model, max_tokens):
    resp = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _unknown_reply(support_phone):
    return (
        "I'm sorry, but I couldn't find that information. Please contact our "
        f"support team at {support_phone} for further assistance."
    )


@chatbot_bp.route('/api/chat', methods=['POST'])
def chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get('message') or '').strip()
    history = payload.get('history') or []  # [{role: 'user'|'assistant', content: '...'}]

    if not message:
        return jsonify({'error': 'Message is required.'}), 400
    if len(message) > 1000:
        message = message[:1000]

    support_phone = (
        current_app.config.get('SUPPORT_PHONE_NUMBER')
        or os.environ.get('SUPPORT_PHONE_NUMBER')
        or DEFAULT_SUPPORT_PHONE
    )

    api_key = current_app.config.get('GROQ_API_KEY') or os.environ.get('GROQ_API_KEY', '')
    if not api_key:
        return jsonify({'reply': _unknown_reply(support_phone), 'vehicles': []})

    model = current_app.config.get('GROQ_MODEL') or os.environ.get('GROQ_MODEL', DEFAULT_MODEL)

    user = _current_user()
    is_dealer = _is_dealer(user)
    personal = _is_personal(message)

    vehicles = []           # cars rendered as clickable cards in the widget
    context_block = ""

    # ── Priority 1a: a logged-in dealer asking about their OWN records ──
    if is_dealer and personal:
        try:
            own, own_total = _find_dealer_vehicles(user.id, message)
        except Exception:
            own, own_total = [], 0
        if own:
            context_block = (
                f"YOUR INVENTORY ({own_total} vehicle(s) on your account, "
                f"showing up to {len(own)}):\n"
                + "\n".join(_format_dealer_vehicle_line(v) for v in own)
            )
            vehicles = own
        else:
            context_block = (
                "YOUR INVENTORY: no vehicles on your account match this query "
                "(you may not have any listings yet, or none match the filters)."
            )
    else:
        # ── Priority 1b: public inventory for visitors / general browsing ──
        try:
            pub, pub_total = _find_matching_vehicles(message)
        except Exception:
            pub, pub_total = [], 0
        if pub:
            context_block = (
                f"PUBLIC INVENTORY MATCHES ({pub_total} total, showing top {len(pub)}):\n"
                + "\n".join(_format_vehicle_line(v) for v in pub)
            )
            vehicles = pub
        else:
            context_block = "PUBLIC INVENTORY MATCHES: none found for this query."

    wants_detail = _wants_detail(message)
    system_content = _build_system_prompt(support_phone, context_block, wants_detail, is_dealer)
    max_tokens = 600 if wants_detail else 200

    messages = [{"role": "system", "content": system_content}]
    # STATELESS BY DEFAULT: only carry prior turns into the model when the user
    # EXPLICITLY asks to continue, compare, or reference the earlier discussion.
    # Otherwise every message is answered as a brand-new, independent request so
    # information from previous questions never bleeds into the current answer.
    if _wants_continuation(message):
        for turn in history[-8:]:
            role = turn.get('role')
            content = (turn.get('content') or '')[:1000]
            if role in ('user', 'assistant') and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    try:
        reply = _call_groq(messages, api_key, model, max_tokens)
    except requests.exceptions.HTTPError as e:
        current_app.logger.error(f"Groq API error: {e} — {getattr(e.response, 'text', '')}")
        return jsonify({'reply': _unknown_reply(support_phone), 'vehicles': []}), 502
    except Exception as e:
        current_app.logger.error(f"Chatbot error: {e}")
        return jsonify({'reply': _unknown_reply(support_phone), 'vehicles': []}), 500

    return jsonify({'reply': reply, 'vehicles': vehicles})
