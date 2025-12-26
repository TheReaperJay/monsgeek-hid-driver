#!/bin/bash
set -e

INSTALL_DIR="/opt/monsgeek-hid"

echo "Installing MonsGeek HID driver..."

# Check for root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

# Create install directory
mkdir -p "$INSTALL_DIR"

# Copy files
cp monsgeek_hid.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/monsgeek_hid.py"

# Install service
cp monsgeek-hid.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable monsgeek-hid.service
systemctl start monsgeek-hid.service

echo "Installation complete!"
echo "Check status with: systemctl status monsgeek-hid.service"
