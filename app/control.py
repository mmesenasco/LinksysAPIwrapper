"""Write actions for JNAP: DHCP reservations and node reboot.

All of this is gated behind ALLOW_WRITES in main.py. DHCP changes use a strict
read-modify-write: we fetch the ENTIRE LAN settings object, mutate only the
reservations list, and send the whole object back. Sending a partial object to
SetLANSettings would wipe the DHCP range, gateway and the other reservations.
"""
from __future__ import annotations

import re

from .jnap import JNAPClient

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


class ValidationError(ValueError):
    pass


def _norm_mac(mac: str) -> str:
    if not MAC_RE.match(mac):
        raise ValidationError(f"MAC non valido: {mac!r} (atteso AA:BB:CC:DD:EE:FF)")
    return mac.upper()


def _check_ip(ip: str) -> str:
    if not IP_RE.match(ip) or any(int(o) > 255 for o in ip.split(".")):
        raise ValidationError(f"IP non valido: {ip!r}")
    return ip


async def get_lan_settings(client: JNAPClient) -> dict:
    return await client.call("router/GetLANSettings")


async def list_reservations(client: JNAPClient) -> list[dict]:
    s = await get_lan_settings(client)
    return (s.get("dhcpSettings") or {}).get("reservations") or []


async def _save(client: JNAPClient, settings: dict) -> None:
    # SetLANSettings takes the same shape GetLANSettings returns.
    await client.call("router/SetLANSettings", settings)


async def add_reservation(
    client: JNAPClient, mac: str, ip: str, description: str
) -> dict:
    """Add or update (by MAC) a DHCP reservation. Returns the new list."""
    mac = _norm_mac(mac)
    ip = _check_ip(ip)
    if not description.strip():
        raise ValidationError("description obbligatoria")

    settings = await get_lan_settings(client)
    dhcp = settings.get("dhcpSettings") or {}
    res = dhcp.get("reservations") or []

    # reject IP already reserved for a different MAC
    for r in res:
        if r.get("ipAddress") == ip and r.get("macAddress", "").upper() != mac:
            raise ValidationError(
                f"{ip} è già prenotato per {r.get('macAddress')}"
            )

    # update in place if the MAC exists, else append
    res = [r for r in res if r.get("macAddress", "").upper() != mac]
    res.append({"macAddress": mac, "ipAddress": ip, "description": description.strip()})
    dhcp["reservations"] = res
    settings["dhcpSettings"] = dhcp

    await _save(client, settings)
    return {"reservations": res}


async def delete_reservation(client: JNAPClient, mac: str) -> dict:
    """Remove the reservation matching MAC. Returns the new list."""
    mac = _norm_mac(mac)
    settings = await get_lan_settings(client)
    dhcp = settings.get("dhcpSettings") or {}
    res = dhcp.get("reservations") or []
    before = len(res)
    res = [r for r in res if r.get("macAddress", "").upper() != mac]
    if len(res) == before:
        raise ValidationError(f"nessuna prenotazione per {mac}")
    dhcp["reservations"] = res
    settings["dhcpSettings"] = dhcp
    await _save(client, settings)
    return {"reservations": res}


async def reboot_target(base_url: str, password: str, timeout: float = 10.0) -> None:
    """Reboot a specific device by its own IP (master or child node).

    Velop child nodes each speak JNAP on their own IP and accept the same admin
    password as the master.
    """
    node = JNAPClient(base_url, password, timeout=timeout)
    try:
        await node.call("core/Reboot")
    finally:
        await node.aclose()
