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

    # ----- query ---------------------------------------------------------
    def recent(self, hours: int = VIEW_DEFAULT_H) -> list[dict]:
        cutoff = time.time() - hours * 3600
        return sorted(
            (e for e in self.events if e.get("ts", 0) >= cutoff),
            key=lambda e: e["ts"], reverse=True,
        )
