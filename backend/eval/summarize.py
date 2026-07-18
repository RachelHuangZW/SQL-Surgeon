import os
import sys
import json
import statistics
from glob import glob

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def find_run_dir(run_id: str = None) -> str:
    if run_id:
        path = os.path.join(RESULTS_DIR, run_id)
        if not os.path.isdir(path):
            print(f"Run '{run_id}' not found in {RESULTS_DIR}")
            sys.exit(1)
        return path
    # Default: latest directory by modification time
    dirs = [d for d in glob(os.path.join(RESULTS_DIR, "*")) if os.path.isdir(d)]
    if not dirs:
        print(f"No run directories found in {RESULTS_DIR}")
        sys.exit(1)
    return max(dirs, key=os.path.getmtime)


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


def iqr(values):
    if len(values) < 4:
        return None
    s = sorted(values)
    n = len(s)
    return round(s[3 * n // 4] - s[n // 4], 1)


def load_results(run_dir: str) -> list:
    files = sorted(glob(os.path.join(run_dir, "*.json")))
    results = []
    for f in files:
        if os.path.basename(f) == "summary.json":
            continue
        with open(f) as fh:
            results.append(json.load(fh))
    return results


def print_summary(results: list, run_dir: str):
    col_widths = {
        "query":     8,
        "status":    10,
        "b1":        10,
        "surg_comb": 11,
        "b2_comb":   10,
        "reduction": 10,
        "l2_f1":     8,
        "elapsed":   9,
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
        f"{'elapsed_s':>{col_widths['elapsed']}}"
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
    elapsed_list = []

    for r in results:
        query    = r.get("query", "?")
        status   = r.get("status", "?")
        b1       = r.get("b1_cost")
        s_comb   = r.get("surgeon_combined_cost")
        b2_comb  = r.get("b2_combined_cost")
        l2       = r.get("l2_combined") or {}
        f1       = l2.get("f1")
        verdict  = r.get("verdict") or "—"
        retries  = r.get("retry_count")
        elapsed  = r.get("elapsed_seconds")
        retries_str = str(retries) if retries is not None else "—"
        elapsed_str = f"{elapsed:.0f}s" if elapsed is not None else "—"

        reduction = cost_reduction(b1, s_comb)

        if status == "success":
            success += 1
        if reduction is not None:
            reductions.append(reduction)
        if f1 is not None:
            f1_scores.append(f1)
        if elapsed is not None:
            elapsed_list.append(elapsed)

        print(
            f"{query:<{col_widths['query']}}"
            f"{status:<{col_widths['status']}}"
            f"{fmt_cost(b1):>{col_widths['b1']}}"
            f"{fmt_cost(s_comb):>{col_widths['surg_comb']}}"
            f"{fmt_cost(b2_comb):>{col_widths['b2_comb']}}"
            f"{fmt_pct(reduction):>{col_widths['reduction']}}"
            f"{fmt_f1(f1):>{col_widths['l2_f1']}}"
            f"{elapsed_str:>{col_widths['elapsed']}}"
            f"{verdict:<{col_widths['verdict']}}"
            f"{retries_str:>{col_widths['retries']}}"
        )

    print(sep)

    med_r   = round(statistics.median(reductions), 1) if reductions else None
    med_f1  = round(statistics.median(f1_scores),  3) if f1_scores  else None
    iqr_r   = iqr(reductions)
    iqr_f1  = iqr(f1_scores)
    med_ela = round(statistics.median(elapsed_list), 0) if elapsed_list else None

    run_id     = results[0].get("run_id", "?")    if results else "?"
    git_commit = results[0].get("git_commit", "?") if results else "?"

    print(f"\nRun ID       : {run_id}  |  Git: {git_commit}")
    print(f"Total queries: {total}  |  Succeeded: {success}/{total}")
    print(f"Median reduction (b1→surg_combined): {fmt_pct(med_r)}  IQR: {fmt_pct(iqr_r)}")
    print(f"Median L2 F1                       : {fmt_f1(med_f1)}  IQR: {fmt_f1(iqr_f1)}")
    if med_ela is not None:
        print(f"Median elapsed per query           : {med_ela:.0f}s")


if __name__ == "__main__":
    run_id_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_dir = find_run_dir(run_id_arg)
    results = load_results(run_dir)
    if not results:
        print(f"No result files found in {run_dir}")
    else:
        run_label = os.path.basename(run_dir)
        print(f"\nSQL Surgeon — Eval Summary  [{run_label}]  ({len(results)} queries)\n")
        print_summary(results, run_dir)
