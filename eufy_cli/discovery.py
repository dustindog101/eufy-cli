"""One-shot, read-only Eufy local UDP discovery."""

from __future__ import annotations

import ipaddress
import math
import socket
import time
from typing import Any


LOOKUP_PACKET = bytes.fromhex("f13000020000")
LOOKUP_PORT = 32108
REPLY_HEADER = bytes.fromhex("f141")
RFC1918_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _safe_lan_ipv4(address: ipaddress.IPv4Address) -> bool:
    return address.is_loopback or address.is_link_local or any(address in network for network in RFC1918_NETWORKS)


class DiscoveryError(ValueError):
    """Raised for unsafe or invalid discovery options."""


def parse_reply(data: bytes) -> str:
    """Return the reconstructed internal DID or reject a malformed packet."""
    if len(data) < 24 or data[:2] != REPLY_HEADER:
        raise DiscoveryError("invalid discovery reply header or length")
    declared = int.from_bytes(data[2:4], "big")
    if declared != len(data) - 4:
        raise DiscoveryError("discovery reply length field does not match datagram")
    first = data[4:12].rstrip(b"\0").decode("utf-8", errors="strict")
    middle = int.from_bytes(data[12:16], "big")
    last = data[16:24].rstrip(b"\0").decode("utf-8", errors="strict")
    if not first or not last or middle > 999_999:
        raise DiscoveryError("discovery reply fields are invalid")
    return f"{first}-{middle:06d}-{last}"


def _target(host: str | None) -> str:
    if host is None:
        return "255.255.255.255"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise DiscoveryError("--host must be a private IPv4 address") from exc
    if address.version != 4 or not _safe_lan_ipv4(address):
        raise DiscoveryError("--host must be a private IPv4 address")
    return str(address)


def discover(*, host: str | None = None, timeout: float = 3.0, cache: Any = None) -> dict[str, Any]:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0.1 or timeout > 10.0:
        raise DiscoveryError("--timeout must be between 0.1 and 10 seconds")
    destination = _target(host)
    responders: dict[tuple[str, int, str], dict[str, Any]] = {}
    malformed = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", 0))
            # Exactly one local lookup datagram. Discovery stops after replies;
            # it never sends CHECK_CAM or starts a P2P handshake.
            sock.sendto(LOOKUP_PACKET, (destination, LOOKUP_PORT))
            deadline = time.monotonic() + float(timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, address = sock.recvfrom(2048)
                except socket.timeout:
                    break
                try:
                    source_ip = ipaddress.ip_address(address[0])
                    if source_ip.version != 4 or not _safe_lan_ipv4(source_ip):
                        continue
                    did = parse_reply(data)
                except (ValueError, UnicodeDecodeError, DiscoveryError):
                    malformed += 1
                    continue
                port = int(address[1])
                if port < 1 or port > 65535:
                    malformed += 1
                    continue
                match = cache.match_station_did(did) if cache is not None else None
                key = (str(source_ip), port, did)
                responders[key] = {
                    "host": str(source_ip),
                    "port": port,
                    "device_alias": match.alias if match else None,
                    "model": match.model if match else None,
                    "matched_cached_device": match is not None,
                }
    except OSError as exc:
        raise DiscoveryError("local UDP discovery could not run") from exc
    return {
        "source": "local_udp_lookup",
        "packet_sent": "f13000020000",
        "destination": f"{destination}:{LOOKUP_PORT}",
        "handshake_started": False,
        "malformed_replies": malformed,
        "responders": list(responders.values()),
    }
