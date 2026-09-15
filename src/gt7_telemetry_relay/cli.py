"""Command-line interface for gt7-telemetry-relay."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence

from .relay import GT7_HEARTBEAT_PORT, GT7_TELEMETRY_PORT, RelayConfig, TelemetryRelay, parse_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gt7-telemetry-relay",
        description="Own one GT7 telemetry connection and re-broadcast each UDP packet to multiple apps.",
        epilog="Example: gt7-telemetry-relay 192.168.1.42 --output 127.0.0.1:33741:A --output 127.0.0.1:33742:B",
    )
    parser.add_argument("console", help="IP address or hostname of the PS5 running GT7")
    parser.add_argument(
        "-o",
        "--output",
        action="append",
        required=True,
        metavar="HOST:PORT[:TYPE]",
        help="UDP destination and packet type (A, B, or C; default: C); repeat for each app",
    )
    parser.add_argument("--bind", default="0.0.0.0", help="local interface to bind (default: %(default)s)")
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=GT7_TELEMETRY_PORT,
        help="local GT7 receive port (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-port",
        type=int,
        default=GT7_HEARTBEAT_PORT,
        help="PS5 heartbeat port (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="heartbeat cadence in seconds (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every heartbeat and received packet")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        outputs = tuple(parse_output(value) for value in args.output)
        config = RelayConfig(
            console_host=args.console,
            outputs=outputs,
            bind_host=args.bind,
            telemetry_port=args.telemetry_port,
            heartbeat_port=args.heartbeat_port,
            heartbeat_interval=args.heartbeat_interval,
        )
    except ValueError as error:
        parser.error(str(error))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    relay = TelemetryRelay(config)

    def stop(_signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info("stopping relay")
        relay.close()

    previous = signal.signal(signal.SIGINT, stop)
    try:
        relay.run()
    except OSError as error:
        logging.getLogger(__name__).error("could not start relay: %s", error)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
        relay.close()
    logging.getLogger(__name__).info(
        "stopped: received=%d forwarded=%d send_errors=%d",
        relay.packets_received,
        relay.packets_forwarded,
        relay.send_errors,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
