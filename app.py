from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

import database
from config import MAX_INFERENCE_FPS
from tracker import PostureTracker

app = FastAPI(title="KineticGuard — Posture & Vitals Telemetry Portal")
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"

# Initialize SQLite database schema
database.init_db()


class PatientCreate(BaseModel):
    name: str
    age: int
    gender: str
    room_number: str
    client_id: str
    notes: Optional[str] = ""


class DeviceAnnounce(BaseModel):
    device_id: str
    client_id: str
    ip_address: Optional[str] = ""
    mac_address: Optional[str] = ""
    firmware_version: Optional[str] = "1.0.0"
    sensors: Optional[str] = "MAX30102,DS18B20"


class VitalsTelemetry(BaseModel):
    client_id: str
    heart_rate: Optional[float] = None
    spo2: Optional[float] = None
    temperature_c: Optional[float] = None
    temperature_f: Optional[float] = None
    finger_detected: Optional[bool] = True


def evaluate_vitals_status(
    heart_rate: Optional[float],
    spo2: Optional[float],
    temp_c: Optional[float],
    finger_detected: bool,
) -> str:
    if not finger_detected:
        return "NO_FINGER"
    alerts: List[str] = []
    if heart_rate is not None and heart_rate > 0:
        if heart_rate > 120.0:
            alerts.append("TACHYCARDIA")
        elif heart_rate < 50.0:
            alerts.append("BRADYCARDIA")
    if spo2 is not None and spo2 > 0 and spo2 < 92.0:
        alerts.append("HYPOXEMIA")
    if temp_c is not None and temp_c > 0:
        if temp_c > 38.0:
            alerts.append("FEVER")
        elif temp_c < 35.0:
            alerts.append("HYPOTHERMIA")
    return ", ".join(alerts) if alerts else "NORMAL"


class PatientSession:
    """Manages tracking, latest frame, vital signs, and connected dashboard clients."""
    def __init__(self, patient_id: int, patient_name: str, client_id: str) -> None:
        self.patient_id: int = patient_id
        self.patient_name: str = patient_name
        self.client_id: str = client_id
        self.tracker: PostureTracker = PostureTracker(
            patient_id=patient_id,
            patient_name=patient_name,
            on_event_log=database.log_posture_event,
        )
        self.events: Set[asyncio.Event] = set()
        self.latest_frame: Optional[bytes] = None
        self.frame_id: int = 0
        self.is_streaming: bool = False
        self.last_inference_time: float = 0.0

        # Real-time Vitals from ESP32
        self.latest_vitals: Optional[Dict[str, Any]] = None
        self.last_vitals_log_time: float = 0.0

    def register_dashboard(self) -> asyncio.Event:
        event = asyncio.Event()
        self.events.add(event)
        return event

    def unregister_dashboard(self, event: asyncio.Event) -> None:
        self.events.discard(event)

    def publish_frame(self, frame_bytes: bytes) -> None:
        self.latest_frame = frame_bytes
        self.frame_id += 1
        for event in list(self.events):
            event.set()

    def update_vitals(
        self,
        heart_rate: Optional[float],
        spo2: Optional[float],
        temperature_c: Optional[float],
        temperature_f: Optional[float],
        finger_detected: bool = True,
    ) -> Dict[str, Any]:
        if temperature_c is not None and temperature_f is None:
            temperature_f = (temperature_c * 9.0 / 5.0) + 32.0
        elif temperature_f is not None and temperature_c is None:
            temperature_c = (temperature_f - 32.0) * 5.0 / 9.0

        status = evaluate_vitals_status(heart_rate, spo2, temperature_c, finger_detected)
        now_epoch = time.time()

        self.latest_vitals = {
            "heart_rate": round(heart_rate, 1) if heart_rate is not None else None,
            "spo2": round(spo2, 1) if spo2 is not None else None,
            "temperature_c": round(temperature_c, 2) if temperature_c is not None else None,
            "temperature_f": round(temperature_f, 2) if temperature_f is not None else None,
            "finger_detected": finger_detected,
            "status": status,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }

        # Log periodically to SQLite every 4 seconds or on abnormal status
        if (now_epoch - self.last_vitals_log_time >= 4.0) or (status not in ("NORMAL", "NO_FINGER")):
            self.last_vitals_log_time = now_epoch
            database.log_vitals_record(
                patient_id=self.patient_id,
                client_id=self.client_id,
                heart_rate=heart_rate,
                spo2=spo2,
                temperature_c=temperature_c,
                temperature_f=temperature_f,
                finger_detected=finger_detected,
                status=status,
            )

        return self.latest_vitals


class MultiPatientManager:
    """Manages patient sessions associated by patient_id and client_id."""
    def __init__(self) -> None:
        self.sessions_by_id: Dict[int, PatientSession] = {}
        self.sessions_by_client_id: Dict[str, PatientSession] = {}

    def get_or_create_session(self, patient_id: int) -> PatientSession:
        if patient_id in self.sessions_by_id:
            return self.sessions_by_id[patient_id]

        patient = database.get_patient_by_id(patient_id)
        if not patient:
            raise ValueError(f"Patient with id {patient_id} not found in database.")

        session = PatientSession(
            patient_id=patient["id"],
            patient_name=patient["name"],
            client_id=patient["client_id"],
        )
        self.sessions_by_id[patient_id] = session
        self.sessions_by_client_id[patient["client_id"]] = session
        return session

    def get_session_by_client_id(self, client_id: str) -> Optional[PatientSession]:
        if client_id in self.sessions_by_client_id:
            return self.sessions_by_client_id[client_id]

        patient = database.get_patient_by_client_id(client_id)
        if not patient:
            return None

        return self.get_or_create_session(patient["id"])


manager = MultiPatientManager()


# --- HTML Page Endpoints ---

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@app.get("/capture")
async def capture_page() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "capture.html")


@app.get("/test")
async def test_page() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "test.html")


# --- REST API Endpoints ---

@app.get("/api/patients")
async def api_get_patients() -> List[Dict[str, Any]]:
    patients = database.get_all_patients()
    enriched: List[Dict[str, Any]] = []
    for p in patients:
        session = manager.sessions_by_id.get(p["id"])
        is_streaming = session.is_streaming if session else False
        telemetry = session.tracker.latest_telemetry if session else {}
        vitals = session.latest_vitals if (session and session.latest_vitals) else database.get_latest_patient_vitals(p["id"])

        enriched.append({
            **p,
            "is_streaming": is_streaming,
            "current_posture": telemetry.get("posture", "NO DATA"),
            "posture_duration": telemetry.get("posture_duration", 0.0),
            "fall_alert": telemetry.get("fall_alert", False),
            "prolonged_alert": telemetry.get("prolonged_alert", False),
            "fall_state": telemetry.get("fall_state", "NORMAL"),
            "vitals": vitals,
        })
    return enriched


@app.post("/api/patients")
async def api_create_patient(patient_data: PatientCreate) -> Dict[str, Any]:
    existing = database.get_patient_by_client_id(patient_data.client_id)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Client ID '{patient_data.client_id}' is already assigned to {existing['name']}.",
        )

    patient_id = database.create_patient(
        name=patient_data.name,
        age=patient_data.age,
        gender=patient_data.gender,
        room_number=patient_data.room_number,
        client_id=patient_data.client_id,
        notes=patient_data.notes or "",
    )
    new_patient = database.get_patient_by_id(patient_id)
    return {"status": "success", "patient": new_patient}


@app.get("/api/patients/{patient_id}")
async def api_get_patient(patient_id: int) -> Dict[str, Any]:
    patient = database.get_patient_by_id(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    session = manager.sessions_by_id.get(patient_id)
    telemetry = session.tracker.latest_telemetry if session else {}
    vitals = session.latest_vitals if (session and session.latest_vitals) else database.get_latest_patient_vitals(patient_id)

    return {
        **patient,
        "is_streaming": session.is_streaming if session else False,
        "telemetry": telemetry,
        "vitals": vitals,
    }


@app.get("/api/patients/{patient_id}/logs")
async def api_get_patient_logs(patient_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    return database.get_patient_logs(patient_id, limit=limit)


@app.get("/api/patients/{patient_id}/vitals")
async def api_get_patient_vitals(patient_id: int, limit: int = 40) -> List[Dict[str, Any]]:
    return database.get_patient_vitals_logs(patient_id, limit=limit)


@app.post("/api/patients/{patient_id}/reset_alert")
async def api_reset_alert(patient_id: int) -> Dict[str, str]:
    session = manager.sessions_by_id.get(patient_id)
    if session:
        session.tracker.reset()
    return {"status": "success", "message": "Alert reset successfully"}


# --- ESP32 Device Announcement & Ingestion Endpoints ---

@app.post("/api/devices/announce")
async def api_announce_device(announce: DeviceAnnounce) -> Dict[str, Any]:
    """ESP32 announces itself on boot and periodically."""
    device = database.register_or_update_device(
        device_id=announce.device_id,
        client_id=announce.client_id,
        ip_address=announce.ip_address or "",
        mac_address=announce.mac_address or "",
        firmware_version=announce.firmware_version or "1.0.0",
        sensors=announce.sensors or "MAX30102,DS18B20",
    )
    patient = database.get_patient_by_client_id(announce.client_id)
    return {
        "status": "success",
        "message": f"Device '{announce.device_id}' registered for client '{announce.client_id}'",
        "device": device,
        "assigned_patient": patient["name"] if patient else None,
    }


@app.get("/api/devices")
async def api_get_devices() -> List[Dict[str, Any]]:
    return database.get_all_sensor_devices()


@app.post("/api/vitals")
async def api_post_vitals(telemetry: VitalsTelemetry) -> Dict[str, Any]:
    """HTTP Ingestion endpoint for ESP32 MAX30102 and DS18B20 readings."""
    session = manager.get_session_by_client_id(telemetry.client_id)
    patient_id = session.patient_id if session else None

    if not patient_id:
        patient = database.get_patient_by_client_id(telemetry.client_id)
        if patient:
            session = manager.get_or_create_session(patient["id"])
            patient_id = patient["id"]
        else:
            # Auto-register an ad-hoc patient for this client_id
            patient_id = database.create_patient(
                name=f"Patient ({telemetry.client_id})",
                age=65,
                gender="Unspecified",
                room_number="Sensor Node",
                client_id=telemetry.client_id,
                notes="Auto-registered from ESP32 vitals announcement",
            )
            session = manager.get_or_create_session(patient_id)

    updated_vitals = session.update_vitals(
        heart_rate=telemetry.heart_rate,
        spo2=telemetry.spo2,
        temperature_c=telemetry.temperature_c,
        temperature_f=telemetry.temperature_f,
        finger_detected=telemetry.finger_detected if telemetry.finger_detected is not None else True,
    )

    return {
        "status": "success",
        "client_id": telemetry.client_id,
        "patient_id": patient_id,
        "vitals": updated_vitals,
    }


# --- WebSocket Endpoints ---

@app.websocket("/ws/capture/{client_id}")
@app.websocket("/ws/capture")
async def capture_ws(ws: WebSocket, client_id: Optional[str] = None) -> None:
    await ws.accept()

    if not client_id:
        client_id = ws.query_params.get("client_id")
    if not client_id:
        all_p = database.get_all_patients()
        if all_p:
            client_id = all_p[0]["client_id"]
        else:
            await ws.close(code=1008, reason="No registered patients")
            return

    session = manager.get_session_by_client_id(client_id)
    if not session:
        new_id = database.create_patient(
            name=f"Client {client_id}",
            age=70,
            gender="Unspecified",
            room_number="Auto-Registered",
            client_id=client_id,
            notes="Auto-registered from camera connection",
        )
        session = manager.get_or_create_session(new_id)

    session.is_streaming = True
    session.tracker.reset()

    try:
        while True:
            data = await ws.receive_bytes()

            # Drain backlogged frames
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_bytes(), timeout=0.001)
                except (asyncio.TimeoutError, TimeoutError):
                    break

            annotated_jpeg = await asyncio.to_thread(session.tracker.process_raw_bytes, data)
            if annotated_jpeg is not None:
                session.publish_frame(annotated_jpeg)

            try:
                await ws.send_text("ack")
            except Exception:
                break

    except WebSocketDisconnect:
        pass
    finally:
        session.is_streaming = False
        session.tracker.reset()


@app.websocket("/ws/vitals/{client_id}")
async def vitals_ws(ws: WebSocket, client_id: str) -> None:
    """Real-time bi-directional WebSocket streaming endpoint for ESP32 sensor telemetry."""
    await ws.accept()
    session = manager.get_session_by_client_id(client_id)

    if not session:
        patient = database.get_patient_by_client_id(client_id)
        if patient:
            session = manager.get_or_create_session(patient["id"])
        else:
            new_id = database.create_patient(
                name=f"Vitals Patient ({client_id})",
                age=65,
                gender="Unspecified",
                room_number="Sensor Bed",
                client_id=client_id,
                notes="Auto-registered from ESP32 vitals WebSocket",
            )
            session = manager.get_or_create_session(new_id)

    import json
    try:
        # Send initial welcome confirmation to ESP32
        await ws.send_text(json.dumps({"status": "connected", "client_id": client_id}))

        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
            except Exception:
                continue

            # Update session vitals
            session.update_vitals(
                heart_rate=data.get("heart_rate") or data.get("bpm"),
                spo2=data.get("spo2"),
                temperature_c=data.get("temperature_c") or data.get("temp_c"),
                temperature_f=data.get("temperature_f") or data.get("temp_f"),
                finger_detected=data.get("finger_detected", True),
            )

            # Acknowledge to ESP32
            await ws.send_text(json.dumps({"status": "ack"}))

    except WebSocketDisconnect:
        pass


@app.websocket("/ws/dashboard/{patient_id}")
@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket, patient_id: Optional[int] = None) -> None:
    await ws.accept()

    if patient_id is None:
        p_param = ws.query_params.get("patient_id")
        if p_param:
            patient_id = int(p_param)
        else:
            all_p = database.get_all_patients()
            if all_p:
                patient_id = all_p[0]["id"]
            else:
                await ws.close(code=1008, reason="No registered patients")
                return

    try:
        session = manager.get_or_create_session(patient_id)
    except ValueError:
        await ws.close(code=1008, reason="Invalid patient ID")
        return

    event = session.register_dashboard()
    last_sent_id: int = -1

    try:
        while True:
            msg = await ws.receive_text()
            if msg in ("ready", "next"):
                if session.frame_id == last_sent_id or session.latest_frame is None:
                    event.clear()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        await ws.send_text("wait")
                        continue

                if session.latest_frame is not None and session.frame_id != last_sent_id:
                    last_sent_id = session.frame_id
                    await ws.send_bytes(session.latest_frame)

    except WebSocketDisconnect:
        pass
    finally:
        session.unregister_dashboard(event)
