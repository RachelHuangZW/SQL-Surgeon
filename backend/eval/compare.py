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

REGRESSION_THRESHOLD = 5.0  # flag if median metric degrades by more than 5%


def load_run(run_id: str) -> list:
    run_dir = os.path.join(RESULTS_DIR, run_id)
    if not os.path.isdir(run_dir):
        print(f"ERROR: run '{run_id}' not found in {RESULTS_DIR}")
        sys.exit(1)
    results = []
    for f in sorted(glob(os.path.join(run_dir, "*.json"))):
        if os.path.basename(f) == "summary.json":
            continue
        with open(f) as fh:
            results.append(json.load(fh))
    return results


def extract_metrics(results: list) -> dict:
    reductions, f1_scores = [], []
    for r in results:
        b1     = r.get("b1_cost")
        s_comb = r.get("surgeon_combined_cost")
        f1     = (r.get("l2_combined") or {}).get("f1")
        if b1 and s_comb and b1 > 0:
            reductions.append((b1 - s_comb) / b1 * 100)
        if f1 is not None:
            f1_scores.append(f1)
    return {
        "n":              len(results),
        "success":        sum(1 for r in results if r.get("status") == "success"),
        "median_reduction": round(statistics.median(reductions), 2) if reductions else None,
        "median_f1":        round(statistics.median(f1_scores),  3) if f1_scores  else None,
    }


def compare(baseline_id: str, current_id: str):
    base_results = load_run(baseline_id)
    curr_results = load_run(current_id)

    base = extract_metrics(base_results)
    curr = extract_metrics(curr_results)

    print(f"\nRegression comparison: {baseline_id} (baseline) → {current_id} (current)\n")
    print(f"{'Metric':<30} {'Baseline':>12} {'Current':>12} {'Delta':>10}")
    print("-" * 68)

    regressions = []

    def row(label, b_val, c_val, fmt, higher_is_better=True):
        if b_val is None or c_val is None:
            print(f"{label:<30} {'—':>12} {'—':>12} {'—':>10}")
            return
        delta = c_val - b_val
        pct   = (delta / abs(b_val) * 100) if b_val != 0 else 0
        sign  = "+" if delta >= 0 else ""
        flag  = ""
        if higher_is_better and pct < -REGRESSION_THRESHOLD:
            flag = " ← REGRESSION"
            regressions.append(label)
        elif not higher_is_better and pct > REGRESSION_THRESHOLD:
            flag = " ← REGRESSION"
            regressions.append(label)
        print(f"{label:<30} {fmt(b_val):>12} {fmt(c_val):>12} {sign}{pct:+.1f}%{flag}")

    row("Median cost reduction (%)", base["median_reduction"], curr["median_reduction"],
        lambda v: f"{v:.2f}%", higher_is_better=True)
    row("Median L2 F1",             base["median_f1"],        curr["median_f1"],
        lambda v: f"{v:.3f}",  higher_is_better=True)
    row("Success rate",
        base["success"] / base["n"] * 100 if base["n"] else None,
        curr["success"] / curr["n"] * 100 if curr["n"] else None,
        lambda v: f"{v:.1f}%", higher_is_better=True)

    print("-" * 68)
    if regressions:
        print(f"\nREGRESSION DETECTED in: {', '.join(regressions)}")
        print(f"(threshold: >{REGRESSION_THRESHOLD}% degradation)")
        sys.exit(1)
    else:
        print("\nNo regression detected.")
        sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m eval.compare <baseline_run_id> <current_run_id>")
        sys.exit(1)
    compare(sys.argv[1], sys.argv[2])
