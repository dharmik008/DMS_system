"""
utils/kyc_engine/file_utils.py — Caryanams DMS
--------------------------------------------------
Magic-byte content sniffing (so a renamed .exe wearing a ".jpg"
extension is rejected) and bytes -> OpenCV BGR ndarray decoding,
including first-page rasterisation for PDF uploads.
"""

import io

import cv2
import numpy as np
from PIL import Image


def sniff_real_extension(file_bytes: bytes) -> str | None:
    """Validates actual file content via magic bytes, not just the extension
    the client claims — a core anti-spoofing measure."""
    if file_bytes[:3] == b"\xff\xd8\xff":
        return "jpg"
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "webp"
    if file_bytes[:5] == b"%PDF-":
        return "pdf"
    return None


def load_as_cv_image(file_bytes: bytes, real_ext: str) -> np.ndarray:
    """Decodes the uploaded bytes (rasterising the first page if PDF) into
    an OpenCV BGR ndarray for downstream analysis."""
    if real_ext == "pdf":
        import fitz  # PyMuPDF — imported lazily, only needed for PDF uploads
        pdf = fitz.open(stream=file_bytes, filetype="pdf")
        page = pdf.load_page(0)
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    else:
        pil_img = Image.open(io.BytesIO(file_bytes)).convert("RGB")

    arr = np.array(pil_img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def pil_image_for_hash(file_bytes: bytes, cv_image: np.ndarray, real_ext: str) -> Image.Image:
    """Returns a PIL image suitable for perceptual hashing."""
    if real_ext == "pdf":
        return Image.fromarray(cv2.cvtColor(cv_image, cv2.COLOR_BGR2RGB))
    return Image.open(io.BytesIO(file_bytes)).convert("RGB")
