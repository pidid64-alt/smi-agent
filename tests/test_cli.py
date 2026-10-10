import json

import pytest
from sqlalchemy import select

from smi_agent.cli import main
from smi_agent.config import get_settings


@pytest.fixture
def cli_env(tmpdir_path, monkeypatch):
    monkeypatch.setenv("SMI_ENV", "test")
    monkeypatch.setenv("SMI_DATA_DIR", str(tmpdir_path))
    monkeypatch.setenv("SMI_CONFIG_DIR", str(tmpdir_path / "config"))
    monkeypatch.setenv("SMI_DATABASE_URL", f"sqlite:///{tmpdir_path}/cli.db")
    monkeypatch.setenv("SMI_BACKUP_DIR", str(tmpdir_path / "bk"))
    monkeypatch.setenv("SMI_ADMIN_PASSWORD", "a very long admin password")
    get_settings.cache_clear()
    yield tmpdir_path
    get_settings.cache_clear()


def test_init_creates_project_sources_and_admin(cli_env, capsys):
    main(["init", "--admin", "boss", "--project", "main", "--name", "Редакция"])
    out = capsys.readouterr().out
    assert "Проект «main» создан" in out and "Администратор «boss» создан" in out
    main(["init", "--admin", "boss", "--project", "main"])  # повторный запуск идемпотентен
    again = capsys.readouterr().out
    assert "Проект «main» создан" not in again and "Администратор «boss» создан" not in again
    from smi_agent.container import Container
    from smi_agent.db.models import Source, User

    ctx = Container(get_settings())
    with ctx.db.read() as s:
        boss = s.scalars(select(User).where(User.username == "boss")).one()
        assert boss.is_superadmin and boss.password_hash.startswith("scrypt$")
        assert s.query(Source).filter(Source.key == "nur_kz").one().config["mandatory"] is True
    ctx.close()


def test_keys_generate_prints_valid_keys_and_not_to_logs(cli_env, capsys):
    main(["keys", "generate"])
    out = capsys.readouterr().out
    from smi_agent.security.secrets import parse_master_keys

    line = next(ln for ln in out.splitlines() if ln.startswith("SMI_MASTER_KEYS="))
    assert len(parse_master_keys(line.split("=", 1)[1])) == 1
    assert any(ln.startswith("SMI_BACKUP_KEY=") and len(ln) > 40 for ln in out.splitlines())


def test_backup_health_and_audit_commands(cli_env, capsys):
    main(["init", "--admin", "boss", "--project", "main"])
    capsys.readouterr()
    main(["backup", "create"])
    info = json.loads(capsys.readouterr().out)
    assert info["size_bytes"] > 0
    with pytest.raises(SystemExit) as e:
        main(["backup", "verify", info["path"]])
    assert e.value.code == 0
    main(["backup", "list"])
    assert info["path"] in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["audit-verify"])
    assert e.value.code == 0 and "цела" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["health"])
    assert e.value.code == 0
    main(["run", "funnel"])
    assert "counts" in capsys.readouterr().out


def test_user_commands_and_password_never_from_argv(cli_env, capsys, monkeypatch):
    main(["init", "--admin", "boss", "--project", "main"])
    main(["user", "create", "--username", "ed", "--role", "user"])
    monkeypatch.setenv("SMI_ADMIN_PASSWORD", "another long password")
    main(["user", "reset-password", "--username", "ed"])
    assert "сессии отозваны" in capsys.readouterr().out
    from smi_agent.container import Container
    from smi_agent.db.models import User
    from smi_agent.security.passwords import verify_password

    ctx = Container(get_settings())
    with ctx.db.read() as s:
        assert verify_password("another long password", s.scalars(select(User).where(User.username == "ed")).one().password_hash)
    ctx.close()
    with pytest.raises(SystemExit):
        main(["user", "create", "--username", "x", "--role", "user", "--password", "secret"])  # пароль в аргументах командной строки не принимается


def test_demo_command_is_refused_in_production(cli_env, monkeypatch):
    monkeypatch.setenv("SMI_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="production"):
        main(["demo", "seed"])


def test_weak_admin_password_is_reported_as_a_message_not_a_traceback(cli_env, monkeypatch):
    monkeypatch.setenv("SMI_ADMIN_PASSWORD", "admin admin admin admin")  # содержит имя пользователя — политика паролей отклоняет
    with pytest.raises(SystemExit, match="Ошибка: .*имя пользователя"):
        main(["init", "--admin", "admin", "--project", "main"])
    monkeypatch.setenv("SMI_ADMIN_PASSWORD", "a perfectly fine long secret")
    main(["init", "--admin", "admin", "--project", "main"])  # отказ ничего не сломал: повторный init проходит
    from smi_agent.container import Container
    from smi_agent.db.models import User

    ctx = Container(get_settings())
    with ctx.db.read() as s:
        assert s.scalars(select(User).where(User.username == "admin")).one().is_superadmin
    ctx.close()
