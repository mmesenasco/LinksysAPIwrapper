"""Per-client connection-health tracking over a rolling window.

Flags devices in trouble:
- flapping: many online<->offline transitions in the window
- roaming:  keeps switching parent node
- weak:     wireless client whose average signal stays low

In-memory only (a restart rebuilds it within the window), since this is about
recent behaviour, not durable state.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

WINDOW_S = 1800      # 30 minutes
FLAP_MIN = 4         # online<->offline transitions in window => flapping
ROAM_MIN = 3         # parent-node changes in window => roaming
WEAK_SIGNAL = 20     # avg SNR below this (wireless) => weak


class ClientHealth:
    def __init__(self, window_s: int = WINDOW_S):
        self.window_s = window_s
        # mac -> deque[(ts, online, node, signal, wireless)]
        self.obs: dict[str, deque] = defaultdict(deque)
        self.flapping: set[str] = set()
        self._new_flapping: list[tuple[str, str]] = []

    def update(self, clients: list[dict], now: float | None = None) -> None:
        now = now or time.time()
        cutoff = now - self.window_s
        for c in clients:
            mac = c.get("mac")
            if not mac:
                continue
            self.obs[mac].append((
                now, bool(c.get("online")), c.get("node") or "",
                c.get("signal"), c.get("connection") == "Wireless",
            ))
        # prune old samples and empty macs
        for mac in list(self.obs):
            dq = self.obs[mac]
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if not dq:
                del self.obs[mac]
        # recompute the flapping set, record newly-flapping devices
        self._new_flapping = []
        now_flap: set[str] = set()
        for mac, dq in self.obs.items():
            if self._metrics(dq)["status"] == "flapping":
                now_flap.add(mac)
                if mac not in self.flapping:
                    self._new_flapping.append((mac, dq[-1][2]))
        self.flapping = now_flap

    @staticmethod
    def _metrics(dq) -> dict:
        flaps = roams = 0
        prev_online = None
        prev_node = None
        sigs: list[float] = []
        for _ts, online, node, sig, wl in dq:
            if prev_online is not None and online != prev_online:
                flaps += 1
            prev_online = online
            if online:
                if prev_node is not None and node and node != prev_node:
                    roams += 1
                if node:
                    prev_node = node
                if wl and sig is not None:
                    sigs.append(sig)
        weak = bool(sigs) and (sum(sigs) / len(sigs) < WEAK_SIGNAL)
        if flaps >= FLAP_MIN:
            status = "flapping"
        elif roams >= ROAM_MIN:
            status = "roaming"
        elif weak:
            status = "weak"
        else:
            status = "stable"
        return {"flaps": flaps, "roams": roams, "weak": weak,
                "status": status, "samples": len(dq)}

    def metrics(self, mac: str | None) -> dict:
        dq = self.obs.get(mac or "")
        return self._metrics(dq) if dq else {
            "flaps": 0, "roams": 0, "weak": False, "status": "stable", "samples": 0}

    def take_new_flapping(self) -> list[tuple[str, str]]:
        out = self._new_flapping
        self._new_flapping = []
        return out
