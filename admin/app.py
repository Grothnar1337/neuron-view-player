import os
import hmac
import subprocess
import datetime
import pathlib
from functools import wraps

from flask import Flask, request, redirect, render_template_string, Response
import requests

SDP_PATH = os.environ.get("SDP_PATH", "/opt/stream/unicats.sdp")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/opt/stream")
COMPOSE_SERVICE = os.environ.get("COMPOSE_SERVICE", "mediamtx")
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997")
MEDIAMTX_PATH = os.environ.get("MEDIAMTX_PATH", "live")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

app = Flask(__name__)


def check_auth(username, password):
    return hmac.compare_digest(username or "", ADMIN_USER) and hmac.compare_digest(
        password or "", ADMIN_PASS
    )


def authenticate():
    return Response(
        "Login required", 401, {"WWW-Authenticate": 'Basic realm="Stream Admin"'}
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)

    return decorated


TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Stream admin</title>
<style>
  body { background:#26282c; color:#e8e6e1; font-family: -apple-system, Segoe UI, sans-serif; margin:0; padding:40px; }
  h1 { font-size:20px; font-weight:600; margin-bottom:24px; }
  h2 { font-size:15px; font-weight:600; color:#c7c5be; margin:0 0 12px; }
  .card { background:#2f3136; border-radius:10px; padding:20px 24px; margin-bottom:20px; max-width:720px; }
  .status-grid { display:grid; grid-template-columns: 160px 1fr; gap:8px 16px; font-size:14px; }
  .status-grid div:nth-child(odd) { color:#9a988f; }
  .ok { color:#7fd08a; }
  .bad { color:#e2726a; }
  textarea { width:100%; min-height:220px; background:#1e2023; color:#e8e6e1; border:1px solid #44464b;
             border-radius:6px; padding:12px; font-family: Consolas, monospace; font-size:13px; box-sizing:border-box; }
  button { background:#4c8bf5; color:white; border:none; padding:10px 20px; border-radius:6px;
           font-size:14px; cursor:pointer; margin-top:12px; }
  button:hover { background:#3b78e0; }
  .msg { padding:10px 14px; border-radius:6px; margin-bottom:16px; font-size:14px; max-width:720px; }
  .msg.ok { background:#1f3a26; color:#8fdb9a; }
  .msg.err { background:#3a1f1f; color:#e2726a; }
  .hint { color:#79776f; font-size:12px; margin-top:8px; }
  a { color:#7fa9f0; }
</style>
</head>
<body>
<h1>Stream admin</h1>

{% if saved == "1" %}
<div class="msg ok">Saved. MediaMTX is restarting the pipeline now, give it a few seconds.</div>
{% elif saved == "error" %}
<div class="msg err">That doesn't look like a valid SDP file (expected it to start with "v="). Nothing was changed.</div>
{% elif saved == "norestart" %}
<div class="msg err">The SDP was saved, but the restart failed &mdash; couldn't run
  <code>docker compose restart</code>. Restart it by hand:
  <code>cd /opt/stream &amp;&amp; docker compose restart mediamtx</code></div>
{% endif %}

<div class="card">
  <h2>Status</h2>
  <div class="status-grid">
    <div>MediaMTX</div><div class="{{ 'ok' if status.mediamtx == 'running' else 'bad' }}">{{ status.mediamtx }}</div>
    <div>Path "{{ mtx_path }}"</div><div class="{{ 'ok' if status.path == 'publishing' else 'bad' }}">{{ status.path }}</div>
    <div>Viewers connected</div><div>{{ status.readers }}</div>
    <div>Bytes received</div><div>{{ status.bytes }}</div>
    <div>SDP last updated</div><div>{{ status.sdp_updated }}</div>
    <div>Player URL</div><div><a href="{{ player_url }}">{{ player_url }}</a></div>
  </div>
</div>

<div class="card">
  <h2>Configuration</h2>
  <form method="post" action="/save">
    <textarea name="sdp" spellcheck="false">{{ sdp }}</textarea>
    <div class="hint">Paste the full contents of the new SDP file, then save. The old file is kept as a .bak.</div>
    <button type="submit">Save &amp; restart</button>
  </form>
</div>

</body>
</html>
"""


def get_status():
    status = {
        "mediamtx": "unreachable",
        "path": "unknown",
        "readers": 0,
        "bytes": 0,
        "sdp_updated": "no file",
    }
    try:
        r = requests.get(f"{MEDIAMTX_API}/v3/paths/get/{MEDIAMTX_PATH}", timeout=2)
        status["mediamtx"] = "running"
        if r.ok:
            d = r.json()
            status["path"] = "publishing" if d.get("ready") else "not publishing"
            status["readers"] = len(d.get("readers", []))
            status["bytes"] = d.get("bytesReceived", 0)
        else:
            status["path"] = "path not found"
    except Exception:
        status["mediamtx"] = "unreachable"

    try:
        mtime = os.path.getmtime(SDP_PATH)
        status["sdp_updated"] = datetime.datetime.fromtimestamp(mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except FileNotFoundError:
        pass

    return status


def player_url():
    host = (request.host or "").split(":")[0]
    return f"http://{host}:8889/{MEDIAMTX_PATH}"


@app.route("/", methods=["GET"])
@requires_auth
def index():
    sdp_content = ""
    if os.path.exists(SDP_PATH):
        sdp_content = open(SDP_PATH).read()
    return render_template_string(
        TEMPLATE,
        status=get_status(),
        sdp=sdp_content,
        saved=request.args.get("saved"),
        mtx_path=MEDIAMTX_PATH,
        player_url=player_url(),
    )


@app.route("/healthz", methods=["GET"])
def healthz():
    """Unauthenticated liveness check, for monitoring. Reveals nothing."""
    return "ok\n", 200, {"Content-Type": "text/plain"}


@app.route("/save", methods=["POST"])
@requires_auth
def save():
    # Browsers submit textarea content with CRLF line endings regardless of
    # what was pasted in, so normalise before writing.
    new_sdp = request.form.get("sdp", "").replace("\r\n", "\n").replace("\r", "\n")
    new_sdp = new_sdp.strip() + "\n"

    if not new_sdp.startswith("v=") or "m=video" not in new_sdp:
        return redirect("/?saved=error")

    if os.path.exists(SDP_PATH):
        pathlib.Path(SDP_PATH + ".bak").write_text(open(SDP_PATH).read())

    # Truncate and rewrite in place. Must stay in place: the live file is
    # bind-mounted into the MediaMTX container by inode, and a rename would
    # leave the container looking at the old content.
    with open(SDP_PATH, "w", newline="\n") as f:
        f.write(new_sdp)

    # check=False only ignores a non-zero exit status — a missing docker binary
    # still raises. The file is already written at this point, so swallowing
    # that into a 500 would tell the operator the save failed when it didn't.
    try:
        subprocess.run(
            ["docker", "compose", "restart", COMPOSE_SERVICE],
            cwd=COMPOSE_DIR,
            check=False,
        )
    except OSError:
        app.logger.exception("could not restart %s", COMPOSE_SERVICE)
        return redirect("/?saved=norestart")

    return redirect("/?saved=1")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
