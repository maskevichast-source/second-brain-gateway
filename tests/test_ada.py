

def test_loans_in_summary_and_morning_brief():
    from app import ada
    data = {"currency": "₸", "month_label": "ноябрь", "loans": {
        "count": 2, "balance": 3_200_000, "month_left": 190_000, "last_end": "2030-12",
        "soon": [{"name": "Forte заём", "date": "2026-11-20", "amount": 151304, "days_left": 2},
                 {"name": "Kaspi кредиты", "date": "2026-11-28", "amount": 56975, "days_left": 10}]}}
    text = ada.format_summary(data)
    assert "кредиты: 2 шт." in text and "Forte заём" in text and "190 000" in text
    brief = ada.loans_brief(data)
    assert "Forte заём" in brief and "через 2 дн." in brief and "Kaspi" not in brief
    assert ada.loans_brief({"loans": {}}) == "" and ada.loans_brief({}) == ""
    assert ada.format_summary({"currency": "₸", "loans": {}}) == ""
