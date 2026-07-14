from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'

# ── CSRF Protection (app-wide) ──────────────────────────────────────────────
# Protects every POST/PUT/PATCH/DELETE route in the app (all blueprints).
# Forms must include {{ csrf_token() }} as a hidden field; JS fetch/XHR
# calls get the token automatically via static/js/csrf.js (reads the
# <meta name="csrf-token"> tag and attaches it as the X-CSRFToken header).
csrf = CSRFProtect()
