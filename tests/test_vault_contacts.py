import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vault_contacts as vc  # noqa: E402

NOW = datetime(2026, 10, 12, 23, 30)

VCF = """BEGIN:VCARD
VERSION:3.0
N:Серикбаев;Серик;;;
FN:Серик Серикбаев
ORG:Рога и копыта;Отдел продаж
TITLE:Менеджер
TEL;type=CELL;type=VOICE;type=pref:+7 707 000 00 00
item1.TEL:+7 701 111 11 11
EMAIL;type=INTERNET:serik@example.com
BDAY:1990-03-05
NICKNAME:Серёга
ADR;type=HOME:;;Абая 1;Астана;;010000;Казахстан
NOTE:длинная заметка которая\\n продолжается
END:VCARD
BEGIN:VCARD
VERSION:3.0
N:Петрова;Аля;;;
BDAY:--0412
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Без Данных
END:VCARD
BEGIN:VCARD
VERSION:3.0
TEL:123
END:VCARD
"""


def test_parse_and_facts():
    cards = vc.parse_vcards(VCF)
    assert len(cards) == 4
    name, facts = vc.card_to_facts(cards[0])
    assert name == "Серик Серикбаев"
    assert facts["phones"] == ["+7 707 000 00 00", "+7 701 111 11 11"]
    assert facts["email"] == ["serik@example.com"] and facts["company"] == ["Рога и копыта, Отдел продаж"]
    assert facts["role"] == ["Менеджер"] and facts["birthday"] == ["1990-03-05"] and facts["aliases"] == ["Серёга"]
    assert "Абая 1" in facts["address"][0]
    assert vc.card_to_facts(cards[1])[0] == "Аля Петрова" and vc.card_to_facts(cards[1])[1]["birthday"] == ["04-12"]
    assert vc.card_to_facts(cards[3]) is None            # без имени карточку не создаём


def test_run_creates_and_merges_and_is_idempotent(tmp_path):
    vcf = tmp_path / "c.vcf"
    vcf.write_text(VCF, encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "1-Люди").mkdir(parents=True)
    (vault / "1-Люди" / "Аля Петрова.md").write_text('---\ntitle: "Аля Петрова"\ntype: "person"\nbirthday: "01-01"\n---\nмои заметки\n', encoding="utf-8")
    report = vc.run(vcf, vault, now=NOW)
    assert sorted(report["created"]) == ["Без Данных", "Серик Серикбаев"] and report["updated"] == []
    card = (vault / "1-Люди" / "Серик Серикбаев.md").read_text(encoding="utf-8")
    assert 'phones:\n  - "+7 707 000 00 00"\n  - "+7 701 111 11 11"' in card and 'birthday: "1990-03-05"' in card
    assert 'birthday: "01-01"' in (vault / "1-Люди" / "Аля Петрова.md").read_text(encoding="utf-8")   # не затёрто
    assert vc.run(vcf, vault, now=NOW) == {"created": [], "updated": []}


def test_dry_run_and_missing_files(tmp_path):
    vcf = tmp_path / "c.vcf"
    vcf.write_text(VCF, encoding="utf-8")
    assert vc.main([str(vcf), str(tmp_path), "--dry-run"]) == 0 and not (tmp_path / "1-Люди").exists()
    assert vc.main([str(tmp_path / "нет.vcf"), str(tmp_path)]) == 1
