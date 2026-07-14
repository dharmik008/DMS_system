"""
Caryanams KYC Module — Blur Detection
----------------------------------------
Uses the variance of the Laplacian operator as a sharpness metric.
Sharp images have high-frequency edges -> high variance.
Blurry / out-of-focus images -> low variance.
"""

import cv2
import numpy as np


def compute_blur_score(image: np.ndarray) -> float:
    """Return the Laplacian variance of a BGR/gray image. Higher = sharper."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return float(laplacian.var())


def is_blurry(image: np.ndarray, threshold: float = 120.0) -> tuple[bool, float]:
    """
    Returns (is_blurry, score).
    threshold is tunable per document type/lighting conditions.
    """
    score = compute_blur_score(image)
    return score < threshold, score


def is_rotated(image: np.ndarray, max_skew_degrees: float = 8.0) -> tuple[bool, float]:
    """
    Estimates document skew via the dominant Hough line angle.
    Returns (is_rotated_beyond_threshold, estimated_angle_degrees).
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=120, minLineLength=100, maxLineGap=10)

    if lines is None or len(lines) == 0:
        return False, 0.0

    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 - x1 == 0:
            continue
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        # normalize to [-45, 45] so near-horizontal / near-vertical lines both count
        if angle > 45:
            angle -= 90
        elif angle < -45:
            angle += 90
        angles.append(angle)

    if not angles:
        return False, 0.0

    median_angle = float(np.median(angles))
    return abs(median_angle) > max_skew_degrees, median_angle
