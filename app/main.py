"""FastAPI app: background JNAP poller + GUI + JSON API + optional MQTT."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import control
from .jnap import JNAPClient, JNAPError
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
ALLOW_WRITES = os.getenv("ALLOW_WRITES", "false").lower() in ("1", "true", "yes")

_state = State()
_state.error = "no poll yet"
_client: JNAPClient | None = None   # shared master client, set in lifespan


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
    global _client
    client = JNAPClient(ROUTER_URL, _password())
    _client = client
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

_STATIC = Path(__file__).parent / "static"
_INDEX = (_STATIC / "index.html").read_text()
_CLIENTS = (_STATIC / "clients.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX


@app.get("/clients", response_class=HTMLResponse)
async def clients_page() -> str:
    return _CLIENTS


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(_state.to_dict())


@app.get("/api/clients")
async def api_clients() -> JSONResponse:
    return JSONResponse({
        "updated_iso": _state.to_dict()["updated_iso"],
        "clients": _state.clients,
    })


@app.get("/healthz")
async def healthz() -> JSONResponse:
    code = 200 if _state.ok else 503
    return JSONResponse({"ok": _state.ok, "error": _state.error}, status_code=code)


# --------------------------------------------------------------------------
# Phase 2: write actions (DHCP reservations, node reboot).
# Disabled unless ALLOW_WRITES is set. Mutating calls run read-modify-write.
# --------------------------------------------------------------------------
class Reservation(BaseModel):
    mac: str
    ip: str
    description: str


def _require_writes() -> JNAPClient:
    if not ALLOW_WRITES:
        raise HTTPException(403, "scritture disabilitate (ALLOW_WRITES non attivo)")
    if _client is None:
        raise HTTPException(503, "client non pronto")
    return _client


@app.get("/api/config")
async def api_config() -> dict:
    return {"allow_writes": ALLOW_WRITES, "router_url": ROUTER_URL}


@app.get("/api/reservations")
async def get_reservations() -> dict:
    if _client is None:
        raise HTTPException(503, "client non pronto")
    try:
        return {"reservations": await control.list_reservations(_client)}
    except JNAPError as e:
        raise HTTPException(502, str(e))


@app.post("/api/reservations")
async def post_reservation(r: Reservation) -> dict:
    client = _require_writes()
    try:
        return await control.add_reservation(client, r.mac, r.ip, r.description)
    except control.ValidationError as e:
        raise HTTPException(400, str(e))
    except JNAPError as e:
        raise HTTPException(502, str(e))


@app.delete("/api/reservations/{mac}")
async def del_reservation(mac: str) -> dict:
    client = _require_writes()
    try:
        return await control.delete_reservation(client, mac)
    except control.ValidationError as e:
        raise HTTPException(400, str(e))
    except JNAPError as e:
        raise HTTPException(502, str(e))


@app.post("/api/nodes/{ip}/reboot")
async def reboot_node(ip: str) -> dict:
    _require_writes()
    try:
        control._check_ip(ip)
        await control.reboot_target(f"http://{ip}", _password())
        return {"ok": True, "rebooting": ip}
    except control.ValidationError as e:
        raise HTTPException(400, str(e))
    except JNAPError as e:
        raise HTTPException(502, str(e))
