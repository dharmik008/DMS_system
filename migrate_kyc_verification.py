"""
Migration: Add auto-verification columns to dealer_kyc + create
kyc_duplicate_hash table, for the real OCR/image-quality KYC engine
(utils/kyc_engine).

Run ONCE with: python migrate_kyc_verification.py
Safe to re-run — skips columns/tables that already exist.
"""
import sqlite3
import os

DB_PATH = os.environ.get('DATABASE_URL', 'caryanams.db')
if DB_PATH.startswith('sqlite:///'):
    DB_PATH = DB_PATH[len('sqlite:///'):]

NEW_COLUMNS = [
    ('aadhaar_front_number', 'VARCHAR(20)'),
    ('aadhaar_back_number',  'VARCHAR(20)'),
    ('pan_number',           'VARCHAR(20)'),
    ('aadhaar_front_name',   'VARCHAR(150)'),
    ('aadhaar_back_name',    'VARCHAR(150)'),
    ('pan_name',             'VARCHAR(150)'),
    ('aadhaar_front_dob',    'VARCHAR(20)'),
    ('pan_dob',              'VARCHAR(20)'),
    ('cross_validation_notes', 'TEXT'),
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kyc_duplicate_hash (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doc_type     VARCHAR(20) NOT NULL,
    sha256_hash  VARCHAR(64) NOT NULL,
    phash        VARCHAR(64) NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dealer_id, doc_type)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_kyc_duplicate_hash_sha256 ON kyc_duplicate_hash(sha256_hash);
"""


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"[WARN] Database not found at: {DB_PATH}")
        print("       Run this migration after the app has initialised the DB at least once.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(dealer_kyc)")
    existing = {row[1] for row in cur.fetchall()}

    for col_name, col_def in NEW_COLUMNS:
        if col_name not in existing:
            cur.execute(f"ALTER TABLE dealer_kyc ADD COLUMN {col_name} {col_def}")
            print(f"  \u2705 Added column: dealer_kyc.{col_name}")
        else:
            print(f"  \u23ed  Column already exists: dealer_kyc.{col_name}")

    cur.execute(CREATE_TABLE_SQL)
    cur.execute(CREATE_INDEX_SQL)
    print("  \u2705 Ensured table: kyc_duplicate_hash")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == '__main__':
    migrate()
