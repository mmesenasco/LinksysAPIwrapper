"""Thin async JNAP client for Linksys Velop / MR9000 (HTTP, no cloud)."""
from __future__ import annotations

import base64
from typing import Any

import httpx

JNAP_BASE = "http://linksys.com/jnap/"


class JNAPError(RuntimeError):
    def __init__(self, action: str, result: str, detail: Any = None):
        super().__init__(f"{action} -> {result}: {detail}")
        self.action = action
        self.result = result
        self.detail = detail


class JNAPClient:
    """Minimal JNAP transport.

    Every action is a POST to /JNAP/ with the action URL in a header and a
    JSON body. Auth is Basic base64('admin:<password>') in X-JNAP-Authorization.
    Read actions mostly need no auth; writes always do.
    """

    def __init__(self, base_url: str, password: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/JNAP/"
        self._auth = "Basic " + base64.b64encode(
            f"admin:{password}".encode()
        ).decode()
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self, action: str, body: dict | None = None, auth: bool = True
    ) -> dict:
        """Call one JNAP action. `action` is relative, e.g. 'core/GetDeviceInfo'."""
        headers = {
            "Content-Type": "application/json",
            "X-JNAP-Action": JNAP_BASE + action,
        }
        if auth:
            headers["X-JNAP-Authorization"] = self._auth
        resp = await self._client.post(
            self.endpoint, headers=headers, json=body or {}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "OK":
            raise JNAPError(action, data.get("result", "?"), data.get("error"))
        return data.get("output", {})

    async def try_call(
        self, action: str, body: dict | None = None, auth: bool = True
    ) -> dict | None:
        """Like call() but returns None on any JNAP/transport error.

        Used for best-effort actions whose names vary by firmware.
        """
        try:
            return await self.call(action, body, auth)
        except (JNAPError, httpx.HTTPError):
            return None

    async def try_call_host(
        self, base_url: str, action: str, body: dict | None = None,
        auth: bool = True, timeout: float = 5.0,
    ) -> dict | None:
        """Best-effort call to a JNAP action on ANOTHER host (e.g. a mesh node),
        reusing this client's auth and HTTP pool. None on any error.

        Velop slave nodes each speak JNAP on their own IP and accept the same
        admin credentials as the master.
        """
        headers = {
            "Content-Type": "application/json",
            "X-JNAP-Action": JNAP_BASE + action,
        }
        if auth:
            headers["X-JNAP-Authorization"] = self._auth
        try:
            resp = await self._client.post(
                base_url.rstrip("/") + "/JNAP/", headers=headers,
                json=body or {}, timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("result") != "OK":
                return None
            return data.get("output", {})
        except httpx.HTTPError:
            return None
