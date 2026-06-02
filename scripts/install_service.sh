#!/bin/bash
# install_service.sh — Install moneymaker as a systemd service

set -e

SERVICE_FILE="$(dirname "$0")/../moneymaker.service"
DEST="/etc/systemd/system/moneymaker.service"

echo "Installing moneymaker service..."
sudo cp "$SERVICE_FILE" "$DEST"
sudo systemctl daemon-reload
sudo systemctl enable moneymaker
sudo systemctl start moneymaker

echo ""
echo "Done. Service status:"
sudo systemctl status moneymaker --no-pager
echo ""
echo "View logs with: journalctl -u moneymaker -f"