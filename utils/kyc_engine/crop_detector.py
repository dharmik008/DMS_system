"""
Caryanams KYC Module — Crop / Border / Corner Detection
-----------------------------------------------------------
Heuristic checks (no ML model required) for:
  - half-cropped documents (content cut off at an edge)
  - missing corners
  - black borders (common in bad scans/photocopies)
  - basic perspective distortion (document not roughly rectangular)
"""

import cv2
import numpy as np


def _largest_contour(image: np.ndarray):
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def has_black_border(image: np.ndarray, ratio_threshold: float = 0.35) -> bool:
    """Checks a thin strip along each edge for near-black pixels (photocopy/scan artifact)."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    strip = max(4, int(min(h, w) * 0.02))

    edges_pixels = np.concatenate([
        gray[:strip, :].flatten(),
        gray[-strip:, :].flatten(),
        gray[:, :strip].flatten(),
        gray[:, -strip:].flatten(),
    ])
    dark_ratio = float(np.mean(edges_pixels < 40))
    return dark_ratio > ratio_threshold


def is_cropped_or_corner_missing(image: np.ndarray, coverage_threshold: float = 0.55) -> bool:
    """
    If the detected document contour covers far less area than the full
    frame, or touches the image boundary on 2+ sides unevenly, treat it
    as cropped / a missing corner.
    """
    h, w = image.shape[:2]
    contour = _largest_contour(image)
    if contour is None:
        return True  # no discernible document boundary at all

    area_ratio = cv2.contourArea(contour) / float(h * w)
    x, y, cw, ch = cv2.boundingRect(contour)

    touches_edges = sum([
        x <= 2,
        y <= 2,
        (x + cw) >= w - 2,
        (y + ch) >= h - 2,
    ])

    # A full, well-framed document usually covers most of the frame and
    # touches at most one edge tightly. Cropped documents touch 2+ edges
    # while covering a smaller-than-expected fraction of the frame.
    return area_ratio < coverage_threshold and touches_edges >= 2


def has_perspective_distortion(image: np.ndarray, angle_tolerance: float = 12.0) -> bool:
    """Approximates the document boundary as a quadrilateral and checks
    whether its corner angles deviate significantly from 90 degrees."""
    contour = _largest_contour(image)
    if contour is None:
        return False

    peri = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
    if len(approx) != 4:
        return False  # can't reliably assess; don't false-positive

    pts = approx.reshape(4, 2).astype(np.float32)

    def angle(p0, p1, p2):
        v1 = p0 - p1
        v2 = p2 - p1
        cos_a = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        return np.degrees(np.arccos(np.clip(cos_a, -1, 1)))

    angles = [
        angle(pts[i - 1], pts[i], pts[(i + 1) % 4]) for i in range(4)
    ]
    deviation = max(abs(a - 90) for a in angles)
    return deviation > angle_tolerance
