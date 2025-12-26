# MonsGeek M5W Linux USB Wired Mode Fix

A userspace HID driver that enables USB wired mode for the MonsGeek M5W keyboard on Linux.

## The Problem

The MonsGeek M5W keyboard has three connection modes:
- **Bluetooth** - Works, but has input lag
- **2.4GHz Wireless** - Works, but has input lag due to keystroke batching in firmware
- **USB Wired** - Does NOT work out of the box on Linux

When connected via USB cable (Fn+U to switch), the keyboard enumerates as device `3151:4015` with three HID interfaces:
- Interface 0: Standard keyboard (works fine)
- Interface 1: Extra functions (fails to probe)
- Interface 2: Extra functions (fails to probe)

The Linux `usbhid` driver attempts to probe all three interfaces. Interfaces 1 and 2 do not respond properly to standard HID initialization commands (`SET_IDLE`, `GET_DESCRIPTOR`), causing timeouts and `-32 (EPIPE)` or `-110 (ETIMEDOUT)` errors.

After approximately 7 seconds of failed probing, the kernel gives up and **disconnects the entire USB device**, including the working Interface 0.

## Why Standard Fixes Don't Work

| Approach | Result |
|----------|--------|
| udev rules to block interfaces 1/2 | Device stays connected but no keypresses detected |
| driver_override in udev | Doesn't prevent probe fast enough |
| HID quirks | `usbhid` is built into the kernel, can't set quirks at runtime |
| Kernel boot parameters | Requires reboot, inconsistent results |

## The Solution

This userspace HID driver bypasses the kernel's `usbhid` driver entirely:

1. **Reads directly from `/dev/hidraw*`** - The raw HID reports from Interface 0 are accessible via hidraw before the device disconnects
2. **Creates a virtual keyboard via `/dev/uinput`** - Injects translated keypresses into the Linux input subsystem
3. **Auto-reconnects** - When the physical device disconnects and reconnects (mode switch), the driver automatically picks it back up

The kernel's Interface 0 works for ~7 seconds before disconnect. This driver captures the hidraw data during that window and continues working through the virtual keyboard.

## Installation

1. Clone the repository and install:
```bash
git clone https://github.com/TheReaperJay/monsgeek-hid-driver.git
cd monsgeek-hid-driver
sudo ./install.sh
```

Or manually:
```bash
sudo mkdir -p /opt/monsgeek-hid
sudo cp monsgeek_hid.py /opt/monsgeek-hid/
sudo chmod +x /opt/monsgeek-hid/monsgeek_hid.py
sudo cp monsgeek-hid.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable monsgeek-hid.service
sudo systemctl start monsgeek-hid.service
```

2. Check status:
```bash
sudo systemctl status monsgeek-hid.service
```

## Uninstall

```bash
sudo systemctl stop monsgeek-hid.service
sudo systemctl disable monsgeek-hid.service
sudo rm /etc/systemd/system/monsgeek-hid.service
sudo rm -rf /opt/monsgeek-hid
sudo systemctl daemon-reload
```

## Usage

Once installed, the service runs automatically at boot. Simply:
1. Connect your MonsGeek M5W via USB cable
2. Press Fn+U to switch to wired mode
3. Type normally

The driver will detect the keyboard and create a virtual input device called "MonsGeek Virtual Keyboard".

## How It Works

```
Physical Keyboard (USB)
        │
        ▼
   ┌─────────┐
   │ hidraw  │  ← Raw HID reports (8 bytes per event)
   └────┬────┘
        │
        ▼
┌───────────────────┐
│ monsgeek_hid.py   │  ← Translates HID scancodes to Linux keycodes
└────────┬──────────┘
         │
         ▼
    ┌─────────┐
    │ uinput  │  ← Virtual keyboard device
    └────┬────┘
         │
         ▼
    Linux Input Subsystem → Applications
```

### HID Report Format

Standard HID keyboard reports are 8 bytes:
- Byte 0: Modifier keys (Ctrl, Shift, Alt, Meta as bit flags)
- Byte 1: Reserved
- Bytes 2-7: Up to 6 simultaneous key scancodes

The driver translates HID scancodes (e.g., `0x04` = A) to Linux keycodes (e.g., `30` = KEY_A).

## Files

- `monsgeek_hid.py` - The userspace HID driver
- `monsgeek-hid.service` - Systemd service file
- `README.md` - This file

## Troubleshooting

**Service not starting:**
```bash
sudo journalctl -u monsgeek-hid.service -f
```

**Check if virtual keyboard exists:**
```bash
cat /proc/bus/input/devices | grep -A4 "MonsGeek Virtual"
```

**Check if hidraw device is detected:**
```bash
ls -la /dev/hidraw*
cat /sys/class/hidraw/hidraw*/device/uevent | grep -A2 4015
```

**Manual test run:**
```bash
sudo python3 ~/monsgeek/monsgeek_hid.py
```

## Technical Details

- **Vendor ID:** 0x3151 (ROYUAN)
- **Product ID:** 0x4015 (MonsGeek Keyboard - Wired)
- **Product ID:** 0x4011 (MonsGeek Keyboard - 2.4GHz Dongle)
- **Kernel errors:** `-32 (EPIPE)`, `-110 (ETIMEDOUT)` on interfaces 1.1 and 1.2

## License

Public domain. Use at your own risk.
