"""
migrate_vehicle_approval_status.py
────────────────────────────────────────────────────────────────────────────
One-time fix-up for existing databases created before the approval_status
case-mismatch bug was fixed (model default was 'Pending' with a capital P,
while every filter elsewhere in the app checks lowercase 'pending' /
'approved' / 'rejected'). Vehicles inserted under the old default were
invisible to the admin's Pending queue — this script normalizes them.

Safe to run multiple times; only touches rows that don't already match the
expected lowercase values. Works on both SQLite and PostgreSQL.

Usage:
    python migrate_vehicle_approval_status.py
"""

from app import create_app
from extensions import db
from sqlalchemy import text


def run():
    app = create_app()
    with app.app_context():
        # Map any case variant we might encounter to the canonical lowercase
        # value used everywhere in the codebase.
        fixes = [
            ("Pending",  "pending"),
            ("PENDING",  "pending"),
            ("Approved", "approved"),
            ("APPROVED", "approved"),
            ("Rejected", "rejected"),
            ("REJECTED", "rejected"),
        ]
        total_fixed = 0
        for wrong, right in fixes:
            result = db.session.execute(
                text("UPDATE vehicles SET approval_status = :right WHERE approval_status = :wrong"),
                {"right": right, "wrong": wrong},
            )
            if result.rowcount:
                print(f"Fixed {result.rowcount} vehicle(s): '{wrong}' → '{right}'")
                total_fixed += result.rowcount
        db.session.commit()

        if total_fixed == 0:
            print("No mismatched approval_status values found — nothing to fix.")
        else:
            print(f"\nDone. {total_fixed} vehicle row(s) normalized.")
            print("These vehicles now correctly appear in the admin Pending queue for review.")


if __name__ == "__main__":
    run()
