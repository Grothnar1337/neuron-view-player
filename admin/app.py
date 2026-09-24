import os
import hmac
import ipaddress
import subprocess
import datetime
import pathlib
from functools import wraps

from flask import Flask, jsonify, request, redirect, render_template_string, Response
import requests

SDP_PATH = os.environ.get("SDP_PATH", "/opt/stream/unicats.sdp")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/opt/stream")
COMPOSE_SERVICE = os.environ.get("COMPOSE_SERVICE", "mediamtx")
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997")
MEDIAMTX_PATH = os.environ.get("MEDIAMTX_PATH", "live")

# The browser reaches MediaMTX's WebRTC endpoint directly, on whatever hostname
# the operator used to open this panel — so only the port is configured here.
WEBRTC_PORT = int(os.environ.get("WEBRTC_PORT", "8889"))

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
  :root { --pad: clamp(16px, 2.4vw, 40px); --gap: clamp(14px, 1.5vw, 22px); }
  * { box-sizing: border-box; }
  body { background:#26282c; color:#e8e6e1; font-family: -apple-system, Segoe UI, sans-serif;
         margin:0; padding: var(--pad); }
  .page { max-width:1700px; margin:0 auto; }
  h1 { font-size: clamp(18px, 1.5vw, 22px); font-weight:600; margin:0 0 var(--gap); }
  h2 { font-size:15px; font-weight:600; color:#c7c5be; margin:0 0 12px; display:flex;
       align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  .card { background:#2f3136; border-radius:10px; padding: clamp(14px, 1.3vw, 22px) clamp(16px, 1.5vw, 24px); }

  /* Two columns on a wide screen, preview on the right. The preview sticks so
     it stays in view while the SDP box is scrolled. */
  .layout { display:grid; gap: var(--gap); align-items:start;
            grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); }
  .col { display:flex; flex-direction:column; gap: var(--gap); min-width:0; }
  @media (min-width: 1100px) { .col-side { position:sticky; top: var(--pad); } }
  /* Below that it stacks — preview first, since it is the thing you glance at. */
  @media (max-width: 1099px) {
    .layout { grid-template-columns: 1fr; }
    .col-side { order:-1; }
  }
  /* Stacked on a mid-width window, a full-bleed 16:9 preview is ~500px tall and
     pushes the status out of view. Cap it so both are on screen at once; on a
     phone it goes full width again, where there is no room to spare anyway. */
  @media (min-width: 620px) and (max-width: 1099px) {
    .video-wrap { max-width:640px; }
  }

  .status-grid { display:grid; grid-template-columns: minmax(100px, 150px) minmax(0, 1fr);
                 gap:8px 16px; font-size:14px; }
  .status-grid > div { overflow-wrap:anywhere; }
  .status-grid div:nth-child(odd) { color:#9a988f; }
  .ok { color:#7fd08a; }
  .bad { color:#e2726a; }
  .idle { color:#9a988f; }
  textarea { width:100%; min-height: clamp(180px, 26vh, 340px); background:#1e2023; color:#e8e6e1;
             border:1px solid #44464b; border-radius:6px; padding:12px;
             font-family: Consolas, monospace; font-size:13px; resize:vertical; }
  button { background:#4c8bf5; color:white; border:none; padding:10px 20px; border-radius:6px;
           font-size:14px; cursor:pointer; margin-top:12px; }
  button:hover { background:#3b78e0; }
  .msg { padding:10px 14px; border-radius:6px; margin-bottom: var(--gap); font-size:14px; }
  .msg.ok { background:#1f3a26; color:#8fdb9a; }
  .msg.err { background:#3a1f1f; color:#e2726a; }
  .hint { color:#79776f; font-size:12px; margin-top:8px; }
  a { color:#7fa9f0; }

  /* --- preview --- */
  .video-wrap { position:relative; background:#000; border-radius:6px; overflow:hidden;
                aspect-ratio:16/9; width:100%; }
  video { width:100%; height:100%; display:block; object-fit:contain; background:#000; }
  .overlay { position:absolute; inset:0; display:grid; place-items:center; text-align:center;
             padding:16px; background:#1a1c1f; color:#9a988f; font-size:14px; }
  .overlay[hidden] { display:none; }
  .overlay .big { display:block; color:#c7c5be; font-size:15px; font-weight:600; margin-bottom:4px; }
  .pill { font-size:11px; font-weight:600; letter-spacing:.06em; text-transform:uppercase;
          padding:3px 9px; border-radius:20px; background:#3a3d43; color:#9a988f; white-space:nowrap; }
  .pill.live { background:#1f3a26; color:#8fdb9a; }
  .pill.warn { background:#3a331f; color:#dbc78f; }
  .pill.err  { background:#3a1f1f; color:#e2726a; }
  .row { display:flex; align-items:center; gap:10px; }
  .linkbtn { background:none; border:1px solid #44464b; color:#c7c5be; padding:5px 12px;
             font-size:12px; margin:0; border-radius:5px; }
  .linkbtn:hover { background:#3a3d43; }
</style>
</head>
<body>
<div class="page">
<h1>Stream admin</h1>

{% if saved == "1" %}
<div class="msg ok">Saved. MediaMTX is restarting the pipeline now, give it a few seconds.</div>
{% elif saved == "error" %}
<div class="msg err">That doesn't look like a usable SDP. It needs to start with "v=", have an
  <code>m=video</code> line with a port, and an IPv4 <code>c=</code> line (unicast address or
  multicast group). Nothing was changed.</div>
{% elif saved == "norestart" %}
<div class="msg err">The SDP was saved, but the restart failed &mdash; couldn't run
  <code>docker compose restart</code>. Restart it by hand:
  <code>cd /opt/stream &amp;&amp; docker compose restart mediamtx</code></div>
{% endif %}

<div class="layout">

  <div class="col col-main">
    <div class="card">
      <h2>Status <span id="poll-state" class="pill">live</span></h2>
      <div class="status-grid">
        <div>MediaMTX</div><div id="s-mediamtx" class="idle">&hellip;</div>
        <div>Path "{{ mtx_path }}"</div><div id="s-path" class="idle">&hellip;</div>
        <div>Source</div><div id="s-source" class="idle">&hellip;</div>
        <div>Viewers connected</div><div id="s-readers">&hellip;</div>
        <div>Bytes received</div><div id="s-bytes">&hellip;</div>
        <div>Throughput</div><div id="s-rate" class="idle">measuring&hellip;</div>
        <div>SDP last updated</div><div id="s-sdp">&hellip;</div>
        <div>Player URL</div><div><a href="{{ player_url }}">{{ player_url }}</a></div>
      </div>
    </div>

    <div class="card">
      <h2>Configuration</h2>
      <form method="post" action="/save">
        <textarea id="sdp" name="sdp" spellcheck="false">{{ sdp }}</textarea>
        <div class="hint">Paste the full contents of the new SDP file, then save. Unicast or multicast,
          any port &mdash; nothing else needs changing. The old file is kept as a .bak.</div>
        <button type="submit">Save &amp; restart</button>
      </form>
    </div>
  </div>

  <div class="col col-side">
    <div class="card">
      <h2>Preview
        <span class="row">
          <span id="pill" class="pill">connecting</span>
          <button type="button" id="reconnect" class="linkbtn">Reconnect</button>
        </span>
      </h2>
      <div class="video-wrap">
        <video id="preview" autoplay muted playsinline></video>
        <div class="overlay" id="overlay"><span><span class="big" id="overlay-title">Connecting</span>
          <span id="overlay-detail">Negotiating with MediaMTX&hellip;</span></span></div>
      </div>
      <div class="hint">Live, straight from MediaMTX &mdash; the video goes direct to your browser and is
        not relayed through this page. Muted; there is no audio in this stream.</div>
    </div>
  </div>

</div>

<script>
const MTX_PATH   = {{ mtx_path|tojson }};
const MTX_PORT   = {{ webrtc_port|tojson }};
const WHEP_URL   = location.protocol + "//" + location.hostname + ":" + MTX_PORT + "/" + MTX_PATH + "/whep";
const POLL_MS    = 3000;

/* ------------------------------------------------------------------ status */
let prevBytes = null, prevAt = null;

function human(n) {
  if (n === null || n === undefined) return "-";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
}

function setText(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  if (cls !== undefined) el.className = cls;
}

async function poll() {
  try {
    const r = await fetch("api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    setText("poll-state", "live", "pill live");

    setText("s-mediamtx", d.mediamtx, d.mediamtx === "running" ? "ok" : "bad");
    setText("s-path", d.path, d.path === "publishing" ? "ok" : "bad");
    setText("s-readers", d.readers);
    setText("s-bytes", human(d.bytes) + " (" + d.bytes.toLocaleString() + ")");
    setText("s-sdp", d.sdp_updated);
    setText("s-source", d.source, d.source_warning ? "bad" : "");
    if (d.source_warning) document.getElementById("s-source").title = d.source_warning;

    /* Rate is derived here rather than server-side: a total that is large but
       static looks identical to a healthy stream in a single sample, and that
       is exactly the failure worth catching. */
    const now = Date.now();
    if (prevBytes !== null && now > prevAt) {
      const rate = (d.bytes - prevBytes) / ((now - prevAt) / 1000);
      setText("s-rate", rate > 0 ? human(rate) + "/s" : "no data arriving",
              rate > 0 ? "ok" : "bad");
    }
    prevBytes = d.bytes; prevAt = now;

    syncPlayer(d.path === "publishing");
  } catch (e) {
    setText("poll-state", "no contact", "pill err");
    setText("s-mediamtx", "admin panel unreachable", "bad");
  }
}

/* ------------------------------------------------------------- whep player */
let pc = null, resourceUrl = null, starting = false, wantPlaying = false;

function pill(text, cls) { setText("pill", text, "pill " + cls); }

function overlay(title, detail) {
  const o = document.getElementById("overlay");
  if (title === null) { o.hidden = true; return; }
  o.hidden = false;
  document.getElementById("overlay-title").textContent = title;
  document.getElementById("overlay-detail").textContent = detail || "";
}

function iceComplete(conn) {
  /* MediaMTX accepts a trickle-less offer, which avoids implementing the WHEP
     PATCH flow. On a LAN with no STUN this gathers host candidates in ms; the
     timeout is only a guard against a gathering state that never settles. */
  if (conn.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { clearTimeout(t); conn.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => { if (conn.iceGatheringState === "complete") done(); };
    const t = setTimeout(done, 3000);
    conn.addEventListener("icegatheringstatechange", check);
  });
}

async function startPlayer() {
  if (starting || pc) return;
  starting = true;
  pill("connecting", "warn");
  overlay("Connecting", "Negotiating with MediaMTX…");
  try {
    pc = new RTCPeerConnection({ iceServers: [] });
    pc.addTransceiver("video", { direction: "recvonly" });

    pc.ontrack = (e) => {
      document.getElementById("preview").srcObject = e.streams[0];
      overlay(null);
      pill("live", "live");
    };
    pc.onconnectionstatechange = () => {
      if (!pc) return;
      const s = pc.connectionState;
      if (s === "failed" || s === "disconnected" || s === "closed") {
        pill("dropped", "err");
        overlay("Connection lost", "Retrying…");
        stopPlayer();
        if (wantPlaying) setTimeout(() => { if (wantPlaying) startPlayer(); }, 2000);
      }
    };

    await pc.setLocalDescription(await pc.createOffer());
    await iceComplete(pc);

    const res = await fetch(WHEP_URL, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription.sdp,
    });
    if (!res.ok) throw new Error("WHEP returned " + res.status);

    /* MediaMTX does expose Location through CORS (verified against 1.x), so
       the DELETE on teardown normally works and frees the session at once.
       Guarded anyway: if a future version stops exposing it, the read returns
       null cross-origin, and closing the peer connection still drops the
       session — MediaMTX just reaps it on ICE timeout rather than instantly. */
    const loc = res.headers.get("Location");
    if (loc) resourceUrl = new URL(loc, WHEP_URL).href;

    await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
  } catch (err) {
    pill("failed", "err");
    overlay("Could not connect", String(err && err.message ? err.message : err));
    stopPlayer();
    if (wantPlaying) setTimeout(() => { if (wantPlaying) startPlayer(); }, 4000);
  } finally {
    starting = false;
  }
}

function stopPlayer() {
  if (resourceUrl) {
    fetch(resourceUrl, { method: "DELETE" }).catch(() => {});
    resourceUrl = null;
  }
  if (pc) { try { pc.close(); } catch (e) {} pc = null; }
  const v = document.getElementById("preview");
  if (v) v.srcObject = null;
}

function syncPlayer(publishing) {
  wantPlaying = publishing;
  if (publishing) {
    if (!pc && !starting) startPlayer();
  } else if (pc || starting) {
    stopPlayer();
    pill("no signal", "");
    overlay("No signal", "The path is not publishing. The preview starts on its own when it is.");
  } else {
    pill("no signal", "");
    overlay("No signal", "The path is not publishing. The preview starts on its own when it is.");
  }
}

/* --------------------------------------------------------------- lifecycle */
document.getElementById("reconnect").addEventListener("click", () => {
  stopPlayer();
  if (wantPlaying) startPlayer();
});

/* A hidden tab does not need a 12 Mbit/s video feed or a status poll. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPlayer();
    pill("paused", "");
    overlay("Paused", "Preview stops while this tab is in the background.");
  } else {
    prevBytes = null;
    poll();
  }
});

window.addEventListener("pagehide", stopPlayer);

poll();
setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
</script>
</div>
</body>
</html>
"""


def parse_sdp(text):
    """Pull out what decides how the stream is received: the first m=video port
    and the first IPv4 c= address. Neuron View puts c= inside the m= section
    (media level) and suffixes it, e.g. 10.0.0.50/32 or 239.1.1.1/16 (TTL), so
    the suffix is stripped. Returns (mode, addr, port); mode is one of
    "unicast", "multicast", "placeholder" (c=0.0.0.0) or None if unusable."""
    port = addr = None
    for line in text.splitlines():
        line = line.strip()
        if port is None and line.startswith("m=video"):
            fields = line.split()
            if len(fields) > 1 and fields[1].split("/")[0].isdigit():
                port = int(fields[1].split("/")[0])
        elif addr is None and line.startswith("c=IN IP4 "):
            addr = line[len("c=IN IP4 "):].split("/")[0].strip()
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return None, addr, port
    if port is None:
        return None, addr, port
    if str(ip) == "0.0.0.0":
        return "placeholder", addr, port
    return ("multicast" if ip.is_multicast else "unicast"), addr, port


def local_ipv4s():
    """This host's IPv4 addresses, or None if they can't be read."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {f.split("/")[0] for line in out.splitlines() for f in line.split() if "." in f}


def describe_source(sdp_text):
    """(label, warning) for the status panel."""
    mode, addr, port = parse_sdp(sdp_text)
    if mode is None:
        return "unreadable SDP", "no usable c= address / m=video port"
    if mode == "placeholder":
        return "placeholder (no source configured)", None
    label = f"{mode} {addr}:{port}"
    if mode == "unicast":
        local = local_ipv4s()
        if local is not None and addr not in local:
            return label, (
                f"{addr} is not an address on this server, so nothing will arrive. "
                "Point the sender's destination here."
            )
    return label, None


def get_status():
    status = {
        "mediamtx": "unreachable",
        "path": "unknown",
        "readers": 0,
        "bytes": 0,
        "sdp_updated": "no file",
        "source": "no file",
        "source_warning": None,
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
        status["source"], status["source_warning"] = describe_source(
            pathlib.Path(SDP_PATH).read_text()
        )
    except FileNotFoundError:
        pass

    return status


def player_url():
    host = (request.host or "").split(":")[0]
    return f"http://{host}:{WEBRTC_PORT}/{MEDIAMTX_PATH}"


@app.route("/", methods=["GET"])
@requires_auth
def index():
    sdp_content = ""
    if os.path.exists(SDP_PATH):
        sdp_content = open(SDP_PATH).read()
    return render_template_string(
        TEMPLATE,
        sdp=sdp_content,
        saved=request.args.get("saved"),
        mtx_path=MEDIAMTX_PATH,
        webrtc_port=WEBRTC_PORT,
        player_url=player_url(),
    )


@app.route("/api/status", methods=["GET"])
@requires_auth
def api_status():
    """Polled by the page every few seconds so the status can update without a
    reload — a reload would tear down the preview's WebRTC connection."""
    resp = jsonify(get_status())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/healthz", methods=["GET"])
def healthz():
    """Unauthenticated liveness check, for monitoring. Reveals nothing."""
    return "ok\n", 200, {"Content-Type": "text/plain"}


@app.route("/save", methods=["POST"])
@requires_auth
def save():
    # Browsers submit textarea content with CRLF line endings regardless of
    # what was pasted, so normalise before writing.
    new_sdp = request.form.get("sdp", "").replace("\r\n", "\n").replace("\r", "\n")
    new_sdp = new_sdp.strip() + "\n"

    if not new_sdp.startswith("v=") or parse_sdp(new_sdp)[0] is None:
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
