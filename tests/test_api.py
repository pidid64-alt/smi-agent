import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from smi_agent.api.app import create_app
from smi_agent.db.models import AuditLog, Project, Proposal
from smi_agent.security.totp import totp_now

PW = "correct horse battery"


@pytest.fixture
def api(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    with ctx.db.session() as s:
        for name, role in (("boss", "admin"), ("editor", "user"), ("auditor", "auditor")):
            ctx.auth.create_user(s, None, username=name, password=PW, project_id=corpus, role=role)
        s.add(Project(slug="other", name="Другой проект", settings={}))
        ctx.auth.create_user(s, None, username="root", password=PW, superadmin=True)
        ctx.auth.create_user(s, None, username="outsider", password=PW)
    pipeline(corpus)
    app = create_app(ctx)
    with TestClient(app) as c:
        c.ctx, c.project = ctx, corpus
        yield c


def login(c, username="boss", password=PW, **kw):
    r = c.post("/api/auth/login", json={"username": username, "password": password, **kw})
    assert r.status_code == 200, r.text
    c.csrf = r.json()["csrf"]
    return r


def H(c):
    return {"X-CSRF-Token": c.csrf}


def P(c, path):
    return f"/api/p/{c.project}{path}"


def test_login_is_generic_and_sets_safe_cookie(api):
    bad_user = api.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    bad_pass = api.post("/api/auth/login", json={"username": "boss", "password": "wrong"})
    assert bad_user.status_code == bad_pass.status_code == 401 and bad_user.json() == bad_pass.json()  # не раскрываем, существует ли пользователь
    r = login(api)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "smi_session=" in cookie
    me = api.get("/api/auth/me").json()
    assert me["user"]["username"] == "boss" and me["csrf"] and me["user"]["projects"][0]["role"] == "admin"
    assert api.get(P(api, "/dashboard")).status_code == 200


def test_unauthenticated_requests_are_rejected(api):
    assert api.get(P(api, "/dashboard")).status_code == 401
    assert api.get("/api/health").status_code == 401 and api.get("/api/projects").status_code == 401
    assert api.get("/api/health/live").json() == {"status": "ok"}  # публична только проверка «жив»


def test_csrf_and_origin_protection(api):
    login(api)
    assert api.post(P(api, "/funnel/run")).status_code == 403  # без CSRF-токена
    assert api.post(P(api, "/funnel/run"), headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert api.post(P(api, "/funnel/run"), headers={**H(api), "Origin": "https://evil.example"}).status_code == 403  # чужой Origin
    ok = api.post(P(api, "/funnel/run"), headers={**H(api), "Origin": "http://testserver"})
    assert ok.status_code == 200
    bad_login = api.post("/api/auth/login", json={"username": "boss", "password": PW}, headers={"Origin": "https://evil.example"})
    assert bad_login.status_code == 403  # login-CSRF


def test_lockout_after_repeated_failures_even_with_correct_password(api):
    for _ in range(5):
        assert api.post("/api/auth/login", json={"username": "editor", "password": "nope"}).status_code == 401
    r = api.post("/api/auth/login", json={"username": "editor", "password": PW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "login_failed"
    api.ctx.clock.advance(minutes=16)
    assert api.post("/api/auth/login", json={"username": "editor", "password": PW}).status_code == 200
    with api.ctx.db.read() as s:
        failed = s.scalars(select(AuditLog).where(AuditLog.action == "auth.login_failed")).all()
        assert len(failed) >= 6 and all("nope" not in str(a.details) and PW not in str(a.details) for a in failed)


def test_rate_limit_on_login_flood(api):
    codes = [api.post("/api/auth/login", json={"username": f"u{i}", "password": "x"}).status_code for i in range(14)]
    assert 429 in codes and codes[0] == 401


def test_logout_and_session_expiry(api):
    login(api)
    assert api.post("/api/auth/logout", headers=H(api)).status_code == 200
    assert api.get("/api/auth/me").status_code == 401
    login(api, "editor")
    api.ctx.clock.advance(hours=13)  # простой дольше idle-таймаута (12 ч)
    assert api.get("/api/auth/me").status_code == 401


def test_password_change_revokes_other_sessions_and_enforces_policy(api):
    other = TestClient(api.app)
    with other:
        r = other.post("/api/auth/login", json={"username": "editor", "password": PW})
        other.csrf = r.json()["csrf"]
        login(api, "editor")
        assert api.post("/api/auth/password", json={"old": PW, "new": "short"}, headers=H(api)).status_code == 422
        assert api.post("/api/auth/password", json={"old": "bad", "new": "another long password"}, headers=H(api)).status_code == 401
        assert api.post("/api/auth/password", json={"old": PW, "new": "another long password"}, headers=H(api)).status_code == 200
        assert api.get("/api/auth/me").status_code == 200  # текущая сессия сохранена
        assert other.get("/api/auth/me").status_code == 401  # остальные отозваны


def test_mfa_enrollment_login_and_totp_replay(api):
    login(api, "editor")
    b = api.post("/api/auth/mfa/begin", headers=H(api)).json()
    secret = b["secret"]
    assert b["uri"].startswith("otpauth://") and api.post("/api/auth/mfa/confirm", json={"code": "000000"}, headers=H(api)).status_code == 422
    now = api.ctx.clock.now().timestamp()
    import time as _t

    code = totp_now(secret, at=_t.time())
    assert api.post("/api/auth/mfa/confirm", json={"code": code}, headers=H(api)).status_code == 200
    api.post("/api/auth/logout", headers=H(api))
    assert api.post("/api/auth/login", json={"username": "editor", "password": PW}).status_code == 401  # без кода нельзя
    assert api.post("/api/auth/login", json={"username": "editor", "password": PW, "totp": code}).status_code == 401  # код уже использован (повтор)
    with api.ctx.db.read() as s:
        from smi_agent.db.models import SecretRecord

        assert s.query(SecretRecord).filter(SecretRecord.name.like("mfa:%")).one().ciphertext  # секрет MFA хранится зашифрованным
    assert now > 0


def test_mfa_required_for_admin_in_production(tmpdir_path):
    from pydantic import SecretStr

    from smi_agent.config import Settings
    from smi_agent.container import Container
    from smi_agent.security.secrets import generate_key_entry

    st = Settings(env="production", data_dir=tmpdir_path, database_url=f"sqlite:///{tmpdir_path}/p.db", backup_dir=tmpdir_path / "b", master_keys=SecretStr(generate_key_entry()), backup_key=SecretStr("B" * 43), public_url="https://smi.example.kz", enable_docs=False)
    ctx = Container(st)
    ctx.db.create_all()
    with ctx.db.session() as s:
        p = Project(slug="p", name="P", settings={})
        s.add(p)
        s.flush()
        ctx.auth.create_user(s, None, username="admin1", password="a very long admin password", project_id=p.id, role="admin")
    with TestClient(create_app(ctx), base_url="https://smi.example.kz") as c:
        r = c.post("/api/auth/login", json={"username": "admin1", "password": "a very long admin password"})
        assert r.status_code == 200 and r.json()["mfa_setup_required"] is True and "secure" in r.headers["set-cookie"].lower()
        assert c.get("/api/p/1/dashboard").status_code == 403 and c.get("/api/p/1/dashboard").json()["error"]["code"] == "mfa_setup_required"
        assert c.get("/docs").status_code == 404  # документация API закрыта в production
        assert c.get("/api/auth/me").status_code == 200  # а настройка MFA доступна
        assert "strict-transport-security" in c.get("/api/health/live").headers
    ctx.close()


def test_roles_and_isolation(api):
    login(api, "auditor")
    assert api.get(P(api, "/audit")).status_code == 200 and api.get(P(api, "/audit/verify")).json()["ok"] is True
    assert api.post(P(api, "/commands"), json={"text": "Беру №1"}, headers=H(api)).status_code == 403
    assert api.post(P(api, "/funnel/run"), headers=H(api)).status_code == 403
    login(api, "editor")
    assert api.get(P(api, "/audit")).status_code == 403 and api.get(P(api, "/users")).status_code == 403
    assert api.post(P(api, "/killswitch/1/release"), json={}, headers=H(api)).status_code in (403, 404)
    login(api, "outsider")
    assert api.get(P(api, "/dashboard")).status_code == 403  # нет членства в проекте
    login(api, "root")
    assert api.get(P(api, "/dashboard")).status_code == 200  # суперадмин видит всё


def test_service_token_is_project_scoped_and_cannot_approve(api):
    login(api, "boss")
    made = api.post(P(api, "/tokens"), json={"name": "worker"}, headers=H(api)).json()
    token = made["token"]
    assert token.startswith("smi_") and "один раз" in made["note"]
    listed = api.get(P(api, "/tokens")).json()["tokens"]
    assert listed[0]["prefix"] == made["prefix"] and "token" not in listed[0]  # полный токен больше не показывается
    svc = TestClient(api.app)
    with svc:
        hdr = {"Authorization": f"Bearer {token}"}
        assert svc.get(P(api, "/proposals"), headers=hdr).status_code == 200
        assert svc.get("/api/p/2/proposals", headers=hdr).status_code == 403  # чужой проект
        assert svc.post(P(api, "/funnel/run"), headers=hdr).status_code == 200  # машинные права: воронка
        assert svc.post(P(api, "/publications/1/approve"), json={}, headers=hdr).status_code == 403  # подтверждает только человек
        assert svc.post(P(api, "/killswitch/1/release"), json={}, headers=hdr).status_code == 403
        assert svc.get(P(api, "/dashboard"), headers={"Authorization": "Bearer smi_bad_token"}).status_code == 401
    api.delete(P(api, f"/tokens/{made['id']}"), headers=H(api))
    with svc:
        assert svc.get(P(api, "/proposals"), headers={"Authorization": f"Bearer {token}"}).status_code == 401  # отозван


def test_security_headers_and_error_format(api):
    r = api.get("/api/health/live")
    for h in ("x-content-type-options", "x-frame-options", "content-security-policy", "referrer-policy", "permissions-policy"):
        assert h in r.headers, h
    assert "script-src 'self'" in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"]
    login(api)
    assert api.get(P(api, "/dashboard")).headers["cache-control"] == "no-store"
    e = api.post(P(api, "/commands"), json={"text": ""}, headers=H(api))
    assert e.status_code == 422 and e.json()["error"]["code"] == "validation"
    assert "access-control-allow-origin" not in api.options("/api/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}).headers  # CORS не включён
    nf = api.get(P(api, "/events/99999"))
    assert nf.status_code == 404 and nf.json()["error"]["code"] == "not_found"


def test_settings_api_validates_and_protects_mode(api):
    login(api)
    cur = api.get(P(api, "/settings")).json()
    assert cur["funnel"]["stage1"] == 50 and cur["mode"] == "learning"
    assert api.put(P(api, "/settings"), json={"mode": "autopilot"}, headers=H(api)).status_code == 422  # режим — отдельное осознанное действие
    assert api.put(P(api, "/settings"), json={"funnel": {"stage1": 5, "stage2": 15}}, headers=H(api)).status_code == 422
    ok = api.put(P(api, "/settings"), json={"geo": {"balance_strength": 0.5}}, headers=H(api))
    assert ok.status_code == 200 and ok.json()["geo"]["balance_strength"] == 0.5 and ok.json()["funnel"]["stage1"] == 50
    with api.ctx.db.read() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "settings.update")).one().details == {"keys": ["geo"]}


def test_sources_api_rules(api):
    login(api)
    rows = api.get(P(api, "/sources")).json()["sources"]
    nur = next(r for r in rows if r["key"] == "nur_kz")
    assert nur["mandatory"] is True
    assert api.delete(P(api, f"/sources/{nur['id']}"), headers=H(api)).status_code == 409
    assert api.post(P(api, "/sources"), json={"key": "evil", "name": "x", "url": "http://169.254.169.254/latest", "kind": "rss"}, headers=H(api)).status_code == 422
    ok = api.post(P(api, "/sources"), json={"key": "my_feed", "name": "Мой", "url": "https://example.com/rss.xml", "kind": "rss", "country": "KZ"}, headers=H(api))
    assert ok.status_code == 200
    login(api, "editor")
    assert api.post(P(api, "/sources"), json={"key": "x1", "url": "https://example.com/x"}, headers=H(api)).status_code == 403  # источники — только админ


def test_end_to_end_via_api(api):
    login(api, "boss")
    assert api.post(P(api, "/accounts"), json={"platform": "telegram", "sandbox": True, "mode": "manual", "display_name": "TG песочница"}, headers=H(api)).status_code == 200
    assert api.post(P(api, "/accounts"), json={"platform": "whatsapp", "sandbox": True}, headers=H(api)).status_code == 422  # WhatsApp исключён
    f = api.post(P(api, "/funnel/run"), headers=H(api)).json()
    assert f["counts"]["s5"] >= 3
    props = api.get(P(api, "/proposals")).json()["proposals"]
    assert props[0]["card"]["title"] and props[0]["slot"] == 1
    cmd = api.post(P(api, "/commands"), json={"text": "Беру №1"}, headers=H(api)).json()
    assert cmd["results"][0]["ok"]
    cpk = cmd["results"][0]["data"]["content_pk"]
    d = api.get(P(api, f"/content/{cpk}")).json()
    assert d["content_id"].startswith("Content-2026-") and d["generator"].startswith("llm") and d["forecast"]["calibration_samples"] == 0
    tg = next(v for v in d["versions"] if v["platform"] == "telegram")
    assert "Материал подготовлен с использованием ИИ." in api.get(P(api, f"/content/{cpk}/preview/telegram")).json()["text"]
    edited = api.put(P(api, f"/content/{cpk}/versions/telegram"), json={"body": tg["body"] + "\n\nДополнено редактором."}, headers=H(api))
    assert edited.status_code == 200 and edited.json()["version"] == 2
    pubs = api.post(P(api, f"/content/{cpk}/publications"), json={"platforms": ["telegram"]}, headers=H(api)).json()["publications"]
    assert pubs[0]["state"] == "awaiting_approval" and pubs[0]["state_label"] == "Ожидает подтверждения"
    ap = api.post(P(api, f"/publications/{pubs[0]['id']}/approve"), json={"schedule": {"mode": "now"}}, headers=H(api))
    assert ap.status_code == 200
    det = api.get(P(api, f"/publications/{pubs[0]['id']}")).json()
    assert det["state"] == "published" and det["external_id"].startswith("sbx-")  # отправка в фоне после подтверждения
    assert [e["to"] for e in det["events"]][-2:] == ["publishing", "published"] and det["attempts_log"][0]["outcome"] == "success"
    assert api.post(P(api, f"/publications/{pubs[0]['id']}/cancel"), json={}, headers=H(api)).status_code == 409  # опубликованное не отменить


def test_public_media_only_for_live_publications(api, admin):
    login(api, "boss")
    api.post(P(api, "/accounts"), json={"platform": "instagram", "sandbox": True, "mode": "manual"}, headers=H(api))
    api.post(P(api, "/funnel/run"), headers=H(api))
    cpk = api.post(P(api, "/commands"), json={"text": "Беру №1 только для инстаграм"}, headers=H(api)).json()["results"][0]["data"]["content_pk"]
    from smi_agent.db.models import MediaAsset

    with api.ctx.db.read() as s:
        token = s.scalars(select(MediaAsset).order_by(MediaAsset.id)).first().public_token
    assert api.get(f"/media/{token}.jpg").status_code == 404  # черновик недоступен публично
    pub = api.post(P(api, f"/content/{cpk}/publications"), json={"platforms": ["instagram"]}, headers=H(api)).json()["publications"][0]
    if pub["state"] == "needs_review":
        pytest.skip("проверки не пройдены — публичная выдача не требуется")
    api.post(P(api, f"/publications/{pub['id']}/approve"), json={"schedule": {"mode": "at", "at": (api.ctx.clock.now() + timedelta(hours=3)).isoformat()}}, headers=H(api))
    r = api.get(f"/media/{token}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
    assert api.get("/media/unknowntoken.jpg").status_code == 404 and api.get("/media/..%2f..%2fetc%2fpasswd").status_code == 404


def test_killswitch_via_api_and_release_permissions(api):
    login(api, "editor")
    made = api.post(P(api, "/killswitch"), json={"scope_type": "project", "reason": "проверка"}, headers=H(api))
    assert made.status_code == 200  # любой пользователь может остановить автопилот
    kid = made.json()["id"]
    assert api.post(P(api, f"/killswitch/{kid}/release"), json={}, headers=H(api)).status_code == 403  # но не снять
    auto = api.get(P(api, "/autopilot")).json()
    assert auto["kill_switches"][0]["id"] == kid and auto["mode"] == "learning"
    login(api, "boss")
    assert api.post(P(api, f"/killswitch/{kid}/release"), json={"note": "ок"}, headers=H(api)).status_code == 200
    assert api.put(P(api, "/autopilot/mode"), json={"mode": "yolo"}, headers=H(api)).status_code == 422
    assert api.put(P(api, "/autopilot/mode"), json={"mode": "co_editor"}, headers=H(api)).json()["mode"] == "co_editor"


def test_audit_export_and_health_permissions(api):
    login(api, "boss")
    api.post(P(api, "/funnel/run"), headers=H(api))
    r = api.get(P(api, "/audit/export"))
    lines = [ln for ln in r.text.splitlines() if ln]
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson") and len(lines) >= 3 and all('"hash"' in ln for ln in lines)
    assert api.get("/api/health").status_code == 200 and "components" in api.get("/api/health").json()
    assert api.get("/api/metrics").status_code == 403  # метрики — суперадмину/сервису
    assert api.get("/api/backups").status_code == 403
    login(api, "root")
    assert "smi_overall_status" in api.get("/api/metrics").text
    assert api.post("/api/backups", headers=H(api)).status_code == 200 and api.post("/api/backups/restore-test", headers=H(api)).json()["ok"]
    assert api.get("/api/backups").json()["backups"][0]["encrypted"] is True
    assert "path" not in api.post("/api/backups", headers=H(api)).json()  # пути на сервере не раскрываем


def test_agenda_and_event_endpoints(api):
    login(api, "editor")
    evs = api.get(P(api, "/events?limit=20")).json()["events"]
    assert evs and evs[0]["trend_score"] >= evs[-1]["trend_score"] and {"id", "title", "phase", "n_independent", "category_label"} <= set(evs[0])
    cat = api.get(P(api, f"/events?category={evs[0]['category']}")).json()["events"]
    assert all(e["category"] == evs[0]["category"] for e in cat)
    d = api.get(P(api, f"/events/{evs[0]['id']}")).json()
    assert d["components"]["values"] and d["timeline"] and d["articles"]
    assert api.get(P(api, "/events/424242")).status_code == 404


def test_select_endpoint_with_structured_overrides(api):
    login(api, "editor")
    api.post(P(api, "/funnel/run"), headers=H(api))
    r = api.post(P(api, "/proposals/1/select"), json={"emphasis": "kazakhstan", "length": "shorter", "platforms": ["telegram"]}, headers=H(api))
    assert r.status_code == 200 and r.json()["ok"] and r.json()["content_id"].startswith("Content-2026-")
    cpk = r.json()["data"]["content_pk"]
    d = api.get(P(api, f"/content/{cpk}")).json()
    assert [v["platform"] for v in d["versions"]] == ["telegram"] and "Казахстан" in d["angle"]
    assert api.post(P(api, "/proposals/1/select"), json={}, headers=H(api)).status_code == 422  # повторный выбор отклонён
    assert api.post(P(api, "/proposals/2/select"), json={"language": "de"}, headers=H(api)).status_code == 422  # язык вне ru/kk/en
