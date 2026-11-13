"""One RAG query end to end: retrieve, expand to parent clauses, generate.

Shared by /ask, /api/rag/query and the golden-set evaluator, so every path runs the
same pipeline and emits the same spans.
"""

from opentelemetry import trace

from . import rag

tracer = trace.get_tracer("legal-rag.pipeline")


def answer(question, index, store):
    with tracer.start_as_current_span("rag.ask") as span:
        span.set_attribute("rag.question_length", len(question))
        trace_id = format(span.get_span_context().trace_id, "032x")
        detail = {}
        hits = index.retrieve(question, detail)
        best = {}
        for hit in hits:  # hits are sorted best-first, so the first score per parent wins
            best.setdefault(hit.parent_id, hit.score)
        parents = store.get_parents(list(best))
        for parent in parents:
            parent["score"] = best[parent["parent_id"]]
        parents.sort(key=lambda p: -p["score"])
        result = rag.generate(question, parents)
        store.record_access(parents[0]["doc_id"] if parents else "none", "ask", f"question_chars={len(question)}")
        span.set_attribute("rag.llm", result["llm"])
    return {
        **result,
        "trace_id": trace_id,
        "retrieved_parent_ids": [p["parent_id"] for p in parents],
        "parents": parents,
        "stages": detail,
    }
