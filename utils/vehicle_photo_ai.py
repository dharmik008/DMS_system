"""
utils/vehicle_photo_ai.py
────────────────────────────────────────────────────────────────────────────
Strict per-angle vehicle photo verification, ported from the standalone
"car-upload-single" tool so the same guarantee applies inside the DMS
"Add Vehicle" / "Edit Vehicle" flow: each of the 7 mandatory slots must
actually contain a photo of that angle (front/rear/side/interior/engine/
boot), not just "any image file".

Uses CLIP (openai/clip-vit-base-patch32) — free, runs locally, no API key.
First call downloads ~600MB of model weights (needs internet once), every
call after that is fully offline.

HONEST LIMITATION (same as the standalone tool): Left and Right side
profiles are visually mirror images of each other — no free/local model
can reliably tell them apart. Both slots are checked against a single
"side profile" category, so a genuine side-view photo will pass either
slot, but a front/rear/interior/engine/boot photo will still be correctly
rejected from either side slot.
"""

import io
import threading

import numpy as np
from PIL import Image, ImageOps

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

# DMS slot name -> CLIP category key
SLOT_TO_LABEL = {
    "front":      "front",
    "rear":       "rear",
    "left_side":  "side",
    "right_side": "side",
    "interior":   "interior",
    "engine":     "engine",
    "boot":       "boot",
}

SLOT_DISPLAY = {
    "front": "Front View", "rear": "Rear / Tail View",
    "left_side": "Left Side View", "right_side": "Right Side View",
    "interior": "Interior", "engine": "Engine Bay", "boot": "Boot / Trunk",
}

CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"
MIN_CONFIDENCE = 0.35  # 7 candidate labels, random baseline ~14%

CLIP_LABELS = {
    "front":    "a front view photo of a car, showing the headlights, grille and front bumper facing the camera",
    "rear":     "a rear view photo of a car, showing the tail lights, boot lid and rear bumper facing the camera",
    "side":     "a side profile photo of a car, showing the doors and both wheels in a straight line from the side",
    "interior": "a photo of the interior cabin of a car, showing the dashboard, seats and steering wheel from inside",
    "engine":   "a photo of an open car engine bay with the bonnet lifted, showing the engine",
    "boot":     "a photo of an open car boot or trunk storage compartment",
    "irrelevant": "a photo unrelated to a car, such as a person, a document, a screenshot, or an empty background",
}
LABEL_DISPLAY = {
    "front": "a front view", "rear": "a rear/back view", "side": "a side profile",
    "interior": "the interior", "engine": "the engine bay", "boot": "an open boot",
    "irrelevant": "something not related to the car",
}

LIMITS = {
    "min_width": 480,
    "min_height": 360,
    "min_file_size": 15 * 1024,
    "max_file_size": 12 * 1024 * 1024,
    "min_sharpness": 12.0,
    "min_color_std": 5.0,
}

_clip_lock = threading.Lock()
_clip_model = None
_clip_processor = None


class ClipUnavailable(RuntimeError):
    """Raised when CLIP/torch cannot be loaded (broken env, missing weights, etc.)."""


def get_clip():
    """Lazy-load CLIP once; first call downloads weights, every call after
    that is fully offline. Safe to call from a background warm-up thread.

    Raises ClipUnavailable (instead of leaking a raw ImportError/TypeError)
    if torch/transformers are not usable, so callers get one clear error
    instead of a stack trace from deep inside transformers.
    """
    global _clip_model, _clip_processor
    with _clip_lock:
        if _clip_model is None:
            try:
                import torch  # noqa: F401  (import check only)
                from transformers import CLIPModel, CLIPProcessor
            except Exception as exc:
                raise ClipUnavailable(
                    f"torch/transformers import failed ({exc.__class__.__name__}: {exc}). "
                    "Vehicle-photo AI verification is disabled until the Python env is fixed."
                ) from exc

            print("Loading CLIP model for vehicle-photo verification (first time downloads ~600MB)...")
            try:
                _clip_processor = CLIPProcessor.from_pretrained(CLIP_MODEL_NAME)
                _clip_model = CLIPModel.from_pretrained(CLIP_MODEL_NAME)
                _clip_model.eval()
            except Exception as exc:
                _clip_processor = None
                _clip_model = None
                raise ClipUnavailable(
                    f"CLIP weights could not be loaded ({exc.__class__.__name__}: {exc})."
                ) from exc
            print("Vehicle-photo CLIP model ready.")
    return _clip_model, _clip_processor


def _laplacian_variance(gray: np.ndarray) -> float:
    lap = np.zeros((gray.shape[0] - 2, gray.shape[1] - 2), dtype=np.float32)
    lap += -4 * gray[1:-1, 1:-1]
    lap += gray[0:-2, 1:-1]
    lap += gray[2:, 1:-1]
    lap += gray[1:-1, 0:-2]
    lap += gray[1:-1, 2:]
    return float(np.std(lap))


def validate_image_bytes(raw: bytes) -> dict:
    """Format / size / blur / blank checks. Returns {ok, raw, format} or {ok:False, error}."""
    if len(raw) < LIMITS["min_file_size"]:
        return {"ok": False, "error": "File bahut chhoti / corrupt hai."}
    if len(raw) > LIMITS["max_file_size"]:
        return {"ok": False, "error": "File size 12MB se zyada hai."}

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))  # reopen after verify()
        img_format = img.format
        img = ImageOps.exif_transpose(img)
    except Exception:
        return {"ok": False, "error": "Yeh valid image file nahi hai."}

    if img_format not in ALLOWED_FORMATS:
        return {"ok": False, "error": f"Format {img_format} allowed nahi hai (jpg/png/webp use karein)."}

    w, h = img.size
    if w < LIMITS["min_width"] or h < LIMITS["min_height"]:
        return {"ok": False, "error": f"Resolution kam hai ({w}x{h})."}

    small = img.convert("L").resize((320, max(1, int(320 * h / w))))
    gray = np.asarray(small, dtype=np.float32)

    if _laplacian_variance(gray) < LIMITS["min_sharpness"]:
        return {"ok": False, "error": "Image blurry lag rahi hai."}
    if float(np.std(gray)) < LIMITS["min_color_std"]:
        return {"ok": False, "error": "Image blank / ek-rang jaisi lag rahi hai."}

    return {"ok": True, "raw": raw, "format": img_format}


def classify_vehicle_photo(raw: bytes, slot: str) -> dict:
    """CLIP zero-shot check: does this photo actually match the requested
    slot's angle? Fail-CLOSED on any error — never silently passes."""
    target_label = SLOT_TO_LABEL.get(slot)
    slot_display = SLOT_DISPLAY.get(slot, slot)
    if target_label is None:
        return {"ok": False, "error": f"Unknown photo slot: {slot}"}

    try:
        import torch
        model, processor = get_clip()  # raises ClipUnavailable if env is broken

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        label_keys = list(CLIP_LABELS.keys())
        texts = [CLIP_LABELS[k] for k in label_keys]

        inputs = processor(text=texts, images=img, return_tensors="pt", padding=True)
        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)[0]

        scores = {label_keys[i]: float(probs[i]) for i in range(len(label_keys))}
        best_label = max(scores, key=scores.get)
        best_score = scores[best_label]

        if best_label != target_label:
            return {
                "ok": False,
                "error": f"❌ {slot_display} ke liye galat photo. Yeh {LABEL_DISPLAY[best_label]} jaisa lag raha hai.",
            }
        if best_score < MIN_CONFIDENCE:
            return {
                "ok": False,
                "error": f"❌ {slot_display} clearly dikhai nahi de raha. Saaf, sahi angle se photo lo.",
            }
        return {"ok": True}

    except ClipUnavailable as exc:
        print(f"[vehicle_photo_ai] CLIP unavailable: {exc}")
        return {
            "ok": False,
            "error": "Photo AI verification abhi available nahi hai (server config issue). Admin ko batayein.",
        }
    except Exception as exc:
        return {"ok": False, "error": f"Photo verify nahi ho payi, dobara try karein. ({exc.__class__.__name__})"}


def verify_vehicle_photo(file_storage, slot: str) -> dict:
    """Convenience wrapper: reads a Werkzeug FileStorage, runs validate +
    classify, and returns {ok, raw, format} or {ok: False, error}."""
    raw = file_storage.read()
    result = validate_image_bytes(raw)
    if not result["ok"]:
        return result
    verdict = classify_vehicle_photo(result["raw"], slot)
    if not verdict["ok"]:
        return verdict
    return result


def _warm_up():
    try:
        get_clip()
    except ClipUnavailable as exc:
        print(f"[vehicle_photo_ai] Skipping warm-up, CLIP unavailable: {exc}")


def warm_up_in_background():
    """Call once at app startup so the first real upload isn't the one
    that pays the ~600MB download / model-load cost."""
    threading.Thread(target=_warm_up, daemon=True).start()
