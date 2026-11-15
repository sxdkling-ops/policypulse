"""Run the full pipeline on one known-stale answer. Usage: python smoke_test.py"""
import sys

from llm import GatewayError
from pipeline import check_answer

ANSWER = "The EU AI Act is still a proposal and has not been adopted."

try:
    report = check_answer(ANSWER, progress=lambda i, n, c: print(f"[{i + 1}/{n}] {c}"))
except GatewayError as e:
    sys.exit(f"Gateway error: {e}")

for r in report["results"]:
    print(f"\n{r['verdict']} ({r['freshness_score']}/100) - {r['claim']}")
    print(f"  evidence: {r['evidence']}")
    print(f"  fix: {r['suggested_correction']}")
    for s in r["sources"]:
        print(f"  - {s['url']}")
print("\nOverall:", report["overall"])

ok = any(r["verdict"] == "Outdated" for r in report["results"])
print("PASS" if ok else "FAIL: expected an Outdated verdict")
sys.exit(0 if ok else 1)
