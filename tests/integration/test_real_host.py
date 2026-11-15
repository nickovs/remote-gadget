"""End-to-end tests against a real, initialised gadget host.

Skipped unless RGADGET_TEST_PROFILE (a profile name) or RGADGET_TEST_HOST is set.
They start and stop the gadget, insert and eject its media and write to its storage, but
never type or send
Ctrl-Alt-Del, since that would act on whatever computer the gadget is plugged into. The
target must not have the drive mounted.
"""

from __future__ import annotations

import os

import pytest

from remote_gadget import Config, Gadget, RemoteError, WrongStateError

pytestmark = pytest.mark.integration

PROFILE = os.environ.get("RGADGET_TEST_PROFILE")
HOST = os.environ.get("RGADGET_TEST_HOST")


@pytest.fixture(scope="module")
def gadget() -> Gadget:
    if not (PROFILE or HOST):
        pytest.skip("set RGADGET_TEST_PROFILE or RGADGET_TEST_HOST")
    g = Gadget(Config.load(profile=PROFILE, overrides={"host": HOST}))
    yield g
    g.eject()
    g.stop()


def test_doctor_has_no_failures(gadget):
    checks = gadget.doctor()["checks"]
    assert [c for c in checks if c["status"] == "fail"] == []


def test_storage_round_trip(gadget, tmp_path):
    gadget.eject()
    src = tmp_path / "hello.txt"
    src.write_bytes(os.urandom(100_000))
    gadget.write(str(src), "/rgadget-test/hello.txt")
    assert "HELLO" in gadget.ls("/rgadget-test").upper()
    out = tmp_path / "back.txt"
    gadget.read("/rgadget-test/hello.txt", str(out))
    assert out.read_bytes() == src.read_bytes()
    with pytest.raises(RemoteError, match="not empty"):
        gadget.rm("/rgadget-test")
    assert gadget.rm("/rgadget-test/hello.txt")["kind"] == "file"
    assert gadget.rm("/rgadget-test")["kind"] == "directory"
    with pytest.raises(RemoteError, match="not found"):
        gadget.rm("/rgadget-test")


def test_start_stop_and_media_cycle(gadget):
    gadget.start()
    gadget.eject()
    st = gadget.state()
    assert (st.state, st.inserted) == ("started", False)
    assert gadget.mount(ro=True)["media"] == {"inserted": True, "ro": True}
    st = gadget.state()
    assert (st.inserted, st.ro) == (True, True)
    with pytest.raises(WrongStateError):
        gadget.mount(ro=False)
    with pytest.raises(WrongStateError):
        gadget.write(__file__)
    assert gadget.eject()["changed"] is True
    assert gadget.mount(ro=False)["media"]["ro"] is False
    assert gadget.stop()["changed"] is True
    st = gadget.state()
    assert (st.state, st.inserted, st.ro) == ("stopped", True, False)  # media is remembered
    gadget.write(__file__, "/rgadget-test.py")  # fine while stopped
    assert gadget.rm("/rgadget-test.py")["kind"] == "file"
