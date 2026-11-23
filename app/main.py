"""FastAPI app: background JNAP poller + GUI + JSON API + optional MQTT."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .jnap import JNAPClient
from .mqtt_pub import MQTTPublisher
from .poller import State, poll


def _password() -> str:
    # Docker secret first, then env var.
    sf = os.getenv("ROUTER_PASSWORD_FILE", "/run/secrets/router_password")
    if Path(sf).exists():
        return Path(sf).read_text().strip()
    return os.getenv("ROUTER_PASSWORD", "")


ROUTER_URL = os.getenv("ROUTER_URL", "http://192.168.1.1")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "30"))
MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USERNAME")
MQTT_PASS = os.getenv("MQTT_PASSWORD")

_state = State()
_state.error = "no poll yet"


async def _loop(client: JNAPClient, pub: MQTTPublisher | None) -> None:
    global _state
    while True:
        _state = await poll(client)
        if pub:
            try:
                pub.publish(_state)
            except Exception as exc:  # noqa: BLE001
                print(f"[mqtt] publish failed: {exc}", flush=True)
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = JNAPClient(ROUTER_URL, _password())
    pub = None
    if MQTT_HOST:
        try:
            pub = MQTTPublisher(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS)
            pub.connect()
            print(f"[mqtt] connected to {MQTT_HOST}:{MQTT_PORT}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[mqtt] disabled, connect failed: {exc}", flush=True)
            pub = None
    task = asyncio.create_task(_loop(client, pub))
    try:
        yield
    finally:
        task.cancel()
        if pub:
            pub.close()
        await client.aclose()


app = FastAPI(title="JNAP Velop", lifespan=lifespan)

_INDEX = (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(_state.to_dict())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    code = 200 if _state.ok else 503
    return JSONResponse({"ok": _state.ok, "error": _state.error}, status_code=code)
