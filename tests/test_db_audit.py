import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from smi_agent.db.models import AuditLog, Project
from smi_agent.security.rbac import Actor


def test_create_all_is_idempotent(ctx):
    ctx.db.create_all()
    ctx.db.create_all()
    with ctx.db.read() as s:
        assert s.query(Project).count() == 0


def test_write_sessions_are_serialized(ctx):
    order = []

    def w1():
        with ctx.db.session() as s:
            s.add(Project(slug="a", name="A", settings={}))
            s.flush()
            order.append("w1-locked")
            time.sleep(0.6)
            order.append("w1-commit")

    def w2():
        time.sleep(0.15)
        with ctx.db.session() as s:
            order.append("w2-start")
            s.add(Project(slug="b", name="B", settings={}))
            s.flush()
            order.append("w2-write")

    t1, t2 = threading.Thread(target=w1), threading.Thread(target=w2)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert order.index("w1-commit") < order.index("w2-write")


def test_audit_log_is_append_only(ctx):
    with ctx.db.session() as s:
        ctx.audit.log(s, Actor.system(), "test.event")
    with pytest.raises(IntegrityError):
        with ctx.db.session() as s:
            s.execute(text("UPDATE audit_log SET action='x'"))
    with pytest.raises(IntegrityError):
        with ctx.db.session() as s:
            s.execute(text("DELETE FROM audit_log"))


def test_audit_chain_verifies_and_detects_tampering(ctx):
    with ctx.db.session() as s:
        for i in range(5):
            ctx.audit.log(s, Actor.user(1, "admin"), f"test.{i}", details={"i": i})
    with ctx.db.read() as s:
        res = ctx.audit.verify_chain(s)
        assert res.ok and res.checked == 5
    # злоумышленник с доступом к БД снимает триггер и правит запись — цепочка это обнаруживает
    with ctx.db.session() as s:
        s.execute(text("DROP TRIGGER audit_log_no_update"))
        s.execute(text("UPDATE audit_log SET action='forged' WHERE id=3"))
    with ctx.db.read() as s:
        res = ctx.audit.verify_chain(s)
        assert not res.ok and res.first_bad_id == 3


def test_audit_detects_deleted_tail(ctx):
    with ctx.db.session() as s:
        for i in range(3):
            ctx.audit.log(s, Actor.system(), f"t.{i}")
    with ctx.db.session() as s:
        s.execute(text("DROP TRIGGER audit_log_no_delete"))
        s.execute(text("DELETE FROM audit_log WHERE id=3"))
    with ctx.db.read() as s:
        assert not ctx.audit.verify_chain(s).ok


def test_secrets_never_reach_the_audit_log(ctx):
    token = "123456789:AAH-fakefakefakefakefakefakefakefake"
    with ctx.db.session() as s:
        ctx.audit.log(s, Actor.system(), "x", details={"token": token, "note": f"url https://api.telegram.org/bot{token}/send", "password": "hunter2", "nested": {"api_key": "sk-abcdefghijklmnopqrstuv"}})
    with ctx.db.read() as s:
        row = s.query(AuditLog).one()
        blob = str(row.details)
        assert token not in blob and "hunter2" not in blob and "sk-abcdef" not in blob
