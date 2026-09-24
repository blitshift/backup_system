#!/bin/bash
# Install systemd units for backup system
# Run as root

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing systemd units..."

# Retired units: the weekly deep verify was replaced by the nightly
# `backup verify` (reads 1/7 of the data per night). Remove it from hosts
# installed before that. Idempotent: every step tolerates an absent unit.
# stop and disable are separate calls (not `disable --now`) because disable
# exits non-zero on the dangling symlinks left once the unit files are gone
# from the repo, which would skip the stop -- hence also the explicit rm.
for unit in backup-verify-deep.timer backup-verify-deep.service; do
    if [ -L "/etc/systemd/system/$unit" ] || [ -e "/etc/systemd/system/$unit" ]; then
        echo "  Removing retired $unit"
    fi
    systemctl stop "$unit" 2>/dev/null || true
    systemctl disable "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit" "/etc/systemd/system/timers.target.wants/$unit"
done

# Symlink unit files
for unit in backup.service backup.timer backup-verify.service backup-verify.timer; do
    ln -sf "$SCRIPT_DIR/$unit" "/etc/systemd/system/$unit"
    echo "  Linked $unit"
done

# Reload systemd
systemctl daemon-reload
systemctl reset-failed backup-verify-deep.service backup-verify-deep.timer 2>/dev/null || true

# Enable timers
systemctl enable --now backup.timer
systemctl enable --now backup-verify.timer

echo ""
echo "Timers enabled:"
systemctl list-timers 'backup*'
