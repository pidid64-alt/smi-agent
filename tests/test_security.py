import base64
import socket

import httpx
import pytest

from smi_agent.core.errors import Forbidden, SSRFBlocked
from smi_agent.monitoring.http import SafeHttp
from smi_agent.security.passwords import check_password_policy, hash_password, needs_rehash, verify_password
from smi_agent.security.rbac import Actor, Perm, authorize, can
from smi_agent.security.redaction import mask, redact, sanitize_details
from smi_agent.security.secrets import SecretRevoked, SecretStore, generate_key_entry, parse_master_keys
from smi_agent.security.ssrf import EgressGuard, UrlPolicy, ip_is_public
from smi_agent.security.totp import generate_secret, provisioning_uri, totp_now, verify_totp


def test_password_roundtrip_and_policy():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) and not verify_password("wrong", h)
    assert not verify_password("x", "garbage") and not needs_rehash(h)
    assert check_password_policy("short", production=True)
    assert not check_password_policy("a-long-enough-pass", production=True)
    assert check_password_policy("password", production=False)


def test_totp_rfc6238_vector_and_replay_protection():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_now(secret, at=59) == "287082"  # RFC 6238, T=59, SHA1 (6 цифр)
    step = verify_totp(secret, "287082", at=59)
    assert step == 1
    assert verify_totp(secret, "287082", at=59, last_step=step) is None  # повтор кода отклоняется
    assert verify_totp(secret, "000000", at=59) is None
    assert provisioning_uri(generate_secret(), "a@b.kz").startswith("otpauth://totp/")


def test_secret_store_encrypts_binds_aad_rotates_and_revokes(ctx, admin):
    from smi_agent.db.models import Project, SecretRecord

    with ctx.db.session() as s:
        s.add(Project(slug="p", name="P", settings={}))
    with ctx.db.session() as s:
        a = ctx.secrets.put(s, admin, 1, "acc:1", "token-A")
        b = ctx.secrets.put(s, admin, 1, "acc:2", "token-B")
        ids = (a.id, b.id)
        assert b"token-A" not in a.ciphertext
    with ctx.db.session() as s:
        assert ctx.secrets.get(s, admin, ids[0]) == "token-A"
        # подмена шифртекста между записями ломает проверку AAD
        ra, rb = s.get(SecretRecord, ids[0]), s.get(SecretRecord, ids[1])
        ra.ciphertext = rb.ciphertext
        with pytest.raises(Exception):
            ctx.secrets.get(s, admin, ids[0])
        s.rollback()
    with ctx.db.session() as s:
        ctx.secrets.revoke(s, admin, ids[1], reason="leak")
    with ctx.db.session() as s:
        with pytest.raises(SecretRevoked):
            ctx.secrets.get(s, admin, ids[1])
        assert ctx.secrets.get(s, admin, ids[0]) == "token-A"


def test_master_key_rotation_reencrypts(ctx, admin, settings):
    from pydantic import SecretStr

    from smi_agent.audit import AuditService
    from smi_agent.db.models import Project

    with ctx.db.session() as s:
        s.add(Project(slug="p", name="P", settings={}))
    with ctx.db.session() as s:
        rec = ctx.secrets.put(s, admin, 1, "acc", "tok")
        rid = rec.id
        old_kid = rec.key_id
    new_entry = generate_key_entry("k2")
    old_entry = f"{old_kid}:{base64.urlsafe_b64encode(ctx.secrets.keys[old_kid]).decode()}"
    settings2 = settings.model_copy(update={"master_keys": SecretStr(f"{new_entry},{old_entry}")})
    store2 = SecretStore(settings2, AuditService(ctx.clock), ctx.clock)
    with ctx.db.session() as s:
        assert store2.rotate_master_key(s, admin) == 1
    with ctx.db.session() as s:
        assert store2.get(s, admin, rid) == "tok"
        assert s.get(type(rec), rid).key_id == "k2"
    assert len(parse_master_keys(f"{new_entry},{old_entry}")) == 2


def test_redaction_masks_tokens_and_keys():
    t = "https://api.telegram.org/bot123456789:AAH-fakefakefakefakefakefakefakefake/sendMessage?access_token=abc123def456&x=1 Bearer abcdefghijklmnop"
    r = redact(t)
    assert "AAH-fake" not in r and "abc123def456" not in r and "abcdefghijklmnop" not in r
    assert sanitize_details({"Authorization": "Bearer x", "ok": 5, "list": [{"client_secret": "zzz"}]}) == {"Authorization": "[redacted]", "ok": 5, "list": [{"client_secret": "[redacted]"}]}
    assert mask("abcdefghijkl") == "abcd…ijkl"


@pytest.mark.parametrize("ip,expected", [("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False), ("100.64.0.1", False), ("::1", False), ("::ffff:127.0.0.1", False), ("2001:4860:4860::8888", True), ("0.0.0.0", False)])
def test_ip_is_public(ip, expected):
    assert ip_is_public(ip) is expected


@pytest.mark.parametrize("url", ["http://localhost/", "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest", "ftp://example.com/", "http://user:pw@example.com/", "http://example.com:8080/", "http://2130706433/", "http://0x7f000001/", "http://evil.internal/"])
def test_ssrf_blocks_dangerous_urls(url):
    guard = EgressGuard(UrlPolicy(), resolver=lambda h, p: {"example.com": ["93.184.216.34"]}.get(h, [socket.gethostbyname(h)] if h[0].isdigit() else ["10.0.0.1"]))
    with pytest.raises(SSRFBlocked):
        guard.check_url(url)


def test_safe_http_redirect_to_private_ip_and_size_limit(settings):
    resolver = lambda h, p: {"example.com": ["93.184.216.34"], "evil.test": ["127.0.0.1"]}[h]  # noqa: E731

    def handler(req: httpx.Request):
        if req.url.path == "/redir":
            return httpx.Response(302, headers={"location": "http://evil.test/x"})
        if req.url.path == "/big":
            return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})
        if req.url.path == "/bin":
            return httpx.Response(200, content=b"\x00", headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, content=b"<rss/>", headers={"content-type": "application/rss+xml"})

    h = SafeHttp(settings, transport=httpx.MockTransport(handler), resolver=resolver)
    assert h.get("https://example.com/feed").status == 200
    with pytest.raises(SSRFBlocked):
        h.get("https://example.com/redir")
    from smi_agent.core.errors import FetchError

    with pytest.raises(FetchError):
        h.get("https://example.com/big", max_bytes=1000)
    with pytest.raises(FetchError):
        h.get("https://example.com/bin")


def test_guarded_backend_checks_addresses_at_connect_time():
    import httpcore

    from smi_agent.security.ssrf import GuardedBackend

    gb = GuardedBackend(httpcore.SyncBackend(), EgressGuard(UrlPolicy(), lambda h, p: ["127.0.0.1"]))
    with pytest.raises(SSRFBlocked):
        gb.connect_tcp("rebind.example", 80)  # DNS-rebinding: имя «внезапно» указывает на loopback


def test_rbac_matrix():
    user, admin, auditor, ai = Actor.user(1, "user"), Actor.user(2, "admin"), Actor.user(3, "auditor"), Actor.ai()
    assert can(user, Perm.PUBLISH_APPROVE) and not can(user, Perm.KILL_RELEASE) and not can(user, Perm.USERS_MANAGE)
    assert can(admin, Perm.KILL_RELEASE) and can(admin, Perm.AUTOPILOT_MANAGE)
    assert can(auditor, Perm.AUDIT_VIEW) and not can(auditor, Perm.PUBLISH_APPROVE) and not can(auditor, Perm.PROPOSALS_ACT)
    assert can(ai, Perm.PUBLISH_AUTO) and not can(ai, Perm.PUBLISH_APPROVE) and not can(ai, Perm.KILL_RELEASE)
    with pytest.raises(Forbidden):
        authorize(ai, Perm.KILL_RELEASE)
    assert not can(Actor("user", "9", "nonsense"), Perm.AGENDA_VIEW)
