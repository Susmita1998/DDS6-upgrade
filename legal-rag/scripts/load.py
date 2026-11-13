"""Generate normal and slow traffic against the RAG API so traces and logs exist to inspect.

Usage (from legal-rag):  python scripts/load.py [--base http://127.0.0.1:8100] [--rounds 5]
"""

import argparse
import time

import httpx

QUESTIONS = [
    "What is the liability cap and what are the carve-outs?",
    "Does the amendment change the liability cap?",
    "How long do confidentiality obligations survive?",
    "What is the breach notification deadline for personal data?",
    "What is the NDA duration?",
    "What is the capital of France?",
]
SEARCHES = [
    "liability cap section 4.1",
    "does the amendment supersede the cap",
    "governing law Delaware",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=60) as client:
        for _ in range(args.rounds):
            for q in QUESTIONS:
                r = client.post("/ask", json={"question": q})
                r.raise_for_status()
            for q in SEARCHES:
                client.get("/search", params={"q": q}).raise_for_status()
        # A few slow searches run the unindexed audit report, which is the latency to explain.
        for q in SEARCHES:
            started = time.perf_counter()
            r = client.get("/search", params={"q": q, "slow": 1})
            r.raise_for_status()
            print(f"slow search {q!r}: {(time.perf_counter() - started) * 1000:.0f} ms")
    print(f"done: {args.rounds * (len(QUESTIONS) + len(SEARCHES)) + len(SEARCHES)} requests")


if __name__ == "__main__":
    main()
