"""UDP transport for owning one GT7 telemetry connection and fanning it out."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import selectors
import socket
import time
from typing import Callable


LOG = logging.getLogger(__name__)

GT7_HEARTBEAT_PORT = 33739
GT7_TELEMETRY_PORT = 33740
FORWARD_PACKET_TYPES = frozenset({"A", "B", "C"})
PACKET_SIZES = {"A": 296, "B": 316, "C": 368}
_KEY = b"Simulator Interface Packet GT7 v"
_NONCE_XORS = {"A": 0xDEADBEAF, "B": 0xDEADBEEF, "C": 0xDEADBEEF}
_WINDOWS_UDP_PORT_UNREACHABLE = 10054


@dataclass(frozen=True)
class RelayOutput:
    """One UDP destination and the GT7 packet format it expects."""

    host: str
    port: int
    packet_type: str = "C"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("output host must not be empty")
        _validate_port(self.port, "output port")
        if self.packet_type not in FORWARD_PACKET_TYPES:
            choices = ", ".join(sorted(FORWARD_PACKET_TYPES))
            raise ValueError(f"output packet type must be one of: {choices}")


@dataclass(frozen=True)
class RelayConfig:
    """Settings for one relay instance.

    ``outputs`` identify the consumer and its requested encrypted packet format.
    Legacy ``(host, port)`` pairs remain valid and default to C.
    """

    console_host: str
    outputs: tuple[RelayOutput | tuple[str, int], ...]
    bind_host: str = "0.0.0.0"
    telemetry_port: int = GT7_TELEMETRY_PORT
    heartbeat_port: int = GT7_HEARTBEAT_PORT
    heartbeat_interval: float = 1.0
    receive_buffer_size: int = 65_535

    def __post_init__(self) -> None:
        if not self.console_host:
            raise ValueError("console_host must not be empty")
        if not self.outputs:
            raise ValueError("at least one output is required")
        normalized_outputs = tuple(
            output if isinstance(output, RelayOutput) else RelayOutput(*output)
            for output in self.outputs
        )
        object.__setattr__(self, "outputs", normalized_outputs)
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be greater than zero")
        for label, port in (("telemetry_port", self.telemetry_port), ("heartbeat_port", self.heartbeat_port)):
            _validate_port(port, label)


class TelemetryRelay:
    """Maintain GT7's heartbeat and copy every incoming UDP datagram to outputs."""

    def __init__(
        self,
        config: RelayConfig,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._socket_factory = socket_factory
        self._clock = clock
        self._socket: socket.socket | None = None
        self._stopped = False
        self.packets_received = 0
        self.packets_forwarded = 0
        self.send_errors = 0

    def open(self) -> None:
        """Bind the sole GT7 receive port.

        The same socket sends the heartbeat so GT7 responds to the port being
        listened on, rather than to an ephemeral source port.
        """
        if self._socket is not None:
            return
        udp_socket = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.bind((self.config.bind_host, self.config.telemetry_port))
            udp_socket.setblocking(False)
        except Exception:
            udp_socket.close()
            raise
        self._socket = udp_socket
        LOG.info("listening on %s:%d", self.config.bind_host, self.config.telemetry_port)
        for output in self.config.outputs:
            LOG.info(
                "forwarding C packets as %s to %s:%d",
                output.packet_type,
                output.host,
                output.port,
            )

    def close(self) -> None:
        self._stopped = True
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def run(self) -> None:
        """Run until :meth:`close` is called or an interrupt is received."""
        self.open()
        assert self._socket is not None
        self._stopped = False
        next_heartbeat = 0.0
        with selectors.DefaultSelector() as selector:
            selector.register(self._socket, selectors.EVENT_READ)
            while not self._stopped:
                now = self._clock()
                if now >= next_heartbeat:
                    self.send_heartbeat()
                    next_heartbeat = now + self.config.heartbeat_interval
                timeout = max(0.0, next_heartbeat - self._clock())
                events = selector.select(timeout)
                for _key, _mask in events:
                    self.forward_available()

    def send_heartbeat(self) -> None:
        """Ask GT7 to keep emitting the richest (C) packet format."""
        udp_socket = self._require_socket()
        try:
            udp_socket.sendto(
                b"C",
                (self.config.console_host, self.config.heartbeat_port),
            )
        except OSError as error:
            LOG.warning("could not send GT7 heartbeat: %s", error)
        else:
            LOG.debug("sent C heartbeat to %s:%d", self.config.console_host, self.config.heartbeat_port)

    def forward_available(self) -> int:
        """Drain received datagrams without blocking and fan each one out.

        Returns the number of source datagrams handled. An unavailable output
        does not prevent the remaining consumers from receiving a packet.
        """
        udp_socket = self._require_socket()
        handled = 0
        while True:
            try:
                packet, source = udp_socket.recvfrom(self.config.receive_buffer_size)
            except BlockingIOError:
                return handled
            except ConnectionResetError as error:
                # Windows reports a prior UDP send that elicited ICMP "Port
                # Unreachable" as WSAECONNRESET (10054) on recvfrom(). This
                # can happen normally when GT7 or an output app stops.
                if self._stopped:
                    return handled
                if _is_windows_udp_port_unreachable(error):
                    LOG.debug("ignoring Windows UDP port-unreachable notification: %s", error)
                    return handled
                raise RuntimeError(f"could not receive GT7 telemetry: {error}") from error
            except OSError as error:
                if self._stopped:
                    return handled
                raise RuntimeError(f"could not receive GT7 telemetry: {error}") from error

            handled += 1
            self.packets_received += 1
            self._forward_packet(udp_socket, packet, source)

    def forward_packet(self, packet: bytes) -> None:
        """Forward a packet supplied by a caller; useful for embedding and tests."""
        self._forward_packet(self._require_socket(), packet, None)

    def _forward_packet(
        self,
        udp_socket: socket.socket,
        packet: bytes,
        source: tuple[str, int] | None,
    ) -> None:
        try:
            packets_by_type = {
                packet_type: _convert_c_packet(packet, packet_type)
                for packet_type in {output.packet_type for output in self.config.outputs}
            }
            for output in self.config.outputs:
                packet_for_output = packets_by_type[output.packet_type]
                try:
                    udp_socket.sendto(packet_for_output, (output.host, output.port))
                except OSError as error:
                    self.send_errors += 1
                    LOG.warning(
                        "could not forward %d-byte %s packet to %s:%d: %s",
                        len(packet_for_output),
                        output.packet_type,
                        output.host,
                        output.port,
                        error,
                    )
                else:
                    self.packets_forwarded += 1
        except ValueError as error:
            LOG.warning("discarding packet that cannot be converted from C: %s", error)
        if source is not None:
            LOG.debug("forwarded %d-byte packet from %s:%d", len(packet), source[0], source[1])

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise RuntimeError("relay is not open")
        return self._socket

    def __enter__(self) -> "TelemetryRelay":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _is_windows_udp_port_unreachable(error: ConnectionResetError) -> bool:
    """Return whether Windows reported an asynchronous UDP port error."""
    return (
        error.errno == _WINDOWS_UDP_PORT_UNREACHABLE
        or getattr(error, "winerror", None) == _WINDOWS_UDP_PORT_UNREACHABLE
    )


def parse_output(value: str) -> RelayOutput:
    """Parse ``HOST:PORT[:TYPE]`` (or ``[IPv6]:PORT[:TYPE]``)."""
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or not value[closing + 1 :].startswith(":"):
            raise ValueError("expected [IPv6-address]:port[:packet-type]")
        host = value[1:closing]
        remainder = value[closing + 2 :]
    else:
        host, separator, remainder = value.partition(":")
        if not separator or not host:
            raise ValueError("expected host:port[:packet-type]")
    port_text, separator, packet_type = remainder.partition(":")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError("port must be an integer") from error
    return RelayOutput(host, port, packet_type or "C")


def _convert_c_packet(packet: bytes, packet_type: str) -> bytes:
    """Return a valid encrypted A, B, or C packet derived from a C packet."""
    if packet_type not in FORWARD_PACKET_TYPES:
        raise ValueError(f"unsupported output packet type: {packet_type}")
    if len(packet) != PACKET_SIZES["C"]:
        raise ValueError(f"expected a {PACKET_SIZES['C']}-byte C packet, got {len(packet)} bytes")
    if packet_type == "C":
        return packet

    output_size = PACKET_SIZES[packet_type]
    if packet_type == "B":
        # B and C share a nonce derivation, so the encrypted prefix is valid.
        return packet[:output_size]

    # A uses a different nonce derivation. Re-key the encrypted C prefix while
    # preserving the seed that receivers use to derive the nonce.
    source_stream = _salsa20_stream(output_size, _packet_nonce(packet, "C"))
    target_stream = _salsa20_stream(output_size, _packet_nonce(packet, "A"))
    converted = bytearray(
        encrypted ^ source_key ^ target_key
        for encrypted, source_key, target_key in zip(packet[:output_size], source_stream, target_stream)
    )
    converted[0x40:0x44] = packet[0x40:0x44]
    return bytes(converted)


def _packet_nonce(packet: bytes, packet_type: str) -> bytes:
    seed = int.from_bytes(packet[0x40:0x44], "little")
    return (seed ^ _NONCE_XORS[packet_type]).to_bytes(4, "little") + seed.to_bytes(4, "little")


def _salsa20_stream(length: int, nonce: bytes) -> bytes:
    """Generate Salsa20/20 keystream bytes for GT7's 32-byte key."""
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(_salsa20_block(nonce, counter))
        counter += 1
    return bytes(output[:length])


def _salsa20_block(nonce: bytes, counter: int) -> bytes:
    key_words = [int.from_bytes(_KEY[index : index + 4], "little") for index in range(0, 32, 4)]
    nonce_words = [int.from_bytes(nonce[index : index + 4], "little") for index in range(0, 8, 4)]
    constants = [int.from_bytes(value, "little") for value in (b"expa", b"nd 3", b"2-by", b"te k")]
    state = [
        constants[0], *key_words[:4], constants[1], *nonce_words,
        counter & 0xFFFFFFFF, counter >> 32, constants[2], *key_words[4:], constants[3],
    ]
    working = state.copy()
    for _ in range(10):
        _salsa20_round(working, 0, 4, 8, 12)
        _salsa20_round(working, 5, 9, 13, 1)
        _salsa20_round(working, 10, 14, 2, 6)
        _salsa20_round(working, 15, 3, 7, 11)
        _salsa20_round(working, 0, 1, 2, 3)
        _salsa20_round(working, 5, 6, 7, 4)
        _salsa20_round(working, 10, 11, 8, 9)
        _salsa20_round(working, 15, 12, 13, 14)
    return b"".join(((value + original) & 0xFFFFFFFF).to_bytes(4, "little") for value, original in zip(working, state))


def _salsa20_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    mask = 0xFFFFFFFF
    state[b] ^= _rotate_left((state[a] + state[d]) & mask, 7)
    state[c] ^= _rotate_left((state[b] + state[a]) & mask, 9)
    state[d] ^= _rotate_left((state[c] + state[b]) & mask, 13)
    state[a] ^= _rotate_left((state[d] + state[c]) & mask, 18)


def _rotate_left(value: int, amount: int) -> int:
    return ((value << amount) | (value >> (32 - amount))) & 0xFFFFFFFF


def _validate_port(port: int, label: str) -> None:
    if not 1 <= port <= 65_535:
        raise ValueError(f"{label} must be between 1 and 65535")
