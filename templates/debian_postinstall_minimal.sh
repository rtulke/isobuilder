#!/bin/bash
# templates/debian_postinstall_minimal.sh
# Minimal first-boot post-install stub — customize as needed
set -euo pipefail

LOGFILE="/var/log/postinstall.log"
exec > >(tee -a "$LOGFILE") 2>&1
echo "=== Post-install started: $(date) ==="

# ============================================================
# Sanity check
# ============================================================
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must be run as root" >&2
    exit 1
fi

# ============================================================
# Add your customization below
# Examples:
#   - Install additional packages
#   - Configure services
#   - Deploy config files
#   - Set up users / SSH keys
# ============================================================



# ============================================================
# Self-disable and finish
# ============================================================
systemctl disable postinstall.service || true
echo "=== Post-install finished: $(date) ==="
