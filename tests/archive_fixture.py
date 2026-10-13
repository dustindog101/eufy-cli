"""Tiny synthetic NSKeyedArchive builder used only by tests."""

from __future__ import annotations

import plistlib
from typing import Any


class Builder:
    def __init__(self) -> None:
        self.objects: list[Any] = ["$null"]
        self.classes: dict[str, plistlib.UID] = {}

    def _class(self, name: str) -> plistlib.UID:
        if name not in self.classes:
            index = len(self.objects)
            self.objects.append({"$classname": name, "$classes": [name, "NSObject"]})
            self.classes[name] = plistlib.UID(index)
        return self.classes[name]

    def _append(self, value: Any) -> plistlib.UID:
        index = len(self.objects)
        self.objects.append(value)
        return plistlib.UID(index)

    def string(self, value: str) -> plistlib.UID:
        return self._append({"NS.string": value, "$class": self._class("NSString")})

    def array(self, values: list[Any]) -> plistlib.UID:
        encoded = [value if isinstance(value, plistlib.UID) else self.string(value) if isinstance(value, str) else value for value in values]
        return self._append({"NS.objects": encoded, "$class": self._class("NSArray")})

    def dictionary(self, values: dict[str, Any]) -> plistlib.UID:
        keys = [self.string(key) for key in values]
        encoded = []
        for value in values.values():
            if isinstance(value, str):
                encoded.append(self.string(value))
            elif isinstance(value, list):
                encoded.append(self.array(value))
            else:
                encoded.append(value)
        return self._append({"NS.keys": keys, "NS.objects": encoded, "$class": self._class("NSDictionary")})

    def custom(self, name: str, values: dict[str, Any]) -> plistlib.UID:
        encoded: dict[str, Any] = {"$class": self._class(name)}
        for key, value in values.items():
            if isinstance(value, str):
                encoded[key] = self.string(value)
            elif isinstance(value, list):
                encoded[key] = self.array(value)
            elif isinstance(value, dict):
                encoded[key] = self.custom(value.pop("__class__", "NSDictionary"), value) if "__class__" in value else self.dictionary(value)
            else:
                encoded[key] = value
        return self._append(encoded)

    def plist(self, root: plistlib.UID) -> bytes:
        return plistlib.dumps(
            {"$archiver": "NSKeyedArchiver", "$version": 100000, "$objects": self.objects, "$top": {"root": root}},
            fmt=plistlib.FMT_BINARY,
        )


def device_archive(*, richer: bool = True, role: int | str = 1) -> bytes:
    b = Builder()
    member = b.custom("BCFamilyMember", {"_member_type": role, "_admin_user_id": "private-admin", "_member_user_id": "private-guest"})
    doorbell_fields: dict[str, Any] = {
        "device_sn": "synthetic-doorbell-serial",
        "device_name": "Front Door",
        "device_model": "T8210",
        "device_type": 21,
        "device_channel": 0,
        "parent_sn": "synthetic-station-serial",
        "member": member,
        "statusCache": b.dictionary({"motionDetection": True, "batteryLevel": 82}),
    }
    if richer:
        doorbell_fields.update({
            "main_sw_version": "2.2.4.6",
            "params": b.array([
                b.custom("BCDeviceParam", {"_param_type": 1011, "_param_value": True}),
                b.custom("BCDeviceParam", {"_param_type": 1101, "_param_value": 82}),
            ]),
            "category": 21,
        })
    doorbell = b.custom("ECCamera", doorbell_fields)
    station = b.custom("ECHomebase2", {
        "device_sn": "synthetic-station-serial",
        "device_model": "T8010",
        "device_type": 10002,
        "main_sw_version": "3.4.2.6h",
        "p2p_did": "synthetic-local-did",
        "ip_addr": "10.254.50.25",
        "dsk_key": "synthetic-dsk-key-never-display",
        "dsk_expiration": 2_000_000_000,
        "app_conn": "synthetic-cloud-address-never-display",
        "member": member,
        "devices": b.array([doorbell]),
    })
    root = b.custom("BCDevicesMan", {"devices": b.array([station, doorbell])})
    return b.plist(root)


def userinfo_archive(role: str = "guest") -> bytes:
    b = Builder()
    root = b.custom("BCAccountInfo", {
        "privilege": role,
        "token": "synthetic-token-never-display",
        "email": "user@example.invalid.invalid",
    })
    return b.plist(root)
