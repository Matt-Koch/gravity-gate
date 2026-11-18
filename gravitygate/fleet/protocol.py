"""Discovery: small JSON beacons broadcast over UDP on the local network.

* Every machine's agent sends   {"t": "agent", "machine": {...}, "roles": [...]}
* The brain sends               {"t": "brain", "id": "A3F2", "port": 8000}
  Agents take the brain's address from the packet's source IP, so nothing has
  to be configured and the brain can be any box.

If a fleet key is set (same value baked into every image), beacons carry an
HMAC and unsigned or wrongly signed packets are ignored. That keeps a
stray laptop on the same Wi-Fi from posing as the brain.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from typing import Callable

PORT = 47263
MAGIC = "gravitygate/1"


def _sign(body: bytes, key: str) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()[:32]


def encode(msg: dict, key: str = "") -> bytes:
    body = json.dumps({"v": MAGIC, **msg}, separators=(",", ":"), sort_keys=True).encode()
    if key:
        return json.dumps({"s": _sign(body, key), "b": body.decode()}).encode()
    return body


def decode(data: bytes, key: str = "") -> dict | None:
    """Parsed message, or None if it isn't ours or the signature is wrong."""
    try:
        outer = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(outer, dict):
        return None
    if "s" in outer and "b" in outer:
        body = outer["b"].encode()
        if not key or not hmac.compare_digest(outer["s"], _sign(body, key)):
            return None
        try:
            msg = json.loads(body)
        except ValueError:
            return None
    else:
        if key:
            return None  # a fleet key is set: refuse unsigned beacons
        msg = outer
    return msg if isinstance(msg, dict) and msg.get("v") == MAGIC else None


def make_socket(bind: bool) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Brain and agent on the same box both listen on PORT.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if bind:
        s.bind(("", PORT))
    s.setblocking(False)
    return s


def send(msg: dict, key: str = "", addr: str = "255.255.255.255", port: int = PORT) -> None:
    s = make_socket(bind=False)
    try:
        s.sendto(encode(msg, key), (addr, port))
    except OSError:
        pass  # no network yet (cable out, Wi-Fi still joining): try again next tick
    finally:
        s.close()


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, key: str, handler: Callable[[dict, str], None]):
        self.key = key
        self.handler = handler

    def datagram_received(self, data: bytes, addr) -> None:
        msg = decode(data, self.key)
        if msg is not None:
            self.handler(msg, addr[0])


async def listen(handler: Callable[[dict, str], None], key: str = "", port: int = PORT):
    """Start receiving beacons; returns the transport (close it to stop)."""
    loop = asyncio.get_running_loop()
    sock = make_socket(bind=False)
    sock.bind(("", port))
    transport, _ = await loop.create_datagram_endpoint(lambda: _Receiver(key, handler), sock=sock)
    return transport
