"""Ways out of the Seatbelt sandbox that must stay shut (macOS only)."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from ithaca import config, sandbox

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="needs Seatbelt")


@pytest.fixture
def outside(monkeypatch):
    monkeypatch.setattr(sandbox, "resolve_backend", _seatbelt)
    d = Path(tempfile.mkdtemp(prefix="ithaca-outside-", dir=Path.home() / ".ithaca" if (Path.home() / ".ithaca").exists() else None))
    (d / "canary.txt").write_text("original\n")
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def _seatbelt():
    return "seatbelt"


@pytest.mark.parametrize("cmd, target", [
    ("echo x > {o}/direct.txt", "direct.txt"),
    ("ln -s {o} out && echo x > out/via-link.txt", "via-link.txt"),
    ("ln -s {o}/canary.txt c.txt && echo pwned > c.txt", "canary.txt"),
    ("ln {o}/canary.txt hl.txt && echo pwned >> hl.txt", "canary.txt"),
])
async def test_writes_outside_the_workspace_are_blocked(outside, cmd, target):
    r = await sandbox.run(cmd.format(o=outside), timeout=20)
    f = outside / target
    assert not f.exists() or f.read_text() == "original\n", r.output
    assert r.exit_code != 0


async def test_symlink_into_tmp_is_blocked(outside):
    t = Path(tempfile.mkdtemp(prefix="ithaca-tmpprobe-", dir="/private/tmp"))
    try:
        r = await sandbox.run(f"ln -s {t} t && echo x > t/via-link.txt", timeout=20)
        assert not (t / "via-link.txt").exists() and r.exit_code != 0
    finally:
        shutil.rmtree(t, ignore_errors=True)


async def test_reads_outside_are_blocked_and_apps_cannot_be_scripted(outside):
    r = await sandbox.run(f"cat {outside}/canary.txt", timeout=20)
    assert "original" not in r.output
    r = await sandbox.run("osascript -e 'tell application \"Finder\" to get name of startup disk'", timeout=20)
    assert r.exit_code != 0


async def test_everyday_tools_still_work(outside):
    r = await sandbox.run("python3 -c 'print(6*7)' && git --version >/dev/null && d=$(mktemp -d) && echo ok > $d/f && cat $d/f",
                          timeout=60)
    assert r.exit_code == 0 and "42" in r.output and "ok" in r.output
    assert config.WORKSPACE_DIR.exists()


def test_a_seatbelt_that_stopped_enforcing_is_not_trusted(monkeypatch):
    import subprocess

    monkeypatch.setattr(sandbox, "_seatbelt_checked", None)
    real_run = subprocess.run

    def ignore_profile(argv, **kw):  # a sandbox-exec that runs the command but enforces nothing
        return real_run(argv[3:], **kw)

    monkeypatch.setattr(subprocess, "run", ignore_profile)
    assert sandbox._seatbelt_available() is False
    monkeypatch.setattr(sandbox, "_seatbelt_checked", None)
    monkeypatch.setattr(subprocess, "run", real_run)
    assert sandbox._seatbelt_available() is True
