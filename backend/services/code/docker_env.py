"""Shared helpers for Docker-out-of-Docker agent container execution.

These utilities deal with UID/GID alignment and path translation when the
backend itself runs inside a Docker container and spawns sibling agent
containers that bind-mount host directories.
"""
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _warn(message: str, *args: object) -> None:
    """Emit a warning that shows up even with default gunicorn log levels."""
    logger.warning(message, *args)


def is_wsl() -> bool:
    """Detect WSL2, where Docker Desktop bind mounts often have UID mismatch."""
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def container_user() -> str:
    """Return the user the agent container should run as.

    Agent images run Claude / Codex CLIs, and Claude refuses permission
    bypasses when running as root. We therefore default to the image's
    non-root ``node`` user. On native Linux/macOS Docker where the backend
    runs as a non-root host user, matching that UID avoids file-ownership
    friction; on Windows/WSL2 the bind-mounted workdir is world-writable, so
    ``node`` works fine.

    Override with ``AGENT_CONTAINER_USER=node`` or
    ``AGENT_CONTAINER_USER=match-host``. ``match-host`` is ignored when the
    host UID is 0 to avoid the Claude root restriction.
    """
    mode = os.getenv("AGENT_CONTAINER_USER", "").lower()
    if mode == "node":
        return "node"
    if mode in ("match-host", "host"):
        try:
            uid = os.getuid()
            if uid == 0:
                _warn("AGENT_CONTAINER_USER=match-host ignored: host UID is 0 (root)")
                return "node"
            return f"{uid}:{os.getgid()}"
        except AttributeError:
            return "node"
    if mode:
        return mode
    # Auto-detect only on native Linux/macOS where the backend is not root.
    if sys.platform != "win32" and not is_wsl():
        try:
            uid = os.getuid()
            if uid != 0:
                return f"{uid}:{os.getgid()}"
        except AttributeError:
            pass
    return "node"


def _decode_mountinfo(value: str) -> str:
    """Decode octal escapes (\\040 -> space, \\134 -> backslash, etc.)."""
    def repl(match: re.Match) -> str:
        return chr(int(match.group(1), 8))

    return re.sub(r"\\(\d{3})", repl, value)


def _mount_source_from_mountinfo(
    mount_point: str,
) -> tuple[str, str] | None:
    r"""Return ``(host_source, root)`` from /proc/self/mountinfo.

    For ordinary bind mounts ``root`` is ``/`` and ``host_source`` is already
    the host path. For 9p/WSL2 mounts ``host_source`` is the share root
    (e.g. ``G:\``) and ``root`` is the subdirectory inside that share
    (e.g. ``/Work/workflow/.fe-agent-work``); the caller combines them.
    """
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                # mountinfo: ID parentID major:minor root mount_point opts - fs_type source ...
                if len(parts) < 10:
                    continue
                # mount_point itself may contain escaped spaces; decode before comparing.
                decoded_mount_point = _decode_mountinfo(parts[4])
                if decoded_mount_point == mount_point:
                    try:
                        sep = parts.index("-")
                    except ValueError:
                        continue
                    if len(parts) > sep + 2:
                        source = _decode_mountinfo(parts[sep + 2])
                        root = _decode_mountinfo(parts[3])
                        return source, root
    except OSError:
        pass
    return None


def _mount_source_from_mounts(mount_point: str) -> str | None:
    """Fallback to /proc/mounts (simpler, sometimes the only thing available)."""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                # /proc/mounts: source mount_point fs_type opts dump pass
                if len(parts) < 3:
                    continue
                if parts[1] == mount_point:
                    return parts[0]
    except OSError:
        pass
    return None


def _is_windows_source(source: str) -> bool:
    r"""True when a mountinfo source is a Windows drive (WSL2/9p), not a Linux device.

    WSL2/Docker-Desktop binds show ``source`` as a drive (``C:`` / ``G:\``) or a UNC
    path (``\\\\wsl$\\…``). A native-Linux bind shows ``source`` as a backing DEVICE
    (``/dev/root``, ``/dev/nvme0n1p1``, ``overlay``, ``tmpfs``, a UUID) — none of
    which match a drive letter.
    """
    s = source.strip()
    return bool(re.match(r"(?i)^[a-z]:", s)) or s.startswith("\\\\")


def _resolve_host_source(source: str, root: str) -> str:
    r"""Combine a mountinfo ``(source, root)`` into the host path of a bind mount.

    * WSL2/9p (Windows source): ``source`` is the share drive and ``root`` the
      subpath inside it — combine into a Windows path (``G:\`` + ``/Work/x`` →
      ``G:\Work\x``).
    * Native Linux: ``root`` is ALREADY the host path of the bound directory on its
      source filesystem (``/data/workflow/.fe-agent-work``); ``source`` is just the
      backing DEVICE (``/dev/root``) and must NOT be prepended — doing so yields
      ``/dev/root/data/workflow/.fe-agent-work``, which the host daemon can't
      resolve (the "failed to fulfil mount request … not a directory" failure).
    """
    if root and root != "/":
        if _is_windows_source(source):
            # e.g. source=G:\, root=/Work/workflow/.fe-agent-work -> G:\Work\workflow\.fe-agent-work
            return source.rstrip("/\\") + root.replace("/", "\\")
        return root
    return source


def mount_source(mount_point: str) -> str | None:
    r"""Find the host source path for a bind mount (Linux + WSL2/9p)."""
    info = _mount_source_from_mountinfo(mount_point)
    if info:
        return _resolve_host_source(*info)
    return _mount_source_from_mounts(mount_point)


def mount_failure_hint(stderr: str | None) -> str:
    """An actionable hint when ``docker run`` died at the bind-mount / OCI layer.

    The agent container couldn't even be CREATED because the host daemon couldn't
    mount the workdir (``failed to fulfil mount request`` / ``not a directory``).
    In this Docker-out-of-Docker setup that almost always means a STALE bind mount:
    the backend's ``/data/workflow/.fe-agent-work`` no longer points at the host's
    current ``.fe-agent-work`` inode (e.g. the dir was recreated / re-mounted AFTER
    the backend container started, so new tmpdirs created inside the container are
    invisible to the host daemon). The agent never ran — recreating the backend
    container re-establishes the bind. Returns "" when the stderr isn't a mount fail.
    """
    s = (stderr or "").lower()
    signals = (
        "failed to fulfil mount request",
        "oci runtime create failed",
        "error response from daemon: failed to create",
    )
    if any(k in s for k in signals) or ("mount" in s and "not a directory" in s):
        return (
            "Docker-out-of-Docker 挂载失败:后端容器无法把工作目录挂进 agent 容器"
            "(沙箱 .fe-agent-work 绑定挂载已失效——通常是该目录在后端容器启动后被重建/重挂,"
            "容器内仍指向旧 inode,新建的工作目录宿主侧看不到)。Agent 未运行,非生成内容问题。"
            "修复:重建后端容器以重新绑定 —— `docker compose up -d --force-recreate backend`"
            "(或 `make redeploy`),然后重试。"
        )
    return ""


def host_workdir(workdir: Path) -> str:
    """Convert an in-container workdir path to the host path for ``docker run -v``.

    When the backend itself runs inside Docker and spawns sibling agent
    containers via Docker-out-of-Docker, the host Docker daemon interprets the
    ``-v`` source path as a host path. We first try to read the real host
    source from ``/proc/self/mountinfo`` / ``/proc/mounts``. If that fails, we
    fall back to ``AGENT_WORKDIR_HOST`` or the legacy relative path.
    """
    container_mount = "/data/workflow/.fe-agent-work"
    workdir_str = str(workdir).replace("\\", "/")
    _warn("[docker_env] host_workdir input: %s", workdir_str)

    if not workdir_str.startswith(container_mount + "/"):
        _warn("[docker_env] workdir not under container mount, returning as-is")
        return workdir_str

    # An explicit AGENT_WORKDIR_HOST overrides auto-detection (escape hatch when a
    # host's mountinfo can't be resolved correctly); else auto-detect; else fall
    # back to the legacy relative path.
    host_root = os.getenv("AGENT_WORKDIR_HOST", "").strip() or mount_source(container_mount)
    _warn("[docker_env] resolved host_root = %r", host_root)
    if not host_root:
        host_root = "./.fe-agent-work"
        _warn("[docker_env] FALLBACK host_root = %r", host_root)

    suffix = workdir_str[len(container_mount) + 1:]
    # Normalize Windows drive-root paths like G:\ to G: before appending suffix.
    host_root = host_root.rstrip("\\/").replace("\\", "/")
    result = f"{host_root}/{suffix}"
    _warn("[docker_env] host_workdir result: %s", result)
    return result
