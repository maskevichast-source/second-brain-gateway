#!/usr/bin/env python3
"""Проверка качества разбора записей: 25 типичных записей владельца и ожидаемые тип и сфера.

    python tools/eval_samples.py            # нужен DEEPSEEK_API_KEY: прогонит записи через реальный ИИ
    python tools/eval_samples.py --offline  # без ключа и сети: проверит код на заранее заданных ответах ИИ

Без ключа скрипт ничего не отправляет и вежливо об этом сообщает. С ключом тексты записей уходят в DeepSeek,
поэтому в наборе только выдуманные примеры. Данные: tests/fixtures/eval_samples.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.llm import LLMResult, structure_capture  # noqa: E402
from app.service import build_private_response, build_response  # noqa: E402
from app.textutil import find_urls, split_private  # noqa: E402
from app.vault import parse_note  # noqa: E402

SAMPLES_PATH = ROOT / "tests" / "fixtures" / "eval_samples.json"


def load_samples(path: Path = SAMPLES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sample_now(data: dict) -> datetime:
    return datetime.fromisoformat(data["now"]).replace(tzinfo=ZoneInfo(data.get("timezone", "Asia/Almaty")))


def normalize(sample: dict, result: LLMResult | None, now: datetime) -> list[dict]:
    """Прогоняет запись через тот же код, что и сервис, и возвращает свойства получившихся заметок."""
    private, text = split_private(sample["text"])
    if private:
        response = build_private_response(capture_id="eval-" + sample["id"], client="eval", text=text, now=now)
    else:
        urls = find_urls(sample["text"])
        response = build_response(capture_id="eval-" + sample["id"], revision=1, client="eval", content=sample["text"],
                                  clarifications="", result=result, links=[], content_urls=urls, now=now)
    return [dict(parse_note(f["markdown"], 1).front, _body=parse_note(f["markdown"], 1).body) for f in response["files"]]


def compare(sample: dict, fronts: list[dict]) -> list[str]:
    """Список расхождений с ожиданием (пустой — всё совпало)."""
    expect = sample["expect"]
    problems: list[str] = []
    want = expect["notes"]
    if len(fronts) != len(want):
        return [f"заметок {len(fronts)}, ожидалось {len(want)}"]
    for i, (exp, got) in enumerate(zip(want, fronts), 1):
        tag = f"#{i} " if len(want) > 1 else ""
        for key in ("type", "area", "kind", "status"):
            if key in exp and str(got.get(key, "")) != exp[key]:
                problems.append(f"{tag}{key}: {got.get(key, '—')} вместо {exp[key]}")
        for key, value in (exp.get("fields") or {}).items():
            shown = got.get(key)
            if isinstance(shown, list):
                shown = ", ".join(map(str, shown))
            if str(shown if shown is not None else "—") != str(value):
                problems.append(f"{tag}{key}: {shown if shown is not None else '—'} вместо {value}")
        for key in exp.get("absent") or []:
            if got.get(key) not in (None, ""):
                problems.append(f"{tag}поле {key} не должно быть заполнено")
    if expect.get("private") and not all(str(f.get("private")).lower() == "true" for f in fronts):
        problems.append("запись должна быть приватной")
    for word in expect.get("forbid_text") or []:
        if any(word in str(f.get("_body", "")).lower() for f in fronts):
            problems.append(f"в тексте не должно быть «{word}»")
    return problems


def offline_results(data: dict) -> list[tuple[dict, list[dict]]]:
    now = sample_now(data)
    out = []
    for sample in data["samples"]:
        result = LLMResult.model_validate(sample["ai"]) if sample.get("ai") else None
        out.append((sample, normalize(sample, result, now)))
    return out


async def live_results(data: dict, api_key: str) -> list[tuple[dict, list[dict]]]:
    now = sample_now(data)
    settings = Settings(
        capture_token="x" * 32, deepseek_api_key=api_key,
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        data_dir=Path("."), timezone=data.get("timezone", "Asia/Almaty"), llm_timeout=60.0,
        rate_limit_per_minute=1000, max_content_chars=20000)
    out = []
    for sample in data["samples"]:
        private, _ = split_private(sample["text"])
        result = None if private else await structure_capture(settings, content=sample["text"], now=now)
        out.append((sample, normalize(sample, result, now)))
    return out


def print_table(rows: list[tuple[dict, list[dict]]]) -> int:
    print(f"{'запись':<18} {'ожидалось (тип/сфера)':<28} {'получено (тип/сфера)':<28} итог")
    good = type_ok = area_ok = 0
    for sample, fronts in rows:
        want = "+".join(f"{n['type']}/{n['area']}" for n in sample["expect"]["notes"])
        got = "+".join(f"{f.get('type', '?')}/{f.get('area', '?')}" for f in fronts)
        problems = compare(sample, fronts)
        pairs = list(zip(sample["expect"]["notes"], fronts))
        type_ok += all(e["type"] == str(g.get("type")) for e, g in pairs) and len(pairs) == len(sample["expect"]["notes"])
        area_ok += all(e["area"] == str(g.get("area")) for e, g in pairs) and len(pairs) == len(sample["expect"]["notes"])
        good += not problems
        print(f"{sample['id']:<18} {want:<28} {got:<28} {'OK' if not problems else 'расхождение'}")
        for p in problems:
            print(f"{'':<18}   - {p}")
    total = len(rows)
    print(f"\nПолностью совпало: {good} из {total}; тип верный: {type_ok}; сфера верная: {area_ok}.")
    return 0 if good == total else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Проверка качества разбора записей.")
    parser.add_argument("--offline", action="store_true", help="не вызывать ИИ: проверить код на заранее заданных ответах")
    args = parser.parse_args(argv)
    data = load_samples()
    if args.offline:
        print("Режим без сети: проверяю код на заранее заданных ответах ИИ.\n")
        return print_table(offline_results(data))
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("Не задан DEEPSEEK_API_KEY, поэтому настоящий прогон не запускаю (запись уходит в DeepSeek только с ключом).\n"
              "Задай ключ в переменной окружения и запусти снова. Проверка кода без ИИ: python tools/eval_samples.py --offline")
        return 0
    print(f"Прогоняю {len(data['samples'])} записей через DeepSeek...\n")
    return print_table(asyncio.run(live_results(data, key)))


if __name__ == "__main__":
    sys.exit(main())
