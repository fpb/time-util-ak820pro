# ak820ctl — AK820 Pro host toolkit

Host-side tools for an **Ajazz AK820 Pro** running the custom
[QMK firmware](https://github.com/fpb/qmk_firmware) (USB VID `0x0C45`,
PID `0x8009`). One command-line program, **`ak820ctl`**, talks to the keyboard
over its Raw HID interface to:

- **set the LCD clock** (the external PCF8563 RTC), and
- **provision the LCD** — write pre-rendered image assets and GIF animations into
  the keyboard's external SPI flash.

It finds the keyboard's raw interface by HID usage page/usage (`0xFF60`/`0x61`),
not by interface number, then speaks VIA's *custom-value* framing on two
channels: `0x10` (clock) and `0x11` (flash).

The `assets/` directory holds the **asset-authoring pipeline** (source images +
the Python packers) so this repo is the complete "customise and provision my
keyboard" toolkit.

## What works on which firmware branch

The firmware repo has several branches; not every command applies to all of them.

| command | HID channel | supported branches |
| --- | --- | --- |
| `ak820ctl clock …` | `0x10` | `ak820pro-lcd-flash` (both backends), `ak820pro-lcd-embedded` |
| `ak820ctl info` / `flash …` (assets, animations) | `0x11` | **`ak820pro-lcd-flash` only** |

Flash provisioning is a feature of the **`ak820pro-lcd-flash`** branch, which keeps
the LCD art (and GIF animations) in the keyboard's external SPI flash — either
dashboard backend (`-e DASHBOARD_BACKEND=custom|qp`) exposes the `0x11` channel.
`ak820pro-lcd-embedded` embeds its art in the firmware image, so it has no
flash-write channel (clock only). The `ak820pro-dev-*` branches (rgb / lvgl
experiments) are not targets for this tool.

## Build

Depends on [hidapi](https://github.com/libusb/hidapi) via `pkg-config`.

| Platform | Install |
| --- | --- |
| **macOS** | `brew install hidapi pkg-config` |
| **Debian/Ubuntu** | `sudo apt install libhidapi-dev libhidapi-hidraw0 pkg-config` |
| **Fedora** | `sudo dnf install hidapi-devel pkgconf-pkg-config` |
| **Windows** (MSYS2/MinGW-w64) | `pacman -S mingw-w64-x86_64-hidapi mingw-w64-x86_64-pkg-config` |

On Linux the build prefers the **hidraw** backend (`hidapi-hidraw`): the libusb
backend cannot read HID usage info for a device whose interfaces are claimed by
the kernel HID driver (as a keyboard's are), so the raw interface would never be
found.

```sh
make            # builds ./ak820ctl  (ak820ctl.exe on Windows)
make clean
```

Python 3 (stdlib only — no Pillow/ffmpeg) is required for the asset/animation
packers in `assets/`.

## Setting the clock

```sh
./ak820ctl clock                       # set to the host's current local time
./ak820ctl clock 2026-07-01T14:30:00   # set a specific time (YYYY-MM-DDTHH:MM:SS)
./ak820ctl list                        # list the keyboard's HID interfaces
```

The LCD clock is an external PCF8563/D8563 RTC; the keyboard keeps time on its own
once set.

## Provisioning LCD assets (tiles branch only)

The dashboard art (splash, fonts, status icons) lives in external SPI flash on the
`tiles` firmware. You author it as PNGs, pack it into one blob, and write that
blob to the keyboard.

### 1. Author / edit the images

Source PNGs live in [`assets/`](assets/):

- `sonixqmk.png` — 128×128 boot splash.
- `apple_icon_24x24.png`, `windows_icon_24x24.png` — OS icons.
- `cable_icon_24x24.png`, `bluetooth_icon_24x24.png`, `2_4_g_icon_24x24.png` —
  connection icons.
- `Iosevka-Regular-30.png`, `Iosevka-Medium-20.png` — font atlases. Each glyph
  cell is marked by a magenta `(255,0,255)` pixel at its top-left corner, so the
  packer derives the grid, advance width and glyph count from the markers — no
  metrics file needed. (Regular-30: 95 glyphs @ 15×34; Medium-20: 95 @ 10×23,
  first char `0x20`.)

Everything is RGB565; colours are baked in. To change the dashboard, edit these
PNGs (keeping each image's dimensions).

### 2. Pack the blob

```sh
cd assets
python3 mkraw.py --flash      # -> flash_assets.bin  +  flash_assets.h
```

`mkraw.py --flash` produces two files:

- **`flash_assets.bin`** — the blob you flash (a 4 KB index sector then the assets,
  page-aligned; pixels stored **lo-byte-first** because the on-device DMA swaps
  each 16-bit word).
- **`flash_assets.h`** — the generated `ASSET_*` ids. **This is the one firmware
  coupling:** if you add/remove/reorder assets, copy this header into the firmware
  tree and rebuild:

  ```sh
  cp flash_assets.h \
     …/qmk_firmware/keyboards/a_jazz/ak820pro/graphics/res/flash_assets.h
  ```

  If you only changed image *pixels* (not the set of assets), the header is
  unchanged and no firmware rebuild is needed.

### 3. Write it to the keyboard

```sh
./ak820ctl info                                 # JEDEC id + writable base (sanity)
./ak820ctl flash write 0x0CE0000 assets/flash_assets.bin
```

`flash write` erases the needed sectors, streams the blob, then CRC32-verifies it
on the device against the file. `0x0CE0000` is the asset region (3.12 MB that has
been erased since manufacture, so it is always writable). Assets take effect on the
next boot.

## Provisioning a GIF animation (tiles branch only)

Toggle it on the keyboard with **Fn+Delete**.

```sh
cd assets
python3 mkanim.py myloop.gif -o myloop.bin      # GIF -> 128x128 RGB565 frames
cd ..
./ak820ctl flash write 0x540000 assets/myloop.bin --unlock
```

- `mkanim.py` decodes the GIF (LZW, interlace, frame disposal — stdlib only),
  scales/crops to 128×128 (`--fit cover|contain`), and emits the stock animation
  format (`count` byte, per-frame duration bytes, then frames at `0x8000` stride).
- The slot `0x540000` is a stock **animation** slot, so writing it needs
  `--unlock`. Up to **243 frames** fit.
- **Playback is a fixed ~100 ms/frame**, so a GIF authored at a different rate will
  play faster or slower than its original; `mkanim.py` prints both the source and
  playback durations.

`ak820ctl flash write` also accepts `erase <addr> [sectors]` and
`crc <addr> <len>` for manual work.

## Linux permissions

`hidraw` device nodes are root-only by default, so a non-root run may report the
raw interface as "not found". Either run with `sudo`, or install a udev rule:

```
# /etc/udev/rules.d/99-ak820ctl.rules
KERNEL=="hidraw*", ATTRS{idVendor}=="0c45", ATTRS{idProduct}=="8009", MODE="0666"
```

```sh
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then re-plug the keyboard.

## Troubleshooting

- **`Raw HID interface (0xFF60) not found`** — run `./ak820ctl list`:
  - No lines → the keyboard isn't visible (check USB; in a VM, pass the device
    through to the guest).
  - Lines with `usage_page=0x0000` → you're on the libusb backend; build against
    `hidapi-hidraw`.
  - Lines with real usages but none `0xFF60` → the firmware wasn't built with raw
    HID enabled.
- **`flash: refused`** — writing below the asset base needs `--unlock` (and only
  the stock animation slots are unlockable; the stock LCD-asset region is never
  writable). Also, flashing is refused while an animation is playing — toggle it
  off first.
- **`command not handled by firmware`** on a flash command → you're on a branch
  without the `0x11` channel; flash provisioning needs the `tiles` firmware.
