"""Ground-truth style evaluation helpers for retrieval+answer quality."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from threelane_memory import query as memory_query

DEFAULT_EVAL_PATH = (
    Path(__file__).resolve().parents[1] / "test_dataset" / "arthur_eval_questions.json"
)


def load_eval_cases(path: str | None = None) -> list[dict]:
    """Load evaluation cases from JSON."""
    eval_path = Path(path) if path else DEFAULT_EVAL_PATH
    with eval_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, list):
        raise ValueError("Evaluation file must be a JSON array.")

    return payload


def evaluate_cases(
    cases: list[dict],
    speaker: str = "default",
    query_fn: Callable[[str, str], str] | None = None,
) -> dict:
    """Evaluate cases by checking required/forbidden substrings in answers."""
    ask = query_fn or (lambda question, who: memory_query(question, speaker=who))
    results = []
    passed = 0

    for case in cases:
        question = str(case.get("question", "")).strip()
        if not question:
            continue

        required = [str(x).strip() for x in case.get("expected_contains", []) if str(x).strip()]
        forbidden = [str(x).strip() for x in case.get("forbidden_contains", []) if str(x).strip()]

        answer = ask(question, speaker)
        answer_lower = answer.lower()

        missing = [token for token in required if token.lower() not in answer_lower]
        violated = [token for token in forbidden if token.lower() in answer_lower]
        ok = not missing and not violated
        if ok:
            passed += 1

        results.append(
            {
                "id": case.get("id"),
                "question": question,
                "answer": answer,
                "required": required,
                "forbidden": forbidden,
                "missing": missing,
                "violated": violated,
                "passed": ok,
            }
        )

    total = len(results)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": (passed / total) if total else 0.0,
        "results": results,
    }
