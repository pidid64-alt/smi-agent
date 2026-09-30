"""Синтетический корпус новостей для тестов. ВСЕ события, имена и цифры вымышленные и не относятся к реальным организациям."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

T0 = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)

# (key, name, country, reliability, tier, group, official, wire)
SOURCE_DEFS = {
    "nur": ("nur_kz", "NUR.KZ", "KZ", 0.75, "quality", "nur", False),
    "kursiv": ("kursiv", "Kursiv.media", "KZ", 0.85, "quality", "kursiv", False),
    "tengri": ("tengrinews", "Tengrinews", "KZ", 0.75, "quality", "tengri", False),
    "zakon": ("zakon_kz", "Zakon.kz", "KZ", 0.72, "quality", "zakon", False),
    "informburo": ("informburo", "Informburo.kz", "KZ", 0.72, "quality", "informburo", False),
    "reuters": ("reuters", "Reuters", "GB", 0.95, "quality", "reuters", False),
    "bbc": ("bbc_world", "BBC News", "GB", 0.92, "quality", "bbc", False),
    "nbk": ("nbk_official", "Нацбанк РК (пресс-служба)", "KZ", 0.98, "primary", "nbk", True),
}

# Вымышленные материалы: (id, source, минуты от T0, заголовок, текст)
ARTICLES = [
    ("A1", "nur", 0, "Нацбанк сохранил базовую ставку на уровне 16,5%",
     "Национальный банк Казахстана сохранил базовую ставку на уровне 16,5% годовых. Решение принято на заседании Комитета по денежно-кредитной политике 30 сентября. Инфляция в августе составила 12,3%, сообщили в Бюро национальной статистики. «Мы видим замедление инфляции, но риски остаются высокими», — заявил председатель Нацбанка Тимур Сулейменов."),
    ("A2", "kursiv", 25, "Базовая ставка в Казахстане осталась прежней — 16,5%",
     "Нацбанк Казахстана не стал менять базовую ставку и оставил её на уровне 16,5%. Об этом говорится в сообщении регулятора. Годовая инфляция в августе замедлилась до 12,3%. Председатель Нацбанка Тимур Сулейменов отметил: «Мы видим замедление инфляции, но риски остаются высокими». Следующее заседание запланировано на ноябрь."),
    ("A3", "tengri", 50, "Нацбанк РК не изменил ставку: 16,5% годовых",
     "Национальный банк Республики Казахстан принял решение оставить базовую ставку без изменений, на уровне 16,5%. Аналитики ожидали такого решения. Инфляция по итогам августа — 12,3%. Курс доллара к тенге остался стабильным."),
    ("A4", "reuters", 70, "Kazakhstan central bank holds base rate at 16.5%",
     "Kazakhstan's central bank kept its base rate unchanged at 16.5% on Tuesday, as expected, citing slowing inflation of 12.3% in August. Governor Timur Suleimenov said risks remain high. The National Bank of Kazakhstan will hold its next policy meeting in November."),
    ("A5", "zakon", 90, "Нацбанк оставил ставку на уровне 16,5%",
     "Национальный банк Казахстана сохранил базовую ставку на уровне 16,5% годовых. Об этом передает Kazinform. Решение принято на заседании Комитета по денежно-кредитной политике. Инфляция в августе составила 12,3%."),
    ("B1", "nur", 40, "Нацбанк ужесточит требования к капиталу банков с 1 января",
     "Национальный банк Казахстана с 1 января 2027 года повысит требования к достаточности капитала для банков второго уровня. Новые нормативы затронут 12 банков. Регулятор ожидает, что мера повысит устойчивость финансовой системы. Об этом сообщили в пресс-службе регулятора."),
    ("C1", "reuters", 10, "Tesla delivers record 500,000 cars in third quarter",
     "Tesla said on Wednesday it delivered a record 500,000 vehicles in the third quarter, beating analyst estimates of 470,000. Shares rose 4% in premarket trading. CEO Elon Musk praised the team for the result."),
    ("C2", "kursiv", 120, "Tesla поставила рекордные 500 тысяч автомобилей в третьем квартале",
     "Компания Tesla сообщила о рекордных поставках в третьем квартале: 500 тысяч электромобилей, что выше ожиданий аналитиков в 470 тысяч. Акции компании выросли на 4% в ходе предторговой сессии. Глава компании Илон Маск поблагодарил команду."),
    ("C3", "bbc", 30, "Tesla stock jumps after record quarter of 500,000 deliveries",
     "Shares in Tesla jumped 4% in premarket trading after the electric carmaker reported record deliveries of 500,000 vehicles in the third quarter, well above the 470,000 expected by analysts."),
    ("D1", "tengri", 15, "В Алматы открыли новую станцию метро",
     "В Алматы после реконструкции открыли станцию метро «Сарыарка». Пассажиропоток ожидается на уровне 20 тысяч человек в сутки. Строительство заняло три года и обошлось в 18 млрд тенге. Об этом сообщил аким города."),
    ("D2", "informburo", 80, "Акимат Алматы: новая станция метро начала работу",
     "Станция метро в Алматы начала принимать пассажиров. Строительство обошлось в 18 млрд тенге, рассказал аким города. Ожидаемый пассажиропоток — 20 тысяч человек в сутки."),
    ("E1", "nur", 20, "Tesla запустила в Казахстане сеть зарядных станций",
     "Компания Tesla открыла первые зарядные станции Supercharger в Алматы и Астане. Всего будет установлено 40 зарядных устройств. Об этом сообщили представители компании на презентации в Астане."),
    ("F1", "bbc", 45, "Scientists find ancient tomb in Egypt with 50 mummies",
     "Archaeologists have discovered an ancient tomb in Egypt containing 50 mummies dating back 2,500 years, the ministry of antiquities said on Tuesday. The find was made near Luxor."),
]
SAME_EVENT = [("A1", "A2", "A3", "A4", "A5"), ("C1", "C2", "C3"), ("D1", "D2")]


def source_row(key_alias: str) -> dict:
    k, name, country, rel, tier, group, official = SOURCE_DEFS[key_alias]
    return {"key": k, "name": name, "country": country, "reliability": rel, "tier": tier, "independence_group": group, "is_official": official}


def published_at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ns_article(aid: str, know, *, alias_index=None):
    """Статья в виде объекта с признаками (для тестов сходства без БД)."""
    from smi_agent.core.text import simhash, tokenize
    from smi_agent.ingestion.features import compute_features

    row = next(a for a in ARTICLES if a[0] == aid)
    _id, src, minutes, title, body = row
    sd = source_row(src)
    source = NS(id=list(SOURCE_DEFS).index(src) + 1, aliases=[], **sd)
    f = compute_features(title, "", body, know=know, alias_index=alias_index or {}).as_dict()
    toks = tokenize(f"{title} {body[:1000]}", lang=f["lang"])
    return NS(id=int(aid[1:]) + ord(aid[0]) * 100, source_id=source.id, source=source, title=title, summary="", body=body, lang=f["lang"], features=f, published_at=published_at(minutes), simhash=f"{simhash(toks):016x}", has_full_text=True)
