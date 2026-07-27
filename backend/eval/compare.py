"""
Regression test: compare two eval runs.
Usage: python -m eval.compare <baseline_run_id> <current_run_id>

Exit code 0 = no regression, 1 = regression detected.
"""
import os
import sys
import json
import statistics
from glob import glob

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

AGGREGATE_REGRESSION_THRESHOLD = 5.0  # flag if aggregate metric degrades by more than 5%


def load_run(run_id: str) -> dict:
    """Returns {query_name: result_dict}"""
    run_dir = os.path.join(RESULTS_DIR, run_id)
    if not os.path.isdir(run_dir):
        print(f"ERROR: run '{run_id}' not found in {RESULTS_DIR}")
        sys.exit(1)
    results = {}
    for f in sorted(glob(os.path.join(run_dir, "*.json"))):
        if os.path.basename(f) == "summary.json":
            continue
        with open(f) as fh:
            r = json.load(fh)
            results[r["query"]] = r
    return results


def extract_aggregate(results: dict) -> dict:
    reductions, f1_scores, elapsed_list, token_list = [], [], [], []
    for r in results.values():
        b1     = r.get("b1_cost")
        s_comb = r.get("surgeon_combined_cost")
        f1     = (r.get("l2_combined") or {}).get("f1")
        ela    = r.get("elapsed_seconds")
        if b1 and s_comb and b1 > 0:
            reductions.append((b1 - s_comb) / b1 * 100)
        if f1 is not None:
            f1_scores.append(f1)
        if ela is not None:
            elapsed_list.append(ela)
        tin  = r.get("total_input_tokens")
        tout = r.get("total_output_tokens")
        if tin is not None and tout is not None:
            token_list.append(tin + tout)
    n = len(results)
    return {
        "n":                n,
        "pass_count":       sum(1 for r in results.values() if r.get("score", 0) == 1),
        "pass_rate":        sum(1 for r in results.values() if r.get("score", 0) == 1) / n * 100 if n else 0,
        "success_count":    sum(1 for r in results.values() if r.get("status") == "success"),
        "median_reduction": round(statistics.median(reductions), 2) if reductions else None,
        "median_f1":        round(statistics.median(f1_scores),  3) if f1_scores  else None,
        "median_elapsed":   round(statistics.median(elapsed_list), 1) if elapsed_list else None,
        "median_tokens":    round(statistics.median(token_list),   0) if token_list  else None,
    }


def classify_fail_reason(r: dict) -> str:
    """Classify why a score=0 result failed."""
    if r.get("score") == 1:
        return "pass"
    error = r.get("error") or r.get("agent_error") or ""
    status = r.get("status", "")
    if error and any(w in error for w in ["429", "RESOURCE_EXHAUSTED", "spending cap"]):
        return "api_error"
    if status == "exception" or (error and any(w in error.lower() for w in ["timeout", "timed out", "read operation timed out"])):
        return "timeout"
    b1     = r.get("b1_cost")
    s_cost = r.get("surgeon_combined_cost")
    if b1 and s_cost is not None and (b1 - s_cost) / b1 < 0.10:
        return "low_cost_reduction"
    precision = (r.get("l2_combined") or {}).get("precision", 0)
    recall    = (r.get("l2_combined") or {}).get("recall",    0)
    if precision < 0.5 and recall >= 0.5:
        return "low_precision"   # over-indexing: too many indexes recommended
    if recall < 0.5 and precision >= 0.5:
        return "low_recall"      # under-indexing: missed key indexes
    if precision < 0.5 and recall < 0.5:
        return "low_f1"          # both wrong
    return "other"


def categorize_per_query(base: dict, curr: dict) -> dict:
    all_queries = sorted(set(base.keys()) | set(curr.keys()))
    improved, regressed, stable_pass, stable_fail, only_in_base, only_in_curr = [], [], [], [], [], []

    for q in all_queries:
        b = base.get(q)
        c = curr.get(q)
        if b is None:
            only_in_curr.append(q)
            continue
        if c is None:
            only_in_base.append(q)
            continue
        b_score = b.get("score", 0)
        c_score = c.get("score", 0)
        if b_score == 0 and c_score == 1:
            improved.append(q)
        elif b_score == 1 and c_score == 0:
            regressed.append({"query": q, "base_status": b.get("status"), "curr_status": c.get("status"), "curr_error": c.get("error")})
        elif b_score == 1 and c_score == 1:
            stable_pass.append(q)
        else:
            stable_fail.append(q)

    return {
        "improved":     improved,
        "regressed":    regressed,
        "stable_pass":  stable_pass,
        "stable_fail":  stable_fail,
        "only_in_base": only_in_base,
        "only_in_curr": only_in_curr,
    }


def compare(baseline_id: str, current_id: str):
    base = load_run(baseline_id)
    curr = load_run(current_id)

    base_agg = extract_aggregate(base)
    curr_agg = extract_aggregate(curr)
    cats     = categorize_per_query(base, curr)

    print(f"\nRegression comparison: {baseline_id} (baseline) → {current_id} (current)\n")

    # --- Aggregate metrics table ---
    print(f"{'Metric':<32} {'Baseline':>12} {'Current':>12} {'Delta':>10}")
    print("-" * 70)

    agg_regressions = []

    def row(label, b_val, c_val, fmt, higher_is_better=True):
        if b_val is None or c_val is None:
            print(f"{label:<32} {'—':>12} {'—':>12} {'—':>10}")
            return
        delta = c_val - b_val
        pct   = (delta / abs(b_val) * 100) if b_val != 0 else 0
        sign  = "+" if delta >= 0 else ""
        flag  = ""
        if higher_is_better and pct < -AGGREGATE_REGRESSION_THRESHOLD:
            flag = " ← REGRESSION"
            agg_regressions.append(label)
        elif not higher_is_better and pct > AGGREGATE_REGRESSION_THRESHOLD:
            flag = " ← REGRESSION"
            agg_regressions.append(label)
        print(f"{label:<32} {fmt(b_val):>12} {fmt(c_val):>12} {sign}{pct:+.1f}%{flag}")

    row("Pass rate (%)",             base_agg["pass_rate"],        curr_agg["pass_rate"],        lambda v: f"{v:.1f}%")
    row("Median cost reduction (%)", base_agg["median_reduction"], curr_agg["median_reduction"], lambda v: f"{v:.2f}%")
    row("Median L2 F1",              base_agg["median_f1"],        curr_agg["median_f1"],        lambda v: f"{v:.3f}")
    row("Median elapsed (s)",        base_agg["median_elapsed"],   curr_agg["median_elapsed"],   lambda v: f"{v:.1f}s", higher_is_better=False)
    row("Median total tokens",       base_agg["median_tokens"],    curr_agg["median_tokens"],    lambda v: f"{v:,.0f}", higher_is_better=False)

    print("-" * 70)
    print(f"  Queries: {base_agg['n']} baseline / {curr_agg['n']} current")
    print(f"  Passed:  {base_agg['pass_count']}/{base_agg['n']} baseline  →  {curr_agg['pass_count']}/{curr_agg['n']} current")

    # --- Per-query categorization ---
    print(f"\nPer-query categorization  (score threshold: cost reduction ≥10%, F1 ≥0.5)\n")
    print(f"  Improved  (0→1): {len(cats['improved']):3d} queries  {cats['improved'] or ''}")
    print(f"  Regressed (1→0): {len(cats['regressed']):3d} queries")
    print(f"  Stable  ✓ (1→1): {len(cats['stable_pass']):3d} queries")
    print(f"  Stable  ✗ (0→0): {len(cats['stable_fail']):3d} queries")
    if cats["only_in_base"]:
        print(f"  Only in baseline: {cats['only_in_base']}")
    if cats["only_in_curr"]:
        print(f"  Only in current:  {cats['only_in_curr']}")

    if cats["regressed"]:
        print(f"\n  Regressed queries (details):")
        for r in cats["regressed"]:
            err = f" — {r['curr_error']}" if r.get("curr_error") else ""
            print(f"    {r['query']}: baseline=pass, current={r['curr_status']}{err}")

    # --- Fail reason breakdown for all score=0 in current run ---
    fail_buckets: dict[str, list] = {}
    for q, r in curr.items():
        if r.get("score", 0) == 0:
            reason = classify_fail_reason(r)
            fail_buckets.setdefault(reason, []).append(q)
    if fail_buckets:
        print(f"\n  Fail reason breakdown (score=0 in current run):")
        for reason in ["timeout", "api_error", "low_cost_reduction", "low_precision", "low_recall", "low_f1", "other"]:
            qs = fail_buckets.get(reason, [])
            if qs:
                label = {
                    "timeout":            "timeout          (infra)",
                    "api_error":          "api_error        (infra)",
                    "low_cost_reduction": "low_cost_reduction  (surgeon didn't help)",
                    "low_precision":      "low_precision    (over-indexing)",
                    "low_recall":         "low_recall       (under-indexing)",
                    "low_f1":             "low_f1           (both wrong)",
                    "other":              "other",
                }[reason]
                print(f"    {label}: {len(qs):2d}  {qs}")

    # --- Verdict ---
    print("\n" + "=" * 70)
    has_query_regression   = len(cats["regressed"]) > 0
    has_aggregate_regression = len(agg_regressions) > 0

    if has_query_regression or has_aggregate_regression:
        if has_query_regression:
            print(f"REGRESSION DETECTED: {len(cats['regressed'])} query/queries regressed (1→0)")
        if has_aggregate_regression:
            print(f"REGRESSION DETECTED in aggregate metrics: {', '.join(agg_regressions)}")
            print(f"(threshold: >{AGGREGATE_REGRESSION_THRESHOLD}% degradation)")
        sys.exit(1)
    else:
        print("No regression detected.")
        sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m eval.compare <baseline_run_id> <current_run_id>")
        sys.exit(1)
    compare(sys.argv[1], sys.argv[2])
