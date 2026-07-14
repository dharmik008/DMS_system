"""
utils/vehicle_price_ai.py
────────────────────────────────────────────────────────────────────────────
Assistive "Estimate Price" feature for the Add/Edit Vehicle form.

Given the vehicle's basic details, its condition-detail fields (already
captured on the form — accident history, loan status, keys available,
etc.), a reference price the dealer supplies (what a similar car new/in
top condition sells for), and the vehicle's own photos, this module
computes a suggested asking price.

IMPORTANT — what this is and isn't:
  This is a transparent, rule-based assistive calculator, not a market-data
  valuation engine. It does NOT look up live market prices; it starts from
  the reference price the dealer enters and adjusts it down (or slightly
  up) using:
    1. Age-based depreciation — the same slab structure Indian motor
       insurers publish for computing a used vehicle's Insured Declared
       Value (IDV), used here only as a well-known, defensible baseline.
    2. Mileage vs. the age-adjusted "expected" odometer reading.
    3. The condition-detail fields already on the form, reusing the
       severity weights from utils/vehicle_issues.detect_vehicle_issues.
    4. An optional visual condition check — the SAME CLIP model already
       loaded for the 7-angle photo verification (utils/vehicle_photo_ai)
       is reused in zero-shot mode to gauge how clean vs. visibly damaged
       (dents/scratches/rust) each submitted photo looks. This is a rough
       visual signal, not a certified damage assessment.

  The final number is always returned as an estimated RANGE with a full
  breakdown, and the caller (route) must present it to the dealer as a
  suggestion to review — never as an authoritative valuation.
"""

from __future__ import annotations

import io
from datetime import datetime

import numpy as np
from PIL import Image

from utils.vehicle_issues import detect_vehicle_issues

# ── Age-based depreciation slabs ────────────────────────────────────────────
# Mirrors the depreciation schedule commonly used in India to compute a used
# vehicle's Insured Declared Value (IDV) for motor insurance — a well
# documented, industry-standard baseline for "how much value a vehicle
# loses simply from age". Beyond 5 years the schedule isn't standardised
# across insurers, so we extend it conservatively at +5%/year, capped.
AGE_DEPRECIATION_SLABS = [
    (0.5, 0.05),   # up to 6 months
    (1.0, 0.15),   # 6 months – 1 year
    (2.0, 0.20),   # 1 – 2 years
    (3.0, 0.30),   # 2 – 3 years
    (4.0, 0.40),   # 3 – 4 years
    (5.0, 0.50),   # 4 – 5 years
]
MAX_AGE_DEPRECIATION = 0.70   # hard cap regardless of age
EXTRA_DEPRECIATION_PER_YEAR_BEYOND_5 = 0.05

EXPECTED_ANNUAL_KM = 12000     # typical average yearly usage in India
MAX_MILEAGE_PENALTY = 0.15     # cap: -15% for very high mileage
MAX_MILEAGE_BONUS = 0.05       # cap: +5% for unusually low mileage

CONDITION_SEVERITY_WEIGHT = {"High": 0.07, "Medium": 0.04, "Low": 0.015}
MAX_CONDITION_DEDUCTION = 0.35

MAX_IMAGE_DEDUCTION = 0.10     # cap: -10% from visual damage signal

MIN_PRICE_FLOOR_FRACTION = 0.10   # never estimate below 10% of reference


def _age_depreciation_fraction(age_years: float) -> float:
    """Returns the fraction of value lost purely to age (0.0–0.70)."""
    if age_years <= 0:
        return 0.0
    for slab_years, slab_pct in AGE_DEPRECIATION_SLABS:
        if age_years <= slab_years:
            return slab_pct
    extra_years = age_years - 5.0
    pct = 0.50 + extra_years * EXTRA_DEPRECIATION_PER_YEAR_BEYOND_5
    return min(pct, MAX_AGE_DEPRECIATION)


def _mileage_adjustment_fraction(age_years: float, mileage_km: float) -> tuple[float, str]:
    """Returns (signed_fraction, note). Positive fraction = deduction,
    negative fraction = small bonus for low usage."""
    age_years = max(age_years, 0.5)  # avoid absurd expectations for brand-new cars
    expected_km = age_years * EXPECTED_ANNUAL_KM
    diff = mileage_km - expected_km

    if diff <= 0:
        # Driven less than expected for its age — small positive signal.
        under_by = abs(diff)
        bonus = min(MAX_MILEAGE_BONUS, (under_by / 10000.0) * 0.015)
        if bonus > 0.005:
            return (-bonus, f"Odometer is {int(under_by):,} km below the typical average for its age — slight premium applied.")
        return (0.0, "Mileage is roughly in line with its age.")
    else:
        penalty = min(MAX_MILEAGE_PENALTY, (diff / 10000.0) * 0.02)
        return (penalty, f"Odometer is {int(diff):,} km above the typical average for its age — value adjusted down.")


def _condition_deduction_fraction(vehicle_data: dict) -> tuple[float, list]:
    """Reuses the existing issue-detection logic so this estimator always
    stays consistent with whatever the Vehicle Condition Details section
    on the form reports elsewhere in the app."""
    result = detect_vehicle_issues(vehicle_data)
    issues = result.get("issues", [])
    total = 0.0
    for issue in issues:
        total += CONDITION_SEVERITY_WEIGHT.get(issue.get("severity"), 0.0)
    return min(total, MAX_CONDITION_DEDUCTION), issues


# ── Visual (CLIP zero-shot) condition scoring ───────────────────────────────
# Reuses the exact CLIP model already loaded for photo-angle verification
# (utils/vehicle_photo_ai) — no second model, no extra download.
_DAMAGE_LABELS = {
    "clean": "a clean, well-maintained car body panel or interior in good condition, no visible damage",
    "damaged": "a car with visible damage such as dents, deep scratches, rust, cracked paint, or a broken part",
}


def _score_image_condition(raw: bytes) -> float | None:
    """Returns a damage probability 0.0 (clean) – 1.0 (visibly damaged),
    or None if CLIP isn't available / the image can't be read."""
    try:
        from utils.vehicle_photo_ai import get_clip, ClipUnavailable
        import torch

        model, processor = get_clip()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        keys = list(_DAMAGE_LABELS.keys())
        texts = [_DAMAGE_LABELS[k] for k in keys]

        inputs = processor(text=texts, images=img, return_tensors="pt", padding=True)
        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)[0]

        scores = {keys[i]: float(probs[i]) for i in range(len(keys))}
        return scores.get("damaged", 0.0)
    except Exception:
        return None


def _image_condition_deduction(image_files: list) -> tuple[float, dict]:
    """image_files: list of Werkzeug FileStorage (already-selected mandatory
    photo slots, if any). Returns (deduction_fraction, meta)."""
    if not image_files:
        return 0.0, {"available": False, "reason": "No photos supplied for visual check.", "photos_scanned": 0}

    scores = []
    for f in image_files:
        try:
            f.stream.seek(0)
            raw = f.read()
            f.stream.seek(0)
        except Exception:
            continue
        if not raw:
            continue
        score = _score_image_condition(raw)
        if score is not None:
            scores.append(score)

    if not scores:
        return 0.0, {"available": False, "reason": "Visual AI condition check is not available right now.", "photos_scanned": 0}

    avg_damage = sum(scores) / len(scores)
    deduction = min(MAX_IMAGE_DEDUCTION, avg_damage * MAX_IMAGE_DEDUCTION * 2)  # scale so ~50%+ avg damage hits the cap
    return deduction, {
        "available": True,
        "photos_scanned": len(scores),
        "avg_damage_score": round(avg_damage, 3),
    }


def estimate_vehicle_price(vehicle_data: dict, image_files: list | None = None) -> dict:
    """
    Parameters
    ----------
    vehicle_data : dict with keys:
        year (int, required), mileage (int, km, required),
        reference_price (float, required) — what a similar car in top
            condition currently sells for; the anchor this estimate
            adjusts down from,
        plus the condition-detail fields already used elsewhere:
        accident_history, loan_status, rc_service_records, major_issues,
        keys_available, body_panel_status.
    image_files : optional list of Werkzeug FileStorage — the mandatory
        photo slots the dealer has already selected on the form.

    Returns
    -------
    dict — always includes "ok". On success also includes
    "estimated_price", "estimated_range", and a full "breakdown" so the
    UI can show *why* the number came out the way it did (never a black
    box), plus a disclaimer string.
    """
    try:
        year = int(vehicle_data.get("year"))
        mileage = float(vehicle_data.get("mileage") or 0)
        reference_price = float(vehicle_data.get("reference_price"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "Year, mileage and reference price are required and must be valid numbers."}

    if reference_price <= 0:
        return {"ok": False, "error": "Reference price must be greater than 0."}

    current_year = datetime.now().year
    age_years = max(0.0, current_year - year)

    age_dep = _age_depreciation_fraction(age_years)
    mileage_adj, mileage_note = _mileage_adjustment_fraction(age_years, mileage)
    condition_dep, issues = _condition_deduction_fraction(vehicle_data)
    image_dep, image_meta = _image_condition_deduction(image_files or [])

    total_deduction = age_dep + mileage_adj + condition_dep + image_dep
    # Floor: never let the combined deductions wipe out more value than
    # leaves the price below MIN_PRICE_FLOOR_FRACTION of the reference.
    total_deduction = min(total_deduction, 1 - MIN_PRICE_FLOOR_FRACTION)

    estimated_price = reference_price * (1 - total_deduction)
    estimated_price = round(estimated_price / 500.0) * 500  # round to nearest ₹500

    low = round((estimated_price * 0.95) / 500.0) * 500
    high = round((estimated_price * 1.05) / 500.0) * 500

    return {
        "ok": True,
        "reference_price": reference_price,
        "estimated_price": estimated_price,
        "estimated_range": {"low": low, "high": high},
        "breakdown": {
            "vehicle_age_years": round(age_years, 1),
            "age_depreciation_pct": round(age_dep * 100, 1),
            "mileage_adjustment_pct": round(mileage_adj * 100, 1),
            "mileage_note": mileage_note,
            "condition_deduction_pct": round(condition_dep * 100, 1),
            "condition_issues": issues,
            "image_deduction_pct": round(image_dep * 100, 1),
            "image_condition": image_meta,
            "total_deduction_pct": round(total_deduction * 100, 1),
        },
        "disclaimer": (
            "This is an automated estimate based on the details and photos you provided — "
            "not a guaranteed valuation. Please cross-check with similar listings in your "
            "market before finalizing the asking price."
        ),
    }
