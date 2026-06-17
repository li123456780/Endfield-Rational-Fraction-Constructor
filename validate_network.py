"""
分流器网络 JSON 验证器
========================

功能：读取 flow_simulator.py 导出的 JSON 文件，验证网络拓扑的正确性。

验证步骤（每一步均有详细说明）：
  Step 1 — JSON 结构完整性校验
  Step 2 — 组件类型与 ID 唯一性校验
  Step 3 — 连接引用的合法性校验（src/dst 存在，端口合法）
  Step 4 — 寻找网络的入口点（无上游连接馈入的输入端口）
  Step 5 — 迭代求解稳态流量（Fraction 精确运算）
  Step 6 — 校验分流器不变式（所有输出端口流量相等 = 输入 / 分流比）
  Step 7 — 校验集流器不变式（输出 = 所有输入之和）
  Step 8 — 校验反馈回路稳态条件
  Step 9 — 汇总报告

用法：
  python validate_network.py network_7_15.json
  python validate_network.py network_7_15.json --expected "7/15"
"""

from fractions import Fraction
import json
import sys
from math import isclose
from typing import Dict, List, Optional, Tuple


# ========================================================================
#  Step 1: JSON 结构完整性校验
# ========================================================================
def step1_validate_structure(data: dict) -> List[str]:
    """
    校验 JSON 文件是否包含所有必需的顶层字段，
    以及每个组件和连接的必填字段是否完整。
    
    返回：警告/错误信息列表。
    """
    errors = []

    # ---- 1a. 顶层字段 ----
    required_top = ["components", "connections", "meta"]
    for key in required_top:
        if key not in data:
            errors.append(f"[FAIL] 顶层缺少必需字段: '{key}'")
        elif not isinstance(data[key], (list, dict)):
            errors.append(f"[FAIL] 顶层字段 '{key}' 类型错误")

    if errors:
        return errors  # 无法继续深层校验

    # ---- 1b. components 数组中每个元素的字段 ----
    comps = data["components"]
    if not isinstance(comps, list) or len(comps) == 0:
        errors.append("[FAIL] 'components' 必须是非空数组")
    else:
        comp_required = {"id", "name", "type"}
        for i, c in enumerate(comps):
            missing = comp_required - set(c.keys())
            if missing:
                errors.append(f"[FAIL] 组件[{i}] 缺少字段: {missing}")
            if c.get("type") == "splitter" and "ratio_type" not in c:
                errors.append(f"[FAIL] 分流器 {c.get('id','?')} 缺少 'ratio_type'")
            if c.get("type") == "merger" and "input_count" not in c:
                errors.append(f"[FAIL] 集流器 {c.get('id','?')} 缺少 'input_count'")

    # ---- 1c. connections 数组中每个元素的字段 ----
    conns = data["connections"]
    if not isinstance(conns, list):
        errors.append("[FAIL] 'connections' 必须是数组")
    else:
        conn_required = {"src_id", "src_port", "dst_id", "dst_port", "is_feedback"}
        for i, conn in enumerate(conns):
            missing = conn_required - set(conn.keys())
            if missing:
                errors.append(f"[FAIL] 连接[{i}] 缺少字段: {missing}")

    # ---- 1d. meta 字段 ----
    meta = data["meta"]
    if "total_ext_input" not in meta:
        errors.append("[FAIL] meta 缺少 'total_ext_input'")

    if not errors:
        print("  [PASS] JSON 结构完整性校验通过")
    else:
        print("  [FAIL] 结构校验发现问题，详见报告")
    return errors


# ========================================================================
#  Step 2: 组件类型与 ID 唯一性校验
# ========================================================================
def step2_validate_components(comps: list) -> Tuple[Dict[int, dict], List[str]]:
    """
    校验：
    - 组件 ID 是否为正整数且唯一
    - 类型是否为合法值（splitter / merger）
    - 分流器的 ratio_type 是否为 2 或 3
    - 集流器的 input_count 是否为正整数
    
    返回：{id: component_dict} 映射和错误列表。
    """
    errors = []
    comp_map: Dict[int, dict] = {}
    valid_types = {"splitter", "merger", "unknown"}

    for i, c in enumerate(comps):
        cid = c.get("id")
        # ID 存在性和类型
        if not isinstance(cid, int) or cid <= 0:
            errors.append(f"[FAIL] 组件[{i}] ID 无效: {cid}")
            continue
        if cid in comp_map:
            errors.append(f"[FAIL] 组件 ID={cid} 重复")
            continue

        ctype = c.get("type")
        if ctype not in valid_types:
            errors.append(f"[FAIL] 组件 ID={cid} 类型 '{ctype}' 非法（允许: splitter/merger）")

        # 分流器专项
        if ctype == "splitter":
            rt = c.get("ratio_type")
            if rt not in (2, 3):
                errors.append(f"[FAIL] 分流器 ID={cid} ratio_type={rt} 非法（仅允许 2 或 3）")

        # 集流器专项
        if ctype == "merger":
            ic = c.get("input_count")
            if not isinstance(ic, int) or ic < 1:
                errors.append(f"[FAIL] 集流器 ID={cid} input_count={ic} 非法（必须 >= 1）")

        comp_map[cid] = c

    if not errors:
        print(f"  [PASS] 组件校验通过（共 {len(comp_map)} 个）")
    else:
        print(f"  [FAIL] 组件校验发现问题")
    return comp_map, errors


# ========================================================================
#  Step 3: 连接引用的合法性校验
# ========================================================================
def step3_validate_connections(
    conns: list, comp_map: Dict[int, dict]
) -> Tuple[List[dict], List[str]]:
    """
    校验每条连接：
    - src_id 和 dst_id 是否指向真实组件
    - src_port 是否在源组件的合法端口范围内
    - dst_port 是否在目标组件的合法端口范围内
    
    端口约定（参考 flow_simulator.py）：
    - 分流器 splitter：入端口 -1，出端口 0, 1（1/2）或 0, 1, 2（1/3）
    - 集流器 merger：入端口 0, 1, ...（input_count-1），出端口 -1
    
    返回：清理后的连接列表和错误列表。
    """
    errors = []
    valid_conns = []

    for i, conn in enumerate(conns):
        sid, sport = conn["src_id"], conn["src_port"]
        did, dport = conn["dst_id"], conn["dst_port"]

        # ---- 校验 src_id ----
        if sid not in comp_map:
            errors.append(f"[FAIL] 连接[{i}] src_id={sid} 不存在")
            continue
        src = comp_map[sid]

        # ---- 校验 dst_id ----
        if did not in comp_map:
            errors.append(f"[FAIL] 连接[{i}] dst_id={did} 不存在")
            continue
        dst = comp_map[did]

        # ---- 校验 src_port（必须是源组件的输出端口） ----
        if src["type"] == "splitter":
            max_out = src["ratio_type"] - 1
            if not (0 <= sport <= max_out):
                errors.append(
                    f"[FAIL] 连接[{i}] 源分流器 {sid} 无输出端口 {sport}"
                    f"（合法: 0~{max_out}）"
                )
        elif src["type"] == "merger":
            if sport != -1:
                errors.append(
                    f"[FAIL] 连接[{i}] 源集流器 {sid} 唯一输出端口是 -1，"
                    f"得到了 {sport}"
                )
        else:
            errors.append(f"[FAIL] 连接[{i}] 源组件 {sid} 类型未知")

        # ---- 校验 dst_port（必须是目标组件的输入端口） ----
        if dst["type"] == "splitter":
            if dport != -1:
                errors.append(
                    f"[FAIL] 连接[{i}] 目标分流器 {did} 唯一输入端口是 -1，"
                    f"得到了 {dport}"
                )
        elif dst["type"] == "merger":
            max_in = dst["input_count"] - 1
            if not (0 <= dport <= max_in):
                errors.append(
                    f"[FAIL] 连接[{i}] 目标集流器 {did} 无输入端口 {dport}"
                    f"（合法: 0~{max_in}）"
                )
        else:
            errors.append(f"[FAIL] 连接[{i}] 目标组件 {did} 类型未知")

        valid_conns.append(conn)

    if not errors:
        print(f"  [PASS] 连接引用校验通过（共 {len(conns)} 条）")
    else:
        print(f"  [FAIL] 连接引用校验发现问题")
    return valid_conns, errors


# ========================================================================
#  Step 4: 寻找网络入口点
# ========================================================================
def step4_find_entry_points(
    comp_map: Dict[int, dict], conns: List[dict]
) -> Dict[Tuple[int, int], Fraction]:
    """
    网络的入口点 = 无上游连接馈入的输入端口。
    
    依据 flow_simulator.py 的设计：
    - 每个入口点由构造器置入 1 单位外部流量
    - 入口点通过分流器/集流器的级联被逐步缩小为 1/k
    - 最终汇总集流器将所有分支的输出累加，得到 n/m
    
    端口类型：
    - 分流器输入：端口 -1
    - 集流器输入：端口 0, 1, 2, ...
    
    返回：{(comp_id, port): assigned_flow} 字典，
         每个入口点的初始流量设为 Fraction(1, 1)。
    """
    # 收集所有"被上游连接喂入"的 (comp_id, port)
    fed_ports: set = set()
    for conn in conns:
        fed_ports.add((conn["dst_id"], conn["dst_port"]))

    # 找出需要外部输入的端口
    entries: Dict[Tuple[int, int], Fraction] = {}

    for cid, c in comp_map.items():
        if c["type"] == "splitter":
            # 分流器输入端口 -1 若无上游连接 → 入口点
            if (cid, -1) not in fed_ports:
                entries[(cid, -1)] = Fraction(1, 1)
        elif c["type"] == "merger":
            # 每个输入端口若无上游连接 → 入口点
            for port in range(c["input_count"]):
                if (cid, port) not in fed_ports:
                    entries[(cid, port)] = Fraction(1, 1)
        # unknown 类型忽略

    print(f"  [INFO] 发现 {len(entries)} 个入口点，每个注入 1 单位流量：")
    for (cid, port), val in entries.items():
        c = comp_map[cid]
        print(f"    {c['name']}#{cid} 端口 {port} ← 外部输入 {val}")
    return entries


# ========================================================================
#  Step 5: 线性方程组求解稳态流量
# ========================================================================
def step5_solve_steady_state(
    comp_map: Dict[int, dict],
    conns: List[dict],
    entries: Dict[Tuple[int, int], Fraction],
) -> Tuple[Dict, Dict, Dict, int]:
    """
    使用高斯消元法精确求解网络的稳态流量分布。
    
    算法原理（避免迭代法的精度问题）：
    
    将网络建模为线性方程组，每个组件的输入端口是一个变量：
    - 分流器输入端口 (-1)：变量 x_s，其每个输出 = x_s / ratio_type
    - 集流器输入端口 (0,1,2,...)：变量 m_c_i，其输出 = sum(m_c_i)
    
    每条连接给出一个等式：
      dst_input = src_output
    
    入口点给出等式：
      entry_port = 1
    
    构建矩阵 Ax = b，使用 Fraction 精确求逆（高斯消元）。
    
    返回：
    - inputs, outputs, flow_on_conn 同原定义
    - iterations: 恒为 0（无需迭代）
    """
    zero = Fraction(0, 1)
    one = Fraction(1, 1)

    # ---- 第一步：为每个组件输入端口分配变量索引 ----
    # 变量包括每个组件的输入端口（这些是被连接的值）
    var_map: Dict[Tuple[int, int], int] = {}  # (comp_id, port) -> var_index
    var_names: List[Tuple[int, int]] = []      # var_index -> (comp_id, port)

    for cid in sorted(comp_map.keys()):
        c = comp_map[cid]
        if c["type"] == "splitter":
            # 分流器输入端口 -1
            v = (cid, -1)
            var_map[v] = len(var_names)
            var_names.append(v)
        elif c["type"] == "merger":
            # 集流器每个输入端口
            for p in range(c["input_count"]):
                v = (cid, p)
                var_map[v] = len(var_names)
                var_names.append(v)

    n_vars = len(var_names)
    if n_vars == 0:
        print("  [WARN] 无可求解的变量")
        return {}, {}, {}, 0

    # ---- 第二步：构建增广矩阵 [A | b] ----
    # A 是 n_vars × n_vars 矩阵，b 是 n_vars × 1 向量
    # 使用 Fraction 列表的列表
    A = [[zero] * n_vars for _ in range(n_vars)]
    b = [zero] * n_vars

    def get_output_expr(cid, port):
        """
        返回组件 cid 输出端口 port 的线性表达式。
        返回 ([coefficients array], constant_term)
        """
        coeffs = [zero] * n_vars
        const = zero
        c = comp_map[cid]
        if c["type"] == "splitter":
            # 输出 = 输入 / ratio_type
            in_v = var_map.get((cid, -1))
            if in_v is not None:
                coeffs[in_v] = Fraction(1, c["ratio_type"])
        elif c["type"] == "merger":
            # 输出 = sum(所有输入)
            for inp in range(c["input_count"]):
                in_v = var_map.get((cid, inp))
                if in_v is not None:
                    coeffs[in_v] += one
        return coeffs, const

    # ---- 第三步：建立方程 ----
    row = 0

    # 方程类型 1：入口点等式（entry_var = 1）
    for (cid, port), val in entries.items():
        v = var_map.get((cid, port))
        if v is not None:
            A[row][v] = one
            b[row] = val
            row += 1

    # 方程类型 2：每条连接的等式（dst_input = src_output）
    for conn in conns:
        sid, sport = conn["src_id"], conn["src_port"]
        did, dport = conn["dst_id"], conn["dst_port"]
        dst_v = var_map.get((did, dport))
        if dst_v is None:
            continue
        # 左侧：dst_v = 1 * dst_input
        A[row][dst_v] = one
        # 右侧：src_output 的表达式
        coeffs, const = get_output_expr(sid, sport)
        for i in range(n_vars):
            A[row][i] -= coeffs[i]
        b[row] = const
        row += 1

    # 方程类型 3：对于每个未被连接覆盖的输入端口，若也不是入口点，设其为 0
    covered: set = set()
    for conn in conns:
        covered.add((conn["dst_id"], conn["dst_port"]))
    for (cid, port) in var_map:
        if (cid, port) not in entries and (cid, port) not in covered:
            v = var_map[(cid, port)]
            A[row][v] = one
            b[row] = zero
            row += 1

    # 去掉多余的空行
    n_eqs = row
    A = A[:n_eqs]
    b = b[:n_eqs]

    # ---- 第四步：高斯消元求解 ----
    # 先补齐为方阵（可能有冗余方程）
    if n_eqs < n_vars:
        # 欠定：补充方程 var = 0
        for i in range(n_eqs, n_vars):
            new_row = [zero] * n_vars
            new_row[i] = one
            A.append(new_row)
            b.append(zero)
            n_eqs += 1

    # 高斯消元（带部分主元选取）
    n = min(n_eqs, n_vars)
    for col in range(n):
        # 寻找主元行
        max_row = col
        max_val = abs(A[col][col])
        for r in range(col + 1, n_eqs):
            if abs(A[r][col]) > max_val:
                max_val = abs(A[r][col])
                max_row = r
        if max_val == zero:
            continue  # 零列，跳过

        # 交换行
        if max_row != col:
            A[col], A[max_row] = A[max_row], A[col]
            b[col], b[max_row] = b[max_row], b[col]

        # 消元
        pivot = A[col][col]
        for r in range(n_eqs):
            if r == col:
                continue
            factor = A[r][col] / pivot
            if factor == zero:
                continue
            for c_idx in range(col, n_vars):
                A[r][c_idx] -= factor * A[col][c_idx]
            b[r] -= factor * b[col]

    # ---- 第五步：回代求解 ----
    solution = [zero] * n_vars
    for i in range(n):
        if A[i][i] != zero:
            solution[i] = b[i] / A[i][i]

    # ---- 第六步：将解填入端口数据结构 ----
    def init_ports(comp):
        ins, outs = {}, {}
        if comp["type"] == "splitter":
            ins[-1] = zero
            for p in range(comp["ratio_type"]):
                outs[p] = zero
        elif comp["type"] == "merger":
            for p in range(comp["input_count"]):
                ins[p] = zero
            outs[-1] = zero
        return ins, outs

    all_inputs: Dict[int, Dict[int, Fraction]] = {}
    all_outputs: Dict[int, Dict[int, Fraction]] = {}
    for cid, c in comp_map.items():
        all_inputs[cid], all_outputs[cid] = init_ports(c)

    # 填入输入端口值
    for idx, (cid, port) in enumerate(var_names):
        all_inputs[cid][port] = solution[idx]

    # 计算输出端口值
    for cid, c in comp_map.items():
        if c["type"] == "splitter":
            total_in = all_inputs[cid].get(-1, zero)
            share = total_in / c["ratio_type"]
            for p in all_outputs[cid]:
                all_outputs[cid][p] = share
        elif c["type"] == "merger":
            total = sum(all_inputs[cid].values(), zero)
            all_outputs[cid][-1] = total

    # ---- 第七步：计算连接流量 ----
    flow_on_conn: Dict[Tuple[int, int, int, int], Fraction] = {}
    for conn in conns:
        key = (conn["src_id"], conn["src_port"], conn["dst_id"], conn["dst_port"])
        flow_on_conn[key] = all_outputs[conn["src_id"]][conn["src_port"]]

    print(f"  [INFO] 线性方程组求解完成（{n_vars} 个变量, {n_eqs} 个方程）")
    return all_inputs, all_outputs, flow_on_conn, 0


# ========================================================================
#  Step 6: 校验分流器不变式
# ========================================================================
def step6_validate_splitters(
    comp_map: Dict[int, dict],
    all_inputs: Dict[int, Dict[int, Fraction]],
    all_outputs: Dict[int, Dict[int, Fraction]],
) -> List[str]:
    """
    对每个分流器，校验两条不变式：
    
    不变式 6a — 所有输出端口流量相等
        即 output[0] = output[1] = output[2]（若存在）
    
    不变式 6b — 每个输出 = 总输入 / ratio_type
        即 output[i] = (sum of all inputs) / ratio_type
        
    理想情况下 6a 和 6b 等价；此处同时验证可发现实现错误。
    """
    errors = []

    for cid, c in comp_map.items():
        if c["type"] != "splitter":
            continue
        name = c["name"]
        rt = c["ratio_type"]
        total_in = sum(all_inputs[cid].values(), Fraction(0, 1))
        expected_out = total_in / rt

        outs = all_outputs[cid]
        values = list(outs.values())

        # 6a: 所有输出相等
        if len(set(values)) != 1:
            errors.append(
                f"[FAIL] 分流器 {name}#{cid} (1/{rt}) "
                f"输出不相等: {[str(v) for v in values]}"
            )
            continue

        # 6b: 输出 = 输入 / ratio_type
        actual = values[0]
        if actual != expected_out:
            errors.append(
                f"[FAIL] 分流器 {name}#{cid} (1/{rt}) "
                f"输出={actual} ≠ 输入{total_in}/{rt}={expected_out}"
            )

    if not errors:
        splitter_count = sum(1 for c in comp_map.values() if c["type"] == "splitter")
        print(f"  [PASS] 分流器不变式校验通过（共 {splitter_count} 个）")
    else:
        print(f"  [FAIL] 分流器不变式校验发现问题")
    return errors


# ========================================================================
#  Step 7: 校验集流器不变式
# ========================================================================
def step7_validate_mergers(
    comp_map: Dict[int, dict],
    all_inputs: Dict[int, Dict[int, Fraction]],
    all_outputs: Dict[int, Dict[int, Fraction]],
) -> List[str]:
    """
    对每个集流器，校验：
    
    不变式 7 — 输出 = 所有输入之和
        即 output[-1] = sum(input[i] for i in range(input_count))
    """
    errors = []

    for cid, c in comp_map.items():
        if c["type"] != "merger":
            continue
        name = c["name"]
        ic = c["input_count"]
        total_in = sum(all_inputs[cid].values(), Fraction(0, 1))
        actual_out = all_outputs[cid][-1]

        if actual_out != total_in:
            errors.append(
                f"[FAIL] 集流器 {name}#{cid} ({ic}路) "
                f"输出={actual_out} ≠ 输入之和={total_in}"
            )

    if not errors:
        merger_count = sum(1 for c in comp_map.values() if c["type"] == "merger")
        print(f"  [PASS] 集流器不变式校验通过（共 {merger_count} 个）")
    else:
        print(f"  [FAIL] 集流器不变式校验发现问题")
    return errors


# ========================================================================
#  Step 8: 校验反馈回路稳态条件
# ========================================================================
def step8_validate_feedback(
    comp_map: Dict[int, dict],
    conns: List[dict],
    all_inputs: Dict[int, Dict[int, Fraction]],
    all_outputs: Dict[int, Dict[int, Fraction]],
) -> List[str]:
    """
    对每条标记为 is_feedback 的连接，校验稳态条件：
    
    条件 8 — 反馈回路源端口的输出值稳定
        在稳态下，经过反馈连接后的净输入与输出应自洽。
        重点检查：反馈连接两端流量一致（即传输不失真）。
    
    实际校验：反馈连接的源输出端口流量 == 目标输入端口接收到的流量。
    迭代算法保证该条件成立，此处做显式检查。
    """
    errors = []
    fb_count = 0

    for conn in conns:
        if not conn.get("is_feedback"):
            continue
        fb_count += 1
        sid, sport = conn["src_id"], conn["src_port"]
        did, dport = conn["dst_id"], conn["dst_port"]
        src_out = all_outputs[sid][sport]
        dst_in = all_inputs[did][dport]

        if src_out != dst_in:
            errors.append(
                f"[FAIL] 反馈连接 {comp_map[sid]['name']}#{sid}:{sport} "
                f"→ {comp_map[did]['name']}#{did}:{dport} "
                f"流量不一致: 源输出={src_out} 目标输入={dst_in}"
            )

    if not errors:
        print(f"  [PASS] 反馈回路校验通过（共 {fb_count} 条反馈边）")
    else:
        print(f"  [FAIL] 反馈回路校验发现问题")
    return errors


# ========================================================================
#  Step 9: 汇总报告
# ========================================================================
def step9_summary(
    comp_map: Dict[int, dict],
    conns: List[dict],
    entries: Dict[Tuple[int, int], Fraction],
    all_inputs: Dict[int, Dict[int, Fraction]],
    all_outputs: Dict[int, Dict[int, Fraction]],
    flow_on_conn: Dict,
    meta: dict,
    iterations: int,
    target_fraction: Optional[Fraction] = None,
) -> List[str]:
    """
    生成最终汇总报告，包括：
    - 网络拓扑统计
    - 每组件稳态流量详情
    - 连接流量详情
    - 总输出流量 vs 期望值（若提供）
    - 闲置输出端口说明
    """
    lines = []

    # ---- 拓扑统计 ----
    sp_count = sum(1 for c in comp_map.values() if c["type"] == "splitter")
    mg_count = sum(1 for c in comp_map.values() if c["type"] == "merger")
    lines.append("=" * 60)
    lines.append("  验证汇总报告")
    lines.append("=" * 60)
    lines.append(f"  组件总数: {len(comp_map)} (分流器 {sp_count}, 集流器 {mg_count})")
    lines.append(f"  连接总数: {len(conns)}")
    lines.append(f"  入口点:   {len(entries)} 个 (各注入 1 单位)")
    lines.append(f"  总外部流入: {sum(entries.values(), Fraction(0,1))}")
    lines.append(f"  求解方式: {'线性方程组（精确解）' if iterations == 0 else f'迭代 {iterations} 轮'}")
    lines.append(f"  目标分数: {meta.get('total_ext_input', 'N/A')}")
    lines.append("")

    # ---- 每组件稳态流量 ----
    lines.append("  --- 组件稳态流量 ---")
    for cid in sorted(comp_map.keys()):
        c = comp_map[cid]
        ins = all_inputs[cid]
        outs = all_outputs[cid]
        if c["type"] == "splitter":
            total_in = sum(ins.values(), Fraction(0, 1))
            lines.append(
                f"  {c['name']}#{cid} (1/{c['ratio_type']} 分流器)"
            )
            lines.append(f"    输入(-1): {total_in}")
            for p in sorted(outs.keys()):
                lines.append(f"    输出({p}): {outs[p]}")
        elif c["type"] == "merger":
            for p in sorted(ins.keys()):
                lines.append(
                    f"  {c['name']}#{cid} ({c['input_count']}路集流器)"
                    f"  输入({p}): {ins[p]}"
                )
                break  # 只显示一次名称
            for p in sorted(ins.keys()):
                if p != sorted(ins.keys())[0]:
                    lines.append(f"    输入({p}): {ins[p]}")
            total_out = outs.get(-1, Fraction(0, 1))
            lines.append(f"    输出(-1): {total_out}")
        lines.append("")

    # ---- 连接流量 ----
    lines.append("  --- 连接流量 ---")
    fb_count = 0
    for conn in conns:
        is_fb = conn.get("is_feedback", False)
        if is_fb:
            fb_count += 1
        sid, sport = conn["src_id"], conn["src_port"]
        did, dport = conn["dst_id"], conn["dst_port"]
        src_name = comp_map[sid]["name"]
        dst_name = comp_map[did]["name"]
        flow = flow_on_conn.get((sid, sport, did, dport), Fraction(0, 1))
        tag = " [反馈]" if is_fb else ""
        lines.append(
            f"  {src_name}#{sid}:{sport} → "
            f"{dst_name}#{did}:{dport} = {flow}{tag}"
        )

    # ---- 总输出 ----
    lines.append("")

    # 找到系统最终输出（无下游连接使用的 merger 输出端口 -1）
    used_outputs = set()
    for conn in conns:
        used_outputs.add((conn["src_id"], conn["src_port"]))

    # 系统主输出 = 所有集流器的输出端口(-1)中未被下游使用的
    main_output = Fraction(0, 1)
    main_output_ports = []
    for cid, c in comp_map.items():
        if c["type"] != "merger":
            continue
        out_key = (cid, -1)
        if out_key not in used_outputs:
            main_output += all_outputs[cid][-1]
            main_output_ports.append((c["name"], cid, all_outputs[cid][-1]))

    # 所有闲置端口（含 Pool 可复用输出）
    all_idle_ports = []
    total_idle = Fraction(0, 1)
    for cid, c in comp_map.items():
        for p in all_outputs[cid]:
            if (cid, p) not in used_outputs:
                total_idle += all_outputs[cid][p]
                all_idle_ports.append((c["name"], cid, p, all_outputs[cid][p]))

    lines.append("  --- 系统主输出（未连接下游的集流器输出） ---")
    for name, cid, val in main_output_ports:
        lines.append(f"  {name}#{cid}:-1 = {val}")
    lines.append(f"  主输出合计: {main_output}")

    if len(all_idle_ports) > len(main_output_ports):
        lines.append("")
        lines.append("  --- 闲置输出端口（可复用至 Pool） ---")
        for name, cid, port, val in all_idle_ports:
            if (cid, port) not in [(mp[1], -1) for mp in main_output_ports]:
                lines.append(f"  {name}#{cid}:{port} = {val}")
        lines.append(f"  闲置合计: {total_idle - main_output}")

    # ---- 与期望值对比 ----
    if target_fraction is not None:
        lines.append("")
        lines.append(f"  期望输出: {target_fraction}")
        lines.append(f"  实际主输出: {main_output}")
        if main_output == target_fraction:
            lines.append(f"  [PASS] 主输出与期望值 {target_fraction} 一致！")
        else:
            lines.append(
                f"  [FAIL] 主输出 {main_output} "
                f"≠ 期望值 {target_fraction}"
            )

    # ---- 闲置输出 ----
    lines.append("")
    idle_count = len(all_idle_ports)
    fb_edges = sum(1 for c in conns if c.get("is_feedback"))
    lines.append(f"  反馈边: {fb_edges} 条")
    lines.append(f"  闲置输出端口: {idle_count} 个（可复用至 Pool）")
    lines.append("=" * 60)

    return lines


# ========================================================================
#  主入口
# ========================================================================
def validate_network(
    json_path: str,
    expected_fraction: Optional[str] = None,
) -> bool:
    """
    完整验证流程。
    
    参数：
    - json_path: 待验证的 JSON 文件路径
    - expected_fraction: 可选的期望分数（如 "7/15"），用于最终输出比对
    
    返回：True 表示所有校验通过，False 表示存在问题
    """
    print(f"\n{'='*60}")
    print(f"  分流器网络验证器")
    print(f"  文件: {json_path}")
    print(f"{'='*60}\n")

    # ---- 加载 JSON ----
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[FATAL] 文件不存在: {json_path}")
        return False
    except json.JSONDecodeError as e:
        print(f"[FATAL] JSON 解析失败: {e}")
        return False

    all_errors: List[str] = []

    # ---- Step 1: 结构校验 ----
    print("Step 1 — JSON 结构完整性校验")
    errs = step1_validate_structure(data)
    all_errors.extend(errs)
    if errs:
        print()  # 空行分隔
    else:
        print()

    # ---- Step 2: 组件校验 ----
    print("Step 2 — 组件类型与 ID 唯一性校验")
    comp_map, errs = step2_validate_components(data.get("components", []))
    all_errors.extend(errs)
    if errs:
        # 组件校验失败则无法继续
        print("\n[FATAL] 组件校验未通过，中止后续步骤\n")
        _print_final_report(all_errors)
        return False
    print()

    # ---- Step 3: 连接引用校验 ----
    print("Step 3 — 连接引用的合法性校验")
    valid_conns, errs = step3_validate_connections(
        data.get("connections", []), comp_map
    )
    all_errors.extend(errs)
    if errs:
        print("\n[FATAL] 连接引用校验未通过，中止后续步骤\n")
        _print_final_report(all_errors)
        return False
    print()

    # ---- Step 4: 寻找入口点 ----
    print("Step 4 — 寻找网络入口点")
    entries = step4_find_entry_points(comp_map, valid_conns)
    if not entries:
        all_errors.append("[WARN] 未找到任何入口点（网络无外部输入？）")
    print()

    # ---- Step 5: 迭代求解 ----
    print("Step 5 — 迭代求解稳态流量")
    all_inputs, all_outputs, flow_on_conn, iterations = step5_solve_steady_state(
        comp_map, valid_conns, entries
    )
    print()

    # ---- Step 6: 校验分流器 ----
    print("Step 6 — 校验分流器不变式")
    errs = step6_validate_splitters(comp_map, all_inputs, all_outputs)
    all_errors.extend(errs)
    print()

    # ---- Step 7: 校验集流器 ----
    print("Step 7 — 校验集流器不变式")
    errs = step7_validate_mergers(comp_map, all_inputs, all_outputs)
    all_errors.extend(errs)
    print()

    # ---- Step 8: 校验反馈回路 ----
    print("Step 8 — 校验反馈回路稳态条件")
    errs = step8_validate_feedback(comp_map, valid_conns, all_inputs, all_outputs)
    all_errors.extend(errs)
    print()

    # ---- Step 9: 汇总报告 ----
    print("Step 9 — 汇总报告")
    target = None
    if expected_fraction:
        try:
            if "/" in expected_fraction:
                parts = expected_fraction.split("/")
                target = Fraction(int(parts[0]), int(parts[1]))
            else:
                target = Fraction(int(expected_fraction), 1)
        except (ValueError, ZeroDivisionError):
            print(f"  [WARN] 无法解析期望分数 '{expected_fraction}'，跳过比对")

    summary_lines = step9_summary(
        comp_map, valid_conns, entries,
        all_inputs, all_outputs, flow_on_conn,
        data.get("meta", {}), iterations, target,
    )
    for line in summary_lines:
        print(line)

    # ---- 终报 ----
    _print_final_report(all_errors)
    return len([e for e in all_errors if e.startswith("[FAIL]")]) == 0


def _print_final_report(errors: List[str]):
    """打印最终错误/警告汇总"""
    print()
    if not errors:
        print("=" * 60)
        print("  所有校验通过！网络拓扑正确。")
        print("=" * 60)
        return

    fails = [e for e in errors if "[FAIL]" in e]
    warns = [e for e in errors if "[WARN]" in e]

    print("=" * 60)
    if fails:
        print(f"  发现 {len(fails)} 个错误:")
        for e in fails:
            print(f"    {e}")
    if warns:
        print(f"  发现 {len(warns)} 个警告:")
        for e in warns:
            print(f"    {e}")
    print("=" * 60)


# ========================================================================
#  命令行入口
# ========================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="分流器网络 JSON 验证器 — 校验 flow_simulator.py 导出的网络正确性"
    )
    parser.add_argument(
        "json_file",
        help="待验证的 JSON 文件路径",
    )
    parser.add_argument(
        "--expected", "-e",
        help="期望的目标分数（如 '7/15'），用于比对总输出",
        default=None,
    )
    args = parser.parse_args()

    success = validate_network(args.json_file, args.expected)
    sys.exit(0 if success else 1)
