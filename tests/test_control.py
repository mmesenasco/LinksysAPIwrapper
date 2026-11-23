"""Tests for DHCP reservation read-modify-write (no router needed)."""
import asyncio

from app import control


class FakeClient:
    """Captures SetLANSettings so we can assert the full object is preserved."""

    def __init__(self, settings):
        self.settings = settings
        self.saved = None

    async def call(self, action, body=None, auth=True):
        if action == "router/GetLANSettings":
            return self.settings
        if action == "router/SetLANSettings":
            self.saved = body
            return {}
        raise AssertionError(f"unexpected {action}")


def base_settings():
    return {
        "ipAddress": "192.168.1.1",
        "networkPrefixLength": 24,
        "isDHCPEnabled": True,
        "dhcpSettings": {
            "firstClientIPAddress": "192.168.1.10",
            "lastClientIPAddress": "192.168.1.254",
            "leaseMinutes": 1440,
            "reservations": [
                {"macAddress": "AA:AA:AA:AA:AA:AA", "ipAddress": "192.168.1.50",
                 "description": "nas"},
            ],
        },
    }


def run(coro):
    return asyncio.run(coro)


def test_add_preserves_other_fields_and_existing_reservations():
    c = FakeClient(base_settings())
    run(control.add_reservation(c, "bb:bb:bb:bb:bb:bb", "192.168.1.162", "inverter"))
    saved = c.saved
    # range / gateway kept
    assert saved["dhcpSettings"]["firstClientIPAddress"] == "192.168.1.10"
    assert saved["ipAddress"] == "192.168.1.1"
    macs = {r["macAddress"] for r in saved["dhcpSettings"]["reservations"]}
    assert macs == {"AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"}  # old kept, new upper-cased


def test_add_same_mac_updates_in_place():
    c = FakeClient(base_settings())
    run(control.add_reservation(c, "aa:aa:aa:aa:aa:aa", "192.168.1.99", "nas-new"))
    res = c.saved["dhcpSettings"]["reservations"]
    assert len(res) == 1
    assert res[0]["ipAddress"] == "192.168.1.99"
    assert res[0]["description"] == "nas-new"


def test_ip_conflict_rejected():
    c = FakeClient(base_settings())
    try:
        run(control.add_reservation(c, "cc:cc:cc:cc:cc:cc", "192.168.1.50", "x"))
    except control.ValidationError:
        assert c.saved is None  # nothing written
    else:
        raise AssertionError("expected ValidationError on IP conflict")


def test_bad_mac_rejected():
    c = FakeClient(base_settings())
    try:
        run(control.add_reservation(c, "not-a-mac", "192.168.1.60", "x"))
    except control.ValidationError:
        assert c.saved is None
    else:
        raise AssertionError("expected ValidationError on bad MAC")


def test_delete_missing_rejected():
    c = FakeClient(base_settings())
    try:
        run(control.delete_reservation(c, "ff:ff:ff:ff:ff:ff"))
    except control.ValidationError:
        assert c.saved is None
    else:
        raise AssertionError("expected ValidationError deleting missing MAC")


def test_delete_removes_and_saves():
    c = FakeClient(base_settings())
    run(control.delete_reservation(c, "aa:aa:aa:aa:aa:aa"))
    assert c.saved["dhcpSettings"]["reservations"] == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
