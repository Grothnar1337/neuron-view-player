# Stream admin panel

Small internal web page: shows whether MediaMTX is publishing, viewer count and
bytes received, and lets you paste in a new SDP file and restart the pipeline.

## Enable the MediaMTX API

In `mediamtx.yml`, on the same VM, make sure the API is on (it's on by default,
bound to localhost):

```yaml
api: yes
apiAddress: 127.0.0.1:9997
```

## Deploy

```bash
sudo mkdir -p /opt/stream-admin
sudo cp app.py requirements.txt /opt/stream-admin/
cd /opt/stream-admin
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Edit `stream-admin.service`: set `ADMIN_PASS` to something real, and confirm
`SDP_PATH` / `COMPOSE_DIR` match your setup. Then:

```bash
sudo cp stream-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stream-admin
```

Open `http://<vm-ip>:8080/` — it'll prompt for the admin/pass you set.

## Notes

- The service calls `docker compose restart mediamtx` in `COMPOSE_DIR`, so
  whatever user runs it needs docker permissions (either run the service as
  root, which the unit file does by default, or add a dedicated user to the
  `docker` group and set `User=` in the unit file).
- A `.bak` copy of the previous SDP is kept next to the live one on every save.
- This has no rate limiting or CSRF protection — it's meant for a trusted
  internal network only. If it'll be reachable beyond your LAN, put it behind
  a VPN or tighten the auth further.
- Open port 8080 on the VM firewall if you're not already covering it.
