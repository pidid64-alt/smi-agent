#!/usr/bin/env python3
"""Сквозная проверка демо «как у пользователя»: запускает сервер, входит под demo, проверяет интерфейс и API, останавливает сервер.

Только стандартная библиотека: одинаково запускается на Windows, Linux и macOS (в CI — на Windows и Linux).
Вывод — ASCII, чтобы не зависеть от кодовой страницы консоли.

    python scripts/smoke_demo.py                     # smi-agent serve --demo --port 8123
    python scripts/smoke_demo.py --port 8124 -- cmd /c scripts\\run-demo.cmd --port 8124   # проверить Windows-лаунчер

Проверяется: интерфейс и все его ES-модули отдаются с JavaScript-типом (на Windows тип берётся из реестра), вход и сессия,
пять предложений, выбор темы → генерация материала → карточка-изображение (Pillow + шрифты), здоровье системы.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_REF = re.compile(r"""(?:from\s*|import\s*\(\s*)["'](\.{1,2}/[^"']+\.js)["']""")
ASSET_REF = re.compile(r"""(?:src|href)=["'](/static/[^"']+)["']""")


class Fail(Exception):
    pass


class Client:
    """Минимальный HTTP-клиент с cookie-сессией. Прокси отключены: проверяем локальный сервер."""

    def __init__(self, base: str) -> None:
        self.base, self.csrf = base, ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict[str, str], bytes]:
        headers = {"Accept": "*/*"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310 — локальный http
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def json(self, method: str, path: str, body: dict | None = None, expect: int = 200):
        status, _, raw = self.call(method, path, body)
        if status != expect:
            raise Fail(f"{method} {path}: expected HTTP {expect}, got {status}: {raw[:300]!r}")
        return json.loads(raw) if raw else None


def step(ok: bool, what: str) -> None:
    if not ok:
        raise Fail(what)
    print(f"  ok  {what}", flush=True)


def stop_tree(proc: subprocess.Popen) -> None:
    """Останавливает процесс вместе с потомками (лаунчер .cmd запускает python как дочерний процесс)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603, S607
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_ready(client: Client, proc: subprocess.Popen, timeout: float) -> None:
    deadline, last = time.monotonic() + timeout, "no response"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise Fail(f"server exited early with code {proc.returncode}")
        try:
            status, _, _ = client.call("GET", "/api/health/live", timeout=3)
            if status == 200:
                return
            last = f"HTTP {status}"
        except OSError as e:  # сервер ещё поднимается (создание демо-данных занимает несколько секунд)
            last = type(e).__name__
        time.sleep(0.5)
    raise Fail(f"server did not become ready in {timeout:.0f}s ({last})")


def check_frontend(c: Client) -> None:
    status, hdr, body = c.call("GET", "/")
    html = body.decode("utf-8", "replace")
    step(status == 200 and "text/html" in hdr.get("content-type", "") and 'type="module"' in html, "GET / returns the UI page")
    queue = [urllib.parse.urljoin("/", u) for u in ASSET_REF.findall(html)]
    seen: set[str] = set()
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        status, hdr, raw = c.call("GET", url)
        ctype = hdr.get("content-type", "")
        want = "javascript" if url.endswith(".js") else "text/css" if url.endswith(".css") else ""
        if status != 200 or (want and want not in ctype):
            raise Fail(f"{url}: HTTP {status}, Content-Type {ctype!r} (expected {want or 'any'}) - browsers refuse ES modules with a non-JavaScript type")
        if url.endswith(".js"):
            queue += [urllib.parse.urljoin(url, ref) for ref in MODULE_REF.findall(raw.decode("utf-8", "replace"))]
    step(len(seen) >= 10, f"all {len(seen)} UI files (html, css, ES modules) are served with correct Content-Type")


def check_api(c: Client, password: str) -> None:
    status, _, _ = c.call("GET", "/api/auth/me")
    step(status == 401, "API requires login (401 without a session)")
    login = c.json("POST", "/api/auth/login", {"username": "demo", "password": password})
    c.csrf = login["csrf"]
    step(bool(c.csrf), "login as demo works")
    me = c.json("GET", "/api/auth/me")
    step(me["demo"] is True and me["env"] == "dev", "session is valid, demo mode is on")
    pid = me["user"]["projects"][0]["project_id"]
    props = c.json("GET", f"/api/p/{pid}/proposals")["proposals"]
    step(len(props) == 5, f"5 proposals in the funnel (got {len(props)})")
    events = c.json("GET", f"/api/p/{pid}/events")
    step(bool(events), "events are listed")
    c.json("POST", f"/api/p/{pid}/proposals/1/select", {})
    slot1 = next(p for p in c.json("GET", f"/api/p/{pid}/proposals")["proposals"] if p["slot"] == 1)
    step(slot1["content_pk"] is not None, "selecting proposal 1 created a content item")
    content = c.json("GET", f"/api/p/{pid}/content/{slot1['content_pk']}")
    step({v["platform"] for v in content["versions"]} == {"telegram", "instagram", "facebook"}, "native versions for Telegram, Instagram and Facebook")
    asset_ids = [m["asset_id"] for v in content["versions"] for m in v["media"]]
    step(bool(asset_ids), "a cover card was rendered")
    status, hdr, raw = c.call("GET", f"/api/p/{pid}/assets/{asset_ids[0]}")
    step(status == 200 and raw[:3] == b"\xff\xd8\xff" and "image/jpeg" in hdr.get("content-type", ""), f"cover card is a valid JPEG ({len(raw)} bytes, Pillow + fonts work)")
    health = c.json("GET", "/api/health")
    step(health["status"] in ("ok", "warn"), f"health endpoint answers (status={health['status']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--timeout", type=float, default=240, help="seconds to wait for the server")
    ap.add_argument("cmd", nargs="*", help="command that starts the server (after --); default: smi-agent serve --demo")
    args = ap.parse_args()
    cmd = args.cmd or [sys.executable, "-m", "smi_agent.cli", "serve", "--demo", "--port", str(args.port)]
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}  # как у обычного пользователя: кодировка по умолчанию
    env.setdefault("SMI_NO_PAUSE", "1")
    password = os.environ.get("SMI_DEMO_PASSWORD", "smi-agent-showcase")
    log = tempfile.NamedTemporaryFile(prefix="smi-smoke-", suffix=".log", delete=False)
    kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    print(f"starting: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)  # noqa: S603
    client = Client(f"http://127.0.0.1:{args.port}")
    try:
        wait_ready(client, proc, args.timeout)
        print("server is up", flush=True)
        check_frontend(client)
        check_api(client, password)
    except Fail as e:
        print(f"FAIL: {e}", flush=True)
        return _report(proc, log, failed=True)
    except Exception as e:  # noqa: BLE001 — любая неожиданность тоже провал проверки
        print(f"FAIL: unexpected {type(e).__name__}: {e}", flush=True)
        return _report(proc, log, failed=True)
    return _report(proc, log, failed=False)


def _report(proc: subprocess.Popen, log, *, failed: bool) -> int:
    stop_tree(proc)
    log.close()
    text = Path(log.name).read_text(encoding="utf-8", errors="replace")
    if failed:
        print("---- server output (tail) ----")
        print("\n".join(text.splitlines()[-60:]).encode("ascii", "replace").decode("ascii"))
        print("------------------------------")
    else:
        # в выводе сервера не должно быть необработанных исключений
        bad = [ln for ln in text.splitlines() if "Traceback (most recent call last)" in ln]
        if bad:
            print(f"FAIL: server log contains {len(bad)} traceback(s)")
            print("\n".join(text.splitlines()[-60:]).encode("ascii", "replace").decode("ascii"))
            failed = True
        else:
            print("SMOKE OK")
    try:
        Path(log.name).unlink()
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
