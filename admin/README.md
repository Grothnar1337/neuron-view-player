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

| Path       | Auth | Purpose                                   |
|------------|------|-------------------------------------------|
| `/`        | yes  | Status + SDP editor                        |
| `/save`    | yes  | Writes the SDP, restarts MediaMTX          |
| `/healthz` | no   | Liveness for monitoring; returns `ok`      |

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
