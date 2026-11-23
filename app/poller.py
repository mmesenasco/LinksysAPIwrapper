"""Build a single state snapshot from JNAP, used by GUI, API and MQTT."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .jnap import JNAPClient

# 5GHz DFS channels (EU/FCC): a backhaul here can drop on radar detection.
DFS_CHANNELS = set(range(52, 65)) | set(range(100, 145))


@dataclass
class Node:
    name: str
    device_id: str
    model: str
    location: str
    ip: str
    serial: str
    firmware: str
    role: str  # Master / Slave
    online: bool = True


@dataclass
class Backhaul:
    node: str          # friendly location of the child node
    node_ip: str
    parent_ip: str
    connection_type: str  # Wired / Wireless
    band: str | None = None
    channel: int | None = None
    rssi_station: int | None = None
    rssi_ap: int | None = None
    speed_mbps: float | None = None
    is_dfs: bool = False


@dataclass
class State:
    ok: bool = False
    error: str | None = None
    updated: float = field(default_factory=time.time)
    poll_seconds: float = 0.0
    router: dict[str, Any] = field(default_factory=dict)
    wan: dict[str, Any] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)
    backhaul: list[Backhaul] = field(default_factory=list)
    clients_total: int = 0
    clients_per_node: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["updated_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(self.updated)
        )
        return d


def _props(dev: dict) -> dict:
    return {p["name"]: p.get("value") for p in dev.get("properties", []) or []}


async def poll(client: JNAPClient) -> State:
    start = time.time()
    st = State()
    try:
        info = await client.call("core/GetDeviceInfo", auth=False)
        st.router = {
            "model": info.get("modelNumber"),
            "firmware": info.get("firmwareVersion"),
            "firmware_date": info.get("firmwareDate"),
            "serial": info.get("serialNumber"),
        }

        devices = (
            await client.call("devicelist/GetDevices3", {"sinceRevision": 0})
        ).get("devices", [])

        id_to_name: dict[str, str] = {}
        ip_to_name: dict[str, str] = {}
        online_clients = 0
        per_node: dict[str, int] = {}

        for dev in devices:
            if dev.get("nodeType") is not None:
                conns = dev.get("connections") or [{}]
                ip = conns[0].get("ipAddress", "")
                props = _props(dev)
                name = props.get("userDeviceLocation") or dev.get(
                    "friendlyName", "?"
                )
                node = Node(
                    name=name,
                    device_id=dev.get("deviceID", ""),
                    model=dev.get("model", {}).get("modelNumber", "?"),
                    location=name,
                    ip=ip,
                    serial=dev.get("unit", {}).get("serialNumber", ""),
                    firmware=dev.get("unit", {}).get("firmwareVersion", ""),
                    role=dev.get("nodeType", ""),
                )
                st.nodes.append(node)
                id_to_name[node.device_id] = name
                if ip:
                    ip_to_name[ip] = name
            else:
                conns = dev.get("connections") or []
                if conns:  # has an active connection = online client
                    online_clients += 1
                    parent = conns[0].get("parentDeviceID")
                    if parent:
                        pname = parent  # resolved below once nodes known
                        per_node[pname] = per_node.get(pname, 0) + 1

        st.clients_total = online_clients
        # resolve parent IDs to node names where possible
        st.clients_per_node = {
            id_to_name.get(pid, pid): n for pid, n in per_node.items()
        }

        # Backhaul (parent + per-link radio metrics)
        bh = await client.try_call("nodes/diagnostics/GetBackhaulInfo")
        if bh:
            for b in bh.get("backhaulDevices", []):
                wc = b.get("wirelessConnectionInfo") or {}
                ch = wc.get("channel")
                st.backhaul.append(
                    Backhaul(
                        node=ip_to_name.get(b.get("ipAddress", ""), b.get("ipAddress", "")),
                        node_ip=b.get("ipAddress", ""),
                        parent_ip=b.get("parentIPAddress", ""),
                        connection_type=b.get("connectionType", ""),
                        band=wc.get("radioID"),
                        channel=ch,
                        rssi_station=wc.get("stationRSSI"),
                        rssi_ap=wc.get("apRSSI"),
                        speed_mbps=(
                            float(b["speedMbps"]) if b.get("speedMbps") else None
                        ),
                        is_dfs=ch in DFS_CHANNELS if isinstance(ch, int) else False,
                    )
                )

        # mark online nodes: master + any child seen in backhaul
        seen_ips = {b.node_ip for b in st.backhaul}
        for node in st.nodes:
            node.online = node.role == "Master" or node.ip in seen_ips

        # WAN status (best effort; action name varies by firmware)
        wan = await client.try_call("router/GetWANStatus")
        if wan:
            st.wan = {
                "status": wan.get("wanStatus"),
                "type": (wan.get("wanConnection") or {}).get("wanType"),
                "ip": (wan.get("wanConnection") or {}).get("ipAddress"),
            }

        st.ok = True
    except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
        st.ok = False
        st.error = f"{type(exc).__name__}: {exc}"

    st.poll_seconds = round(time.time() - start, 3)
    st.updated = time.time()
    return st
