import json
from types import SimpleNamespace

import cv2
import numpy as np

from monica_faces.detect import Detector


class FakeAnalysis:
    """InsightFace returns numpy scalars and float32 arrays, which the JSON upload cannot carry."""

    def get(self, image):
        return [
            SimpleNamespace(bbox=np.array([10, 20, 110, 140], np.float32), det_score=np.float32(0.9),
                            normed_embedding=np.ones(512, np.float32) / np.sqrt(512)),
            SimpleNamespace(bbox=np.array([0, 0, 10, 10], np.float32), det_score=np.float32(0.99),
                            normed_embedding=np.ones(512, np.float32)),
        ]


def test_found_faces_are_plain_floats_and_tiny_faces_are_skipped():
    detector = Detector("buffalo_l", ["CPUExecutionProvider"], 640)
    detector.__dict__["analysis"] = FakeAnalysis()
    image = cv2.imencode(".png", np.zeros((200, 200, 3), np.uint8))[1].tobytes()

    [face] = detector.faces(image)

    assert json.loads(json.dumps({"box": face.box, "quality": face.quality})) == {
        "box": [0.05, 0.1, 0.5, 0.6], "quality": 0.9}
    assert face.crop.startswith(b"\xff\xd8")
    assert detector.faces(b"not an image") is None
