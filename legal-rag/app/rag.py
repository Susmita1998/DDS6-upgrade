"""Method 1: hierarchical hybrid RAG for contract Q&A.

Ingest: parse clauses -> parent chunks (clause) + child chunks (word windows)
        -> tokenize -> embed -> index (BM25 + dense vectors)
Query:  BM25 + dense search -> reciprocal rank fusion -> rerank
        -> child-to-parent expansion -> cited answer

embed() is a hashed lexical stand-in so the app runs offline. Swap it for a
neural embedding model in production; the rest of the pipeline stays the same.
"""

import hashlib
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

import httpx
from opentelemetry import metrics, trace

log = logging.getLogger("legal-rag.rag")
tracer = trace.get_tracer("legal-rag.rag")
retrieval_ms = metrics.get_meter("legal-rag").create_histogram(
    "rag.retrieval.duration", unit="ms", description="Hybrid search + rerank latency"
)

CHILD_WORDS = 60
CHILD_OVERLAP = 8
EMBED_DIM = 512
CANDIDATES = 50
RERANK_POOL = 20
RERANK_KEEP = 5
RRF_K = 60
MIN_SCORE = 0.25
SECTION_BONUS = 0.5
REFUSAL = "I am sorry, but the provided documentation does not contain information regarding this question."

STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "this to was were will with shall may any each either other such under".split()
)
ARTICLE_RE = re.compile(r"^ARTICLE\s+\d+\s*[-–:]\s*(.+)$")
CLAUSE_RE = re.compile(r"^(\d+\.\d+)\s+(.+)$")
TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
SECTION_RE = re.compile(r"\b\d+\.\d+\b")


@dataclass
class Hit:
    child_id: str
    parent_id: str
    doc_id: str
    clause_id: str
    score: float


def parse_clauses(body):
    """Split contract text into clauses, remembering the article each clause sits under."""
    clauses, article, current = [], "", None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if match := ARTICLE_RE.match(line):
            article, current = match.group(1).strip().title(), None
        elif match := CLAUSE_RE.match(line):
            current = {"clause_id": match.group(1), "article": article, "text": match.group(2)}
            clauses.append(current)
        elif current is not None:
            current["text"] += " " + line
    return clauses


def chunk_clauses(doc_id, title, clauses):
    """Parent = whole clause. Child = overlapping word windows with the heading path prepended."""
    parents, children = [], []
    step = CHILD_WORDS - CHILD_OVERLAP
    for clause in clauses:
        parent_id = f"{doc_id}#{clause['clause_id']}"
        heading = " > ".join(filter(None, [title, clause["article"], clause["clause_id"]]))
        parents.append(
            {
                "parent_id": parent_id,
                "doc_id": doc_id,
                "clause_id": clause["clause_id"],
                "heading": heading,
                "text": clause["text"],
            }
        )
        words = clause["text"].split()
        for n, start in enumerate(range(0, max(len(words) - CHILD_OVERLAP, 1), step)):
            window = " ".join(words[start : start + CHILD_WORDS])
            children.append(
                {
                    "child_id": f"{parent_id}/{n}",
                    "parent_id": parent_id,
                    "doc_id": doc_id,
                    "clause_id": clause["clause_id"],
                    "title": title,
                    "heading": heading,
                    "text": f"{heading}\n{window}",
                }
            )
    return parents, children


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def embed(text):
    """Sparse, L2-normalised hashed unigram + bigram vector (stand-in for a neural embedder)."""
    toks = tokenize(text)
    grams = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
    vec = Counter()
    for gram in grams:
        h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        vec[h % EMBED_DIM] += 1 if h >> 63 == 0 else -1
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {i: v / norm for i, v in vec.items() if v}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(i, 0.0) for i, v in a.items())


def elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000, 2)


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.postings = defaultdict(dict)
        self.lengths = {}
        for doc_id, toks in docs.items():
            self.lengths[doc_id] = len(toks)
            for term, tf in Counter(toks).items():
                self.postings[term][doc_id] = tf
        self.n = len(docs)
        self.avgdl = sum(self.lengths.values()) / max(self.n, 1)

    def idf(self, term):
        df = len(self.postings.get(term, {}))
        if df == 0:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, terms, k):
        scores = Counter()
        for term in set(terms):
            idf = self.idf(term)
            for doc_id, tf in self.postings.get(term, {}).items():
                norm = tf * (self.k1 + 1) / (
                    tf + self.k1 * (1 - self.b + self.b * self.lengths[doc_id] / self.avgdl)
                )
                scores[doc_id] += idf * norm
        return scores.most_common(k)


def rrf(rankings, k=RRF_K):
    """Reciprocal rank fusion of several ranked lists of (id, score)."""
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, (item, _) in enumerate(ranking, start=1):
            scores[item] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class HybridIndex:
    """In-process index over child chunks: BM25 (sparse) + hashed vectors (dense)."""

    def __init__(self, children):
        self.children = {c["child_id"]: c for c in children}
        self.tokens = {cid: tokenize(c["text"]) for cid, c in self.children.items()}
        self.bm25 = BM25(self.tokens)
        self.vectors = {cid: embed(c["text"]) for cid, c in self.children.items()}

    def retrieve(self, query, detail=None):
        """Return reranked hits. If `detail` is a dict, fill it with per-stage results and timings."""
        started = time.perf_counter()
        terms = tokenize(query)
        timings = {}
        with tracer.start_as_current_span("rag.retrieve") as span:
            span.set_attribute("rag.query_terms", len(terms))
            t = time.perf_counter()
            with tracer.start_as_current_span("rag.sparse_search"):
                sparse = self.bm25.search(terms, CANDIDATES)
            timings["sparse"] = elapsed_ms(t)
            t = time.perf_counter()
            with tracer.start_as_current_span("rag.dense_search"):
                dense = self._dense(query)
            timings["dense"] = elapsed_ms(t)
            t = time.perf_counter()
            with tracer.start_as_current_span("rag.fuse"):
                fused = rrf([sparse, dense])[:RERANK_POOL]
            timings["fuse"] = elapsed_ms(t)
            t = time.perf_counter()
            with tracer.start_as_current_span("rag.rerank") as rerank_span:
                hits = self._rerank(terms, query, fused)
                rerank_span.set_attribute("rag.pool_size", len(fused))
            timings["rerank"] = elapsed_ms(t)
            span.set_attribute("rag.hits", len(hits))
        timings["total"] = elapsed_ms(started)
        retrieval_ms.record(timings["total"])
        if detail is not None:
            detail["timings_ms"] = timings
            detail["sparse"] = self._describe(sparse[:10])
            detail["dense"] = self._describe(dense[:10])
            detail["fused"] = self._describe(fused[:10])
            detail["reranked"] = self._describe([(h.child_id, h.score) for h in hits])
        return hits

    def _describe(self, pairs):
        return [
            {"child_id": cid, "heading": self.children[cid]["heading"], "score": round(score, 3)}
            for cid, score in pairs
        ]

    def _dense(self, query):
        qvec = embed(query)
        scored = ((cid, cosine(qvec, vec)) for cid, vec in self.vectors.items())
        return sorted((s for s in scored if s[1] > 0), key=lambda s: -s[1])[:CANDIDATES]

    def _rerank(self, terms, query, fused):
        """Cross-encoder stand-in: IDF-weighted query-term coverage, plus a bonus for an explicit section reference."""
        query_terms = set(terms)
        total = sum(self.bm25.idf(t) for t in query_terms) or 1.0
        cited = set(SECTION_RE.findall(query))
        hits = []
        for child_id, _ in fused:
            child = self.children[child_id]
            matched = query_terms & set(self.tokens[child_id])
            coverage = sum(self.bm25.idf(t) for t in matched) / total
            score = coverage + (SECTION_BONUS if child["clause_id"] in cited else 0.0)
            hits.append(Hit(child_id, child["parent_id"], child["doc_id"], child["clause_id"], round(score, 3)))
        hits.sort(key=lambda h: -h.score)
        return hits[:RERANK_KEEP]


def generate(question, parents):
    """Answer from parent clauses with citations. Uses Claude if ANTHROPIC_API_KEY is set, else extractive."""
    if not parents or parents[0]["score"] < MIN_SCORE:
        return {"answer": REFUSAL, "llm": "none", "citations": []}
    citations = [
        {"doc_id": p["doc_id"], "clause": p["clause_id"], "heading": p["heading"], "score": p["score"]}
        for p in parents
    ]
    key = os.getenv("ANTHROPIC_API_KEY")
    with tracer.start_as_current_span("rag.generate") as span:
        if key:
            answer, llm = _call_claude(question, parents, key), "anthropic"
        else:
            answer = "\n".join(f"- {p['text']} [{p['doc_id']} §{p['clause_id']}]" for p in parents)
            llm = "extractive"
        span.set_attribute("rag.llm", llm)
        span.set_attribute("rag.citations", len(citations))
    return {"answer": answer, "llm": llm, "citations": citations}


def _call_claude(question, parents, key):
    context = "\n\n".join(f"[{p['doc_id']} §{p['clause_id']}] {p['text']}" for p in parents)
    system = (
        "You answer questions about legal contracts using only the provided clauses. "
        "Cite each claim as [doc §clause]. If the clauses do not answer the question, reply exactly: "
        f"'{REFUSAL}' Flag any amendment that overrides an earlier clause."
    )
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            "max_tokens": 600,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": f"Clauses:\n{context}\n\nQuestion: {question}"}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]
