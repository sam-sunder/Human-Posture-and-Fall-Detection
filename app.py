import asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from tracker import PostureTracker

app = FastAPI(title="Live Posture Monitor")
tracker = PostureTracker()
TEMPLATES_DIR = Path(__file__).parent / "templates"


class ConnectionManager:
    """Manages dashboard clients using an event-driven latest-frame model.
    Guarantees 0 buffered frames and 0 latency accumulation.
    """
    def __init__(self):
        self.events: set[asyncio.Event] = set()
        self.latest_frame: bytes | None = None
        self.frame_id: int = 0

    def register(self) -> asyncio.Event:
        event = asyncio.Event()
        self.events.add(event)
        return event

    def unregister(self, event: asyncio.Event):
        self.events.discard(event)

    def publish_frame(self, frame_bytes: bytes):
        self.latest_frame = frame_bytes
        self.frame_id += 1
        for event in list(self.events):
            event.set()


manager = ConnectionManager()


@app.get("/")
async def root():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/capture")
async def capture_page():
    return FileResponse(TEMPLATES_DIR / "capture.html")


@app.get("/dashboard")
async def dashboard_page():
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@app.websocket("/ws/capture")
async def capture_ws(ws: WebSocket):
    await ws.accept()
    tracker.reset()

    try:
        while True:
            # Receive frame from client
            data = await ws.receive_bytes()

            # Drain any queued frames to ensure we only process the latest frame
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_bytes(), timeout=0.001)
                except (asyncio.TimeoutError, TimeoutError):
                    break

            # Run inference & overlay off the event loop
            annotated_jpeg = await asyncio.to_thread(tracker.process_raw_bytes, data)
            if annotated_jpeg is not None:
                manager.publish_frame(annotated_jpeg)

            # Send ACK back so phone knows the server is ready for the next frame
            try:
                await ws.send_text("ack")
            except Exception:
                break

    except WebSocketDisconnect:
        pass
    finally:
        tracker.reset()


@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await ws.accept()
    event = manager.register()
    last_sent_id = -1

    try:
        while True:
            # Client sends 'ready' when connected and after each frame render
            msg = await ws.receive_text()
            if msg in ("ready", "next"):
                # If no new frame has arrived yet, wait for one
                if manager.frame_id == last_sent_id or manager.latest_frame is None:
                    event.clear()
                    try:
                        # Wait up to 2 seconds for a fresh frame
                        await asyncio.wait_for(event.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        # Timeout keepalive, continue loop to check client status
                        await ws.send_text("wait")
                        continue

                # Send the single freshest frame available
                if manager.latest_frame is not None and manager.frame_id != last_sent_id:
                    last_sent_id = manager.frame_id
                    await ws.send_bytes(manager.latest_frame)
    except WebSocketDisconnect:
        pass
    finally:
        manager.unregister(event)
