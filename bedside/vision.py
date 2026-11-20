"""Head and hand gesture recognition for CoreS3 JPEG frames.

Apple Vision detects faces and hand landmarks on the Mac. A small Swift bridge is
compiled on first use and kept alive as a subprocess, so Python needs neither
PyObjC nor a native extension. OpenCV provides a face-only fallback.

``HeadGestures`` exposes only ``"yes"``, ``"no"`` and ``None``. The latter
covers calibration, stillness, unclear movement and tracking loss.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import math
import os
import platform
import select
import shutil
import statistics
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


_VISION_BRIDGE_SOURCE = r"""import Foundation
import Vision
import CoreImage

func emit(_ value: String) {
    FileHandle.standardOutput.write((value + "\n").data(using: .utf8)!)
}

let faceRequest = VNDetectFaceRectanglesRequest()
let handRequest = VNDetectHumanHandPoseRequest()
handRequest.maximumHandCount = 2

func palmPosition() -> String {
    guard let hands = handRequest.results, !hands.isEmpty else { return "none" }
    guard hands.count == 1 else { return "ambiguous" }
    let joints: [VNHumanHandPoseObservation.JointName] = [.indexMCP, .middleMCP, .ringMCP, .littleMCP]
    let points = joints.compactMap { try? hands[0].recognizedPoint($0) }.filter { $0.confidence >= 0.35 }
    guard points.count >= 3 else { return "none" }
    let count = CGFloat(points.count)
    let x = points.reduce(0.0) { $0 + $1.location.x } / count
    let y = points.reduce(0.0) { $0 + $1.location.y } / count
    return "palm \(x) \(y)"
}

while let line = readLine() {
    guard let data = Data(base64Encoded: line), let image = CIImage(data: data) else {
        emit("none")
        continue
    }
    let handler = VNImageRequestHandler(ciImage: image, options: [:])
    do {
        try handler.perform([faceRequest])
        let faces = faceRequest.results ?? []
        if faces.count > 1 {
            emit("ambiguous \(faces.count)")
            continue
        }
        // A failed hand request must not disable an otherwise usable face.
        let hands = (try? handler.perform([handRequest])) == nil ? "none" : palmPosition()
        if let face = faces.first {
            let box = face.boundingBox
            let yaw = face.yaw?.doubleValue ?? 0
            let pitch = face.pitch?.doubleValue ?? 0
            let roll = face.roll?.doubleValue ?? 0
            let anglesValid = (face.yaw != nil && face.pitch != nil) ? 1 : 0
            emit("face \(box.midX) \(box.midY) \(box.width) \(box.height) \(face.confidence) \(yaw) \(pitch) \(roll) \(anglesValid)|\(hands)")
        } else {
            emit("none|\(hands)")
        }
    } catch {
        emit("none")
    }
}
"""


@dataclass(frozen=True)
class FacePose:
    """Normalized location of the largest detected face."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    angles_valid: bool = False


class _VisionBridge:
    """Persistent Vision subprocess, compiled lazily and cached per machine."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen[str]] = None
        self.palm: Optional[Tuple[float, float]] = None
        self._lock = threading.Lock()
        self.error: Optional[str] = None
        self.last_status = "unavailable"
        self._has_response = False

    def _binary(self) -> Optional[str]:
        if platform.system() != "Darwin":
            return None
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            return None
        digest = hashlib.sha256(_VISION_BRIDGE_SOURCE.encode("utf-8")).hexdigest()[:16]
        cache_dir = os.path.join(tempfile.gettempdir(), "cores3-vision")
        binary = os.path.join(cache_dir, "face-" + digest)
        if os.path.isfile(binary) and os.access(binary, os.X_OK):
            return binary
        os.makedirs(cache_dir, exist_ok=True)
        source = os.path.join(cache_dir, "face-" + digest + ".swift")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(_VISION_BRIDGE_SOURCE)
        result = subprocess.run(
            [swiftc, source, "-o", binary, "-framework", "Vision", "-framework", "CoreImage"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            self.error = (result.stderr or "swiftc failed").strip()[-400:]
            return None
        return binary

    def _start(self) -> bool:
        binary = self._binary()
        if binary is None:
            return False
        try:
            self._process = subprocess.Popen(
                [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._has_response = False
            return True
        except OSError as exc:
            self.error = str(exc)
            self._process = None
            return False

    def detect(self, jpeg_b64: str) -> Optional[FacePose]:
        """Return the largest Vision face for one base64 JPEG, if available."""
        with self._lock:
            self.palm = None
            if self._process is None and not self._start():
                self.last_status = "unavailable"
                return None
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                self.last_status = "unavailable"
                return None
            try:
                process.stdin.write(jpeg_b64.strip() + "\n")
                process.stdin.flush()
                timeout = 3.0 if not self._has_response else 0.75
                ready, _, _ = select.select([process.stdout], [], [], timeout)
                if not ready:
                    self.error = "Vision helper timed out"
                    self.last_status = "gap"
                    self._close_locked()
                    return None
                face_data, _, hand_data = process.stdout.readline().strip().partition("|")
                hand_fields = hand_data.split()
                if len(hand_fields) == 3 and hand_fields[0] == "palm":
                    x, y = map(float, hand_fields[1:])
                    if 0 <= x <= 1 and 0 <= y <= 1:
                        self.palm = (x, y)
                line = face_data.split()
                if not line:
                    self._close_locked()
                    self.last_status = "gap"
                    return None
                if line[0] == "ambiguous":
                    self._has_response = True
                    self.error = None
                    self.last_status = "ambiguous_face"
                    return None
                if line[0] != "face" or len(line) != 10:
                    self._has_response = True
                    self.error = None
                    self.last_status = "no_face"
                    return None
                values = [float(item) for item in line[1:]]
                self._has_response = True
                self.error = None
                self.last_status = "face"
                return FacePose(*values[:8], angles_valid=bool(int(values[8])))
            except (BrokenPipeError, OSError, ValueError):
                self.error = "Vision helper stopped"
                self.last_status = "gap"
                self._close_locked()
                return None

    def _close_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()

    def close(self) -> None:
        with self._lock:
            self._close_locked()


class _OpenCVDetector:
    """Optional fallback based on a standard OpenCV Haar cascade."""

    def __init__(self) -> None:
        self.cv2 = None
        self.np = None
        self.classifier = None
        self.last_status = "unavailable"
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
            cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            classifier = cv2.CascadeClassifier(cascade_path)
            if classifier.empty():
                return
            self.cv2, self.np, self.classifier = cv2, np, classifier
            self.last_status = "ready"
        except Exception:
            return

    def detect(self, jpeg_b64: str) -> Optional[FacePose]:
        if self.cv2 is None or self.np is None or self.classifier is None:
            return None
        try:
            raw = base64.b64decode(jpeg_b64, validate=True)
            image = self.cv2.imdecode(self.np.frombuffer(raw, dtype=self.np.uint8), self.cv2.IMREAD_GRAYSCALE)
            if image is None:
                return None
            height, width = image.shape[:2]
            faces = self.classifier.detectMultiScale(image, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            if len(faces) > 1:
                self.last_status = "ambiguous_face"
                return None
            if len(faces) == 0 or width == 0 or height == 0:
                self.last_status = "no_face"
                return None
            x, y, w, h = max(faces, key=lambda box: int(box[2]) * int(box[3]))
            self.last_status = "face"
            return FacePose((x + w / 2) / width, 1 - (y + h / 2) / height, w / width, h / height, 0.8)
        except Exception:
            self.last_status = "invalid_frame"
            return None


class HandWave:
    """Track a single visible palm from a short rest through an out-and-back stroke."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.rest: Deque[Tuple[float, float]] = collections.deque(maxlen=3)
        self.origin: Optional[Tuple[float, float]] = None
        self.axis: Optional[str] = None
        self.sign = 0
        self.peak = 0.0
        self.samples = 0
        self.age = 0

    @property
    def ready(self) -> bool:
        return self.origin is not None

    def feed(self, point: Optional[Tuple[float, float]]) -> Optional[str]:
        if point is None:
            # A new hand entering the frame must establish its own origin.
            self.reset()
            return None
        if self.origin is None:
            self.rest.append(point)
            if len(self.rest) == self.rest.maxlen:
                xs, ys = zip(*self.rest)
                if max(xs) - min(xs) < 0.045 and max(ys) - min(ys) < 0.045:
                    self.origin = (statistics.median(xs), statistics.median(ys))
            return None

        dx, dy = point[0] - self.origin[0], point[1] - self.origin[1]
        if self.axis is None:
            if max(abs(dx), abs(dy)) < 0.09:
                return None
            if abs(dx) > 1.6 * abs(dy):
                self.axis = "horizontal"
            elif abs(dy) > 1.6 * abs(dx):
                self.axis = "vertical"
            else:
                self.reset()
                return None
            displacement = dx if self.axis == "horizontal" else dy
            self.sign = 1 if displacement > 0 else -1

        along, across = (dx, dy) if self.axis == "horizontal" else (dy, dx)
        travel = along * self.sign
        self.age += 1
        self.peak = max(self.peak, travel)
        if self.age > 20 or abs(across) > max(0.07, self.peak * 0.65):
            self.reset()
            return None
        if travel >= 0.09:
            self.samples += 1
        elif self.samples >= 2 and self.peak >= 0.14 and travel <= 0.45 * self.peak:
            answer = "yes" if self.axis == "vertical" else "no"
            self.reset()
            return answer
        return None


class HeadGestures:
    """Turn a bounded JPEG stream into deliberate yes/no answers.

    Call ``reset`` for each question. The detector calibrates to the person's
    resting pose and accepts at most one answer for that question.
    """

    def __init__(self, *, calibration_frames: int = 8,
                 min_amplitude: float = 0.075, axis_ratio: float = 1.22,
                 neutral_radius: float = 0.035, neutral_frames: int = 3,
                 min_confidence: float = 0.20, angle_amplitude: float = 0.13,
                 angle_neutral_radius: float = 0.085, neutral_stability: float = 0.05,
                 angle_neutral_stability: float = 0.10, max_gap_frames: int = 3,
                 candidate_max_frames: int = 15) -> None:
        self.calibration_frames = max(3, calibration_frames)
        self.min_amplitude = max(0.02, min_amplitude)
        self.axis_ratio = max(1.0, axis_ratio)
        self.neutral_radius = max(0.01, neutral_radius)
        self.neutral_frames = max(1, neutral_frames)
        self.min_confidence = min_confidence
        self.angle_amplitude = max(0.05, angle_amplitude)
        self.angle_neutral_radius = max(0.02, angle_neutral_radius)
        self.neutral_stability = max(0.01, neutral_stability)
        self.angle_neutral_stability = max(0.02, angle_neutral_stability)
        self.max_gap_frames = max(1, max_gap_frames)
        self.candidate_max_frames = max(6, candidate_max_frames)
        self._vision = _VisionBridge()
        self._opencv = _OpenCVDetector()
        self._hand = HandWave()
        self._lock = threading.Lock()
        self.reset()

    def reset(self, prompt_id: Optional[str] = None) -> None:
        """Start neutral calibration for a new prompt."""
        with self._lock:
            self.prompt_id = prompt_id
            self._calibration: Deque[Tuple[float, float]] = collections.deque(maxlen=self.calibration_frames)
            self._reset_tracking()
            self._hand.reset()
            self.answer_source = None
            self._answered = False
            self._gap_count = 0
            self._last_detection_status = "calibrating"
            self.last_status = "calibrating"

    @property
    def ready(self) -> bool:
        """Whether there is a resting pose for the current question."""
        return self._baseline is not None or self._hand.ready or self._answered

    @property
    def feature_mode(self) -> Optional[str]:
        """Current feature source, either ``angles`` or ``center``."""
        return self._mode

    @property
    def backend(self) -> str:
        if self._vision.error is None and platform.system() == "Darwin" and shutil.which("swiftc"):
            return "apple-vision"
        if self._opencv.classifier is not None:
            return "opencv-haar"
        return "unavailable"

    def _detect(self, jpeg_b64: str) -> Optional[FacePose]:
        pose = self._vision.detect(jpeg_b64)
        if pose is None and self._vision.last_status == "ambiguous_face":
            self._last_detection_status = "ambiguous_face"
            return None
        if pose is None:
            pose = self._opencv.detect(jpeg_b64)
            self._last_detection_status = self._opencv.last_status
            if self._opencv.classifier is None:
                self._last_detection_status = self._vision.last_status
        else:
            self._last_detection_status = "face"
        if pose is None or pose.confidence < self.min_confidence or pose.width <= 0:
            return None
        return pose

    def _clear_candidate(self) -> None:
        self._candidate_axis: Optional[str] = None
        self._candidate_sign = 0
        self._candidate_samples = 0
        self._candidate_neutral = 0
        self._candidate_age = 0
        self._candidate_peak = 0.0
        self._opposite_samples = 0
        self._opposite_peak = 0.0

    def _accept(self, answer: str, source: str = "camera") -> str:
        self._clear_candidate()
        self._answered = True
        self.answer_source = source
        self.last_status = "accepted_" + answer
        return answer

    def _reset_tracking(self) -> None:
        self._calibration.clear()
        self._baseline: Optional[Tuple[float, float]] = None
        self._mode: Optional[str] = None
        self._clear_candidate()

    def _register_gap(self, status: str, *, hard: bool = False) -> None:
        """Allow short occlusions. Recover from sustained loss with a fresh baseline."""
        self._gap_count += 1
        if self._candidate_axis is not None:
            self._candidate_age += 1
            self._candidate_neutral = 0
        if hard or self._gap_count > self.max_gap_frames or self._candidate_age >= self.candidate_max_frames:
            self._reset_tracking()
            self.last_status = status if hard else "recalibrating"
            return
        self.last_status = "gap_paused" if self._candidate_axis is not None else status

    def _thresholds(self, mode: str) -> Tuple[float, float, float]:
        if mode == "angles":
            return self.angle_amplitude, self.angle_neutral_radius, self.angle_neutral_stability
        return self.min_amplitude, self.neutral_radius, self.neutral_stability

    @staticmethod
    def _robust_baseline(points: list[Tuple[float, float]], stability: float) -> Optional[Tuple[float, float]]:
        """Use the most recent settled pose after the initial tracking window."""
        if len(points) < 3:
            return None
        recent_x, recent_y = zip(*points[-max(3, len(points) // 2):])
        if max(recent_x) - min(recent_x) > stability or max(recent_y) - min(recent_y) > stability:
            return None
        baseline = (statistics.median(recent_x), statistics.median(recent_y))
        tolerance = stability * 1.5
        required = max(3, (len(points) * 3 + 4) // 5)
        inliers = sum(abs(x - baseline[0]) <= tolerance and abs(y - baseline[1]) <= tolerance for x, y in points)
        return baseline if inliers >= required else None

    def _feed_pose(self, x: float, y: float, mode: str = "center") -> Optional[str]:
        """Consume a normalized pose. Used by deterministic smoke tests."""
        if self._answered:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            self._register_gap("invalid_pose")
            return None
        if self._mode is None:
            self._mode = mode
        elif mode != self._mode:
            self._register_gap("feature_gap")
            return None
        self._gap_count = 0

        threshold, neutral_radius, stability = self._thresholds(self._mode)
        if self._baseline is None:
            self.last_status = "calibrating"
            self._calibration.append((x, y))
            if len(self._calibration) >= self.calibration_frames:
                points = list(self._calibration)
                baseline = self._robust_baseline(points, stability)
                self._baseline = baseline
                self.last_status = "ready" if baseline is not None else "stabilizing"
            return None

        dx, dy = x - self._baseline[0], y - self._baseline[1]
        near_neutral = abs(dx) <= neutral_radius and abs(dy) <= neutral_radius

        vertical, horizontal = abs(dy), abs(dx)
        if vertical >= threshold and vertical >= horizontal * self.axis_ratio:
            dominant: Optional[str] = "vertical"
        elif horizontal >= threshold and horizontal >= vertical * self.axis_ratio:
            dominant = "horizontal"
        else:
            dominant = None

        if self._candidate_axis is None:
            if dominant is None:
                self.last_status = "ready" if near_neutral else "moving"
                return None
            value = dy if dominant == "vertical" else dx
            self._candidate_axis = dominant
            self._candidate_sign = 1 if value > 0 else -1
            self._candidate_peak = abs(value)
            self._candidate_samples = 1
            self._candidate_age = 1
            self.last_status = "motion_candidate"
            return None

        self._candidate_age += 1
        if self._candidate_age > self.candidate_max_frames:
            self._reset_tracking()
            self.last_status = "candidate_timeout"
            return None

        if dominant is not None and dominant != self._candidate_axis:
            self._reset_tracking()
            self.last_status = "ambiguous_motion"
            return None

        nod = self._candidate_axis == "vertical"
        value = dy if nod else dx
        strong_nod = False
        if nod and mode == "angles":
            # A nod can return to a slightly different resting pitch.
            # Require a substantial reversal of the measured excursion.
            return_radius = max(neutral_radius, min(threshold * 0.95, self._candidate_peak * 0.48))
            near_neutral = vertical <= return_radius and horizontal <= max(neutral_radius, threshold)
            strong_nod = self._candidate_peak >= threshold * 1.65

        if near_neutral:
            self._candidate_neutral += 1
            return_frames = min(self.neutral_frames, 2) if nod else self.neutral_frames
            # At five frames per second, a sustained dip can have just
            # one clear return frame before the person nods again.
            if strong_nod and self._candidate_samples >= 2 and vertical <= self._candidate_peak * 0.35:
                return_frames = 1
            if self._candidate_neutral >= return_frames:
                # A lone large peak still needs two return frames.
                sustained = self._candidate_samples >= 2 or strong_nod
                if not nod and self._opposite_samples:
                    # A fast shake may sample each side only once. Require
                    # clear excursions on both sides and a settled return.
                    sustained |= min(self._candidate_peak, self._opposite_peak) >= 1.5 * threshold
                if sustained:
                    return self._accept("yes" if nod else "no")
                self._clear_candidate()
                self.last_status = "ready"
                return None
            self.last_status = "returning_neutral"
            return None

        self._candidate_neutral = 0
        if abs(value) >= threshold and value * self._candidate_sign < 0:
            if not nod and dominant == "horizontal":
                # A normal shake crosses center without stopping there.
                # Two measured samples on each side establish the gesture,
                # even when no neutral frame was captured in between.
                self._opposite_samples += 1
                self._opposite_peak = max(self._opposite_peak, abs(value))
                if self._candidate_samples >= 2 and self._opposite_samples >= 2:
                    return self._accept("no")
                self.last_status = "shake_reversal"
                return None
            self._reset_tracking()
            self.last_status = "ambiguous_motion"
            return None
        if dominant == self._candidate_axis:
            self._candidate_samples += 1
            self._candidate_peak = max(self._candidate_peak, abs(value))
        self.last_status = "motion_candidate"
        return None

    def feed(self, jpeg_b64: str) -> Optional[str]:
        """Process one base64 JPEG frame and return ``yes``, ``no`` or ``None``."""
        if not isinstance(jpeg_b64, str) or not jpeg_b64.strip():
            with self._lock:
                self._hand.reset()
                self._register_gap("invalid_frame")
            return None
        pose = self._detect(jpeg_b64)
        with self._lock:
            if self._answered:
                return None
            if self._last_detection_status == "ambiguous_face":
                self._hand.reset()
            else:
                hand_answer = self._hand.feed(self._vision.palm)
                if hand_answer:
                    return self._accept(hand_answer, "camera_hand")
            if pose is None:
                self._register_gap(self._last_detection_status, hard=self._last_detection_status == "ambiguous_face")
                return None
            angles_valid = pose.angles_valid and all(
                math.isfinite(value) for value in (pose.yaw, pose.pitch)
            )
            if self._mode == "angles":
                if not angles_valid:
                    self._register_gap("feature_gap")
                    return None
                return self._feed_pose(pose.yaw, pose.pitch, "angles")
            if self._mode == "center":
                return self._feed_pose(pose.x, pose.y, "center")
            if angles_valid:
                return self._feed_pose(pose.yaw, pose.pitch, "angles")
            return self._feed_pose(pose.x, pose.y, "center")

    def close(self) -> None:
        self._vision.close()

    def __enter__(self) -> "HeadGestures":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def self_test() -> bool:
    """Run a dependency-free motion test for setup checks."""
    detector = HeadGestures(calibration_frames=4, neutral_frames=2)
    for _ in range(4):
        detector._feed_pose(0.5, 0.5)
    neutral_ok = all(detector._feed_pose(0.5, 0.5) is None for _ in range(6))
    detector.reset("nod")
    for _ in range(4):
        detector._feed_pose(0.5, 0.5)
    result = None
    for y in (0.40, 0.39, 0.44, 0.50, 0.50):
        result = detector._feed_pose(0.5, y) or result
    nod_ok = result == "yes"

    detector.reset("shake")
    for _ in range(4):
        detector._feed_pose(0.5, 0.5)
    result = None
    for x in (0.40, 0.39, 0.44, 0.50, 0.50):
        result = detector._feed_pose(x, 0.5) or result
    shake_ok = result == "no"

    detector.reset("mixed")
    for _ in range(4):
        detector._feed_pose(0.5, 0.5)
    result = None
    for x, y in ((0.40, 0.40), (0.39, 0.39), (0.50, 0.50), (0.50, 0.50)):
        result = detector._feed_pose(x, y) or result
    mixed_ok = result is None

    gap_detector = HeadGestures(calibration_frames=4, neutral_frames=3, max_gap_frames=2)
    for _ in range(4):
        gap_detector._feed_pose(0.5, 0.5)
    gap_detector._feed_pose(0.5, 0.40)
    gap_detector._feed_pose(0.5, 0.39)
    gap_detector._register_gap("feature_gap")
    gap_detector._register_gap("feature_gap")
    gap_detector._feed_pose(0.5, 0.39)
    gap_result = None
    for _ in range(3):
        gap_result = gap_detector._feed_pose(0.5, 0.5) or gap_result
    gap_ok = gap_result == "yes"

    # A shifted resting pose and a rebound visible in only one frame
    # reproduce the missed nod at CoreS3 capture speed.
    gap_detector.reset("reacquire")
    for _ in range(4):
        gap_detector._feed_pose(0.0, 0.1, "angles")
    gap_detector._feed_pose(0.0, 0.4, "angles")
    for _ in range(3):
        gap_detector._register_gap("feature_gap")
    lost_ok = not gap_detector.ready
    holds = [gap_detector._feed_pose(0.08, -0.26, "angles") for _ in range(4)]
    recovered = gap_detector.ready and all(answer is None for answer in holds)
    dip = [(0.00, 0.17), (0.03, 0.09), (0.04, -0.16)]
    observed = [gap_detector._feed_pose(yaw, pitch, "angles") for yaw, pitch in dip]
    rebound_ok = observed == [None, None, "yes"]
    repeated = [gap_detector._feed_pose(yaw, pitch, "angles") for yaw, pitch in dip]
    one_answer = all(answer is None for answer in repeated)

    detector.close()
    gap_detector.close()
    return all((neutral_ok, nod_ok, shake_ok, mixed_ok, gap_ok, lost_ok, recovered, rebound_ok, one_answer))


if __name__ == "__main__":
    passed = self_test()
    print("self_test:", "ok" if passed else "failed")
    raise SystemExit(0 if passed else 1)
