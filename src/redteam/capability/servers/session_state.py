"""Read operator-exported Playwright cookies without exposing them to an Agent."""

import json
import os
import re
import stat
import time
from urllib.parse import urlsplit

from redteam.capability.servers.config import SessionProfile


def cookie_header(session: SessionProfile, origin: str, path: str) -> str:
    # Import locally to keep network's public ToolInputError stable for existing callers.
    from redteam.capability.servers.network import ToolInputError

    assert session.storage_state_path is not None
    try:
        descriptor = os.open(session.storage_state_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or time.time() - info.st_mtime > session.storage_state_max_age_seconds
                or info.st_mtime > time.time() + 60
            ):
                raise ValueError
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError
        state = json.loads(raw)
        cookies = state["cookies"]
        if not isinstance(cookies, list) or len(cookies) > 200:
            raise ValueError
        target = urlsplit(origin)
        selected: list[tuple[int, str]] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise ValueError
            domain = cookie.get("domain", "")
            # Narrower than browser domain suffix matching: dedicated exact-host snapshots.
            if domain != target.hostname:
                continue
            route = cookie.get("path", "/")
            if not isinstance(route, str) or not route.startswith("/"):
                raise ValueError
            if not (path == route or path.startswith(route.rstrip("/") + "/")):
                continue
            expiry = cookie.get("expires", -1)
            if not isinstance(expiry, (int, float)):
                raise ValueError
            if expiry != -1 and expiry <= time.time():
                continue
            secure = cookie.get("secure", False)
            if not isinstance(secure, bool):
                raise ValueError
            if secure and target.scheme != "https":
                continue
            if cookie.get("partitionKey") is not None:
                continue
            name, value = cookie.get("name"), cookie.get("value")
            if (
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name)
                or not isinstance(value, str)
                or any(ord(c) < 33 or ord(c) > 126 or c in ';,"\\' for c in value)
            ):
                raise ValueError
            selected.append((len(route), f"{name}={value}"))
        result = "; ".join(value for _, value in sorted(selected, reverse=True))
        if not result or len(result) > 8192:
            raise ValueError
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ToolInputError("browser session unavailable, stale or outside cookie scope") from exc
