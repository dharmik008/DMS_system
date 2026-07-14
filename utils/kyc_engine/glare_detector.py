"""
Caryanams KYC Module — Glare / Exposure / Screenshot Detection
--------------------------------------------------------------
"""

import cv2
import numpy as np


def compute_brightness(image: np.ndarray) -> float:
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray))


def has_glare(image: np.ndarray, bright_ratio_threshold: float = 0.06) -> tuple[bool, float]:
    """
    Glare shows up as tight clusters of near-saturated (>=250) pixels,
    distinct from generally bright but evenly-lit photos.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bright_pixels = np.sum(gray >= 250)
    ratio = float(bright_pixels) / gray.size
    return ratio > bright_ratio_threshold, ratio


def is_too_dark(image: np.ndarray, threshold: float = 60) -> bool:
    return compute_brightness(image) < threshold


def is_overexposed(image: np.ndarray, threshold: float = 225) -> bool:
    return compute_brightness(image) > threshold


def is_low_resolution(image: np.ndarray, min_w: int = 1000, min_h: int = 700) -> bool:
    h, w = image.shape[:2]
    return w < min_w or h < min_h


def looks_like_screenshot(image: np.ndarray, filename: str = "") -> bool:
    """
    Heuristic: screenshots are typically perfectly rectangular with hard
    flat-color UI edges (status bar / browser chrome) and very low edge
    noise near the borders compared to real photographed documents.
    Also flags common screenshot aspect ratios + filename patterns.
    """
    lower_name = filename.lower()
    if any(tag in lower_name for tag in ("screenshot", "screen shot", "snip")):
        return True

    h, w = image.shape[:2]
    aspect = w / float(h)
    common_screen_aspects = [16 / 9, 9 / 16, 19.5 / 9, 4 / 3, 3 / 4, 18 / 9]
    aspect_match = any(abs(aspect - a) < 0.03 for a in common_screen_aspects)

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge_strip = gray[:8, :]
    flatness = float(np.std(edge_strip))  # very low std = flat UI bar, not a physical scan
    return aspect_match and flatness < 6.0
