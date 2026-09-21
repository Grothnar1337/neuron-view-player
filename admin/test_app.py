"""Smoke test for the admin panel. No MediaMTX and no docker required.

    cd admin
    python3 -m venv /tmp/t && /tmp/t/bin/pip install -q -r requirements.txt
    /tmp/t/bin/python test_app.py

Covers the things that would be embarrassing to get wrong: that auth actually
rejects, that a bad paste cannot destroy the live SDP, and that the file is
rewritten in place (the MediaMTX bind mount depends on the inode not changing).
"""

import base64
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
work = tempfile.mkdtemp(prefix="stream-admin-test-")
sdp = os.path.join(work, "unicats.sdp")
shutil.copy(HERE.parent / "unicats.sdp.example", sdp)

os.environ["SDP_PATH"] = sdp
os.environ["COMPOSE_DIR"] = work
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = "s3cret"
os.environ["MEDIAMTX_API"] = "http://127.0.0.1:59997"  # nothing there, on purpose

sys.path.insert(0, str(HERE))
import app as adminapp  # noqa: E402

adminapp.app.logger.disabled = True
c = adminapp.app.test_client()
ok = True


def check(label, cond, extra=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


def auth(u="admin", p="s3cret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


print("\nAuth")
check("no credentials -> 401", c.get("/").status_code == 401)
check("wrong password -> 401", c.get("/", headers=auth(p="nope")).status_code == 401)
check("wrong user -> 401", c.get("/", headers=auth(u="root")).status_code == 401)
check("empty credentials -> 401", c.get("/", headers=auth("", "")).status_code == 401)
check("correct credentials -> 200", c.get("/", headers=auth()).status_code == 200)
check("/healthz needs no auth", c.get("/healthz").status_code == 200)
check("/save needs auth", c.post("/save", data={"sdp": "v=0\nm=video 1 x\n"}).status_code == 401)

print("\nStatus page")
body = c.get("/", headers=auth()).get_data(as_text=True)
check("degrades gracefully with MediaMTX down", "unreachable" in body)
check("shows the current SDP", "m=video 5004" in body)
check("shows the player URL", ":8889/live" in body)

print("\nSave")
new = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 10.0.0.99\r\nt=0 0\r\nm=video 6000 RTP/AVP 96\r\n"
loc = c.post("/save", data={"sdp": new}, headers=auth()).headers.get("Location", "")
# 'norestart' is the expected outcome wherever docker is not on PATH.
check("valid SDP is accepted", "saved=1" in loc or "norestart" in loc, loc)
check("browser CRLF normalised to LF", b"\r" not in pathlib.Path(sdp).read_bytes())
check("new SDP written", "m=video 6000" in pathlib.Path(sdp).read_text())
check("previous SDP kept as .bak", "m=video 5004" in pathlib.Path(sdp + ".bak").read_text())

print("\nThe live SDP is not destroyable by a bad paste")
before = pathlib.Path(sdp).read_text()
loc = c.post("/save", data={"sdp": "not an sdp at all"}, headers=auth()).headers.get("Location", "")
check("garbage rejected", "saved=error" in loc)
check("garbage left the file alone", pathlib.Path(sdp).read_text() == before)
loc = c.post(
    "/save",
    data={"sdp": "v=0\no=- 0 0 IN IP4 1.2.3.4\ns=x\nt=0 0\nm=audio 5004 RTP/AVP 96\n"},
    headers=auth(),
).headers.get("Location", "")
check("SDP with no m=video rejected", "saved=error" in loc)
check("that left the file alone too", pathlib.Path(sdp).read_text() == before)

print("\nBind-mount requirement")
ino = os.stat(sdp).st_ino
c.post("/save", data={"sdp": new.replace("6000", "6002")}, headers=auth())
check("rewritten in place, inode unchanged", os.stat(sdp).st_ino == ino)

shutil.rmtree(work, ignore_errors=True)
print("\n" + ("ALL PASS" if ok else "FAILURES") + "\n")
sys.exit(0 if ok else 1)
