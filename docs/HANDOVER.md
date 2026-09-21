# Handover: RTP/SDP unicast stream to WebRTC, browser-embeddable

> **Archived brief — kept verbatim for the reasoning.** This is the original
> handover. It has since been built out into this repo; where the "Remaining
> steps" at the bottom landed:
>
> | Item | Where it lives now |
> |---|---|
> | 1. Copy the real SDP | `deploy/install.sh --sdp ...` (it refuses to start without one); `unicats.sdp.example` |
> | 2. sysctl UDP tuning | `deploy/99-stream.conf`, applied by `install.sh` |
> | 3. Firewall rules | `deploy/firewall.sh` — reads the RTP port out of the SDP, `--source-ip` scopes it |
> | 4. Deploy the admin panel | `install.sh` does it; password in a systemd drop-in, not the unit |
> | 5. Repoint the sender / fix `c=` | `install.sh` warns if `c=` is not an address on this VM; `check.sh` catches a stalled sender |
> | 6. Test in a browser | `deploy/check.sh`, then the player URL it prints |
> | 7. Lock the RTP port / redundancy | Still open — `--source-ip` is there when you want it; redundancy still out of scope |
>
> Two gaps in the config as handed over were also closed: `mediamtx.yml` was
> missing the `api:` block that the admin panel depends on, and RTSP/API are
> now bound to loopback rather than every interface.
>
> Operational detail is in [RUNBOOK.md](RUNBOOK.md).

## Goal
A dedicated VM that takes a unicast RTP stream (described by an SDP file) and
serves it as WebRTC in a plain browser page, so a third-party product's
embedded browser can be pointed at a URL and just show the video. No iframe
allowed — the target product navigates directly to a URL.

## Source stream — read this before touching anything
- Delivered as RTP/UDP, described by an `.sdp` file (not MPEG-TS — confirmed
  via `ffprobe`/`ffmpeg` output: bare `h264` stream, RTP payload type from
  RFC 6184, not `MP2T/90000`).
- **1920x1080, 60fps, H.264 High 4:4:4 Predictive, yuv444p.** This cannot be
  changed at the source. Browsers only decode 4:2:0, so a transcode step
  (4:4:4 → 4:2:0) is mandatory and unavoidable — don't waste time trying
  `-c:v copy`, it publishes but shows black in-browser.
- The SDP has **two `m=video` sections**; only the first carries data. Always
  use `-map 0:v:0` in FFmpeg or it stalls waiting on the dead second stream.
- No audio in this stream. Use `-an`, don't waste time chasing "waiting for
  audio track" hangs.
- Expect `non-existing PPS 0 referenced` / `no frame!` for the first second —
  normal, it's FFmpeg waiting for the next keyframe when joining mid-stream.
- Expect some `RTP: missed N packets` — this is real UDP loss, not a bug.
  Mitigated (not eliminated) by large socket buffers — see sysctl below.
  If it's excessive, the fix is wired networking + larger buffers, in that
  order, not code changes.
- It's **unicast** — only one receiver can bind the destination port. VLC
  and FFmpeg fight over it; only run one at a time. Moving to a new VM means
  repointing the sender's destination IP and editing the SDP's `c=` line to
  match — the pipeline will sit at 0 fps silently otherwise.

## Architecture decided on
```
RTP/UDP source → FFmpeg (transcode 4:4:4→4:2:0) → RTSP → MediaMTX → WebRTC → browser
```
- **MediaMTX** (`bluenviron/mediamtx` Docker image, `-ffmpeg` tag so FFmpeg
  is bundled) does the WebRTC/WHEP serving and has a built-in bare player
  page at `/<path>` — this was tested and works, no custom page needed.
- **No Caddy / no TLS layer.** Deliberately dropped — the target embedded
  browser is being pointed at a plain `http://` URL, so no mixed-content
  issue, and this is LAN-only. Revisit only if the consuming page becomes
  HTTPS or this needs to leave the LAN (MediaMTX can self-terminate TLS via
  `webrtcEncryption`/`webrtcServerCert` if that day comes — don't
  reintroduce Caddy for that, it's unnecessary).
- FFmpeg is launched **by MediaMTX itself** via `runOnInit` on the path
  config (not a separate container/process) so MediaMTX supervises and
  restarts it (`runOnInitRestart: yes`).
- `network_mode: host` in Docker Compose — simplest way to avoid juggling
  UDP port mappings for RTP in, RTSP, WebRTC signaling and WebRTC media.

## Two network paths — both must be open
- **Signaling/page**: TCP 8889 (WebRTC/WHEP + player page). Also TCP 8888
  if HLS fallback is ever needed for a browser with no WebRTC support.
- **Media itself**: UDP 8189 (WebRTC media, goes direct MediaMTX↔viewer,
  bypasses any reverse proxy — this is why Caddy alone wouldn't have been
  sufficient if kept).
- RTP in from the source: whatever port the SDP specifies, UDP, source IP
  only if you want to lock it down.

## VM target
VMware, no GPU passthrough, plenty of spare CPU. Decided: Ubuntu 24.04 LTS,
8 vCPU, 8GB RAM, VMXNET3 NIC on same VLAN as the source if possible.
- `preset fast` (not `veryfast`) and `-b:v 12M` since CPU headroom is
  available — better quality on the 4:4:4→4:2:0 squeeze, which is hardest on
  sharp edges/text content.
- Raise VMXNET3 RX ring in-guest: `sudo ethtool -G <iface> rx 4096`
  (persist via netplan/udev).
- DRS: set to manual or partially-automated for this VM, or expect a glitch
  during vMotion.

## Files already built (see attached)
- `docker-compose.yml` — single `mediamtx` service, host networking.
- `mediamtx.yml` — path config with the FFmpeg `runOnInit` command, auth
  block restricting publish to localhost only, read open to anyone.
- `admin/app.py` + `admin/requirements.txt` + `admin/stream-admin.service`
  + `admin/README.md` — a small separate Flask service (not in the Compose
  stack, deployed as a systemd unit) giving a headless web UI: status
  (publishing? viewer count? bytes?) pulled from MediaMTX's own API on
  127.0.0.1:9997, and a textarea to paste a new SDP and hit save, which
  writes the file (keeping a `.bak`), then runs
  `docker compose restart mediamtx` so FFmpeg picks it up. Basic HTTP auth
  only — internal network use, not internet-facing as-is.

## Remaining steps / open items
1. Copy the actual SDP file from the working VM (`unicats.sdp`) to the new
   VM at the path `mediamtx.yml` expects (`/opt/stream/unicats.sdp` by
   MediaMTX's mount, matches `SDP_PATH` in the admin app's env).
2. Host sysctl tuning for UDP buffers, in `/etc/sysctl.d/99-stream.conf`:
   ```
   net.core.rmem_max=134217728
   net.core.rmem_default=33554432
   net.core.netdev_max_backlog=5000
   ```
   then `sudo sysctl --system`.
3. Firewall rules (ufw or equivalent): allow TCP 8889 (and 8888 if HLS
   fallback wanted), UDP 8189, and UDP on the RTP port scoped to the
   source's IP if possible.
4. Deploy the admin panel per its own README (separate venv + systemd unit,
   not containerized).
5. Repoint the sender to the new VM's IP and update the SDP's `c=` line to
   match, per the source-stream note above.
6. Test in a normal browser at `http://<vm-ip>:8889/live` before pointing
   the actual embedded browser at it.
7. Not yet decided / worth a follow-up: whether to lock the RTP ingest port
   to the source's IP specifically, and whether multi-VM redundancy is
   ever needed (out of scope for this first pass — it's a single
   unicast source to a single receiver by design).
