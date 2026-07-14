import os
import json
from glob import glob

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def cost_reduction(b1, surgeon):
    if b1 and surgeon and b1 > 0:
        return round((b1 - surgeon) / b1 * 100, 1)
    return None


def fmt_cost(val):
    if val is None:
        return "—"
    return f"{val:,.0f}"


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:.1f}%"


def fmt_f1(val):
    if val is None:
        return "—"
    return f"{val:.3f}"


def load_results() -> list:
    files = sorted(glob(os.path.join(RESULTS_DIR, "*.json")))
    results = []
    for f in files:
        name = os.path.basename(f)
        if name.startswith("summary_"):
            continue
        with open(f) as fh:
            results.append(json.load(fh))
    return results


def print_summary(results: list):
    col_widths = {
        "query":     8,
        "status":    9,
        "b1":        10,
        "surg_comb": 11,
        "b2_comb":   10,
        "reduction": 10,
        "l2_f1":     8,
        "verdict":   8,
        "retries":   7,
    }

    header = (
        f"{'query':<{col_widths['query']}}"
        f"{'status':<{col_widths['status']}}"
        f"{'b1_cost':>{col_widths['b1']}}"
        f"{'surg_comb':>{col_widths['surg_comb']}}"
        f"{'b2_comb':>{col_widths['b2_comb']}}"
        f"{'reduction':>{col_widths['reduction']}}"
        f"{'l2_f1':>{col_widths['l2_f1']}}"
        f"{'verdict':<{col_widths['verdict']}}"
        f"{'retries':>{col_widths['retries']}}"
    )
    sep = "-" * len(header)

    print(sep)
    print(header)
    print(sep)

    total = len(results)
    success = 0
    reductions = []
    f1_scores = []

    for r in results:
        query   = r.get("query", "?")
        status  = r.get("status", "?")
        b1      = r.get("b1_cost")
        s_comb  = r.get("surgeon_combined_cost")
        b2_comb = r.get("b2_combined_cost")
        l2      = r.get("l2_combined") or {}
        f1      = l2.get("f1")
        verdict = r.get("verdict") or "—"
        retries = r.get("retry_count")
        retries_str = str(retries) if retries is not None else "—"

        reduction = cost_reduction(b1, s_comb)

        if status == "success":
            success += 1
        if reduction is not None:
            reductions.append(reduction)
        if f1 is not None:
            f1_scores.append(f1)

        print(
            f"{query:<{col_widths['query']}}"
            f"{status:<{col_widths['status']}}"
            f"{fmt_cost(b1):>{col_widths['b1']}}"
            f"{fmt_cost(s_comb):>{col_widths['surg_comb']}}"
            f"{fmt_cost(b2_comb):>{col_widths['b2_comb']}}"
            f"{fmt_pct(reduction):>{col_widths['reduction']}}"
            f"{fmt_f1(f1):>{col_widths['l2_f1']}}"
            f"{verdict:<{col_widths['verdict']}}"
            f"{retries_str:>{col_widths['retries']}}"
        )

    print(sep)

    avg_reduction = round(sum(reductions) / len(reductions), 1) if reductions else None
    avg_f1        = round(sum(f1_scores)  / len(f1_scores),  3) if f1_scores  else None

    print(f"\nTotal queries : {total}")
    print(f"Succeeded     : {success}/{total}")
    print(f"Avg reduction : {fmt_pct(avg_reduction)}  (b1 → surgeon_combined)")
    print(f"Avg L2 F1     : {fmt_f1(avg_f1)}")


if __name__ == "__main__":
    results = load_results()
    if not results:
        print(f"No result files found in {RESULTS_DIR}")
    else:
        print(f"\nSQL Surgeon — Eval Summary ({len(results)} queries)\n")
        print_summary(results)
