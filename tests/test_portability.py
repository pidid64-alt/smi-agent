"""Переносимость (Windows / Linux / macOS): кодировки, файл .env, MIME интерфейса, демо одной командой без bash."""

from __future__ import annotations

import ast
import io
import mimetypes
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smi_agent import cli
from smi_agent.cli import DEMO_MARKER, main
from smi_agent.config import Settings, check_env_file, get_settings
from smi_agent.demo.seed import DEFAULT_DEMO_PASSWORD, demo_password

ROOT = Path(__file__).resolve().parent.parent
MODE = re.compile(r"^[rwaxbt+]{1,4}$")


# --------------------------------------------------------------------------------- кодировки
def _implicit_text_io(tree: ast.AST) -> list[int]:
    """Строки, где текстовый файл открывается без encoding: на Windows это кодовая страница системы (cp1251/cp1252), а не UTF-8."""
    found: list[int] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or any(k.arg == "encoding" for k in n.keywords):
            continue
        f = n.func
        consts = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        mode = next((c for c in consts if MODE.match(c)), next((k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), None))
        if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
            found.append(n.lineno)
        elif isinstance(f, ast.Name) and f.id == "open" or isinstance(f, ast.Attribute) and f.attr == "fdopen":
            if "b" not in (mode or "r"):
                found.append(n.lineno)
        elif isinstance(f, ast.Attribute) and f.attr == "open" and (not n.args or (mode is not None and n.args and isinstance(n.args[0], ast.Constant))):
            if "b" not in (mode or "r"):  # Path.open(); os.open(path, flags) и Image.open(path) сюда не попадают
                found.append(n.lineno)
    return found


def test_text_files_are_always_opened_with_explicit_encoding():
    bad = []
    for folder in ("src/smi_agent", "tests", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            bad += [f"{path.relative_to(ROOT)}:{line}" for line in _implicit_text_io(ast.parse(path.read_text(encoding="utf-8")))]
    assert not bad, "текстовый ввод-вывод без encoding= (на Windows это cp1251/cp1252): " + ", ".join(bad)


def test_the_scanner_itself_catches_the_problem():
    bad = ast.parse("from pathlib import Path\nPath('a').read_text()\nopen('f')\nopen('f', 'rb')\nopen('f', encoding='utf-8')\nPath('x').open()\nPath('x').open('rb')\nos.open(p, os.O_RDONLY)\nImage.open(p)\nos.fdopen(fd, 'w')\n")
    assert _implicit_text_io(bad) == [2, 3, 6, 10]


def test_tolerant_stdio_never_raises_on_text_the_console_encoding_cannot_represent(monkeypatch):
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252", errors="strict"))  # Windows без русской локали, вывод в файл
    with pytest.raises(UnicodeEncodeError):
        print("Демо создано", flush=True)
    cli._tolerant_stdio()
    print("Демо создано", flush=True)
    assert b"\\u0414" in buf.getvalue()  # ничего не потеряно и ничего не упало


# --------------------------------------------------------------------------------- .env
def test_env_saved_as_utf16_by_powershell_redirect_is_reported_clearly(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("SMI_DEMO_MODE=1\n".encode("utf-16"))  # «echo ... > .env» в Windows PowerShell 5.1 пишет именно так (UTF-16 + BOM)
    with pytest.raises(RuntimeError, match="UTF-16"):
        check_env_file(env)
    env.write_bytes(b"SMI_DEMO_MODE=1\n")
    check_env_file(env)  # обычный файл и отсутствующий файл — не ошибка
    check_env_file(tmp_path / "missing.env")


def test_get_settings_fails_fast_on_utf16_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes("SMI_DEMO_MODE=1\n".encode("utf-16"))
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="UTF-8"):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["utf8", "utf8-bom"])
def test_env_file_with_russian_comments_and_optional_bom_is_read(tmp_path, monkeypatch, bom):
    monkeypatch.chdir(tmp_path)
    for key in ("SMI_DEMO_MODE", "SMI_EMBEDDED_WORKER"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_bytes(bom + "SMI_DEMO_MODE=1\n# комментарий по-русски: Имя, Ёлка\nSMI_EMBEDDED_WORKER=1\n".encode())
    st = Settings()
    assert st.demo_mode is True and st.embedded_worker is True  # BOM не ломает первую переменную


def test_demo_password_can_come_from_env_file_settings(monkeypatch):
    monkeypatch.delenv("SMI_DEMO_PASSWORD", raising=False)
    assert demo_password() == DEFAULT_DEMO_PASSWORD
    assert demo_password(Settings(demo_password="from-env-file-long-password")) == "from-env-file-long-password"
    monkeypatch.setenv("SMI_DEMO_PASSWORD", "from-environment-long-password")
    assert demo_password(Settings(demo_password="from-env-file-long-password")) == "from-environment-long-password"
    monkeypatch.setenv("SMI_DEMO_PASSWORD", "")
    assert demo_password() == DEFAULT_DEMO_PASSWORD  # пустое значение не означает «пароль пустой»


# --------------------------------------------------------------------------------- MIME интерфейса
def test_ui_modules_are_served_as_javascript_even_if_the_registry_says_text_plain(ctx):
    from smi_agent.api.app import create_app

    mimetypes.add_type("text/plain", ".js")  # так выглядит испорченный реестр Windows: браузер не исполнит <script type="module">
    assert mimetypes.guess_type("main.js")[0] == "text/plain"
    with TestClient(create_app(ctx)) as client:
        assert client.get("/static/js/main.js").headers["content-type"].startswith("text/javascript")
        assert client.get("/static/js/views/agenda.js").headers["content-type"].startswith("text/javascript")
        assert client.get("/static/css/app.css").headers["content-type"].startswith("text/css")
        assert client.get("/").headers["content-type"].startswith("text/html")


# --------------------------------------------------------------------------------- serve --demo
@pytest.fixture
def demo_cli(tmp_path, monkeypatch):
    """Вызов `serve --demo` без запуска сервера; окружение процесса и кэш настроек восстанавливаются."""
    before = dict(os.environ)
    calls: list[tuple] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append((a, k)))
    for key in ("SMI_ENV", "SMI_DEMO_PASSWORD", "SMI_DEMO_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # .env из рабочей папки разработчика не должен влиять
    get_settings.cache_clear()
    yield calls
    os.environ.clear()
    os.environ.update(before)
    get_settings.cache_clear()


def test_serve_demo_prepares_an_isolated_environment_without_bash(demo_cli, tmp_path, capsys):
    root = tmp_path / "demo"
    main(["serve", "--demo", "--demo-dir", str(root), "--port", "8765"])
    ((args, kwargs),) = demo_cli
    assert args == ("smi_agent.api.app:app_factory",) and kwargs["factory"] is True and kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8765
    st = get_settings()
    assert st.demo_mode and st.embedded_worker and st.env == "dev"
    assert Path(st.data_dir) == root.resolve() and Path(st.backup_dir) == root.resolve() / "backups"
    assert st.database_url == f"sqlite:///{(root.resolve() / 'demo.db').as_posix()}"  # прямые слэши: «sqlite:///C:/…» читается и на Windows
    assert (root / DEMO_MARKER).is_file()
    out = capsys.readouterr().out
    assert "http://localhost:8765" in out and "demo" in out and DEFAULT_DEMO_PASSWORD in out


def test_serve_demo_clears_only_a_directory_it_created_itself(demo_cli, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "important.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit, match="не пуст"):
        main(["serve", "--demo", "--demo-dir", str(foreign)])
    assert (foreign / "important.txt").read_text(encoding="utf-8") == "keep"
    own = tmp_path / "own"
    main(["serve", "--demo", "--demo-dir", str(own)])
    (own / "stale.db").write_text("old", encoding="utf-8")
    main(["serve", "--demo", "--demo-dir", str(own)])  # повторный запуск начинает с чистого состояния
    assert not (own / "stale.db").exists() and (own / DEMO_MARKER).is_file()


def test_serve_demo_is_refused_in_production(demo_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SMI_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="production"):
        main(["serve", "--demo", "--demo-dir", str(tmp_path / "d")])
    assert not (tmp_path / "d").exists() and demo_cli == []


def test_serve_demo_is_not_exposed_to_the_network_with_the_well_known_password(demo_cli, tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="общеизвестн"):
        main(["serve", "--demo", "--host", "0.0.0.0", "--demo-dir", str(tmp_path / "d")])  # noqa: S104
    assert demo_cli == []
    monkeypatch.setenv("SMI_DEMO_PASSWORD", "my own long demo password")
    get_settings.cache_clear()
    main(["serve", "--demo", "--host", "0.0.0.0", "--demo-dir", str(tmp_path / "d")])  # noqa: S104
    assert len(demo_cli) == 1


def test_serve_demo_environment_yields_a_working_application(demo_cli, tmp_path):
    from smi_agent.api.app import app_factory

    main(["serve", "--demo", "--demo-dir", str(tmp_path / "d")])
    with TestClient(app_factory()) as client:  # поднимается так же, как под uvicorn: создание БД и демо-данных — в lifespan
        r = client.post("/api/auth/login", json={"username": "demo", "password": DEFAULT_DEMO_PASSWORD})
        assert r.status_code == 200, r.text
        pid = client.get("/api/auth/me").json()["user"]["projects"][0]["project_id"]
        assert len(client.get(f"/api/p/{pid}/proposals").json()["proposals"]) == 5
    assert (tmp_path / "d" / "demo.db").is_file() and (tmp_path / "d" / "master.key").is_file()  # данные и dev-ключ — внутри каталога демо
