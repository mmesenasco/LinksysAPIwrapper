"""Event detection + persistence: DFS channel changes, new devices, node up/down.

Events are appended to a JSONL file on a volume so they survive restarts.
Detection is diff-based: each poll's State is compared with the previous one.
Baselines (first seen channels / MACs / node states) are recorded silently so a
fresh start or a restart doesn't flag everything as "new".
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from .poller import DFS_CHANNELS, State

MAX_DAYS = 7          # how long to keep events in the file
VIEW_DEFAULT_H = 24   # default window served to the UI


def is_randomized_mac(mac: str) -> bool:
    """True for locally-administered (randomized) MACs — bit 0x02 of octet 1.

    Modern phones rotate these per-SSID, so they are noise for new-device
    alerts and are excluded from them.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


class EventStore:
    def __init__(self, data_dir: str = "/data"):
        self.dir = Path(data_dir)
        self.events_file = self.dir / "events.jsonl"
        self.macs_file = self.dir / "known_macs.json"
        self.events: deque[dict] = deque(maxlen=20000)
        self.known_macs: set[str] = set()
        self.last_channels: dict[str, tuple] = {}
        self.last_nodes: dict[str, bool] = {}
        self.last_security: dict | None = None
        self.persist = True
        self.seeded = False
        self._load()

    # ----- persistence ---------------------------------------------------
    def _load(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.events_file.exists():
                cutoff = time.time() - MAX_DAYS * 86400
                kept = []
                for line in self.events_file.read_text().splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("ts", 0) >= cutoff:
                        kept.append(e)
                self.events.extend(kept)
                # rewrite trimmed file
                self.events_file.write_text(
                    "\n".join(json.dumps(e) for e in kept) + ("\n" if kept else "")
                )
            if self.macs_file.exists():
                self.known_macs = set(json.loads(self.macs_file.read_text()))
                self.seeded = True
        except OSError:
            self.persist = False  # read-only / no volume: in-memory only

    def _append(self, event: dict) -> None:
        self.events.append(event)
        if not self.persist:
            return
        try:
            with self.events_file.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            self.persist = False

    def _persist_macs(self) -> None:
        if not self.persist:
            return
        try:
            self.macs_file.write_text(json.dumps(sorted(self.known_macs)))
        except OSError:
            self.persist = False

    # ----- detection -----------------------------------------------------
    def _add(self, etype: str, ts: float, **fields) -> None:
        self._append({
            "ts": ts,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
            "type": etype,
            **fields,
        })

    def update(self, state: State) -> None:
        if not state.ok:
            return
        now = state.updated

        # --- backhaul channel changes (DFS) ---
        cur_ch = {
            (b.node or b.node_ip): (b.channel, b.band, b.is_dfs)
            for b in state.backhaul if b.channel is not None
        }
        if self.last_channels:
            for key, (ch, band, dfs) in cur_ch.items():
                prev = self.last_channels.get(key)
                if prev and prev[0] != ch:
                    involved_dfs = bool(prev[2] or dfs)
                    self._add(
                        "dfs_change" if involved_dfs else "channel_change",
                        now, node=key, from_channel=prev[0], to_channel=ch,
                        band=band, dfs=involved_dfs,
                    )
        self.last_channels = cur_ch

        # --- new devices ---
        online = {
            c["mac"]: c for c in state.clients if c.get("online") and c.get("mac")
        }
        if self.seeded:
            for mac, c in online.items():
                if mac not in self.known_macs and not is_randomized_mac(mac):
                    self._add("new_device", now, mac=mac, name=c.get("name"),
                              ip=c.get("ip"), node=c.get("node"))
        self.known_macs |= set(online)
        self.seeded = True
        self._persist_macs()

        # --- node up/down ---
        cur_nodes = {n.name: n.online for n in state.nodes}
        if self.last_nodes:
            for name, on in cur_nodes.items():
                prev = self.last_nodes.get(name)
                if prev is not None and prev != on:
                    self._add("node_up" if on else "node_down", now, node=name)
        self.last_nodes = cur_nodes

        # --- security posture drift ---
        # Skip while the mesh is unstable (e.g. a reboot): snapshots are
        # unreliable then — the port-forward table briefly reads empty and
        # would produce a storm of false added/removed events.
        mesh_stable = bool(state.nodes) and all(n.online for n in state.nodes)
        self._security_drift(state.security or {}, now, mesh_stable)

    def _security_drift(self, sec: dict, now: float, mesh_stable: bool) -> None:
        if not mesh_stable:
            return  # don't compare or move the baseline during a reboot
        prev = self.last_security
        if prev is not None and sec:
            # risk transitions: a flag turning to the risky value
            risky_on = {
                "manage_remote": "sec_remote_mgmt_on",
                "dmz": "sec_dmz_on",
                "upnp": "sec_upnp_on",
                "wps": "sec_wps_on",
                "admin_default": "sec_admin_default",
            }
            for key, etype in risky_on.items():
                if not prev.get(key) and sec.get(key):
                    self._add(etype, now, setting=key)
            # protections turning off
            for key, etype in {"firewall_ipv4": "sec_firewall_off",
                               "firewall_ipv6": "sec_firewall_off"}.items():
                if prev.get(key) and sec.get(key) is False:
                    self._add(etype, now, setting=key)
            # port-forward rules added/removed — only ENABLED rules are
            # actually exposed; a disabled rule appearing/disappearing is not.
            # Only diff when both snapshots actually carried a reading.
            if "port_forwards" in prev and "port_forwards" in sec:
                def pf_set(s):
                    return {
                        (r.get("external_port"), r.get("protocol"),
                         r.get("internal_ip"))
                        for r in (s.get("port_forwards") or [])
                        if r.get("enabled")
                    }
                before, after = pf_set(prev), pf_set(sec)
                for r in after - before:
                    self._add("sec_portforward_added", now,
                              external_port=r[0], protocol=r[1], internal_ip=r[2])
                for r in before - after:
                    self._add("sec_portforward_removed", now,
                              external_port=r[0], protocol=r[1], internal_ip=r[2])
        if sec:
            # preserve a previous port-forward reading if this poll lacked one,
            # so a failed read doesn't later look like everything was re-added
            if "port_forwards" not in sec and prev and "port_forwards" in prev:
                sec = {**sec, "port_forwards": prev["port_forwards"]}
            self.last_security = sec

    # ----- query ---------------------------------------------------------
    def recent(self, hours: int = VIEW_DEFAULT_H) -> list[dict]:
        cutoff = time.time() - hours * 3600
        return sorted(
            (e for e in self.events if e.get("ts", 0) >= cutoff),
            key=lambda e: e["ts"], reverse=True,
        )
