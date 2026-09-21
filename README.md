# neuron-view-player

Serves a unicast RTP/UDP stream (described by an `.sdp` file) as WebRTC on a
plain `http://` URL, so a third-party product's embedded browser can be pointed
straight at it and just show the video. No iframe, no custom player page, no
credentials at the viewer.

```
RTP/UDP source → FFmpeg (transcode 4:4:4 → 4:2:0) → RTSP → MediaMTX → WebRTC → browser
                 └──────────── inside the MediaMTX container ────────────┘
```

Viewers open `http://<vm-ip>:8889/live` — MediaMTX's own bare player page.

## The three things that make this non-obvious

1. **The transcode is mandatory.** The source is 1080p60 H.264 **High 4:4:4
   Predictive, yuv444p**, and no browser decodes 4:4:4. `-c:v copy` publishes
   fine and shows black. This cannot be fixed at the source.
2. **The SDP has two `m=video` sections** and only the first carries data.
   Without `-map 0:v:0` FFmpeg waits forever on the dead one.
3. **It's unicast.** Only one process can bind the RTP port, and the SDP's
   `c=` line must be the receiving VM's own address — get it wrong and the
   pipeline sits at 0 fps with no error at all.

Full background in [docs/HANDOVER.md](docs/HANDOVER.md).

## Deploy

Target: Ubuntu 24.04 LTS, 8 vCPU, 8 GB RAM, VMXNET3 NIC on the same VLAN as the
source. No GPU needed — the transcode is CPU by design.

```bash
git clone <this-repo> stream && cd stream
cp /path/to/real/unicats.sdp ./unicats.sdp     # not in the repo, see below
sudo ./deploy/install.sh --sdp ./unicats.sdp --source-ip 10.0.0.9
```

Don't have the real SDP to hand? Stand the whole stack up now with a stub and
paste the real one in later — see [Two-phase install](#two-phase-install).

That installs Docker, lays the stack down in `/opt/stream`, applies the UDP
sysctl tuning, raises the NIC RX ring at boot, installs the admin panel as a
systemd unit, opens the firewall, starts everything and runs `check.sh`.

It prints a generated admin password once — save it.

Useful flags:

| Flag | Effect |
|---|---|
| `--placeholder-sdp` | Install with a stub SDP; add the real one later |
| `--viewer-cidr 10.0.4.0/24` | Scope the viewer-facing rules instead of `any` |
| `--mgmt-cidr 10.0.9.0/24` | Scope SSH and the admin panel |
| `--source-ip 10.0.0.9` | Lock the RTP ingest port to the source |
| `--iface ens160` | Override NIC autodetection |
| `--no-hls` | Turn off the HLS fallback and its firewall rule |
| `--no-firewall` / `--no-packages` | Skip those steps |

Re-running `install.sh` is safe: it upgrades config in place, keeps the live
SDP (backing it up if it changed), and keeps the existing admin password.

### The SDP

`unicats.sdp` is **not in the repo** — it holds real network topology and it is
per-VM. Copy the working one from the current VM, and make sure its `c=` line
is the address of the VM you are deploying to. `unicats.sdp.example` shows the
expected shape.

`install.sh` refuses to start without it, on purpose: Docker would create a
*directory* at the bind-mount path and the failure would look like an FFmpeg
problem.

### Two-phase install

If the real SDP isn't available yet, install with a stub and add it later:

```bash
sudo ./deploy/install.sh --placeholder-sdp --no-firewall
```

That writes [deploy/placeholder.sdp](deploy/placeholder.sdp), whose `c=` line
is `0.0.0.0`. That address is the point of it: FFmpeg binds whatever `c=` says,
so a plausible-but-wrong IP fails to bind and MediaMTX restarts it in a loop,
whereas `0.0.0.0` binds to everything, succeeds, and simply waits — quiet, and
correct the moment real packets arrive.

Everything else installs and is verified for real. `check.sh` recognises the
stub by that `c=` line and reports the source checks as `WAIT` rather than
`FAIL`, so it still exits 0:

```
Placeholder SDP in use — /opt/stream/unicats.sdp has c=0.0.0.0, so no
source is expected yet.
  ...
  WAIT  path 'live' is not publishing — nothing is reaching the browser
  WAIT  no data, as expected — the placeholder SDP points at no source
```

Then paste the real SDP into the admin panel at `:8080` and save — it backs up
the stub, writes in place and restarts the pipeline. Set its `c=` line to this
VM's address as you paste. Re-running `install.sh --placeholder-sdp` later will
*not* overwrite a real SDP; it says so and leaves it alone.

If you did use the firewall, note that the rule was opened for the
placeholder's port (5004). Re-run `deploy/firewall.sh` if the real port
differs, and delete the stale rule.

## Verify

```bash
sudo ./deploy/check.sh
```

Checks the container, the FFmpeg transcode, that bytes are actually *moving*
(a non-zero total with a stopped sender looks healthy in a single sample), the
listeners, the host tuning and recent RTP loss. Then load
`http://<vm-ip>:8889/live` in a normal desktop browser **before** pointing the
embedded one at it.

## Ports

| Port | Proto | Reachable by | Purpose |
|---|---|---|---|
| 8889 | TCP | viewers | WebRTC/WHEP signalling + player page |
| 8189 | UDP | viewers | WebRTC media — goes **direct**, no proxy can carry it |
| 8888 | TCP | viewers | HLS fallback (optional) |
| 8080 | TCP | ops | Admin panel |
| *from SDP* | UDP | the source | RTP ingest |
| 8554 | TCP | loopback | RTSP, internal hop only |
| 9997 | TCP | loopback | MediaMTX API, used by the admin panel |

There is **no TLS and no reverse proxy**, deliberately: the consuming browser
is pointed at a plain `http://` URL and this is LAN-only. If that changes,
MediaMTX self-terminates TLS via `webrtcEncryption`/`webrtcServerCert` — don't
reintroduce Caddy for it.

## Admin panel

`http://<vm-ip>:8080/` — status (publishing? viewers? bytes?) and a textarea to
paste a new SDP, which is saved (old kept as `.bak`) and the pipeline
restarted. Basic auth, internal network only. See [admin/README.md](admin/README.md).

## Layout

```
docker-compose.yml       single mediamtx service, host networking
mediamtx.yml             server config + the FFmpeg runOnInit pipeline
unicats.sdp.example      template; the real one is deployment-specific
admin/                   Flask status + SDP-editor panel (systemd, not in compose)
  test_app.py            smoke test — auth, bad-paste safety, in-place write
deploy/
  install.sh             provision a clean VM; idempotent
  check.sh               post-deploy verification
  firewall.sh            ufw rules, RTP port read from the SDP
  placeholder.sdp        stub for a two-phase install (c=0.0.0.0)
  uninstall.sh           tear it back down
  99-stream.conf         UDP buffer sysctl
  nic-rxring.service     raises the VMXNET3 RX ring at boot
docs/
  HANDOVER.md            original design brief and reasoning
  RUNBOOK.md             symptom → fix, routine ops, moving VMs
handover/                the untouched original handover pack
```

## Troubleshooting

[docs/RUNBOOK.md](docs/RUNBOOK.md) maps each `check.sh` failure to its fix.
The short version: black video means the transcode; no bytes means the SDP's
`c=` line or the sender's destination; connection never establishing means UDP
8189.

## If you cloned this on Windows

The shell scripts run on the VM and CRLF breaks the shebang. `.gitattributes`
forces LF, but if you copied the files rather than cloning:

```bash
sed -i 's/\r$//' deploy/*.sh
```
