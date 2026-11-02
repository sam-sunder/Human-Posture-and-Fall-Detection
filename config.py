from __future__ import annotations
import os

# --- Model & Performance Settings ---
MODEL_NAME: str = os.getenv("YOLO_MODEL", "yolo11n-pose.pt")
INFERENCE_SIZE: int = int(os.getenv("INFERENCE_SIZE", "384"))
MAX_INFERENCE_FPS: float = float(os.getenv("MAX_INFERENCE_FPS", "15.0"))
JPEG_QUALITY: int = int(os.getenv("JPEG_QUALITY", "75"))
MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.20"))

# --- Posture Classification & Prolonged Posture Settings ---
# Time (seconds) in unchanged posture before triggering a prolonged posture alert
PROLONGED_POSTURE_SECONDS: float = float(os.getenv("PROLONGED_POSTURE_SECONDS", "8.0"))
# Angle thresholds relative to vertical (degrees)
TORSO_UPRIGHT_ANGLE: float = float(os.getenv("TORSO_UPRIGHT_ANGLE", "35.0"))
TORSO_LYING_ANGLE: float = float(os.getenv("TORSO_LYING_ANGLE", "55.0"))
# Center shift ratio to detect posture shifts / active movement
MOVEMENT_THRESHOLD: float = float(os.getenv("MOVEMENT_THRESHOLD", "0.08"))

# --- Sudden Fall Detection Settings ---
# Downward velocity threshold (normalized body heights per second)
FALL_VELOCITY_THRESHOLD: float = float(os.getenv("FALL_VELOCITY_THRESHOLD", "1.1"))
# Rapid torso angle change rate (degrees per second)
FALL_ANGULAR_VELOCITY_THRESHOLD: float = float(os.getenv("FALL_ANGULAR_VELOCITY_THRESHOLD", "80.0"))
# Immobility/lying confirmation time on floor before latching fall alert (seconds)
FALL_CONFIRMATION_SECONDS: float = float(os.getenv("FALL_CONFIRMATION_SECONDS", "1.2"))
# Aspect ratio (width / height) threshold indicating horizontal posture on ground
FALL_ASPECT_RATIO_THRESHOLD: float = float(os.getenv("FALL_ASPECT_RATIO_THRESHOLD", "1.05"))
