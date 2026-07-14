"""
utils/kyc_engine — Caryanams DMS
---------------------------------
Real dealer-KYC document verification pipeline, ported from the
standalone `caryanams-kyc` module.

Scope: this engine validates exactly the 3 documents the DMS already
collects from a dealer — Aadhaar Front, Aadhaar Back and PAN Card.
Nothing else from the original module (its own Flask app, its own
Dealer/KYCDocument models, its own upload routes) was brought over;
only the pure validation logic was, and it now runs inside the DMS's
existing `dealer.kyc_submit` route against the DMS's own `DealerKYC`
model.

Modules:
    ocr.py             — EasyOCR/pytesseract text extraction + classification
    blur_detector.py   — Laplacian-variance blur + rotation/skew checks
    crop_detector.py   — black border / crop / corner / perspective checks
    face_detector.py   — Haar-cascade face detection (Aadhaar Front)
    glare_detector.py  — glare / exposure / resolution / screenshot checks
    validator.py        — orchestrates all of the above into one
                           validate_document() call + cross-document checks
    duplicate.py        — SHA256 + perceptual-hash duplicate detection,
                           backed by the DMS's KYCDuplicateHash table
    file_utils.py        — magic-byte sniffing + bytes -> OpenCV image decode
"""
