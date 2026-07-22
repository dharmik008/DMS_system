from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, jsonify
from functools import wraps
import os
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _tz

_IST = _tz(_timedelta(hours=5, minutes=30))


def _now_ist():
    """Return current IST time as a naive datetime."""
    return _datetime.now(_IST).replace(tzinfo=None)

from db import (
    user_get_by_id, vehicles_get_by_dealer, vehicles_inventory_summary,
    vehicles_get_fuel_breakdown, vehicle_create, vehicle_update, vehicle_delete, vehicle_get,
    leads_get_by_dealer, leads_get_stage_counts, lead_create, lead_update, lead_delete, lead_get,
    agents_get_by_dealer, agents_get_leads_count, agent_create, agent_update, agent_delete, agent_get,
    deals_get_by_dealer, deals_get_recent, deals_get_status_counts, deals_get_financial_summary,
    deals_get_monthly_revenue, deal_create, deal_update, deal_get, leads_get_source_counts,
    documents_get_by_dealer, document_create, document_delete,
    inquiries_get_by_dealer, inquiry_update_status,
    user_update_subscription
)
from models import db, User, Vehicle, VehicleImage
from utils.image_utils import save_uploaded_image, is_allowed_image
from utils.vehicle_issues import detect_vehicle_issues
from subscription_features import feature_required

dealer_bp = Blueprint('dealer', __name__)


def log_dealer_action(action, module, status='Success', description=None):
    """Log dealer activity into the unified AdminLog table."""
    try:
        from extensions import db
        from models import AdminLog
        from flask import request as _req, g as _g
        from utils.request_meta import get_request_meta
        dealer_name = _g.user.get('name', 'Dealer') if _g.user else 'Dealer'
        dealer_id = _g.user.get('id') if _g.user else None
        ip, browser, os_name, device = get_request_meta(_req)
        log = AdminLog(
            user_id=dealer_id,
            admin_user=dealer_name,
            user_role='Dealer',
            action=action,
            module=module,
            description=description or action,
            ip_address=ip,
            device=device,
            browser=browser,
            timezone='Asia/Kolkata (IST)',
            status=status,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass


def dealer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user or g.user.get('role') != 'dealer':
            # g.user is None when user deleted from DB — session auto-invalidates
            session.clear()
            flash('Your account has been deleted. Please contact the admin.', 'error')
            return redirect(url_for('auth.login'))
        # ── Block suspended dealer even if session is active ─────────────────
        if not g.user.get('is_active', True):
            session.clear()
            flash('Your account has been suspended. Please contact the admin.', 'error')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


# ── KYC restriction: dealers without complete KYC are redirected ──────────────
# Allowed routes when KYC is incomplete (endpoint names)
_KYC_EXEMPT_ENDPOINTS = {
    'dealer.kyc_upload',     # KYC upload page
    'dealer.kyc_submit',     # KYC form POST
    'auth.logout',           # allow logout
}


def kyc_required(f):
    """Decorator that blocks access until ALL 3 KYC documents are verified
    (auto-approved by the verification engine at upload time)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user or g.user.get('role') != 'dealer':
            return f(*args, **kwargs)
        from models import DealerKYC
        dealer_id = g.user['id']
        kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()

        docs_uploaded = (
            kyc is not None and
            kyc.aadhaar_front and
            kyc.aadhaar_back and
            kyc.pan_card
        )

        # Only fully approved when ALL 3 per-doc statuses are 'approved'
        all_approved = (
            docs_uploaded and
            getattr(kyc, 'aadhaar_front_status', 'pending') == 'approved' and
            getattr(kyc, 'aadhaar_back_status', 'pending') == 'approved' and
            getattr(kyc, 'pan_card_status', 'pending') == 'approved'
        )

        if not all_approved:
            if docs_uploaded:
                # Check if any doc is rejected
                rejected = [
                    d for d in ('aadhaar_front', 'aadhaar_back', 'pan_card')
                    if getattr(kyc, d + '_status', 'pending') == 'rejected'
                ]
                if rejected:
                    doc_names = {'aadhaar_front': 'Aadhaar Front', 'aadhaar_back': 'Aadhaar Back', 'pan_card': 'PAN Card'}
                    names = ', '.join(doc_names[d] for d in rejected)
                    flash(f'Some KYC documents were rejected ({names}). Please re-upload them to continue.', 'error')
                else:
                    flash('Please finish uploading all 3 KYC documents to continue.', 'info')
            else:
                flash('Complete your KYC verification to continue using the DMS.', 'warning')
            return redirect(url_for('dealer.kyc_upload'))
        return f(*args, **kwargs)
    return decorated


def get_dealer_id():
    return g.user['id'] if g.user else None

# ========== KYC UPLOAD (dealer-side) ==========


@dealer_bp.route('/kyc', methods=['GET'])
@dealer_required
def kyc_upload():
    """Show KYC document upload page for dealers with incomplete KYC."""
    from models import DealerKYC, DealerNotification
    dealer_id = get_dealer_id()
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()

    # If all 3 docs are already approved, redirect straight to dashboard
    if (kyc and
            getattr(kyc, 'aadhaar_front_status', 'pending') == 'approved' and
            getattr(kyc, 'aadhaar_back_status', 'pending') == 'approved' and
            getattr(kyc, 'pan_card_status', 'pending') == 'approved'):
        return redirect(url_for('dealer.dashboard'))

    notifications = DealerNotification.query.filter_by(
        dealer_id=dealer_id).order_by(DealerNotification.created_at.desc()).limit(10).all()
    return render_template('dealer/kyc_upload.html', kyc=kyc, notifications=notifications)


@dealer_bp.route('/api/notifications/mark-read', methods=['POST'])
@dealer_required
def mark_notifications_read():
    """Mark all dealer notifications as read."""
    from models import DealerNotification
    dealer_id = get_dealer_id()
    DealerNotification.query.filter_by(dealer_id=dealer_id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


@dealer_bp.route('/kyc/submit', methods=['POST'])
@dealer_required
def kyc_submit():
    """Handle dealer KYC document upload.

    Every file goes through the real verification engine
    (utils/kyc_engine) before it is ever saved: image-quality checks
    (blur, glare, exposure, screenshot, resolution), OCR + document-type
    classification, per-document field validation (Aadhaar number / QR /
    face / logo, PAN regex + logo), and cross-dealer duplicate
    detection. A document that passes every check is saved AND
    auto-approved immediately — the dealer's own verified upload is the
    approval, no admin step required. A document that fails any check
    is rejected immediately with the specific reason, without touching
    the previously-saved file.
    """
    from models import DealerKYC
    from utils.upload_helpers import save_image
    from utils.kyc_engine import duplicate as kyc_dup
    from utils.kyc_engine.file_utils import sniff_real_extension, load_as_cv_image, pil_image_for_hash
    from utils.kyc_engine.validator import validate_document, cross_validate_kyc, DOC_TYPE_MAP

    cfg = current_app.config
    dealer_id = get_dealer_id()

    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()
    if not kyc:
        kyc = DealerKYC(dealer_id=dealer_id, kyc_status='pending')
        db.session.add(kyc)
    kyc.submitted_at = _now_ist()

    folder = os.path.join(
        current_app.config.get('KYC_UPLOAD_FOLDER',
                               os.path.join(os.path.dirname(__file__), '..', 'static', 'uploads', 'dealers')),
        str(dealer_id)
    )
    os.makedirs(folder, exist_ok=True)
    errors = []
    saved_any = False

    allowed_ext = cfg.get('KYC_ALLOWED_EXTENSIONS', {'jpg', 'jpeg', 'png', 'webp', 'pdf'})
    max_bytes = cfg.get('KYC_MAX_UPLOAD_BYTES', 10 * 1024 * 1024)

    for doc_key in ('aadhaar_front', 'aadhaar_back', 'pan_card'):
        f = request.files.get(doc_key)
        if not f or not f.filename:
            continue
        label = doc_key.replace('_', ' ').title()

        # Don't overwrite an already-approved document
        current_status = getattr(kyc, doc_key + '_status', 'pending') or 'pending'
        if current_status == 'approved':
            flash(f'{label} is already approved — skipped.', 'info')
            continue

        # ── 1) Basic file checks (extension + size) ─────────────────────
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext not in allowed_ext:
            errors.append(f'{label}: Unsupported file type. Allowed: {", ".join(sorted(allowed_ext))}.')
            continue

        file_bytes = f.stream.read()
        f.stream.seek(0)
        if not file_bytes:
            errors.append(f'{label}: Uploaded file is empty.')
            continue
        if len(file_bytes) > max_bytes:
            errors.append(f'{label}: File exceeds the {max_bytes // (1024*1024)} MB limit.')
            continue

        # ── 2) Magic-byte sniff — reject a mislabeled extension ─────────
        real_ext = sniff_real_extension(file_bytes)
        if real_ext is None:
            errors.append(f'{label}: File content does not match a supported image/PDF format.')
            continue

        # ── 3) Decode + run the full verification pipeline ──────────────
        try:
            cv_image = load_as_cv_image(file_bytes, real_ext)
        except Exception:
            errors.append(f'{label}: Could not read this file. It may be corrupted.')
            continue

        expected_type = DOC_TYPE_MAP[doc_key]
        result = validate_document(cv_image, expected_type, cfg)
        if not result.ok:
            errors.append(f'{label}: {result.error_message}')
            setattr(kyc, doc_key + '_status', 'rejected')
            setattr(kyc, doc_key + '_reject', result.error_message)
            setattr(kyc, doc_key + '_reviewed_by', 'Auto-KYC Engine')
            setattr(kyc, doc_key + '_reviewed_at', _now_ist())
            continue

        # ── 4) Cross-dealer duplicate detection (SHA256 + pHash) ────────
        sha256_hash = kyc_dup.compute_sha256(file_bytes)
        phash = kyc_dup.compute_phash(pil_image_for_hash(file_bytes, cv_image, real_ext))
        duplicate = kyc_dup.find_duplicate(sha256_hash, phash)
        if duplicate and not (duplicate.dealer_id == dealer_id and duplicate.doc_type == doc_key):
            msg = 'This document has already been uploaded (duplicate detected).'
            errors.append(f'{label}: {msg}')
            setattr(kyc, doc_key + '_status', 'rejected')
            setattr(kyc, doc_key + '_reject', msg)
            setattr(kyc, doc_key + '_reviewed_by', 'Auto-KYC Engine')
            setattr(kyc, doc_key + '_reviewed_at', _now_ist())
            continue

        # ── 5) Passed everything — save the file & record extracted data ─
        f.stream.seek(0)
        saved = save_image(f, folder, prefix=doc_key.replace('_', '-'), vehicle_mode=False)
        if not saved:
            errors.append(f'Failed to save {label}.')
            continue

        setattr(kyc, doc_key, saved)
        # ── Verification pipeline passed => auto-approve right here. ────
        # The dealer's own document now IS the approval — no admin
        # review step is required for a document that clears every
        # image-quality, OCR and field check plus the duplicate check.
        setattr(kyc, doc_key + '_status', 'approved')
        setattr(kyc, doc_key + '_reject', None)
        setattr(kyc, doc_key + '_reviewed_by', 'Auto-KYC Engine')
        setattr(kyc, doc_key + '_reviewed_at', _now_ist())

        if expected_type == 'pan':
            kyc.pan_number = result.extracted_number
            kyc.pan_name = result.extracted_name
            kyc.pan_dob = result.extracted_dob
        elif expected_type == 'aadhaar_front':
            kyc.aadhaar_front_number = result.extracted_number
            kyc.aadhaar_front_name = result.extracted_name
            kyc.aadhaar_front_dob = result.extracted_dob
        else:  # aadhaar_back
            kyc.aadhaar_back_number = result.extracted_number
            kyc.aadhaar_back_name = result.extracted_name

        kyc_dup.register_hash(sha256_hash, phash, dealer_id, doc_key, db.session)
        saved_any = True

        # ── Register in Centralized Document Storage ──────────────────────
        try:
            from db import cds_register
            cds_register({
                'dealer_id':     dealer_id,
                'file_name':     saved,
                'original_name': f.filename,
                'file_path':     os.path.join('uploads', 'dealers', str(dealer_id), saved),
                'module_name':   'KYC',
                'document_type': label,
                'uploaded_by':   dealer_id,
                'performed_by':  f'dealer:{dealer_id}',
            })
        except Exception:
            pass  # Never break the main KYC flow
        # ─────────────────────────────────────────────────────────────────

    # ── Cross-document validation once all 3 slots are filled ───────────
    # Informational only — logged on the record, never blocks approval,
    # since OCR misreads make hard-failing on these too risky.
    if kyc.aadhaar_front and kyc.aadhaar_back and kyc.pan_card:
        issues = cross_validate_kyc(kyc, cfg)
        kyc.cross_validation_notes = '; '.join(issues) if issues else None
        if issues:
            flash('Documents verified and approved, but a cross-check note was recorded: '
                  + '; '.join(issues), 'warning')

    if errors:
        for e in errors:
            flash(e, 'error')
    if saved_any and not errors:
        flash('KYC documents verified and approved successfully.', 'success')
    elif saved_any:
        flash('Some documents were verified and approved; see errors above for the rest.', 'info')

    # Recalculate overall status based on per-doc statuses
    kyc.recalculate_status()
    db.session.commit()
    log_dealer_action('Submitted KYC documents', 'KYC',
                       status='Failed' if errors and not saved_any else 'Success')
    return redirect(url_for('dealer.kyc_upload'))


# ========== DASHBOARD ==========
@dealer_bp.route('/dashboard')
@dealer_required
@kyc_required
def dashboard():
    dealer_id = get_dealer_id()

    inventory_summary = vehicles_inventory_summary(dealer_id)
    lead_stages = leads_get_stage_counts(dealer_id)
    recent_leads = leads_get_by_dealer(dealer_id, page=1, per_page=5)['items']
    recent_deals = deals_get_recent(dealer_id)
    financial = deals_get_financial_summary(dealer_id)
    # FIXED: fetch latest 5 inquiries so the dashboard widget can display them
    recent_inquiries = inquiries_get_by_dealer(dealer_id)[:5]
    pending_inquiries = sum(
        1 for i in recent_inquiries if i['status'] == 'pending')

    return render_template('dealer/dashboard.html',
                           total_vehicles=inventory_summary['total'],
                           available=inventory_summary['available'],
                           total_leads=sum(lead_stages.values()),
                           new_leads=lead_stages.get('new', 0),
                           total_deals=financial['total_deals'],
                           delivered=financial['total_deals'],
                           revenue=financial['total_revenue'],
                           lead_stages=lead_stages,
                           recent_leads=recent_leads,
                           recent_deals=recent_deals,
                           recent_inquiries=recent_inquiries,       # FIXED: now passed to template
                           pending_inquiries=pending_inquiries,     # FIXED: badge count for pending
                           # ── Financial KPI cards (Net Revenue replaces Total Revenue) ──
                           net_revenue=financial['net_revenue'],
                           total_sales=financial['total_sales'],
                           total_purchase_cost=financial['total_purchase_cost'],
                           total_cost=financial['total_cost'],
                           gross_profit=financial['gross_profit'],
                           total_vehicles_sold=financial['total_vehicles_sold'],
                           )

# ========== INVENTORY ==========


@dealer_bp.route('/inventory')
@dealer_required
@kyc_required
def inventory():
    dealer_id = get_dealer_id()
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    fuel = request.args.get('fuel', '')
    approval = request.args.get('approval', '')
    search = request.args.get('search', '')

    vehicles = vehicles_get_by_dealer(
        dealer_id, status=status, fuel=fuel, approval=approval, search=search, page=page)

    # Enrich each vehicle with dynamically computed issue data
    for v in vehicles['items']:
        v['vehicle_issues'] = detect_vehicle_issues(v)

    # Counts for the approval-status tab bar — always across ALL of this
    # dealer's vehicles (unaffected by the current filter/search) so the
    # tab labels stay stable while browsing.
    from models import Vehicle as _V
    all_dealer_vehicles = _V.query.filter_by(dealer_id=dealer_id).all()
    approval_counts = {
        'all':      len(all_dealer_vehicles),
        'pending':  sum(1 for v in all_dealer_vehicles if v.approval_status == 'pending'),
        'approved': sum(1 for v in all_dealer_vehicles if v.approval_status == 'approved'),
        'rejected': sum(1 for v in all_dealer_vehicles if v.approval_status == 'rejected'),
    }

    return render_template('dealer/inventory.html', vehicles=vehicles, status_filter=status,
                            fuel_filter=fuel, approval_filter=approval, search=search,
                            approval_counts=approval_counts)



def _format_price_inr(val):
    """Format a numeric price string into Indian-style '₹5.25 Lakh' / '₹1.10 Crore' wording."""
    try:
        num = float(str(val).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if num >= 1_00_00_000:
        return f"₹{num / 1_00_00_000:.2f} Crore".replace('.00', '')
    if num >= 1_00_000:
        return f"₹{num / 1_00_000:.2f} Lakh".replace('.00', '')
    return f"₹{int(num):,}"


def _build_local_description(data):
    """Build a natural-sounding used-car listing description purely from the
    fields the dealer has already filled in — no external AI/API call needed."""
    import random

    make = (data.get('make') or '').strip()
    model = (data.get('model') or '').strip()
    variant = (data.get('variant') or '').strip()
    year = data.get('year')
    color = (data.get('color') or '').strip()
    fuel_type = (data.get('fuel_type') or '').strip()
    transmission = (data.get('transmission') or '').strip()
    mileage = data.get('mileage')
    engine_cc = data.get('engine_cc')
    price = data.get('price')
    condition = (data.get('condition') or '').strip()
    accident_history = (data.get('accident_history') or '').strip()
    keys_available = (data.get('keys_available') or '').strip()
    major_issues = (data.get('major_issues') or '').strip()

    def clean_num(v):
        if v is None:
            return None
        try:
            s = str(v).replace(',', '').strip()
            return s if s and s not in ('0', 'None') else None
        except Exception:
            return None

    mileage = clean_num(mileage)
    engine_cc = clean_num(engine_cc)
    year = str(year).strip() if year not in (None, '', 0, '0') else None

    # ── Sentence 1: headline ─────────────────────────────────────────────
    title_bits = []
    if year:
        title_bits.append(year)
    title_bits.append(make)
    title_bits.append(model)
    if variant:
        title_bits.append(variant)
    headline_name = ' '.join(b for b in title_bits if b)

    headline_openers = [
        f"Presenting a well-maintained {headline_name}, ready for its next owner.",
        f"Up for sale: a {headline_name} that's been kept in great shape.",
        f"Here's a {headline_name} in solid, dependable condition.",
        f"Check out this {headline_name}, available now at a great price.",
    ]
    sentences = [random.choice(headline_openers)]

    # ── Sentence 2: spec highlights (fuel, transmission, engine, color) ──
    spec_bits = []
    if fuel_type:
        spec_bits.append(f"runs on {fuel_type}")
    if transmission:
        spec_bits.append(f"comes with {transmission} transmission")
    if engine_cc:
        spec_bits.append(f"a {engine_cc}cc engine")
    if color:
        spec_bits.append(f"finished in an attractive {color} shade")
    if spec_bits:
        if len(spec_bits) == 1:
            spec_sentence = f"It {spec_bits[0]}."
        else:
            spec_sentence = "It " + ", ".join(spec_bits[:-1]) + f" and {spec_bits[-1]}."
        sentences.append(spec_sentence)

    # ── Sentence 3: mileage / condition ──────────────────────────────────
    cond_bits = []
    if mileage:
        try:
            mileage_fmt = f"{int(float(mileage)):,}"
        except Exception:
            mileage_fmt = mileage
        cond_bits.append(f"has covered {mileage_fmt} km")
    if condition:
        cond_bits.append(f"is in {condition.lower()} condition")
    if cond_bits:
        cond_sentence = "The vehicle " + " and ".join(cond_bits) + "."
        sentences.append(cond_sentence)

    # ── Sentence 4: accident history / keys / issues (honesty first) ────
    honesty_bits = []
    if accident_history:
        al = accident_history.lower()
        if al in ('no', 'none', 'no accident', 'accident free', 'accident-free'):
            honesty_bits.append("no reported accident history")
        else:
            honesty_bits.append(f"accident history: {accident_history}")
    if keys_available:
        kl = keys_available.lower()
        if kl in ('2', 'two', 'both', 'yes', '1', 'one'):
            honesty_bits.append(f"{keys_available} key(s) available" if keys_available not in ('yes',) else "keys available")
    if honesty_bits:
        sentences.append("Comes with " + " and ".join(honesty_bits) + ".")

    if major_issues:
        sentences.append(f"Please note the following for full transparency: {major_issues}.")

    # ── Sentence 5: price + closing call to action ───────────────────────
    price_fmt = _format_price_inr(price)
    closers = []
    if price_fmt:
        closers.append(f"Available at an asking price of {price_fmt}")
        if str(data.get('negotiable', '')).lower() in ('true', '1', 'yes', 'on'):
            closers.append("(price negotiable)")
    closing = " ".join(closers).strip()
    if closing:
        sentences.append(closing + ". Contact us today to book a test drive!")
    else:
        sentences.append("Contact us today to book a test drive!")

    return " ".join(s.strip() for s in sentences if s and s.strip())


@dealer_bp.route('/vehicles/ai-description', methods=['POST'])
@dealer_required
def ai_generate_description():
    """Generate a marketing-ready vehicle description locally from whatever
    vehicle fields the dealer has filled in on the Add/Edit form.

    This does NOT call any external AI API and needs no API key —
    it builds a natural-sounding description directly from the form data."""
    data = request.get_json(silent=True) or {}

    make = (data.get('make') or '').strip()
    model = (data.get('model') or '').strip()
    if not make or not model:
        return jsonify(success=False, error='Make and Model are required.'), 400

    try:
        text = _build_local_description(data)
        if not text:
            raise ValueError('Could not build description from provided fields.')
        return jsonify(success=True, description=text)
    except Exception as e:
        current_app.logger.error(f"Local description generation failed: {e}")
        return jsonify(success=False, error='Could not generate description right now. Please try again.'), 502


MANDATORY_SLOTS = [
    ('img_front',      'front',      0),
    ('img_rear',       'rear',       1),
    ('img_right_side', 'right_side', 2),
    ('img_left_side',  'left_side',  3),
    ('img_engine',     'engine',     4),
    ('img_boot',       'boot',       5),
    ('img_interior',   'interior',   6),
]
SLOT_LABELS = {
    'front': 'Front View', 'rear': 'Rear / Tail View',
    'right_side': 'Right Side View', 'left_side': 'Left Side View',
    'engine': 'Engine Bay', 'boot': 'Boot / Trunk', 'interior': 'Interior',
}


@dealer_bp.route('/inventory/api/classify-photo', methods=['POST'])
@dealer_required
def classify_vehicle_photo_api():
    """Called by the Add/Edit Vehicle form the instant a photo is selected —
    strict per-angle AI check so Front box only accepts a front photo, Boot
    box only accepts a boot photo, etc. Mirrors /api/classify from the
    standalone car-upload-single tool."""
    from utils.vehicle_photo_ai import verify_vehicle_photo, SLOT_TO_LABEL

    slot = request.form.get('slot')
    if slot not in SLOT_TO_LABEL:
        return jsonify(ok=False, error='Unknown photo slot.'), 400
    if 'file' not in request.files:
        return jsonify(ok=False, error='No file provided.'), 400

    result = verify_vehicle_photo(request.files['file'], slot)
    if not result['ok']:
        return jsonify(ok=False, error=result['error']), 422
    return jsonify(ok=True)


@dealer_bp.route('/inventory/api/estimate-price', methods=['POST'])
@dealer_required
def estimate_vehicle_price_api():
    """Called by the "Estimate Price (AI)" button on the Add/Edit Vehicle
    form.

    DEFAULT (free, no API cost): the offline rule-based calculator in
    utils/vehicle_price_ai.py. It adjusts the dealer's own Asking Price
    down/up using age, mileage, Condition Details and photo checks — all
    local, no external API calls, no charges.

    OPTIONAL (paid): if the dealer explicitly ticks "Also check live
    market listings" on the form (use_live_market=1), we additionally call
    utils/vehicle_market_price_ai.py, which uses the Anthropic API with
    web_search — this costs money per call (Anthropic bills per token +
    per search), so it only ever runs when the dealer opts in on purpose.
    If that call fails for any reason, we silently keep the free result
    that's already been computed."""
    from utils.vehicle_price_ai import estimate_vehicle_price

    vehicle_data = {
        'make':                 request.form.get('make'),
        'model':                request.form.get('model'),
        'variant':              request.form.get('variant'),
        'year':                 request.form.get('year'),
        'fuel_type':            request.form.get('fuel_type'),
        'transmission':         request.form.get('transmission'),
        'mileage':              request.form.get('mileage'),
        'city':                 (g.user or {}).get('city'),
        'reference_price':      request.form.get('reference_price'),
        'accident_history':     request.form.get('accident_history', 'NA'),
        'loan_status':          request.form.get('loan_status', 'NA'),
        'rc_service_records':   request.form.get('rc_service_records', 'NA'),
        'major_issues':         ','.join(request.form.getlist('major_issues')) or 'None',
        'keys_available':       request.form.get('keys_available', 'NA'),
        'body_panel_status':    request.form.get('body_panel_status', 'NA'),
    }

    # Any of the 7 mandatory photo slots the dealer has already picked —
    # purely optional; the estimate still works from details alone.
    image_files = []
    for field_name, _img_type, _sort in MANDATORY_SLOTS:
        f = request.files.get(field_name)
        if f and f.filename and is_allowed_image(f.filename):
            image_files.append(f)

    result = estimate_vehicle_price(vehicle_data, image_files)
    if not result['ok']:
        return jsonify(result), 400
    result['source'] = 'offline_free'

    # ── Optional paid add-on — only runs if the dealer explicitly asked ──
    if request.form.get('use_live_market') == '1':
        from utils.vehicle_market_price_ai import estimate_market_price
        market = estimate_market_price(vehicle_data, image_files)
        if market['ok']:
            market['source'] = 'live_market'
            market['offline_estimate'] = {
                'estimated_price': result['estimated_price'],
                'estimated_range': result['estimated_range'],
            }
            return jsonify(market)
        else:
            # Paid lookup failed/unavailable — keep the free result, just
            # note why the live search didn't run.
            result['live_market_note'] = market.get('error')

    return jsonify(result)


@dealer_bp.route('/inventory/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def add_vehicle():
    dealer_id = get_dealer_id()

    if request.method == 'POST':
        # ── Server-side: require all 7 mandatory image slots, present AND
        # verified to actually show that angle (same AI gate as the live
        # per-field check, so client-side JS can never be bypassed) ────────
        from utils.vehicle_photo_ai import verify_vehicle_photo

        missing_slots = []
        slot_results = {}
        for field_name, img_type, _ in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename or not is_allowed_image(f.filename):
                missing_slots.append(SLOT_LABELS.get(img_type, img_type))
                continue
            f.stream.seek(0)
            verdict = verify_vehicle_photo(f, img_type)
            f.stream.seek(0)
            if not verdict['ok']:
                missing_slots.append(f"{SLOT_LABELS.get(img_type, img_type)} — {verdict['error']}")
            else:
                slot_results[img_type] = True

        if missing_slots:
            flash(
                'Please fix the following vehicle photos before submitting: '
                + '; '.join(missing_slots),
                'error'
            )
            return render_template('dealer/vehicle_form.html', action='Add', vehicle=None)

        # ── Primary image: first mandatory slot (front) or explicit 'image' field ──
        # Try 'image' field first (explicit primary); fall back to 'front' slot
        image_file = request.files.get('image') or request.files.get('img_front')
        filename = 'default_car.jpg'

        if image_file and image_file.filename and is_allowed_image(image_file.filename):
            filename = save_uploaded_image(
                image_file, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)

        vehicle_data = {
            'dealer_id': dealer_id,
            'make': request.form.get('make'),
            'model': request.form.get('model'),
            'variant': request.form.get('variant'),
            'year': int(request.form.get('year')),
            'color': request.form.get('color'),
            'fuel_type': request.form.get('fuel_type'),
            'transmission': request.form.get('transmission'),
            'mileage': int(request.form.get('mileage') or 0),
            'engine_cc': int(request.form.get('engine_cc') or 0),
            'price': float(request.form.get('price')),
            'negotiable': request.form.get('negotiable') == 'on',
            'condition': request.form.get('condition'),
            'status': request.form.get('status'),
            'description': request.form.get('description'),
            'vin_number': (request.form.get('vin_number') or '').strip().upper(),
            'registration_number': (request.form.get('registration_number') or '').strip().upper(),
            'insurance_valid_till': request.form.get('insurance_valid_till'),
            'rc_available': request.form.get('rc_available') == 'on',
            'featured': request.form.get('featured') == 'on',
            'image_filename': filename,
            # new condition detail fields
            'accident_history':   request.form.get('accident_history', 'NA').strip(),
            'loan_status':        request.form.get('loan_status', 'NA').strip(),
            'rc_service_records': request.form.get('rc_service_records', 'NA').strip(),
            'major_issues':       ','.join(request.form.getlist('major_issues')) or 'None',
            'keys_available':     request.form.get('keys_available', 'NA').strip(),
            'body_panel_status':  request.form.get('body_panel_status', 'NA').strip(),
        }

        vehicle_id = vehicle_create(vehicle_data)

        # ── Save typed mandatory images (7 slots) into vehicle_images ────────────
        # Order matches VehicleImage.MANDATORY_TYPES; front is already primary above
        # but we ALSO store it in vehicle_images so gallery always includes it
        # (MANDATORY_SLOTS already defined above for validation — reuse here)
        gallery_sort = 10  # gallery images start at sort_order 10+
        saved_any = False

        for field_name, img_type, sort_idx in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                img_rec = VehicleImage(
                    vehicle_id=vehicle_id,
                    filename=fname,
                    image_type=img_type,
                    sort_order=sort_idx
                )
                db.session.add(img_rec)
                # If no explicit primary was uploaded, use first mandatory slot
                if img_type == 'front' and filename == 'default_car.jpg':
                    from models import Vehicle as _V
                    _v = _V.query.get(vehicle_id)
                    if _v:
                        _v.image_filename = fname
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'add_vehicle mandatory image error: {e}')

        # ── Save additional gallery images ────────────────────────────────────────
        extra_files = request.files.getlist('extra_images')
        for f in extra_files[:10]:  # max 10 additional
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                img_rec = VehicleImage(
                    vehicle_id=vehicle_id,
                    filename=fname,
                    image_type='gallery',
                    sort_order=gallery_sort
                )
                db.session.add(img_rec)
                gallery_sort += 1
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'add_vehicle gallery image error: {e}')

        if saved_any:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f'add_vehicle image commit error: {e}')

        log_dealer_action(f'Added vehicle: {vehicle_data.get("make")} {vehicle_data.get("model")} {vehicle_data.get("year")}', 'Vehicles')
        flash('Vehicle added successfully', 'success')
        return redirect(url_for('dealer.inventory'))

    return render_template('dealer/vehicle_form.html', action='Add', vehicle=None)


@dealer_bp.route('/inventory/edit/<int:vid>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_vehicle(vid):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(vid)

    if not vehicle or vehicle['dealer_id'] != dealer_id:
        flash('Vehicle not found', 'error')
        return redirect(url_for('dealer.inventory'))

    if request.method == 'POST':
        image_file = request.files.get('image')

        if image_file and image_file.filename and is_allowed_image(image_file.filename):
            new_fname = save_uploaded_image(
                image_file, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
            vehicle['image_filename'] = new_fname

        update_data = {
            'make': request.form.get('make'),
            'model': request.form.get('model'),
            'variant': request.form.get('variant'),
            'year': int(request.form.get('year')),
            'color': request.form.get('color'),
            'fuel_type': request.form.get('fuel_type'),
            'transmission': request.form.get('transmission'),
            'mileage': int(request.form.get('mileage') or 0),
            'engine_cc': int(request.form.get('engine_cc') or 0),
            'price': float(request.form.get('price')),
            'negotiable': request.form.get('negotiable') == 'on',
            'condition': request.form.get('condition'),
            'status': request.form.get('status'),
            'description': request.form.get('description'),
            'vin_number': (request.form.get('vin_number') or '').strip().upper(),
            'registration_number': (request.form.get('registration_number') or '').strip().upper(),
            'insurance_valid_till': request.form.get('insurance_valid_till'),
            'rc_available': request.form.get('rc_available') == 'on',
            'featured': request.form.get('featured') == 'on',
            'image_filename': vehicle.get('image_filename', 'default_car.jpg'),
            # new condition detail fields
            'accident_history':   request.form.get('accident_history', 'NA').strip(),
            'loan_status':        request.form.get('loan_status', 'NA').strip(),
            'rc_service_records': request.form.get('rc_service_records', 'NA').strip(),
            'major_issues':       ','.join(request.form.getlist('major_issues')) or 'None',
            'keys_available':     request.form.get('keys_available', 'NA').strip(),
            'body_panel_status':  request.form.get('body_panel_status', 'NA').strip(),
        }

        # ── Handle typed mandatory image uploads on edit ──────────────────────────
        MANDATORY_SLOTS = [
            ('img_front',      'front',      0),
            ('img_rear',       'rear',       1),
            ('img_right_side', 'right_side', 2),
            ('img_left_side',  'left_side',  3),
            ('img_engine',     'engine',     4),
            ('img_boot',       'boot',       5),
            ('img_interior',   'interior',   6),
        ]
        saved_any = False
        for field_name, img_type, sort_idx in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename or not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                # Check if a typed record already exists for this slot — replace it
                existing = VehicleImage.query.filter_by(
                    vehicle_id=vid, image_type=img_type).first()
                if existing:
                    existing.filename = fname
                else:
                    img_rec = VehicleImage(
                        vehicle_id=vid,
                        filename=fname,
                        image_type=img_type,
                        sort_order=sort_idx
                    )
                    db.session.add(img_rec)
                # Inventory card must always show the Front image — only sync
                # the primary/cover image when the Front slot itself was
                # updated; other categories (rear, sides, boot, engine,
                # interior) must never change the Inventory card.
                if img_type == 'front':
                    update_data['image_filename'] = fname
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'edit_vehicle typed image error ({img_type}): {e}')

        if saved_any:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f'edit_vehicle image commit error: {e}')

        vehicle_update(vid, update_data)

        # ── Featured Listing directly controls marketplace approval ─────────
        # There is no admin approval step. On save, the "Featured Listing"
        # checkbox decides publication: checked → 'approved' (live on the
        # marketplace), unchecked → 'pending' (not marketplace-listed).
        # vehicle_update() blocks 'approval_status', so we set it directly
        # here based on the submitted featured flag.
        try:
            from extensions import db
            from models import Vehicle as _V
            _v = _V.query.get(vid)
            if _v:
                _v.approval_status = 'approved' if update_data.get('featured') else 'pending'
                db.session.commit()
        except Exception as e:
            try:
                from extensions import db
                db.session.rollback()
            except Exception:
                pass
            current_app.logger.error(f'edit_vehicle approval-set error: {e}')

        log_dealer_action(f'Updated vehicle: {update_data.get("make")} {update_data.get("model")} (ID:{vid})', 'Vehicles')
        flash('Vehicle updated successfully.', 'success')
        return redirect(url_for('dealer.inventory'))

    # ── GET: load existing typed images for pre-filling the form ─────────────
    existing_typed_imgs = VehicleImage.query.filter_by(vehicle_id=vid).all()
    vehicle['extra_images_typed'] = [
        {'filename': img.filename, 'image_type': img.image_type, 'id': img.id}
        for img in existing_typed_imgs
    ]

    return render_template('dealer/vehicle_form.html', action='Edit', vehicle=vehicle)


@dealer_bp.route('/inventory/delete/<int:vid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_vehicle(vid):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(vid)

    if vehicle and vehicle['dealer_id'] == dealer_id:
        vehicle_delete(vid)
        log_dealer_action(f'Deleted vehicle ID:{vid}', 'Vehicles')
        flash('Vehicle deleted successfully', 'success')
    else:
        flash('Vehicle not found', 'error')

    return redirect(url_for('dealer.inventory'))


@dealer_bp.route('/inventory/<int:car_id>')
@dealer_required
@kyc_required
def inventory_detail(car_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)

    if not vehicle or vehicle['dealer_id'] != dealer_id:
        flash('Vehicle not found', 'error')
        return redirect(url_for('dealer.inventory'))

    # FIXED: load extra gallery images from vehicle_images table and attach to vehicle dict
    # Without this, vehicle.get('extra_images') in the template is always None,
    # so the gallery never shows uploaded photos even after they are saved to DB.
    try:
        extra_imgs = (
            VehicleImage.query
            .filter_by(vehicle_id=car_id)
            .order_by(VehicleImage.sort_order, VehicleImage.id)
            .all()
        )
        vehicle['extra_images'] = [img.filename for img in extra_imgs]
        # Pass full image objects so template can show type labels
        vehicle['extra_images_typed'] = [
            {'filename': img.filename, 'image_type': img.image_type, 'id': img.id}
            for img in extra_imgs
        ]
    except Exception as e:
        current_app.logger.error(
            f"Could not load extra images for vehicle {car_id}: {e}")
        vehicle['extra_images'] = []
        vehicle['extra_images_typed'] = []

    return render_template('dealer/inventory_detail.html', vehicle=vehicle)


@dealer_bp.route('/inventory/upload-images', methods=['POST'])
@dealer_required
@kyc_required
def upload_vehicle_images():
    """
    AJAX endpoint — accepts multiple image files and stores them as
    extra gallery images for a vehicle.

    Request (multipart/form-data):
        vehicle_id  int
        images      file[]   (up to 15 files per batch; jpg/jpeg/png/gif/webp)

    Response (JSON):
        { "success": true,  "filenames": ["abc.jpg", ...], "count": N }
        { "success": false, "error": "..." }
    """
    dealer_id = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)

    if not vehicle_id:
        return jsonify({'success': False, 'error': 'vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    files = request.files.getlist('images')
    # Support per-file image_type: client can send image_types[] matching order of images[]
    # Falls back to 'gallery' for all files if not provided
    image_types = request.form.getlist('image_types')
    saved = []
    front_fname = None  # only the Front slot should ever become the primary/cover image
    MAX_FILES = 15
    ALLOWED = {'jpg', 'jpeg', 'png', 'webp'}  # webp accepted then converted

    # FIXED: entire file-save + DB block wrapped in try/except.
    # Previously, any filesystem or DB error would bubble up to Flask's default
    # error handler which returns an HTML 500 page. The browser JS then tried
    # to parse that HTML as JSON → "Unexpected token '<'" crash.
    # Now: errors always return {"success": false, "error": "..."} JSON + rollback.
    try:
        for idx, f in enumerate(files[:MAX_FILES]):
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue

            img_type = image_types[idx] if idx < len(image_types) else 'gallery'
            # Validate image_type value
            valid_types = VehicleImage.MANDATORY_TYPES + ['gallery']
            if img_type not in valid_types:
                img_type = 'gallery'

            # For typed mandatory slots: replace existing record if one exists
            if img_type != 'gallery':
                existing = VehicleImage.query.filter_by(
                    vehicle_id=vehicle_id, image_type=img_type).first()
                if existing:
                    # Remove old file from disk
                    old_path = os.path.join(
                        current_app.config['UPLOAD_FOLDER'], existing.filename)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass
                    db.session.delete(existing)

            fname = save_uploaded_image(
                f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)

            sort_val = VehicleImage.MANDATORY_TYPES.index(img_type) \
                if img_type in VehicleImage.MANDATORY_TYPES else 10 + len(saved)

            img_rec = VehicleImage(
                vehicle_id=vehicle_id,
                filename=fname,
                image_type=img_type,
                sort_order=sort_val
            )
            db.session.add(img_rec)
            saved.append({'filename': fname, 'image_type': img_type})
            if img_type == 'front':
                front_fname = fname

        if saved:
            db.session.commit()

        # Inventory card must always show the Front image — only sync the
        # primary/cover image when the Front slot itself was uploaded here.
        if front_fname:
            from db import execute
            execute("UPDATE vehicles SET image_filename=%s WHERE id=%s",
                    (front_fname, vehicle_id))

        filenames = [s['filename'] for s in saved]
        return jsonify({'success': True, 'filenames': filenames,
                        'saved': saved, 'count': len(saved)})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"upload_vehicle_images error for vehicle {vehicle_id}: {e}")
        return jsonify({'success': False, 'error': 'Upload failed. Please try again.'}), 500

@dealer_bp.route('/inventory/delete-image', methods=['POST'])
@dealer_required
@kyc_required
def delete_vehicle_image():
    """AJAX endpoint — delete a single gallery image by filename."""
    dealer_id = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)
    filename = request.form.get('filename', '').strip()

    if not vehicle_id or not filename:
        return jsonify({'success': False, 'error': 'vehicle_id and filename required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    try:
        img = VehicleImage.query.filter_by(vehicle_id=vehicle_id, filename=filename).first()
        if img:
            db.session.delete(img)
            db.session.commit()
        # Remove file from disk
        file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@dealer_bp.route('/inventory/replace-image', methods=['POST'])
@dealer_required
@kyc_required
def replace_vehicle_image():
    """
    AJAX endpoint — replace a specific VehicleImage record with a new upload.
    Overwrites the file on disk keeping the same filename (so DB stays clean),
    OR creates a new file and updates the DB record if replace_vi_id is given.

    Request (multipart/form-data):
        vehicle_id   int
        images       file   (single image)
        image_types  str    (the image_type e.g. 'front', 'interior', 'gallery')
        replace_vi_id int   (VehicleImage.id to replace)
    """
    dealer_id  = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)
    vi_id      = request.form.get('replace_vi_id', type=int)

    if not vehicle_id:
        return jsonify({'success': False, 'error': 'vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    files = request.files.getlist('images')
    if not files or not files[0].filename:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    f = files[0]
    if not is_allowed_image(f.filename):
        return jsonify({'success': False, 'error': 'Invalid image type'}), 400

    img_type = (request.form.get('image_types') or 'gallery').strip()
    valid_types = VehicleImage.MANDATORY_TYPES + ['gallery']
    if img_type not in valid_types:
        img_type = 'gallery'

    try:
        # If we have a specific vi_id, overwrite that record's file
        if vi_id:
            vi = VehicleImage.query.filter_by(id=vi_id, vehicle_id=vehicle_id).first()
            if vi:
                # Save new file, delete old
                new_fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], vi.filename)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                vi.filename  = new_fname
                vi.image_type = img_type
                db.session.commit()

                # Inventory card must always show the Front image — only
                # sync the primary/cover image when the Front slot is the
                # one being replaced; other categories must not affect it.
                if img_type == 'front':
                    from db import execute
                    execute("UPDATE vehicles SET image_filename=%s WHERE id=%s",
                            (new_fname, vehicle_id))

                return jsonify({'success': True, 'filename': new_fname, 'vi_id': vi_id})

        # Fallback: treat as a normal typed upload (replace by type)
        existing = VehicleImage.query.filter_by(
            vehicle_id=vehicle_id, image_type=img_type).first()
        if existing:
            old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], existing.filename)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            db.session.delete(existing)

        fname = save_uploaded_image(
            f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
        sort_val = VehicleImage.MANDATORY_TYPES.index(img_type) \
            if img_type in VehicleImage.MANDATORY_TYPES else 99
        img_rec = VehicleImage(
            vehicle_id=vehicle_id, filename=fname,
            image_type=img_type, sort_order=sort_val)
        db.session.add(img_rec)
        db.session.commit()

        # Inventory card must always show the Front image — only sync the
        # primary/cover image when the Front slot itself was replaced.
        if img_type == 'front':
            from db import execute
            execute("UPDATE vehicles SET image_filename=%s WHERE id=%s",
                    (fname, vehicle_id))

        return jsonify({'success': True, 'filename': fname})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"replace_vehicle_image error: {e}")
        return jsonify({'success': False, 'error': 'Replace failed. Please try again.'}), 500


@dealer_bp.route('/inventory/set-primary-image', methods=['POST'])
@dealer_required
@kyc_required
def set_primary_image():
    """
    AJAX endpoint — set a VehicleImage as the primary image for a vehicle.
    Called automatically after mask editor saves a processed image.

    Request (JSON): { vi_id: int, vehicle_id: int }
    """
    dealer_id = get_dealer_id()
    data      = request.get_json(silent=True) or {}
    vi_id     = data.get('vi_id')
    vehicle_id = data.get('vehicle_id')

    if not vi_id or not vehicle_id:
        return jsonify({'success': False, 'error': 'vi_id and vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    vi = VehicleImage.query.filter_by(id=int(vi_id), vehicle_id=vehicle_id).first()
    if not vi:
        return jsonify({'success': False, 'error': 'Image not found'}), 404

    try:
        from db import vehicle_update
        vehicle_update(vehicle_id, {'image_filename': vi.filename})
        return jsonify({'success': True, 'filename': vi.filename})
    except Exception as e:
        current_app.logger.error(f'set_primary_image error: {e}')
        return jsonify({'success': False, 'error': 'Could not update primary'}), 500


# ========== LEADS ==========


@dealer_bp.route('/leads')
@dealer_required
@kyc_required
def leads():
    dealer_id = get_dealer_id()
    stage = request.args.get('stage', '')
    search = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)

    leads_data = leads_get_by_dealer(
        dealer_id, stage=stage, search=search, page=page)
    stage_counts = leads_get_stage_counts(dealer_id)

    return render_template('dealer/leads.html',
                           leads=leads_data['items'],
                           stage_counts=stage_counts,
                           stage_filter=stage,
                           search=search,
                           current_page=leads_data['page'],
                           total_pages=leads_data['pages'],
                           total=leads_data['total'],
                           stages=['new', 'interested',
                                   'test_drive', 'negotiation', 'lost',
                                   'connected', 'not_connected']
                           )


@dealer_bp.route('/leads/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def add_lead():
    import re
    dealer_id = get_dealer_id()
    agents = agents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        follow_up_date = request.form.get('follow_up_date')
        stage = request.form.get('stage')

        # Backend validation: connected stage requires follow_up_date
        if stage == 'connected' and not follow_up_date:
            flash('Follow-up Date is required when Stage is "Connected"', 'error')
            return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        # Only connected keeps follow_up_date; all others → NULL
        if stage != 'connected':
            follow_up_date = None

        # Validate follow-up date (must be future)
        if follow_up_date:
            from datetime import datetime
            follow_dt = datetime.strptime(follow_up_date, '%Y-%m-%dT%H:%M')
            if follow_dt.date() <= datetime.now().date():
                flash(
                    'Follow-up date must be a future date (tomorrow or later)', 'error')
                return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        # Phone validation: 10-digit number only
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        phone_valid = len(customer_phone) == 10 if customer_phone else True
        if customer_phone and not phone_valid:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        lead_data = {
            'dealer_id': dealer_id,
            'agent_id': request.form.get('agent_id') or None,
            'vehicle_id': request.form.get('vehicle_id') or None,
            'customer_name': request.form.get('customer_name'),
            'customer_email': request.form.get('customer_email'),
            'customer_phone': customer_phone,
            'customer_city': request.form.get('customer_city'),
            'source': request.form.get('source'),
            'stage': request.form.get('stage'),
            'notes': request.form.get('notes'),
            'budget': float(request.form.get('budget')) if request.form.get('budget') else None,
            'assigned_to': request.form.get('assigned_to'),
            'follow_up_date': follow_up_date
        }

        lead_create(lead_data)
        log_dealer_action(f'Created lead for {lead_data.get("customer_name", "customer")}', 'Leads')
        flash('Lead added successfully', 'success')
        return redirect(url_for('dealer.leads'))

    return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)


@dealer_bp.route('/leads/edit/<int:lid>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_lead(lid):
    dealer_id = get_dealer_id()
    lead = lead_get(lid)

    if not lead or lead['dealer_id'] != dealer_id:
        flash('Lead not found', 'error')
        return redirect(url_for('dealer.leads'))

    agents = agents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        follow_up_date = request.form.get('follow_up_date')
        stage = request.form.get('stage')

        # Backend validation: connected stage requires follow_up_date
        if stage == 'connected' and not follow_up_date:
            flash('Follow-up Date is required when Stage is "Connected"', 'error')
            return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        # Only connected keeps follow_up_date; all others → NULL
        if stage != 'connected':
            follow_up_date = None

        # Validate follow-up date (must be future)
        if follow_up_date:
            from datetime import datetime
            follow_dt = datetime.strptime(follow_up_date, '%Y-%m-%dT%H:%M')
            if follow_dt.date() <= datetime.now().date():
                flash(
                    'Follow-up date must be a future date (tomorrow or later)', 'error')
                return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        # Phone validation: +91XXXXXXXXXX or 10-digit number
        import re
        customer_phone = (request.form.get('customer_phone') or '').strip()
        phone_valid = bool(
            re.match(r'^\+91[0-9]{10}$', customer_phone) or
            re.match(r'^[0-9]{10}$', customer_phone)
        )
        if not phone_valid:
            flash('Phone must be a 10-digit number or +91 followed by 10 digits.', 'error')
            return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        update_data = {
            'agent_id': request.form.get('agent_id') or None,
            'vehicle_id': request.form.get('vehicle_id') or None,
            'customer_name': request.form.get('customer_name'),
            'customer_email': request.form.get('customer_email'),
            'customer_phone': customer_phone,
            'customer_city': request.form.get('customer_city'),
            'source': request.form.get('source'),
            'stage': request.form.get('stage'),
            'notes': request.form.get('notes'),
            'budget': float(request.form.get('budget')) if request.form.get('budget') else None,
            'assigned_to': request.form.get('assigned_to'),
            'follow_up_date': follow_up_date
        }

        lead_update(lid, update_data)
        log_dealer_action(f'Updated lead for {update_data.get("customer_name", "customer")} (ID:{lid})', 'Leads')
        flash('Lead updated successfully', 'success')
        return redirect(url_for('dealer.leads'))

    return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)


@dealer_bp.route('/leads/delete/<int:lid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_lead(lid):
    dealer_id = get_dealer_id()
    lead = lead_get(lid)

    if lead and lead['dealer_id'] == dealer_id:
        lead_delete(lid)
        flash('Lead deleted successfully', 'success')
    else:
        flash('Lead not found', 'error')

    return redirect(url_for('dealer.leads'))

# ========== AGENTS ==========


@dealer_bp.route('/agents')
@dealer_required
@kyc_required
def agents():
    dealer_id = get_dealer_id()
    agents_list = agents_get_by_dealer(dealer_id)
    agent_leads_count = agents_get_leads_count(dealer_id)

    return render_template('dealer/agents.html',
                           agents=agents_list,
                           agent_leads_count=agent_leads_count
                           )


@dealer_bp.route('/agents/add', methods=['POST'])
@dealer_required
@kyc_required
def add_agent():
    import re
    dealer_id = get_dealer_id()
    phone = (request.form.get('phone') or '').strip()

    # Validate phone: 10-digit number only
    phone_digits = ''.join(c for c in phone if c.isdigit())[:10]
    phone_valid = len(phone_digits) == 10
    if not phone_valid:
        flash('Phone must be a valid 10-digit number.', 'error')
        return redirect(url_for('dealer.agents'))

    agent_data = {
        'dealer_id': dealer_id,
        'name': request.form.get('name'),
        'email': request.form.get('email'),
        'phone': phone_digits,
        'status': request.form.get('status')
    }

    agent_create(agent_data)
    log_dealer_action(f'Added agent: {agent_data.get("name")}', 'Agents')
    flash('Agent added successfully', 'success')
    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/edit/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def edit_agent(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        phone = (request.form.get('phone') or '').strip()
        phone_digits = ''.join(c for c in phone if c.isdigit())[:10]
        if len(phone_digits) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return redirect(url_for('dealer.agents'))

        update_data = {
            'name': request.form.get('name'),
            'email': request.form.get('email'),
            'phone': phone_digits,
            'status': request.form.get('status')
        }
        agent_update(agent_id, update_data)
        log_dealer_action(f'Updated agent: {update_data.get("name")} (ID:{agent_id})', 'Agents')
        flash('Agent updated successfully', 'success')
    else:
        flash('Agent not found', 'error')

    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/update-status/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def update_agent_status(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        new_status = request.form.get('status')
        agent_update(agent_id, {'status': new_status})
        log_dealer_action(f'Set agent {agent_id} status to {new_status}', 'Agents')
        flash('Agent status updated', 'success')

    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/delete/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def delete_agent(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        agent_name = agent.get('name', f'ID:{agent_id}')
        agent_delete(agent_id)
        log_dealer_action(f'Deleted agent: {agent_name}', 'Agents')
        flash('Agent deleted successfully', 'success')
    else:
        flash('Agent not found', 'error')

    return redirect(url_for('dealer.agents'))

# ========== DEALS ==========


@dealer_bp.route('/deals')
@dealer_required
@kyc_required
def deals():
    dealer_id = get_dealer_id()
    status = request.args.get('status', '')

    deals_list = deals_get_by_dealer(dealer_id, status=status)
    status_counts = deals_get_status_counts(dealer_id)

    return render_template('dealer/deals.html',
                           deals=deals_list,
                           status_counts=status_counts,
                           status_filter=status,
                           statuses=['negotiation', 'booked',
                                     'finalized', 'delivered', 'cancelled']
                           )


@dealer_bp.route('/deals/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def add_deal():
    import re
    dealer_id = get_dealer_id()
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        # Phone validation: 10-digit number only (optional in deal)
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        if customer_phone and len(customer_phone) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)

        # Final price validation
        final_price_raw = request.form.get('final_price')
        if not final_price_raw or float(final_price_raw) <= 0:
            flash('Final Price is required and must be greater than 0.', 'error')
            return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)

        deal_data = {
            'dealer_id': dealer_id,
            'lead_id': request.form.get('lead_id') or None,
            'vehicle_id': int(request.form.get('vehicle_id')),
            'customer_name': request.form.get('customer_name'),
            'customer_phone': customer_phone,
            'customer_email': request.form.get('customer_email'),
            'asking_price': float(request.form.get('asking_price')) if request.form.get('asking_price') else None,
            'final_price': float(final_price_raw),
            'payment_mode': request.form.get('payment_mode'),
            'loan_amount': float(request.form.get('loan_amount')) if request.form.get('loan_amount') else None,
            'down_payment': float(request.form.get('down_payment')) if request.form.get('down_payment') else None,
            'emi_months': int(request.form.get('emi_months')) if request.form.get('emi_months') else None,
            'emi_amount': float(request.form.get('emi_amount')) if request.form.get('emi_amount') else None,
            'bank_name': request.form.get('bank_name'),
            'status': request.form.get('status'),
            'booking_amount': float(request.form.get('booking_amount')) if request.form.get('booking_amount') else 0,
            'notes': request.form.get('notes'),
            # ── Financial Summary (Revenue & Profit Management) ────────────
            'purchase_price': float(request.form.get('purchase_price')) if request.form.get('purchase_price') else 0,
            'transportation_cost': float(request.form.get('transportation_cost')) if request.form.get('transportation_cost') else 0,
            'repair_cost': float(request.form.get('repair_cost')) if request.form.get('repair_cost') else 0,
            'registration_cost': float(request.form.get('registration_cost')) if request.form.get('registration_cost') else 0,
            'marketing_cost': float(request.form.get('marketing_cost')) if request.form.get('marketing_cost') else 0,
            'other_expenses': float(request.form.get('other_expenses')) if request.form.get('other_expenses') else 0,
        }

        deal_id = deal_create(deal_data)
        log_dealer_action(f'Created deal for {deal_data.get("customer_name", "customer")} — ₹{deal_data.get("final_price", 0):,.0f}', 'Sales')
        flash('Deal created successfully', 'success')
        return redirect(url_for('dealer.deals'))

    return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)


@dealer_bp.route('/deals/edit/<int:did_>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_deal(did_):
    dealer_id = get_dealer_id()
    deal = deal_get(did_)

    if not deal or deal['dealer_id'] != dealer_id:
        flash('Deal not found', 'error')
        return redirect(url_for('dealer.deals'))

    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        import re
        # Phone validation: 10-digit number only (optional in deal)
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        if customer_phone and len(customer_phone) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)

        # Final price validation
        final_price_raw = request.form.get('final_price')
        if not final_price_raw or float(final_price_raw) <= 0:
            flash('Final Price is required and must be greater than 0.', 'error')
            return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)

        update_data = {
            'customer_name': request.form.get('customer_name'),
            'customer_phone': customer_phone,
            'customer_email': request.form.get('customer_email'),
            'asking_price': float(request.form.get('asking_price')) if request.form.get('asking_price') else None,
            'final_price': float(final_price_raw),
            'payment_mode': request.form.get('payment_mode'),
            'loan_amount': float(request.form.get('loan_amount')) if request.form.get('loan_amount') else None,
            'down_payment': float(request.form.get('down_payment')) if request.form.get('down_payment') else None,
            'emi_months': int(request.form.get('emi_months')) if request.form.get('emi_months') else None,
            'emi_amount': float(request.form.get('emi_amount')) if request.form.get('emi_amount') else None,
            'bank_name': request.form.get('bank_name'),
            'status': request.form.get('status'),
            'booking_amount': float(request.form.get('booking_amount')) if request.form.get('booking_amount') else 0,
            'notes': request.form.get('notes'),
            # ── Financial Summary (Revenue & Profit Management) ────────────
            'purchase_price': float(request.form.get('purchase_price')) if request.form.get('purchase_price') else 0,
            'transportation_cost': float(request.form.get('transportation_cost')) if request.form.get('transportation_cost') else 0,
            'repair_cost': float(request.form.get('repair_cost')) if request.form.get('repair_cost') else 0,
            'registration_cost': float(request.form.get('registration_cost')) if request.form.get('registration_cost') else 0,
            'marketing_cost': float(request.form.get('marketing_cost')) if request.form.get('marketing_cost') else 0,
            'other_expenses': float(request.form.get('other_expenses')) if request.form.get('other_expenses') else 0,
        }

        deal_update(did_, update_data)
        log_dealer_action(f'Updated deal ID:{did_} for {update_data.get("customer_name", "customer")}', 'Sales')
        flash('Deal updated successfully', 'success')
        return redirect(url_for('dealer.deals'))

    return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)


@dealer_bp.route('/deals/invoice/<int:did_>')
@dealer_required
@kyc_required
def invoice(did_):
    dealer_id = get_dealer_id()
    deal = deal_get(did_)

    if not deal or deal['dealer_id'] != dealer_id:
        flash('Deal not found', 'error')
        return redirect(url_for('dealer.deals'))

    vehicle = deal.get('vehicle')
    dealer_info = g.user

    return render_template('dealer/invoice.html', deal=deal, vehicle=vehicle, dealer=dealer_info)

# ========== FINANCE ==========


@dealer_bp.route('/finance')
@dealer_required
@kyc_required
@feature_required('finance')
def finance():
    dealer_id = get_dealer_id()
    # Finance is computed live from Deal records, so it is always in sync with
    # Deals & Sales / Dashboard / Reports — one query, one source of truth.
    financial = deals_get_financial_summary(dealer_id)
    recent_deals = deals_get_by_dealer(dealer_id)[:10]

    return render_template('dealer/finance.html',
                           total_revenue=financial['total_revenue'],
                           total_deals=financial['total_deals'],
                           loan_deals=financial['loan_deals'],
                           cash_deals=financial['cash_deals'],
                           total_gst=financial['total_gst'],
                           recent_deals=recent_deals,
                           # ── Financial KPIs ──────────────────────────────
                           net_revenue=financial['net_revenue'],
                           total_sales=financial['total_sales'],
                           total_purchase_cost=financial['total_purchase_cost'],
                           total_cost=financial['total_cost'],
                           gross_profit=financial['gross_profit'],
                           total_vehicles_sold=financial['total_vehicles_sold'],
                           )

# ========== DOCUMENTS ==========


@dealer_bp.route('/documents')
@dealer_required
@kyc_required
def documents():
    dealer_id = get_dealer_id()
    # Own uploaded documents (dealer's Documents table)
    docs = documents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']
    # CDS documents assigned to this dealer by admin
    try:
        from db import cds_dealer_active_docs
        cds_docs = cds_dealer_active_docs(dealer_id)
    except Exception:
        cds_docs = []

    return render_template('dealer/documents.html', documents=docs, vehicles=vehicles, cds_docs=cds_docs)


@dealer_bp.route('/documents/upload', methods=['POST'])
@dealer_required
@kyc_required
def upload_document():
    dealer_id = get_dealer_id()

    file = request.files.get('document')
    if not file or not file.filename:
        flash('Please select a file', 'error')
        return redirect(url_for('dealer.documents'))

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['pdf', 'jpg', 'jpeg', 'png']:
        flash('Invalid file type. Allowed: PDF, JPG, PNG', 'error')
        return redirect(url_for('dealer.documents'))

    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))

    doc_data = {
        'dealer_id': dealer_id,
        'vehicle_id': request.form.get('vehicle_id') or None,
        'customer_name': request.form.get('customer_name'),
        'doc_type': request.form.get('doc_type'),
        'filename': filename,
        'original_name': file.filename,
        'notes': request.form.get('notes')
    }

    document_create(doc_data)

    # ── Register in Centralized Document Storage ──────────────────────────────
    try:
        from db import cds_register
        cds_register({
            'dealer_id':     dealer_id,
            'file_name':     filename,
            'original_name': file.filename,
            'file_path':     os.path.join('images', 'uploads', filename),
            'module_name':   'Documents',
            'document_type': request.form.get('doc_type', ''),
            'uploaded_by':   dealer_id,
            'performed_by':  f'dealer:{dealer_id}',
            'user_role':     'Dealer',
        })
    except Exception:
        pass  # Never break the main upload flow
    # ─────────────────────────────────────────────────────────────────────────

    flash('Document uploaded successfully', 'success')
    log_dealer_action(f'Uploaded document: {file.filename}', 'Documents')
    return redirect(url_for('dealer.documents'))


@dealer_bp.route('/documents/delete/<int:docid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_document(docid):
    dealer_id = get_dealer_id()
    docs = documents_get_by_dealer(dealer_id)
    doc = next((d for d in docs if d['id'] == docid), None)

    if doc:
        # Delete file
        filepath = os.path.join(
            current_app.config['UPLOAD_FOLDER'], doc['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        document_delete(docid)
        log_dealer_action(f'Deleted document: {doc.get("original_name", doc.get("filename"))}', 'Documents')
        flash('Document deleted successfully', 'success')
    else:
        flash('Document not found', 'error')

    return redirect(url_for('dealer.documents'))

# ========== REPORTS ==========


@dealer_bp.route('/reports')
@dealer_required
@kyc_required
@feature_required('reports')
def reports():
    dealer_id = get_dealer_id()

    inventory_summary = vehicles_inventory_summary(dealer_id)
    lead_stages = leads_get_stage_counts(dealer_id)
    total_leads = sum(lead_stages.values())
    converted = lead_stages.get('converted', 0)
    conversion_rate = round(
        (converted / total_leads * 100) if total_leads > 0 else 0)

    sources = leads_get_source_counts(dealer_id)
    fuels = vehicles_get_fuel_breakdown(dealer_id)
    monthly = deals_get_monthly_revenue(dealer_id)
    all_deals = deals_get_by_dealer(dealer_id)
    financial = deals_get_financial_summary(dealer_id)

    return render_template('dealer/reports.html',
                           total_revenue=financial['net_revenue'],
                           total_inventory=inventory_summary['total'],
                           available_count=inventory_summary['available'],
                           sold_count=inventory_summary['sold'],
                           conversion_rate=conversion_rate,
                           converted=converted,
                           total_leads=total_leads,
                           all_deals=all_deals,
                           sources=sources,
                           fuels=fuels,
                           monthly=monthly,
                           # ── Financial Summary totals for export/print ──────
                           net_revenue=financial['net_revenue'],
                           total_sales=financial['total_sales'],
                           total_purchase_cost=financial['total_purchase_cost'],
                           total_cost=financial['total_cost'],
                           gross_profit=financial['gross_profit'],
                           total_vehicles_sold=financial['total_vehicles_sold'],
                           )

# ========== INQUIRIES ==========


@dealer_bp.route('/inquiries')
@dealer_required
@kyc_required
def inquiries():
    dealer_id = get_dealer_id()
    inquiries_list = inquiries_get_by_dealer(dealer_id)

    return render_template('dealer/inquiries.html', inquiries=inquiries_list)


@dealer_bp.route('/inquiries/respond/<int:iid>', methods=['POST'])
@dealer_required
@kyc_required
def respond_inquiry(iid):
    dealer_id = get_dealer_id()
    inquiries_list = inquiries_get_by_dealer(dealer_id)
    inquiry = next((i for i in inquiries_list if i['id'] == iid), None)

    if inquiry:
        inquiry_update_status(iid, 'responded')
        log_dealer_action(f'Responded to inquiry #{iid}', 'Inquiries')
        flash('Inquiry marked as responded', 'success')
    else:
        flash('Inquiry not found', 'error')

    return redirect(url_for('dealer.inquiries'))

# ========== SUBSCRIPTION ==========


# ── Plan definitions (single source of truth) ─────────────────────────────
PLANS = [
    {
        'name': 'Starter', 'key': 'starter', 'price_inr': 0, 'period': 'month',
        'recommended': False,
        'features': ['25 Listings', '50 Leads/month', 'Basic CRM', '100MB Storage']
    },
    {
        'name': 'Growth', 'key': 'growth', 'price_inr': 2999, 'period': 'month',
        'recommended': True,
        'features': ['100 Listings', '500 Leads/month', 'Full CRM', '5GB Storage', 'EMI Calculator', 'Analytics Dashboard']
    },
    {
        'name': 'Pro', 'key': 'pro', 'price_inr': 5999, 'period': 'month',
        'recommended': False,
        'features': ['Unlimited Listings', 'Unlimited Leads', 'Multi-Branch Support', 'Staff Roles', 'Priority Support', 'API Access']
    },
]

def _get_razorpay_client():
    import razorpay
    return razorpay.Client(auth=(
        current_app.config['RAZORPAY_KEY_ID'],
        current_app.config['RAZORPAY_KEY_SECRET']
    ))


def _generate_demo_transaction_id():
    """Generate a transaction id like DEMO-20260001 (year + running sequence)."""
    from models import DealerPayment
    year = _now_ist().strftime('%Y')
    count_this_year = DealerPayment.query.filter(
        DealerPayment.transaction_id.like(f'DEMO-{year}%')
    ).count()
    return f'DEMO-{year}{str(count_this_year + 1).zfill(4)}'


@dealer_bp.route('/subscription')
@dealer_required
def subscription():
    from models import DealerSubscription

    dealer_id = get_dealer_id()

    # Current/most-recent active subscription record (demo system).
    current_sub = (DealerSubscription.query
                   .filter_by(dealer_id=dealer_id, is_active=True)
                   .order_by(DealerSubscription.activated_at.desc())
                   .first())

    razorpay_enabled = current_app.config.get('RAZORPAY_ENABLED', False)
    allow_free_plan  = current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True)

    return render_template(
        'dealer/subscription.html',
        plans=PLANS,
        dealer=g.user,
        current_sub=current_sub,
        razorpay_enabled=razorpay_enabled,
        allow_free_plan=allow_free_plan,
    )


# ── Plan Activation (AJAX) — Demo payment or Free-for-now ─────────────────
# Structured so that flipping RAZORPAY_ENABLED = True later only requires
# adding the real Razorpay order-create/verify calls inside the `if`
# branch below — the rest of the subscription logic stays unchanged.
@dealer_bp.route('/subscription/activate', methods=['POST'])
@dealer_required
def activate_subscription():
    from models import DealerSubscription, DealerPayment
    from datetime import timedelta

    data = request.get_json(silent=True) or {}
    plan_key = (data.get('plan') or '').lower()
    method   = (data.get('method') or 'demo').lower()   # 'demo' or 'free'

    plan = next((p for p in PLANS if p['key'] == plan_key), None)
    if not plan or plan_key == 'starter':
        return jsonify({'success': False, 'error': 'Invalid plan selected.'}), 400

    dealer_id   = get_dealer_id()
    dealer_name = g.user.get('name', '') if g.user else ''

    razorpay_enabled = current_app.config.get('RAZORPAY_ENABLED', False)

    if razorpay_enabled:
        # ── FUTURE: Razorpay live payment flow goes here ───────────────
        # e.g. create a Razorpay order via _get_razorpay_client() and
        # return order details to the frontend for checkout, then verify
        # the signature in a separate /subscription/verify-payment route
        # before calling the same activation code below.
        return jsonify({'success': False, 'error': 'Online payments are not enabled yet. Please contact support.'}), 400

    # ── Demo payment flow (Razorpay disabled) ──────────────────────────
    if method == 'free':
        allow_free_plan = current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True)
        if not allow_free_plan:
            return jsonify({'success': False, 'error': 'Free plan activation is currently disabled by admin.'}), 403

        amount         = 0
        payment_method = 'Free'
        payment_status = 'Free Trial'
        transaction_id = None
        success_msg    = f'{plan["name"]} plan activated for free (admin-enabled trial).'
    else:
        amount         = plan['price_inr']
        payment_method = 'Demo'
        payment_status = 'Pending'
        transaction_id = _generate_demo_transaction_id()
        success_msg    = 'Demo payment successful. Your subscription has been activated.'

    # Deactivate any previously active subscription for this dealer
    DealerSubscription.query.filter_by(dealer_id=dealer_id, is_active=True) \
        .update({'is_active': False})

    expiry = _now_ist() + timedelta(days=30)
    sub = DealerSubscription(
        dealer_id=dealer_id,
        plan_name=plan['key'],
        price=plan['price_inr'],
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        activated_at=_now_ist(),
        expires_at=expiry,
        is_active=True,
    )
    db.session.add(sub)
    db.session.flush()   # get sub.id before commit

    payment = DealerPayment(
        dealer_id=dealer_id,
        subscription_id=sub.id,
        amount=amount,
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        notes=f'{dealer_name} activated {plan["name"]} plan via {payment_method} flow.',
    )
    db.session.add(payment)

    # Keep the legacy User.subscription_* fields in sync for the rest of
    # the app (feature gating, dashboards, etc.) that already read them.
    user_update_subscription(dealer_id, plan['key'], expiry)

    db.session.commit()

    log_dealer_action(
        f'Activated subscription plan: {plan["name"]} via {payment_method}'
        + (f' (Txn: {transaction_id})' if transaction_id else ''),
        'Subscription'
    )

    return jsonify({
        'success':        True,
        'plan':           plan['key'],
        'plan_name':      plan['name'],
        'payment_method': payment_method,
        'payment_status': payment_status,
        'amount':         amount,
        'transaction_id': transaction_id,
        'message':        success_msg,
    })

# ========== MY ACCOUNT ==========


@dealer_bp.route('/my-account', methods=['GET', 'POST'])
@dealer_required
def my_account():
    from models import db, User
    dealer_id = get_dealer_id()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_profile':
            user = User.query.get(dealer_id)
            if user:
                name = request.form.get('name', '').strip()
                phone = request.form.get('phone', '').strip()
                if name:
                    user.name = name
                user.phone = phone
                db.session.commit()
                # Refresh session
                session['user_name'] = user.name
                log_dealer_action('Updated profile information', 'Profile')
                flash('Profile updated successfully!', 'success')
            return redirect(url_for('dealer.my_account'))

        elif action == 'change_password':
            user = User.query.get(dealer_id)
            if user:
                current_password = request.form.get('current_password', '')
                new_password = request.form.get('new_password', '')
                confirm_password = request.form.get('confirm_password', '')

                if not user.check_password(current_password):
                    log_dealer_action('Failed password change attempt (wrong current password)',
                                       'Profile', status='Failed')
                    flash('Current password is incorrect.', 'error')
                elif len(new_password) < 6:
                    flash('New password must be at least 6 characters.', 'error')
                elif new_password != confirm_password:
                    flash('New passwords do not match.', 'error')
                else:
                    user.set_password(new_password)
                    db.session.commit()
                    log_dealer_action('Changed account password', 'Profile')
                    flash('Password changed successfully!', 'success')
            return redirect(url_for('dealer.my_account'))

    return render_template('dealer/my_account.html', current_user=g.user)


# ========== WEBSITE SETTINGS ==========


@dealer_bp.route('/website-settings', methods=['GET', 'POST'])
@dealer_required
@kyc_required
@feature_required('mini_website')
def website_settings():
    """Dealer can set their website_name, upload website_logo, and fill contact info."""
    dealer_id = get_dealer_id()
    dealer_user = User.query.get(dealer_id)

    if request.method == 'POST':
        # ── Website name: sanitise to a clean slug ─────────────────────────
        raw_name = request.form.get('website_name', '').strip()
        slug_error = None

        if raw_name:
            import re
            # Keep only alphanumerics, hyphens, underscores; convert spaces → hyphens
            slug = re.sub(r'[^a-zA-Z0-9_-]', '-', raw_name.strip())
            slug = re.sub(r'-{2,}', '-', slug).strip('-')   # collapse multiple hyphens
            slug = slug[:80]                                  # max length

            if not slug:
                slug_error = 'Website name can only contain letters, numbers, hyphens and underscores.'
            else:
                # Check for duplicate (exclude current dealer)
                existing = User.query.filter(
                    User.role == 'dealer',
                    User.website_name == slug,
                    User.id != dealer_id
                ).first()
                if existing:
                    slug_error = f'The name "{slug}" is already taken by another dealer. Please choose a different name.'
                else:
                    dealer_user.website_name = slug

        if slug_error:
            flash(slug_error, 'error')
            # Re-render with error (don't save anything)
            return render_template('dealer/website_settings.html', dealer=dealer_user)

        # ── Optional logo upload ─────────────────────────────────────────
        logo_file = request.files.get('website_logo')
        if logo_file and logo_file.filename and is_allowed_image(logo_file.filename):
            logo_fname = save_uploaded_image(
                logo_file, current_app.config['UPLOAD_FOLDER'],
                f"wlogo_{dealer_id}_{uuid.uuid4().hex[:8]}",
                vehicle_mode=False
            )
            dealer_user.website_logo = logo_fname

        # ── Other fields ─────────────────────────────────────────────────
        dealer_user.whatsapp_number = request.form.get(
            'whatsapp_number', '').strip() or dealer_user.whatsapp_number
        dealer_user.address = request.form.get('address', '').strip()
        dealer_user.google_maps_url = request.form.get(
            'google_maps_url', '').strip()
        dealer_user.years_in_business = request.form.get(
            'years_in_business', type=int) or dealer_user.years_in_business
        dealer_user.business_hours = request.form.get(
            'business_hours', '').strip()

        db.session.commit()
        log_dealer_action('Updated website settings', 'Website')
        flash('Website settings saved!', 'success')
        return redirect(url_for('dealer.website_settings'))

    return render_template('dealer/website_settings.html', dealer=dealer_user)
