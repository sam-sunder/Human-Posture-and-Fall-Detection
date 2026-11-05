from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

import database
from config import MAX_INFERENCE_FPS
from tracker import PostureTracker

app = FastAPI(title="Clinical Posture & Fall Detection Portal")
TEMPLATES_DIR: Path = Path(__file__).parent / "templates"

# Initialize SQLite database on startup
database.init_db()


class PatientCreate(BaseModel):
    name: str
    age: int
    gender: str
    room_number: str
    client_id: str
    notes: Optional[str] = ""


class PatientSession:
    """Manages tracking, latest frame, and connected dashboard clients for a single patient."""
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


# --- REST API Endpoints ---

@app.get("/api/patients")
async def api_get_patients() -> List[Dict[str, Any]]:
    patients = database.get_all_patients()
    enriched = []
    for p in patients:
        session = manager.sessions_by_id.get(p["id"])
        is_streaming = session.is_streaming if session else False
        telemetry = session.tracker.latest_telemetry if session else {}
        enriched.append({
            **p,
            "is_streaming": is_streaming,
            "current_posture": telemetry.get("posture", "NO DATA"),
            "posture_duration": telemetry.get("posture_duration", 0.0),
            "fall_alert": telemetry.get("fall_alert", False),
            "prolonged_alert": telemetry.get("prolonged_alert", False),
            "fall_state": telemetry.get("fall_state", "NORMAL"),
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
    return {
        **patient,
        "is_streaming": session.is_streaming if session else False,
        "telemetry": telemetry,
    }


@app.get("/api/patients/{patient_id}/logs")
async def api_get_patient_logs(patient_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    return database.get_patient_logs(patient_id, limit=limit)


@app.post("/api/patients/{patient_id}/reset_alert")
async def api_reset_alert(patient_id: int) -> Dict[str, str]:
    session = manager.sessions_by_id.get(patient_id)
    if session:
        session.tracker.reset()
    return {"status": "success", "message": "Alert reset successfully"}


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

            # Drain any backlogged frames in WebSocket queue
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_bytes(), timeout=0.001)
                except (asyncio.TimeoutError, TimeoutError):
                    break

            # Execute model inference & HUD overlay in worker thread
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
