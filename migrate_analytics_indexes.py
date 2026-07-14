"""
migrate_analytics_indexes.py
════════════════════════════
One-time migration to add indexes backing the new unique-visitor /
unique-actor aggregation on the Visitor Logs and Activity Logs admin pages.

Run ONCE after deploying the analytics-fix update:
    python migrate_analytics_indexes.py

What it does
────────────
Adds indexes on the columns COUNT(DISTINCT ...) now groups by:
  visitor_logs : ip_address, device_type, session_id, user_id
  admin_logs   : ip_address, user_role, module, user_id

Safe to re-run — every statement is wrapped so an "already exists" error
is skipped rather than raised. No rows, tables, or existing columns are
touched or removed.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

INDEXES = [
    ('ix_visitor_logs_ip_address',  'visitor_logs', 'ip_address'),
    ('ix_visitor_logs_device_type', 'visitor_logs', 'device_type'),
    ('ix_visitor_logs_session_id',  'visitor_logs', 'session_id'),
    ('ix_visitor_logs_user_id',     'visitor_logs', 'user_id'),
    ('ix_admin_logs_ip_address',    'admin_logs',   'ip_address'),
    ('ix_admin_logs_user_role',     'admin_logs',   'user_role'),
    ('ix_admin_logs_module',        'admin_logs',   'module'),
    ('ix_admin_logs_user_id',       'admin_logs',   'user_id'),
]

with app.app_context():
    dialect = db.engine.dialect.name  # 'sqlite' or 'postgresql'
    with db.engine.connect() as conn:
        for index_name, table, column in INDEXES:
            try:
                if dialect == 'postgresql':
                    conn.execute(text(
                        f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})'
                    ))
                else:
                    # SQLite also supports IF NOT EXISTS on CREATE INDEX
                    conn.execute(text(
                        f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})'
                    ))
                conn.commit()
                print(f'  ✓ {index_name} ensured on {table}.{column}')
            except Exception as e:
                print(f'  · {index_name} skipped: {e}')

    print('\nMigration complete.')
