# Runbook

Operating notes for the RTP → WebRTC stream VM. The design reasoning lives in
[HANDOVER.md](HANDOVER.md); this is what to do when something is wrong.

## First move, always

```bash
sudo ./deploy/check.sh     # from your checkout of this repo, on the VM
```

It checks the container, the FFmpeg transcode, whether bytes are actually
*moving* (not just non-zero), the listeners, the host tuning and recent RTP
loss. Every failure line below corresponds to one of its checks.

## Where things live

| Thing | Path |
|---|---|
| Compose stack + config | `/opt/stream/` |
| Live SDP (and `.bak`) | `/opt/stream/unicats.sdp` |
| Admin panel | `/opt/stream-admin/` |
| Admin credentials | `/etc/systemd/system/stream-admin.service.d/10-local.conf` |
| MediaMTX logs | `docker logs -f mediamtx` |
| Admin logs | `journalctl -u stream-admin -f` |

## Ports

| Port | Proto | Who | Notes |
|---|---|---|---|
| 8889 | TCP | viewers | WebRTC/WHEP signalling **and** the player page |
| 8189 | UDP | viewers | WebRTC media, direct to the viewer — a proxy cannot carry this |
| 8888 | TCP | viewers | HLS fallback, optional |
| 8080 | TCP | ops | Admin panel |
| *SDP port* | UDP | source | RTP ingest; read the port from the SDP's `m=video` line |
| 8554 | TCP | — | RTSP, **loopback only**, internal hop |
| 9997 | TCP | — | MediaMTX API, **loopback only**, used by the admin panel |

## "It says WAIT, not OK"

The SDP at `/opt/stream/unicats.sdp` is the placeholder — its `c=` line is
`0.0.0.0`. No source is configured yet, so `check.sh` reports the source
checks as `WAIT` and exits 0. Everything else was genuinely verified.

Paste the real SDP into the admin panel to finish, setting its `c=` line to
this VM's address. To confirm which one is live:

```bash
grep '^c=' /opt/stream/unicats.sdp
```

## Symptoms

### Black video in the browser, but the page loads

Almost always the 4:4:4 → 4:2:0 transcode not running. The source is H.264
High 4:4:4 Predictive and **no browser decodes that** — if someone has changed
the FFmpeg line to `-c:v copy`, it will publish happily and show black. Check
`-pix_fmt yuv420p` is still in `runOnInit` in `mediamtx.yml`.

### `check.sh` says bytesReceived is not increasing

Nothing is arriving from the source. In order:

1. Is the sender pointed at **this** VM's IP?
2. Does the SDP's `c=` line hold this VM's address? If it does not, FFmpeg
   binds somewhere useless and sits at 0 fps *silently* — no error.
3. Is something else holding the RTP port? It is **unicast**, so only one
   receiver can bind it. VLC and FFmpeg will fight over it. `sudo ss -lnup |
   grep <port>`.
4. Firewall: `sudo ufw status | grep <port>`.

### Pipeline stalls immediately, no frames ever

The SDP has **two `m=video` sections** and only the first carries data. FFmpeg
must have `-map 0:v:0` or it waits forever on the dead second stream. This is
already in `mediamtx.yml` — check nobody removed it.

### "waiting for audio track" / FFmpeg hangs on audio

There is no audio in this source. `-an` must be present.

### `non-existing PPS 0 referenced` / `no frame!` for the first second

Normal. FFmpeg joined mid-stream and is waiting for the next keyframe. It
clears on its own within about a second.

### `RTP: missed N packets`

Real UDP loss, not a bug. A few per hour is fine. If it is constant:

1. Network path first — wired, same VLAN as the source, no wireless hop.
2. Then buffers: confirm `net.core.rmem_max` is 134217728 (`sysctl -n
   net.core.rmem_max`) and the NIC RX ring is 4096 (`ethtool -g <iface>`).
3. Do **not** start editing FFmpeg flags. That is not where this is fixed.

### Page loads, connection never establishes

UDP 8189 is blocked, or ICE is advertising the wrong address. WebRTC media
goes direct from MediaMTX to the viewer and bypasses any proxy. On a
multi-homed VM, pin the right address in `mediamtx.yml`:

```yaml
webrtcAdditionalHosts: [10.0.0.50]
```

then `cd /opt/stream && docker compose restart mediamtx`.

### Embedded browser shows nothing but a desktop browser works

Likely no WebRTC support in that browser. Point it at the HLS fallback
instead: `http://<vm-ip>:8888/live`. It is a few seconds behind but it is
plain MPEG-TS HLS, which almost anything plays.

### CPU pegged

The transcode is the whole cost. `preset fast` at 12 Mbit on 1080p60 was
chosen because there was headroom. If the VM is smaller than the 8 vCPU it
was sized for, step down to `-preset veryfast` and/or `-b:v 8M` in
`mediamtx.yml`. Quality on sharp edges and text degrades first.

## Routine operations

**Change the SDP** — use the admin panel at `http://<vm-ip>:8080/`. It keeps a
`.bak` and restarts MediaMTX for you. By hand:

```bash
sudo cp /opt/stream/unicats.sdp /opt/stream/unicats.sdp.bak
sudo nano /opt/stream/unicats.sdp
cd /opt/stream && sudo docker compose restart mediamtx
```

Edit the file **in place**. It is bind-mounted into the container as a single
file, so replacing it with a rename swaps the inode and the container keeps
reading the old content.

**Restart the pipeline**

```bash
cd /opt/stream && sudo docker compose restart mediamtx
```

**Update MediaMTX**

```bash
cd /opt/stream && sudo docker compose pull && sudo docker compose up -d
```

Pin the image tag in `docker-compose.yml` once you are happy with a version —
`latest-ffmpeg` will move under you.

**Change the admin password**

```bash
sudo systemctl edit stream-admin    # add Environment=ADMIN_PASS=...
sudo systemctl restart stream-admin
```

## Moving to a different VM

1. Run `deploy/install.sh` on the new VM with the real SDP.
2. Edit the SDP's `c=` line to the **new** VM's address.
3. Repoint the sender's destination IP at the new VM.
4. Stop the old VM's receiver before starting the new one — unicast, one
   binder only.
5. `./deploy/check.sh`, then load the player in a normal browser before
   pointing the embedded one at it.

## VMware notes

- Set DRS to manual or partially-automated for this VM, or expect a visible
  glitch during vMotion.
- No GPU passthrough is assumed; the transcode is pure CPU by design.

## Known gaps

- Read access to the stream is anonymous — the consuming product navigates
  straight to a URL and cannot present credentials. Access control is the
  firewall's job.
- The admin panel has no CSRF protection or rate limiting. Internal network
  only; put it behind a VPN if that stops being true.
- Single VM, single unicast source. No redundancy — deliberately out of scope
  for the first pass.
- Not yet decided: whether to permanently lock the RTP ingest port to the
  source IP (`firewall.sh --source-ip` does it when you are ready).
