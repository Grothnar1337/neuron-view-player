# Quick start — Neuron View stream player

Takes the unicast RTP stream from Neuron View and serves it as a plain web
page, so any browser — including the embedded one in the target product — can
be pointed at a URL and just show the video.

**Server: `10.144.40.160`**

| What | URL |
|---|---|
| **Video** | http://10.144.40.160:8889/live |
| **Admin panel** | http://10.144.40.160:8080/ |
| HLS fallback | http://10.144.40.160:8888/live |

Ask the stream owner for the admin panel login. Nothing else needs credentials
— the video URL opens for anyone on the network, which is what lets the
embedded browser use it.

---

## 1. Point Neuron View at the server

In Neuron View, set the unicast stream destination to **`10.144.40.160`**.

> **Only one receiver at a time.** This is a unicast stream, so exactly one
> program can listen on the destination port. If someone has VLC or ffmpeg
> open against it, they will fight the server for the port and nobody gets a
> picture. Close anything else before expecting this to work.

## 2. Load the SDP

Neuron View gives you an SDP describing the stream. Copy it straight out —
**it works as-is, no editing needed.** Load it here whenever it changes: new
stream, different port, different settings.

1. Open **http://10.144.40.160:8080/** and log in.
2. Paste the **whole** SDP into the big text box, replacing what's there.
3. Click **Save & restart**.
4. Wait about 10 seconds, then refresh the page.

The previous SDP is saved automatically as a backup, so a bad paste is not
a disaster.

> ### Worth one glance: the `c=` line
>
> Neuron View writes the *destination* into `c=`, so as long as the stream's
> destination is set to this server, it comes out correct on its own:
>
> ```
> c=IN IP4 10.144.40.160/32
> ```
>
> If it shows any other address, the destination is wrong **in Neuron View** —
> fix it there and copy the SDP again. Don't hand-edit this line: that would
> paper over a sender still pointed somewhere else, and no video would arrive
> regardless.
>
> Worth checking because it fails **silently**. The panel just sits at "not
> publishing" with zero bytes, looking exactly like a stream that never
> arrived.

## 3. Check it's working

On the admin panel:

| Field | Healthy |
|---|---|
| MediaMTX | `running` |
| Path "live" | `publishing` |
| Bytes received | **increasing** — refresh twice and compare |
| Viewers connected | number of open players |

"Bytes received" is the one that matters. A number that sits still means the
stream is not arriving, even if it isn't zero.

Then open http://10.144.40.160:8889/live in a normal browser before pointing
the real product at it.

## 4. Use it in the product

Point the embedded browser straight at:

```
http://10.144.40.160:8889/live
```

No iframe, no player page to build, no login. It is a bare video page.

If that product's browser shows nothing but a normal desktop browser works,
it probably has no WebRTC support — use the HLS fallback
(http://10.144.40.160:8888/live) instead. It works almost everywhere but runs
several seconds behind.

---

## If something's wrong

| Symptom | Most likely cause |
|---|---|
| Page loads, video is black | The stream's codec settings changed at source. Escalate. |
| "not publishing", bytes at 0 | Neuron View isn't sending here — check its destination, then re-copy the SDP |
| Bytes stuck at a non-zero number | Neuron View stopped sending, or something else grabbed the port |
| Was working, now nothing | Check nobody opened VLC against the stream |
| Picture tears or breaks up | Note the time and escalate — don't change settings |
| Nothing loads at all | Server may be down — escalate |

Anything not on this list, or anything needing a config change, goes to
whoever owns the server. Please don't edit the FFmpeg settings — they are
tuned and easy to break in ways that look like a network fault.

## For whoever owns the server

- [README.md](README.md) — architecture and deployment
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — full symptom-to-fix guide
- Health check, on the server: `sudo ./deploy/check.sh`
