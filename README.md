# remote-gadget

Control a Raspberry Pi (or any Linux board with a USB device controller) that is
plugged into another computer as a **USB keyboard + USB drive**, over `ssh`.

Use it to get files onto or off a machine with no network, or to type into BIOS/UEFI
setup screens, installers and login prompts.

```console
$ remote-gadget write firmware.bin      # copy into the gadget's drive
wrote 1048576 bytes to /firmware.bin
$ remote-gadget plug --ro               # target sees a (read-only) drive + keyboard
lab-pi: plugged read-only
$ remote-gadget type --enter 'fs0:\firmware-update.efi'
typed 26 characters
$ remote-gadget unplug
lab-pi: unplugged
```

## How it works

The client runs `ssh`/`scp` to the gadget host (the Pi). There, a small stdlib-only
Python helper, uploaded by `init`, builds a USB gadget through configfs with:

* a **mass-storage** function backed by a FAT image file, and
* a **boot-protocol keyboard** (HID).

`unplug` detaches the gadget from the USB device controller, which turns off the
data-line pull-up. The target sees the device removed, exactly as if the cable had been
pulled out. `plug` reattaches it, and the target enumerates it again and rereads the drive.
Files are copied in and out of the image with `mtools`, so no mounting is needed.

See [docs/design.md](docs/design.md) for the full design.

## Requirements

* **Client:** Python ≥ 3.10, OpenSSH `ssh` and `scp`. Key-based (non-interactive) login
  to the gadget host.
* **Gadget host:** a board with a USB device/OTG port: Pi Zero / Zero W / Zero 2 W,
  Pi 4 (USB-C), Pi 5, or other Linux with configfs and a UDC. It needs `python3`
  (3.11+ recommended, 3.9 works) and passwordless `sudo` (or a root login with `nosudo`).
  Raspberry Pi OS is the tested platform. `init` installs `mtools` and `dosfstools` on
  Debian-family systems if you ask it to.
* A USB **data** cable, plugged into the Pi's device port. On the Zero, that is the port
  marked "USB", not "PWR".

## Installation

From a checkout:

```console
$ uv tool install .        # or: pipx install .
```

## Configuration

Profiles live in INI files: `./.rgadget`, `~/.rgadget` and `/etc/rgadget`, in that
order of precedence. Each profile is taken whole from the first of these files that
defines it; settings are not merged between files.

The easiest way to create or change a profile is the `config` command:

```console
$ remote-gadget config -p lab-pi --host rgadget.local --keymap gb --ro   # new, in ./.rgadget
$ remote-gadget config -p lab-pi --update --size 128 --unset keymap      # change it
$ remote-gadget config -p lab-pi --overwrite --host other.local          # replace it
$ remote-gadget config -p lab-pi --user --host rgadget.local             # in ~/.rgadget
```

An existing profile is edited where it is found (in `./.rgadget` or `~/.rgadget`), and needs
`--update` or `--overwrite`. Comments in the file are kept. You can also edit the files
by hand:

```ini
[default]
profile = lab-pi

[lab-pi]
host     = rgadget.local      ; required
username = pi                 ; default: whatever ssh config says
key      = ~/.ssh/id_gadget   ; default: ssh's choice
nosudo   = false              ; true if the ssh user is root
path     = /var/lib/remote-gadget
size     = 64                 ; MiB, used when init creates the image
keymap   = us                 ; any XKB layout on the Pi, e.g. gb, de, fr, de:nodeadkeys
readonly = false              ; default for `plug`
; vendor_id = 0x1d6b          ; optional USB IDs (targets identify by class anyway)
; product_id = 0x0104
```

The profile is chosen by `--profile`, then `$RGADGET_PROFILE`, then
`[default] profile`, and otherwise the only profile in the file. Every setting can be
overridden on the command line (`--host`, `--keymap`, `--nosudo`, ...). With no config
file at all, `--host` is enough.

## Usage

```
remote-gadget [global options] <command> [options]
```

| Command | What it does |
|---------|--------------|
| `config -p NAME [settings] [--update\|--overwrite] [--user] [--unset KEY]` | Create or change a profile (see Configuration). |
| `init [--install-packages] [--reboot] [--reformat -y]` | Set up the Pi: dwc2 overlay, packages, state dir, storage image. Safe to re-run. Exit status 4 means a reboot is needed. |
| `doctor [--fix]` | Check connectivity, sudo, packages, overlay, UDC and image, and fix what it can. |
| `state` | Show `uninitialised`, `stopped`, `unplugged` or `plugged` (with `read-only`/`read-write` and whether the target has configured the device), plus free space. |
| `plug [--ro\|--rw]` | Connect to the target, optionally with the drive write-protected. |
| `unplug` | Disconnect from the target. |
| `write LOCAL [REMOTE]` | Copy a file into the drive. Refused while plugged. `REMOTE` defaults to `/<name>`; a trailing `/` means a directory. |
| `read REMOTE [LOCAL] [-f]` | Copy a file out of the drive. Warns if plugged read-write. |
| `ls [DIR]` | List the drive. |
| `rm [-r] REMOTE` | Delete a file or empty directory (`-r` deletes a directory tree). Refused while plugged. |
| `type [TEXT\|-] [--enter] [--timeout S] [--delay MS]` | Type text (from stdin if TEXT is omitted). |
| `reset` | Send Ctrl-Alt-Del. |

Global options include `-q/--quiet` (only warnings and errors are printed) and
`--color auto|always|never`. `NO_COLOR` is respected.

**Exit status:** 0 success · 1 the command failed · 2 configuration or usage error ·
3 ssh connection failure · 4 done, but a reboot is required · 130 interrupted.

### Sharing the drive with the target

`unplug` is the same as pulling out the cable. If the target has the drive mounted
read-write, **eject it on the target first**, or anything the target has not flushed yet is
lost (and the filesystem may be left inconsistent). Plugging in with `--ro` avoids the
problem when the target only needs to read.

### Typing

All the text is translated with the keymap **before** anything is sent, so a character
that the layout cannot type fails the whole command before any keystroke is sent. If the
target stops reading the keyboard for `--timeout` seconds, for example because it is
asleep, `type` stops and prints exactly what was not typed. The keymap must match the
layout the *target* is using. `us` is built in. Other layouts come from `ckbcomp`
(Debian's `console-setup`) on the Pi. Characters that need dead keys are not supported.

### First-time setup

```console
$ remote-gadget --host rgadget.local init --install-packages --reboot
$ remote-gadget --host rgadget.local doctor
```

## Library use

```python
from remote_gadget import Config, Gadget, TypingTimeoutError

g = Gadget(Config.load(profile="lab-pi"), on_warning=print)
g.unplug()
g.write("fw.bin", "/fw.bin")
g.plug(ro=True)
try:
    g.type("hello\n")
except TypingTimeoutError as e:
    print("not typed:", e.unsent)
print(g.state())
```

## Development

```console
$ uv sync                     # creates .venv with the dev tools
$ uv run pytest               # unit + helper tests
$ uv run tox -p auto          # lint, mypy, py310-py313, and the helper on py3.9
$ RGADGET_TEST_HOST=rgadget.local uv run pytest tests/integration
```

The integration tests plug, unplug and write to the gadget's drive, but never type or
send Ctrl-Alt-Del.

## License

MIT © Nicko van Someren
