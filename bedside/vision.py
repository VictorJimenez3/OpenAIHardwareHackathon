"""Conservative head-gesture recognition for CoreS3 JPEG frames.

The preferred backend is Apple's Vision face detector. A small Swift bridge is
compiled on first use and kept alive as a subprocess, so Python needs neither
PyObjC nor a native extension. OpenCV is used when Vision is unavailable. A
frame without a clear face never becomes an answer.

``HeadGestures`` exposes only ``"yes"``, ``"no"`` and ``None``. The latter
covers calibration, stillness, an unclear movement, a lost face and the
neutral period required before another answer can be accepted.
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

while let line = readLine() {
    guard let data = Data(base64Encoded: line), let image = CIImage(data: data) else {
        emit("none")
        continue
    }
    let request = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(ciImage: image, options: [:])
    do {
        try handler.perform([request])
        let faces = request.results ?? []
        if faces.count > 1 {
            emit("ambiguous \(faces.count)")
            continue
        }
        if let face = faces.max(by: {
            $0.boundingBox.width * $0.boundingBox.height < $1.boundingBox.width * $1.boundingBox.height
        }) {
            let box = face.boundingBox
            let yaw = face.yaw?.doubleValue ?? 0
            let pitch = face.pitch?.doubleValue ?? 0
            let roll = face.roll?.doubleValue ?? 0
            let anglesValid = (face.yaw != nil && face.pitch != nil) ? 1 : 0
            emit("face \(box.midX) \(box.midY) \(box.width) \(box.height) \(face.confidence) \(yaw) \(pitch) \(roll) \(anglesValid)")
        } else {
            emit("none")
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
                line = process.stdout.readline().strip().split()
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


class HeadGestures:
    """Turn a bounded JPEG stream into deliberate yes/no answers.

    A fresh instance calibrates against ``calibration_frames`` neutral frames.
    After an answer is emitted, ``feed`` requires ``neutral_frames`` frames
    near that neutral pose before it can emit another answer. ``reset`` should
    be called whenever the Mac activates a new yes/no question.
    """

    def __init__(self, *, calibration_frames: int = 8, history_frames: int = 14,
                 min_amplitude: float = 0.075, axis_ratio: float = 1.22,
                 neutral_radius: float = 0.035, neutral_frames: int = 3,
                 min_confidence: float = 0.20, angle_amplitude: float = 0.14,
                 angle_neutral_radius: float = 0.10, neutral_stability: float = 0.05,
                 angle_neutral_stability: float = 0.10, max_gap_frames: int = 4,
                 candidate_max_frames: int = 15) -> None:
        self.calibration_frames = max(3, calibration_frames)
        self.history_frames = max(6, history_frames)
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
        self._lock = threading.Lock()
        self.reset()

    def reset(self, prompt_id: Optional[str] = None) -> None:
        """Start neutral calibration for a new prompt."""
        with self._lock:
            self.prompt_id = prompt_id
            self._calibration: Deque[Tuple[float, float]] = collections.deque(maxlen=self.calibration_frames)
            self._history: Deque[Tuple[float, float]] = collections.deque(maxlen=self.history_frames)
            self._baseline: Optional[Tuple[float, float]] = None
            self._mode: Optional[str] = None
            self._candidate_axis: Optional[str] = None
            self._candidate_sign = 0
            self._candidate_samples = 0
            self._candidate_neutral = 0
            self._candidate_age = 0
            self._neutral_gate = False
            self._neutral_gate_count = 0
            self._gap_count = 0
            self._last_detection_status = "calibrating"
            self.last_status = "calibrating"

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
        self._candidate_axis = None
        self._candidate_sign = 0
        self._candidate_samples = 0
        self._candidate_neutral = 0
        self._candidate_age = 0

    def _register_gap(self, status: str, *, hard: bool = False) -> None:
        """Pause briefly missing frames, then require neutral recentering."""
        self._gap_count += 1
        if hard or self._gap_count > self.max_gap_frames:
            self._history.clear()
            self._clear_candidate()
            self._neutral_gate = True
            self._neutral_gate_count = 0
            self.last_status = status if hard else "gap_recenter_required"
            return
        self.last_status = "gap_paused" if self._candidate_axis is not None else status

    def _thresholds(self, mode: str) -> Tuple[float, float, float]:
        if mode == "angles":
            return self.angle_amplitude, self.angle_neutral_radius, self.angle_neutral_stability
        return self.min_amplitude, self.neutral_radius, self.neutral_stability

    @staticmethod
    def _robust_baseline(points: list[Tuple[float, float]], stability: float) -> Optional[Tuple[float, float]]:
        """Estimate neutral while discarding endpoint noise on each axis."""
        if len(points) < 3:
            return None
        trim = max(1, len(points) // 5)
        sorted_x = sorted(point[0] for point in points)
        sorted_y = sorted(point[1] for point in points)
        inner_x = sorted_x[trim:-trim] or sorted_x
        inner_y = sorted_y[trim:-trim] or sorted_y
        baseline = (statistics.median(inner_x), statistics.median(inner_y))
        tolerance = stability * 2.0
        inlier_x = [value for value in sorted_x if abs(value - baseline[0]) <= tolerance]
        inlier_y = [value for value in sorted_y if abs(value - baseline[1]) <= tolerance]
        required = max(3, (len(points) * 3 + 4) // 5)
        if len(inlier_x) < required or len(inlier_y) < required:
            return None
        return baseline

    def _feed_pose(self, x: float, y: float, mode: str = "center") -> Optional[str]:
        """Consume a normalized pose. Used by deterministic smoke tests."""
        if not (math.isfinite(x) and math.isfinite(y)):
            self.last_status = "invalid_pose"
            return None
        self._gap_count = 0
        if self._mode is None:
            self._mode = mode
        elif mode != self._mode:
            self._history.clear()
            self._clear_candidate()
            self.last_status = "feature_mode_locked"
            return None

        threshold, neutral_radius, stability = self._thresholds(self._mode)
        if self._baseline is None:
            self._calibration.append((x, y))
            if len(self._calibration) >= self.calibration_frames:
                points = list(self._calibration)
                baseline = self._robust_baseline(points, stability)
                if baseline is not None:
                    self._baseline = baseline
                    self._history.clear()
                    self.last_status = "ready"
                else:
                    self.last_status = "stabilizing"
            return None

        dx, dy = x - self._baseline[0], y - self._baseline[1]
        self._history.append((dx, dy))
        near_neutral = abs(dx) <= neutral_radius and abs(dy) <= neutral_radius

        if self._neutral_gate:
            if near_neutral:
                self._neutral_gate_count += 1
                if self._neutral_gate_count >= self.neutral_frames:
                    self._neutral_gate = False
                    self._neutral_gate_count = 0
                    self._history.clear()
                    self.last_status = "ready"
                else:
                    self.last_status = "neutral_required"
            else:
                self._neutral_gate_count = 0
                self._history.clear()
                self.last_status = "neutral_required"
            return None

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
            self._candidate_samples = 1
            # At about five frames per second a clear nod can peak in just
            # one image. Still require a return toward the calibrated pose.
            if mode == "angles" and dominant == "vertical" and vertical >= threshold * 1.65:
                self._candidate_samples = 2
            self._candidate_neutral = 0
            self._candidate_age = 1
            self.last_status = "motion_candidate"
            return None

        self._candidate_age += 1
        if self._candidate_age > self.candidate_max_frames:
            self._history.clear()
            self._clear_candidate()
            self._neutral_gate = True
            self._neutral_gate_count = 0
            self.last_status = "candidate_timeout"
            return None

        if near_neutral:
            self._candidate_neutral += 1
            return_frames = min(self.neutral_frames, 2) if self._candidate_axis == "vertical" else self.neutral_frames
            if self._candidate_samples >= 2 and self._candidate_neutral >= return_frames:
                answer = "yes" if self._candidate_axis == "vertical" else "no"
                self._clear_candidate()
                self._neutral_gate = True
                self._neutral_gate_count = 0
                self._history.clear()
                self.last_status = "accepted_" + answer
                return answer
            self.last_status = "returning_neutral"
            return None

        self._candidate_neutral = 0
        if dominant is not None and dominant != self._candidate_axis:
            self._clear_candidate()
            self.last_status = "ambiguous_motion"
            return None
        value = dy if self._candidate_axis == "vertical" else dx
        if abs(value) >= threshold:
            sign = 1 if value > 0 else -1
            if sign != self._candidate_sign:
                self._clear_candidate()
                self.last_status = "ambiguous_motion"
                return None
            self._candidate_samples += 1
        self.last_status = "motion_candidate"
        return None

    def feed(self, jpeg_b64: str) -> Optional[str]:
        """Process one base64 JPEG frame and return ``yes``, ``no`` or ``None``."""
        if not isinstance(jpeg_b64, str) or not jpeg_b64.strip():
            with self._lock:
                self._register_gap("invalid_frame")
            return None
        pose = self._detect(jpeg_b64)
        with self._lock:
            if pose is None:
                self._register_gap(self._last_detection_status, hard=self._last_detection_status == "ambiguous_face")
                return None
            angles_valid = pose.angles_valid and all(
                math.isfinite(value) for value in (pose.yaw, pose.pitch, pose.roll)
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
    detector = HeadGestures(calibration_frames=4, history_frames=8, neutral_frames=2)
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
    detector.close()
    gap_detector.close()
    return neutral_ok and nod_ok and shake_ok and mixed_ok and gap_ok


if __name__ == "__main__":
    print("self_test:", "ok" if self_test() else "failed")
