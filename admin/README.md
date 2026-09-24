# Stream admin panel

Small internal web page: shows whether MediaMTX is publishing, viewer count and
bytes received, and lets you paste in a new SDP file and restart the pipeline.

`deploy/install.sh` installs this for you. The steps below are the manual
equivalent, for when you are fixing it rather than provisioning it.

## Prerequisite: the MediaMTX API

The panel reads MediaMTX's own API. The repo's `mediamtx.yml` already enables
it; if you are using a different config, it needs:

```yaml
api: yes
apiAddress: 127.0.0.1:9997
```

and an `authInternalUsers` entry granting `action: api` from `127.0.0.1`.

## Deploy manually

```bash
sudo mkdir -p /opt/stream-admin
sudo cp app.py requirements.txt /opt/stream-admin/
cd /opt/stream-admin
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
```

Edit `stream-admin.service`: set `ADMIN_PASS` to something real, and confirm
`SDP_PATH` / `COMPOSE_DIR` match your setup. Then:

```bash
sudo cp stream-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stream-admin
```

Open `http://<vm-ip>:8080/` — it'll prompt for the admin/pass you set.

## Changing the password later

```bash
sudo systemctl edit stream-admin
```

and add:

```
[Service]
Environment=ADMIN_PASS=the-new-one
```

then `sudo systemctl restart stream-admin`. An override drop-in is better than
editing the unit in place — a redeploy overwrites the unit but leaves the
override alone.

## Endpoints

| Path          | Auth | Purpose                                     |
|---------------|------|---------------------------------------------|
| `/`           | yes  | Preview, status and the SDP editor           |
| `/api/status` | yes  | JSON status, polled by the page every 3s     |
| `/save`       | yes  | Writes the SDP, restarts MediaMTX            |
| `/healthz`    | no   | Liveness for monitoring; returns `ok`        |

## Layout

Fluid, capped at 1700px. Above 1100px it is two columns — status and the SDP
editor on the left, preview on the right, where the preview sticks so it stays
in view while the SDP box is scrolled. Below 1100px it stacks with the
**preview first**, since that is what you glance at.

Between 620 and 1099px the preview is capped at 640px wide: full-bleed 16:9 at
that width is around 500px tall and pushes the status off screen. On a phone it
goes full width again.

## The preview player

The page embeds a live preview so you can see the picture without opening a
separate tab. It matters more than it sounds: the status readout alone cannot
distinguish a healthy stream from one that is flowing but visibly broken —
during one incident every field read green while the picture was tearing badly.

**It costs the server almost nothing.** The transcode runs once regardless of
viewer count; a viewer only costs MediaMTX packetise-and-send, a few percent of
a core. The decode happens in your browser. Deliberately *not* done: a separate
low-resolution preview encode, which would be a second x264 instance and is the
one version of this feature that would need a bigger VM.

How it works:

- The page speaks **WHEP** directly to MediaMTX on `:8889`, using the same
  hostname you opened the panel with. Media flows browser↔MediaMTX over UDP
  8189 and is never relayed through this Flask app.
- MediaMTX answers the CORS preflight from the panel's origin, so this needs no
  MediaMTX configuration. `WEBRTC_PORT` (default 8889) is the only knob.
- The offer is sent **after ICE gathering completes**, which avoids
  implementing WHEP's PATCH trickle flow. On a LAN with no STUN that is
  instant.
- The player starts and stops itself from the polled status: it connects when
  the path reports `publishing`, tears down and shows "No signal" when it does
  not, and retries on a dropped connection.
- It stops while the tab is in the background — no point pulling 12 Mbit/s into
  a hidden tab — and resumes when you come back.

**Why status is polled rather than rendered:** a full page reload would tear
down the WebRTC connection every few seconds. `/api/status` returns the same
data the template used to render server-side, and the page updates the fields
in place. The throughput figure is derived in the browser from consecutive
samples, because a large but static byte total looks identical to a healthy
stream in any single sample — which is exactly the failure worth catching.

## Tests

```bash
python3 -m venv /tmp/t && /tmp/t/bin/pip install -q -r requirements.txt
/tmp/t/bin/python test_app.py
```

No MediaMTX or docker needed. Covers auth rejection, that a bad paste cannot
destroy the live SDP, CRLF normalisation, and that the file is rewritten in
place. Without docker on PATH the save test reports `saved=norestart`, which
is the correct behaviour, not a failure.

## Notes

- The service calls `docker compose restart mediamtx` in `COMPOSE_DIR`, so
  whatever user runs it needs docker permissions (either run the service as
  root, which the unit file does by default, or add a dedicated user to the
  `docker` group and set `User=` in the unit file).
- A `.bak` copy of the previous SDP is kept next to the live one on every save.
- The SDP is rewritten **in place** on purpose. The live file is bind-mounted
  into the container as a single file, so a write-temp-then-rename would swap
  the inode and leave the container reading the old content until it is
  recreated. Don't "improve" this into an atomic rename.
- This has no rate limiting or CSRF protection — it's meant for a trusted
  internal network only. If it'll be reachable beyond your LAN, put it behind
  a VPN or tighten the auth further.
- Open port 8080 on the VM firewall if you're not already covering it;
  `deploy/firewall.sh` does this, scoped to your management subnet if you
  pass one.
