# gt7-telemetry-relay

`gt7-telemetry-relay` lets multiple telemetry applications share one Gran Turismo 7
UDP stream. It owns GT7's single telemetry connection, keeps it alive, and
forwards the data to one or more local or network destinations.

The relay always requests GT7's richest `C` packet format. Each output can
independently receive an encrypted `A`, `B`, or `C` packet, so older apps can
run alongside apps that use the extended data.

## Features

- One GT7 telemetry connection, many consumers.
- Per-output `A`, `B`, or `C` packet format selection.
- Encrypted packets remain compatible with applications that natively support
  the selected format.
- Local, LAN, and IPv6 output destinations.
- No third-party Python dependencies.

## Requirements

- Gran Turismo 7 and the computer running the relay on the same network.
- A GT7 version that supports the `C` telemetry format.
- Python 3.10 or newer when installing from source. Standalone releases include
  Python and do not require a separate installation.

## Install

Clone the repository, then install the command-line tool:

```sh
python3 -m pip install .
```

For development, use an editable install instead:

```sh
python3 -m pip install -e .
```

## Standalone binaries

If you do not want to install Python, download the binary for your operating
system from the [latest release](https://github.com/ThrottleGeist/gt7-telemetry-relay/releases/latest).
Open a terminal in the downloaded file's folder, then run the matching command:

| Platform | Release file | Start command |
| --- | --- | --- |
| Windows (64-bit) | `gt7-telemetry-relay-windows-x86_64.exe` | `./gt7-telemetry-relay-windows-x86_64.exe` |
| macOS, Apple Silicon | `gt7-telemetry-relay-macos-arm64` | `./gt7-telemetry-relay-macos-arm64` |
| macOS, Intel | `gt7-telemetry-relay-macos-x86_64` | `./gt7-telemetry-relay-macos-x86_64` |
| Linux (64-bit) | `gt7-telemetry-relay-linux-x86_64` | `./gt7-telemetry-relay-linux-x86_64` |

On macOS and Linux, first allow the downloaded file to run:

```sh
chmod +x gt7-telemetry-relay-macos-arm64
```

Then use the same arguments as the installed command. For example:

```sh
./gt7-telemetry-relay-macos-arm64 192.168.1.42 \
  --output 127.0.0.1:33741:A \
  --output 127.0.0.1:33742:B
```

On Windows, run the `.exe` from PowerShell with the same arguments. If macOS
blocks an unsigned download, use **Open Anyway** in **System Settings → Privacy
& Security** after confirming the file came from this project's release page.
Alternatively, remove its quarantine attribute from Terminal:

```sh
xattr -dr com.apple.quarantine gt7-telemetry-relay-macos-arm64
```

Replace the filename with the Intel binary name when appropriate.

## Quick start

Replace `192.168.1.42` with the PS5's LAN address:

```sh
gt7-telemetry-relay 192.168.1.42 \
  --output 127.0.0.1:33741:A \
  --output 127.0.0.1:33742:B \
  --output 127.0.0.1:33743:C
```

This starts one GT7 connection and routes the stream as follows:

```text
PS5 / GT7 C ── encrypted UDP to :33740 ──> gt7-telemetry-relay
                                              ├──> 127.0.0.1:33741 (A)
                                              ├──> 127.0.0.1:33742 (B)
                                              └──> 127.0.0.1:33743 (C)
```

At startup, the relay confirms its routing configuration:

```text
INFO listening on 0.0.0.0:33740
INFO forwarding C packets as A to 127.0.0.1:33741
INFO forwarding C packets as B to 127.0.0.1:33742
INFO forwarding C packets as C to 127.0.0.1:33743
```

## Configure each telemetry app

For every downstream app:

1. Set its **receive/listen port** to the port in its corresponding
   `--output` value.
2. Select the matching packet format, if the app exposes that setting.
3. Disable the app's own GT7 heartbeat or connection feature. The repeater is
   the only process that should request telemetry from the PS5.

Do not use port `33740` as an output. It is reserved for the relay's incoming
GT7 feed; forwarding to it would create a local UDP loop.

For an app on another machine, use that machine's reachable address and allow
the selected UDP port through its firewall:

```sh
gt7-telemetry-relay 192.168.1.42 --output 192.168.1.75:33741:B
```

## Output syntax and packet formats

Each output uses this syntax:

```text
--output HOST:PORT[:TYPE]
```

`TYPE` is optional and defaults to `C`.

| Type | Size | Use case |
| --- | ---: | --- |
| `A` | 296 bytes | Older apps that support GT7's base telemetry format. |
| `B` | 316 bytes | Apps that also use steering and motion data. |
| `C` | 368 bytes | Current extended stream; the default and most complete option. |

Examples:

```sh
--output 127.0.0.1:33741             # C (default)
--output 127.0.0.1:33742:B           # B
--output '[::1]:33743:A'              # IPv6 A output
--output 192.168.1.75:33744:C         # C on another machine
```

Internally, C is forwarded unchanged. B uses the compatible encrypted C
prefix, while A is re-encrypted with the nonce derivation its receivers
expect.

## Command-line options

Run `gt7-telemetry-relay --help` for the full reference.

| Option | Default | Description |
| --- | --- | --- |
| `-o`, `--output HOST:PORT[:TYPE]` | required | Destination and requested `A`, `B`, or `C` format. Repeat for each app. |
| `--bind HOST` | `0.0.0.0` | Local interface that receives GT7 telemetry. |
| `--telemetry-port PORT` | `33740` | Local UDP port used for GT7 telemetry. |
| `--heartbeat-port PORT` | `33739` | UDP port on the PS5 that receives GT7 heartbeats. |
| `--heartbeat-interval SECONDS` | `1.0` | How frequently the relay asks GT7 to keep streaming. |
| `-v`, `--verbose` | off | Log every heartbeat and received packet. |

Stop the relay with <kbd>Ctrl</kbd>+<kbd>C</kbd>.

## Troubleshooting

### `Address already in use`

Another process owns the relay's receive port, normally `33740`. Find it with:

```sh
lsof -nP -iUDP:33740
```

Quit that application, or configure the relay to use a different receive port
only if the telemetry source is configured to send there too. A stopped UDP
process releases its port immediately; there is no TCP-style `TIME_WAIT` delay.

### An output app receives no data

Check that the app is actually listening on its configured output port:

```sh
lsof -nP -iUDP:33741 -iUDP:33742 -iUDP:33743
```

Confirm the relay's startup logs show the expected destination and format.
Then verify that the app's receive port and packet type match its `--output`
entry, and that the app's own GT7 heartbeat is disabled.

## How it works

GT7's undocumented telemetry interface sends encrypted UDP data to port
`33740` after receiving a one-byte heartbeat on port `33739`. The heartbeat is
sent from the same socket that listens on `33740`, which causes GT7 to return
the stream to the relay. `gt7-telemetry-relay` sends the `C` heartbeat, receives the C
stream, and fans it out to the configured destinations.

UDP has no delivery guarantee. The relay forwards datagrams as they arrive; it
does not decode telemetry fields, buffer packets, or retry failed sends.

## Support the project

If `gt7-telemetry-relay` helps your GT7 setup, sharing it with other sim racers,
reporting issues, and contributing improvements are all valuable ways to help.

You can also follow my YouTube channel. If you would like to make a one-time
contribution toward open-source GT7 tools, testing, and future development,
you can support ThrottleGeist here:

[![YouTube: ThrottleGeistRacing](https://img.shields.io/badge/YouTube-ThrottleGeistRacing-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@ThrottleGeistRacing) [![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=throttlegeist&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://buymeacoffee.com/throttlegeist)

Support is always optional, and simply using, sharing, or improving the project
is appreciated.

## Development

Run the standard-library test suite from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Publishing standalone releases

Pushing a tag that starts with `v` (for example, `v1.0.0`) builds standalone
executables for Linux, Windows, Apple Silicon Macs, and Intel Macs, then
attaches them to the corresponding GitHub Release. You can also run the
**Build standalone releases** workflow manually to download the executables as
workflow artifacts.

## License

Distributed under the [MIT License](LICENSE).
