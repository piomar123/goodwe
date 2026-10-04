from __future__ import annotations

import asyncio
import time
from unittest import IsolatedAsyncioTestCase

from goodwe.exceptions import MaxRetriesException
from goodwe.protocol import ProtocolCommand, UdpInverterProtocol
from tests.test_protocol_concurrency import _FakeTransport

TIMEOUT = 0.2
REPLY_DELAY = 0.05


def _command(name: bytes, strict: bool = True) -> ProtocolCommand:
    if strict:
        return ProtocolCommand(name, lambda data, expected=b'response to ' + name: data == expected)
    # Like a Modbus RTU read: the response does not echo the register, so any
    # response of the right shape (here: any 'response to ...') passes.
    return ProtocolCommand(name, lambda data: data.startswith(b'response to '))


class _SlowNetwork:
    """Fake inverter with a per-send reply delay: delays[payload] is a list
    consumed one entry per send (None = no reply), REPLY_DELAY once empty.
    Replies to a transport that has been closed in the meantime are lost,
    like a datagram arriving at a closed UDP socket."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.delays: dict[bytes, list] = {}
        self.sent: list[tuple[float, bytes]] = []
        self.on_send = None

    def send(self, transport: _FakeTransport, payload: bytes) -> None:
        self.sent.append((time.monotonic(), payload))
        if self.on_send:
            self.on_send(payload)
        queue = self.delays.get(payload)
        delay = queue.pop(0) if queue else REPLY_DELAY
        if delay is not None:
            asyncio.get_running_loop().call_later(delay, self._reply, transport, payload)

    def _reply(self, transport: _FakeTransport, payload: bytes) -> None:
        if not transport.closed:
            self.protocol.datagram_received(b'response to ' + payload, ('127.0.0.1', 8899))

    def payloads(self) -> list[bytes]:
        return [p for _, p in self.sent]


class _FakeUdpProtocol(UdpInverterProtocol):
    def __init__(self, retries: int):
        super().__init__('127.0.0.1', 8899, 0xf7, TIMEOUT, retries)
        self.network = _SlowNetwork(self)

    async def _connect(self) -> None:
        if not self._transport or self._transport.is_closing():
            self._transport = _FakeTransport(self.network)


async def _send(protocol, name: bytes, strict: bool = True) -> bytes:
    return (await asyncio.wait_for(protocol.send_request(_command(name, strict)), 10)).result()


class TestUdpRetries(IsolatedAsyncioTestCase):

    async def test_late_reply_to_earlier_attempt_is_accepted(self):
        protocol = _FakeUdpProtocol(retries=1)
        # 1st attempt answered after its timeout, the retry is never answered
        protocol.network.delays = {b'A': [TIMEOUT * 1.3, None]}

        self.assertEqual(b'response to A', await _send(protocol, b'A'))
        self.assertEqual([b'A', b'A'], protocol.network.payloads())

    async def test_late_duplicate_reply_does_not_answer_next_request(self):
        protocol = _FakeUdpProtocol(retries=3)
        protocol.keep_alive = True
        # 1st attempt answered late - after the retry was already answered
        protocol.network.delays = {b'A': [TIMEOUT * 1.5]}

        self.assertEqual(b'response to A', await _send(protocol, b'A', strict=False))
        protocol.network.delays[b'B'] = [TIMEOUT * 0.6]
        self.assertEqual(b'response to B', await _send(protocol, b'B', strict=False))

    async def test_late_reply_to_failed_request_does_not_answer_next_request(self):
        protocol = _FakeUdpProtocol(retries=0)
        protocol.keep_alive = True
        protocol.network.delays = {b'A': [TIMEOUT * 1.5], b'B': [TIMEOUT * 0.6]}

        with self.assertRaises(MaxRetriesException):
            await _send(protocol, b'A', strict=False)
        self.assertEqual(b'response to B', await _send(protocol, b'B', strict=False))

    async def test_keep_alive_reuses_transport_without_retries(self):
        protocol = _FakeUdpProtocol(retries=3)
        protocol.keep_alive = True
        await _send(protocol, b'A')
        transport = protocol._transport
        await _send(protocol, b'B')
        self.assertIs(transport, protocol._transport)
        self.assertFalse(transport.closed)

    async def test_transport_closed_after_request_without_keep_alive(self):
        protocol = _FakeUdpProtocol(retries=1)
        protocol.network.delays = {b'A': [TIMEOUT * 1.3, None]}
        await _send(protocol, b'A')
        self.assertIsNone(protocol._transport)

    async def test_retry_timeouts_grow_up_to_a_cap(self):
        protocol = _FakeUdpProtocol(retries=4)
        protocol.network.delays = {b'A': [None] * 5}

        start = time.monotonic()
        with self.assertRaises(MaxRetriesException):
            await _send(protocol, b'A')
        sent = [t for t, _ in protocol.network.sent] + [time.monotonic()]
        waits = [b - a for a, b in zip(sent, sent[1:])]
        expected = [1, 1.5, 2.25, 3, 3]  # x TIMEOUT
        self.assertEqual(len(expected), len(waits))
        for wait, factor in zip(waits, expected):
            self.assertAlmostEqual(wait, factor * TIMEOUT, delta=0.08)
        self.assertLess(time.monotonic() - start, sum(expected) * TIMEOUT + 0.5)

    async def test_invalid_datagram_is_ignored(self):
        protocol = _FakeUdpProtocol(retries=3)
        protocol.network.on_send = lambda payload: asyncio.get_running_loop().call_soon(
            protocol.datagram_received, b'garbage', ('127.0.0.1', 8899))

        self.assertEqual(b'response to A', await _send(protocol, b'A'))
        self.assertEqual([b'A'], protocol.network.payloads())

    async def test_timeout_still_fires_after_invalid_datagram(self):
        protocol = _FakeUdpProtocol(retries=0)
        protocol.network.delays = {b'A': [None]}
        protocol.network.on_send = lambda payload: asyncio.get_running_loop().call_soon(
            protocol.datagram_received, b'garbage', ('127.0.0.1', 8899))

        start = time.monotonic()
        with self.assertRaises(MaxRetriesException):
            await _send(protocol, b'A')
        self.assertLess(time.monotonic() - start, TIMEOUT + 0.2)
