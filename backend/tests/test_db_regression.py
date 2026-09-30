import os, sys, time
sys.path.insert(0, "/Users/rachel/SQL-Surgeon/backend")
from dotenv import load_dotenv; load_dotenv("/Users/rachel/SQL-Surgeon/.env")
import psycopg2
from db import client as C
from db.client import DBClient, UnsafeSQLError, QueryTimeoutError

ADMIN, RO = os.environ["DATABASE_URL"], os.environ["SURGEON_READONLY_DATABASE_URL"]
fails = 0
def check(name, cond):
    global fails; print(("  PASS " if cond else "  FAIL ") + name); fails += 0 if cond else 1
def raises(fn, exc):
    try: fn(); return None
    except exc as e: return e
    except Exception as e: print("     unexpected:", type(e).__name__, e); return None
def admin_one(sql):
    c = psycopg2.connect(ADMIN); c.autocommit = True
    try:
        cur = c.cursor(); cur.execute(sql); return cur.fetchone()
    finally: c.close()

def suite(label, ro_dsn):
    print(f"\n=== {label} ===")
    C._pools.clear()
    if ro_dsn: os.environ["SURGEON_READONLY_DATABASE_URL"] = ro_dsn
    else: os.environ.pop("SURGEON_READONLY_DATABASE_URL", None)
    os.environ["SURGEON_STATEMENT_TIMEOUT_MS"] = "800"
    db = DBClient(ADMIN)
    pool_ = lambda: C._pools[db.readonly_dsn]

    with db._readonly_transaction() as conn:
        cur = conn.cursor(); cur.execute("SELECT current_setting('transaction_read_only'), current_setting('statement_timeout'), current_setting('lock_timeout')")
        s = cur.fetchone()
    check(f"事务内设置 = {s}", s == ("on", "800ms", "2s"))

    plan = db.execute_explain(open(os.path.expanduser("~/join-order-benchmark/1a.sql")).read())
    check("JOB 1a 返回执行计划", isinstance(plan, list) and "Plan" in plan[0])
    check("CTE DELETE 被挡", raises(lambda: db.execute_explain("WITH d AS (DELETE FROM kind_type RETURNING *) SELECT count(*) FROM d"), UnsafeSQLError) is not None)
    check("UPDATE 被挡", raises(lambda: db.execute_explain("UPDATE kind_type SET kind='x'"), UnsafeSQLError) is not None)
    check("CREATE TABLE AS 被挡", raises(lambda: db.execute_explain("CREATE TABLE evil AS SELECT 1"), UnsafeSQLError) is not None)
    check("多语句逃逸被挡", raises(lambda: db.execute_explain("SELECT 1; COMMIT; DELETE FROM kind_type"), UnsafeSQLError) is not None)
    check("关闭只读被挡", raises(lambda: db.execute_explain("SELECT set_config('transaction_read_only','off',true)"), psycopg2.Error) is not None)

    t = time.time()
    e = raises(lambda: db.execute_explain("SELECT pg_sleep(10)"), QueryTimeoutError)
    check(f"pg_sleep(10) 在 {time.time()-t:.2f}s 超时", e is not None and time.time() - t < 2)

    db.execute_explain("SELECT pg_advisory_lock(4242)")
    check("advisory lock 归还时已释放", admin_one("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=4242")[0] == 0)
    check("kind_type 数据完好（7 行）", admin_one("SELECT count(*) FROM kind_type")[0] == 7)
    check("没有建出 evil 表", admin_one("SELECT to_regclass('public.evil') IS NULL")[0])

    for _ in range(30):
        raises(lambda: db.execute_explain("SELECT * FROM does_not_exist"), psycopg2.Error)
    check(f"30 次报错后没有泄漏（借出={len(pool_()._used)}）", len(pool_()._used) == 0)
    try:
        with db._readonly_transaction(): raise ValueError("x")
    except ValueError: pass
    check(f"with 里抛异常后连接已归还（借出={len(pool_()._used)}）", len(pool_()._used) == 0)

    held = [pool_().getconn() for _ in range(db.config.pool_max)]
    e = raises(lambda: db.execute_explain("SELECT 1"), RuntimeError)
    check(f"池子满时友好报错: {e}", e is not None and "max" in str(e))
    for c in held: pool_().putconn(c)

    admin_one("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name='sql_surgeon_readonly'")
    raises(lambda: db.execute_explain("SELECT 1"), psycopg2.Error)  # 失效连接可能失败一次
    check(f"后端被杀后能恢复（借出={len(pool_()._used)}）", isinstance(db.execute_explain("SELECT 1"), list) and len(pool_()._used) == 0)

suite("只读用户 DSN", RO)
suite("回退：超级用户 DSN（只靠事务层保护）", None)
print(f"\n{fails} 个失败")
