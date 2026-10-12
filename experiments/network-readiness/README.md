# Network readiness probe

This is a dependency-free TCP measurement tool for an explicitly named device.
It is not peer discovery, NAT traversal, a relay, or a model/hidden-state
transport. The measured payload is a deterministic echo payload only.

## Local loopback

Run the bounded server in one terminal:

```bash
python3 experiments/network-readiness/network_readiness.py serve \
  --host 127.0.0.1 --port 9100 --max-connections 3
```

Run the probe in another terminal:

```bash
python3 experiments/network-readiness/network_readiness.py probe \
  --host 127.0.0.1 --port 9100 --payload-bytes 65536 --samples 3
```

The JSON result includes device identity, best-effort available memory, each
handshake RTT, payload round-trip rate, and bytes passed to `sendall`/`recv`.
The rate is not converted to tokens/s. A probe returns a non-zero exit code and
JSON error when the connection is refused, times out, or the protocol fails.

The repeatable local scenarios are:

```bash
python3 experiments/network-readiness/network_readiness.py self-test
```

`self-test` only binds `127.0.0.1`, and covers normal echo, an explicitly
rejecting peer, an unavailable endpoint (the host OS may report refusal or a
timeout), response timeout, bounded server exit, and listener cleanup. It
reports raw child stdout/stderr and return codes in one JSON document.
The probe's `cleanup.connections_closed` count includes connected sockets that
were closed after an error; a refused connection is not counted because no
connected socket was returned by `socket.create_connection`.

## Two explicitly selected Macs

First choose the server Mac and its existing LAN address. Do not use a public
address or change firewall/permission settings as part of this probe. On the
server Mac, bind only the selected LAN interface and opt in explicitly:

```bash
python3 experiments/network-readiness/network_readiness.py serve \
  --host SERVER_LAN_IP --port 9100 --allow-non-loopback --max-connections 3
```

On the other selected Mac, run:

```bash
python3 experiments/network-readiness/network_readiness.py probe \
  --host SERVER_LAN_IP --port 9100 --payload-bytes 65536 --samples 3
```

Replace `SERVER_LAN_IP` with the tested device's address. This tool does not
scan a subnet, discover peers, configure NAT/CGNAT, open ports, or use a cloud
account. Testers must record whether the Macs are on the same LAN and the
negotiated wired/link rate outside this tool. macOS execution was not part of
the Windows validation run; the commands above are the manual Mac procedure.

## JSON and limitations

- `bytes_written` and `bytes_read` count the protocol header plus payload at
  the Python socket API boundary; they are not packet-capture byte counts.
- RTT is the empty-frame request/response measurement. Payload rate is one
  payload send plus one echo receive, not a model throughput measurement.
- Available memory is labelled with the platform method. Windows uses
  `GlobalMemoryStatusEx`, Linux uses `MemAvailable`, and macOS uses a
  `vm_stat` free+inactive+speculative approximation when run there.
- A successful TCP echo proves only that this named endpoint accepted and
  returned the test bytes. It does not prove llama.cpp RPC, hidden-state
  semantics, NAT traversal, or two-device inference.
