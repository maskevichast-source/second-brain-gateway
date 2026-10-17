"""Схема заметок «Второго Мозга», версия 1.

Единый источник правды: из неё строится промпт для ИИ и проверяются его ответы.
Человекочитаемое описание лежит в docs/schema.html.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 1

AREAS = (
    "семья", "дом", "работа", "учёба", "здоровье", "финансы", "отдых",
    "хобби", "поездки", "окружение", "техника", "развитие", "разное",
)
DEFAULT_AREA = "разное"
PRIORITIES = ("низкий", "обычный", "высокий")

STATUS_A = ("хочу", "в-процессе", "готово", "отложено", "отказался")
STATUS_B = ("открыто", "в-работе", "ждёт", "сделано", "отложено", "отменено")

# Поля, которые ИИ может дополнить из собственных знаний (с пометкой ai_filled).
AI_FILLABLE = frozenset({"year", "creator", "author", "work", "platform"})


@dataclass(frozen=True)
class Field:
    """Тип значения поля: str, int, num, date, datetime, md, list, link, enum."""

    kind: str
    values: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None


S = Field("str")
LST = Field("list")
LNK = Field("link")
DATE = Field("date")
DT = Field("datetime")
MD = Field("md")


def INT(lo: int, hi: int) -> Field:
    return Field("int", lo=lo, hi=hi)


def ENUM(*values: str) -> Field:
    return Field("enum", values=values)


NUM = Field("num", lo=0)


@dataclass(frozen=True)
class TypeSpec:
    id: str
    name: str
    group: str
    when: str
    kinds: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    fields: dict[str, Field] = field(default_factory=dict)


def _t(id, name, group, when, kinds=(), statuses=(), /, **fields) -> TypeSpec:
    return TypeSpec(id, name, group, when, tuple(kinds), tuple(statuses), dict(fields))


G_CONSUME = "Смотрю, читаю, слушаю, посещаю, покупаю"
G_PEOPLE = "Люди"
G_TIME = "Дела и время"
G_MIND = "Мысли и знания"
G_HOME = "Дом, документы, поездки, здоровье"
G_SERVICE = "Служебные"

_TYPES = [
    _t("media", "Медиа", G_CONSUME,
       "всё, что можно посмотреть, прочитать, послушать, пройти или изучить: фильм, сериал, книга, видео, статья, подкаст, курс, игра, музыка",
       ("фильм", "сериал", "мультфильм", "аниме", "книга", "аудиокнига", "видео", "статья",
        "подкаст", "курс", "игра", "музыка", "приложение", "другое"),
       STATUS_A,
       creator=S, year=INT(1850, 2100), platform=S, recommended_by=LNK, why=S,
       length=S, progress=S, rating=INT(1, 10), finished_at=DATE),
    _t("place", "Место", G_CONSUME,
       "место, куда хочется сходить или где уже был: заведение, отель, магазин, район, достопримечательность",
       ("ресторан", "кафе", "бар", "отель", "магазин", "услуга", "достопримечательность", "район", "парк", "другое"),
       STATUS_A,
       city=S, address=S, phone=S, recommended_by=LNK, why=S,
       price_level=ENUM("дёшево", "средне", "дорого")),
    _t("product", "Покупка", G_CONSUME,
       "вещь или услуга, которую хочется купить, выбрать или подарить",
       ("техника", "одежда", "для-дома", "подарок", "услуга", "другое"),
       STATUS_A,
       price=NUM, currency=S, where_to_buy=S, alternatives=LST, for_whom=LNK, deadline=DATE),

    _t("person", "Человек", G_PEOPLE,
       "данные о человеке или знакомство с ним",
       (), ("актуален", "неактуален"),
       name=S, first_name=S, last_name=S, patronymic=S, aliases=LST,
       relation=S,
       relation_group=ENUM("семья", "друзья", "коллеги", "клиенты", "подрядчики", "врачи", "соседи", "знакомые", "другое"),
       how_met=S, met_date=DATE, introduced_by=LNK, last_contact=DATE, contact_goal=S,
       phones=LST, email=S, messengers=LST, social=LST, website=S, address=S, city=S, country=S, timezone=S,
       company=S, role=S, industry=S, skills=LST, can_help_with=LST,
       birthday=MD, family=S, interests=LST, preferences=S, gift_ideas=LST, languages=LST, notes=S,
       orbit=INT(0, 4), cadence_days=INT(1, 365), partner=S, children=LST),
    _t("interaction", "Контакт", G_PEOPLE,
       "состоялся контакт с человеком: звонок, встреча, переписка; если после него есть что сделать, это отдельная заметка task",
       ("звонок", "видеозвонок", "встреча", "сообщение", "письмо"), (),
       topic=S, agreements=S, follow_up=S, follow_up_due=DATE, duration=S),

    _t("task", "Дело", G_TIME,
       "нужно сделать одно конкретное действие",
       (), STATUS_B,
       due=DT, remind_at=DT, repeat=S, estimate=S, project=LNK, promise=ENUM("я", "мне")),
    _t("event", "Событие", G_TIME,
       "запланированное событие с датой: встреча, билеты, мероприятие, визит",
       (), ("запланировано", "состоялось", "перенесено", "отменено"),
       event_at=DT, event_end=DT, location=S, remind_before=S, link=S),
    _t("project", "Проект", G_TIME,
       "цель из нескольких шагов, которую ведут неделями",
       (), STATUS_B,
       outcome=S, deadline=DATE, next_action=S),
    _t("goal", "Цель", G_TIME,
       "долгая цель или желание без конкретных шагов",
       (), ("активна", "достигнута", "пересмотрена", "отброшена"),
       horizon=ENUM("неделя", "месяц", "год", "жизнь"), metric=S, deadline=DATE),

    _t("idea", "Идея", G_MIND, "идея, которую можно развить",
       (), ("сырая", "прорабатываю", "реализована", "отложена", "отброшена"),
       next_step=S),
    _t("learning", "Узнал", G_MIND, "факт, вывод, инструкция или лайфхак, который не хочется потерять",
       (), (), takeaway=S, learned_from=S),
    _t("quote", "Цитата", G_MIND, "чужие слова, которые стоит сохранить",
       (), (), author=S, work=S),
    _t("question", "Вопрос", G_MIND, "вопрос, на который нужно найти ответ",
       (), ("открыт", "отвечен"), asked_to=LNK, answer=S),
    _t("decision", "Решение", G_MIND, "принятое решение и его причины",
       (), ("принято", "пересмотреть", "пересмотрено"),
       options=LST, reasons=S, decided_at=DATE, review_at=DATE),
    _t("journal", "Дневник", G_MIND,
       "события дня, рефлексия; настроение записывается, только если человек назвал его сам",
       (), (), mood=S),

    _t("home", "Дом и быт", G_HOME,
       "конкретная вещь или процесс в доме: техника, мебель, растения, ремонт, коммунальные дела, уход по расписанию",
       ("ремонт", "техника", "мебель", "растение", "уход", "коммунальное", "другое"),
       STATUS_B,
       item=S, schedule=S, next_due=DATE, contractor=LNK, warranty_until=DATE),
    _t("document", "Документ", G_HOME, "договор, справка, гарантия, претензия, счёт, инструкция",
       ("договор", "справка", "гарантия", "претензия", "счёт", "инструкция", "другое"),
       ("действует", "ждёт-подписи", "истёк", "расторгнут"),
       counterparty=S, number=S, doc_date=DATE, valid_until=DATE, location=S),
    _t("case", "Дело-процесс", G_HOME,
       "дело из нескольких этапов: спор, обращение, оформление, претензионная переписка",
       ("спор", "обращение", "оформление", "процесс", "другое"),
       STATUS_B,
       counterparty=S, opened_at=DATE, deadline=DATE, next_step=S, timeline=S),
    _t("trip", "Поездка", G_HOME, "поездка: идея, планы, брони",
       (), ("идея", "планирую", "забронировано", "в-пути", "завершена", "отменена"),
       destination=S, date_from=DATE, date_to=DATE, bookings=LST),
    _t("health", "Здоровье", G_HOME, "самочувствие, приём врача, лекарства, тренировки, сон",
       ("самочувствие", "приём-врача", "симптом", "лекарство", "тренировка", "сон", "питание"), (),
       follow_up_due=DATE),

    _t("bookmark", "Закладка", G_SERVICE,
       "ссылка, про которую пока неизвестно, что это; ночной разбор потом переложит её в нужный тип",
       (), ("не-разобрано", "разобрано"),
       domain=S, page_title=S, why_saved=S),
    _t("note", "Заметка", G_SERVICE, "всё, что не подошло ни под один тип", (), ()),
]

TYPES: dict[str, TypeSpec] = {t.id: t for t in _TYPES}
DEFAULT_TYPE = "note"

# Порядок общих полей в блоке свойств
HEAD_KEYS = ("schema", "title", "summary", "created", "type", "kind", "status", "stage", "area")
TAIL_KEYS = ("priority", "tags", "people", "related", "source", "source_url", "private",
             "ai_filled", "capture_id", "confidence")
COMMON_KEYS = frozenset(HEAD_KEYS + TAIL_KEYS)
