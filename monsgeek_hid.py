#!/usr/bin/env python3
"""
Userspace HID driver for MonsGeek M5W keyboard (wired mode)
Reads from hidraw, injects via uinput

Optimized for low latency:
- Pre-computed event structs
- Batched SYN_REPORT (one per HID report, not per key)
- 1ms polling when connected
- Zero-copy timestamp (kernel fills it in)
"""

import os
import sys
import time
import struct
import select
import glob
import fcntl

# Constants
EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0x00

# Pre-computed struct format for input_event (64-bit Linux)
# struct input_event { struct timeval time; u16 type; u16 code; s32 value; }
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# Pre-computed SYN_REPORT event (timestamp 0,0 - kernel fills it in)
SYN_EVENT = struct.pack(EVENT_FORMAT, 0, 0, EV_SYN, SYN_REPORT, 0)

# HID scancode to Linux keycode mapping
HID_TO_LINUX = {
    0x04: 30,   # A
    0x05: 48,   # B
    0x06: 46,   # C
    0x07: 32,   # D
    0x08: 18,   # E
    0x09: 33,   # F
    0x0a: 34,   # G
    0x0b: 35,   # H
    0x0c: 23,   # I
    0x0d: 36,   # J
    0x0e: 37,   # K
    0x0f: 38,   # L
    0x10: 50,   # M
    0x11: 49,   # N
    0x12: 24,   # O
    0x13: 25,   # P
    0x14: 16,   # Q
    0x15: 19,   # R
    0x16: 31,   # S
    0x17: 20,   # T
    0x18: 22,   # U
    0x19: 47,   # V
    0x1a: 17,   # W
    0x1b: 45,   # X
    0x1c: 21,   # Y
    0x1d: 44,   # Z
    0x1e: 2,    # 1
    0x1f: 3,    # 2
    0x20: 4,    # 3
    0x21: 5,    # 4
    0x22: 6,    # 5
    0x23: 7,    # 6
    0x24: 8,    # 7
    0x25: 9,    # 8
    0x26: 10,   # 9
    0x27: 11,   # 0
    0x28: 28,   # Enter
    0x29: 1,    # Escape
    0x2a: 14,   # Backspace
    0x2b: 15,   # Tab
    0x2c: 57,   # Space
    0x2d: 12,   # -
    0x2e: 13,   # =
    0x2f: 26,   # [
    0x30: 27,   # ]
    0x31: 43,   # backslash
    0x32: 43,   # non-US # (same as backslash)
    0x33: 39,   # ;
    0x34: 40,   # '
    0x35: 41,   # `
    0x36: 51,   # ,
    0x37: 52,   # .
    0x38: 53,   # /
    0x39: 58,   # Caps Lock
    0x3a: 59,   # F1
    0x3b: 60,   # F2
    0x3c: 61,   # F3
    0x3d: 62,   # F4
    0x3e: 63,   # F5
    0x3f: 64,   # F6
    0x40: 65,   # F7
    0x41: 66,   # F8
    0x42: 67,   # F9
    0x43: 68,   # F10
    0x44: 87,   # F11
    0x45: 88,   # F12
    0x46: 99,   # Print Screen
    0x47: 70,   # Scroll Lock
    0x48: 119,  # Pause
    0x49: 110,  # Insert
    0x4a: 102,  # Home
    0x4b: 104,  # Page Up
    0x4c: 111,  # Delete
    0x4d: 107,  # End
    0x4e: 109,  # Page Down
    0x4f: 106,  # Right
    0x50: 105,  # Left
    0x51: 108,  # Down
    0x52: 103,  # Up
    0x53: 69,   # Num Lock
    0x54: 98,   # Keypad /
    0x55: 55,   # Keypad *
    0x56: 74,   # Keypad -
    0x57: 78,   # Keypad +
    0x58: 96,   # Keypad Enter
    0x59: 79,   # Keypad 1
    0x5a: 80,   # Keypad 2
    0x5b: 81,   # Keypad 3
    0x5c: 75,   # Keypad 4
    0x5d: 76,   # Keypad 5
    0x5e: 77,   # Keypad 6
    0x5f: 71,   # Keypad 7
    0x60: 72,   # Keypad 8
    0x61: 73,   # Keypad 9
    0x62: 82,   # Keypad 0
    0x63: 83,   # Keypad .
    0x64: 86,   # non-US backslash
    0x65: 127,  # Compose
    0x66: 116,  # Power
    0x67: 117,  # Keypad =
}

# Modifier bit positions to Linux keycodes
MODIFIER_MAP = (
    (0, 29),   # Left Ctrl
    (1, 42),   # Left Shift
    (2, 56),   # Left Alt
    (3, 125),  # Left Meta (Super)
    (4, 97),   # Right Ctrl
    (5, 54),   # Right Shift
    (6, 100),  # Right Alt
    (7, 126),  # Right Meta
)

# Pre-compute key event templates (just need to fill in value)
def make_key_event(keycode, value):
    return struct.pack(EVENT_FORMAT, 0, 0, EV_KEY, keycode, value)


class MonsGeekHID:
    def __init__(self):
        self.uinput_fd = None
        self.hidraw_fd = None
        self.prev_modifiers = 0
        self.prev_keys = set()
        self.write_buffer = bytearray(EVENT_SIZE * 16)  # Pre-allocate buffer

    def find_hidraw(self):
        for hr in glob.glob("/dev/hidraw*"):
            try:
                base = os.path.basename(hr)
                uevent_path = f"/sys/class/hidraw/{base}/device/uevent"
                if os.path.exists(uevent_path):
                    with open(uevent_path) as f:
                        content = f.read()
                    if "3151" in content and "4015" in content:
                        if "2.4G" not in content and "4011" not in content:
                            return hr
            except:
                pass
        return None

    def setup_uinput(self):
        UI_SET_EVBIT = 0x40045564
        UI_SET_KEYBIT = 0x40045565
        UI_DEV_CREATE = 0x5501
        UI_DEV_SETUP = 0x405c5503
        BUS_USB = 0x03

        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)

        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)

        # Enable all keys
        all_keys = set(HID_TO_LINUX.values()) | {kc for _, kc in MODIFIER_MAP}
        for key in all_keys:
            fcntl.ioctl(fd, UI_SET_KEYBIT, key)

        # Setup device
        name = b"MonsGeek Virtual Keyboard"
        name = name + b'\x00' * (80 - len(name))
        setup = struct.pack("<HHHH", BUS_USB, 0x3151, 0x4015, 1) + name + struct.pack("<I", 0)

        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)

        time.sleep(0.3)
        self.uinput_fd = fd

    def emit_events(self, events):
        """Write multiple events + SYN_REPORT in one syscall"""
        if not events:
            return
        # Combine all events + SYN into single write
        data = b''.join(events) + SYN_EVENT
        os.write(self.uinput_fd, data)

    def process_hid_report(self, data):
        if len(data) < 8:
            return

        modifiers = data[0]
        keys = set(data[2:8]) - {0}
        events = []

        # Handle modifier changes
        for bit, keycode in MODIFIER_MAP:
            was_pressed = (self.prev_modifiers >> bit) & 1
            is_pressed = (modifiers >> bit) & 1
            if was_pressed != is_pressed:
                events.append(make_key_event(keycode, is_pressed))

        # Handle key releases (releases before presses for proper rollover)
        for hid_code in self.prev_keys - keys:
            if hid_code in HID_TO_LINUX:
                events.append(make_key_event(HID_TO_LINUX[hid_code], 0))

        # Handle key presses
        for hid_code in keys - self.prev_keys:
            if hid_code in HID_TO_LINUX:
                events.append(make_key_event(HID_TO_LINUX[hid_code], 1))

        # Emit all events with single SYN_REPORT
        self.emit_events(events)

        self.prev_modifiers = modifiers
        self.prev_keys = keys

    def release_all_keys(self):
        """Release all held keys on disconnect"""
        events = []
        for hid_code in self.prev_keys:
            if hid_code in HID_TO_LINUX:
                events.append(make_key_event(HID_TO_LINUX[hid_code], 0))
        for bit, keycode in MODIFIER_MAP:
            if (self.prev_modifiers >> bit) & 1:
                events.append(make_key_event(keycode, 0))
        self.emit_events(events)
        self.prev_modifiers = 0
        self.prev_keys = set()

    def connect_hidraw(self):
        hr = self.find_hidraw()
        if hr:
            try:
                self.hidraw_fd = os.open(hr, os.O_RDONLY | os.O_NONBLOCK)
                print(f"[OK] Connected to {hr}")
                self.prev_modifiers = 0
                self.prev_keys = set()
                return True
            except OSError as e:
                print(f"[ERR] Can't open {hr}: {e}")
        return False

    def disconnect_hidraw(self):
        if self.hidraw_fd is not None:
            try:
                os.close(self.hidraw_fd)
            except:
                pass
            self.hidraw_fd = None
            self.release_all_keys()

    def run(self):
        print("MonsGeek Userspace HID Driver (Optimized)")
        print("=" * 45)
        print("Switch to WIRED mode (Fn+U) now...")
        print("Press Ctrl+C to exit")
        print()

        self.setup_uinput()
        print("[OK] Virtual keyboard created")

        try:
            while True:
                if self.hidraw_fd is None:
                    if not self.connect_hidraw():
                        time.sleep(0.1)  # 100ms poll when disconnected
                        continue

                try:
                    # 1ms timeout for minimal latency when connected
                    r, _, _ = select.select([self.hidraw_fd], [], [], 0.001)
                    if r:
                        # Read all available data
                        while True:
                            try:
                                data = os.read(self.hidraw_fd, 64)
                                if len(data) >= 8:
                                    self.process_hid_report(data)
                            except BlockingIOError:
                                break
                except OSError as e:
                    if e.errno == 19:  # ENODEV
                        print("[!] Device disconnected, waiting for reconnect...")
                        self.disconnect_hidraw()
                    elif e.errno != 11:  # Not EAGAIN
                        raise

        except KeyboardInterrupt:
            print("\n[!] Shutting down...")
        finally:
            self.disconnect_hidraw()
            if self.uinput_fd is not None:
                try:
                    fcntl.ioctl(self.uinput_fd, 0x5502)  # UI_DEV_DESTROY
                    os.close(self.uinput_fd)
                except:
                    pass
            print("[OK] Cleanup complete")


if __name__ == "__main__":
    driver = MonsGeekHID()
    driver.run()
