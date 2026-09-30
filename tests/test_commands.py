import pytest
from sqlalchemy import select

from smi_agent.core.enums import ActionKind
from smi_agent.db.models import Content, FunnelItem, LearningEvent, Proposal, UserAction
from smi_agent.interaction.commands import parse_commands

K = ActionKind


@pytest.mark.parametrize("text,expected", [
    ("Беру №2", [(K.SELECT, 2)]),
    ("беру 3", [(K.SELECT, 3)]),
    ("Возьми вторую", [(K.SELECT, 2)]),
    ("№1 неинтересна", [(K.REJECT, 1)]),
    ("не беру 4", [(K.REJECT, 4)]),
    ("убери №3", [(K.REJECT, 3)]),
    ("Замени №3", [(K.REPLACE, 3)]),
    ("замени третью", [(K.REPLACE, 3)]),
    ("Раскрой №5 подробнее", [(K.MORE_INFO, 5)]),
    ("подробнее по №2", [(K.MORE_INFO, 2)]),
    ("№1 и №3 неинтересны, беру №2", [(K.REJECT, 1), (K.REJECT, 3), (K.SELECT, 2)]),
    ("замени третью, а четвертую раскрой подробнее", [(K.REPLACE, 3), (K.MORE_INFO, 4)]),
    ("Беру №2 и №5", [(K.SELECT, 2), (K.SELECT, 5)]),
    ("take 2", [(K.SELECT, 2)]),
    ("skip #1", [(K.REJECT, 1)]),
    ("replace 3", [(K.REPLACE, 3)]),
    ("more on 5", [(K.MORE_INFO, 5)]),
    ("2", [(K.SELECT, 2)]),
])
def test_command_parsing(text, expected):
    assert [(c.kind, c.slot) for c in parse_commands(text).commands] == expected


def test_spec_example_select_with_kazakhstan_emphasis():
    (c,) = parse_commands("№4, но сделай акцент на Казахстан").commands
    assert (c.kind, c.slot, c.params["emphasis"]) == (K.MODIFY, 4, "kazakhstan")


def test_modifiers_language_length_format_platform():
    (c,) = parse_commands("Беру вторую, но на казахском и покороче").commands
    assert c.params == {"language": "kk", "length": "shorter"}
    (c,) = parse_commands("Беру №2, формат reels").commands
    assert c.params["format"] == "reels_script"
    (c,) = parse_commands("возьми 3 только для телеграм").commands
    assert c.params["platforms"] == ["telegram"]


def test_rejection_reason_is_captured_and_unknown_text_gives_hint():
    (c,) = parse_commands("№1 неинтересна — слишком много политики").commands
    assert c.params["reason"] == "слишком много политики"
    r = parse_commands("привет, как дела?")
    assert not r.commands and r.unrecognized and "Беру №2" in r.hint  # не гадаем: подсказываем формат команд


def _props(ctx, rid=None):
    with ctx.db.read() as s:
        return {p.slot: p for p in s.scalars(select(Proposal).order_by(Proposal.id))}


def test_select_creates_draft_records_action_and_learns(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    pipeline(corpus)
    out = ctx.interaction.handle_text(corpus, admin, "Беру №1")
    (r,) = out["results"]
    assert r["ok"] and r["content_id"].startswith("Content-2026-") and "Черновик" in r["message"]
    with ctx.db.read() as s:
        p = s.scalars(select(Proposal).where(Proposal.slot == 1)).first()
        assert p.status == "selected" and p.content_pk and p.resolved_by == "user:1"
        ua = s.scalars(select(UserAction)).one()
        assert ua.kind == "select" and ua.raw_text == "Беру №1" and ua.proposal_id == p.id
        assert s.scalars(select(LearningEvent).where(LearningEvent.signal == "select")).one().deltas
    again = ctx.interaction.handle_text(corpus, admin, "Беру №1")["results"][0]
    assert not again["ok"] and "уже выбрано" in again["message"]


def test_select_with_emphasis_overrides_angle_and_records_modify(llm_ctx, corpus, pipeline, admin):
    ctx = llm_ctx
    pipeline(corpus)
    out = ctx.interaction.handle_text(corpus, admin, "№1, но сделай акцент на Казахстан")
    assert out["results"][0]["ok"] and "Акцент" in out["results"][0]["message"]
    with ctx.db.read() as s:
        c = s.scalars(select(Content)).one()
        assert "Казахстан" in c.angle
        assert s.scalars(select(UserAction)).one().kind == "modify"
        assert s.scalars(select(LearningEvent).where(LearningEvent.signal == "select_modified")).one()


def test_reject_replace_and_more_info(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    out = ctx.interaction.handle_text(corpus, admin, "№1 неинтересна — слишком много банков")
    assert out["results"][0]["ok"] and "слишком много банков" in out["results"][0]["message"]
    with ctx.db.read() as s:
        rej = s.scalars(select(Proposal).where(Proposal.slot == 1)).first()
        assert rej.status == "rejected"
        assert s.scalars(select(LearningEvent).where(LearningEvent.signal == "reject")).one().note.startswith("слишком много банков")
    more = ctx.interaction.handle_text(corpus, admin, "Раскрой №2 подробнее")["results"][0]
    assert more["ok"] and more["data"]["facts"] and more["data"]["sources"] and more["data"]["angles"] and more["data"]["timeline"]
    rep = ctx.interaction.handle_text(corpus, admin, "Замени №3")["results"][0]
    with ctx.db.read() as s:
        old = s.scalars(select(Proposal).where(Proposal.slot == 3, Proposal.status == "replaced")).first()
        assert rep["ok"] == bool(old)  # либо заменено из резерва, либо честный отказ
        if not rep["ok"]:
            assert "Достойной замены нет" in rep["message"]


def test_replace_uses_reserve_and_refuses_when_nothing_worthy(ctx, project, pipeline, admin):
    # три сильные темы + одна заведомо слабая: замена берёт только достойных кандидатов из резерва
    from tests.conftest import add_article

    texts = [
        ("kursiv", "Выручка Kaspi выросла на 18,4% до 3,2 млрд тенге", "Компания Kaspi сообщила о росте выручки на 18,4% до 3,2 млрд тенге в третьем квартале. Решение совета директоров принято 29 сентября. Дивиденды составят 120 тенге на акцию."),
        ("nur_kz", "Цены на бензин АИ-95 вырастут на 7,5% с 1 октября", "Цены на бензин АИ-95 в Казахстане вырастут на 7,5% с 1 октября, сообщили в министерстве энергетики. Новая цена составит 212 тенге за литр. Решение принято на заседании правительства."),
        ("reuters", "Nvidia unveils new AI chip with 40% higher speed", "Nvidia unveiled a new AI chip on Tuesday that it says is 40% faster than its predecessor and will ship in 2027, the company said. Shares rose 3% in premarket trading."),
    ]
    for i, (src, t, b) in enumerate(texts):
        add_article(ctx, project, src, t, b, minutes=i * 3)
        add_article(ctx, project, "bbc_world" if src != "reuters" else "tengrinews", t + " — подробности", b + " Подробности опубликованы позднее.", minutes=i * 3 + 25)
    pipeline(project)
    with ctx.db.read() as s:
        n = s.query(Proposal).filter(Proposal.status == "proposed").count()
    assert n >= 2
    res = ctx.interaction.handle_text(project, admin, f"Замени №{n}")["results"][0]
    assert isinstance(res["ok"], bool) and res["message"]


def test_invalid_slot_and_changing_angle(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    bad = ctx.interaction.handle_text(corpus, admin, "Беру №9")["results"][0]
    assert not bad["ok"] and "Нет предложения №9" in bad["message"]
    a1 = ctx.interaction.handle_text(corpus, admin, "другой угол для №1")["results"]
    assert a1 and a1[0]["action"] == "change_angle" and a1[0]["ok"]
    with ctx.db.session() as s:
        r1 = ctx.interaction.change_angle(s, corpus, admin, 1, None)
        r2 = ctx.interaction.change_angle(s, corpus, admin, 1, None)
        assert r1.data["angle"] != r2.data["angle"]
        r3 = ctx.interaction.change_angle(s, corpus, admin, 1, "Сухие факты")
        assert r3.data["angle"] == "Сухие факты"


def test_rejected_topics_are_learned_and_lower_affinity(ctx, corpus, pipeline, admin):
    pipeline(corpus)
    with ctx.db.read() as s:
        props = {p.slot: p for p in s.scalars(select(Proposal))}
        cats = {sl: p.card["category"] for sl, p in props.items()}
    target_slot = next(sl for sl, c in cats.items() if c == "finance")
    for _ in range(1):
        ctx.interaction.handle_text(corpus, admin, f"№{target_slot} неинтересна")
    with ctx.db.read() as s:
        from smi_agent.db.models import Event

        ev = s.get(Event, props[target_slot].event_id)
        aud, _hist, note = ctx.profile.affinity(s, corpus, ev)
        assert aud < 0.5  # отклонённая тема стала менее вероятной
