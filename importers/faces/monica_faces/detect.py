"""InsightFace detection and ArcFace embeddings, decoded from bytes in memory."""

from dataclasses import dataclass
from functools import cached_property

import cv2
import numpy as np

# Faces smaller than this many pixels on a side identify no one reliably.
MIN_SIDE = 24
# Full quality from this size up; smaller faces are discounted.
CLEAR_SIDE = 80
CROP = 112


@dataclass(frozen=True)
class Found:
    box: tuple[float, float, float, float]  # left, top, width, height as fractions of the image
    quality: float
    embedding: np.ndarray  # unit-length float32
    crop: bytes  # JPEG


class Detector:
    def __init__(self, model: str, providers: list[str], det_size: int):
        self.model, self.providers, self.det_size = model, providers, det_size

    @cached_property
    def analysis(self):
        from insightface.app import FaceAnalysis

        from .config import settings
        app = FaceAnalysis(name=self.model, root=str(settings().model_root),
                           allowed_modules=["detection", "recognition"], providers=self.providers)
        app.prepare(ctx_id=-1, det_size=(self.det_size, self.det_size))
        return app

    def faces(self, data: bytes) -> list[Found] | None:
        """None when the bytes are not an image OpenCV can read."""
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        height, width = image.shape[:2]
        found = []
        for face in self.analysis.get(image):
            left, top, right, bottom = map(float, face.bbox)
            left, top = max(0.0, left), max(0.0, top)
            right, bottom = min(float(width), right), min(float(height), bottom)
            side = min(right - left, bottom - top)
            if side < MIN_SIDE:
                continue
            quality = float(face.det_score) * min(1.0, side / CLEAR_SIDE)
            found.append(Found(
                box=(left / width, top / height, (right - left) / width, (bottom - top) / height),
                quality=round(quality, 4),
                embedding=face.normed_embedding.astype(np.float32),
                crop=_crop(image, left, top, right, bottom),
            ))
        return found


def _crop(image: np.ndarray, left: float, top: float, right: float, bottom: float) -> bytes:
    """A square crop with some margin, for checking matches locally."""
    size = max(right - left, bottom - top) * 1.3
    cx, cy = (left + right) / 2, (top + bottom) / 2
    height, width = image.shape[:2]
    x0, y0 = int(max(0, cx - size / 2)), int(max(0, cy - size / 2))
    x1, y1 = int(min(width, cx + size / 2)), int(min(height, cy + size / 2))
    square = cv2.resize(image[y0:y1, x0:x1], (CROP, CROP), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", square, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
