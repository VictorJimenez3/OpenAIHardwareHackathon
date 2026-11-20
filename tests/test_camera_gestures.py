import unittest
from unittest.mock import patch

from bedside.vision import FacePose, HeadGestures


class CameraGestureTests(unittest.TestCase):
    def setUp(self):
        self.tracker = HeadGestures(calibration_frames=4)
        self.addCleanup(self.tracker.close)
        self.face = None
        vision = patch.object(self.tracker._vision, "detect", side_effect=lambda _: self.face)
        fallback = patch.object(self.tracker._opencv, "detect", return_value=None)
        self.addCleanup(vision.stop)
        self.addCleanup(fallback.stop)
        vision.start()
        fallback.start()

    def sample(self, palm=None, yaw=None, pitch=0.0, ambiguous=False):
        self.tracker._vision.palm = palm
        self.tracker._vision.last_status = "ambiguous_face" if ambiguous else "no_face"
        self.face = None if yaw is None else FacePose(0.5, 0.5, 0.4, 0.4, 1, yaw, pitch, angles_valid=True)
        return self.tracker.feed("frame")

    def test_shake_crossing_center_in_either_direction(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                self.tracker.reset()
                angles = [0] * 4 + [0.18, 0.24, 0.04, -0.17, -0.23, 0, 0.20, -0.20]
                readings = [self.sample(yaw=yaw * direction) for yaw in angles]
                self.assertEqual([a for a in readings if a], ["no"])
                self.assertEqual(self.tracker.answer_source, "camera")

    def test_nod_and_hand_can_answer_successive_prompts(self):
        nod = [self.sample(yaw=0, pitch=pitch) for pitch in [0] * 4 + [0.18, 0.25, 0.02, 0]]
        self.assertEqual([a for a in nod if a], ["yes"])
        for axis, sign, expected in (("x", -1, "no"), ("y", 1, "yes"), ("y", -1, "yes"), ("x", 1, "no")):
            with self.subTest(axis=axis, sign=sign):
                self.tracker.reset()
                self.assertFalse(self.tracker.ready)
                self.assertIsNone(self.tracker.answer_source)
                readings = []
                for offset in (0, 0, 0, 0.10, 0.18, 0.22, 0.11, 0.04, 0, 0.17, 0.19, 0):
                    dx, dy = (sign * offset, 0) if axis == "x" else (0, sign * offset)
                    readings.append(self.sample(palm=(0.5 + dx, 0.5 + dy)))
                self.assertEqual([a for a in readings if a], [expected])
                self.assertEqual(self.tracker.answer_source, "camera_hand")

    def test_still_diagonal_and_interrupted_hand_do_not_answer(self):
        for path in (
            [(0.50, 0.50), (0.52, 0.48)] * 12,
            [(0.50, 0.50)] * 4 + [(0.62, 0.62), (0.68, 0.68), (0.50, 0.50)],
            [(0.50, 0.50)] * 4 + [(0.61, 0.50), (0.72, 0.50), None, (0.50, 0.50)] * 2,
            [(0.50, 0.50)] * 4 + [(0.72, 0.50)] + [(0.50, 0.50)] * 4,
        ):
            self.tracker.reset()
            self.assertFalse(any(self.sample(palm=point) for point in path))
        self.tracker.reset()
        for x in (0.5, 0.5, 0.5, 0.62, 0.7):
            self.assertIsNone(self.sample(palm=(x, 0.5)))
        self.assertIsNone(self.sample(palm=(0.5, 0.5), ambiguous=True))
        self.assertIsNone(self.sample(palm=(0.5, 0.5)))
