# Dealer KYC — Real Verification Engine + Self-Approval

The dealer KYC upload (Aadhaar Front, Aadhaar Back, PAN Card — the same
3 documents already collected in this DMS) now runs through a real
verification pipeline before a file is ever saved, ported from the
`caryanams-kyc-fixed` module. **A document that passes the engine is
auto-approved immediately — the dealer's own verified upload is the
approval. Admin sign-off is no longer required for the KYC to unlock
the dashboard.**

## What changed

- **`utils/kyc_engine/`** (new) — the validation engine: OCR + document
  classification, image-quality checks (blur, glare, exposure,
  screenshot, resolution), per-document field checks (Aadhaar 12-digit
  number + QR + face + UIDAI markers; PAN regex + IT department
  markers), and cross-dealer duplicate detection (SHA256 + perceptual
  hash).
- **`dealer/routes.py`** — `kyc_submit()` now runs every uploaded file
  through the engine:
  - **Pass** → file is saved, `<doc>_status` is set straight to
    `'approved'`, `<doc>_reviewed_by` = `'Auto-KYC Engine'`. Once all 3
    docs pass, the dealer is unblocked immediately — no waiting on an
    admin queue.
  - **Fail** (blurry, wrong doc in the wrong slot, invalid number,
    duplicate, etc.) → `<doc>_status` is set to `'rejected'` with the
    specific reason in `<doc>_reject`, exactly like the existing
    reject-reason UI already displays, and the dealer can immediately
    re-upload.
- **`models.py`** — `DealerKYC` gained extracted-field columns
  (`aadhaar_front_number`, `pan_name`, etc.) and a `cross_validation_notes`
  field; new `KYCDuplicateHash` table backs the duplicate check.
- **`migrate_kyc_verification.py`** (new) — run once after deploying:
  `python migrate_kyc_verification.py`
- **`requirements.txt`** — added `easyocr`, `pytesseract`, `imagehash`,
  `PyMuPDF`.

## Scope

Only the verification logic for the DMS's existing 3 KYC documents was
brought over. The original module's own Flask app / routes / Dealer /
KYCDocument models were **not** copied.

The admin KYC approve/reject screens (`admin/routes.py`,
`templates/admin/kyc_*.html`) were left in place untouched, as a manual
override/audit tool — but they are no longer on the critical path for a
dealer to get unlocked, since a passing upload is approved the moment
it's verified.

Cross-document checks (Aadhaar front/back number match, PAN vs Aadhaar
name similarity, DOB match) are recorded as notes
(`cross_validation_notes`) rather than hard rejections, since OCR
misreads make hard-failing on these too risky for a real KYC flow —
matching the original module's own documented design choice.

## Setup

```bash
pip install -r requirements.txt   # pulls in easyocr (torch-based) + friends
python migrate_kyc_verification.py
```

> `pytesseract` is a fallback OCR engine only used if EasyOCR fails to
> load — if you want it available, also install the `tesseract-ocr`
> binary on the host (`apt install tesseract-ocr`).
