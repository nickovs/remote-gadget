"""End-to-end tests against a real, initialised gadget host.

Skipped unless RGADGET_TEST_PROFILE (a profile name) or RGADGET_TEST_HOST is set.
They plug and unplug the gadget and write to its storage, but never type or send
Ctrl-Alt-Del, since that would act on whatever computer the gadget is plugged into.
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
    g.unplug()


def test_doctor_has_no_failures(gadget):
    checks = gadget.doctor()["checks"]
    assert [c for c in checks if c["status"] == "fail"] == []


def test_storage_round_trip(gadget, tmp_path):
    gadget.unplug()
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


def test_plug_cycle(gadget):
    gadget.unplug()
    assert gadget.state().state == "unplugged"
    assert gadget.plug(ro=True)["ro"] is True
    st = gadget.state()
    assert (st.state, st.ro) == ("plugged", True)
    with pytest.raises(WrongStateError):
        gadget.plug(ro=False)
    with pytest.raises(WrongStateError):
        gadget.write(__file__)
    assert gadget.unplug()["changed"] is True
    assert gadget.plug(ro=False)["ro"] is False
    assert gadget.unplug()["changed"] is True
