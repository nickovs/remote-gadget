"""Fixtures for testing the remote helper against a fake sysroot.

The helper is loaded from its file so these tests run on Python 3.9 without
importing the (3.10+) client package.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

HELPER_PATH = Path(__file__).parents[2] / "src/remote_gadget/remote/helper.py"
FIXTURES = Path(__file__).parents[1] / "fixtures"
UDC = "3f980000.usb"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rgadget_helper", HELPER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def helper() -> ModuleType:
    return _load()


class FakeHost:
    """A temp-dir sysroot with configfs, a UDC and an initialised state dir."""

    def __init__(self, root: Path):
        self.root = root / "sysroot"
        self.path = root / "state"
        (self.root / "sys/kernel/config/usb_gadget").mkdir(parents=True)
        self.udc_dir = self.root / "sys/class/udc" / UDC
        self.udc_dir.mkdir(parents=True)
        self.set_udc_state("not attached")
        for d in ("bin", "staging"):
            (self.path / d).mkdir(parents=True)
        (self.root / "etc").mkdir()
        (self.root / "etc/os-release").write_text(
            'ID=raspbian\nID_LIKE=debian\nPRETTY_NAME="Test"\n'
        )
        (self.root / "etc/machine-id").write_text("0123456789abcdef0123\n")

    def initialise(self, **state: object) -> None:
        st = {"initialised": True, "size": 64, "udc": None, "unplugged_at": None}
        st.update(state)
        (self.path / "state.json").write_text(json.dumps(st))

    def state(self) -> dict:
        return json.loads((self.path / "state.json").read_text())

    def set_udc_state(self, state: str) -> None:
        (self.udc_dir / "state").write_text(state + "\n")

    @property
    def gadget(self) -> Path:
        return self.root / "sys/kernel/config/usb_gadget/rgadget"

    def attr(self, rel: str) -> str:
        return (self.gadget / rel).read_text().strip()

    def make_hid(self) -> Path:
        """Create the hidg device lookup files; returns the FIFO standing in for /dev/hidg0."""
        (self.gadget / "functions/hid.usb0/dev").write_text("240:0\n")
        cls = self.root / "sys/class/hidg/hidg0"
        cls.mkdir(parents=True)
        (cls / "dev").write_text("240:0\n")
        (self.root / "dev").mkdir(exist_ok=True)
        fifo = self.root / "dev/hidg0"
        os.mkfifo(fifo)
        return fifo

    def args(self, *op: str) -> list[str]:
        return ["--path", str(self.path), "--sysroot", str(self.root), *op]


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    return FakeHost(tmp_path)


@pytest.fixture
def run(helper: ModuleType, host: FakeHost, capsys: pytest.CaptureFixture[str]):
    """Run a helper op against the fake host; returns (exit code, parsed JSON payload)."""

    def _run(*op: str):
        code = helper.main(host.args(*op))
        out = capsys.readouterr().out.strip().splitlines()
        return code, json.loads(out[-1])

    return _run


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout.encode(), stderr.encode())


@pytest.fixture
def fake_cmd(helper: ModuleType, monkeypatch: pytest.MonkeyPatch):
    """Replace helper.run_cmd; returns the list of commands run.  Set ``.reply`` to a
    function cmd -> CompletedProcess to customise results."""

    class Recorder(list):
        reply = staticmethod(lambda cmd: completed())

    rec = Recorder()

    def fake(cmd, input=None, env=None):
        rec.append(list(cmd))
        return rec.reply(cmd)

    monkeypatch.setattr(helper, "run_cmd", fake)
    return rec


class FifoReader:
    """Open a FIFO's read end now (so writers can open it) and drain it in the background."""

    def __init__(self, fifo, read: bool = True):
        import select
        import threading
        import time

        self.fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.data = bytearray()
        self._stop = threading.Event()

        def drain() -> None:
            while not self._stop.is_set():
                if select.select([self.fd], [], [], 0.05)[0]:
                    with contextlib.suppress(BlockingIOError):
                        self.data += os.read(self.fd, 65536)
                else:
                    time.sleep(0.01)

        self._thread = threading.Thread(target=drain, daemon=True)
        if read:
            self._thread.start()

    def close(self, expect: int = 0, timeout: float = 5.0) -> bytes:
        """Wait until ``expect`` bytes have arrived (or timeout), then close."""
        import time

        deadline = time.monotonic() + timeout
        while len(self.data) < expect and time.monotonic() < deadline:
            time.sleep(0.01)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(1)
        os.close(self.fd)
        return bytes(self.data)
