from __future__ import annotations

import unittest
from unittest.mock import patch

import gt7_telemetry_relay.relay as relay_module
from gt7_telemetry_relay.relay import RelayConfig, RelayOutput, TelemetryRelay, _packet_nonce, _salsa20_stream, parse_output


class FakeSocket:
    def __init__(self, packets: list[tuple[bytes, tuple[str, int]]] | None = None) -> None:
        self.packets = list(packets or [])
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.bound_to: tuple[str, int] | None = None
        self.closed = False

    def bind(self, address: tuple[str, int]) -> None:
        self.bound_to = address

    def setblocking(self, _: bool) -> None:
        pass

    def recvfrom(self, _: int) -> tuple[bytes, tuple[str, int]]:
        if not self.packets:
            raise BlockingIOError
        return self.packets.pop(0)

    def sendto(self, packet: bytes, address: tuple[str, int]) -> int:
        self.sent.append((packet, address))
        return len(packet)

    def close(self) -> None:
        self.closed = True


class RelayTests(unittest.TestCase):
    def test_sends_heartbeat_from_the_bound_telemetry_socket(self) -> None:
        fake_socket = FakeSocket()
        relay = TelemetryRelay(
            RelayConfig("192.168.1.42", (RelayOutput("127.0.0.1", 33741),)),
            socket_factory=lambda *_: fake_socket,
        )

        relay.open()
        relay.send_heartbeat()

        self.assertEqual(("0.0.0.0", 33740), fake_socket.bound_to)
        self.assertEqual([(b"C", ("192.168.1.42", 33739))], fake_socket.sent)

    def test_logs_each_forwarding_destination_when_opened(self) -> None:
        fake_socket = FakeSocket()
        relay = TelemetryRelay(
            RelayConfig(
                "192.168.1.42",
                (RelayOutput("127.0.0.1", 33741, "C"), RelayOutput("127.0.0.1", 33742, "B")),
            ),
            socket_factory=lambda *_: fake_socket,
        )

        with self.assertLogs("gt7_telemetry_relay.relay", "INFO") as logs:
            relay.open()

        self.assertTrue(any("forwarding C packets as C to 127.0.0.1:33741" in message for message in logs.output))
        self.assertTrue(any("forwarding C packets as B to 127.0.0.1:33742" in message for message in logs.output))

    def test_fans_every_packet_to_each_output_without_changing_it(self) -> None:
        packet_one = b"1" * 368
        packet_two = b"2" * 368
        fake_socket = FakeSocket([(packet_one, ("192.168.1.42", 33739)), (packet_two, ("192.168.1.42", 33739))])
        relay = TelemetryRelay(
            RelayConfig(
                "192.168.1.42",
                (RelayOutput("127.0.0.1", 33741), RelayOutput("127.0.0.1", 33742)),
            ),
            socket_factory=lambda *_: fake_socket,
        )

        relay.open()
        self.assertEqual(2, relay.forward_available())

        self.assertEqual(
            [
                (packet_one, ("127.0.0.1", 33741)),
                (packet_one, ("127.0.0.1", 33742)),
                (packet_two, ("127.0.0.1", 33741)),
                (packet_two, ("127.0.0.1", 33742)),
            ],
            fake_socket.sent,
        )
        self.assertEqual(2, relay.packets_received)
        self.assertEqual(4, relay.packets_forwarded)

    def test_parse_output_accepts_ipv4_and_bracketed_ipv6(self) -> None:
        self.assertEqual(RelayOutput("localhost", 33741), parse_output("localhost:33741"))
        self.assertEqual(RelayOutput("::1", 33742, "A"), parse_output("[::1]:33742:A"))

    def test_forwards_c_as_a_b_and_c_for_independent_outputs(self) -> None:
        seed = (0x12345678).to_bytes(4, "little")
        plaintext = bytes(range(256)) + bytes(range(112))
        encrypted_c = bytearray(_xor(plaintext, _salsa20_stream(368, _nonce_from_seed(seed, "C"))))
        encrypted_c[0x40:0x44] = seed
        fake_socket = FakeSocket()
        relay = TelemetryRelay(
            RelayConfig(
                "192.168.1.42",
                (
                    RelayOutput("127.0.0.1", 33741, "A"),
                    RelayOutput("127.0.0.1", 33742, "B"),
                    RelayOutput("127.0.0.1", 33743, "C"),
                ),
            ),
            socket_factory=lambda *_: fake_socket,
        )

        relay.open()
        relay.forward_packet(bytes(encrypted_c))

        packet_a, packet_b, packet_c = (sent[0] for sent in fake_socket.sent)
        self.assertEqual(296, len(packet_a))
        self.assertEqual(316, len(packet_b))
        self.assertEqual(bytes(encrypted_c), packet_c)
        self.assertEqual(bytes(encrypted_c[:316]), packet_b)
        self.assertEqual(seed, packet_a[0x40:0x44])
        self.assertEqual(
            _decrypt_prefix(bytes(encrypted_c), "C", 296)[:0x40],
            _decrypt_prefix(packet_a, "A", 296)[:0x40],
        )
        self.assertEqual(
            _decrypt_prefix(bytes(encrypted_c), "C", 296)[0x44:],
            _decrypt_prefix(packet_a, "A", 296)[0x44:],
        )

    def test_salsa20_matches_the_reference_keystream(self) -> None:
        key = b"\x80" + b"\0" * 31
        expected = bytes.fromhex(
            "e3be8fdd8beca2e3ea8ef9475b29a6e7"
            "003951e1097a5c38d23b7a5fad9f6844"
            "b22c97559e2723c7cbbd3fe4fc8d9a07"
            "44652a83e72a9c461876af4d7ef1a117"
        )
        with patch.object(relay_module, "_KEY", key):
            self.assertEqual(expected, _salsa20_stream(64, b"\0" * 8))

    def test_config_requires_valid_outputs_and_heartbeat(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one output"):
            RelayConfig("ps5", ())
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            RelayConfig("ps5", (RelayOutput("localhost", 33741),), heartbeat_interval=0)
        with self.assertRaisesRegex(ValueError, "output packet type"):
            RelayOutput("localhost", 33741, "~")
        self.assertEqual(
            (RelayOutput("localhost", 33741),),
            RelayConfig("ps5", (("localhost", 33741),)).outputs,
        )


def _nonce_from_seed(seed: bytes, packet_type: str) -> bytes:
    packet = bytearray(368)
    packet[0x40:0x44] = seed
    return _packet_nonce(packet, packet_type)


def _decrypt_prefix(packet: bytes, packet_type: str, length: int) -> bytes:
    return _xor(packet[:length], _salsa20_stream(length, _packet_nonce(packet, packet_type)))


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


if __name__ == "__main__":
    unittest.main()
