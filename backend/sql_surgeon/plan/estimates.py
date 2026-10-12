from .table import ACCURATE_Q, flatten_plan

# q-error
BAD_Q = 10
# 
SECOND_SOURCE_RATIO = 10
# 占总执行时间
TIME_SHARE = 0.10
# 太少行数不报
MIN_ROWS = 1000

def build_parent_to_children_map(rows):
    """
    遍历 flatten_plan 拍平后的 rows，构建 {parent_id: [子节点的行]} 的映射字典
    叶子节点不会出现在 key 里，查的时候用 .get(id, [])
    """
    parent_map = {}
    for row in rows:
        # 根节点没有父节点，跳过
        if row["parent_id"] is None:
            continue
        # 第一次遇到这个父节点时先建空列表，再把当前行加进去
        parent_map.setdefault(row["parent_id"], []).append(row)
    return parent_map


def find_origins(rows):
    """
    找估错的源头：自己错得厉害，而且不是从子节点继承来的错
    返回 [{"id", "q_error", "direction", "child_max_q"}]
    """
    children_map = build_parent_to_children_map(rows)
    origins = []
    for row in rows:
        # 没执行过的节点没有 q-error
        if row["q_error"] is None:
            continue
        # 错得不够厉害
        if row["q_error"] < BAD_Q:
            continue
        # 行数太少：估计和实际里取大的，再乘循环次数 = 总行数
        if max(row["plan_rows"], row["actual_rows"]) * row["loops"] < MIN_ROWS:
            continue
        
        # 子节点里最大的 q-error；叶子节点没有子节点，当作 1（完全准）
        child_qs = [c["q_error"] for c in children_map.get(row["id"], []) if c["q_error"] is not None]
        child_max_q = max(child_qs, default=1)

        # 规则 1：子节点都准 → 错是在这里第一次出现的
        # 规则 2：子节点已经错了，但这里又错得多 SECOND_SOURCE_RATIO 倍 → 第二个错误源
        if child_max_q < ACCURATE_Q or row["q_error"] >= child_max_q * SECOND_SOURCE_RATIO:
            origins.append({
                "id": row["id"],
                "q_error": row["q_error"],
                "direction": row["direction"],
                "child_max_q": child_max_q
            })
    
    return origins


def find_consequences(rows, execution_time_ms):
    """
    找估错造成的慢操作，只报耗时占总执行时间 >= TIME_SHARE 的
    返回 [{"id", "type", "time_share", ...}]
    """
    if not execution_time_ms:
        return []
    children_map = build_parent_to_children_map(rows)

    def time_share(row):
        # Actual Total Time 是每次循环的平均值，并且包含子节点的时间
        # 注意：并行计划里 loops 包含了 worker 数，这里会算多，偏向"多报"
        return row["actual_total_time"] * row["loops"] / execution_time_ms

    consequences = []
    for row in rows:
        # 后果 1：Hash 实际分批比计划多 → 少估行数，内存装不下，溢出到磁盘
        if row["op"] == "Hash" and row["hash_batch"] > row["original_hash_batch"]:
            # 溢出的代价主要算在上一层的 Hash Join 里
            join = next(r for r in rows if r["id"] == row["parent_id"])
            share = time_share(join)
            if share >= TIME_SHARE:
                consequences.append({
                    "id": row["id"],
                    "type": "hash_spill",
                    "time_share": share,
                    "planned_batches": row["original_hash_batch"],
                    "actual_batches": row["hash_batch"],
                })

        # 后果 2：Nested Loop 内侧被执行的次数远超计划
        if row["op"] == "Nested Loop":
            children = children_map.get(row["id"], [])
            if len(children) != 2:
                continue
            # 第一个子节点是外侧（Outer），第二个是内侧（Inner）
            outer, inner = children
            # 优化器预计内侧执行次数 = 外侧估计行数 × 外侧循环次数
            planned_loops = max(outer["plan_rows"] * outer["loops"], 1)
            share = time_share(inner)
            if inner["loops"] / planned_loops >= BAD_Q and share >= TIME_SHARE:
                consequences.append({
                    "id": inner["id"],
                    "type": "nested_loop_rescan",
                    "time_share": share,
                    "planned_loops": planned_loops,
                    "actual_loops": inner["loops"],
                })
    return consequences


def analyze_estimates(explain_output):
    """
    总入口：输入 DBClient.execute_explain 的结果，输出 {"origins": [...], "consequences": [...]}
    """
    rows = flatten_plan(explain_output)
    if not rows:
        return {"origins": [], "consequences": []}
    execution_time_ms = explain_output[0].get("Execution Time")
    return {
        "origins": find_origins(rows),
        "consequences": find_consequences(rows, execution_time_ms),
    }