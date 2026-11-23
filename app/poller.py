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
    clients: list[dict] = field(default_factory=list)
    leases: list[dict] = field(default_factory=list)

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

        # Pass 1: nodes, so clients can resolve their parent node name.
        id_to_name: dict[str, str] = {}
        ip_to_name: dict[str, str] = {}
        for dev in devices:
            if dev.get("nodeType") is None:
                continue
            conns = dev.get("connections") or [{}]
            ip = conns[0].get("ipAddress", "")
            props = _props(dev)
            name = props.get("userDeviceLocation") or dev.get("friendlyName", "?")
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

        # Optional live wireless metrics keyed by MAC (best effort).
        netmap: dict[str, dict] = {}
        nc = await client.try_call("networkconnections/GetNetworkConnections")
        if nc:
            for c in nc.get("connections", []) or []:
                m = (c.get("macAddress") or "").upper()
                if m:
                    netmap[m] = c

        # Pass 2: clients (everything that is not a node).
        per_node: dict[str, int] = {}
        for dev in devices:
            if dev.get("nodeType") is not None:
                continue
            conns = dev.get("connections") or []
            online = bool(conns)
            conn = conns[0] if conns else {}
            props = _props(dev)
            model = dev.get("model") or {}
            unit = dev.get("unit") or {}
            mac = (conn.get("macAddress") or "").upper()
            nd = netmap.get(mac)
            # GetNetworkConnections nests wireless info under "wireless";
            # its absence (for an entry that exists) means a wired client.
            wl = (nd or {}).get("wireless") or {}
            is_wireless = bool(wl)
            band = wl.get("band") or ""
            signal = wl.get("signalDecibels")   # SNR-like, higher is better
            guest = bool(wl.get("isGuest")) if is_wireless else False
            rate = (nd or {}).get("negotiatedMbps", conn.get("negotiatedMbps"))
            if is_wireless:
                connection = "Wireless"
            elif nd is not None:
                connection = "Wired"
            else:
                connection = ""   # online but unknown -> show "—", never guess
            parent = conn.get("parentDeviceID")
            node_name = id_to_name.get(parent, "") if parent else ""
            st.clients.append({
                "name": props.get("userDeviceName") or dev.get("friendlyName") or "?",
                "mac": mac,
                "ip": conn.get("ipAddress", ""),
                "ipv6": conn.get("ipv6Address", ""),
                "node": node_name,
                "type": props.get("userDeviceType") or model.get("deviceType", ""),
                "manufacturer": props.get("userDeviceManufacturer")
                or model.get("manufacturer", ""),
                "model": props.get("userDeviceModelNumber")
                or model.get("modelNumber", ""),
                "os": props.get("userDeviceOS") or unit.get("operatingSystem", ""),
                "connection": connection,
                "band": band,
                "signal": signal,
                "guest": guest,
                "rate_mbps": rate,
                "online": online,
            })
            if online:
                key = node_name or "?"
                per_node[key] = per_node.get(key, 0) + 1

        st.clients_total = sum(1 for c in st.clients if c["online"])
        st.clients_per_node = per_node

        # DHCP leases (ARP-like view): merge leases + reservations + live status.
        reserved: set[str] = set()
        lan = await client.try_call("router/GetLANSettings")
        if lan:
            for r in (lan.get("dhcpSettings") or {}).get("reservations", []) or []:
                reserved.add((r.get("macAddress") or "").upper())
        by_mac = {c["mac"]: c for c in st.clients}
        ld = await client.try_call("router/GetDHCPClientLeases")
        if ld:
            for lease in ld.get("leases", []) or []:
                mac = (lease.get("macAddress") or "").upper()
                c = by_mac.get(mac)
                st.leases.append({
                    "host": lease.get("hostName") or "",
                    "ip": lease.get("ipAddress") or "",
                    "mac": mac,
                    "expiration": lease.get("expiration") or "",
                    "type": "Riservato" if mac in reserved else "Dinamico",
                    "online": bool(c and c.get("online")),
                    "node": (c or {}).get("node", ""),
                })

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
