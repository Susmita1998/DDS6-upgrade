"""Golden-set evaluation: runs every test case through the live pipeline and scores it.

Runs in a background thread so the UI can poll live progress. Scores are lexical proxies
(token overlap), not LLM-judged RAGAS metrics. They are deterministic and free to run,
but extractive answers quote clauses, so they reward quoting.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace

from . import pipeline, rag

BASE = Path(__file__).resolve().parent.parent
GOLDEN = BASE / "eval" / "golden_dataset.json"
RUNS_DIR = BASE / "eval" / "runs"
tracer = trace.get_tracer("legal-rag.eval")

# Question scaffolding is removed before measuring how much of the question the answer covers.
QUESTION_WORDS = {
    "what", "how", "does", "do", "we", "our", "if", "can", "is", "are", "who", "which", "give", "me",
    "walk", "through", "about", "any", "they", "their", "there", "long", "much", "many", "you", "that",
    "it", "still", "after", "big", "picture", "actually", "covered", "covers",
}
PASS_ANSWER_RECALL = 0.6

_lock = threading.Lock()
_state = {
    "running": False, "run_id": None, "total": 0, "cases": [], "trace_id": None,
    "started_at": None, "finished_at": None, "mode": None, "error": None,
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mode():
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "extractive"


def load_golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _words(text):
    return set(rag.tokenize(text))


def _share(needles, haystack):
    return len(needles & haystack) / len(needles) if needles else None


def score_case(case, result, elapsed_ms):
    gt = set(case["ground_truth_context_ids"])
    retrieved = result["retrieved_parent_ids"]
    answer_words = _words(result["answer"])
    refused = result["llm"] == "none"
    row = {
        "test_case_id": case["test_case_id"],
        "question": case["question"],
        "question_type": case["question_type"],
        "difficulty": case["difficulty"],
        "expected_answer": case["expected_answer"],
        "answer": result["answer"],
        "llm": result["llm"],
        "retrieved_ids": retrieved,
        "ground_truth_context_ids": sorted(gt),
        "latency_ms": round(elapsed_ms, 1),
        "trace_id": result["trace_id"],
        "refused": refused,
    }
    if not gt:  # negative case: the only correct behaviour is to decline
        row.update(context_precision=None, context_recall=None, faithfulness=None,
                   answer_recall=None, answer_relevancy=None, passed=refused)
        return row
    hit = set(retrieved) & gt
    context_words = _words(" ".join(p["text"] for p in result["parents"]))
    question_words = _words(case["question"]) - QUESTION_WORDS
    row.update(
        context_recall=round(len(hit) / len(gt), 3),
        context_precision=round(len(hit) / len(retrieved), 3) if retrieved else 0.0,
        faithfulness=round(_share(answer_words, context_words) or 0.0, 3),
        answer_recall=round(_share(_words(case["expected_answer"]), answer_words) or 0.0, 3),
        answer_relevancy=round(_share(question_words, answer_words) or 0.0, 3),
    )
    row["passed"] = (
        not refused and row["context_recall"] == 1.0 and row["answer_recall"] >= PASS_ANSWER_RECALL
    )
    return row


def _mean(rows, key):
    vals = [r[key] for r in rows if r[key] is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _percentile(values, p):
    values = sorted(values)
    return round(values[int(p * (len(values) - 1))], 1) if values else None


def summarize(rows, total=None):
    if not rows:
        return None
    pos = [r for r in rows if r["ground_truth_context_ids"]]
    neg = [r for r in rows if not r["ground_truth_context_ids"]]
    by_type = {}
    for qtype in sorted({r["question_type"] for r in rows}):
        sub = [r for r in rows if r["question_type"] == qtype]
        by_type[qtype] = {
            "count": len(sub),
            "pass_rate": round(sum(r["passed"] for r in sub) / len(sub), 3),
            "context_recall": _mean(sub, "context_recall"),
            "context_precision": _mean(sub, "context_precision"),
        }
    return {
        "completed": len(rows),
        "total": total or len(rows),
        "passed": sum(r["passed"] for r in rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
        "context_recall": _mean(pos, "context_recall"),
        "context_precision": _mean(pos, "context_precision"),
        "faithfulness": _mean(pos, "faithfulness"),
        "answer_recall": _mean(pos, "answer_recall"),
        "answer_relevancy": _mean(pos, "answer_relevancy"),
        "negative_refusal_rate": round(sum(r["passed"] for r in neg) / len(neg), 3) if neg else None,
        "latency_p50_ms": _percentile([r["latency_ms"] for r in rows], 0.5),
        "latency_p95_ms": _percentile([r["latency_ms"] for r in rows], 0.95),
        "by_type": by_type,
    }


def status():
    with _lock:
        snap = {k: (list(v) if k == "cases" else v) for k, v in _state.items()}
    snap["done"] = len(snap["cases"])
    snap["summary"] = summarize(snap["cases"], snap["total"])
    return snap


def start(index, store):
    golden = load_golden()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with _lock:
        if _state["running"]:
            raise RuntimeError("an evaluation is already running")
        _state.update(running=True, run_id=run_id, total=len(golden), cases=[], trace_id=None,
                      started_at=_now(), finished_at=None, mode=mode(), error=None)
    threading.Thread(target=_execute, args=(index, store, golden), daemon=True).start()
    return run_id


def _execute(index, store, golden):
    try:
        with tracer.start_as_current_span("eval.run") as span:
            span.set_attribute("eval.cases", len(golden))
            span.set_attribute("eval.mode", mode())
            with _lock:
                _state["trace_id"] = format(span.get_span_context().trace_id, "032x")
            for case in golden:
                started = time.perf_counter()
                with tracer.start_as_current_span("eval.case") as case_span:
                    case_span.set_attribute("eval.test_case_id", case["test_case_id"])
                    result = pipeline.answer(case["question"], index, store)
                    row = score_case(case, result, (time.perf_counter() - started) * 1000)
                    case_span.set_attribute("eval.passed", row["passed"])
                with _lock:
                    _state["cases"].append(row)
            summary = summarize(_state["cases"], len(golden))
            span.set_attribute("eval.pass_rate", summary["pass_rate"])
    except Exception as exc:  # surface the failure in the UI instead of dying silently
        with _lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        with _lock:
            _state["running"] = False
            _state["finished_at"] = _now()
            run = {k: (list(v) if k == "cases" else v) for k, v in _state.items() if k != "running"}
        _save(run)


def _save(run):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**run, "summary": summarize(run["cases"], run["total"])}
    (RUNS_DIR / f"{run['run_id']}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_runs():
    if not RUNS_DIR.exists():
        return []
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        runs.append({
            "run_id": data["run_id"], "started_at": data["started_at"], "finished_at": data["finished_at"],
            "mode": data["mode"], "trace_id": data["trace_id"], "summary": data["summary"],
        })
    return runs
