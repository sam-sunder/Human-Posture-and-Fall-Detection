from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from config import (
    FALL_ANGULAR_VELOCITY_THRESHOLD,
    FALL_ASPECT_RATIO_THRESHOLD,
    FALL_CONFIRMATION_SECONDS,
    FALL_VELOCITY_THRESHOLD,
    INFERENCE_SIZE,
    JPEG_QUALITY,
    MIN_CONFIDENCE,
    MODEL_NAME,
    PROLONGED_POSTURE_SECONDS,
    TORSO_LYING_ANGLE,
    TORSO_UPRIGHT_ANGLE,
)

# Skeleton connections for YOLO Pose (COCO 17 keypoints)
SKELETON_PAIRS: List[Tuple[int, int]] = [
    (5, 6),    # left shoulder - right shoulder
    (5, 11),   # left shoulder - left hip
    (6, 12),   # right shoulder - right hip
    (11, 12),  # left hip - right hip
    (5, 7),    # left shoulder - left elbow
    (7, 9),    # left elbow - left wrist
    (6, 8),    # right shoulder - right elbow
    (8, 10),   # right elbow - right wrist
    (11, 13),  # left hip - left knee
    (13, 15),  # left knee - left ankle
    (12, 14),  # right hip - right knee
    (14, 16),  # right knee - right ankle
]


class PostureTracker:
    def __init__(self, model_path: str = MODEL_NAME) -> None:
        self.model_path: str = model_path
        self._model: Optional[YOLO] = None

        # Trajectory history for dynamic velocity & angle tracking
        # Each item: (timestamp, cy, torso_angle, box_height, aspect_ratio, cx)
        self.history: Deque[Tuple[float, float, Optional[float], float, float, float]] = deque(maxlen=60)

        # Prolonged posture tracking
        self.current_posture: str = "UNKNOWN"
        self.posture_start_time: Optional[float] = None
        self.posture_duration: float = 0.0
        self.prolonged_alert: bool = False

        # Fall detection FSM
        # States: "NORMAL", "DESCENT_DETECTED", "FALL_CONFIRMED"
        self.fall_state: str = "NORMAL"
        self.descent_time: Optional[float] = None
        self.lying_start_time: Optional[float] = None
        self.fall_alert: bool = False
        self.fall_cooldown_time: Optional[float] = None

        # Cached telemetry for API/WebSockets
        self.latest_telemetry: Dict[str, Any] = {}

    @property
    def model(self) -> YOLO:
        if self._model is None:
            self._model = YOLO(self.model_path)
        return self._model

    def reset(self) -> None:
        self.history.clear()
        self.current_posture = "UNKNOWN"
        self.posture_start_time = None
        self.posture_duration = 0.0
        self.prolonged_alert = False
        self.fall_state = "NORMAL"
        self.descent_time = None
        self.lying_start_time = None
        self.fall_alert = False
        self.fall_cooldown_time = None
        self.latest_telemetry = {}

    def _classify_posture(self, torso_angle: Optional[float], aspect_ratio: float) -> str:
        """Classifies human posture based on torso inclination angle and bounding box geometry."""
        if torso_angle is None:
            # Fallback to aspect ratio if keypoints are occluded
            if aspect_ratio >= FALL_ASPECT_RATIO_THRESHOLD:
                return "LYING DOWN"
            elif aspect_ratio <= 0.65:
                return "STANDING"
            else:
                return "SITTING"

        if torso_angle >= TORSO_LYING_ANGLE or aspect_ratio >= FALL_ASPECT_RATIO_THRESHOLD:
            return "LYING DOWN"
        elif torso_angle <= TORSO_UPRIGHT_ANGLE:
            # Distinguish standing vs sitting via aspect ratio
            if aspect_ratio <= 0.68:
                return "STANDING"
            else:
                return "SITTING"
        else:
            return "LEANING / BENDING"

    def _update_prolonged_posture(
        self,
        now: float,
        detected_posture: str,
        cx: float,
        cy: float,
        box_h: float,
    ) -> None:
        """Monitors posture continuity and triggers alert if unchanged beyond threshold."""
        if self.posture_start_time is None or detected_posture != self.current_posture:
            self.current_posture = detected_posture
            self.posture_start_time = now
            self.posture_duration = 0.0
            self.prolonged_alert = False
        else:
            self.posture_duration = now - self.posture_start_time
            if self.posture_duration >= PROLONGED_POSTURE_SECONDS:
                self.prolonged_alert = True

    def _update_fall_detection(
        self,
        now: float,
        cy: float,
        torso_angle: Optional[float],
        box_h: float,
        aspect_ratio: float,
        posture: str,
    ) -> Tuple[float, float]:
        """Calculates vertical velocity and angular velocity to detect falls and confirm floor immobility."""
        downward_vel: float = 0.0
        angular_vel: float = 0.0

        # Look back ~0.25 to 0.45 seconds in trajectory history to calculate velocity
        if len(self.history) >= 1:
            target_time = now - 0.35
            closest: Optional[Tuple[float, float, Optional[float], float, float, float]] = None
            for item in self.history:
                if item[0] <= target_time:
                    closest = item
                else:
                    break
            if closest is None:
                closest = self.history[0]

            dt = now - closest[0]
            if dt >= 0.08:
                # Downward velocity in normalized body heights per second
                downward_vel = (cy - closest[1]) / (box_h * dt)

                if torso_angle is not None and closest[2] is not None:
                    angle_diff = abs(torso_angle - closest[2])
                    angle_diff = min(angle_diff, 360.0 - angle_diff)
                    angular_vel = angle_diff / dt

        # Fall Detection State Machine
        # Phase 1: Rapid descent trigger
        is_rapid_descent = (
            downward_vel >= FALL_VELOCITY_THRESHOLD
            and (
                angular_vel >= FALL_ANGULAR_VELOCITY_THRESHOLD
                or (torso_angle is not None and torso_angle >= 45.0)
                or aspect_ratio >= 0.95
            )
        )

        if is_rapid_descent and self.fall_state == "NORMAL":
            self.fall_state = "DESCENT_DETECTED"
            self.descent_time = now
            self.lying_start_time = None

        # Phase 2: Post-descent lying & confirmation
        if self.fall_state == "DESCENT_DETECTED":
            if self.descent_time is not None:
                time_since_descent = now - self.descent_time
                if time_since_descent <= 3.5:
                    # Check if person is on the ground / lying down
                    if posture == "LYING DOWN" or aspect_ratio >= FALL_ASPECT_RATIO_THRESHOLD:
                        if self.lying_start_time is None:
                            self.lying_start_time = now
                        immobility_time = now - self.lying_start_time

                        if immobility_time >= FALL_CONFIRMATION_SECONDS:
                            self.fall_state = "FALL_CONFIRMED"
                            self.fall_alert = True
                    else:
                        # If person quickly recovers upright posture, cancel false alarm
                        if posture in ("STANDING", "SITTING") and (time_since_descent >= 1.0):
                            self.fall_state = "NORMAL"
                            self.descent_time = None
                            self.lying_start_time = None
                else:
                    # Descent window expired without confirmation
                    self.fall_state = "NORMAL"
                    self.descent_time = None
                    self.lying_start_time = None

        # Phase 3: Auto-recovery from confirmed fall when person stands up
        if self.fall_state == "FALL_CONFIRMED":
            if posture in ("STANDING", "SITTING"):
                if self.fall_cooldown_time is None:
                    self.fall_cooldown_time = now
                elif now - self.fall_cooldown_time >= 2.0:
                    # Reset after person stays upright for 2 seconds
                    self.fall_state = "NORMAL"
                    self.fall_alert = False
                    self.fall_cooldown_time = None
            else:
                self.fall_cooldown_time = None

        return downward_vel, angular_vel

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        now = time.monotonic()
        h, w = frame.shape[:2]

        results = self.model.predict(
            source=frame,
            conf=MIN_CONFIDENCE,
            imgsz=INFERENCE_SIZE,
            verbose=False,
        )
        result = results[0]

        detected: bool = False
        box: Optional[np.ndarray] = None
        keypoints: Optional[np.ndarray] = None
        torso_angle: Optional[float] = None
        cx: float = float(w / 2)
        cy: float = float(h / 2)
        box_h: float = float(h)
        aspect_ratio: float = 1.0

        if result.boxes is not None and len(result.boxes) > 0:
            boxes = result.boxes.xyxy.cpu().numpy()
            confs = result.boxes.conf.cpu().numpy()
            best_idx = int(np.argmax(confs))
            box = boxes[best_idx].astype(int)
            x1, y1, x2, y2 = box
            box_w = max(1.0, float(x2 - x1))
            box_h = max(1.0, float(y2 - y1))
            aspect_ratio = box_w / box_h
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0

            if result.keypoints is not None:
                k = result.keypoints.data.cpu().numpy()[best_idx]
                keypoints = k
                if len(k) >= 13:
                    ls, rs, lh, rh = k[5], k[6], k[11], k[12]
                    # Check shoulder & hip keypoint confidence
                    if min(ls[2], rs[2], lh[2], rh[2]) >= 0.15:
                        shoulder = (ls[:2] + rs[:2]) / 2.0
                        hip = (lh[:2] + rh[:2]) / 2.0
                        dx = shoulder[0] - hip[0]
                        dy = shoulder[1] - hip[1]
                        # Angle with vertical axis
                        torso_angle = float(np.degrees(np.arctan2(abs(dx), -dy)))

            detected = True

        if detected and box is not None:
            # 1. Classify Posture
            posture = self._classify_posture(torso_angle, aspect_ratio)

            # 2. Update Prolonged Posture Tracker
            self._update_prolonged_posture(now, posture, cx, cy, box_h)

            # 3. Update Fall Detection
            downward_vel, angular_vel = self._update_fall_detection(
                now, cy, torso_angle, box_h, aspect_ratio, posture
            )

            # Record in trajectory history
            self.history.append((now, cy, torso_angle, box_h, aspect_ratio, cx))

            # Draw Skeleton Keypoints
            if keypoints is not None:
                for idx1, idx2 in SKELETON_PAIRS:
                    pt1, pt2 = keypoints[idx1], keypoints[idx2]
                    if pt1[2] >= 0.25 and pt2[2] >= 0.25:
                        cv2.line(
                            frame,
                            (int(pt1[0]), int(pt1[1])),
                            (int(pt2[0]), int(pt2[1])),
                            (0, 255, 255),
                            2,
                            cv2.LINE_AA,
                        )
                for pt in keypoints:
                    if pt[2] >= 0.25:
                        cv2.circle(frame, (int(pt[0]), int(pt[1])), 4, (0, 165, 255), -1)

            # Determine Bounding Box Color and Label
            if self.fall_alert:
                box_color: Tuple[int, int, int] = (0, 0, 255)  # Bright Red
                label: str = "🚨 CRITICAL: FALL DETECTED!"
            elif self.prolonged_alert:
                box_color = (0, 140, 255)  # Warning Amber
                label = f"⚠️ PROLONGED {self.current_posture}"
            elif posture == "STANDING":
                box_color = (0, 200, 0)  # Green
                label = "STANDING"
            elif posture == "SITTING":
                box_color = (255, 180, 0)  # Cyan/Blue
                label = "SITTING"
            elif posture == "LYING DOWN":
                box_color = (0, 165, 255)  # Orange
                label = "LYING DOWN"
            else:
                box_color = (128, 128, 128)
                label = posture

            x1, y1, x2, y2 = box
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 3)
            # Label banner
            (text_w, text_h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
            cv2.rectangle(
                frame,
                (x1, max(0, y1 - text_h - 10)),
                (x1 + text_w + 10, y1),
                box_color,
                -1,
            )
            cv2.putText(
                frame,
                label,
                (x1 + 5, y1 - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            # Save latest telemetry
            self.latest_telemetry = {
                "detected": True,
                "posture": posture,
                "posture_duration": round(self.posture_duration, 1),
                "prolonged_alert": self.prolonged_alert,
                "fall_alert": self.fall_alert,
                "fall_state": self.fall_state,
                "torso_angle": round(torso_angle, 1) if torso_angle is not None else None,
                "downward_velocity": round(downward_vel, 2),
                "aspect_ratio": round(aspect_ratio, 2),
            }
        else:
            self.reset()
            downward_vel = 0.0
            self.latest_telemetry = {
                "detected": False,
                "posture": "NO PERSON",
                "posture_duration": 0.0,
                "prolonged_alert": False,
                "fall_alert": False,
                "fall_state": "NORMAL",
                "torso_angle": None,
                "downward_velocity": 0.0,
                "aspect_ratio": 0.0,
            }

        # --- Draw Clinical HUD Overlay ---
        overlay = frame.copy()
        hud_w = min(w - 1, 460)
        hud_h = min(h - 1, 185)
        cv2.rectangle(overlay, (0, 0), (hud_w, hud_h), (18, 24, 38), -1)
        frame = cv2.addWeighted(overlay, 0.85, frame, 0.15, 0)

        # Fall status styling
        if self.fall_alert:
            fall_text = "🚨 FALL CONFIRMED!"
            fall_color = (0, 0, 255)
        elif self.fall_state == "DESCENT_DETECTED":
            fall_text = "⚠️ RAPID DESCENT DETECTED"
            fall_color = (0, 165, 255)
        else:
            fall_text = "NORMAL"
            fall_color = (0, 220, 0)

        # Posture status styling
        if self.prolonged_alert:
            posture_status = f"{self.current_posture} (PROLONGED ALERT!)"
            posture_color = (0, 140, 255)
        else:
            posture_status = f"{self.current_posture} ({self.posture_duration:.1f}s / {PROLONGED_POSTURE_SECONDS:.0f}s)"
            posture_color = (255, 255, 255)

        angle_str = f"{torso_angle:.1f}°" if torso_angle is not None else "n/a"

        lines = [
            ("Fall Monitor:", fall_text, fall_color),
            ("Posture:", posture_status, posture_color),
            ("Torso Angle:", f"{angle_str} (Limit: {TORSO_UPRIGHT_ANGLE:.0f}° / {TORSO_LYING_ANGLE:.0f}°)", (200, 200, 200)),
            ("Vertical Velocity:", f"{downward_vel:+.2f} h/s (Trigger: {FALL_VELOCITY_THRESHOLD:.1f})", (200, 200, 200)),
            ("Aspect Ratio:", f"{aspect_ratio:.2f} (Floor: > {FALL_ASPECT_RATIO_THRESHOLD:.2f})", (200, 200, 200)),
        ]

        y_pos = 26
        for title, val, col in lines:
            cv2.putText(
                frame,
                title,
                (12, y_pos),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (160, 175, 200),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                val,
                (145, y_pos),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                col,
                1 if col == (200, 200, 200) else 2,
                cv2.LINE_AA,
            )
            y_pos += 26

        # Critical full-frame flashing banner if fall is confirmed
        if self.fall_alert:
            # Flashing bar across top of video
            flash = int(now * 4) % 2 == 0
            banner_col = (0, 0, 255) if flash else (0, 50, 180)
            cv2.rectangle(frame, (0, 0), (w, 40), banner_col, -1)
            cv2.putText(
                frame,
                "EMERGENCY: FALL DETECTED - SUBJECT UNRESPONSIVE",
                (max(10, int(w / 2 - 230)), 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.62,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

        return frame

    def process_raw_bytes(self, raw_bytes: bytes) -> Optional[bytes]:
        """Decodes JPEG bytes, executes posture and fall detection, and re-encodes to JPEG bytes."""
        frame = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        annotated = self.process_frame(frame)
        ok, enc = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return None
        return bytes(enc.tobytes())
