"""
Caryanams KYC Module — OCR & Document Classification
---------------------------------------------------------
Wraps EasyOCR (primary) with a pytesseract fallback, then classifies
the extracted text into one of:

    aadhaar_front | aadhaar_back | pan | passport | driving_licence
    voter_id | unknown

and pulls out structured fields (Aadhaar number, PAN number, name, DOB)
used later for cross-document validation.
"""

import re
import threading
from difflib import SequenceMatcher

import cv2
import numpy as np

_reader = None  # lazy singleton — EasyOCR model load is expensive
_reader_lock = threading.Lock()

# Longest edge we ever feed to OCR. Aadhaar/PAN text is large and blocky,
# so full 3000px+ phone-camera resolution buys nothing but slowness.
_OCR_MAX_DIM = 1000


def _get_reader():
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:  # re-check inside the lock
                import easyocr
                _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


def preload_reader() -> None:
    """Call once at server startup so the model loads/downloads before the
    first real upload, instead of stalling the user's first request."""
    _get_reader()


def _downscale_for_ocr(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= _OCR_MAX_DIM:
        return image
    scale = _OCR_MAX_DIM / float(longest)
    return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def extract_text(image: np.ndarray) -> str:
    """Runs OCR and returns the full extracted text, upper-cased & joined."""
    small = _downscale_for_ocr(image)

    try:
        reader = _get_reader()
        results = reader.readtext(
            small, detail=0, paragraph=True,
            canvas_size=_OCR_MAX_DIM, mag_ratio=1.0,
        )
        text = "\n".join(results)
        if text.strip():
            return text.upper()
    except Exception:
        pass

    # Fallback to pytesseract if EasyOCR is unavailable or returned nothing
    try:
        import pytesseract
        text = pytesseract.image_to_string(small)
        return text.upper()
    except Exception:
        return ""


# ---------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------
AADHAAR_NUMBER_RE = re.compile(r"\b(\d{4}\s?\d{4}\s?\d{4})\b")
PAN_NUMBER_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
PIN_CODE_RE = re.compile(r"\b(\d{6})\b")
DOB_RE = re.compile(r"\b(\d{2}[/\-]\d{2}[/\-]\d{4})\b")

# Keyword banks used for classification & sub-validation
AADHAAR_FRONT_KEYWORDS = [
    "GOVERNMENT OF INDIA", "UNIQUE IDENTIFICATION AUTHORITY", "UIDAI",
    "DOB", "DATE OF BIRTH", "MALE", "FEMALE", "TRANSGENDER",
]
AADHAAR_BACK_KEYWORDS = [
    "ADDRESS", "UIDAI.GOV.IN", "HELP@UIDAI.GOV.IN", "1947", "VID",
    "UNIQUE IDENTIFICATION AUTHORITY",
]
PAN_KEYWORDS = [
    "INCOME TAX DEPARTMENT", "GOVT. OF INDIA", "GOVERNMENT OF INDIA",
    "PERMANENT ACCOUNT NUMBER", "PAN",
]
PASSPORT_KEYWORDS = ["PASSPORT", "REPUBLIC OF INDIA", "TYPE P", "NATIONALITY"]
DL_KEYWORDS = ["DRIVING LICENCE", "DRIVING LICENSE", "TRANSPORT DEPARTMENT", "DL NO"]
VOTER_KEYWORDS = ["ELECTION COMMISSION", "ELECTOR", "EPIC", "VOTER"]


def _score(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def classify_document(text: str) -> str:
    """
    Returns one of: aadhaar_front, aadhaar_back, pan, passport,
    driving_licence, voter_id, unknown
    """
    has_aadhaar_number = bool(AADHAAR_NUMBER_RE.search(text))
    has_pan_number = bool(PAN_NUMBER_RE.search(text))

    scores = {
        "pan": _score(text, PAN_KEYWORDS) + (3 if has_pan_number else 0),
        "aadhaar_front": _score(text, AADHAAR_FRONT_KEYWORDS) + (2 if has_aadhaar_number else 0),
        "aadhaar_back": _score(text, AADHAAR_BACK_KEYWORDS) + (1 if has_aadhaar_number else 0),
        "passport": _score(text, PASSPORT_KEYWORDS),
        "driving_licence": _score(text, DL_KEYWORDS),
        "voter_id": _score(text, VOTER_KEYWORDS),
    }

    # PAN number pattern is highly distinctive — trust it over keyword noise
    if has_pan_number and scores["pan"] >= scores["aadhaar_front"]:
        return "pan"

    # Aadhaar back typically has address + helpline but far fewer face/DOB cues
    if scores["aadhaar_back"] > 0 and scores["aadhaar_back"] >= scores["aadhaar_front"] and \
            "ADDRESS" in text:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return "aadhaar_back" if best in ("aadhaar_back", "aadhaar_front") else best
        return "unknown"

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "unknown"
    return best


def extract_aadhaar_number(text: str) -> str | None:
    match = AADHAAR_NUMBER_RE.search(text)
    if not match:
        return None
    return match.group(1).replace(" ", "")


def extract_pan_number(text: str) -> str | None:
    match = PAN_NUMBER_RE.search(text)
    return match.group(1) if match else None


def extract_dob(text: str) -> str | None:
    match = DOB_RE.search(text)
    return match.group(1) if match else None


def extract_name(text: str, doc_type: str) -> str | None:
    """
    Heuristic name extraction: PAN cards list the name on the line right
    after 'INCOME TAX DEPARTMENT' / 'GOVT OF INDIA' header lines; Aadhaar
    lists it near the DOB line. This is intentionally conservative —
    it returns the best-guess candidate line, which downstream code
    treats as advisory (used for similarity scoring, not hard rejection
    on its own).
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    skip_tokens = (
        "GOVERNMENT", "INCOME TAX", "PERMANENT ACCOUNT", "UNIQUE IDENTIFICATION",
        "DOB", "DATE OF BIRTH", "MALE", "FEMALE", "ADDRESS", "PAN", "UIDAI",
        "GOVT", "INDIA", "SIGNATURE", "FATHER",
    )

    candidates = [
        ln for ln in lines
        if 2 <= len(ln.split()) <= 4
        and ln.replace(" ", "").isalpha()
        and not any(tok in ln for tok in skip_tokens)
    ]
    return candidates[0].title() if candidates else None


def name_similarity(name_a: str, name_b: str) -> float:
    """Returns a 0-1 similarity ratio between two names (order-insensitive)."""
    if not name_a or not name_b:
        return 0.0
    norm_a = " ".join(sorted(name_a.upper().split()))
    norm_b = " ".join(sorted(name_b.upper().split()))
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def has_qr_code(image: np.ndarray) -> bool:
    """Detects an Aadhaar QR code using OpenCV's built-in QR detector."""
    import cv2
    detector = cv2.QRCodeDetector()
    data, points, _ = detector.detectAndDecode(image)
    return bool(points is not None and len(points) > 0)
