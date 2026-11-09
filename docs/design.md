# remote-gadget — Detailed Design

Status: draft, 2026-11-08 · Author: Nicko van Someren <nicko@nicko.org> · License: MIT

This document replaces the original outline, `DESIGN.md`, which has been removed.
Where it differs from that outline, §12 says how and why.

---

## 1. Purpose and scope

`remote-gadget` is a command line tool, also usable as a Python library. It controls a
small Linux board, usually a Raspberry Pi, that is plugged into a **target** computer
over USB and acts as a USB *gadget*. The gadget appears to the target as:

* a **USB mass-storage device**, backed by a FAT image file on the Pi, and
* a **USB keyboard** (HID boot keyboard).

The user's machine (the **client**) drives the Pi (the **gadget host**) over `ssh`/`scp`.
Typical uses: getting files onto or off a machine that has no network, and typing into
BIOS/UEFI setup screens, installers or login prompts.

### Terminology

| Term | Meaning |
|------|---------|
| client | The machine running `remote-gadget`. |
| gadget host | The Pi (or other Linux board) that runs in USB device mode. |
| target | The computer the gadget is plugged into, which acts as the USB host. |
| UDC | USB Device Controller on the gadget host (`/sys/class/udc/*`). |
| image | The FAT filesystem image file that backs the mass-storage function. |
| started | The configfs gadget exists on the gadget host but is not bound to the UDC. |
| plugged | The gadget is bound to the UDC, so the target can see it. |

### Out of scope

* Mouse/absolute-pointer HID, network gadgets (ECM/RNDIS), serial gadgets.
* Restoring the gadget automatically after the gadget host reboots (see §6.3).
* Non-FAT storage images.
* Windows as a client platform. It may work, but it is not tested.

---

## 2. Requirements summary

| # | Requirement |
|---|-------------|
| R1 | Subcommands: `init`, `state`, `doctor`, `write`, `read`, `plug`, `unplug`, `type`, `reset`. |
| R2 | Config is INI-style profiles, merged from `./.rgadget`, `~/.rgadget` and `/etc/rgadget` (nearer files win). |
| R3 | Every config value can be overridden on the command line. |
| R4 | Short commands with sensible defaults; terse output; `--quiet` prints only warnings and errors. |
| R5 | Exit status is 0 on success and non-zero on failure; colour is used only on a colour-capable TTY. |
| R6 | `init` sets up the machine. It is idempotent and normally needed only once per gadget host. |
| R7 | `plug`/`unplug` start the gadget if needed on an initialised host. |
| R8 | `write` is refused while plugged; `read` while plugged warns that the data may be inconsistent. |
| R9 | `type` is synchronous with a timeout and reports any text that was not delivered. |
| R10 | Keymaps: US by default. Any layout installed on the gadget host can be used. |
| R11 | Supports Pi Zero/Zero W/Zero 2 W, Pi 4 (USB-C), Pi 5 and any Linux with configfs and a UDC. |
| R12 | `doctor` diagnoses problems, optionally fixes them, and says when its answers are unreliable (for example on a non-Debian or non-Pi host). |
| R13 | The client is a normal Python package (src-layout, `pyproject.toml`, `uv`). Python ≥ 3.10 on the client. The remote helper targets 3.11, and is 3.9-compatible where that costs little. |
| R14 | When unplugged, the gadget's data-line pull-up is off, so the target sees no device. When plugged again, the target enumerates it as new and rereads the storage. |
| R15 | `plug` can show the storage to the target as read-only. |
| R16 | The USB vendor and product IDs can optionally be configured. |

---

## 3. Architecture overview

```
┌──────────────────────── client ────────────────────────┐        ┌──────────── gadget host ────────────┐
│                                                        │  ssh   │                                     │
│  cli.py ──► api.py (Gadget) ──► transport.py ──────────┼───────►│  rgadget-helper (python3, stdlib)   │
│     │            │                 (ssh/scp subprocess)│  scp   │     │                               │
│  output.py    config.py                                │───────►│     ├─ configfs  /sys/kernel/config │
│                  │                                     │        │     ├─ UDC       /sys/class/udc     │
│              profiles from                             │        │     ├─ /dev/hidg0  (keyboard)       │
│        ./.rgadget ~/.rgadget /etc/rgadget              │        │     ├─ image.img  (via mtools)      │
└────────────────────────────────────────────────────────┘        │     └─ keymaps (ckbcomp / builtin)  │
                                                                  └─────────────────────────────────────┘
```

Two cooperating programs:

1. **Client package** `remote_gadget`. This handles the CLI, config, ssh/scp transport, the
   high-level API and output. It installs no software on the gadget host apart from the helper.
2. **Remote helper** `rgadget-helper`. This is a single stdlib-only Python 3 file shipped as
   package data and uploaded to `<path>/bin/rgadget-helper` by `init`. It does everything that
   touches the kernel on the gadget host. The client never sends raw shell snippets.
   It runs the helper with arguments quoted by `shlex.quote`, and passes bulk data (text to
   type) on stdin.

### 3.1 Why a remote helper (alternatives considered)

| Option | Pros | Cons |
|--------|------|------|
| **Uploaded Python helper** (chosen) | Real error handling and structured results; HID report generation and keymap parsing are straightforward; testable with a fake sysfs root. | Needs `python3` on the gadget host (present on Raspberry Pi OS and most Debian images); helper version must be kept in step with the client. |
| Pure shell over ssh | Nothing installed on the gadget host. | Fragile quoting; poor error reporting; HID byte generation in shell is unpleasant; hard to unit test. |
| Long-running daemon on the gadget host | Fast; could queue keystrokes. | Needs a service to manage; more to secure; not needed for synchronous typing. |

### 3.2 Why mtools for file transfer (alternatives considered)

| Option | Pros | Cons |
|--------|------|------|
| **`mtools` on the gadget host** (chosen) | No mount, no root for file operations; works on the image file directly. | FAT only; needs the `mtools` package. |
| Loop-mount with sudo | Any filesystem. | Needs root; stale mounts if interrupted; a mounted image could be exported to the target by accident. |
| Copy the whole image back and forth | Simple gadget-host side. | Slow; needs mtools on the client (awkward on macOS). |

---

## 4. Client package layout

```
pyproject.toml
src/remote_gadget/
    __init__.py         # exports Gadget, Config, errors, __version__
    __main__.py         # python -m remote_gadget
    cli.py              # argparse; maps subcommands to Gadget methods; exit codes
    config.py           # profile loading, merging, validation, CLI overrides
    transport.py        # Transport protocol + SshTransport (ssh/scp via subprocess)
    api.py              # Gadget class: init/state/doctor/write/read/plug/unplug/type/reset
    output.py           # Reporter: levels, colour detection, --quiet
    errors.py           # exception hierarchy
    remote/
        __init__.py
        helper.py       # the remote helper; stdlib only, runs on the gadget host
tests/
    unit/               # config, cli, api against FakeTransport, output
    helper/             # helper.py against a temp-dir fake sysfs/configfs/dev
    integration/        # opt-in, real gadget host via RGADGET_TEST_PROFILE
docs/design.md
```

Entry point: `[project.scripts] remote-gadget = "remote_gadget.cli:main"`.

The client uses the standard library only (`argparse`, `configparser`, `subprocess`,
`shlex`, `json`). There are no runtime dependencies. Dev dependencies are pinned in
`pyproject.toml`: `pytest`, `tox`, `ruff`, `mypy`.

`helper.py` lives inside the package so that it is linted, type-checked and tested like
the rest of the code. It must import nothing outside the standard library, and must
target **Python 3.11** (Raspberry Pi OS Bookworm). Where it costs little, it also keeps
to syntax that runs on **3.9** (Bullseye): `from __future__ import annotations`, no
`match`, no `X | Y` unions at runtime, no `tomllib`, no `ExceptionGroup`/`except*`. Ruff's
`target-version = "py39"` applies to `helper.py` alone, so the linter catches newer
syntax. The client itself needs 3.10+. The client uploads
its exact bytes using `importlib.resources`.

---

## 5. Configuration

### 5.1 Files and merging

The files are read in order of increasing precedence: `/etc/rgadget`, `~/.rgadget`, then
`./.rgadget`. `configparser.read()` is used with the list in that order, so a key in a
nearer file overrides the same key in the same section of a farther file. Missing files
are skipped. A file that exists but cannot be parsed or read is a fatal config error
(exit 2), and the message names the file and line.

`--config FILE` replaces the search list with that single file.

### 5.2 Format

```ini
[default]
profile = lab-pi

[lab-pi]
host     = pi-gadget.local
username = pi
key      = ~/.ssh/id_ed25519_gadget
nosudo   = false
path     = /var/lib/remote-gadget
size     = 64
keymap   = us

[uk-box]
host   = 10.0.0.42
keymap = gb
```

| Key | Type | Default | Validation |
|-----|------|---------|------------|
| `host` | str | — (**required**) | Non-empty; no whitespace; must not start with `-` (prevents ssh option injection). |
| `username` | str | unset (the ssh config decides) | Matches `^[a-z_][a-z0-9_.-]*$` (case-insensitive). |
| `key` | path | unset | `~` expanded; must exist locally and be readable. |
| `nosudo` | bool | `false` | `configparser` boolean values. |
| `path` | abs path | `/var/lib/remote-gadget` | Absolute; no `..` components; no whitespace or shell metacharacters. |
| `size` | int (MiB) | `64` | 8 ≤ size ≤ 32768 (the FAT32 range that mkfs.vfat and the targets handle comfortably). |
| `keymap` | str | `us` | `layout` or `layout:variant`, `[A-Za-z0-9_-]+`. |
| `readonly` | bool | `false` | Default for `plug`: show the storage to the target as write-protected (§6.2). |
| `vendor_id` | hex u16 | `0x1d6b` | `0x0001`–`0xffff`. Optional: targets find keyboards and drives by interface class, so most users never set this. |
| `product_id` | hex u16 | `0x0104` | `0x0001`–`0xffff`. As for `vendor_id`. |

* The `[default]` section may contain only `profile`. Any other key there is a config error.
  (The section name means `configparser` must be built with `default_section` set to a
  sentinel, so `[default]` is not treated as `DEFAULTS`.)
* Unknown keys in a profile produce a warning, not an error. This allows files to be
  shared with later versions.
* **Profile selection:** `--profile NAME`, then `$RGADGET_PROFILE`, then `[default] profile`.
  If none of these is set and the merged config has exactly one profile, that profile is
  used. Otherwise it is an error.
* If no config files exist, `--host` alone is enough to run.

### 5.3 Command-line overrides

These global options override the profile: `--host`, `--username/-u`, `--key/-i`,
`--sudo/--nosudo`, `--path`, `--size`, `--keymap`, `--vendor-id`, `--product-id`
(`readonly` is overridden by `plug --ro/--rw`). They are validated with the same rules
as the config file. The result is an immutable `Config` dataclass.

---

## 6. Gadget-host design

### 6.1 Filesystem layout under `path`

```
<path>/
    bin/rgadget-helper    # uploaded helper, run as `python3 <file>` (no exec bit needed)
    image.img             # FAT image backing the mass-storage LUN
    staging/              # scp landing area for read/write; emptied after each operation
    keymaps/              # cached parsed keymaps (§9.3)
    state.json            # {"initialised": true, "size": 64, "udc": "...",
                          #  "unplugged_at": <UTC timestamp or null>}
```

The default `path` is `/var/lib/remote-gadget`. The FHS reserves `/var/lib` for
persistent state that a program keeps between runs. It survives reboots, unlike `/run`
and (on some images) `/tmp`, and it is on the root filesystem, unlike the FAT `/boot`
partition. `init` creates `path` with sudo, unless `nosudo` is set, and then chowns it to
the ssh user. All later file operations run as that user without sudo.

### 6.2 USB gadget (configfs)

The gadget is built directly through configfs/`libcomposite`. The older `g_multi` module
cannot be reconfigured at runtime.

```
/sys/kernel/config/usb_gadget/rgadget/
    idVendor  <vendor_id>   idProduct <product_id>   (default: Linux Foundation "Multifunction Composite Gadget")
    bcdDevice 0x0100   bcdUSB 0x0200
    strings/0x409/{manufacturer,product,serialnumber}   ("remote-gadget", "remote-gadget", from machine-id)
    configs/c.1/  MaxPower 250, strings/0x409/configuration
    functions/hid.usb0/          protocol 1, subclass 1, report_length 8, report_desc = boot keyboard
    functions/mass_storage.usb0/ stall 1, lun.0/{file=<path>/image.img, removable 1, ro 0|1}
    configs/c.1/hid.usb0 -> functions/hid.usb0
    configs/c.1/mass_storage.usb0 -> functions/mass_storage.usb0
    UDC                          ("" = unplugged, "<udc name>" = plugged)
```

**Lifecycle states** (as reported by `state`):

| State | Condition |
|-------|-----------|
| `uninitialised` | `<path>/state.json` is missing or invalid. |
| `stopped` | Initialised, but the gadget directory does not exist in configfs (for example after a reboot). |
| `unplugged` | The gadget exists and `UDC` is empty. |
| `plugged` | `UDC` is set. `state` also shows `ro` or `rw`. |

**Start** (an internal step, not a subcommand) brings a host from `stopped` to `unplugged`.
It runs `modprobe libcomposite` and then builds the configfs tree. Start needs root and is
idempotent: an existing directory is checked and left alone if its contents match.
`plug` and `unplug` both start the gadget first when it is `stopped`.

#### What "unplugged" must mean electrically

The main requirement is that **while unplugged, the target sees no device at all**: the
gadget's D+ (or D−) pull-up is off, exactly as if the cable had been pulled out. Then,
when the gadget is plugged in again, the target treats it as a new device. It enumerates
it again and rereads the storage from scratch, so it never serves stale cached sectors
from before a `write`.

This design gets that behaviour by binding and unbinding the configfs gadget:

* `unplug` writes `""` to `UDC`. The kernel UDC core calls `usb_gadget_disconnect()` and
  then unbinds the function drivers. On dwc2 (all Pi models) `usb_gadget_disconnect()`
  sets `DCTL.SftDiscon`, which removes the pull-up. The target sees an ordinary
  disconnect.
* `plug` writes the UDC name to `UDC`. The UDC core binds the gadget and calls
  `usb_gadget_connect()`, which turns the pull-up back on. The target sees a new attach
  and enumerates the device from the start.

Alternatives that were rejected:

| Mechanism | Why not |
|-----------|---------|
| `lun.0/forced_eject` (media change only) | The device stays on the bus. Many hosts, and most firmware/UEFI, keep cached state or ignore media-change events, and the keyboard is not affected at all. It does not satisfy the requirement. |
| `/sys/class/udc/<udc>/soft_connect` on its own | It controls the same pull-up, but it only works while the gadget is bound, and it leaves the functions active. Unbinding gives the same electrical result and also leaves the gadget in a known state. |
| Unloading the kernel modules | Slow, needs the configfs tree rebuilt, and gives no extra benefit. |

**Checking it worked:** after unbinding, `unplug` checks that `UDC` reads back as empty.
If not, it is an error, so a failed unplug is never reported as success. It also reads
`/sys/class/udc/<udc>/state`. If that does not say `not attached`, it gives a
**warning**, not an error, because some UDC drivers do not update this file on unbind.
Whether dwc2 does is something to check on real hardware (§13). If it does, the warning
can become an error for dwc2. Before writing to `UDC`, the helper reads it, so that it
knows the starting state and `plug`/`unplug` are idempotent.

**Minimum time unplugged:** `unplug` saves `unplugged_at` in `state.json`. If `plug` runs
within **1 s** of that time, it sleeps for the rest of the second first. This means the
target's hub has always polled and seen the disconnect, even after a very quick
`unplug; plug`. The 1 s value is a constant in the helper, chosen so that a host
polling its hub port every few hundred milliseconds cannot miss the disconnect.

**Pi-side caching:** while unplugged, the LUN keeps `image.img` open, but no I/O goes
through the gadget. `mtools`, run by the helper, works on the same file through the same
page cache, so the gadget sees every `write` as soon as it is plugged again. No extra
flush is needed on the Pi.

#### Read-only plug

`plug --ro` (or `readonly = true` in the profile, with `plug --rw` to override it) shows
the storage to the target as write-protected. The kernel refuses to change `lun.0/ro`
while the LUN has a file open (`EBUSY`). So while unbound, `plug` does this:
1. if the current `ro` value differs from the one wanted: write `""` to `lun.0/file`,
   write `ro`, then write the image path back to `lun.0/file`;
2. bind the UDC.

What changes when the gadget is plugged read-only:
* `read` does not warn, because the target cannot change the image.
* `write` is still refused, because the target may have cached the old contents, so the
  user must still `unplug` first.
* The keyboard is not affected.

**UDC selection:** use the single entry in `/sys/class/udc`. If there are several, use the
`udc` value saved in `state.json` by `init`. If there are none, report "no USB device
controller; dwc2 overlay not active?" and suggest `doctor`.

**Host detection:** `/sys/class/udc/<udc>/state` is meaningful only while the gadget is
bound. When plugged, `state` shows it: `configured` means the target has enumerated the
gadget, while `not attached`, `default` and `suspended` mean it has not. When unplugged,
the target's presence is reported as *unknown*. On a bus-powered Pi Zero, VBUS sensing is
not reliable, so the tool does not try it.

### 6.3 Machine setup done by `init`

`init` sets up the machine; it does not start the gadget. It is idempotent, and each step
first checks whether it is already done.

1. Check ssh login and sudo (`sudo -n true`), unless `nosudo` is set.
2. Detect the platform: `/etc/os-release` (Debian family?) and `/proc/device-tree/model`
   (Pi model).
3. **Pi only:** make sure `dtoverlay=dwc2,dr_mode=peripheral` is set in the `[all]` section
   of the boot config. The file is `/boot/firmware/config.txt` (Bookworm and later), or
   `/boot/config.txt` on older releases. The edit appends a fresh `[all]` section, so earlier
   board-specific sections (the stock file has `dtoverlay=dwc2,dr_mode=host` under `[cm5]`)
   cannot override it, and a timestamped backup is made first. No `modules-load.d` entry is
   needed: the overlay loads `dwc2`, and `plug`/`unplug` load `libcomposite`. If the overlay was added, `init` reports **reboot required** and exits with status 4.
   `--reboot` reboots the Pi straight away.
   On non-Pi hosts, `init` changes no boot configuration and just checks that a UDC exists.
4. Check that the required packages are installed (`mtools`, `dosfstools`, `python3`).
   On Debian, `apt-get install -y` is offered when `--install-packages` is given. It
   waits up to 10 minutes for another apt/dpkg run's lock (`DPkg::Lock::Timeout`).
   Otherwise `init` lists what is missing and fails.
5. Create the `path` directories and upload the helper.
6. Create `image.img` (sparse, `size` MiB) and run `mkfs.vfat -n RGADGET` on it, **only if it
   does not exist**. If it exists with a different size, `init` warns and keeps the image.
   Replacing it requires `--reformat`, which destroys the image's contents and asks for
   confirmation unless `--yes` is given.
7. Write `state.json`.

### 6.4 Helper versioning

The helper has a `HELPER_VERSION` constant (the package version plus a content hash).
Every client call passes `--expect-version V`. On a mismatch, the helper exits with a
reserved code (§7.3). The client then re-uploads the helper (as the user, without sudo)
and retries once. This keeps the promise that `init` is needed only once per machine.

### 6.5 Privilege model

* Commands that need root: `modprobe`, configfs writes, boot-config edits, `apt-get`,
  `reboot`, creating `path`.
* Without `nosudo`, the client runs `sudo -n <path>/bin/rgadget-helper ...`. A password
  prompt causes an immediate failure with a hint, never an interactive hang.
* With `nosudo`, the helper runs directly. Permission errors are turned into clear messages.
* Commands that need no root, such as image access through mtools, are run by the helper
  as the ssh user without sudo.

---

## 7. Client ↔ helper protocol

### 7.1 Invocation

```
ssh [opts] <user@host> [sudo -n] python3 <path>/bin/rgadget-helper --expect-version V --path <path> <op> [args]
ssh [opts] <user@host> [sudo -n] python3 - --path <path> {setup|doctor} [args]   # helper source on stdin
```

* `op` is one of: `ping`, `setup`, `doctor`, `plug`, `unplug`, `state`, `img-put`,
  `img-get`, `img-ls`, `img-rm`, `staging-clean`, `type`, `reset`.
* `setup` and `doctor` are run in **bootstrap** mode: the helper's source is piped to
  `python3 -`, so they work before the helper is installed (or when `path` is broken).
* The version `V` is the first 16 hex digits of the SHA-256 of the helper file. The helper
  hashes its own file, so the two cannot drift.
* Text for `type` is sent on stdin as UTF-8. Nothing that the user wrote ever becomes part
  of a shell command line except through `shlex.quote`.

ssh options are always `-o BatchMode=yes -o ConnectTimeout=10`, plus `-i key` and
`-l username` when they are set. There is one ssh process per helper call. Connection reuse
through `ControlMaster` is a possible future optimisation and is not part of the design.

### 7.2 Results

The helper writes exactly one JSON object to stdout:

```json
{"ok": true, "result": {...}, "warnings": ["..."]}
{"ok": false, "error": {"code": "not_initialised", "message": "...", "hint": "run `remote-gadget init`"}}
```

Diagnostics go to stderr. The client passes them through only with `--verbose`.

### 7.3 Helper exit codes

| Code | Meaning |
|------|---------|
| 0 | ok |
| 1 | operation failed (details in the JSON) |
| 2 | bad arguments |
| 10 | version mismatch, so the client re-uploads |
| 11 | not root (sudo needed) |

ssh's own exit code 255 means a connection failure. The client maps it to
`SshConnectionError`. An ssh exit of 127, or "No such file" from `python3`, means the
helper is missing and is reported as `NotInitialisedError`.

---

## 8. Commands

Global options: `-p/--profile`, `--config`, the config overrides (§5.3), `-q/--quiet`,
`-v/--verbose`, `--color {auto,always,never}`, `--version`.

| Command | Behaviour |
|---------|-----------|
| `init [--reboot] [--install-packages] [--reformat] [--yes]` | §6.3. Prints one line per step: done, already OK, or changed. |
| `state` | Prints the lifecycle state (§6.2), the UDC name, the target status (when plugged), the image size and free space (via `minfo`/`mdir`), and the keymap. Exit 0 if `state` worked, whatever the gadget's state is. |
| `doctor [--fix]` | §8.1. |
| `write LOCAL [REMOTE]` | Refused if plugged (exit 1, suggests `unplug`). REMOTE defaults to `/<basename LOCAL>`; a REMOTE ending in `/` means a directory. Copies by `scp` to `staging/`, then `mcopy -o` into the image (`mmd` creates missing parent directories). LOCAL must be a regular readable file; directories are not supported in v1. Staging is cleaned up on success and on failure. |
| `read REMOTE [LOCAL]` | When plugged read-write, warns that the target may have unflushed writes and the copy may be inconsistent, then continues. `mcopy` to `staging/`, then `scp` back. LOCAL defaults to `./<basename REMOTE>`. An existing local file is not overwritten without `-f`. |
| `ls [REMOTE_DIR]` | (Added.) Lists the image contents with `mdir`, because `read` is hard to use without it. |
| `rm [-r] REMOTE` | (Added.) Deletes from the image. Refused while plugged, because the target caches the FAT and changing it underneath would corrupt the drive. Tries `mdel` (files only), then `mrd` (empty directories; a non-empty one fails with a hint to use `-r`), or `mdeltree` with `-r`. Refuses `/`. |
| `plug [--ro \| --rw]` | Starts the gadget if `stopped`, sets the LUN read-only or read-write (§6.2), waits out the minimum time unplugged, then binds the UDC, which turns the pull-up on. Fails with a hint if the host is `uninitialised`. If already plugged with a different `ro` setting, it fails with a hint to `unplug` first (it never re-plugs on its own). |
| `unplug` | Starts the gadget if `stopped`, so that the end state is a well-defined `unplugged`, then unbinds the UDC, which turns the pull-up off so the target sees the device removed (§6.2). Saves `unplugged_at`. |
| `type [TEXT \| -] [--enter] [--timeout S] [--delay MS]` | §9. With no TEXT, or with `-`, reads stdin. Nothing is added to the text unless `--enter` is given, because a stray Enter at a BIOS menu selects something. Needs `plugged` and target state `configured`. |
| `reset` | Sends Ctrl-Alt-Del: report `05 00 4C 00 00 00 00 00` followed by the all-zero release report. Same preconditions as `type`. |

### 8.1 `doctor`

Each check gives a status of **ok**, **warn**, **fail** or **unknown**. It also says whether
the tool can fix the problem, and if not, gives a one-line suggestion for the user.

| Check | Fixable by `--fix` |
|-------|--------------------|
| Host resolves and the TCP port answers | no |
| ssh login works non-interactively (BatchMode) | no; suggests key or agent setup |
| `sudo -n` works (unless `nosudo`) | no; suggests NOPASSWD sudoers entry |
| `python3` present: ≥ 3.11 ok, 3.9–3.10 warn (best effort), < 3.9 fail | no |
| `mtools` and `dosfstools` installed | yes (apt, Debian only) |
| dwc2 overlay in the boot config (Pi only) | yes; reboot needed |
| configfs mounted and `libcomposite` loaded or loadable (`modprobe -n`) | yes (mount / modprobe) |
| configfs mounted at `/sys/kernel/config` | yes (mount) |
| A UDC is present | no; explains overlay/port/cable (data cable, correct port on Pi Zero) |
| `path` exists and is writable by the user | yes |
| Helper version matches | yes (re-upload) |
| The image exists and is a valid FAT (`minfo`) | no (suggests `init --reformat`) |
| The gadget's configfs tree matches the expected one | yes (rebuild while unplugged) |

**Reliability:** if `/etc/os-release` does not show a Debian-family system, then every
check that depends on the distribution (packages, boot config, module config) reports
**unknown**, and not ok or fail. The summary line says
"N checks could not be verified on this platform". The checks for a non-Pi model work
the same way.

---

## 9. Keyboard and keymaps

### 9.1 HID

The keyboard is a boot-protocol keyboard with 8-byte reports:
`[modifiers, 0, k1..k6]`. Each character is sent as a press report followed by a
release report. That makes repeated characters work, and only one key is ever down at a
time. `--delay MS` (default 0) adds a pause between characters for slow targets such as
some BIOS screens. Newline is sent as Enter (0x28) and tab as Tab (0x2B).

### 9.2 Synchronous typing with a timeout

The helper opens `/dev/hidg0` with `O_WRONLY|O_NONBLOCK` and waits for each report with
`select()`. `--timeout` is the most time allowed with no progress (default 5 s), so it
starts again after each report that is written. When it runs out, the helper stops and
reports how many characters it sent and the text that was not sent. The client prints
that text and exits 1. A key that was pressed but not released is always followed by a
best-effort release report, so no key is left stuck down.

**All the text is translated before anything is sent.** A character that is not in the
keymap fails the whole command before any keystroke is sent. The error names the
character and its position.

### 9.3 Keymaps

A keymap turns a character into a (HID usage, modifier set) pair. The translation is done
**on the gadget host**, because the keymap data lives there and macOS clients have none.

1. **Built-in `us`:** a table inside `helper.py`. It is always available and is the
   default, matching the UEFI assumption.
2. **Installed layouts:** `ckbcomp <layout> [<variant>]` (from Debian's `console-setup`)
   turns any XKB layout installed on the gadget host into one flat Linux console keymap.
   The helper parses its `keycode N = sym sym ...` lines. It uses the columns for
   plain, Shift, AltGr and Shift+AltGr, mapped to HID modifiers none, LShift, RAlt and
   LShift+RAlt. Only `U+XXXX` symbols (optionally with a `+` prefix, which marks a letter
   affected by Caps Lock) are used. `ckbcomp` writes every printable character this way;
   named symbols (`Return`, `dead_acute`, ...) are ignored. Linux keycodes are turned into HID
   usages with a built-in table (the inverse of the kernel's `hid-input` mapping).

   When a character is on several keys, the helper prefers, in this order: no AltGr, then
   not the ISO 102nd key, then fewer modifiers. Hosts disagree about the 102nd key on US
   layouts (Linux XKB gives `<>`, Windows gives `\|`), and about XKB's extra AltGr
   symbols (gb's AltGr+minus gives `\` on Linux but nothing on Windows).
3. If `ckbcomp` is missing and the keymap is not `us`, the command fails with a hint to
   install `console-setup`.

Characters that need dead keys or compose sequences are not supported in v1. They fail
translation, so they are never typed wrongly.

The parsed keymap is cached at `<path>/keymaps/<layout>[_<variant>].json`, because
`ckbcomp` takes about a second on a Pi Zero.

---

## 10. Error handling, output and exit codes

### 10.1 Exception hierarchy (client)

```
RemoteGadgetError
├── ConfigError          (exit 2)
├── UsageError           (exit 2)
├── SshConnectionError   (exit 3)   ssh exit 255, timeouts (not the builtin ConnectionError)
├── RemoteError          (exit 1)   helper ok=false; carries code/message/hint
│   ├── NotInitialisedError
│   ├── WrongStateError         e.g. write while plugged
│   └── TypingTimeoutError      carries the text that was not sent
└── LocalFileError       (exit 1)   missing or unreadable local file, would overwrite
```

`cli.main` catches only `RemoteGadgetError` (exit as shown above) and `KeyboardInterrupt`
(exit 130). Any other exception is a bug and is allowed to produce a traceback.

Exit 4 is reserved for "done, but a reboot is required" (`init`, `doctor --fix`).

### 10.2 Output

`output.Reporter` has the levels `info`, `success`, `warn` and `error`. `info` and
`success` go to stdout. `warn` and `error` go to stderr and are never hidden by
`--quiet`. Colour is used when `--color=always`, or with `auto` when the stream is a TTY,
`NO_COLOR` is unset and `TERM != dumb`. Green means success, yellow a warning and red an
error. Output is one line per fact. For example, `state` prints:

```
lab-pi: plugged (udc 20980000.usb, target: configured)
storage: 64 MiB, 12.3 MiB free · keymap: us
```

### 10.3 Library API

```python
from remote_gadget import Config, Gadget

cfg = Config.load(profile="lab-pi", overrides={"keymap": "gb"})
g = Gadget(cfg)                     # Gadget(cfg, transport=FakeTransport()) in tests
g.unplug(); g.write("fw.bin", "/fw.bin"); g.plug()
g.type("hello\n", timeout=5)
print(g.state())                    # returns a GadgetState dataclass, not text
```

The API returns data and raises exceptions. All printing lives in `cli.py` and `output.py`.

---

## 11. Testing

* **Unit (client):** config merging and validation (tmp-path config files, every
  override); CLI argument parsing and exit-code mapping; `Gadget` against a
  `FakeTransport` that records commands and returns helper JSON from fixtures; colour
  detection.
* **Helper:** `helper.py` takes `--sysroot` (hidden, for tests only) so that configfs,
  `/sys/class/udc` and `/dev/hidg0` can be fake directories or FIFOs under `tmp_path`.
  Tests cover: building and starting configfs idempotently, plug/unplug, the state
  machine, HID report generation, the timeout and partial-delivery path (a FIFO that is
  never read), the US keymap, and parsing recorded `ckbcomp` output for `gb`, `de` and `fr`.
  The mtools calls are tested through a fake `PATH` with stub scripts. Real `mtools`
  tests are optional and skipped when it is not installed.
* **Integration:** marked `integration` and skipped unless `RGADGET_TEST_PROFILE` is set.
  They run the full CLI against a real gadget host.
* **Tooling:** `tox` envs `py310`–`py313` (full suite) plus `py39-helper` (helper tests only, under 3.9), `lint` (`ruff check`, `ruff format --check`)
  and `type` (`mypy --strict` on `src/`). There is a `.pre-commit-hooks.yaml` once the
  suite is fast.

---

## 12. Differences from the original outline (`DESIGN.md`)

| Original outline | This design | Why |
|-------------|-------------|-----|
| A single monolithic `#!/usr/bin/env python` file with no packages | A normal src-layout package; the only single file is the uploaded remote helper | Decision by the author: testability and conventions matter more than single-file install. |
| `state` reports "pending keyboard scan codes" | No queue. `type` is synchronous; `state` reports whether the target has the gadget `configured`, which is what "the keyboard is being read" means in practice | Decision by the author: synchronous typing with a timeout. Text that is not delivered is reported by `type` itself. |
| `state` reports whether a USB host is attached | Reported only while plugged; *unknown* while unplugged | The UDC state is meaningful only while bound, and VBUS sensing is not reliable on bus-powered boards. |
| `init` sets up the gadget and starts it | `init` sets up the machine only; `plug`/`unplug` start the gadget when needed; there is no boot-time persistence | Decision by the author. |
| Keymap = "scan map name" | XKB layout names (`gb`, `de:nodeadkeys`) resolved on the gadget host through `ckbcomp`, with US built in | Uses the layouts Linux already ships, as requested. |
| Default `path` `/opt/run/remote-gadget` | `/var/lib/remote-gadget` | `/var/lib` is the FHS location for persistent state. |
| Plug/unplug semantics not specified | Unplug turns the D+ pull-up off (UDC unbind), so the target sees a real disconnect and enumerates again on plug | The target must reread the storage after a `write`. |
| — | Added `plug --ro`/`readonly`, and the optional `vendor_id`/`product_id` | Requested. |
| — | Added `ls`, `rm` and the `-f`, `--fix`, `--reformat`, `--timeout`, `--delay`, `--enter` options | Needed for the listed commands to be usable and safe. |
| Config "precedence" | Files are merged; nearer files win key by key | Decision by the author. |

---

## 13. Open questions and things to check on hardware

1. **dwc2 unbind behaviour.** *Partly checked* on a Pi Zero 2 W (Raspbian 13, kernel 6.18).
   After `plug`, `/sys/class/udc/3f980000.usb/state` goes to `configured` within about
   150 ms. After `unplug`, it reads `not attached`, so dwc2 does update the file on unbind.
   On the target side (a macOS host), the `remote-gadget` USB device and the `RGADGET`
   disk are both listed while plugged, and both are gone within 2 s of `unplug`. So the
   target does see a real disconnect. Still to do: repeat on a Pi 4 and a Pi 5, then
   decide whether the `unplug` check becomes an error for dwc2.
   One unexplained reading: once, three seconds after `plug`, `state` said `not attached`
   although the Pi's log showed the host enumerating the device. It may just be a slow target.
2. **Minimum time unplugged:** check that 1 s is enough for UEFI firmware, which may poll
   hub ports less often than an OS does.
3. **`ckbcomp` output:** *checked* on trixie for us, gb, de and fr. Recorded output is in
   `tests/fixtures/ckbcomp/`. `ckbcomp` takes about 1 s on a Zero 2 W, so parsed maps are
   cached in `<path>/keymaps/` until the XKB data changes.
4. **Typing on a real target:** *checked* on macOS with the built-in `us` map. A
   70-character string covering letters, digits, Shift symbols and space arrived exactly.
   Not yet tried: other layouts on a real target, `reset`, and the timeout path
   against a real host.
5. **Storage in both directions:** *checked* with a macOS target. A file written by `write`
   while unplugged read back identically on the Mac after `plug --rw`. A file the Mac wrote
   (then ejected) read back identically with `read` after `unplug`. `fsck.vfat -n` found the
   image clean afterwards. The target must eject before `unplug`; this is documented in the
   README, and the tool cannot enforce it.

## 14. Resolved decisions

| Question | Decision |
|----------|----------|
| Helper Python version | Target 3.11; keep to 3.9-compatible syntax where it costs little (§4). |
| Default `path` | `/var/lib/remote-gadget` (§6.1). |
| Read-only LUN | `plug --ro` / `readonly` profile key (§6.2). |
| USB IDs | Optional `vendor_id`/`product_id`; default Linux Foundation IDs (§5.2). |
