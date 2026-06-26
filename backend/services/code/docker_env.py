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


def mount_source(mount_point: str) -> str | None:
    r"""Find the host source path for a bind mount.

    For 9p/WSL2 mounts the mountinfo source is the share root (e.g. ``G:\``)
    and the root field is the subdirectory inside that share; we combine them
    to get the real host path.
    """
    info = _mount_source_from_mountinfo(mount_point)
    if info:
        source, root = info
        if root and root != "/":
            # e.g. source=G:\, root=/Work/workflow/.fe-agent-work -> G:\Work\workflow\.fe-agent-work
            source = source.rstrip("/\\")
            root_win = root.replace("/", "\\")
            return f"{source}{root_win}"
        return source
    return _mount_source_from_mounts(mount_point)


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

    host_root = mount_source(container_mount)
    _warn("[docker_env] mount_source(%r) = %r", container_mount, host_root)
    if not host_root:
        host_root = os.getenv("AGENT_WORKDIR_HOST", "").strip() or "./.fe-agent-work"
        _warn("[docker_env] FALLBACK AGENT_WORKDIR_HOST = %r", host_root)

    suffix = workdir_str[len(container_mount) + 1:]
    # Normalize Windows drive-root paths like G:\ to G: before appending suffix.
    host_root = host_root.rstrip("\\/").replace("\\", "/")
    result = f"{host_root}/{suffix}"
    _warn("[docker_env] host_workdir result: %s", result)
    return result
