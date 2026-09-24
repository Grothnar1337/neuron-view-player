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

`check.sh` prints a **Source** section saying whether the SDP is unicast or a
multicast group. Follow the matching list.

**Unicast**

1. Is the sender pointed at **this** VM's IP?
2. Does the SDP's `c=` line hold this VM's address? If it does not, FFmpeg
   binds somewhere useless and sits at 0 fps *silently* — no error.
3. Is something else holding the RTP port? Only one receiver can bind a unicast
   port. VLC and FFmpeg will fight over it. `sudo ss -lnup | grep <port>`.
4. Firewall, if you run one: `sudo ufw status | grep <port>`.

**Multicast** (`c=` is 224.0.0.0–239.255.255.255)

1. Is the sender actually transmitting to that group and port?
2. Did FFmpeg join? `ip maddr show` should list the group. If it does not, the
   pipeline is not running or the SDP is not what you think.
3. Right NIC? FFmpeg joins on the **default-route** interface. On a host with
   more than one NIC, the group may be on a network the default route does not
   use. Watch it arrive: `sudo tcpdump -ni <nic> host <group>`.
4. The switch needs **IGMP snooping with a querier**. Without a querier the
   stream may arrive for a minute or two and then stop when the switch ages the
   membership out — a classic "works, then dies" symptom.
5. `sysctl net.ipv4.conf.all.rp_filter` should be `2` (set by
   `99-stream.conf`). Strict `1` drops multicast from another subnet.
6. If the SDP has `a=source-filter: incl … <source>`, FFmpeg does a
   source-specific (SSM) join, which needs IGMPv3 on the network.
7. Firewall, if you run one: it must allow IGMP and the UDP port. `firewall.sh`
   only handles the port, and only as read at install time.

### Pipeline stalls immediately, no frames ever

The SDP has **two `m=video` sections** and only the first carries data. FFmpeg
must have `-map 0:v:0` or it waits forever on the dead second stream. This is
already in `mediamtx.yml` — check nobody removed it.

### "waiting for audio track" / FFmpeg hangs on audio

There is no audio in this source. `-an` must be present.

### `non-existing PPS 0 referenced` / `no frame!` for the first second

Normal. FFmpeg joined mid-stream and is waiting for the next keyframe. It
clears on its own within about a second.

### `RTP: missed N packets`, or the picture tears / smears

**Find out whether the packets reached the VM before blaming the network.**
That one check decides everything:

```bash
ip -s link show <iface>          # NIC-level
netstat -su | grep -iE "receive buffer errors|packet receive errors"
```

- **NIC shows `dropped`/`errors`/`missed` climbing** → genuine wire loss.
  Network path first: wired, same VLAN as the source, no wireless hop. Then
  confirm `net.core.rmem_max` is 134217728 and the RX ring is 4096
  (`ethtool -g <iface>`). Do not start editing FFmpeg flags.

- **NIC clean but `receive buffer errors` climbing** → the packets arrived
  and the receiver was too slow to read them. This is a *local* problem and
  the network is fine. See below.

The loss pattern tells you which too: genuine wire loss is a steady trickle
of small numbers. Bursts of thousands at a time are a receiver stall.

#### Receiver-side stalls

This bit the first deployment. Two causes, both now fixed in `mediamtx.yml`,
recorded here because the instinct is to "fix" them in the wrong direction:

1. **`-reorder_queue_size` set too high.** On a lost packet the RTP demuxer
   holds every packet behind it until the queue fills. At 10000 that is
   seconds of stalled reading, long enough to overflow the socket buffer and
   lose thousands more packets — which stalls it again. It looks exactly like
   catastrophic network loss. 500 is FFmpeg's default and is right for a LAN
   unicast feed. **Raising this to absorb loss causes loss.**

2. **The encoder starving the reader.** Check with `top`:

   ```bash
   top -bn1 | grep ffmpeg
   ```

   If ffmpeg is at several hundred percent *and* the box still shows idle CPU,
   x264 cannot use the cores it has. `-tune zerolatency` forces
   `--sliced-threads`, which scales far worse than frame threading. Levers in
   order of increasing latency cost:

   - `-preset veryfast` (or `superfast`) — no latency cost
   - `-thread_queue_size 4096` — lets the demuxer drain while the encoder works
   - drop `-tune zerolatency` — regains frame threading, adds ~100-150ms
   - lower `-b:v` — smallest CPU effect of the four

Confirm the socket buffer is actually the size you think:

```bash
sudo ss -lunpm | grep -A1 ffmpeg
```

`rb` in the `skmem:` line is the receive buffer in bytes; expect ~33554432.

The counters are cumulative, so to measure a change: note the number, restart,
wait a few minutes, and compare the delta — not the absolute value.

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
2. Unicast only: edit the SDP's `c=` line to the **new** VM's address (a
   multicast SDP is unchanged — the group is the same wherever it is received).
3. Unicast only: repoint the sender's destination IP at the new VM.
4. Unicast only: stop the old VM's receiver before starting the new one — one
   binder only. Multicast has no such limit; both can receive at once.
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
- Single VM, single source (unicast or multicast). No redundancy — deliberately out of scope
  for the first pass.
- Not yet decided: whether to permanently lock the RTP ingest port to the
  source IP (`firewall.sh --source-ip` does it when you are ready).
