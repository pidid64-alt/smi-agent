import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from smi_agent.config import Settings
from smi_agent.container import Container
from smi_agent.db.models import (
    HealthRecord,
    JobLease,
    Notification,
    PlatformAccount,
    Project,
    Publication,
    Source,
)
from smi_agent.ops.backup import BackupError
from smi_agent.ops.worker import Job, Worker


def _seed(ctx, admin):
    with ctx.db.session() as s:
        s.add(Project(slug="p", name="P", settings={}))
    with ctx.db.session() as s:
        ctx.audit.log(s, admin, "seed.event", project_id=1, details={"marker": "PLAINTEXT-MARKER-XYZ"})


def test_backup_is_encrypted_verifiable_and_restorable(ctx, admin):
    _seed(ctx, admin)
    res = ctx.backup.create()
    blob = Path(res["path"]).read_bytes()
    assert blob.startswith(b"SMIBK1") and b"PLAINTEXT-MARKER-XYZ" not in blob and b"SQLite format" not in blob
    if os.name != "nt":  # права 0600 — понятие POSIX; на Windows доступ к файлу задаёт ACL каталога (см. docs/OPERATIONS.md)
        assert oct(Path(res["path"]).stat().st_mode)[-3:] == "600"
    v = ctx.backup.verify(res["path"], record_id=res["id"])
    assert v["ok"] and v["integrity"] == "ok" and v["audit_chain_ok"] and v["audit_records"] >= 1
    target = Path(ctx.settings.data_dir) / "restored.db"
    out = ctx.backup.restore(res["path"], target)
    con = sqlite3.connect(target)
    try:
        assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert "PLAINTEXT-MARKER-XYZ" in con.execute("SELECT details FROM audit_log WHERE action='seed.event'").fetchone()[0]
    finally:
        con.close()
    assert out["seconds"] >= 0
    with pytest.raises(BackupError):
        ctx.backup.restore(res["path"], target)  # не перезаписываем молча
    assert ctx.backup.list()[0]["verify_status"] == "ok"


def test_tampered_or_wrong_key_backup_is_rejected(ctx, admin, settings):
    _seed(ctx, admin)
    res = ctx.backup.create()
    p = Path(res["path"])
    data = bytearray(p.read_bytes())
    data[-5] ^= 0xFF
    p.write_bytes(bytes(data))
    v = ctx.backup.verify(p)
    assert not v["ok"] and "повреждена" in v["error"]
    res2 = ctx.backup.create()
    other = Container(settings.model_copy(update={"backup_key": SecretStr("A" * 43)}), clock=ctx.clock, db=ctx.db)
    assert not other.backup.verify(res2["path"])["ok"]


def test_restore_test_reports_rto_and_prune_keeps_last_verified(ctx, admin, settings):
    _seed(ctx, admin)
    ctx.settings.backup_retention = 2
    paths = []
    for _ in range(4):
        ctx.clock.advance(minutes=15)
        paths.append(ctx.backup.create())
    rt = ctx.backup.restore_test(paths[-1]["path"])
    assert rt["ok"] and rt["rto_seconds"] < 60
    assert len(ctx.backup.list()) == 2 and not Path(paths[0]["path"]).exists()


def test_revocations_survive_restore(ctx, admin):
    _seed(ctx, admin)
    with ctx.db.session() as s:
        rec = ctx.secrets.put(s, admin, 1, "tg", "123456789:AAH-fakefakefakefakefakefakefakefake")
        acc = PlatformAccount(project_id=1, platform="telegram", display_name="t", external_id="x", status="connected", secret_id=rec.id)
        s.add(acc)
        sid = rec.id
    res = ctx.backup.create()  # копия содержит рабочий секрет
    with ctx.db.session() as s:
        ctx.secrets.revoke(s, admin, sid, reason="compromised after backup")  # отзыв уже ПОСЛЕ копии
    target = Path(ctx.settings.data_dir) / "restored2.db"
    out = ctx.backup.restore(res["path"], target)
    assert out["revocations_replayed"] == 1
    con = sqlite3.connect(target)
    try:
        assert con.execute("SELECT length(ciphertext), revoked_at IS NOT NULL FROM secrets WHERE id=?", (sid,)).fetchone() == (0, 1)
        assert con.execute("SELECT status FROM platform_accounts").fetchone()[0] == "revoked"
    finally:
        con.close()


def test_health_reports_components_and_degradation(ctx, project, admin):
    r = ctx.health.check()
    assert {"database", "sources", "worker", "publishing", "accounts", "llm", "backup", "disk", "audit_chain", "killswitch", "config"} <= set(r["components"])
    assert r["components"]["database"]["status"] == "ok" and r["components"]["worker"]["status"] == "warn"  # воркер ещё не запускался
    assert "LLM не подключена" in r["components"]["llm"]["summary"]
    # обязательный источник NUR.KZ падает → критично
    with ctx.db.session() as s:
        nur = s.scalar(select(Source).where(Source.key == "nur_kz"))
        nur.consecutive_errors, nur.last_success_at = 5, ctx.clock.now() - timedelta(days=1)
    r = ctx.health.check()
    assert r["components"]["sources"]["status"] == "fail" and r["status"] == "fail" and "NUR.KZ" in r["components"]["sources"]["summary"]
    with ctx.db.read() as s:
        n = s.scalars(select(Notification).where(Notification.kind == "health:sources")).all()
        assert len(n) == 1 and n[0].level == "critical"
    ctx.health.check()  # повторная проверка не плодит оповещения
    with ctx.db.read() as s:
        assert s.query(Notification).filter(Notification.kind == "health:sources").count() == 1
    assert "smi_component_status{component=\"sources\"} 2" in ctx.health.prometheus(r)
    assert ctx.health.history("sources")


def test_health_detects_overdue_and_stuck_publications(published, admin):
    ctx, project, cpk, ids = published
    with ctx.db.session() as s:
        p = s.get(Publication, ids[0])
        p.state, p.scheduled_at, p.next_attempt_at = "scheduled", ctx.clock.now() - timedelta(minutes=30), None
        q = s.get(Publication, ids[1])
        q.state, q.claimed_at = "publishing", ctx.clock.now() - timedelta(minutes=20)
    r = ctx.health.check()["components"]["publishing"]
    assert r["status"] == "fail" and r["detail"]["overdue"] == 1 and r["detail"]["stuck"] == 1


def test_config_lint_flags_unsafe_production_settings(tmpdir_path):
    from smi_agent.security.secrets import generate_key_entry

    st = Settings(env="production", data_dir=tmpdir_path, database_url=f"sqlite:///{tmpdir_path}/p.db", backup_dir=tmpdir_path / "bk", master_keys=SecretStr(generate_key_entry()), public_url="http://example.kz", allow_private_fetch=True, enable_docs=True, require_mfa_for_admin=False)
    c = Container(st)
    c.db.create_all()
    issues = c.health.check()["components"]["config"]["detail"]["issues"]
    assert {"не задан SMI_BACKUP_KEY", "включён allow_private_fetch"} <= set(issues) and any("https" in i for i in issues) and any("/docs" in i for i in issues) and any("MFA" in i for i in issues)
    c.close()
    with pytest.raises(RuntimeError):  # без мастер-ключа production не стартует
        Container(Settings(env="production", data_dir=tmpdir_path / "x", database_url=f"sqlite:///{tmpdir_path}/q.db"))


def test_worker_runs_due_jobs_once_and_respects_intervals(ctx, project):
    w = Worker(ctx, holder="w1")
    first = w.tick()
    assert set(first) == {j.name for j in w.jobs} and set(first.values()) == {"ok"}
    assert w.tick() == {}  # интервалы ещё не наступили
    ctx.clock.advance(seconds=25)
    assert set(w.tick()) == {"publish"}
    with ctx.db.read() as s:
        leases = {j.name: j for j in s.scalars(select(JobLease))}
        assert leases["publish"].runs == 2 and leases["health"].last_ok_at and leases["publish"].lease_until is None
    assert ctx.health.check()["components"]["worker"]["status"] in ("ok", "warn")


def test_worker_lease_prevents_double_execution_and_failures_are_isolated(ctx, project):
    calls = {"a": 0, "b": 0}
    w1, w2 = Worker(ctx, holder="w1"), Worker(ctx, holder="w2")

    def ok():
        calls["a"] += 1

    def boom():
        calls["b"] += 1
        raise RuntimeError("сломалось")

    w1.jobs = [Job("job_a", 10, ok, 300), Job("job_b", 10, boom, 300)]
    w2.jobs = [Job("job_a", 10, ok, 300)]
    # w1 «захватил» job_a и завис (аренда ещё действует)
    with ctx.db.session() as s:
        s.add(JobLease(name="job_a", holder="w1", lease_until=ctx.clock.now() + timedelta(seconds=200), runs=0))
    assert w2.tick() == {}  # чужая аренда действует — не выполняем
    res = w1.tick()
    assert res == {"job_a": "ok", "job_b": "error"} and calls == {"a": 1, "b": 1}  # сбой job_b не помешал job_a
    with ctx.db.read() as s:
        b = s.get(JobLease, "job_b")
        assert b.last_status == "error" and "сломалось" in b.last_error and b.last_ok_at is None


def test_cleanup_and_audit_verify_jobs(ctx, project, admin):
    w = Worker(ctx, holder="w")
    w.j_audit_verify()
    with ctx.db.read() as s:
        rec = s.scalars(select(HealthRecord).where(HealthRecord.component == "audit_chain")).one()
        assert rec.status == "ok"
    w.j_cleanup()
