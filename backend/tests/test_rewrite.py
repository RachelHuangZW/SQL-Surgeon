"""Tests for rewrite_comma_join. Pure string tests — no database needed.

Run from backend/:  ../.venv/bin/python tests/test_rewrite.py
"""
import glob
import os
import re
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.agent.nodes import rewrite_comma_join, _split_and_conditions  # noqa: E402

JOB_DIR = os.path.expanduser("~/join-order-benchmark")
fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(";").strip()


def conditions_of(sql: str) -> list:
    """All predicates of a query: WHERE conjuncts plus every ON condition, normalized and sorted."""
    s = norm(sql)
    conds = [norm(c) for c in re.findall(r"\bON (.+?)(?= JOIN | WHERE |$)", s, re.IGNORECASE)]
    conds = [p for c in conds for p in _split_and_conditions(c)]
    m = re.search(r"\bWHERE\b(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        conds += _split_and_conditions(m.group(1))
    return sorted(norm(c) for c in conds)


print("=== 1. 三表环形连接：条件不能丢 ===")
sql = "SELECT * FROM a, b, c WHERE a.x = b.x AND b.y = c.y AND a.z = c.z"
out = rewrite_comma_join(sql)
check(f"三个条件都在 → {norm(out)}", all(c in out for c in ["a.x = b.x", "b.y = c.y", "a.z = c.z"]))
check("改写前后条件集合相同", conditions_of(sql) == conditions_of(out))

print("\n=== 2. JOB 全部查询：改写前后条件集合完全相同 ===")
files = sorted(glob.glob(os.path.join(JOB_DIR, "[0-9]*.sql")))
if not files:
    print(f"  SKIP 找不到 {JOB_DIR}")
else:
    bad = []
    for f in files:
        src = open(f).read()
        out = rewrite_comma_join(src)
        if out != src and conditions_of(src) != conditions_of(out):
            bad.append(os.path.basename(f))
    check(f"{len(files)} 条查询，条件有出入的: {bad or '无'}", not bad)

print("\n=== 3. 确定性：不同哈希种子下输出一致 ===")
probe = (
    "import sys; sys.path.insert(0, %r); from sql_surgeon.agent.nodes import rewrite_comma_join; "
    "print(rewrite_comma_join(open(%r).read()))"
) % (BACKEND, os.path.join(JOB_DIR, "1a.sql") if files else "/dev/null")
outputs = {
    subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=BACKEND,
                   env={**os.environ, "PYTHONHASHSEED": seed}).stdout
    for seed in ["0", "1", "2", "42", "1234"]
}
check(f"5 个种子 → {len(outputs)} 种输出", len(outputs) == 1)

print(f"\n{fails} 个失败")
sys.exit(1 if fails else 0)
