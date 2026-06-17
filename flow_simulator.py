from fractions import Fraction
from typing import List, Tuple, Dict, Optional
from math import gcd


# ========================================================================
# 基础组件定义
# ========================================================================

class Component:
    _id_counter = 0

    def __init__(self, name: str):
        Component._id_counter += 1
        self.id = Component._id_counter
        self.name = name

    def __repr__(self):
        return f"{self.name}#{self.id}"


class Splitter(Component):
    """分流器 ratio_type=2(1/2) 或 3(1/3)"""
    def __init__(self, ratio_type: int):
        assert ratio_type in (2, 3)
        super().__init__(f"1/{ratio_type}")
        self.ratio_type = ratio_type


class Merger(Component):
    """集流器 input_count 路输入"""
    def __init__(self, input_count: int):
        super().__init__(f"Merger{input_count}")
        self.input_count = input_count


class Connection:
    """物流带连接"""
    def __init__(self, src: Component, src_port: int, dst: Component, dst_port: int, is_feedback: bool = False):
        self.src = src
        self.src_port = src_port
        self.dst = dst
        self.dst_port = dst_port
        self.is_feedback = is_feedback

    def __repr__(self):
        fb = " [FB]" if self.is_feedback else ""
        return f"{self.src}:{self.src_port} -> {self.dst}:{self.dst_port}{fb}"


class Port:
    """输出端口描述"""
    def __init__(self, component: Component, index: int, value: Fraction):
        self.component = component
        self.index = index
        self.value = value

    def __repr__(self):
        return f"{self.component}:{self.index}={self.value}"


class Module:
    """
    子系统模块
    - components: 内部包含的物理组件（空列表表示从 Pool 复用的输出流）
    - entry: 主入口 (component, port_index)，port_index=-1 表示分流器默认输入
    - outputs: 可用的输出端口列表
    - ext_inputs: 需要外部输入的数量
    """
    def __init__(self, name: str, components: List[Component],
                 entry: Tuple[Component, int], outputs: List[Port],
                 ext_inputs: int):
        self.name = name
        self.components = components
        self.entry = entry
        self.outputs = outputs
        self.ext_inputs = ext_inputs

    @property
    def output_value(self) -> Fraction:
        return self.outputs[0].value if self.outputs else Fraction(0)

    def __repr__(self):
        return f"Module({self.name}, out={self.output_value}, ports={len(self.outputs)})"


# ========================================================================
# 网络构建器
# ========================================================================

class FlowNetwork:
    def __init__(self):
        self.components: List[Component] = []
        self.connections: List[Connection] = []
        # Pool: 闲置输出端口缓存 {value: [(component, port_index), ...]}
        self._pool: Dict[Fraction, List[Tuple[Component, int]]] = {}
        self._pool_log: List[str] = []
        self.total_ext_input = Fraction(0)

    # --------------------------------------------------------------------
    # 物理组件创建
    # --------------------------------------------------------------------
    def _add_splitter(self, ratio_type: int) -> Splitter:
        s = Splitter(ratio_type)
        self.components.append(s)
        return s

    def _add_merger(self, input_count: int) -> Merger:
        m = Merger(input_count)
        self.components.append(m)
        return m

    def _connect(self, src: Component, src_port: int, dst: Component, dst_port: int, is_feedback: bool = False):
        self.connections.append(Connection(src, src_port, dst, dst_port, is_feedback))

    # --------------------------------------------------------------------
    # Pool 管理
    # --------------------------------------------------------------------
    def _pool_add(self, value: Fraction, comp: Component, port: int):
        if value not in self._pool:
            self._pool[value] = []
        self._pool[value].append((comp, port))

    def _pool_take(self, value: Fraction) -> Optional[Tuple[Component, int]]:
        if value not in self._pool or not self._pool[value]:
            return None
        item = self._pool[value].pop(0)
        self._pool_log.append(f"复用闲置输出 {value} @ {item[0]}:{item[1]}")
        return item

    # --------------------------------------------------------------------
    # 核心：构造单位分数 1/k 的模块
    # --------------------------------------------------------------------
    def build_unit_fraction(self, k: int, allow_pool: bool = True) -> Module:
        """
        构造输出值为 1/k 的模块。
        allow_pool=False 时强制现场搭建（用于 feedback 内部，确保闲置输出最大化）
        """
        target = Fraction(1, k)

        if allow_pool:
            reused = self._pool_take(target)
            if reused:
                comp, port = reused
                return Module(
                    name=f"Pool[{1/k}]",
                    components=[],
                    entry=(comp, port),
                    outputs=[Port(comp, port, target)],
                    ext_inputs=0
                )

        if k == 2:
            return self._build_1_over_2()
        elif k == 3:
            return self._build_1_over_3()
        elif self._is_composite(k):
            return self._build_composite(k, allow_pool=allow_pool)
        else:
            return self._build_prime(k)

    def _build_1_over_2(self) -> Module:
        s = self._add_splitter(2)
        val = Fraction(1, 2)
        return Module(
            name="1/2",
            components=[s],
            entry=(s, -1),
            outputs=[Port(s, 0, val), Port(s, 1, val)],
            ext_inputs=1
        )

    def _build_1_over_3(self) -> Module:
        s = self._add_splitter(3)
        val = Fraction(1, 3)
        return Module(
            name="1/3",
            components=[s],
            entry=(s, -1),
            outputs=[Port(s, 0, val), Port(s, 1, val), Port(s, 2, val)],
            ext_inputs=1
        )

    def _build_composite(self, k: int, allow_pool: bool = True) -> Module:
        """
        合数：因数分解后串联。
        策略：若因数含 3，把 3 放最后，以保留 feedback 闲置输出能力。
        Pool 复用模块只能作为串联上游，不能作为下游（无入口），发现时强制重建。
        """
        factors = self._factorize(k)
        factors.sort()
        if 3 in factors:
            factors.remove(3)
            factors.append(3)

        mod = self.build_unit_fraction(factors[0], allow_pool=allow_pool)
        for f in factors[1:]:
            next_mod = self.build_unit_fraction(f, allow_pool=allow_pool)
            if not next_mod.components:
                # Pool 复用不能作为串联下游，强制新建
                next_mod = self.build_unit_fraction(f, allow_pool=False)
            mod = self._series(mod, next_mod)
        mod.name = f"1/{k}"
        return mod

    def _build_prime(self, p: int) -> Module:
        """
        质数：feedback(1/(p+1))
        p+1 = 2*t, t=(p+1)//2
        1/(p+1) = 1/2 * 1/t
        强制新建内部模块，确保 feedback 后闲置输出最大化
        """
        t = (p + 1) // 2
        mod_t = self.build_unit_fraction(t, allow_pool=False)
        mod_2 = self._build_1_over_2()

        # 串联顺序：让 1/t 在后（若其最后是 1/3 且有>=2个输出，则保留闲置能力）
        if (mod_t.outputs and len(mod_t.outputs) >= 2 and
            isinstance(mod_t.outputs[0].component, Splitter) and
            mod_t.outputs[0].component.ratio_type == 3):
            inner = self._series(mod_2, mod_t)
        else:
            inner = self._series(mod_t, mod_2)

        return self._feedback(inner, p)

    # --------------------------------------------------------------------
    # 拓扑运算：串联、反馈
    # --------------------------------------------------------------------
    def _series(self, A: Module, B: Module) -> Module:
        """
        串联 A -> B。
        A 可以是 Pool 复用（无物理组件），B 必须是物理模块。
        取 A 的一个输出作为 B 的输入；B 的输出按 A 的输出值缩放。
        最终暴露的只有 B 的输出（A 的其余输出不暴露）。
        """
        assert A.outputs, f"{A.name} 没有可用输出"
        assert B.components, f"{B.name} 是 Pool 复用，不能作为串联下游"
        bridge = A.outputs[0]

        # 物理连接
        self._connect(bridge.component, bridge.index, B.entry[0], B.entry[1])

        # B 的输出值按 bridge.value 缩放
        scaled = [Port(p.component, p.index, bridge.value * p.value) for p in B.outputs]

        # 入口：A 若为物理模块，入口是 A 的入口；否则串联系统无独立入口（依赖 Pool 上游）
        entry = A.entry if A.components else B.entry

        # 外部输入：A 的需求 + B 的需求 - 1（A 的一个输出满足了 B 的一个入口）
        # 若 A 是 Pool 复用（ext_inputs=0），B 的入口被满足，B 不再额外需要那个输入
        ext = A.ext_inputs + max(0, B.ext_inputs - 1)

        return Module(
            name=f"{A.name}×{B.name}",
            components=A.components + B.components,
            entry=entry,
            outputs=scaled,
            ext_inputs=ext
        )

    def _feedback(self, A: Module, p: int) -> Module:
        """
        对 1/(p+1) 模块 A 施加反馈，得到 1/p。
        方程: (1 + x) * 1/(p+1) = x  =>  x = 1/p
        入口用 Merger(2) 收集外部输入和反馈。
        若 A 的最后分流器是 1/3，则多出一个闲置输出（值也为 1/p），加入 Pool。
        """
        assert len(A.outputs) >= 2, "feedback 需要至少 2 个输出（1个反馈+1个系统输出）"
        x = Fraction(1, p)

        m = self._add_merger(2)
        # merger 输出 -> A 的入口
        self._connect(m, -1, A.entry[0], A.entry[1])

        # 取 A 的第一个输出做 feedback -> merger:1
        fb_port = A.outputs[0]
        self._connect(fb_port.component, fb_port.index, m, 1, is_feedback=True)

        # 剩余输出在稳定状态下值都变为 x = 1/p
        # A.outputs[1] 作为系统主输出
        remaining = [Port(A.outputs[1].component, A.outputs[1].index, x)]

        # 若最后分流器是 1/3，且有第 3 个输出，则该输出闲置入 Pool
        last = fb_port.component
        if isinstance(last, Splitter) and last.ratio_type == 3 and len(A.outputs) >= 3:
            spare = A.outputs[2]
            self._pool_add(x, spare.component, spare.index)

        return Module(
            name=f"1/{p}(fb)",
            components=[m] + A.components,
            entry=(m, 0),
            outputs=remaining,
            ext_inputs=1
        )

    # --------------------------------------------------------------------
    # 贪心分子拆分 & 完整分数构造
    # --------------------------------------------------------------------
    def build_fraction(self, n: int, m: int) -> Module:
        """构造 n/m"""
        g = gcd(n, m)
        n, m = n // g, m // g
        print(f"\n========== 构造 {n}/{m} ==========")

        # 分子贪心拆分
        terms = self._decompose(n, m)
        print(f"分子拆分: {n} = " + " + ".join(f"{c}×{d}" for c, d in terms))

        # 收集各分支模块
        branches: List[Tuple[int, int, Module]] = []
        total_ports = 0
        for coeff, d in terms:
            denom = m // d
            mod = self.build_unit_fraction(denom)
            branches.append((coeff, denom, mod))
            total_ports += coeff
            print(f"  需要 {coeff} 路 1/{denom} ({mod.name}), 可用输出端口 {len(mod.outputs)}")

        print(f"总并联支路数: {total_ports}")

        # 最终汇总集流器
        final_merger = self._add_merger(total_ports)

        port_idx = 0
        for coeff, denom, mod in branches:
            for _ in range(coeff):
                if not mod.outputs:
                    # 当前模块输出耗尽，按需补充同规格模块
                    extra = self.build_unit_fraction(denom)
                    mod.outputs.extend(extra.outputs)
                    mod.components.extend(extra.components)
                    mod.ext_inputs += extra.ext_inputs
                src = mod.outputs.pop(0)
                self._connect(src.component, src.index, final_merger, port_idx)
                port_idx += 1

        self.total_ext_input = Fraction(n, m)

        all_components = [final_merger]
        for _, _, mod in branches:
            all_components.extend(mod.components)

        return Module(
            name=f"{n}/{m}",
            components=all_components,
            entry=(final_merger, -1),
            outputs=[],
            ext_inputs=sum(mod.ext_inputs for _, _, mod in branches)
        )

    def _decompose(self, n: int, m: int) -> List[Tuple[int, int]]:
        """
        贪心拆分分子 n。
        对 m 的因数从大到小选取，使 n = sum(coeff * divisor)。
        返回 [(coeff, divisor), ...]。
        """
        divs = self._divisors(m)
        divs.sort(reverse=True)
        rem = n
        res = []
        for d in divs:
            if rem <= 0:
                break
            c = rem // d
            if c > 0:
                res.append((c, d))
                rem -= c * d
        if rem != 0:
            res = [(n, 1)]
        return res

    # --------------------------------------------------------------------
    # 工具函数
    # --------------------------------------------------------------------
    @staticmethod
    def _is_composite(k: int) -> bool:
        if k < 4:
            return False
        for i in range(2, int(k**0.5) + 1):
            if k % i == 0:
                return True
        return False

    @staticmethod
    def _factorize(k: int) -> List[int]:
        f = []
        d = 2
        while d * d <= k:
            while k % d == 0:
                f.append(d)
                k //= d
            d += 1
        if k > 1:
            f.append(k)
        return f

    @staticmethod
    def _divisors(m: int) -> List[int]:
        divs = set()
        for i in range(1, int(m**0.5) + 1):
            if m % i == 0:
                divs.add(i)
                divs.add(m // i)
        return sorted(list(divs))

    # --------------------------------------------------------------------
    # 报告
    # --------------------------------------------------------------------
    def report(self):
        print("\n========== 网络拓扑摘要 ==========")
        sp = sum(1 for c in self.components if isinstance(c, Splitter))
        mg = sum(1 for c in self.components if isinstance(c, Merger))
        print(f"总组件: {len(self.components)} (分流器 {sp}, 集流器 {mg})")
        print(f"连接数: {len(self.connections)}")
        print(f"目标外部输入: {self.total_ext_input}")
        if self._pool_log:
            print("\nPool 复用记录:")
            for log in self._pool_log:
                print(f"  {log}")
        if self._pool:
            print("\nPool 剩余闲置:")
            for val, items in self._pool.items():
                if items:
                    print(f"  {val}: {len(items)} 个")
        print("\n组件清单:")
        for c in self.components:
            if isinstance(c, Splitter):
                print(f"  {c}: 1/{c.ratio_type} 分流器")
            elif isinstance(c, Merger):
                print(f"  {c}: {c.input_count}路集流器")
        print("\n连接详情:")
        for conn in self.connections:
            print(f"  {conn}")

    def export_json(self) -> dict:
        """导出为可视化所需的 JSON 结构"""
        comps = []
        for c in self.components:
            d = {"id": c.id, "name": str(c)}
            if isinstance(c, Splitter):
                d["type"] = "splitter"
                d["ratio_type"] = c.ratio_type
            elif isinstance(c, Merger):
                d["type"] = "merger"
                d["input_count"] = c.input_count
            else:
                d["type"] = "unknown"
            comps.append(d)
        conns = []
        for conn in self.connections:
            conns.append({
                "src_id": conn.src.id,
                "src_port": conn.src_port,
                "dst_id": conn.dst.id,
                "dst_port": conn.dst_port,
                "is_feedback": conn.is_feedback
            })
        return {
            "components": comps,
            "connections": conns,
            "meta": {
                "total_ext_input": str(self.total_ext_input),
                "pool_log": self._pool_log,
                "component_count": len(self.components),
                "connection_count": len(self.connections)
            }
        }


# ========================================================================
# 命令行入口
# ========================================================================

import sys
import json

if __name__ == "__main__":
    import os

    def parse_input(text: str):
        text = text.strip()
        if not text:
            return None
        if "/" in text:
            part = text.split("/")
            if len(part) != 2:
                return None
            return int(part[0]), int(part[1])
        parts = text.split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
        return None

    def build_and_save(n, m, out_path=None):
        net = FlowNetwork()
        system = net.build_fraction(n, m)
        net.report()
        if out_path is None:
            out_path = f"network_{n}_{m}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(net.export_json(), f, ensure_ascii=False, indent=2)
        print(f"[已导出 JSON] -> {out_path}")
        return out_path

    args = sys.argv[1:]

    # 命令行快速模式
    if len(args) >= 1 and args[0] in ("-h", "--help"):
        print("用法: python flow_simulator.py [n m | n/m]")
        print("  无参数时进入交互模式")
        print("  例如: python flow_simulator.py 7 15")
        print("  退出交互模式: q / quit / exit")
        sys.exit(0)

    if len(args) == 1 and "/" in args[0]:
        parsed = parse_input(args[0])
        if parsed:
            n, m = parsed
            build_and_save(n, m)
            sys.exit(0)
    elif len(args) == 2:
        try:
            n, m = int(args[0]), int(args[1])
            build_and_save(n, m)
            sys.exit(0)
        except Exception:
            pass

    # 交互模式
    print("=" * 50)
    print("  有理数分流器构造器")
    print("  输入分数 (如 7/15 或 7 15)，q/quit/exit 退出")
    print("=" * 50)
    while True:
        try:
            text = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break
        text = text.strip()
        if text.lower() in ("q", "quit", "exit"):
            print("再见!")
            break
        parsed = parse_input(text)
        if parsed is None:
            print("格式错误，请输入如 7/15 或 7 15")
            continue
        n, m = parsed
        if m <= 0:
            print("分母必须为正整数")
            continue
        try:
            build_and_save(n, m)
        except Exception as e:
            print(f"构造失败: {e}")
