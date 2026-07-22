"""
app.py — Caryanams DMS
PostgreSQL Migration (v27):
  - SQLALCHEMY_DATABASE_URI now reads from DATABASE_URL env var (PostgreSQL).
  - All inline PRAGMA-based SQLite column-detection replaced with
    information_schema queries that work on PostgreSQL.
  - SQLite-only DDL (AUTOINCREMENT, PRAGMA foreign_keys, INTEGER PRIMARY KEY)
    replaced with PostgreSQL-compatible equivalents.
  - All business logic, routes, templates, and features are unchanged.
"""

from flask import Flask, session, g, flash
import os


def _pg_column_exists(conn, table_name, column_name):
    """
    PostgreSQL-compatible column existence check.
    Replaces: PRAGMA table_info(<table>)
    """
    from sqlalchemy import text
    result = conn.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_name = :tbl
          AND column_name = :col
    """), {"tbl": table_name, "col": column_name})
    return result.scalar() > 0


def _pg_table_exists(conn, table_name):
    """
    PostgreSQL-compatible table existence check.
    Replaces: SELECT name FROM sqlite_master WHERE type='table'
    """
    from sqlalchemy import text
    result = conn.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = :tbl
    """), {"tbl": table_name})
    return result.scalar() > 0


def create_app():
    app = Flask(__name__)
    app.secret_key = 'Caryanams-secret-2025-xK9mP'
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'images', 'uploads')
    app.config['KYC_UPLOAD_FOLDER']     = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'dealers')
    app.config['VEHICLE_UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'vehicles')
    app.config['ALLOWED_IMAGE_EXTENSIONS'] = {'jpg', 'jpeg', 'png', 'webp'}
    app.config['MAX_IMAGE_SIZE']        = 10 * 1024 * 1024   # 10 MB per image

    # ── Dealer KYC document verification engine (utils/kyc_engine) ─────────
    # Real image-quality + OCR checks run on the 3 dealer KYC docs
    # (Aadhaar Front, Aadhaar Back, PAN) at upload time, before an admin
    # ever sees them. Thresholds are tunable via env vars in production.
    app.config['KYC_ALLOWED_EXTENSIONS']            = {'jpg', 'jpeg', 'png', 'webp', 'pdf'}
    app.config['KYC_MAX_UPLOAD_BYTES']              = 10 * 1024 * 1024   # 10 MB hard cap
    app.config['KYC_MIN_WIDTH']                     = int(os.environ.get('KYC_MIN_WIDTH', 1000))
    app.config['KYC_MIN_HEIGHT']                    = int(os.environ.get('KYC_MIN_HEIGHT', 700))
    app.config['KYC_GLARE_BRIGHT_PIXEL_RATIO']      = float(os.environ.get('KYC_GLARE_BRIGHT_PIXEL_RATIO', 0.06))
    app.config['KYC_DARK_MEAN_BRIGHTNESS']          = float(os.environ.get('KYC_DARK_MEAN_BRIGHTNESS', 60))
    app.config['KYC_OVEREXPOSED_MEAN_BRIGHTNESS']   = float(os.environ.get('KYC_OVEREXPOSED_MEAN_BRIGHTNESS', 225))
    app.config['KYC_BLUR_LAPLACIAN_THRESHOLD']      = float(os.environ.get('KYC_BLUR_LAPLACIAN_THRESHOLD', 120.0))
    app.config['KYC_NAME_SIMILARITY_THRESHOLD']     = float(os.environ.get('KYC_NAME_SIMILARITY_THRESHOLD', 0.90))
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024   # 100 MB

    # ── PostgreSQL Database URI ────────────────────────────────────────────────
    # Set DATABASE_URL in your .env file:
    #   DATABASE_URL=postgresql://caryanams_user:yourpassword@localhost:5432/caryanams_db
    # Falls back to SQLite only if DATABASE_URL is not set (for local dev without PG).
    _db_url = os.environ.get('DATABASE_URL', 'sqlite:///Caryanams.db')
    # Heroku / some PaaS providers emit postgres:// — SQLAlchemy requires postgresql://
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # ── Razorpay Payment Gateway ───────────────────────────────────────────────
    # ⚠️  ONLY these two lines need your real keys — get them from:
    #     https://dashboard.razorpay.com → Settings → API Keys
    # For testing use Test keys (rzp_test_...), for live use Live keys (rzp_live_...)
    app.config['ANTHROPIC_API_KEY']   = os.environ.get('ANTHROPIC_API_KEY', '')

    # ── Groq AI Chatbot (Caryanams Assistant) ────────────────────────────────────────────
    # Free key: https://console.groq.com/keys — put GROQ_API_KEY in your .env
    app.config['GROQ_API_KEY'] = os.environ.get('GROQ_API_KEY', '')
    app.config['GROQ_MODEL']   = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
    app.config['RAZORPAY_KEY_ID']     = os.environ.get('RAZORPAY_KEY_ID',     'rzp_test_XXXXXXXXXXXXXXXX')
    app.config['RAZORPAY_KEY_SECRET'] = os.environ.get('RAZORPAY_KEY_SECRET', 'XXXXXXXXXXXXXXXXXXXXXXXX')

    # ── Subscription payment switches ──────────────────────────────────────
    # Razorpay stays OFF until real API keys are added — set RAZORPAY_ENABLED
    # to True (and RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET env vars) to flip
    # subscription checkout over to the live gateway later.
    app.config['RAZORPAY_ENABLED'] = os.environ.get('RAZORPAY_ENABLED', 'False') == 'True'
    # Admin-controlled switch for the "Use Free For Now" demo activation button.
    app.config['ALLOW_FREE_PLAN_ACTIVATION'] = os.environ.get('ALLOW_FREE_PLAN_ACTIVATION', 'True') == 'True'
    # ─────────────────────────────────────────────────────────────────────────

    # ── WhatsApp Cloud API — customer inquiry confirmation ─────────────────
    # OFF by default until real credentials + an approved message template
    # are configured (see utils/whatsapp.py header for full setup steps).
    # Safe no-op otherwise: inquiries still save normally, just no WhatsApp
    # message goes out (logged as 'skipped' in whatsapp_message_log).
    app.config['WHATSAPP_ENABLED']              = os.environ.get('WHATSAPP_ENABLED', 'False') == 'True'
    app.config['WHATSAPP_ACCESS_TOKEN']         = os.environ.get('WHATSAPP_ACCESS_TOKEN', '')
    app.config['WHATSAPP_PHONE_NUMBER_ID']      = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '')
    app.config['WHATSAPP_API_VERSION']          = os.environ.get('WHATSAPP_API_VERSION', 'v20.0')
    app.config['WHATSAPP_COUNTRY_CODE']         = os.environ.get('WHATSAPP_COUNTRY_CODE', '91')
    app.config['WHATSAPP_INQUIRY_TEMPLATE_NAME'] = os.environ.get('WHATSAPP_INQUIRY_TEMPLATE_NAME', 'inquiry_confirmation')
    app.config['WHATSAPP_TEMPLATE_LANGUAGE']    = os.environ.get('WHATSAPP_TEMPLATE_LANGUAGE', 'en_US')
    # ─────────────────────────────────────────────────────────────────────────

    # ── Bank / UPI Fallback Payment Details ───────────────────────────────────
    # Shown when Razorpay is not yet configured (or as an alternative UPI QR).
    # Update these with your real details via env vars or directly here.
    app.config['BANK_UPI_CONFIG'] = {
        'upi_id':       os.environ.get('UPI_ID',       'caryanams@upi'),
        'upi_name':     os.environ.get('UPI_NAME',     'Caryanams Payments'),
        'bank_name':    os.environ.get('BANK_NAME',    'HDFC Bank'),
        'account_name': os.environ.get('BANK_ACNAME',  'Caryanams Pvt Ltd'),
        'account_no':   os.environ.get('BANK_ACCNO',   '50200012345678'),
        'ifsc':         os.environ.get('BANK_IFSC',    'HDFC0001234'),
        'branch':       os.environ.get('BANK_BRANCH',  'Main Branch'),
    }
    # ─────────────────────────────────────────────────────────────────────────

    # ── Public base URL — used for minisite full URLs ──────────────────────────
    # Set APP_URL env var in production: export APP_URL=https://yourdomain.com
    # (or https://yourdomain.com/app if deployed under a subdirectory).
    # If APP_URL is not set, the minisite_url() helpers below derive the base
    # URL dynamically from the incoming request's origin (request.url_root),
    # so local dev automatically resolves to http://localhost:5000 and a live
    # deployment automatically resolves to its real domain — no hardcoding.
    _raw_app_url = os.environ.get('APP_URL', '').rstrip('/')
    app.config['APP_URL'] = _raw_app_url

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['KYC_UPLOAD_FOLDER'],     exist_ok=True)
    os.makedirs(app.config['VEHICLE_UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(__file__), 'static', 'processed'),           exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(__file__), 'static', 'custom_bgs'),         exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(__file__), 'static', 'images', 'defaults'), exist_ok=True)

    # ── Ensure default_car.jpg placeholder always exists ─────────────────────
    _default_car_path = os.path.join(
        os.path.dirname(__file__), 'static', 'images', 'defaults', 'default_car.jpg'
    )
    if not os.path.isfile(_default_car_path):
        try:
            from PIL import Image as _PI, ImageDraw as _PID, ImageFont as _PIF
            W, H = 1280, 720
            _im = _PI.new('RGB', (W, H), (240, 240, 240))
            _dr = _PID.Draw(_im)
            _cc, _oc = (180,180,190), (120,120,130)
            _dr.polygon([(200,420),(200,380),(280,280),(420,240),(640,230),(820,240),(960,280),(1060,380),(1060,420)], fill=_cc, outline=_oc)
            _dr.rectangle([200,400,1060,470], fill=_cc, outline=_oc)
            _dr.polygon([(300,380),(380,270),(640,250),(840,260),(940,380)], fill=(200,200,210), outline=_oc)
            _dr.polygon([(320,375),(385,275),(590,258),(590,375)], fill=(160,200,220), outline=_oc)
            _dr.polygon([(610,258),(840,265),(930,375),(610,375)], fill=(160,200,220), outline=_oc)
            _dr.ellipse([290,430,430,530], fill=(60,60,70), outline=_oc)
            _dr.ellipse([320,455,400,505], fill=(200,200,200))
            _dr.ellipse([830,430,970,530], fill=(60,60,70), outline=_oc)
            _dr.ellipse([860,455,940,505], fill=(200,200,200))
            _dr.ellipse([195,370,230,400], fill=(255,250,200), outline=_oc)
            _dr.rectangle([1055,375,1070,405], fill=(220,50,50), outline=_oc)
            try:
                _fn = _PIF.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 36)
                _fn2 = _PIF.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
            except Exception:
                _fn = _fn2 = _PIF.load_default()
            _dr.text((W//2, 600), 'No Image Available', fill=(150,150,160), font=_fn, anchor='mm')
            _dr.text((W//2, 645), 'Vehicle image will appear here', fill=(170,170,180), font=_fn2, anchor='mm')
            _im.save(_default_car_path, 'JPEG', quality=85, optimize=True)
        except Exception:
            try:
                from PIL import Image as _PIX
                _PIX.new('RGB', (4,3), (200,200,200)).save(_default_car_path, 'JPEG')
            except Exception:
                pass

    from extensions import db, login_manager, csrf
    db.init_app(app)
    login_manager.init_app(app)

    # ── CSRF Protection (Flask-WTF) ─────────────────────────────────────────
    # Protects every state-changing request (POST/PUT/PATCH/DELETE) across
    # ALL blueprints (auth, dealer, admin, user, minisite, background,
    # policies) against Cross-Site Request Forgery. Templates send the token
    # via a hidden {{ csrf_token() }} field; AJAX calls send it automatically
    # via the X-CSRFToken header (see static/js/csrf.js).
    app.config['WTF_CSRF_TIME_LIMIT'] = None          # tokens don't expire mid-session
    app.config['WTF_CSRF_SSL_STRICT'] = False          # allow http:// in local/dev
    csrf.init_app(app)

    # ── Register Blueprints first so their models are imported ────────────────
    # FIX: StudioImage is defined inside background/routes.py (not models.py).
    # If background_bp is registered AFTER db.create_all(), SQLAlchemy never
    # sees StudioImage and skips creating the studio_image table.
    # Solution: register blueprints BEFORE the app context / create_all block.
    from auth.routes       import auth_bp
    from dealer.routes     import dealer_bp
    from user.routes       import user_bp
    from background.routes import background_bp   # ← imports StudioImage into metadata
    from minisite.routes   import minisite_bp
    from admin.routes      import admin_bp
    from policies.routes   import policies_bp         # ← Privacy & Refund Policy pages
    from chatbot.routes    import chatbot_bp           # ← Caryanams Assistant (Groq-powered, multilingual)

    app.register_blueprint(auth_bp,        url_prefix='/auth')
    app.register_blueprint(dealer_bp,      url_prefix='/dealer')
    app.register_blueprint(user_bp,        url_prefix='/')
    app.register_blueprint(background_bp,  url_prefix='/studio')
    app.register_blueprint(minisite_bp,    url_prefix='')
    app.register_blueprint(admin_bp,       url_prefix='/admin')
    app.register_blueprint(policies_bp,    url_prefix='')
    app.register_blueprint(chatbot_bp,     url_prefix='/chatbot')

    # ── Warm up vehicle-photo AI classifier in background (Add Vehicle) ───────
    # Loads CLIP once at startup so the first dealer upload isn't the one
    # that pays the model-load cost. Safe no-op if torch/transformers
    # aren't installed yet — logs a warning instead of crashing the app.
    try:
        from utils.vehicle_photo_ai import warm_up_in_background
        warm_up_in_background()
    except Exception as e:
        app.logger.warning(f'Vehicle photo AI warm-up skipped: {e}')

    # ── Create all tables (including studio_image and admin_logs) ─────────────
    from models import (seed_demo_data, AdminLog, SubAdmin,
                        CentralDocumentStorage, CentralDocumentAuditLog,
                        LeadImportFile, ImportedLead, LeadAssignmentHistory,
                        VisitorLog, WhatsAppMessageLog)
    with app.app_context():
        db.create_all()        # now sees ALL models including StudioImage + Lead Import

        # ── Migrate: add new columns to admin_logs if they don't exist ──────────
        # Uses information_schema instead of SQLite PRAGMA table_info
        try:
            from sqlalchemy import text
            with db.engine.connect() as conn:
                if not _pg_column_exists(conn, 'admin_logs', 'user_role'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN user_role VARCHAR(30) DEFAULT 'Admin'"))
                if not _pg_column_exists(conn, 'admin_logs', 'status'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN status VARCHAR(20) DEFAULT 'Success'"))
                # ── Accurate Activity Logs fix: new optional columns ────────────
                if not _pg_column_exists(conn, 'admin_logs', 'user_id'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN user_id INTEGER"))
                if not _pg_column_exists(conn, 'admin_logs', 'description'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN description TEXT"))
                if not _pg_column_exists(conn, 'admin_logs', 'device'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN device VARCHAR(20)"))
                if not _pg_column_exists(conn, 'admin_logs', 'browser'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN browser VARCHAR(80)"))
                if not _pg_column_exists(conn, 'admin_logs', 'timezone'):
                    conn.execute(text("ALTER TABLE admin_logs ADD COLUMN timezone VARCHAR(50) DEFAULT 'Asia/Kolkata (IST)'"))
                conn.commit()
        except Exception:
            pass

        # ── Migrate: visitor_logs table columns (create_all handles new DBs) ──
        # PostgreSQL: CREATE TABLE IF NOT EXISTS uses SERIAL instead of AUTOINCREMENT.
        # db.create_all() already creates the table from the VisitorLog model,
        # so we only need to add any columns that may be missing on older DBs.
        try:
            from sqlalchemy import text as _vt
            with db.engine.connect() as _vc:
                # Add columns that were added in later versions — safe no-ops on fresh DBs
                if not _pg_column_exists(_vc, 'visitor_logs', 'session_id'):
                    _vc.execute(_vt("ALTER TABLE visitor_logs ADD COLUMN session_id VARCHAR(64)"))
                if not _pg_column_exists(_vc, 'visitor_logs', 'user_id'):
                    _vc.execute(_vt("ALTER TABLE visitor_logs ADD COLUMN user_id INTEGER"))
                if not _pg_column_exists(_vc, 'visitor_logs', 'visitor_name'):
                    _vc.execute(_vt("ALTER TABLE visitor_logs ADD COLUMN visitor_name VARCHAR(100)"))
                if not _pg_column_exists(_vc, 'visitor_logs', 'visitor_role'):
                    _vc.execute(_vt("ALTER TABLE visitor_logs ADD COLUMN visitor_role VARCHAR(30)"))
                _vc.commit()
        except Exception:
            pass

        # ── Migrate: Revenue & Profit Management System — new deals columns ────
        # Adds Financial Summary fields to the deals table on existing DBs.
        # db.create_all() already creates them on fresh databases.
        try:
            from sqlalchemy import text as _dft
            with db.engine.connect() as _dfc:
                _deal_financial_columns = [
                    ('purchase_price',      "FLOAT DEFAULT 0"),
                    ('transportation_cost', "FLOAT DEFAULT 0"),
                    ('repair_cost',         "FLOAT DEFAULT 0"),
                    ('registration_cost',   "FLOAT DEFAULT 0"),
                    ('marketing_cost',      "FLOAT DEFAULT 0"),
                    ('total_cost',          "FLOAT DEFAULT 0"),
                    ('other_expenses',      "FLOAT DEFAULT 0"),
                    ('gross_profit',        "FLOAT DEFAULT 0"),
                    ('net_profit',          "FLOAT DEFAULT 0"),
                ]
                for _col_name, _col_def in _deal_financial_columns:
                    if not _pg_column_exists(_dfc, 'deals', _col_name):
                        _dfc.execute(_dft(f"ALTER TABLE deals ADD COLUMN {_col_name} {_col_def}"))
                _dfc.commit()
        except Exception:
            pass

        # ── Migrate: make leads.dealer_id nullable on existing PostgreSQL DBs ──
        # PostgreSQL supports ALTER COLUMN … DROP NOT NULL directly — no table rebuild needed.
        try:
            from sqlalchemy import text as _lt
            with db.engine.connect() as _lc:
                # Check whether dealer_id column is NOT NULL in PostgreSQL
                _nn_check = _lc.execute(_lt("""
                    SELECT is_nullable
                    FROM information_schema.columns
                    WHERE table_name = 'leads'
                      AND column_name = 'dealer_id'
                """)).fetchone()
                if _nn_check and _nn_check[0] == 'NO':
                    # PostgreSQL supports DROP NOT NULL directly — no table rebuild
                    _lc.execute(_lt("ALTER TABLE leads ALTER COLUMN dealer_id DROP NOT NULL"))
                _lc.commit()
        except Exception:
            pass

        seed_demo_data()

        # ── Migrate: Reassign display IDs (D1/D2, U1/U2, SA1/SA2) ───────────
        try:
            from sqlalchemy import text as _text
            with db.engine.connect() as _conn:
                if not _pg_column_exists(_conn, 'sub_admins', 'display_id'):
                    _conn.execute(_text("ALTER TABLE sub_admins ADD COLUMN display_id TEXT"))
                if not _pg_column_exists(_conn, 'users', 'display_id'):
                    _conn.execute(_text("ALTER TABLE users ADD COLUMN display_id TEXT"))
                for _role, _pfx in (('dealer', 'D'), ('user', 'U')):
                    _rows = _conn.execute(_text(
                        "SELECT id FROM users WHERE role=:role "
                        "ORDER BY COALESCE(created_at, '1970-01-01'::timestamp), id"
                    ), {"role": _role}).fetchall()
                    for _i, _r in enumerate(_rows, 1):
                        _conn.execute(_text("UPDATE users SET display_id=:d WHERE id=:i"),
                                      {"d": f"{_pfx}{_i}", "i": _r[0]})
                _sa_rows = _conn.execute(_text(
                    "SELECT id FROM sub_admins ORDER BY COALESCE(created_at, '1970-01-01'::timestamp), id"
                )).fetchall()
                for _i, _r in enumerate(_sa_rows, 1):
                    _conn.execute(_text("UPDATE sub_admins SET display_id=:d WHERE id=:i"),
                                  {"d": f"SA{_i}", "i": _r[0]})
                _conn.commit()
        except Exception:
            pass

    from db import user_get_by_id

    @app.before_request
    def load_user():
        uid = session.get('user_id')
        g.user = user_get_by_id(uid) if uid else None

    @app.context_processor
    def inject_user():
        return dict(current_user=g.user)  # None when not logged in

    # ── Subscription feature gate — usable in ALL templates as feature_allowed('finance') ──
    @app.context_processor
    def inject_feature_gate():
        from subscription_features import feature_allowed
        return dict(feature_allowed=feature_allowed)

    # ── Minisite URL helper — available in ALL templates ──────────────────────
    # Usage in Jinja:  {{ minisite_url('ABC') }}
    # Returns:  https://yourdomain.com/caryanams/ABC
    @app.context_processor
    def inject_minisite_url():
        def minisite_url(dealer_name, website_name):
            if not website_name:
                return ''
            # Prefer the live incoming request's own origin first, so the
            # displayed/shared link always matches whatever domain/IP the
            # browser is actually using right now (localhost, LAN IP, or
            # the real live domain). Fall back to the static APP_URL env
            # config only if the request origin can't be determined
            # (e.g. outside of a request context, such as a CLI script).
            base = ''
            from flask import request as _req
            try:
                base = _req.url_root.rstrip('/')
            except RuntimeError:
                base = ''
            if not base:
                base = app.config.get('APP_URL', '').rstrip('/')
            if not base:
                base = 'http://localhost:5000'
            d_slug = (dealer_name or '').strip().lower().replace(' ', '')
            w_slug = website_name.strip().lower().replace(' ', '-')
            return f'{base}/caryanams/{d_slug}/{w_slug}'
        return dict(minisite_url=minisite_url)

    @app.template_global('minisite_url')
    def minisite_url_global(dealer_name, website_name):
        if not website_name:
            return ''
        base = ''
        from flask import request as _req
        try:
            base = _req.url_root.rstrip('/')
        except RuntimeError:
            base = ''
        if not base:
            base = app.config.get('APP_URL', '').rstrip('/')
        if not base:
            base = 'http://localhost:5000'
        d_slug = (dealer_name or '').strip().lower().replace(' ', '')
        w_slug = website_name.strip().lower().replace(' ', '-')
        return f'{base}/caryanams/{d_slug}/{w_slug}'

    @app.template_filter('fmtdate')
    def fmtdate(s, fmt='%d %b %Y'):
        if not s:
            return '—'
        try:
            from datetime import datetime, timedelta
            if isinstance(s, str):
                s = s[:19]
                dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
            else:
                dt = s
            # Convert stored UTC → IST (UTC+5:30)
            dt = dt + timedelta(hours=5, minutes=30)
            return dt.strftime(fmt)
        except Exception:
            return str(s)[:10]

    @app.template_filter('fmtprice')
    def fmtprice(v):
        try:
            return '₹{:,.0f}'.format(float(v))
        except Exception:
            return '—'

    # ── Global JSON error handlers (prevent HTML error pages reaching JS) ─────
    # FIX: Without these, Flask returns HTML 404/500 pages which cause the
    # "Unexpected token '<'" JSON parse error in the frontend fetch() calls.
    # Covers all status codes. Uses success:false format matching upload route.
    from flask import jsonify
    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        # Fires when a form/AJAX request is missing a valid CSRF token —
        # e.g. an expired session, a token stripped by a proxy, or a real
        # forged cross-site request. Return JSON (not Flask-WTF's default
        # HTML page) so existing fetch() calls across the app can parse it,
        # and flash a friendly message for normal <form> submissions too.
        try:
            flash('Your session security check failed (page may have been open too long). Please try again.', 'error')
        except Exception:
            pass
        return jsonify({
            'success': False,
            'error': 'Security check failed (invalid or missing CSRF token). Please refresh the page and try again.',
            'code': 400
        }), 400

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({'success': False, 'error': 'Bad request', 'code': 400}), 400

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({'success': False, 'error': 'Forbidden', 'code': 403}), 403

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'success': False, 'error': 'Not found', 'code': 404}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({'success': False, 'error': 'Method not allowed', 'code': 405}), 405

    @app.errorhandler(413)
    def request_too_large(e):
        return jsonify({'success': False, 'error': 'File too large. Maximum size is 100 MB.', 'code': 413}), 413

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({'success': False, 'error': 'Internal server error', 'code': 500}), 500

    return app

app = create_app()

if __name__ == '__main__':
    
    app.run(debug=True, port=5000)
