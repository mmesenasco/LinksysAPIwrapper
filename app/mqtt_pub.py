"""Publish state to Home Assistant via MQTT discovery (optional)."""
from __future__ import annotations

import json
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # paho optional until MQTT is enabled
    mqtt = None  # type: ignore

from .poller import State

DISC_PREFIX = "homeassistant"
NODE_ID = "jnap_velop"
STATE_PREFIX = "jnap_velop"


class MQTTPublisher:
    def __init__(self, host: str, port: int = 1883,
                 username: str | None = None, password: str | None = None):
        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed but MQTT_HOST is set")
        self.host, self.port = host, port
        self._c = mqtt.Client(client_id="jnap-velop", clean_session=True)
        if username:
            self._c.username_pw_set(username, password or "")
        self._c.will_set(f"{STATE_PREFIX}/availability", "offline", retain=True)
        self._discovery_sent: set[str] = set()

    def connect(self) -> None:
        self._c.connect(self.host, self.port, keepalive=60)
        self._c.loop_start()
        self._c.publish(f"{STATE_PREFIX}/availability", "online", retain=True)

    def close(self) -> None:
        self._c.publish(f"{STATE_PREFIX}/availability", "offline", retain=True)
        self._c.loop_stop()
        self._c.disconnect()

    def _device(self, node_name: str, serial: str, model: str) -> dict[str, Any]:
        return {
            "identifiers": [f"velop_{serial or node_name}"],
            "name": f"Velop {node_name}",
            "manufacturer": "Linksys",
            "model": model,
            "via_device": "velop_mesh",
        }

    def _publish_sensor(self, uid: str, name: str, state_topic: str,
                        device: dict, unit: str | None = None,
                        dev_class: str | None = None, icon: str | None = None,
                        value: Any = None) -> None:
        if uid not in self._discovery_sent:
            cfg: dict[str, Any] = {
                "name": name,
                "unique_id": uid,
                "state_topic": state_topic,
                "availability_topic": f"{STATE_PREFIX}/availability",
                "device": device,
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            if dev_class:
                cfg["device_class"] = dev_class
            if icon:
                cfg["icon"] = icon
            self._c.publish(
                f"{DISC_PREFIX}/sensor/{NODE_ID}/{uid}/config",
                json.dumps(cfg), retain=True,
            )
            self._discovery_sent.add(uid)
        self._c.publish(state_topic, str(value), retain=True)

    def publish(self, st: State) -> None:
        # Per-node backhaul sensors
        bh_by_ip = {b.node_ip: b for b in st.backhaul}
        for node in st.nodes:
            dev = self._device(node.name, node.serial, node.model)
            safe = (node.serial or node.name).replace(" ", "_")
            # online
            self._publish_sensor(
                f"velop_{safe}_online", "Online",
                f"{STATE_PREFIX}/{safe}/online", dev,
                icon="mdi:access-point", value="ON" if node.online else "OFF",
            )
            b = bh_by_ip.get(node.ip)
            if b:
                self._publish_sensor(
                    f"velop_{safe}_speed", "Backhaul speed",
                    f"{STATE_PREFIX}/{safe}/speed", dev,
                    unit="Mbit/s", icon="mdi:speedometer", value=b.speed_mbps,
                )
                self._publish_sensor(
                    f"velop_{safe}_rssi", "Backhaul RSSI",
                    f"{STATE_PREFIX}/{safe}/rssi", dev,
                    unit="dBm", dev_class="signal_strength",
                    value=b.rssi_station,
                )
                self._publish_sensor(
                    f"velop_{safe}_channel", "Backhaul channel",
                    f"{STATE_PREFIX}/{safe}/channel", dev,
                    icon="mdi:wifi", value=b.channel,
                )
                self._publish_sensor(
                    f"velop_{safe}_dfs", "On DFS channel",
                    f"{STATE_PREFIX}/{safe}/dfs", dev,
                    icon="mdi:radar", value="ON" if b.is_dfs else "OFF",
                )
        # Mesh-wide sensors
        mesh_dev = {
            "identifiers": ["velop_mesh"],
            "name": "Velop mesh",
            "manufacturer": "Linksys",
            "model": st.router.get("model", "MR9000"),
        }
        self._publish_sensor(
            "velop_clients_total", "Clients online",
            f"{STATE_PREFIX}/clients_total", mesh_dev,
            icon="mdi:devices", value=st.clients_total,
        )
        self._publish_sensor(
            "velop_mesh_ok", "Poll OK",
            f"{STATE_PREFIX}/ok", mesh_dev,
            icon="mdi:check-network", value="ON" if st.ok else "OFF",
        )
