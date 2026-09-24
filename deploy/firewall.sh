#!/usr/bin/env bash
#
# ufw rules for the stream VM. Idempotent — safe to re-run.
#
#   TCP 8889  WebRTC signalling (WHEP) + the built-in player page   -> viewers
#   UDP 8189  WebRTC media, direct MediaMTX <-> viewer              -> viewers
#   TCP 8888  HLS fallback (optional)                               -> viewers
#   TCP 8080  admin panel                                           -> ops
#   UDP <rtp> RTP ingest, port read from the SDP                    -> source
#
# RTSP (8554) and the MediaMTX API (9997) are bound to loopback in
# mediamtx.yml and deliberately have no rule here.
#
# Optional, and NOT SDP-aware: the ingest port is read once, when this runs, so
# re-run it if a pasted SDP changes the port. It does not allow IGMP, which a
# multicast source needs under ufw. Skip the firewall (install.sh --no-firewall)
# if the VM sits on a network that already restricts access.
#
# Usage:
#   sudo ./firewall.sh [--sdp PATH] [--source-ip IP] [--viewer-cidr CIDR]
#                      [--mgmt-cidr CIDR] [--admin-port N] [--no-hls]
#                      [--no-enable] [--dry-run]
#
# Examples:
#   sudo ./firewall.sh --sdp /opt/stream/unicats.sdp --source-ip 10.0.0.9
#   sudo ./firewall.sh --source-ip 10.0.0.9 --viewer-cidr 10.0.4.0/24 \
#                      --mgmt-cidr 10.0.9.0/24
set -euo pipefail

SDP_PATH=/opt/stream/unicats.sdp
SOURCE_IP=""
VIEWER_CIDR="any"
MGMT_CIDR="any"
ADMIN_PORT=8080
WANT_HLS=1
DO_ENABLE=1
DRY_RUN=0

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sdp)          SDP_PATH="$2"; shift 2 ;;
    --source-ip)    SOURCE_IP="$2"; shift 2 ;;
    --viewer-cidr)  VIEWER_CIDR="$2"; shift 2 ;;
    --mgmt-cidr)    MGMT_CIDR="$2"; shift 2 ;;
    --admin-port)   ADMIN_PORT="$2"; shift 2 ;;
    --no-hls)       WANT_HLS=0; shift ;;
    --no-enable)    DO_ENABLE=0; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *)              die "unknown option: $1" ;;
  esac
done

# --dry-run prints the rules and touches nothing, so it needs neither root nor
# ufw — the point is to review the rules before they exist anywhere.
if [[ $DRY_RUN -eq 0 ]]; then
  [[ $EUID -eq 0 ]] || die "run as root (sudo)"
  command -v ufw >/dev/null || die "ufw not installed: apt install ufw"
fi

# The RTP ingest port is whatever the SDP's first m=video line says. Reading it
# from the file is the only way to be sure the rule matches the pipeline.
RTP_PORT=""
if [[ -f "$SDP_PATH" ]]; then
  RTP_PORT=$(awk '/^m=video/ {print $2; exit}' "$SDP_PATH" | tr -d '\r')
fi
if [[ -z "$RTP_PORT" ]]; then
  echo "warning: could not read an m=video port from $SDP_PATH;" >&2
  echo "         skipping the RTP ingest rule — add it by hand once you have" >&2
  echo "         the real SDP, or re-run this script then." >&2
fi

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "+ $*"
  else
    "$@"
  fi
}

# Keep SSH reachable first — enabling ufw without this locks you out of the VM.
run ufw allow from "$MGMT_CIDR" to any port 22 proto tcp comment 'ssh'

# Viewer-facing: WebRTC signalling + page, and the media itself.
run ufw allow from "$VIEWER_CIDR" to any port 8889 proto tcp comment 'mediamtx webrtc/whep + player page'
run ufw allow from "$VIEWER_CIDR" to any port 8189 proto udp comment 'mediamtx webrtc media'

if [[ $WANT_HLS -eq 1 ]]; then
  run ufw allow from "$VIEWER_CIDR" to any port 8888 proto tcp comment 'mediamtx hls fallback'
else
  run ufw --force delete allow from "$VIEWER_CIDR" to any port 8888 proto tcp 2>/dev/null || true
fi

# Admin panel.
run ufw allow from "$MGMT_CIDR" to any port "$ADMIN_PORT" proto tcp comment 'stream admin panel'

# RTP ingest, scoped to the source if we were told what it is.
if [[ -n "$RTP_PORT" ]]; then
  if [[ -n "$SOURCE_IP" ]]; then
    run ufw allow from "$SOURCE_IP" to any port "$RTP_PORT" proto udp comment 'rtp ingest from source'
  else
    echo "note: no --source-ip given, opening UDP $RTP_PORT to everyone." >&2
    echo "      Re-run with --source-ip once you know it, then remove the" >&2
    echo "      broad rule with: ufw delete allow $RTP_PORT/udp" >&2
    run ufw allow "$RTP_PORT"/udp comment 'rtp ingest (unscoped)'
  fi
fi

if [[ $DO_ENABLE -eq 1 ]]; then
  run ufw --force enable
fi

echo
[[ $DRY_RUN -eq 1 ]] || ufw status verbose
