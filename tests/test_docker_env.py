"""
Docker-out-of-Docker host-path resolution.

Regression: on a native-Linux host whose root filesystem device appears as
``/dev/root`` in mountinfo, ``mount_source`` combined the backing DEVICE with the
bind root (a WSL-only trick), producing ``/dev/root/data/workflow/.fe-agent-work``
as the ``docker run -v`` source — which the host daemon can't resolve, so EVERY
fe-agent/be-agent container failed at create time with "failed to fulfil mount
request … not a directory". The device must NOT be prepended on Linux: the
mountinfo ``root`` field is already the host path.
"""
from pathlib import Path

from backend.services.code import docker_env
from backend.services.code.docker_env import _resolve_host_source


def test_resolve_host_source_linux_does_not_prepend_device():
    # Native Linux bind: root IS the host path; the /dev/root device must drop out.
    assert (
        _resolve_host_source("/dev/root", "/data/workflow/.fe-agent-work")
        == "/data/workflow/.fe-agent-work"
    )
    assert (
        _resolve_host_source("/dev/nvme0n1p1", "/srv/app/.fe-agent-work")
        == "/srv/app/.fe-agent-work"
    )
    # Whole-filesystem mount (root == "/") → the source as-is.
    assert _resolve_host_source("/dev/root", "/") == "/dev/root"


def test_resolve_host_source_wsl_drive_preserved():
    # WSL2/9p: the Windows drive + subpath ARE combined into a Windows path.
    assert (
        _resolve_host_source("G:\\", "/Work/workflow/.fe-agent-work")
        == "G:\\Work\\workflow\\.fe-agent-work"
    )


def test_host_workdir_linux_no_device_prefix(monkeypatch):
    """End-to-end: the buggy /dev/root prefix never reaches the -v source again."""
    monkeypatch.delenv("AGENT_WORKDIR_HOST", raising=False)
    monkeypatch.setattr(
        docker_env,
        "_mount_source_from_mountinfo",
        lambda mp: ("/dev/root", "/data/workflow/.fe-agent-work"),
    )
    result = docker_env.host_workdir(Path("/data/workflow/.fe-agent-work/fe-agent-676syp1n"))
    assert result == "/data/workflow/.fe-agent-work/fe-agent-676syp1n"
    assert "/dev/root" not in result


def test_host_workdir_env_override_takes_precedence(monkeypatch):
    """AGENT_WORKDIR_HOST is an escape hatch that overrides auto-detection."""
    monkeypatch.setenv("AGENT_WORKDIR_HOST", "/custom/host/root")
    result = docker_env.host_workdir(Path("/data/workflow/.fe-agent-work/fe-agent-abc"))
    assert result == "/custom/host/root/fe-agent-abc"
