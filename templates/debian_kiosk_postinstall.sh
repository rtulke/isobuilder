#!/bin/bash
# templates/debian_kiosk_postinstall.sh
# Kiosk post-install setup — Chromium/Zabbix, LXDE, autologin
# Target: ge999.naz.ch
set -euo pipefail

# ============================================================
# Config
# ============================================================
KIOSK_USER="system"
KIOSK_URL="https://zabbix.naz.ch"
ZABBIX_USER="demo"
ZABBIX_PASS="demo"
WRAPPER_FILE="/home/${KIOSK_USER}/kiosk.html"

LIGHTDM_CONF="/etc/lightdm/lightdm.conf"
AUTOSTART_DIR="/home/${KIOSK_USER}/.config/lxsession/LXDE"
AUTOSTART_FILE="${AUTOSTART_DIR}/autostart"
UDEV_RULES_FILE="/etc/udev/rules.d/99-disable-input.rules"
SERVICE_FILE="/etc/systemd/system/kiosk.service"
LOGFILE="/var/log/kiosk-setup.log"

exec > >(tee -a "$LOGFILE") 2>&1
echo "=== Kiosk setup started: $(date) ==="

# ============================================================
# Sanity checks
# ============================================================
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: must be run as root" >&2
        exit 1
    fi
}

check_user() {
    if ! id "$KIOSK_USER" &>/dev/null; then
        echo "ERROR: user $KIOSK_USER does not exist" >&2
        exit 1
    fi
}

find_chromium() {
    CHROMIUM_BIN=$(command -v chromium || command -v chromium-browser || true)
    if [[ -z "$CHROMIUM_BIN" ]]; then
        echo "ERROR: chromium not found" >&2
        exit 1
    fi
    echo "INFO: chromium at $CHROMIUM_BIN"
}

# ============================================================
# LightDM autologin
# ============================================================
configure_lightdm() {
    [[ -f "$LIGHTDM_CONF" ]] && cp "$LIGHTDM_CONF" "${LIGHTDM_CONF}.bak"

    cat > "$LIGHTDM_CONF" <<EOF
[Seat:*]
autologin-user=${KIOSK_USER}
autologin-user-timeout=0
user-session=LXDE
EOF

    systemctl enable lightdm
    echo "INFO: lightdm configured for autologin"
}

# ============================================================
# SSH
# ============================================================
configure_ssh() {
    systemctl enable ssh
    systemctl start ssh || true
    echo "INFO: ssh enabled"
}

# ============================================================
# Zabbix auto-login wrapper
# Chromium loads this local HTML file first; JS auto-submits
# the Zabbix login form, landing directly on the dashboard.
# ============================================================
create_zabbix_wrapper() {
    mkdir -p "$(dirname "$WRAPPER_FILE")"

    cat > "$WRAPPER_FILE" <<EOF
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Loading...</title>
</head>
<body>
  <form id="loginform" method="POST" action="${KIOSK_URL}/index.php">
    <input type="hidden" name="name"      value="${ZABBIX_USER}">
    <input type="hidden" name="password"  value="${ZABBIX_PASS}">
    <input type="hidden" name="autologin" value="1">
    <input type="hidden" name="enter"     value="Sign in">
  </form>
  <script>document.getElementById('loginform').submit();</script>
</body>
</html>
EOF

    chown "${KIOSK_USER}:${KIOSK_USER}" "$WRAPPER_FILE"
    echo "INFO: Zabbix wrapper written to $WRAPPER_FILE"
}

# ============================================================
# LXDE autostart
# Chromium flags:
#   --kiosk                          fullscreen, no UI
#   --ignore-certificate-errors      accept self-signed SSL
#   --disable-infobars               no "Chrome is being controlled" bar
#   --disable-session-crashed-bubble no crash restore prompt
#   --incognito                      no local state / cookies persist
#   --app=file://...                 open wrapper as app (no address bar)
# ============================================================
configure_lxde_autostart() {
    mkdir -p "$AUTOSTART_DIR"

    cat > "$AUTOSTART_FILE" <<EOF
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0 -root
@${CHROMIUM_BIN} \
  --kiosk \
  --ignore-certificate-errors \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --no-first-run \
  --incognito \
  --app=file://${WRAPPER_FILE}
EOF

    chown -R "${KIOSK_USER}:${KIOSK_USER}" "/home/${KIOSK_USER}/.config"
    echo "INFO: LXDE autostart configured"
}

# ============================================================
# systemd kiosk service (fallback / restart on crash)
# ============================================================
create_kiosk_service() {
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Chromium Kiosk
After=graphical.target

[Service]
User=${KIOSK_USER}
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/${KIOSK_USER}/.Xauthority
ExecStart=${CHROMIUM_BIN} \
  --kiosk \
  --ignore-certificate-errors \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --no-first-run \
  --incognito \
  --app=file://${WRAPPER_FILE}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical.target
EOF

    systemctl daemon-reload
    systemctl enable kiosk.service
    echo "INFO: kiosk.service enabled"
}

# ============================================================
# Optional: disable physical input devices via udev
# WARNING: locks out local keyboard/mouse — SSH only!
# Uncomment if intentional.
# ============================================================
# disable_input_devices() {
#     cat > "$UDEV_RULES_FILE" <<EOF
# ACTION=="add", SUBSYSTEM=="input", ATTRS{name}=="*Keyboard*", \
#   RUN+="/bin/sh -c 'chmod 000 /dev/input/event*'"
# ACTION=="add", SUBSYSTEM=="input", ATTRS{name}=="*Mouse*", \
#   RUN+="/bin/sh -c 'chmod 000 /dev/input/event*'"
# EOF
#     echo "WARNING: input devices will be disabled after reboot"
# }

# ============================================================
# Self-disable this service after successful run
# ============================================================
cleanup() {
    systemctl disable postinstall.service || true
    echo "=== Kiosk setup finished: $(date) ==="
    echo "INFO: rebooting in 5s..."
    sleep 5
    reboot
}

# ============================================================
# Main
# ============================================================
check_root
check_user
find_chromium
configure_ssh
configure_lightdm
create_zabbix_wrapper
configure_lxde_autostart
create_kiosk_service
cleanup
