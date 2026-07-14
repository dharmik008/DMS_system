"""
utils/kyc_engine/validator.py — Caryanams DMS
------------------------------------------------
Master validation pipeline for a single uploaded KYC document.
Runs: image-quality checks -> OCR/classification -> document-specific
field validation. Ported from the standalone caryanams-kyc module's
kyc_validator.py, with `cross_validate` reworked to operate on the
DMS's single-row `DealerKYC` record instead of a separate
`KYCDocument` table.
"""

from dataclasses import dataclass, field

import numpy as np

from . import ocr
from .blur_detector import is_blurry, is_rotated  # noqa: F401  (kept for future re-enable)
from .crop_detector import has_black_border, is_cropped_or_corner_missing, has_perspective_distortion  # noqa: F401
from .face_detector import has_face
from .glare_detector import (
    has_glare, is_too_dark, is_overexposed, is_low_resolution, looks_like_screenshot,  # noqa: F401
)


@dataclass
class ValidationResult:
    ok: bool
    error_code: str | None = None
    error_message: str | None = None
    detected_doc_type: str = "unknown"
    extracted_number: str | None = None
    extracted_name: str | None = None
    extracted_dob: str | None = None
    warnings: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)


# Human-facing messages — kept centralised so wording stays consistent
# across the route layer and the frontend.
MESSAGES = {
    "wrong_slot_aadhaar_front": "Only Aadhaar Front is allowed in this field.",
    "wrong_slot_aadhaar_back": "Only Aadhaar Back is allowed in this field.",
    "wrong_slot_pan": "Only PAN Card is allowed in this field.",
    "pan_in_aadhaar": "PAN uploaded in Aadhaar section.",
    "aadhaar_in_pan": "Aadhaar uploaded in PAN section.",
    "blurry": "Document is blurry.",
    "cropped": "Document is cropped.",
    "edited": "Document appears edited or tampered.",
    "no_face": "Face not detected.",
    "no_qr": "QR Code missing.",
    "no_logo": "Government logo missing.",
    "invalid_aadhaar": "Invalid Aadhaar Number.",
    "invalid_pan": "Invalid PAN Number.",
    "duplicate": "This document has already been uploaded (duplicate detected).",
    "low_res": "Low Resolution. Please upload an image at least 1000x700.",
    "glare": "Glare detected.",
    "dark": "Dark image detected.",
    "overexposed": "Image is overexposed. Please retake in even lighting.",
    "screenshot": "Screenshot detected. Please upload the original photo/scan.",
    "unrecognized": "Document not recognized.",
    "rotated": "Document appears rotated. Please upload a straight, upright photo.",
    "perspective": "Document has perspective distortion. Please capture it flat and straight-on.",
    "no_address": "Address not found on Aadhaar Back.",
    "no_helpline": "UIDAI helpline / website not found on Aadhaar Back.",
}

PAN_REGEX_NOTE = r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$"

# DMS doc-key ('pan_card') -> validator doc-type ('pan')
DOC_TYPE_MAP = {
    "aadhaar_front": "aadhaar_front",
    "aadhaar_back": "aadhaar_back",
    "pan_card": "pan",
}


def _fail(code: str) -> ValidationResult:
    return ValidationResult(ok=False, error_code=code, error_message=MESSAGES[code])


def run_image_quality_checks(image: np.ndarray, cfg) -> "ValidationResult | None":
    """Returns a ValidationResult (failure) if any generic image-quality
    check fails, or None if the image passes all of them."""

    if looks_like_screenshot(image):
        return _fail("screenshot")

    glare, _ratio = has_glare(image, cfg["KYC_GLARE_BRIGHT_PIXEL_RATIO"])
    if glare:
        return _fail("glare")

    if is_too_dark(image, cfg["KYC_DARK_MEAN_BRIGHTNESS"]):
        return _fail("dark")

    if is_overexposed(image, cfg["KYC_OVEREXPOSED_MEAN_BRIGHTNESS"]):
        return _fail("overexposed")

    return None


def _wrong_slot_message(expected: str, detected: str) -> str:
    if expected in ("aadhaar_front", "aadhaar_back") and detected == "pan":
        return MESSAGES["pan_in_aadhaar"]
    if expected == "pan" and detected in ("aadhaar_front", "aadhaar_back"):
        return MESSAGES["aadhaar_in_pan"]
    return MESSAGES[f"wrong_slot_{expected}"]


def validate_document(image: np.ndarray, expected_doc_type: str, cfg) -> ValidationResult:
    """
    expected_doc_type: 'aadhaar_front' | 'aadhaar_back' | 'pan'
    Runs the full pipeline and returns a ValidationResult.
    """

    # 1) Generic image quality gate — no point OCR-ing a blurry photo.
    quality_failure = run_image_quality_checks(image, cfg)
    if quality_failure:
        return quality_failure

    # 2) OCR + classification
    text = ocr.extract_text(image)
    detected_type = ocr.classify_document(text)

    if detected_type == "unknown":
        return _fail("unrecognized")

    if detected_type != expected_doc_type:
        return ValidationResult(
            ok=False,
            error_code="wrong_document_type",
            error_message=_wrong_slot_message(expected_doc_type, detected_type),
            detected_doc_type=detected_type,
        )

    aadhaar_no = ocr.extract_aadhaar_number(text)
    pan_no = ocr.extract_pan_number(text)
    dob = ocr.extract_dob(text)
    name = ocr.extract_name(text, expected_doc_type)

    # 3) Document-specific field validation
    if expected_doc_type == "aadhaar_front":
        if not aadhaar_no or len(aadhaar_no) != 12:
            return _fail("invalid_aadhaar")
        if not has_face(image):
            return _fail("no_face")
        if not ocr.has_qr_code(image):
            return _fail("no_qr")
        if "UNIQUE IDENTIFICATION AUTHORITY" not in text and "UIDAI" not in text \
                and "GOVERNMENT OF INDIA" not in text:
            return _fail("no_logo")

    elif expected_doc_type == "aadhaar_back":
        if not aadhaar_no or len(aadhaar_no) != 12:
            return _fail("invalid_aadhaar")
        if "ADDRESS" not in text:
            return _fail("no_address")
        if not any(tok in text for tok in ("UIDAI.GOV.IN", "1947", "VID")):
            return _fail("no_helpline")

    elif expected_doc_type == "pan":
        import re
        if not pan_no or not re.match(PAN_REGEX_NOTE, pan_no):
            return _fail("invalid_pan")
        if not any(tok in text for tok in ("INCOME TAX DEPARTMENT", "PERMANENT ACCOUNT NUMBER")):
            return _fail("no_logo")

    return ValidationResult(
        ok=True,
        detected_doc_type=detected_type,
        extracted_number=aadhaar_no if expected_doc_type != "pan" else pan_no,
        extracted_name=name,
        extracted_dob=dob,
    )


def cross_validate_kyc(kyc, cfg) -> list:
    """
    Cross-document checks against a DMS `DealerKYC` row, run once all 3
    slots are filled:
      - Aadhaar Front number == Aadhaar Back number
      - Name on PAN vs name on Aadhaar Front (>=90% similarity by default)
      - DOB match between Aadhaar Front and PAN (if PAN carries DOB —
        many PAN cards don't)

    These are treated as WARNINGS surfaced to the human admin reviewer,
    not hard rejections — OCR misreads make hard-failing on these too
    risky for a real KYC flow.

    Returns a list of human-readable problem strings (empty = all good).
    """
    problems = []

    front_no = getattr(kyc, "aadhaar_front_number", None)
    back_no = getattr(kyc, "aadhaar_back_number", None)
    if front_no and back_no and front_no != back_no:
        problems.append("Aadhaar Front number does not match Aadhaar Back number.")

    front_name = getattr(kyc, "aadhaar_front_name", None)
    pan_name = getattr(kyc, "pan_name", None)
    if front_name and pan_name:
        similarity = ocr.name_similarity(front_name, pan_name)
        threshold = cfg.get("KYC_NAME_SIMILARITY_THRESHOLD", 0.90)
        if similarity < threshold:
            problems.append(
                f"Name on PAN does not sufficiently match name on Aadhaar "
                f"({similarity * 100:.0f}% similarity, {threshold * 100:.0f}% required)."
            )

    front_dob = getattr(kyc, "aadhaar_front_dob", None)
    pan_dob = getattr(kyc, "pan_dob", None)
    if front_dob and pan_dob and front_dob != pan_dob:
        problems.append("Date of birth on PAN does not match Aadhaar.")

    return problems
