"""Командная строка `smi-agent`: инициализация, запуск, воркер, ключи, пользователи, резервные копии, демо, диагностика."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select


def _ctx():
    from .config import get_settings
    from .container import Container
    from .security.redaction import install_log_redaction

    logging.basicConfig(level=os.environ.get("SMI_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    c = Container(get_settings())
    c.db.create_all()
    return c


def _project_id(ctx, slug: str | None) -> int:
    from .db.models import Project

    with ctx.db.read() as s:
        q = select(Project.id).where(Project.archived_at.is_(None))
        if slug:
            q = q.where(Project.slug == slug)
        pid = s.scalar(q.order_by(Project.id))
    if pid is None:
        sys.exit("Проект не найден. Выполните: smi-agent init")
    return pid


def _password(prompt: str) -> str:
    env = os.environ.get("SMI_ADMIN_PASSWORD")
    if env:
        return env  # для автоматизации (Docker secrets, CI); пароли в аргументах командной строки не принимаются намеренно
    if not sys.stdin.isatty():
        sys.exit("Пароль не задан: используйте интерактивный ввод или переменную SMI_ADMIN_PASSWORD")
    pw = getpass.getpass(prompt)
    if pw != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    return pw


def cmd_init(a: argparse.Namespace) -> None:
    from .db.models import Project, User
    from .security.rbac import Actor

    ctx = _ctx()
    with ctx.db.read() as s:
        exists = s.scalar(select(User.id).where(User.username == a.admin.lower()))
    with ctx.db.session() as s:
        pid = s.scalar(select(Project.id).where(Project.slug == a.project))
        if pid is None:
            p = Project(slug=a.project, name=a.name or a.project, settings={})
            s.add(p)
            s.flush()
            pid = p.id
            n = ctx.ingest.seed_sources(s, pid, Actor.system(), extra_yaml=ctx.settings.config_dir / "sources.yaml")
            print(f"Проект «{a.project}» создан, источников добавлено: {n}")
        if not exists:
            ctx.auth.create_user(s, None, username=a.admin, password=_password(f"Пароль администратора {a.admin}: "), project_id=pid, role="admin", superadmin=True)
            print(f"Администратор «{a.admin}» создан")
    print("Готово. Запуск: smi-agent serve  и  smi-agent worker")
    if not ctx.settings.master_keys.get_secret_value():
        print("Внимание: SMI_MASTER_KEYS не задан — создан локальный dev-ключ (data/master.key). Для production: smi-agent keys generate")


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("smi_agent.api.app:app_factory", factory=True, host=a.host, port=a.port, log_level="info", proxy_headers=False)


def cmd_worker(_a: argparse.Namespace) -> None:
    from .ops.worker import Worker

    Worker(_ctx()).run_forever()


def cmd_keys(_a: argparse.Namespace) -> None:
    import base64
    import secrets

    from .security.secrets import generate_key_entry

    print("# Добавьте в секреты окружения (НЕ в Git):")
    print(f"SMI_MASTER_KEYS={generate_key_entry()}")
    print(f"SMI_BACKUP_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}")
    print("# Ротация: добавьте новый ключ ПЕРВЫМ через запятую, затем выполните `smi-agent keys rotate`.")


def cmd_keys_rotate(_a: argparse.Namespace) -> None:
    from .security.rbac import Actor

    ctx = _ctx()
    with ctx.db.session() as s:
        n = ctx.secrets.rotate_master_key(s, Actor.system())
    print(f"Перешифровано секретов: {n}")


def cmd_user(a: argparse.Namespace) -> None:
    from .db.models import User

    ctx = _ctx()
    if a.action == "create":
        pid = _project_id(ctx, a.project)
        with ctx.db.session() as s:
            ctx.auth.create_user(s, None, username=a.username, password=_password(f"Пароль {a.username}: "), project_id=pid, role=a.role)
        print("Пользователь создан")
    else:
        from .security.passwords import check_password_policy, hash_password

        pw = _password("Новый пароль: ")
        with ctx.db.session() as s:
            u = s.scalars(select(User).where(User.username == a.username.lower())).first()
            if u is None:
                sys.exit("Пользователь не найден")
            problems = check_password_policy(pw, production=ctx.settings.is_production, username=u.username)
            if problems:
                sys.exit("; ".join(problems))
            u.password_hash, u.failed_logins, u.locked_until = hash_password(pw), 0, None
            ctx.auth.revoke_sessions(s, u.id)
            from .security.rbac import Actor

            ctx.audit.log(s, Actor.system(), "user.password_reset_cli", target_type="user", target_id=u.id)
        print("Пароль изменён, сессии отозваны")


def cmd_backup(a: argparse.Namespace) -> None:
    ctx = _ctx()
    if a.action == "create":
        r = ctx.backup.create(note="cli")
        print(json.dumps(r, ensure_ascii=False))
    elif a.action == "list":
        for b in ctx.backup.list():
            print(f"{b['id']:>4} {b['ts']} {b['size_bytes']:>10} {b['verify_status'] or '-':>7} {b['path']}")
    elif a.action == "verify":
        r = ctx.backup.verify(a.path)
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r["ok"] else 1)
    elif a.action == "restore":
        if not a.target:
            sys.exit("Укажите --target путь к файлу БД")
        print(json.dumps(ctx.backup.restore(a.path, a.target, force=a.force), ensure_ascii=False))
    elif a.action == "restore-test":
        r = ctx.backup.restore_test(a.path)
        print(json.dumps(r, ensure_ascii=False, default=str))
        sys.exit(0 if r["ok"] else 1)


def cmd_demo(a: argparse.Namespace) -> None:
    from .config import get_settings

    st = get_settings()
    if st.is_production:
        sys.exit("Демо-данные запрещены в production")
    if a.action == "reset":
        ctx = _ctx()
        path = ctx.db.file_path()
        ctx.close()
        if path and path.exists():
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
    os.environ["SMI_DEMO_MODE"] = "1"
    from .config import get_settings as gs

    gs.cache_clear()
    ctx = _ctx()
    from .demo.seed import seed_demo_if_empty

    print("Демо создано" if seed_demo_if_empty(ctx) else "БД не пуста: используйте `smi-agent demo reset`")


def cmd_run(a: argparse.Namespace) -> None:
    from .security.rbac import Actor

    ctx = _ctx()
    pid = _project_id(ctx, a.project)
    if a.what == "pipeline":
        for r in ctx.ingest.poll_due(pid):
            print(f"{r.source_key}: {r.status}")
        ctx.events.process_new(pid)
        with ctx.db.session() as s:
            print("оценено событий:", ctx.scoring.score_recent(s, pid))
    else:
        ctx.events.process_new(pid)
        with ctx.db.session() as s:
            run = ctx.funnel.run(s, pid, Actor.system(), trigger="cli")
            print(json.dumps({"counts": run.counts, "geo_ratio": run.geo_ratio}, ensure_ascii=False))


def cmd_health(_a: argparse.Namespace) -> None:
    ctx = _ctx()
    r = ctx.health.check(record=False)
    for name, c in r["components"].items():
        print(f"{c['status'].upper():5} {name:12} {c['summary']}")
    sys.exit({"ok": 0, "warn": 0, "fail": 2}[r["status"]])


def cmd_audit(_a: argparse.Namespace) -> None:
    ctx = _ctx()
    with ctx.db.read() as s:
        r = ctx.audit.verify_chain(s)
    print("Цепочка аудита цела, записей:" if r.ok else f"НАРУШЕНИЕ: запись {r.first_bad_id} — {r.reason}; проверено:", r.checked)
    sys.exit(0 if r.ok else 1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="smi-agent", description="AI-главный редактор: мониторинг, отбор тем, контент, публикация, аналитика")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="создать БД, проект с источниками и администратора")
    i.add_argument("--admin", default="admin")
    i.add_argument("--project", default="main")
    i.add_argument("--name", default="")
    i.set_defaults(fn=cmd_init)
    s_ = sub.add_parser("serve", help="запустить веб-сервер и API")
    s_.add_argument("--host", default="127.0.0.1")
    s_.add_argument("--port", type=int, default=8000)
    s_.set_defaults(fn=cmd_serve)
    sub.add_parser("worker", help="запустить воркер фоновых заданий").set_defaults(fn=cmd_worker)
    k = sub.add_parser("keys", help="ключи шифрования")
    ks = k.add_subparsers(dest="kaction", required=True)
    ks.add_parser("generate").set_defaults(fn=cmd_keys)
    ks.add_parser("rotate").set_defaults(fn=cmd_keys_rotate)
    u = sub.add_parser("user", help="пользователи")
    u.add_argument("action", choices=["create", "reset-password"])
    u.add_argument("--username", required=True)
    u.add_argument("--role", default="user", choices=["user", "admin", "auditor"])
    u.add_argument("--project", default=None)
    u.set_defaults(fn=cmd_user)
    b = sub.add_parser("backup", help="резервные копии (шифрованные)")
    b.add_argument("action", choices=["create", "list", "verify", "restore", "restore-test"])
    b.add_argument("path", nargs="?", default=None)
    b.add_argument("--target", default=None)
    b.add_argument("--force", action="store_true")
    b.set_defaults(fn=cmd_backup)
    d = sub.add_parser("demo", help="демо-данные (вымышленные новости, песочница)")
    d.add_argument("action", choices=["seed", "reset"])
    d.set_defaults(fn=cmd_demo)
    r = sub.add_parser("run", help="ручной запуск этапов")
    r.add_argument("what", choices=["pipeline", "funnel"])
    r.add_argument("--project", default=None)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("health", help="состояние системы (код выхода 2 при критической проблеме)").set_defaults(fn=cmd_health)
    sub.add_parser("audit-verify", help="проверить хеш-цепочку журнала аудита").set_defaults(fn=cmd_audit)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
