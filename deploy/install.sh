#!/usr/bin/env bash
#
# Provision the stream VM from a clean Ubuntu 24.04 install. Idempotent —
# re-running it upgrades config in place without losing the live SDP.
#
# Run it from a checkout of this repo, on the VM:
#
#   sudo ./deploy/install.sh --sdp ./unicats.sdp --source-ip 10.0.0.9
#
# Don't have the real SDP yet? Stand the stack up with a stub and paste the
# real one into the admin panel afterwards:
#
#   sudo ./deploy/install.sh --placeholder-sdp --no-firewall
#
# What it does:
#   1. installs docker + compose plugin, ufw, ethtool  (skip: --no-packages)
#   2. lays the stack down in /opt/stream
#   3. applies the UDP sysctl tuning
#   4. installs a unit that raises the NIC RX ring at boot
#   5. installs the admin panel as a systemd service in /opt/stream-admin
#   6. opens the firewall                               (skip: --no-firewall)
#   7. starts everything and runs deploy/check.sh
set -euo pipefail

STREAM_DIR=/opt/stream
ADMIN_DIR=/opt/stream-admin
SDP_SRC=""
USE_PLACEHOLDER=0
ADMIN_USER=admin
ADMIN_PASS=""
IFACE=""
SOURCE_IP=""
VIEWER_CIDR="any"
MGMT_CIDR="any"
DO_PACKAGES=1
DO_FIREWALL=1
WANT_HLS=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die()  { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sdp)             SDP_SRC="$2"; shift 2 ;;
    --placeholder-sdp) USE_PLACEHOLDER=1; shift ;;
    --admin-user)   ADMIN_USER="$2"; shift 2 ;;
    --admin-pass)   ADMIN_PASS="$2"; shift 2 ;;
    --iface)        IFACE="$2"; shift 2 ;;
    --source-ip)    SOURCE_IP="$2"; shift 2 ;;
    --viewer-cidr)  VIEWER_CIDR="$2"; shift 2 ;;
    --mgmt-cidr)    MGMT_CIDR="$2"; shift 2 ;;
    --no-packages)  DO_PACKAGES=0; shift ;;
    --no-firewall)  DO_FIREWALL=0; shift ;;
    --no-hls)       WANT_HLS=0; shift ;;
    -h|--help)      awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *)              die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

# ---------------------------------------------------------------- 1. packages
if [[ $DO_PACKAGES -eq 1 ]]; then
  step "Installing packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose-v2 ufw ethtool python3-venv curl
  systemctl enable --now docker
else
  step "Skipping package install (--no-packages)"
fi

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"

# ------------------------------------------------------------------ 2. stack
step "Laying down the stack in $STREAM_DIR"
mkdir -p "$STREAM_DIR"
install -m 0644 "$REPO_DIR/docker-compose.yml" "$STREAM_DIR/docker-compose.yml"
install -m 0644 "$REPO_DIR/mediamtx.yml"       "$STREAM_DIR/mediamtx.yml"

if [[ $WANT_HLS -eq 0 ]]; then
  sed -i 's/^hls: yes$/hls: no/' "$STREAM_DIR/mediamtx.yml"
  echo "    HLS fallback disabled in mediamtx.yml"
fi

# A placeholder SDP is identified by c=0.0.0.0, which no real deployment uses.
# check.sh looks for the same thing.
is_placeholder() {
  [[ -f "$1" ]] && grep -q '^c=IN IP4 0\.0\.0\.0[[:space:]]*$' "$1"
}

[[ -n "$SDP_SRC" && $USE_PLACEHOLDER -eq 1 ]] && \
  die "pass --sdp or --placeholder-sdp, not both"

# The SDP must exist as a *file* before the container starts. Docker creates a
# directory at a missing bind-mount source, and the pipeline then fails in a
# way that looks like an FFmpeg problem rather than a missing file.
if [[ -n "$SDP_SRC" ]]; then
  [[ -f "$SDP_SRC" ]] || die "--sdp: no such file: $SDP_SRC"
  grep -q '^v=' "$SDP_SRC" || die "--sdp: $SDP_SRC does not look like an SDP (no v= line)"
  if [[ -f "$STREAM_DIR/unicats.sdp" ]] && \
     ! cmp -s "$SDP_SRC" "$STREAM_DIR/unicats.sdp"; then
    cp -a "$STREAM_DIR/unicats.sdp" "$STREAM_DIR/unicats.sdp.bak"
    echo "    previous SDP kept as unicats.sdp.bak"
  fi
  install -m 0644 "$SDP_SRC" "$STREAM_DIR/unicats.sdp"
elif [[ $USE_PLACEHOLDER -eq 1 ]]; then
  # Never let --placeholder-sdp clobber a real one on a re-run — that would
  # take a working stream down and the operator would have asked for the
  # opposite of what happened.
  if [[ -f "$STREAM_DIR/unicats.sdp" ]] && ! is_placeholder "$STREAM_DIR/unicats.sdp"; then
    echo "    --placeholder-sdp ignored: $STREAM_DIR/unicats.sdp is a real SDP"
    echo "      and was left alone. Delete it first if you really want the stub."
  else
    install -m 0644 "$REPO_DIR/deploy/placeholder.sdp" "$STREAM_DIR/unicats.sdp"
    echo "    wrote the placeholder SDP — no source will be received yet"
  fi
elif [[ -f "$STREAM_DIR/unicats.sdp" ]]; then
  echo "    keeping the existing $STREAM_DIR/unicats.sdp"
else
  die "no SDP. Pass --sdp /path/to/unicats.sdp, or --placeholder-sdp to stand
       the stack up now and paste the real one into the admin panel later.
       Starting with no file at all would make docker create a directory at
       that path. unicats.sdp.example shows the shape; its c= line must be
       THIS VM's address."
fi

PLACEHOLDER_ACTIVE=0
is_placeholder "$STREAM_DIR/unicats.sdp" && PLACEHOLDER_ACTIVE=1

# Sanity-check the two things about this SDP that have bitten before.
if [[ $(grep -c '^m=video' "$STREAM_DIR/unicats.sdp") -gt 1 ]]; then
  echo "    note: SDP has more than one m=video section — expected for this"
  echo "          source. mediamtx.yml already pins FFmpeg to -map 0:v:0."
fi
# Skipped for the placeholder: its c= is 0.0.0.0 on purpose, so FFmpeg binds
# INADDR_ANY and waits quietly instead of crash-looping on a failed bind.
if [[ $PLACEHOLDER_ACTIVE -eq 0 ]]; then
  SDP_C=$(awk -F'IN IP4 ' '/^c=/ {print $2; exit}' "$STREAM_DIR/unicats.sdp" | tr -d '\r')
  if [[ -n "$SDP_C" ]] && ! ip -4 addr show | grep -qw "$SDP_C"; then
    echo "    WARNING: the SDP's c= address is $SDP_C, which is not an address on"
    echo "             this VM. The pipeline will sit at 0 fps silently. Fix the"
    echo "             c= line and repoint the sender at this VM."
  fi
fi

# ------------------------------------------------------------------ 3. sysctl
step "Applying UDP buffer tuning"
install -m 0644 "$REPO_DIR/deploy/99-stream.conf" /etc/sysctl.d/99-stream.conf
sysctl --system >/dev/null
echo "    rmem_max = $(sysctl -n net.core.rmem_max)"

# --------------------------------------------------------------- 4. NIC rings
step "Setting the NIC RX ring"
if [[ -z "$IFACE" ]]; then
  IFACE=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
fi
if [[ -z "$IFACE" ]]; then
  echo "    could not work out the interface; skipping. Re-run with --iface."
else
  sed "s/@IFACE@/$IFACE/g" "$REPO_DIR/deploy/nic-rxring.service" \
    > /etc/systemd/system/nic-rxring.service
  chmod 0644 /etc/systemd/system/nic-rxring.service
  systemctl daemon-reload
  systemctl enable --now nic-rxring.service >/dev/null 2>&1 || true
  echo "    $IFACE RX ring: $(ethtool -g "$IFACE" 2>/dev/null | awk '/^Current/{f=1} f&&/^RX:/{print $2; exit}')"
fi

# ------------------------------------------------------------------ 5. admin
step "Installing the admin panel in $ADMIN_DIR"
mkdir -p "$ADMIN_DIR"
install -m 0644 "$REPO_DIR/admin/app.py"           "$ADMIN_DIR/app.py"
install -m 0644 "$REPO_DIR/admin/requirements.txt" "$ADMIN_DIR/requirements.txt"
[[ -d "$ADMIN_DIR/venv" ]] || python3 -m venv "$ADMIN_DIR/venv"
"$ADMIN_DIR/venv/bin/pip" install -q --upgrade pip
"$ADMIN_DIR/venv/bin/pip" install -q -r "$ADMIN_DIR/requirements.txt"

install -m 0644 "$REPO_DIR/admin/stream-admin.service" \
  /etc/systemd/system/stream-admin.service

# Credentials go in a drop-in, not the unit: a redeploy overwrites the unit but
# leaves the override in place, so the password survives upgrades.
DROPIN_DIR=/etc/systemd/system/stream-admin.service.d
DROPIN="$DROPIN_DIR/10-local.conf"
mkdir -p "$DROPIN_DIR"
if [[ -n "$ADMIN_PASS" ]]; then
  GENERATED=0
elif [[ -f "$DROPIN" ]]; then
  ADMIN_PASS=""     # keep whatever is already there
  GENERATED=0
else
  ADMIN_PASS=$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64)
  GENERATED=1
fi

if [[ -n "$ADMIN_PASS" ]]; then
  {
    echo "[Service]"
    printf 'Environment=ADMIN_USER=%s\n' "$ADMIN_USER"
    printf 'Environment=ADMIN_PASS=%s\n' "$ADMIN_PASS"
  } > "$DROPIN"
  chmod 0600 "$DROPIN"
else
  echo "    keeping the existing admin credentials in $DROPIN"
fi

systemctl daemon-reload
systemctl enable stream-admin >/dev/null 2>&1 || true

# --------------------------------------------------------------- 6. firewall
if [[ $DO_FIREWALL -eq 1 ]]; then
  step "Configuring the firewall"
  FW_ARGS=(--sdp "$STREAM_DIR/unicats.sdp" --viewer-cidr "$VIEWER_CIDR" --mgmt-cidr "$MGMT_CIDR")
  [[ -n "$SOURCE_IP" ]] && FW_ARGS+=(--source-ip "$SOURCE_IP")
  [[ $WANT_HLS -eq 0 ]] && FW_ARGS+=(--no-hls)
  bash "$REPO_DIR/deploy/firewall.sh" "${FW_ARGS[@]}"
else
  step "Skipping firewall (--no-firewall)"
fi

# ------------------------------------------------------------------ 7. start
step "Starting services"
( cd "$STREAM_DIR" && docker compose up -d )
systemctl restart stream-admin

echo "    waiting for the pipeline to come up..."
sleep 8

VM_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
echo
echo "----------------------------------------------------------------"
echo " Player   http://${VM_IP:-<vm-ip>}:8889/live"
[[ $WANT_HLS -eq 1 ]] && \
echo " HLS      http://${VM_IP:-<vm-ip>}:8888/live   (fallback)"
echo " Admin    http://${VM_IP:-<vm-ip>}:8080/       (user: $ADMIN_USER)"
if [[ ${GENERATED:-0} -eq 1 ]]; then
echo " Password $ADMIN_PASS"
echo "          ^ generated, shown once. Change it with:"
echo "            sudo systemctl edit stream-admin"
fi
echo "----------------------------------------------------------------"

if [[ $PLACEHOLDER_ACTIVE -eq 1 ]]; then
  cat <<EOF

 NEXT STEP — the placeholder SDP is in place, so there is no video yet.
 Open the admin panel above, paste the real SDP into the textarea and save.
 Set its c= line to this VM's address (${VM_IP:-the VM's IP}) and make sure
 the sender is pointed here too.
EOF
  if [[ $DO_FIREWALL -eq 1 ]]; then
    PH_PORT=$(awk '/^m=video/ {print $2; exit}' "$STREAM_DIR/unicats.sdp")
    cat <<EOF

 Note: the firewall rule opened UDP $PH_PORT from the placeholder. If the real
 SDP uses a different port, re-run deploy/firewall.sh afterwards and remove
 the stale rule with: ufw delete allow $PH_PORT/udp
EOF
  fi
fi

bash "$REPO_DIR/deploy/check.sh" || true
