"""
utils/kyc_engine/duplicate.py — Caryanams DMS
-------------------------------------------------
Two layers of duplicate detection, checked across ALL dealers (not just
the current one), backed by the DMS's `KYCDuplicateHash` table:

1. SHA256 — catches byte-identical re-uploads of the exact same file.
2. Perceptual hash (pHash) — catches re-uploads that were re-saved,
   re-compressed, resized, or lightly edited but are visually the same
   document (e.g. a screenshot of the same Aadhaar re-exported as PNG).
"""

import hashlib

import imagehash
from PIL import Image

PHASH_HAMMING_DISTANCE_THRESHOLD = 6  # <= this distance => considered a duplicate


def compute_sha256(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def compute_phash(pil_image: Image.Image) -> str:
    return str(imagehash.phash(pil_image))


def find_duplicate(sha256_hash: str, phash: str):
    """
    Returns the matching KYCDuplicateHash row if this document (or a
    visually-identical version of it) has already been uploaded by
    ANY dealer, else None.
    """
    from models import KYCDuplicateHash

    exact = KYCDuplicateHash.query.filter_by(sha256_hash=sha256_hash).first()
    if exact:
        return exact

    candidate_hash = imagehash.hex_to_hash(phash)
    for row in KYCDuplicateHash.query.all():
        try:
            existing_hash = imagehash.hex_to_hash(row.phash)
        except ValueError:
            continue
        if candidate_hash - existing_hash <= PHASH_HAMMING_DISTANCE_THRESHOLD:
            return row
    return None


def register_hash(sha256_hash: str, phash: str, dealer_id: int, doc_type: str, db_session):
    """Upserts the (dealer_id, doc_type) hash row — a re-upload/replace of
    the same slot overwrites its own previous hash rather than leaving a
    stale duplicate entry behind."""
    from models import KYCDuplicateHash

    entry = KYCDuplicateHash.query.filter_by(dealer_id=dealer_id, doc_type=doc_type).first()
    if entry:
        entry.sha256_hash = sha256_hash
        entry.phash = phash
    else:
        entry = KYCDuplicateHash(
            sha256_hash=sha256_hash, phash=phash, dealer_id=dealer_id, doc_type=doc_type
        )
        db_session.add(entry)
    db_session.commit()
    return entry


def remove_hash_for_document(dealer_id: int, doc_type: str, db_session):
    """Called when a dealer's document for this slot fails validation on
    replace, so stale hashes don't linger. (Successful replace re-uses
    register_hash's upsert instead.)"""
    from models import KYCDuplicateHash

    KYCDuplicateHash.query.filter_by(dealer_id=dealer_id, doc_type=doc_type).delete()
    db_session.commit()
