#!/usr/bin/env bash
#
# Post-deploy verification. Run it after install.sh, after any config change,
# and first thing when someone reports "the video is black".
#
#   ./deploy/check.sh
#
# Exits non-zero if anything critical is wrong. Everything it checks maps to a
# specific failure mode in docs/RUNBOOK.md.
set -uo pipefail

STREAM_DIR=${STREAM_DIR:-/opt/stream}
SDP_FILE=${SDP_FILE:-$STREAM_DIR/unicats.sdp}
MTX_API=${MTX_API:-http://127.0.0.1:9997}
MTX_PATH=${MTX_PATH:-live}

FAILED=0
pass() { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
wait_() { printf '  \033[36mWAIT\033[0m  %s\n' "$*"; }

# A placeholder SDP (written by install.sh --placeholder-sdp) is marked by
# c=0.0.0.0, which no real deployment uses. In that state "no video" is the
# expected condition, not a fault, so the source checks report WAIT and the
# script still exits 0 — everything else is genuinely verified.
PLACEHOLDER=0
if [[ -f "$SDP_FILE" ]] && grep -q '^c=IN IP4 0\.0\.0\.0[[:space:]]*$' "$SDP_FILE"; then
  PLACEHOLDER=1
fi

# Use for the checks that cannot pass until a real source is configured.
nosource() { if [[ $PLACEHOLDER -eq 1 ]]; then wait_ "$@"; else fail "$@"; fi; }

api_field() {
  # api_field <json> <dotted.key>  — no jq dependency, python3 is always here.
  # Lists come back as their length (that is what 'readers' is wanted for),
  # bools as lowercase so the comparisons below stay readable.
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
d = json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    d = d.get(k) if isinstance(d, dict) else None
if d is None:
    print("")
elif isinstance(d, bool):
    print("true" if d else "false")
elif isinstance(d, list):
    print(len(d))
else:
    print(d)
PY
}

# What the SDP asks for: unicast, or a multicast group (224.0.0.0/4). The
# /32 (or /TTL) suffix Neuron View appends is stripped.
SRC_ADDR=$(awk -F'IN IP4 ' '/^c=/ {print $2; exit}' "$SDP_FILE" 2>/dev/null | tr -d '\r' | cut -d/ -f1)
SRC_PORT=$(awk '/^m=video/ {print $2; exit}' "$SDP_FILE" 2>/dev/null | tr -d '\r')
SRC_MODE=unicast
_o=${SRC_ADDR%%.*}
if [[ "$_o" =~ ^[0-9]+$ ]] && (( _o >= 224 && _o <= 239 )); then SRC_MODE=multicast; fi

if [[ $PLACEHOLDER -eq 1 ]]; then
  echo
  printf '\033[36mPlaceholder SDP in use\033[0m — %s has c=0.0.0.0, so no\n' "$SDP_FILE"
  echo "source is expected yet. Everything below is checked except the source"
  echo "itself. Paste the real SDP into the admin panel to finish."
fi

echo
echo "Container"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx mediamtx; then
  pass "mediamtx container is up ($(docker ps --filter name=mediamtx --format '{{.Status}}'))"
else
  fail "mediamtx container is not running — cd $STREAM_DIR && docker compose up -d"
fi

# FFmpeg is launched inside the container by runOnInit. No ffmpeg process means
# the transcode died and MediaMTX has not restarted it yet. On a placeholder
# this is only WAIT: FFmpeg sits blocked probing a source that never arrives,
# and depending on the build it may instead exit and be restarted, so catching
# it mid-restart is not a fault worth going red over.
if docker top mediamtx 2>/dev/null | grep -q '[f]fmpeg'; then
  pass "ffmpeg transcode is running"
else
  nosource "no ffmpeg process in the container — check: docker logs --tail 50 mediamtx"
fi

echo
echo "Source"
if [[ $PLACEHOLDER -eq 1 ]]; then
  wait_ "placeholder — no source configured yet"
elif [[ $SRC_MODE == multicast ]]; then
  pass "multicast group $SRC_ADDR port ${SRC_PORT:-?}"
  # The join is the thing that most often goes wrong, and it is visible on the
  # host: if the group is not listed, FFmpeg never joined it.
  if ip maddr show 2>/dev/null | grep -qw "$SRC_ADDR"; then
    pass "group $SRC_ADDR is joined (see 'ip maddr show' for the NIC)"
  else
    nosource "group $SRC_ADDR is not in 'ip maddr' — FFmpeg has not joined it"
  fi
else
  pass "unicast to ${SRC_ADDR:-?} port ${SRC_PORT:-?}"
  if [[ -n "$SRC_ADDR" ]] && ! ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -qxF "$SRC_ADDR"; then
    fail "c= address $SRC_ADDR is not an address on this host — nothing will arrive"
  fi
fi

echo
echo "Pipeline"
JSON=$(curl -fsS --max-time 3 "$MTX_API/v3/paths/get/$MTX_PATH" 2>/dev/null)
if [[ -z "$JSON" ]]; then
  fail "MediaMTX API unreachable at $MTX_API — is 'api: yes' in mediamtx.yml?"
else
  pass "MediaMTX API is answering"
  READY=$(api_field "$JSON" ready)
  READERS=$(api_field "$JSON" readers)
  B1=$(api_field "$JSON" bytesReceived)
  if [[ "$READY" == "true" ]]; then
    pass "path '$MTX_PATH' is publishing"
  else
    nosource "path '$MTX_PATH' is not publishing — nothing is reaching the browser"
  fi

  # Bytes must be *moving*. A non-zero total with no movement means the sender
  # stopped or is pointed at the wrong IP, which looks identical to healthy
  # in a single sample.
  sleep 3
  J2=$(curl -fsS --max-time 3 "$MTX_API/v3/paths/get/$MTX_PATH" 2>/dev/null)
  B2=$(api_field "$J2" bytesReceived)
  if [[ -n "$B1" && -n "$B2" && "$B2" -gt "$B1" ]]; then
    pass "data is flowing ($(( (B2 - B1) / 3 / 1024 )) KiB/s over 3s)"
  elif [[ $PLACEHOLDER -eq 1 ]]; then
    wait_ "no data, as expected — the placeholder SDP points at no source"
  else
    if [[ $SRC_MODE == multicast ]]; then
      fail "bytesReceived is not increasing (${B1:-?} -> ${B2:-?}). The multicast
        group $SRC_ADDR is not arriving: is the sender transmitting to it, does
        the switch have IGMP snooping + a querier, is the group in 'ip maddr'
        on the right NIC (multi-NIC hosts join on the default route), and is
        rp_filter loose (sysctl net.ipv4.conf.all.rp_filter = 2)?"
    else
      fail "bytesReceived is not increasing (${B1:-?} -> ${B2:-?}). The source is
        not arriving: check the sender's destination IP, the SDP's c= line,
        and that nothing else has the RTP port bound (unicast: one receiver only)."
    fi
  fi
  echo "        viewers connected: ${READERS:-0}"
fi

echo
echo "Listeners"
for spec in "tcp:8889:webrtc signalling + player page" \
            "udp:8189:webrtc media" \
            "tcp:8080:admin panel"; do
  IFS=: read -r proto port label <<<"$spec"
  flag=$([[ $proto == tcp ]] && echo -lnt || echo -lnu)
  if ss $flag 2>/dev/null | grep -q ":$port "; then
    pass "$proto/$port  $label"
  else
    fail "$proto/$port  $label — nothing is listening"
  fi
done
if ss -lnt 2>/dev/null | grep -q ':8888 '; then
  pass "tcp/8888  hls fallback"
else
  warn "tcp/8888  hls fallback is off (fine unless you wanted it)"
fi

echo
echo "Host tuning"
RMEM=$(sysctl -n net.core.rmem_max 2>/dev/null || echo 0)
if [[ "$RMEM" -ge 134217728 ]]; then
  pass "net.core.rmem_max = $RMEM"
else
  warn "net.core.rmem_max = $RMEM (want 134217728) — sudo sysctl --system"
fi
IFACE=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
if [[ -n "$IFACE" ]] && command -v ethtool >/dev/null; then
  RX=$(ethtool -g "$IFACE" 2>/dev/null | awk '/^Current/{f=1} f&&/^RX:/{print $2; exit}')
  if [[ -n "$RX" && "$RX" -ge 4096 ]]; then
    pass "$IFACE RX ring = $RX"
  else
    warn "$IFACE RX ring = ${RX:-?} (want 4096) — systemctl start nic-rxring"
  fi
fi

echo
echo "Recent stream errors (last 200 log lines)"
MISSED=$(docker logs --tail 200 mediamtx 2>&1 | grep -c 'missed .* packets' || true)
if [[ "${MISSED:-0}" -eq 0 ]]; then
  pass "no RTP loss reported"
elif [[ "${MISSED:-0}" -lt 5 ]]; then
  pass "$MISSED RTP loss messages — occasional loss is expected on UDP"
else
  warn "$MISSED RTP loss messages. Real packet loss: check the network path
        (wired, same VLAN as the source) before touching any config."
fi

echo
echo "Services"
systemctl is-active --quiet stream-admin \
  && pass "stream-admin is active" \
  || fail "stream-admin is not active — journalctl -u stream-admin -n 50"

VM_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
echo
if [[ $FAILED -ne 0 ]]; then
  echo "Something is wrong — see docs/RUNBOOK.md for the matching fix."
elif [[ $PLACEHOLDER -eq 1 ]]; then
  echo "The stack is healthy and waiting for a real SDP. Paste it into the"
  echo "admin panel at http://${VM_IP:-<vm-ip>}:8080/ and re-run this check."
else
  echo "All good. Open http://${VM_IP:-<vm-ip>}:8889/$MTX_PATH in a browser."
fi
exit $FAILED
