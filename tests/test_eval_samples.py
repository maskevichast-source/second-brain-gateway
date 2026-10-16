import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import eval_samples  # noqa: E402

DATA = eval_samples.load_samples()
SAMPLES = DATA["samples"]


def test_there_are_25_samples_with_unique_ids_and_valid_expectations():
    from app import schema

    assert len(SAMPLES) == 25 and len({s["id"] for s in SAMPLES}) == 25
    for s in SAMPLES:
        for n in s["expect"]["notes"]:
            assert n["type"] in schema.TYPES and n["area"] in schema.AREAS
    kinds = {n["type"] for s in SAMPLES for n in s["expect"]["notes"]}
    assert len(kinds) >= 15
    assert any(s["expect"].get("private") for s in SAMPLES) and any(len(s["expect"]["notes"]) > 1 for s in SAMPLES)


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_code_normalizes_predefined_ai_answers(sample):
    from app.llm import LLMResult

    result = LLMResult.model_validate(sample["ai"]) if sample.get("ai") else None
    fronts = eval_samples.normalize(sample, result, eval_samples.sample_now(DATA))
    assert eval_samples.compare(sample, fronts) == []


def test_private_samples_never_need_ai():
    for s in SAMPLES:
        if s["expect"].get("private"):
            assert s.get("ai") is None and s["text"].lstrip().lower().startswith(("!", "секрет:"))


def test_compare_detects_mismatch():
    sample = SAMPLES[0]
    assert eval_samples.compare(sample, [{"type": "idea", "area": "дом"}])
    assert eval_samples.compare(sample, []) == ["заметок 0, ожидалось 1"]


def test_script_without_key_is_polite_and_does_not_fail():
    import os

    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    run = subprocess.run([sys.executable, str(Path(eval_samples.__file__)) ], env=env, capture_output=True, text=True)
    assert run.returncode == 0 and "DEEPSEEK_API_KEY" in run.stdout


def test_offline_table_is_all_green(capsys):
    assert eval_samples.main(["--offline"]) == 0
    assert "Полностью совпало: 25 из 25" in capsys.readouterr().out
