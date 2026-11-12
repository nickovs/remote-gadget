#!/usr/bin/env python3
"""remote-gadget helper: runs on the gadget host and does all kernel-facing work.

This file is uploaded to ``<path>/bin/rgadget-helper`` by ``remote-gadget init``
and invoked over ssh by the client.  It must only use the standard library and
must stay runnable on Python 3.9.

Every invocation prints exactly one JSON object on stdout::

    {"ok": true, "result": {...}, "warnings": [...]}
    {"ok": false, "error": {"code": ..., "message": ..., "hint": ...}, "warnings": [...]}

See docs/design.md section 7 for the protocol.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_VERSION = 10
EXIT_NOT_ROOT = 11

DEFAULT_PATH = "/var/lib/remote-gadget"
GADGET_NAME = "rgadget"
DEFAULT_VENDOR_ID = 0x1D6B  # Linux Foundation
DEFAULT_PRODUCT_ID = 0x0104  # Multifunction Composite Gadget
# Long enough that a host polling its hub port every few hundred ms sees the disconnect.
MIN_UNPLUG_SECONDS = 1.0
OVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"
# dwc2 pulls D+ up as soon as it probes, before any gadget is bound, so a target sees a
# device that never answers and may stall its boot.  Keep it unloaded until `plug`.
BLACKLIST_FILE = "etc/modprobe.d/remote-gadget.conf"
BLACKLIST_TEXT = "# Added by remote-gadget: dwc2 is loaded by `plug`\nblacklist dwc2\n"
UDC_PROBE_SECONDS = 2.0

# Standard boot-protocol keyboard report descriptor (kernel Documentation/usb/gadget_hid.rst).
KEYBOARD_REPORT_DESC = bytes.fromhex(
    "05010906a101050719e029e715002501750195088102950175088103"
    "9505750105081901290591029501750391039506750815002565"
    "05071900296581"
    "00c0"
)

MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_RALT = 0x40
HID_ENTER = 0x28
HID_TAB = 0x2B
HID_DELETE = 0x4C
RELEASE_REPORT = bytes(8)

KeyMap = dict[str, tuple[int, int]]  # char -> (modifiers, HID usage)

# Linux input keycode -> USB HID usage (keyboard page) for keys that produce characters.
# KEY_BACKSLASH (43) is sent as HID 0x31; Windows, Linux and UEFI treat it the same as the
# ISO "Non-US #" key (0x32).
LINUX_TO_HID = {
    1: 0x29, 2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22, 7: 0x23, 8: 0x24, 9: 0x25,
    10: 0x26, 11: 0x27, 12: 0x2D, 13: 0x2E, 14: 0x2A, 15: 0x2B, 16: 0x14, 17: 0x1A,
    18: 0x08, 19: 0x15, 20: 0x17, 21: 0x1C, 22: 0x18, 23: 0x0C, 24: 0x12, 25: 0x13,
    26: 0x2F, 27: 0x30, 28: 0x28, 30: 0x04, 31: 0x16, 32: 0x07, 33: 0x09, 34: 0x0A,
    35: 0x0B, 36: 0x0D, 37: 0x0E, 38: 0x0F, 39: 0x33, 40: 0x34, 41: 0x35, 43: 0x31,
    44: 0x1D, 45: 0x1B, 46: 0x06, 47: 0x19, 48: 0x05, 49: 0x11, 50: 0x10, 51: 0x36,
    52: 0x37, 53: 0x38, 57: 0x2C, 86: 0x64,
}  # fmt: skip
KEY_102ND = 86  # the extra ISO key; only used when a character is on no other key


class HelperError(Exception):
    """A failure reported to the client as ``{"ok": false, "error": ...}``."""

    def __init__(self, code: str, message: str, hint: str | None = None, **data: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data


class Ctx:
    """Paths and per-invocation state.  ``sysroot`` is "/" except in tests."""

    def __init__(self, path: str = DEFAULT_PATH, sysroot: str = "/"):
        self.path = path
        self.sysroot = sysroot
        self.warnings: list[str] = []

    @property
    def testing(self) -> bool:
        return self.sysroot != "/"

    def sys(self, rel: str) -> str:
        return os.path.join(self.sysroot, rel.lstrip("/"))

    @property
    def gadget(self) -> str:
        return self.sys("sys/kernel/config/usb_gadget/" + GADGET_NAME)

    @property
    def image(self) -> str:
        return os.path.join(self.path, "image.img")

    @property
    def staging(self) -> str:
        return os.path.join(self.path, "staging")

    @property
    def state_file(self) -> str:
        return os.path.join(self.path, "state.json")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# --------------------------------------------------------------------------- utilities


def run_cmd(
    cmd: list[str], input: bytes | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run an external command, capturing output.  Missing tools become HelperErrors."""
    try:
        return subprocess.run(
            [which(cmd[0]) or cmd[0], *cmd[1:]],  # finds sbin tools for non-root users too
            input=input, capture_output=True, env=env, check=False, timeout=600,
        )  # fmt: skip
    except FileNotFoundError:
        raise HelperError(
            "missing_tool", f"{cmd[0]} is not installed", "run `remote-gadget doctor`"
        ) from None


def which(name: str) -> str | None:
    return shutil.which(name, path=os.environ.get("PATH", os.defpath) + ":/usr/sbin:/sbin")


def read_attr(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def write_attr(path: str, value: Any) -> None:
    """Write a sysfs/configfs attribute.  Strings get a newline so "" is a real write."""
    data = value if isinstance(value, bytes) else (str(value) + "\n").encode()
    try:
        with open(path, "wb", buffering=0) as f:
            f.write(data)
    except OSError as e:
        raise HelperError("sysfs", f"cannot write {path}: {e.strerror}") from None


def require_root(ctx: Ctx) -> None:
    if not ctx.testing and os.geteuid() != 0:
        raise HelperError("not_root", "this operation needs root", "remove `nosudo` or run as root")


def owner_ids() -> tuple[int, int]:
    """The real (pre-sudo) user, who should own the state directory."""
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid and uid.isdigit() and gid.isdigit():
        return int(uid), int(gid)
    return os.getuid(), os.getgid()


def load_state(ctx: Ctx) -> dict[str, Any]:
    try:
        with open(ctx.state_file) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or not st.get("initialised"):
        raise HelperError(
            "not_initialised", "gadget host is not initialised", "run `remote-gadget init`"
        )
    return st


def save_state(ctx: Ctx, st: dict[str, Any]) -> None:
    tmp = ctx.state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    dir_stat = os.stat(ctx.path)
    if os.geteuid() == 0:
        os.chown(tmp, dir_stat.st_uid, dir_stat.st_gid)
    os.replace(tmp, ctx.state_file)


# --------------------------------------------------------------------------- platform


def os_release(ctx: Ctx) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(ctx.sys("etc/os-release")) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep:
                    out[k] = v.strip('"')
    except OSError:
        pass
    return out


def is_debian(ctx: Ctx) -> bool:
    rel = os_release(ctx)
    return "debian" in (rel.get("ID", "") + " " + rel.get("ID_LIKE", "")).split()


def pi_model(ctx: Ctx) -> str | None:
    model = read_attr(ctx.sys("proc/device-tree/model")).rstrip("\x00")
    return model if model.startswith("Raspberry Pi") else None


def boot_config_path(ctx: Ctx) -> str:
    for rel in ("boot/firmware/config.txt", "boot/config.txt"):
        if os.path.isfile(ctx.sys(rel)):
            return ctx.sys(rel)
    return ctx.sys("boot/firmware/config.txt")


def overlay_enabled(text: str) -> bool:
    """True if a dwc2 overlay (not host mode) is active for all boards.

    Only lines under an ``[all]`` section (or before any section) count: the stock
    config has ``dtoverlay=dwc2,dr_mode=host`` under ``[cm5]``.
    """
    section = "all"
    found = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
        elif section == "all" and re.match(r"dtoverlay\s*=\s*dwc2\b", line):
            found = "dr_mode=host" not in line.replace(" ", "")
    return found


def list_udcs(ctx: Ctx) -> list[str]:
    try:
        return sorted(os.listdir(ctx.sys("sys/class/udc")))
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- checks / setup

Check = dict[str, Any]


def _check(name: str, status: str, message: str, hint: str | None = None) -> Check:
    return {"name": name, "status": status, "message": message, "hint": hint, "fixed": False}


def check_python(ctx: Ctx) -> Check:
    v = sys.version_info
    ver = ".".join(str(n) for n in v[:3])
    if v >= (3, 11):
        return _check("python", "ok", "python " + ver)
    return _check("python", "warn", f"python {ver} (3.11+ recommended)")


def check_platform(ctx: Ctx) -> Check:
    rel = os_release(ctx).get("PRETTY_NAME", "unknown OS")
    model = pi_model(ctx) or "not a Raspberry Pi"
    if is_debian(ctx):
        return _check("platform", "ok", f"{rel}, {model}")
    return _check(
        "platform", "warn", f"{rel}, {model}: not Debian-family; some checks are unreliable"
    )


def check_tools(ctx: Ctx) -> Check:
    missing = [t for t in ("mcopy", "mkfs.vfat") if not which(t)]
    if not missing:
        return _check("tools", "ok", "mtools and dosfstools installed")
    return _check(
        "tools", "fail", "missing: " + ", ".join(missing), "install mtools and dosfstools"
    )


def fix_tools(ctx: Ctx) -> None:
    if not is_debian(ctx):
        raise HelperError("unfixable", "cannot install packages on a non-Debian system")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    # Wait for another apt/dpkg run to finish rather than failing on its lock.
    apt = ["apt-get", "-o", "DPkg::Lock::Timeout=600"]
    cmd = [*apt, "install", "-y", "--no-install-recommends", "mtools", "dosfstools"]
    if run_cmd(cmd, env=env).returncode != 0:
        run_cmd([*apt, "update"], env=env)
        r = run_cmd(cmd, env=env)
        if r.returncode != 0:
            raise HelperError("apt", "apt-get failed: " + r.stderr.decode(errors="replace")[-500:])


def check_overlay(ctx: Ctx) -> Check:
    if not pi_model(ctx):
        return _check("boot-overlay", "unknown", "not a Raspberry Pi; boot config not checked")
    path = boot_config_path(ctx)
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return _check("boot-overlay", "fail", f"cannot read {path}: {e.strerror}")
    if overlay_enabled(text):
        return _check("boot-overlay", "ok", "dwc2 overlay enabled in " + path)
    return _check("boot-overlay", "fail", "dwc2 overlay not enabled in " + path)


def fix_overlay(ctx: Ctx) -> None:
    path = boot_config_path(ctx)
    backup = f"{path}.rgadget-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "a") as f:
        f.write(f"\n# Added by remote-gadget\n[all]\n{OVERLAY_LINE}\n")


def dwc2_blacklisted(ctx: Ctx) -> bool:
    try:
        with open(ctx.sys(BLACKLIST_FILE)) as f:
            return any(line.split() == ["blacklist", "dwc2"] for line in f)
    except OSError:
        return False


def dwc2_loaded(ctx: Ctx) -> bool:
    return os.path.isdir(ctx.sys("sys/module/dwc2"))


def check_blacklist(ctx: Ctx) -> Check:
    if not pi_model(ctx):
        return _check("dwc2-boot", "unknown", "not a Raspberry Pi; dwc2 loading not checked")
    if dwc2_blacklisted(ctx):
        return _check("dwc2-boot", "ok", "dwc2 not loaded at boot (loaded by plug)")
    return _check(
        "dwc2-boot",
        "fail",
        "dwc2 loads at boot, so the target sees an unresponsive device until plug",
    )


def fix_blacklist(ctx: Ctx) -> None:
    path = ctx.sys(BLACKLIST_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(BLACKLIST_TEXT)


def load_dwc2(ctx: Ctx) -> None:
    """Load the blacklisted dwc2 module and wait for its UDC to appear."""
    r = run_cmd(["modprobe", "dwc2"])
    if r.returncode != 0:
        raise HelperError(
            "modprobe", "cannot load dwc2: " + r.stderr.decode(errors="replace").strip()
        )
    deadline = time.monotonic() + UDC_PROBE_SECONDS
    while not list_udcs(ctx) and time.monotonic() < deadline:
        time.sleep(0.05)


def check_configfs(ctx: Ctx) -> Check:
    if os.path.isdir(ctx.sys("sys/kernel/config/usb_gadget")):
        return _check("configfs", "ok", "configfs mounted and libcomposite loaded")
    # Nothing loads libcomposite at boot; `plug` does.  Only fail if it cannot be loaded.
    if run_cmd(["modprobe", "-n", "-q", "libcomposite"]).returncode == 0:
        return _check("configfs", "ok", "libcomposite available (loaded on plug)")
    return _check("configfs", "fail", "libcomposite kernel module not available")


def fix_configfs(ctx: Ctx) -> None:
    load_libcomposite(ctx)


def load_libcomposite(ctx: Ctx) -> None:
    if ctx.testing:
        return
    cfg = ctx.sys("sys/kernel/config")
    if not os.path.ismount(cfg):
        run_cmd(["mount", "-t", "configfs", "none", cfg])
    r = run_cmd(["modprobe", "libcomposite"])
    if r.returncode != 0:
        raise HelperError(
            "modprobe", "cannot load libcomposite: " + r.stderr.decode(errors="replace").strip()
        )


def check_udc(ctx: Ctx) -> Check:
    udcs = list_udcs(ctx)
    if udcs:
        return _check("udc", "ok", "USB device controller: " + ", ".join(udcs))
    if pi_model(ctx) and dwc2_blacklisted(ctx) and not dwc2_loaded(ctx):
        if run_cmd(["modprobe", "-n", "-q", "dwc2"]).returncode == 0:
            return _check("udc", "ok", "dwc2 available (loaded on plug)")
        return _check("udc", "fail", "dwc2 kernel module not available")
    hint = "use a USB data cable in the device/OTG port"
    if pi_model(ctx):
        hint = "enable the dwc2 overlay and reboot; " + hint
    return _check("udc", "fail", "no USB device controller found", hint)


def check_path(ctx: Ctx) -> Check:
    uid, _ = owner_ids()
    for d in (ctx.path, os.path.join(ctx.path, "bin"), ctx.staging):
        if not os.path.isdir(d):
            return _check("state-dir", "fail", d + " missing")
    if os.stat(ctx.path).st_uid != uid:
        return _check("state-dir", "fail", ctx.path + " not owned by the ssh user")
    return _check("state-dir", "ok", ctx.path)


def fix_path(ctx: Ctx) -> None:
    uid, gid = owner_ids()
    for d in (ctx.path, os.path.join(ctx.path, "bin"), ctx.staging):
        os.makedirs(d, exist_ok=True)
        os.chown(d, uid, gid)


def check_image(ctx: Ctx) -> Check:
    if not os.path.isfile(ctx.image):
        return _check("image", "fail", ctx.image + " missing", "run `remote-gadget init`")
    r = run_cmd(["minfo", "-i", ctx.image, "::"], env=mtools_env())
    if r.returncode != 0:
        return _check(
            "image",
            "fail",
            "image is not a valid FAT filesystem",
            "`remote-gadget init --reformat`",
        )
    return _check("image", "ok", f"{ctx.image} ({os.path.getsize(ctx.image) >> 20} MiB)")


def check_keymaps(ctx: Ctx) -> Check:
    if which("ckbcomp"):
        return _check("keymaps", "ok", "ckbcomp available; any installed XKB layout can be used")
    return _check(
        "keymaps",
        "warn",
        "ckbcomp missing; only the built-in `us` keymap works",
        "install console-setup",
    )


# (check, fix or None); order matters: later checks depend on earlier fixes.
CHECKS: list[tuple[Callable[[Ctx], Check], Callable[[Ctx], None] | None]] = [
    (check_python, None),
    (check_platform, None),
    (check_tools, fix_tools),
    (check_overlay, fix_overlay),
    (check_blacklist, fix_blacklist),
    (check_configfs, fix_configfs),
    (check_udc, None),
    (check_path, fix_path),
    (check_keymaps, None),
]


def run_checks(ctx: Ctx, fix: bool, install_packages: bool = True) -> dict[str, Any]:
    """Run every check; with ``fix``, try to repair failures.  Shared by doctor and setup."""
    results: list[Check] = []
    reboot = False
    for check, fixer in CHECKS:
        res = check(ctx)
        if res["status"] == "fail" and fix and fixer is not None:
            if fixer is fix_tools and not install_packages:
                res["hint"] = "re-run with --install-packages, or install mtools and dosfstools"
            else:
                try:
                    fixer(ctx)
                except (HelperError, OSError) as e:
                    res["message"] += f"; fix failed: {getattr(e, 'message', None) or e}"
                else:
                    res = check(ctx)
                    res["fixed"] = True
                    if fixer is fix_overlay:
                        reboot = True
        results.append(res)
    # With dwc2 blacklisted and unloaded, no UDC is normal: `plug` loads it.
    unloaded = dwc2_blacklisted(ctx) and not dwc2_loaded(ctx)
    if not list_udcs(ctx) and pi_model(ctx) and not unloaded:
        overlay = next(r for r in results if r["name"] == "boot-overlay")
        reboot = reboot or overlay["status"] == "ok"
    if reboot:
        udc = next(r for r in results if r["name"] == "udc")
        if udc["status"] == "fail":
            udc.update(status="warn", message="no USB device controller until reboot", hint=None)
    return {"checks": results, "reboot_required": reboot, "debian": is_debian(ctx)}


def op_doctor(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    if args.fix:
        require_root(ctx)
    out = run_checks(ctx, fix=args.fix)
    if os.path.isdir(ctx.path):
        out["checks"].append(check_image(ctx))
    return out


def op_setup(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    require_root(ctx)
    out = run_checks(ctx, fix=True, install_packages=args.install_packages)
    failed = [c for c in out["checks"] if c["status"] == "fail" and c["name"] != "udc"]
    if failed:
        raise HelperError(
            "setup_failed",
            "setup incomplete: " + "; ".join(c["name"] + ": " + c["message"] for c in failed),
            "run `remote-gadget doctor` for details",
            **out,
        )
    out["checks"].append(ensure_image(ctx, args.size, args.reformat))
    udcs = list_udcs(ctx)
    try:
        st = load_state(ctx)
    except HelperError:
        st = {"initialised": True, "unplugged_at": None}
    st.update(size=args.size, udc=udcs[0] if len(udcs) == 1 else st.get("udc"))
    save_state(ctx, st)
    return out


def ensure_image(ctx: Ctx, size_mb: int, reformat: bool) -> Check:
    exists = os.path.isfile(ctx.image)
    if exists and not reformat:
        actual = os.path.getsize(ctx.image) >> 20
        if actual != size_mb:
            return _check(
                "image",
                "warn",
                f"existing image is {actual} MiB, not {size_mb} MiB; kept",
                "`remote-gadget init --reformat` replaces it (contents are lost)",
            )
        return _check("image", "ok", f"{ctx.image} ({actual} MiB)")
    if exists and read_attr(os.path.join(ctx.gadget, "UDC")):
        raise HelperError("wrong_state", "cannot reformat while plugged", "unplug first")
    uid, gid = owner_ids()
    tmp = ctx.image + ".new"
    with open(tmp, "wb") as f:
        f.truncate(size_mb << 20)
    os.chown(tmp, uid, gid)
    r = run_cmd(["mkfs.vfat", "-n", "RGADGET", tmp])
    if r.returncode != 0:
        os.unlink(tmp)
        raise HelperError("mkfs", "mkfs.vfat failed: " + r.stderr.decode(errors="replace"))
    os.replace(tmp, ctx.image)
    if os.path.isdir(lun_dir(ctx)):
        # The LUN still holds the old (now unlinked) file open; point it at the new one.
        write_attr(os.path.join(lun_dir(ctx), "file"), "")
        write_attr(os.path.join(lun_dir(ctx), "file"), ctx.image)
    res = _check("image", "ok", f"created {ctx.image} ({size_mb} MiB)")
    res["fixed"] = True
    return res


# --------------------------------------------------------------------------- gadget


def gadget_state(ctx: Ctx, st: dict[str, Any]) -> str:
    if not os.path.isdir(ctx.gadget):
        return "stopped"
    return "plugged" if read_attr(os.path.join(ctx.gadget, "UDC")) else "unplugged"


def udc_state(ctx: Ctx, udc: str) -> str:
    return read_attr(ctx.sys(f"sys/class/udc/{udc}/state"), "unknown")


def choose_udc(ctx: Ctx, st: dict[str, Any]) -> str:
    udcs = list_udcs(ctx)
    if len(udcs) == 1:
        return udcs[0]
    if st.get("udc") in udcs:
        return str(st["udc"])
    if not udcs:
        raise HelperError(
            "no_udc",
            "no USB device controller; dwc2 overlay not active (reboot needed?)",
            "run `remote-gadget doctor`",
        )
    raise HelperError("no_udc", f"several UDCs ({', '.join(udcs)}) and none recorded")


def lun_dir(ctx: Ctx) -> str:
    return os.path.join(ctx.gadget, "functions/mass_storage.usb0/lun.0")


def ensure_gadget(ctx: Ctx, vendor_id: int, product_id: int) -> None:
    """Build the configfs gadget (idempotent).  Must only be called while unbound."""
    g = ctx.gadget
    os.makedirs(g, exist_ok=True)
    write_attr(os.path.join(g, "idVendor"), f"0x{vendor_id:04x}")
    write_attr(os.path.join(g, "idProduct"), f"0x{product_id:04x}")
    write_attr(os.path.join(g, "bcdDevice"), "0x0100")
    write_attr(os.path.join(g, "bcdUSB"), "0x0200")
    strings = os.path.join(g, "strings/0x409")
    os.makedirs(strings, exist_ok=True)
    serial = read_attr(ctx.sys("etc/machine-id"))[:16] or "0"
    for k, v in (("manufacturer", "remote-gadget"), ("product", "remote-gadget"),
                 ("serialnumber", serial)):  # fmt: skip
        write_attr(os.path.join(strings, k), v)
    cfg = os.path.join(g, "configs/c.1")
    os.makedirs(os.path.join(cfg, "strings/0x409"), exist_ok=True)
    write_attr(os.path.join(cfg, "MaxPower"), 250)
    write_attr(os.path.join(cfg, "strings/0x409/configuration"), "keyboard+storage")

    # Function attributes are locked (EBUSY) once linked, so only set them on creation.
    hid = os.path.join(g, "functions/hid.usb0")
    if not os.path.isdir(hid):
        os.makedirs(hid)
        write_attr(os.path.join(hid, "protocol"), 1)
        write_attr(os.path.join(hid, "subclass"), 1)
        write_attr(os.path.join(hid, "report_length"), 8)
        write_attr(os.path.join(hid, "report_desc"), KEYBOARD_REPORT_DESC)
    ms = os.path.join(g, "functions/mass_storage.usb0")
    if not os.path.isdir(ms):
        os.makedirs(ms)
        os.makedirs(lun_dir(ctx), exist_ok=True)  # configfs creates lun.0 itself
        write_attr(os.path.join(ms, "stall"), 1)
        write_attr(os.path.join(lun_dir(ctx), "removable"), 1)
        write_attr(os.path.join(lun_dir(ctx), "ro"), 0)
        write_attr(os.path.join(lun_dir(ctx), "file"), ctx.image)
    for func in ("hid.usb0", "mass_storage.usb0"):
        link = os.path.join(cfg, func)
        if not os.path.islink(link):
            os.symlink(os.path.join(g, "functions", func), link)


def start(ctx: Ctx, args: argparse.Namespace) -> None:
    if not os.path.isdir(ctx.gadget):
        load_libcomposite(ctx)
        if not os.path.isdir(ctx.sys("sys/kernel/config/usb_gadget")):
            raise HelperError("configfs", "configfs usb_gadget not available", "run doctor")
    if not read_attr(os.path.join(ctx.gadget, "UDC")):
        ensure_gadget(ctx, args.vendor_id, args.product_id)


def current_ro(ctx: Ctx) -> bool:
    return read_attr(os.path.join(lun_dir(ctx), "ro"), "0") == "1"


def set_ro(ctx: Ctx, ro: bool) -> None:
    """Change the LUN's write protection.  The kernel refuses while a file is open."""
    if current_ro(ctx) == ro:
        return
    lun = lun_dir(ctx)
    image = read_attr(os.path.join(lun, "file")) or ctx.image
    write_attr(os.path.join(lun, "file"), "")
    write_attr(os.path.join(lun, "ro"), int(ro))
    write_attr(os.path.join(lun, "file"), image)


def op_plug(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    require_root(ctx)
    st = load_state(ctx)
    start(ctx, args)
    if gadget_state(ctx, st) == "plugged":
        if current_ro(ctx) != args.ro:
            raise HelperError(
                "wrong_state",
                f"already plugged {'read-only' if current_ro(ctx) else 'read-write'}",
                "unplug first",
            )
        return {"state": "plugged", "ro": args.ro, "changed": False}
    set_ro(ctx, args.ro)
    unplugged_at = st.get("unplugged_at")
    if isinstance(unplugged_at, (int, float)):
        wait = MIN_UNPLUG_SECONDS - (time.time() - unplugged_at)
        if 0 < wait <= MIN_UNPLUG_SECONDS:
            time.sleep(wait)
    # Load dwc2 only now, so the target sees it just before the gadget binds.
    if not list_udcs(ctx) and pi_model(ctx) and not dwc2_loaded(ctx):
        load_dwc2(ctx)
    udc = choose_udc(ctx, st)
    write_attr(os.path.join(ctx.gadget, "UDC"), udc)
    st["udc"] = udc
    save_state(ctx, st)
    return {"state": "plugged", "ro": args.ro, "changed": True}


def op_unplug(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    require_root(ctx)
    st = load_state(ctx)
    start(ctx, args)
    udc_file = os.path.join(ctx.gadget, "UDC")
    udc = read_attr(udc_file)
    if not udc:
        return {"state": "unplugged", "changed": False}
    write_attr(udc_file, "")
    if read_attr(udc_file):
        raise HelperError("unplug_failed", "UDC still bound after unplug")
    after = udc_state(ctx, udc)
    if after != "not attached":
        ctx.warn(f"UDC reports {after!r} after unplug; the target may still see the device")
    st["unplugged_at"] = time.time()
    save_state(ctx, st)
    return {"state": "unplugged", "changed": True}


def op_state(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    st = load_state(ctx)
    state = gadget_state(ctx, st)
    out: dict[str, Any] = {
        "state": state, "udc": None, "udc_state": None, "ro": None,
        "size_mb": st.get("size"), "free_bytes": None,
    }  # fmt: skip
    if state != "stopped":
        out["ro"] = current_ro(ctx)
    if state == "plugged":
        out["udc"] = read_attr(os.path.join(ctx.gadget, "UDC"))
        out["udc_state"] = udc_state(ctx, out["udc"])
    try:
        out["free_bytes"] = image_free(ctx)
    except HelperError as e:
        ctx.warn("cannot read image: " + e.message)
    return out


# --------------------------------------------------------------------------- image (mtools)


def mtools_env() -> dict[str, str]:
    return dict(os.environ, MTOOLS_SKIP_CHECK="1")


def mtool(ctx: Ctx, cmd: list[str]) -> str:
    r = run_cmd([cmd[0], "-i", ctx.image, *cmd[1:]], env=mtools_env())
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).decode(errors="replace").strip()
        raise HelperError("mtools", f"{cmd[0]} failed: {msg}")
    return r.stdout.decode(errors="replace")


def image_path(p: str) -> str:
    """Validate a path inside the image and return it in mtools ``::/a/b`` form."""
    if "\x00" in p or "\\" in p:
        raise HelperError("bad_path", f"invalid image path {p!r}")
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if ".." in parts:
        raise HelperError("bad_path", f"'..' not allowed in image path {p!r}")
    return "::/" + "/".join(parts)


def staging_file(ctx: Ctx, name: str) -> str:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$", name):
        raise HelperError("bad_path", f"invalid staging name {name!r}")
    return os.path.join(ctx.staging, name)


def image_free(ctx: Ctx) -> int:
    out = mtool(ctx, ["mdir", "::/"])
    m = re.search(r"([\d ]+) bytes free", out)
    if not m:
        raise HelperError("mtools", "cannot parse free space")
    return int(m.group(1).replace(" ", ""))


def op_img_put(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    src = staging_file(ctx, args.name)
    try:
        st = load_state(ctx)
        if gadget_state(ctx, st) == "plugged":
            raise HelperError("wrong_state", "cannot write while plugged", "unplug first")
        dst = image_path(args.dst)
        parts = dst[3:].split("/")
        for i in range(1, len(parts)):
            parent = "::/" + "/".join(parts[:i])
            if run_cmd(["mdir", "-i", ctx.image, parent], env=mtools_env()).returncode != 0:
                mtool(ctx, ["mmd", parent])
        mtool(ctx, ["mcopy", "-o", src, dst])
        return {"dst": dst[2:], "bytes": os.path.getsize(src)}
    finally:
        if os.path.exists(src):
            os.unlink(src)


def op_img_get(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    st = load_state(ctx)
    if gadget_state(ctx, st) == "plugged" and not current_ro(ctx):
        ctx.warn("gadget is plugged read-write: the target may have unflushed writes")
    dst = staging_file(ctx, args.name)
    mtool(ctx, ["mcopy", "-n", image_path(args.src), dst])
    return {"bytes": os.path.getsize(dst)}


def op_img_ls(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    load_state(ctx)
    return {"listing": mtool(ctx, ["mdir", image_path(args.dir)])}


def op_img_rm(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """Delete a file, an empty directory, or (with --recursive) a directory tree."""
    st = load_state(ctx)
    if gadget_state(ctx, st) == "plugged":
        # The target caches the FAT; changing it underneath would corrupt the drive.
        raise HelperError("wrong_state", "cannot delete while plugged", "unplug first")
    target = image_path(args.target)
    if target == "::/":
        raise HelperError("bad_path", "refusing to delete the root of the drive")

    def mtry(tool: str) -> subprocess.CompletedProcess[bytes]:
        return run_cmd([tool, "-i", ctx.image, target], env=mtools_env())

    if mtry("mdel").returncode == 0:  # mdel only deletes files
        return {"removed": target[2:], "kind": "file"}
    r = mtry("mdeltree" if args.recursive else "mrd")
    if r.returncode == 0:
        return {"removed": target[2:], "kind": "directory"}
    msg = (r.stderr or r.stdout).decode(errors="replace")
    if "non empty" in msg:
        raise HelperError("not_empty", f"{target[2:]} is a directory and not empty", "use -r")
    raise HelperError("not_found", f"{target[2:]} not found")


def op_staging_clean(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    path = staging_file(ctx, args.name)
    if os.path.exists(path):
        os.unlink(path)
    return {}


# --------------------------------------------------------------------------- keyboard


def builtin_us() -> KeyMap:
    km: KeyMap = {}
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        km[c] = (0, 0x04 + i)
        km[c.upper()] = (MOD_LSHIFT, 0x04 + i)
    for i, (c, s) in enumerate(zip("1234567890", "!@#$%^&*()")):
        km[c] = (0, 0x1E + i)
        km[s] = (MOD_LSHIFT, 0x1E + i)
    for usage, c, s in ((0x2D, "-", "_"), (0x2E, "=", "+"), (0x2F, "[", "{"), (0x30, "]", "}"),
                        (0x31, "\\", "|"), (0x33, ";", ":"), (0x34, "'", '"'), (0x35, "`", "~"),
                        (0x36, ",", "<"), (0x37, ".", ">"), (0x38, "/", "?")):  # fmt: skip
        km[c] = (0, usage)
        km[s] = (MOD_LSHIFT, usage)
    km[" "] = (0, 0x2C)
    km["\n"] = (0, HID_ENTER)
    km["\t"] = (0, HID_TAB)
    return km


def _sym_char(sym: str) -> str | None:
    m = re.match(r"^\+?U\+([0-9a-fA-F]{4,6})$", sym)
    if not m:
        return None
    ch = chr(int(m.group(1), 16))
    return ch if ch.isprintable() else None


def parse_console_keymap(text: str) -> KeyMap:
    """Build a char map from Linux console keymap text (as produced by ckbcomp).

    Uses columns plain, Shift, AltGr, Shift+AltGr.  When a character is on several keys,
    prefer: no AltGr, then not the ISO 102nd key, then fewest modifiers.  (Hosts disagree
    about the 102nd key on US layouts, and about XKB's extra AltGr symbols.)
    """
    best: dict[str, tuple[tuple[bool, bool, int, int], tuple[int, int]]] = {}
    columns = (0, MOD_LSHIFT, MOD_RALT, MOD_LSHIFT | MOD_RALT)
    for line in text.splitlines():
        m = re.match(r"^\s*keycode\s+(\d+)\s*=\s*(.*)$", line)
        if not m:
            continue
        keycode = int(m.group(1))
        usage = LINUX_TO_HID.get(keycode)
        if usage is None:
            continue
        for col, sym in enumerate(m.group(2).split()[: len(columns)]):
            ch = _sym_char(sym)
            if ch is None:
                continue
            rank = (col >= 2, keycode == KEY_102ND, col, keycode)
            if ch not in best or rank < best[ch][0]:
                best[ch] = (rank, (columns[col], usage))
    km = {ch: entry for ch, (_, entry) in best.items()}
    km["\n"] = (0, HID_ENTER)
    km["\t"] = (0, HID_TAB)
    return km


XKB_SYMBOLS = "/usr/share/X11/xkb/symbols"


def load_keymap(name: str, cache_dir: str | None = None) -> KeyMap:
    """Load a keymap by XKB name (``layout`` or ``layout:variant``).

    Non-US maps come from ckbcomp (~1 s on a Pi Zero), so they are cached as JSON in
    ``cache_dir``; a cache older than the installed XKB data is ignored.
    """
    if not re.match(r"^[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$", name):
        raise HelperError("bad_keymap", f"invalid keymap name {name!r}")
    if name == "us":
        return builtin_us()
    cache = os.path.join(cache_dir, name.replace(":", "_") + ".json") if cache_dir else None
    try:
        if cache and os.path.getmtime(cache) > os.path.getmtime(XKB_SYMBOLS):
            with open(cache) as f:
                return {ch: (v[0], v[1]) for ch, v in json.load(f).items()}
    except (OSError, ValueError, TypeError, IndexError):
        pass
    km = _ckbcomp_keymap(name)
    if cache:
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w") as f:
                json.dump(km, f)
    return km


def _ckbcomp_keymap(name: str) -> KeyMap:
    tool = which("ckbcomp")
    if not tool:
        raise HelperError(
            "bad_keymap", f"keymap {name!r} needs ckbcomp", "install console-setup or use `us`"
        )
    r = run_cmd([tool, *name.split(":")])
    if r.returncode != 0:
        raise HelperError("bad_keymap", f"unknown keymap {name!r}")
    return parse_console_keymap(r.stdout.decode(errors="replace"))


def translate(text: str, keymap: KeyMap) -> tuple[list[tuple[int, int]], list[int]]:
    """Map every character to (modifiers, usage) before anything is sent.

    Returns the keys and, for each key, its position in ``text`` ("\\r" is skipped).
    """
    keys, positions = [], []
    for i, ch in enumerate(text):
        if ch == "\r":
            continue
        if ch not in keymap:
            raise HelperError(
                "unmappable", f"character {ch!r} at position {i} is not in the keymap"
            )
        keys.append(keymap[ch])
        positions.append(i)
    return keys, positions


def hid_device(ctx: Ctx) -> str:
    dev = read_attr(os.path.join(ctx.gadget, "functions/hid.usb0/dev"))
    cls = ctx.sys("sys/class/hidg")
    try:
        names = sorted(os.listdir(cls))
    except FileNotFoundError:
        names = []
    for name in names:
        if dev and read_attr(os.path.join(cls, name, "dev")) == dev:
            return ctx.sys("dev/" + name)
    raise HelperError("no_hid", "cannot find the keyboard's /dev/hidg device")


def _write_report(fd: int, report: bytes, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.write(fd, report)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            select.select([], [fd], [], remaining)


def send_keys(dev: str, keys: list[tuple[int, int]], timeout: float, delay: float) -> int:
    """Type ``keys``; return how many were delivered before a timeout.

    ``timeout`` is the longest wait for any single report, so it restarts on progress.
    """
    fd = os.open(dev, os.O_WRONLY | os.O_NONBLOCK)
    try:
        for i, (mods, usage) in enumerate(keys):
            if not _write_report(fd, bytes([mods, 0, usage, 0, 0, 0, 0, 0]), timeout):
                return i
            if not _write_report(fd, RELEASE_REPORT, timeout):
                with contextlib.suppress(OSError):  # best effort: never leave a key held
                    os.write(fd, RELEASE_REPORT)
                return i + 1
            if delay:
                time.sleep(delay)
        return len(keys)
    finally:
        os.close(fd)


def require_configured(ctx: Ctx) -> None:
    st = load_state(ctx)
    if gadget_state(ctx, st) != "plugged":
        raise HelperError("wrong_state", "gadget is not plugged in", "run `remote-gadget plug`")
    udc = read_attr(os.path.join(ctx.gadget, "UDC"))
    state = udc_state(ctx, udc)
    if state != "configured":
        raise HelperError(
            "not_configured",
            f"target has not configured the gadget (UDC state {state!r})",
            "check the cable and that the target is powered on",
        )


def op_type(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    require_root(ctx)
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HelperError("bad_input", "text to type is not valid UTF-8") from None
    keymap = load_keymap(args.keymap, os.path.join(ctx.path, "keymaps"))
    keys, positions = translate(text, keymap)
    require_configured(ctx)
    sent = send_keys(hid_device(ctx), keys, args.timeout, args.delay / 1000.0)
    if sent < len(keys):
        typed = positions[sent]
        raise HelperError(
            "typing_timeout",
            f"target stopped reading the keyboard after {typed} characters",
            "is the target asleep or at a screen without USB keyboard support?",
            sent=typed,
            unsent=text[typed:],
        )
    return {"sent": len(text)}


def op_reset(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    require_root(ctx)
    require_configured(ctx)
    if send_keys(hid_device(ctx), [(MOD_LCTRL | MOD_LALT, HID_DELETE)], args.timeout, 0) < 1:
        raise HelperError("typing_timeout", "target did not read the keyboard")
    return {}


def op_ping(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    return {"version": own_version()}


# --------------------------------------------------------------------------- entry point


def own_version() -> str | None:
    try:
        with open(__file__, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except (OSError, NameError):
        return None  # running from stdin


def _hex16(s: str) -> int:
    v = int(s, 0)
    if not 0 < v <= 0xFFFF:
        raise argparse.ArgumentTypeError("must be 0x0001-0xffff")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rgadget-helper")
    p.add_argument("--expect-version")
    p.add_argument("--path", default=DEFAULT_PATH)
    p.add_argument("--sysroot", default="/", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="op", required=True)

    def add(
        name: str, func: Callable[[Ctx, argparse.Namespace], dict[str, Any]]
    ) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.set_defaults(func=func)
        return sp

    add("ping", op_ping)
    sp = add("setup", op_setup)
    sp.add_argument("--size", type=int, required=True)
    sp.add_argument("--reformat", action="store_true")
    sp.add_argument("--install-packages", action="store_true")
    add("doctor", op_doctor).add_argument("--fix", action="store_true")
    for name, func in (("plug", op_plug), ("unplug", op_unplug)):
        sp = add(name, func)
        sp.add_argument("--vendor-id", type=_hex16, default=DEFAULT_VENDOR_ID)
        sp.add_argument("--product-id", type=_hex16, default=DEFAULT_PRODUCT_ID)
        if name == "plug":
            sp.add_argument("--ro", action="store_true")
    add("state", op_state)
    sp = add("img-put", op_img_put)
    sp.add_argument("name")
    sp.add_argument("dst")
    sp = add("img-get", op_img_get)
    sp.add_argument("src")
    sp.add_argument("name")
    add("img-ls", op_img_ls).add_argument("dir", nargs="?", default="/")
    sp = add("img-rm", op_img_rm)
    sp.add_argument("target")
    sp.add_argument("--recursive", action="store_true")
    add("staging-clean", op_staging_clean).add_argument("name")
    sp = add("type", op_type)
    sp.add_argument("--keymap", default="us")
    sp.add_argument("--timeout", type=float, default=5.0)
    sp.add_argument("--delay", type=float, default=0.0, help="milliseconds between keys")
    add("reset", op_reset).add_argument("--timeout", type=float, default=5.0)
    return p


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    # Check the version before parsing the rest, so that an old helper given options it
    # does not know still reports a mismatch (and gets replaced) instead of a usage error.
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--expect-version")
    expect = pre.parse_known_args(argv)[0].expect_version
    if expect and expect != own_version():
        error: dict[str, Any] = {"code": "version_mismatch", "message": "helper is out of date"}
        emit({"ok": False, "error": error, "warnings": []})
        return EXIT_VERSION
    args = build_parser().parse_args(argv)
    ctx = Ctx(path=args.path, sysroot=args.sysroot)
    try:
        result = args.func(ctx, args)
    except HelperError as e:
        error = {"code": e.code, "message": e.message, "hint": e.hint, **e.data}
        emit({"ok": False, "error": error, "warnings": ctx.warnings})
        return EXIT_NOT_ROOT if e.code == "not_root" else EXIT_FAIL
    except OSError as e:
        emit({"ok": False, "warnings": ctx.warnings,
              "error": {"code": "os_error", "message": str(e), "hint": None}})  # fmt: skip
        return EXIT_FAIL
    emit({"ok": True, "result": result, "warnings": ctx.warnings})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
