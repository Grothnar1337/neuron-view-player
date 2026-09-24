# neuron-view-player

Serves a unicast or multicast RTP/UDP stream (described by an `.sdp` file) as WebRTC on a
plain `http://` URL, so a third-party product's embedded browser can be pointed
straight at it and just show the video. No iframe, no custom player page, no
credentials at the viewer.

```
RTP/UDP source → FFmpeg (transcode 4:4:4 → 4:2:0) → RTSP → MediaMTX → WebRTC → browser
                 └──────────── inside the MediaMTX container ────────────┘
```

Viewers open `http://<vm-ip>:8889/live` — MediaMTX's own bare player page.

> **Using it rather than deploying it?** [QUICKSTART.md](QUICKSTART.md) is the
> operator guide: point Neuron View at the server, load the SDP, check it's
> running. This README is about how the thing is built and deployed.

## The three things that make this non-obvious

1. **The transcode is mandatory.** The source is 1080p60 H.264 **High 4:4:4
   Predictive, yuv444p**, and no browser decodes 4:4:4. `-c:v copy` publishes
   fine and shows black. This cannot be fixed at the source.
2. **The SDP has two `m=video` sections** and only the first carries data.
   Without `-map 0:v:0` FFmpeg waits forever on the dead one.
3. **The SDP's `c=` line decides unicast vs multicast, and nothing else needs
   configuring.** For **unicast** it must be the receiving VM's own address (get
   it wrong and the pipeline sits at 0 fps with no error at all) and only one
   process can bind the port. For **multicast** it is the group (224.0.0.0/4);
   FFmpeg joins it on the default-route NIC, several receivers can coexist, and
   the network needs IGMP snooping with a querier. Paste the SDP into the admin
   panel and that is the whole change — see [the SDP](#the-sdp).

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
per-VM. Copy it straight out of the Neuron View interface. It works **as-is**;
it does not need editing.

`unicats.sdp.example` in this repo is a minimal illustrative stub. What Neuron
View actually emits looks like this (addresses changed):

```
v=0
o=- 3008501142 3999023993 IN IP4 10.0.0.9
s=Compressed Output 2
t=0 0
m=video 5100 RTP/AVP 98
c=IN IP4 10.0.0.50/32
b=AS:10500
a=source-filter: incl IN IP4 10.0.0.50 10.0.0.9
a=rtpmap:98 H264/90000
a=fmtp:98 width=1920; height=1080; exactframerate=60000/1001; colorimetry=BT709; TCS=SDR; profile-level-id=F4003E; packetization-mode=1; max-br=10000;
```

Reading one of these:

- **`o=`** carries the *sender's* address, **`c=`** the *destination*. Neuron
  View writes the destination it is configured with, so a correctly configured
  sender produces a correct `c=` on its own — it does not need hand-editing.
  If `c=` is not this VM, fix the destination in Neuron View rather than the
  file, or you have a sender still pointed elsewhere.
- **`a=source-filter: incl IN IP4 <dest> <source>`** names the source address,
  which is what `deploy/firewall.sh --source-ip` wants.
- **`m=video <port>`** is the RTP ingest port the firewall rule needs. Note it
  is media-level here — the `c=` line sits *inside* the `m=` section, after it,
  not at session level above it. Both are valid SDP.
- **`b=AS:`** is the source bitrate in kbit/s. Useful context when judging
  quality — see the encoder notes in `docs/RUNBOOK.md`.
- **`profile-level-id=F4003E`** decodes to profile_idc 244, which is
  High 4:4:4 Predictive. That is the byte that makes the transcode mandatory.
- **`exactframerate=60000/1001`** is 59.94, not 60.

Some Neuron View configurations emit **two `m=video` sections** with only the
first carrying data; others emit one. `-map 0:v:0` in `mediamtx.yml` handles
both and must stay.

A **multicast** SDP looks the same except `c=` holds a group and `source-filter`
names the sender:

```
c=IN IP4 239.10.1.5/32
a=source-filter: incl IN IP4 239.10.1.5 10.0.0.9
```

`install.sh`, `check.sh` and the admin panel all read `c=` with the `/32` (or
`/TTL`) suffix stripped and work out unicast vs multicast from it. `check.sh`
prints a **Source** section, and for multicast confirms the group appears in
`ip maddr`. A `source-filter` makes FFmpeg do a source-specific join, which
needs IGMPv3 on the network. `deploy/99-stream.conf` sets `rp_filter` to loose
so multicast from another subnet isn't dropped.

**Multi-NIC hosts:** FFmpeg joins the group on the default-route interface. If
the multicast network isn't the default route, add a route for the group
(`ip route add 239.10.1.0/24 dev <nic>`); there is deliberately no per-stream
NIC setting, so the SDP stays the only thing that changes.

**Firewall:** `deploy/firewall.sh` is optional and is **not** SDP-aware — it
opens the ingest port as read at install time, and does not allow IGMP. If you
run ufw, re-run it when the port changes and allow IGMP yourself for multicast.
Running without a firewall (`install.sh --no-firewall`) avoids all of this.

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
