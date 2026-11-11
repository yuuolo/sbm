"""Read-only SBM release checks. Never downloads or executes an update."""
import json
import re
import threading
import time
import urllib.request

VERSION = "1.0.0"
REPO = "https://github.com/yuuolo/sbm"
TTL = 6 * 3600
_lock = threading.Lock()
_cache = None
_attempt = 0


def version_tuple(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("发布版本号格式不受支持")
    return tuple(map(int, match.groups()))


def check(force=False):
    global _cache, _attempt
    with _lock:
        now = time.time()
        if _cache and now - _attempt < (60 if force else TTL):
            return dict(_cache)
        previous = _cache or {}
        result = {"current": VERSION, "latest": previous.get("latest"),
                  "available": False, "url": REPO + "/releases",
                  "checked": previous.get("checked"), "error": None}
        _attempt = now
        try:
            request = urllib.request.Request(
                "https://api.github.com/repos/yuuolo/sbm/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "sbm/" + VERSION})
            with urllib.request.urlopen(request, timeout=10) as response:
                release = json.loads(response.read(1024 * 1024))
            tag = release["tag_name"]
            latest = version_tuple(tag)
            if release.get("draft") or release.get("prerelease"):
                raise ValueError("未找到稳定发布版本")
            result.update(latest=tag, available=latest > version_tuple(VERSION),
                          url=REPO + "/releases/tag/" + tag, checked=int(now))
        except Exception as exc:
            result["error"] = "无法检查更新：" + str(exc)[:200]
            if result["latest"]:
                result["available"] = version_tuple(result["latest"]) > version_tuple(VERSION)
        _cache = result
        return dict(result)
