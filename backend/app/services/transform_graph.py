"""转换依赖图：解析、准入校验、执行计划与血缘投影。

这个模块只做纯计算：输入是转换节点记录，输出是诊断信息、拓扑计划和字段级血缘，
不修改任何存储。真正的提交由 ``TransformService`` 在全部校验通过后一次性完成。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Callable, Iterable

LIST_SEPARATORS = re.compile(r"[，,、;；|\n\r]+")
DEP_SEPARATORS = re.compile(r"[，,、;；|\s]+")
ARROW_SEPARATORS = re.compile(r"\s*(?:->|→|=>|—>)\s*")
MAX_CHAINS = 400


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in LIST_SEPARATORS.split(str(value)) if item.strip()]


def split_dependencies(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        tokens = [str(item).strip() for item in value]
    else:
        tokens = DEP_SEPARATORS.split(str(value))
    result: list[str] = []
    for token in tokens:
        token = token.strip()
        if token and token not in result:
            result.append(token)
    return result


def parse_order(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def normalize_name(value: str, *, strip_prefix: bool = False) -> str:
    text = str(value or "").strip().upper()
    if strip_prefix and text.startswith("TRAN_"):
        text = text[len("TRAN_"):]
    text = re.sub(r"[^0-9A-Z_]+", "_", text).strip("_")
    return text or "NODE"


def _parse_qualified_field(value: str) -> tuple[str | None, str]:
    text = value.strip()
    if "." in text:
        table, field = text.split(".", 1)
        return table.strip(), field.strip()
    return None, text


def parse_field_mapping(value: Any) -> list[dict[str, str | None]]:
    """解析字段映射。

    支持 ``源字段:目标字段``、``源表.源字段→目标表.目标字段``，多组用逗号/分号分隔。
    也接受已经结构化的列表，方便接口直接提交。
    """
    entries: list[dict[str, str | None]] = []
    if value is None:
        return entries
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                source_table = item.get("source_table") or item.get("源表")
                source_field = item.get("source_field") or item.get("源字段")
                target_table = item.get("target_table") or item.get("目标表")
                target_field = item.get("target_field") or item.get("目标字段")
                if str(source_field or "").strip() and str(target_field or "").strip():
                    entries.append({
                        "source_table": str(source_table).strip() if source_table else None,
                        "source_field": str(source_field).strip(),
                        "target_table": str(target_table).strip() if target_table else None,
                        "target_field": str(target_field).strip(),
                    })
            else:
                entries.extend(parse_field_mapping(str(item)))
        return entries
    text = str(value).strip()
    if not text:
        return entries
    groups = re.split(r"[，,、;；\n\r]+", text)
    for group in groups:
        group = group.strip()
        if not group:
            continue
        if ARROW_SEPARATORS.search(group):
            left, right = ARROW_SEPARATORS.split(group, maxsplit=1)
        elif ":" in group:
            left, right = group.split(":", 1)
        else:
            continue
        source_table, source_field = _parse_qualified_field(left)
        target_table, target_field = _parse_qualified_field(right)
        if not source_field or not target_field:
            continue
        entries.append({
            "source_table": source_table or None,
            "source_field": source_field,
            "target_table": target_table or None,
            "target_field": target_field,
        })
    return entries


# ---------------------------------------------------------------------------
# 节点与依赖图
# ---------------------------------------------------------------------------

def build_node(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("节点编号") or "").strip()
    inputs = _dedupe(split_list(row.get("输入表名")))
    outputs = _dedupe(split_list(row.get("输出表名")))
    deps = split_dependencies(row.get("依赖节点"))
    raw_order = row.get("执行顺序")
    return {
        "id": int(row.get("id", 0)),
        "code": code,
        "inputs": inputs,
        "outputs": outputs,
        "deps": deps,
        "order": parse_order(raw_order),
        "order_text": raw_order,
        "mapping": parse_field_mapping(row.get("字段映射")),
        "status": str(row.get("status") or ""),
        "row": row,
    }


def _error(kind: str, message: str, *, nodes: Iterable[str] = (), tables: Iterable[str] = ()) -> dict[str, Any]:
    return {"代码": kind, "说明": message, "节点": list(nodes), "表": list(tables)}


def expand_group(
    requested: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
) -> tuple[OrderedDict[str, dict[str, Any]], list[dict[str, Any]]]:
    """把提交的节点扩展到依赖闭包；重复节点只保留一次。"""
    errors: list[dict[str, Any]] = []
    by_code: dict[str, dict[str, Any]] = {}
    duplicate_codes: dict[str, int] = {}
    for row in all_rows:
        node = build_node(row)
        code = node["code"]
        if not code:
            continue
        if code in by_code:
            duplicate_codes[code] = duplicate_codes.get(code, 1) + 1
            continue
        by_code[code] = node
    for code, count in duplicate_codes.items():
        errors.append(_error(
            "重复节点编号",
            f"节点编号 {code} 存在 {count} 条记录，无法确定唯一转换节点",
            nodes=[code],
        ))

    group: OrderedDict[str, dict[str, Any]] = OrderedDict()
    missing: list[dict[str, Any]] = []
    seen_missing: set[tuple[str, str]] = set()

    def visit(node: dict[str, Any]) -> None:
        code = node["code"]
        if code in group:
            return
        group[code] = node
        for dep_code in node["deps"]:
            dep_node = by_code.get(dep_code)
            if dep_node is None:
                key = (code, dep_code)
                if key not in seen_missing:
                    seen_missing.add(key)
                    missing.append(_error(
                        "缺失依赖",
                        f"节点 {code or '（未命名节点）'} 依赖的节点 {dep_code} 不存在，无法生成执行计划",
                        nodes=[code, dep_code],
                    ))
                continue
            visit(dep_node)

    for row in requested:
        code = str(row.get("节点编号") or "").strip()
        node = by_code.get(code) or build_node(row)
        if code and by_code.get(code) is None:
            errors.append(_error(
                "缺失依赖",
                f"提交的节点 {code} 不在转换编排清单中",
                nodes=[code],
            ))
            continue
        visit(node)

    errors.extend(missing)
    return group, errors


def _ancestors(code: str, group: OrderedDict[str, dict[str, Any]]) -> set[str]:
    """沿依赖向上收集所有祖先节点（传递闭包）。"""
    seen: set[str] = set()
    stack = list(group[code]["deps"]) if code in group else []
    while stack:
        current = stack.pop()
        if current in seen or current not in group:
            continue
        seen.add(current)
        stack.extend(group[current]["deps"])
    return seen


def _cycle_path(group: OrderedDict[str, dict[str, Any]]) -> list[str] | None:
    """深度优先找一条可读的环路径，例如 A → B → A。"""
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(code: str) -> list[str] | None:
        state[code] = 1
        stack.append(code)
        node = group[code]
        for dep_code in node["deps"]:
            if dep_code not in group:
                continue
            if state.get(dep_code, 0) == 1:
                start = stack.index(dep_code)
                return stack[start:] + [dep_code]
            if state.get(dep_code, 0) == 0:
                found = dfs(dep_code)
                if found:
                    return found
        stack.pop()
        state[code] = 2
        return None

    for code in sorted(group):
        if state.get(code, 0) == 0:
            found = dfs(code)
            if found:
                return found
    return None


def topological_order(
    group: OrderedDict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """按依赖优先排序；同一层用执行顺序、节点编号稳定排序。"""
    import heapq

    indegree = {code: 0 for code in group}
    children: dict[str, list[str]] = {code: [] for code in group}
    for code, node in group.items():
        for dep_code in node["deps"]:
            if dep_code in group:
                indegree[code] += 1
                children[dep_code].append(code)

    def sort_key(code: str) -> tuple[int, str]:
        node = group[code]
        return (node["order"] if node["order"] is not None else 10**9, code)

    heap = [(*sort_key(code), code) for code, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        _, _, code = heapq.heappop(heap)
        order.append(code)
        for child in children[code]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, (*sort_key(child), child))

    errors: list[dict[str, Any]] = []
    if len(order) != len(group):
        cycle = _cycle_path(group)
        path_text = " → ".join(cycle) if cycle else "、".join(sorted(set(group) - set(order)))
        errors.append(_error(
            "循环依赖",
            f"依赖图存在环：{path_text}，整组节点不能启用",
            nodes=cycle or sorted(set(group) - set(order)),
        ))
        # 环存在时仍给出一个稳定顺序，便于预览定位，但不作为可执行结论。
        order = list(order) + [code for code in sorted(group) if code not in order]
    return order, errors


def validate_group(
    group: OrderedDict[str, dict[str, Any]],
    *,
    active_nodes: dict[str, dict[str, Any]],
    order: list[str],
    explicit_codes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """校验输入输出表、依赖节点、执行顺序与同表生产者冲突。"""
    errors: list[dict[str, Any]] = []
    position = {code: index for index, code in enumerate(order)}

    # 1. 必填与基本表信息
    for code, node in group.items():
        if not code:
            errors.append(_error("缺少节点编号", "转换节点缺少节点编号，不能启用"))
        if not node["inputs"]:
            errors.append(_error("缺少输入表", f"节点 {code} 未填写输入表名，无法生成血缘", nodes=[code]))
        if not node["outputs"]:
            errors.append(_error("缺少输出表", f"节点 {code} 未填写输出表名，无法生成血缘", nodes=[code]))
        overlap = set(node["inputs"]) & set(node["outputs"])
        if overlap:
            errors.append(_error(
                "输入输出表冲突",
                f"节点 {code} 的输入表与输出表相同：{'、'.join(sorted(overlap))}，会形成表级自环",
                nodes=[code], tables=sorted(overlap),
            ))
        if node["order"] is None:
            errors.append(_error(
                "执行顺序无效",
                f"节点 {code} 的执行顺序「{node['order_text']}」不是整数，无法保证按序执行",
                nodes=[code],
            ))

    # 2. 依赖必须存在、输出表需进入下游输入表、执行顺序必须早于下游
    for code, node in group.items():
        for dep_code in node["deps"]:
            dep = group.get(dep_code)
            if dep is None:
                continue  # 缺失依赖已由 expand_group 记录
            if not (set(dep["outputs"]) & set(node["inputs"])):
                errors.append(_error(
                    "依赖与输入表不一致",
                    f"节点 {code} 依赖 {dep_code}，但 {dep_code} 的输出表 "
                    f"[{'、'.join(dep['outputs']) or '空'}] 不在 {code} 的输入表 "
                    f"[{'、'.join(node['inputs']) or '空'}] 中",
                    nodes=[code, dep_code], tables=dep["outputs"],
                ))
            if dep["status"] == "已停用" and dep_code not in (explicit_codes or set()):
                errors.append(_error(
                    "依赖节点已停用",
                    f"节点 {code} 依赖的节点 {dep_code} 当前已停用；"
                    "如确需恢复，请把该节点一并提交启用",
                    nodes=[code, dep_code],
                ))
            if node["order"] is not None and dep["order"] is not None and dep["order"] >= node["order"]:
                errors.append(_error(
                    "执行顺序冲突",
                    f"节点 {code}（顺序 {node['order']}）依赖 {dep_code}（顺序 {dep['order']}），"
                    f"依赖节点的执行顺序必须先于下游节点",
                    nodes=[code, dep_code],
                ))

    # 3. 同表多个生产者：按依赖图顺序收集，冲突结论可定位到表和节点
    producers: OrderedDict[str, list[str]] = OrderedDict()
    for code in order:
        node = group.get(code)
        if node is None:
            continue
        for table in node["outputs"]:
            producers.setdefault(table, []).append(code)
    for table, codes in producers.items():
        if len(codes) > 1:
            ordered_codes = sorted(codes, key=lambda item: (position.get(item, len(order)), item))
            errors.append(_error(
                "同表多生产者",
                f"输出表 {table} 同时由节点 {'、'.join(ordered_codes)} 生产，按依赖图顺序无法确定唯一上游",
                nodes=ordered_codes, tables=[table],
            ))
        for active_code, active_node in active_nodes.items():
            if active_code in group:
                continue
            if table in active_node["outputs"]:
                errors.append(_error(
                    "同表多生产者",
                    f"输出表 {table} 已由启用节点 {active_code} 生产，本次启用节点 {codes[0]} 会形成冲突",
                    nodes=[active_code, codes[0]], tables=[table],
                ))

    # 4. 输入表若由本组节点生产，该生产者必须能通过依赖链排在当前节点之前；
    #    直接依赖不是硬性要求，传递依赖同样保证执行顺序。
    for code in order:
        node = group.get(code)
        if node is None:
            continue
        for table in node["inputs"]:
            table_producers = [producer for producer in producers.get(table, []) if producer != code]
            if not table_producers:
                continue
            ancestors = _ancestors(code, group)
            missing_deps = [producer for producer in table_producers if producer not in ancestors]
            if missing_deps:
                errors.append(_error(
                    "依赖声明缺失",
                    f"节点 {code} 的输入表 {table} 由节点 {'、'.join(missing_deps)} 生产，"
                    "但依赖链上没有该生产者，执行顺序无法保证",
                    nodes=[code, *missing_deps], tables=[table],
                ))

    return errors


# ---------------------------------------------------------------------------
# 执行计划
# ---------------------------------------------------------------------------

def build_plan(
    group: OrderedDict[str, dict[str, Any]],
    order: list[str],
    *,
    active_codes: set[str],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for index, code in enumerate(order, start=1):
        node = group.get(code)
        if node is None:
            continue
        plan.append({
            "执行序号": index,
            "节点编号": code,
            "节点id": node["id"],
            "输入表名": "、".join(node["inputs"]),
            "输出表名": "、".join(node["outputs"]),
            "执行顺序": node["order"],
            "依赖节点": "、".join(node["deps"]),
            "本次动作": "沿用已启用版本" if code in active_codes else "启用",
        })
    return plan


# ---------------------------------------------------------------------------
# 字段级血缘投影
# ---------------------------------------------------------------------------

class FieldProjector:
    def __init__(
        self,
        group: OrderedDict[str, dict[str, Any]],
        order: list[str],
        *,
        table_fields: Callable[[str], list[str]],
    ) -> None:
        self.group = group
        self.order = order
        self.table_fields = table_fields
        self.producer_by_table: dict[str, str] = {}
        for code in order:
            node = group.get(code)
            if node is None:
                continue
            for table in node["outputs"]:
                self.producer_by_table.setdefault(table, code)
        self._memo: dict[tuple[str, str, str], list[list[tuple[str, str]]]] = {}

    # -- 字段集合 --------------------------------------------------------
    def output_fields(self, code: str, table: str) -> list[str]:
        node = self.group[code]
        targets = [
            str(item["target_field"])
            for item in node["mapping"]
            if self._target_table(code, item) == table
        ]
        if targets:
            return _dedupe(targets)
        if node["mapping"]:
            # 映射只覆盖了部分输出表时，其余表回退到字典字段。
            fallback = self.table_fields(table)
            return fallback or ["*"]
        fallback = self.table_fields(table)
        return fallback or ["*"]

    def _target_table(self, code: str, item: dict[str, Any]) -> str:
        node = self.group[code]
        return str(item.get("target_table") or (node["outputs"][0] if node["outputs"] else ""))

    def mapping_mode(self, code: str | None, table: str, field: str) -> str:
        """说明字段关系的识别方式，整表兜底时提示补充字段映射。"""
        if not code or code not in self.group:
            return "整表兜底"
        node = self.group[code]
        for item in node["mapping"]:
            if self._target_table(code, item) == table and str(item.get("target_field") or "") in (field, "*"):
                return "显式映射"
        if node["mapping"]:
            return "同名字段推断"
        for table_name in node["inputs"]:
            if field in self.table_fields(table_name):
                return "同名字段推断"
        return "整表兜底"

    def source_tables(self, code: str, out_table: str, out_field: str) -> list[tuple[str, str]]:
        node = self.group[code]
        explicit: list[tuple[str, str]] = []
        for item in node["mapping"]:
            if self._target_table(code, item) != out_table:
                continue
            target_field = str(item.get("target_field") or "")
            if target_field != out_field and target_field != "*":
                continue
            source_table = str(item.get("source_table") or "")
            if not source_table:
                source_table = self._default_source_table(code, str(item.get("source_field") or ""))
            source_field = str(item.get("source_field") or "*")
            if source_table:
                explicit.append((source_table, source_field))
        if explicit:
            return _dedupe(explicit)

        # 没有显式映射时按同名字段推断；推断不到就用首张输入表的全字段兜底。
        same_name: list[tuple[str, str]] = []
        for table in node["inputs"]:
            fields = self.table_fields(table)
            if out_field in fields:
                same_name.append((table, out_field))
        if same_name:
            return _dedupe(same_name)
        if node["inputs"]:
            return [(node["inputs"][0], "*")]
        return []

    def _default_source_table(self, code: str, source_field: str) -> str:
        node = self.group[code]
        for table in node["inputs"]:
            if source_field in self.table_fields(table):
                return table
        return node["inputs"][0] if node["inputs"] else ""

    def _expand_producer_field(self, producer: str, table: str, source_field: str) -> list[str]:
        if source_field == "*":
            fields = self.output_fields(producer, table)
            return fields or ["*"]
        fields = self.output_fields(producer, table)
        if source_field in fields or "*" in fields:
            return [source_field]
        # 生产者的输出字段清单里没有该字段时仍按原字段继续追溯，避免静默丢失血缘。
        return [source_field]

    def _expand_raw_field(self, table: str, source_field: str) -> list[str]:
        if source_field != "*":
            return [source_field]
        fields = self.table_fields(table)
        return fields or ["*"]

    # -- 链路追溯 --------------------------------------------------------
    def chains(self, code: str, out_table: str, out_field: str) -> list[list[tuple[str, str]]]:
        key = (code, out_table, out_field)
        if key in self._memo:
            return self._memo[key]
        chains = self._chains(code, out_table, out_field, frozenset({code}))
        self._memo[key] = chains
        return chains

    def _chains(
        self,
        code: str,
        out_table: str,
        out_field: str,
        visiting: frozenset[str],
    ) -> list[list[tuple[str, str]]]:
        result: list[list[tuple[str, str]]] = []
        for source_table, source_field in self.source_tables(code, out_table, out_field):
            producer = self.producer_by_table.get(source_table)
            if producer and producer in self.group and producer not in visiting:
                for producer_field in self._expand_producer_field(producer, source_table, source_field):
                    for sub_chain in self._chains(producer, source_table, producer_field, visiting | {code}):
                        result.append(sub_chain + [(out_table, out_field)])
                        if len(result) >= MAX_CHAINS:
                            return _dedupe_chains(result)
            elif producer == code:
                # 自产自用不参与血缘传递，避免自环；校验阶段已单独拦截。
                result.append([(source_table, source_field), (out_table, out_field)])
            else:
                for raw_field in self._expand_raw_field(source_table, source_field):
                    result.append([(source_table, raw_field), (out_table, out_field)])
            if len(result) >= MAX_CHAINS:
                break
        return _dedupe_chains(result)

    # -- 投影结果 --------------------------------------------------------
    def project(self) -> list[dict[str, Any]]:
        """按依赖图的直接上下游边投影字段关系。

        每条链路都会展开成相邻表之间的关系，例如
        RAW_ORDERS.amount → ODS_ORDERS.amount → DWD_ORDERS.amount → CLEAN_ORDERS.amount
        同时生成 RAW→ODS、ODS→DWD、DWD→CLEAN 三条字段级关系；
        「传递路径」与「原始输入」保留从原始输入字段到最终输出字段的完整传递。
        """
        relations: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
        for code in self.order:
            node = self.group.get(code)
            if node is None:
                continue
            for out_table in node["outputs"]:
                for out_field in self.output_fields(code, out_table):
                    for chain in self.chains(code, out_table, out_field):
                        for index in range(len(chain) - 1):
                            up_table, up_field = chain[index]
                            down_table, down_field = chain[index + 1]
                            if up_table == down_table:
                                continue
                            key = (up_table, down_table)
                            item = relations.setdefault(key, {
                                "上游表名": up_table,
                                "下游表名": down_table,
                                "上游节点": self.producer_by_table.get(up_table),
                                "下游节点": self.producer_by_table.get(down_table),
                                "字段对": [],
                                "路径": [],
                                "原始输入": [],
                                "字段识别": [],
                            })
                            mode = self.mapping_mode(
                                self.producer_by_table.get(down_table), down_table, down_field,
                            )
                            if mode not in item["字段识别"]:
                                item["字段识别"].append(mode)
                            pair = (up_field, down_field)
                            if pair not in item["字段对"]:
                                item["字段对"].append(pair)
                            path = " → ".join(f"{table}.{field}" for table, field in chain)
                            if path not in item["路径"]:
                                item["路径"].append(path)
                            edge_path = " → ".join(table for table, _ in chain)
                            if edge_path not in item["路径"]:
                                item["路径"].append(edge_path)
                            root = chain[0]
                            if root not in item["原始输入"]:
                                item["原始输入"].append(root)
        return list(relations.values())


def _dedupe(values: Iterable[Any]) -> list[Any]:
    """稳定去重：保持第一次出现的顺序，字符串与元组都按原值保存。"""
    result: list[Any] = []
    for value in values:
        if value == "" or value is None:
            continue
        if value not in result:
            result.append(value)
    return result


def _dedupe_chains(chains: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[list[tuple[str, str]]] = []
    for chain in chains:
        key = tuple(chain)
        if key not in seen:
            seen.add(key)
            result.append(chain)
    return result


def relation_identifier(
    relation: dict[str, Any],
    *,
    node_codes: dict[str, str],
    reserved: set[str],
    version: int,
) -> str:
    """关系编号优先使用节点名，例如 LINE_LAND_TO_CLEAN；冲突时加版本后缀。"""
    up_node = relation["上游节点"]
    down_node = relation["下游节点"]
    up_name = node_codes.get(up_node) if up_node else relation["上游表名"]
    down_name = node_codes.get(down_node) if down_node else relation["下游表名"]
    base = (
        f"LINE_{normalize_name(up_name, strip_prefix=True)}"
        f"_TO_{normalize_name(down_name, strip_prefix=True)}"
    )
    candidate = base
    if candidate in reserved and version > 1:
        candidate = f"{base}_V{version}"
    index = 2
    while candidate in reserved:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def project_lineage(
    group: OrderedDict[str, dict[str, Any]],
    order: list[str],
    *,
    table_fields: Callable[[str], list[str]],
    node_codes: dict[str, str],
    reserved: set[str],
    version: int,
    enable_time: str,
    node_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    projector = FieldProjector(group, order, table_fields=table_fields)
    results: list[dict[str, Any]] = []
    for relation in projector.project():
        up_node = relation["上游节点"]
        down_node = relation["下游节点"]
        if node_filter is not None and up_node not in node_filter and down_node not in node_filter:
            continue
        rel_id = relation_identifier(
            relation, node_codes=node_codes, reserved=reserved, version=version,
        )
        reserved.add(rel_id)
        up_fields = [pair[0] for pair in relation["字段对"]]
        down_fields = [pair[1] for pair in relation["字段对"]]
        path = relation["路径"][0] if relation["路径"] else ""
        roots = relation.get("原始输入") or []
        root_text = "、".join(f"{table}.{field}" for table, field in roots)
        source_nodes = "、".join(code for code in (up_node, down_node) if code)
        modes = relation.get("字段识别") or []
        mode_text = "、".join(modes)
        note = (
            f"字段按启用计划传递（{mode_text}）" if mode_text else "字段按启用计划传递"
        )
        if "整表兜底" in modes:
            note += "；存在整表兜底字段，建议补充字段映射"
        results.append({
            "关系编号": rel_id,
            "上游表名": relation["上游表名"],
            "下游表名": relation["下游表名"],
            "上游字段": "、".join(dedupe_fields(up_fields)),
            "下游字段": "、".join(dedupe_fields(down_fields)),
            "字段识别": mode_text,
            "转换说明": f"{note}：{path}" if path else note,
            "采集批次": f"TRAN-ENABLE-V{version}",
            "关系版本": f"v{version}",
            "来源节点": source_nodes,
            "来源版本": version,
            "原始输入": root_text,
            "传递路径": relation["路径"],
            "生效状态": "生效中",
            "启用时间": enable_time,
        })
    return results


def dedupe_fields(values: Iterable[str]) -> list[str]:
    return _dedupe(values)
