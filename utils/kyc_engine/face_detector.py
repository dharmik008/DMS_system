"""
Caryanams KYC Module — Face Detection
----------------------------------------
Uses OpenCV's bundled Haar cascade classifier to check whether a
face photo is present on the document (required for Aadhaar Front).
No external model download needed — the cascade ships with opencv-python.
"""

import cv2
import numpy as np

_face_cascade = None


def _get_cascade() -> cv2.CascadeClassifier:
    global _face_cascade
    if _face_cascade is None:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(cascade_path)
    return _face_cascade


def detect_faces(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Returns a list of (x, y, w, h) bounding boxes for detected faces."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    cascade = _get_cascade()
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(60, 60),
    )
    return [tuple(f) for f in faces]


def has_face(image: np.ndarray) -> bool:
    return len(detect_faces(image)) > 0
