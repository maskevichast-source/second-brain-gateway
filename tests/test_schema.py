from app import schema
from app.prompt import build_system_prompt


def test_counts_match_the_published_schema():
    assert len(schema.TYPES) == 22
    assert len(schema.AREAS) == 13
    assert schema.SCHEMA_VERSION == 1


def test_default_area_and_type_exist():
    assert schema.DEFAULT_AREA in schema.AREAS
    assert schema.DEFAULT_TYPE in schema.TYPES


def test_type_fields_do_not_shadow_common_fields():
    for spec in schema.TYPES.values():
        clash = set(spec.fields) & schema.COMMON_KEYS
        assert not clash, f"{spec.id}: {clash}"


def test_enum_fields_have_values_and_ranges_are_sane():
    for spec in schema.TYPES.values():
        for name, field in spec.fields.items():
            if field.kind == "enum":
                assert field.values, f"{spec.id}.{name}"
            if field.kind == "int":
                assert field.lo is not None and field.hi is not None and field.lo < field.hi


def test_ai_fillable_fields_exist_somewhere():
    known = {name for spec in schema.TYPES.values() for name in spec.fields}
    assert schema.AI_FILLABLE <= known


def test_status_sets():
    assert schema.TYPES["media"].statuses == schema.STATUS_A
    assert schema.TYPES["task"].statuses == schema.STATUS_B
    assert schema.TYPES["note"].statuses == ()
    assert schema.TYPES["interaction"].statuses == ()
    assert schema.TYPES["media"].statuses[0] == "хочу"
    assert schema.TYPES["task"].statuses[0] == "открыто"


def test_kinds_with_fallback_value():
    for type_id in ("media", "place", "product", "home", "document", "case"):
        assert "другое" in schema.TYPES[type_id].kinds


def test_person_card_has_all_data_groups():
    person = set(schema.TYPES["person"].fields)
    for key in ("phones", "email", "messengers", "birthday", "company", "role", "how_met",
                "aliases", "patronymic", "gift_ideas", "relation_group"):
        assert key in person


def test_prompt_lists_every_type_area_and_rule():
    prompt = build_system_prompt()
    for type_id in schema.TYPES:
        assert f"- {type_id} «" in prompt
    for area in schema.AREAS:
        assert area in prompt
    for must in ("JSON", "<<<ДАННЫЕ", "ai_filled", "в-аду", "needs_review", "не больше 10"):
        assert must in prompt


def test_prompt_is_stable_and_cached():
    assert build_system_prompt() is build_system_prompt()
