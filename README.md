# set-clock

A small command-line utility that sets the real-time clock on an Ajazz AK820 Pro
keyboard (USB VID `0x0C45`, PID `0x8009`), running QMK firmware, over its Raw HID interface.

It locates the keyboard's raw interface by HID usage page/usage
(`0xFF60`/`0x61`) — not by interface number — then sends a time-set report.

## Prerequisites

You must have a QMK enabled firmware installed. You may clone [this repo](https://github.com/fpb/qmk_firmware/tree/ak820pro-rgbless) and build the firmware yourself (use branch `ak820pro-rgbless`). Or just look into the QMKFirmwares folder [over here](https://github.com/fpb/ajazz-ak820-pro).

The tool depends on [hidapi](https://github.com/libusb/hidapi) and uses `pkg-config` to find it.

| Platform | Install |
| --- | --- |
| **macOS** | `brew install hidapi pkg-config` |
| **Debian/Ubuntu** | `sudo apt install libhidapi-dev libhidapi-hidraw0 pkg-config` |
| **Fedora** | `sudo dnf install hidapi-devel pkgconf-pkg-config` |
| **Windows** (MSYS2/MinGW-w64) | `pacman -S mingw-w64-x86_64-hidapi mingw-w64-x86_64-pkg-config` |

On Linux the build prefers the **hidraw** backend (`hidapi-hidraw`). This is
required: the libusb backend cannot read HID usage info for a device whose
interfaces are claimed by the kernel HID driver (as a keyboard's are), so the
raw interface would never be found.

## Build

```sh
make          # builds ./set-clock  (set-clock.exe on Windows)
make clean
```

## Usage

```sh
./set-clock                       # set the clock to the current local time
./set-clock 2026-07-01T14:30:00   # set a specific time (YYYY-MM-DDTHH:MM:SS)
./set-clock --legacy              # talk to the default (non-VIA) firmware
./set-clock --list                # list all HID interfaces of the keyboard
```

On success it prints `reply: OK`.

### Firmware protocol (`--legacy`)

The `via` keymap enables VIA, which takes over the raw-HID endpoint, so the
clock-set command rides VIA's *custom-value* channel
(`id_custom_set_value`, channel `0x10`, value `0x01`). This is the **default**
mode of the tool.

The `default` keymap does not enable VIA and keeps the original bespoke raw-HID
command (`0x01` + payload). Use `--legacy` when talking to that firmware.

If a `set-clock` run reports `error` or `no reply`, try the other mode — the two
protocols are mutually exclusive per firmware build.

## Linux permissions

`hidraw` device nodes are root-only by default, so a non-root run may report
that the raw interface was not found. Either run with `sudo`, or install a udev
rule so the device is user-accessible:

```
# /etc/udev/rules.d/99-set-clock.rules
KERNEL=="hidraw*", ATTRS{idVendor}=="0c45", ATTRS{idProduct}=="8009", MODE="0666"
```

```sh
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then re-plug the keyboard.

## Troubleshooting

- **`Raw HID interface (0xFF60) not found`** — run `./set-clock --list`:
  - No lines at all → the keyboard isn't visible (check the USB connection; in a
    VM make sure the device is passed through to the guest).
  - Lines shown but all `usage_page=0x0000` → you're using the libusb backend;
    install/rebuild against `hidapi-hidraw` (see above).
  - Lines shown with real usages but none `0xFF60` → the firmware wasn't flashed
    with raw HID enabled.
