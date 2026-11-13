"""转换编排业务规则：状态流转、依赖准入、执行计划与血缘投影。

启用编排不是简单改状态：
1. 先把提交的节点扩展到依赖闭包，校验输入输出表、依赖声明、执行顺序；
2. 发现环、缺失依赖、同表多生产者时整组拒绝，不写任何血缘或状态；
3. 校验通过后按拓扑顺序生成执行计划，并从原始输入字段投影到最终输出字段；
4. 状态、血缘、版本快照、变更审计在同一次提交里一起落库。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store
from app.services.transform_graph import (
    build_node,
    build_plan,
    expand_group,
    project_lineage,
    topological_order,
    validate_group,
)

MODULE = "transform"
LINEAGE_MODULE = "lineage"
AUDIT_MODULE = "auditlog"
VERSION_STORE = "transform_versions"
REQUIRED_FIELDS = ["节点编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "编排评审中", "已启用", "已停用"]
ACTION_RULES = {"提交编排评审": "编排评审中", "启用编排": "已启用", "停用编排": "已停用"}
NEGATIVE_ACTIONS = ["停用编排"]

# 进程内串行化启用/停用：并发提交同一组依赖时，后到的一次会看到前一次的结果。
_ENABLE_LOCK = threading.RLock()

# 标记「节点已启用但没有版本记录」的重复提交；与「没有重复」区分开。
_NO_VERSION: dict[str, Any] = {"版本号": None}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TransformService:
    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def versions(self) -> list[dict[str, Any]]:
        return store.aux_rows(VERSION_STORE)

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ["转换逻辑", "执行顺序", "依赖节点", "失败策略", "调度周期", "字段映射"]:
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 单条动作
    # ------------------------------------------------------------------
    def run_action(
        self, entry_id: int, action: str, *, operator: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"转换节点 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于转换编排可执行范围", None
        if action == "启用编排":
            code = str(entry.get("节点编号") or "").strip()
            if not code:
                return None, f"转换节点 {entry_id} 缺少节点编号，不能启用", None
            outcome = self.enable_nodes(
                [code], operator=operator, idempotency_key=idempotency_key,
            )
            if not outcome["ok"]:
                return None, outcome["message"], outcome
            return entry, outcome["message"], outcome
        if action == "停用编排":
            return self.disable_node(entry, operator=operator)
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里", None
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"转换节点已{action}", {"ok": True, "message": f"转换节点已{action}"}

    # ------------------------------------------------------------------
    # 启用准入：校验 → 计划 → 血缘 → 一次性提交
    # ------------------------------------------------------------------
    def validate_nodes(self, nodes: list[str]) -> dict[str, Any]:
        """只做准入校验，返回诊断与计划预览，供启用前确认；不写任何数据。"""
        with _ENABLE_LOCK:
            evaluation = self._evaluate(_dedupe_codes(nodes))
        return {
            "ok": not evaluation["errors"],
            "结论": "可以启用" if not evaluation["errors"] else "拒绝启用",
            "节点集合": evaluation["requested"],
            "错误": evaluation["errors"],
            "执行计划预览": evaluation["plan"],
            "待生成血缘": evaluation["lineage"],
            "待分配版本": evaluation["version"],
            "已生效节点": sorted(evaluation["active_codes"]),
        }

    def enable_nodes(
        self,
        nodes: list[str],
        *,
        operator: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with _ENABLE_LOCK:
            key = str(idempotency_key or "").strip()
            requested = _dedupe_codes(nodes)
            if key:
                replay = _find_version_by_key(key)
                if replay is not None:
                    if replay.get("节点集合") == requested:
                        return {
                            "ok": True,
                            "message": (
                                f"幂等键 {key} 已生效，返回版本 v{replay['版本号']}，"
                                "未重复生成血缘"
                            ),
                            "entry": _version_view(replay, conclusion="幂等复用"),
                            "结论": "幂等复用",
                        }
                    return {
                        "ok": False,
                        "message": (
                            f"幂等键 {key} 已用于节点 "
                            f"{'、'.join(replay.get('节点集合') or [])}，不能再提交另一组节点"
                        ),
                        "entry": {"结论": "拒绝启用", "错误": [{
                            "代码": "幂等键冲突",
                            "说明": f"幂等键 {key} 已绑定版本 v{replay['版本号']}",
                            "节点": requested,
                            "表": [],
                        }]},
                        "结论": "拒绝启用",
                    }

            evaluation = self._evaluate(requested)
            if evaluation["errors"]:
                return {
                    "ok": False,
                    "message": _first_reason(evaluation["errors"]),
                    "entry": {
                        "结论": "拒绝启用",
                        "错误": evaluation["errors"],
                        "执行计划预览": evaluation["plan"],
                        "版本号": evaluation["version"],
                    },
                    "结论": "拒绝启用",
                }

            if evaluation["duplicate"] is _NO_VERSION:
                return {
                    "ok": True,
                    "message": (
                        f"节点 {'、'.join(requested)} 当前已处于启用状态，"
                        "本次重复提交未生成新血缘"
                    ),
                    "entry": {
                        "结论": "重复提交未再生效",
                        "节点集合": requested,
                        "血缘": [],
                    },
                    "结论": "重复提交未再生效",
                }
            if evaluation["duplicate"]:
                version = evaluation["duplicate"]
                if key and not version.get("幂等键"):
                    # 重复提交时把幂等键绑定到已生效版本，后续重试直接复用同一结论。
                    version["幂等键"] = key
                return {
                    "ok": True,
                    "message": (
                        f"节点 {'、'.join(requested)} 均已生效（最近版本 "
                        f"v{version['版本号']}），本次重复提交未生成新血缘"
                    ),
                    "entry": _version_view(version, conclusion="重复提交未再生效"),
                    "结论": "重复提交未再生效",
                }

            # 全部结论确定后再落库：任何异常都不会留下半张血缘图。
            version = self._commit(evaluation, operator=operator, idempotency_key=key)
            return {
                "ok": True,
                "message": (
                    f"转换依赖组已启用，版本 v{version['版本号']}："
                    f"生成执行计划 {len(version['计划'])} 步、"
                    f"字段级血缘 {len(version['血缘'])} 条"
                ),
                "entry": _version_view(version, conclusion="启用成功"),
                "结论": "启用成功",
            }

    def _evaluate(self, requested: list[str]) -> dict[str, Any]:
        versions = self.versions()
        active_versions = [item for item in versions if item.get("状态") in ("生效中", "部分停用")]
        active_codes: set[str] = set()
        active_nodes: dict[str, dict[str, Any]] = {}
        for version in active_versions:
            for code in _live_codes(version):
                row = _find_node_row(code)
                if row is None:
                    continue
                active_codes.add(code)
                active_nodes[code] = build_node(row)
        # 历史遗留的「已启用」节点若没有版本记录（例如迁移前启用），也按当前生效节点
        # 参与冲突校验，避免绕过唯一生产者约束。
        for row in store.rows(MODULE):
            code = str(row.get("节点编号") or "").strip()
            if row.get("status") == "已启用" and code and code not in active_codes:
                active_codes.add(code)
                active_nodes[code] = build_node(row)

        duplicate = None
        if requested and set(requested) <= active_codes:
            candidates = [
                version for version in versions
                if version.get("状态") in ("生效中", "部分停用")
                and _live_codes(version) & set(requested)
            ]
            duplicate = candidates[-1] if candidates else _NO_VERSION

        all_rows = store.rows(MODULE)
        requested_rows = []
        missing_requests = []
        for code in requested:
            row = _find_node_row(code, all_rows)
            if row is None:
                missing_requests.append(code)
                continue
            requested_rows.append(row)

        errors: list[dict[str, Any]] = []
        if not requested:
            errors.append({"代码": "缺少依赖节点", "说明": "未提交任何转换节点，无法生成执行计划", "节点": [], "表": []})
        for code in missing_requests:
            errors.append({
                "代码": "缺失依赖",
                "说明": f"提交的节点 {code} 不在转换编排清单中",
                "节点": [code],
                "表": [],
            })

        group, expand_errors = expand_group(requested_rows, all_rows)
        errors.extend(expand_errors)

        order, cycle_errors = topological_order(group)
        errors.extend(cycle_errors)
        errors.extend(validate_group(
            group, active_nodes=active_nodes, order=order, explicit_codes=set(requested),
        ))

        version_number = len(versions) + 1
        plan = build_plan(group, order, active_codes=active_codes)
        lineage = []
        if not errors:
            node_codes = {code: code for code in group}
            reserved = {
                str(row.get("关系编号") or "")
                for row in store.rows(LINEAGE_MODULE)
                if str(row.get("关系编号") or "")
            }
            new_codes = {code for code in group if code not in active_codes}
            lineage = project_lineage(
                group,
                order,
                table_fields=_table_fields,
                node_codes=node_codes,
                reserved=reserved,
                version=version_number,
                enable_time=_now(),
                node_filter=new_codes,
            )

        return {
            "errors": errors,
            "plan": plan,
            "lineage": lineage,
            "group": group,
            "order": order,
            "version": version_number,
            "active_codes": active_codes,
            "new_codes": {code for code in group if code not in active_codes},
            "requested": requested,
            "duplicate": duplicate,
        }

    def _commit(
        self,
        evaluation: dict[str, Any],
        *,
        operator: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        version_number = evaluation["version"]
        enable_time = _now()
        group = evaluation["group"]
        new_codes = evaluation["new_codes"]
        lineage_rows = evaluation["lineage"]
        plan = evaluation["plan"]

        touched_rows = [row for row in (_find_node_row(code) for code in group) if row is not None]
        before_text = _status_before(touched_rows)
        lineage_before = len(store.rows(LINEAGE_MODULE))
        audit_before = len(store.rows(AUDIT_MODULE))
        version_before = len(self.versions())
        status_before = {
            id(row): (row.get("status"), row.get("pending"), row.get("abnormal")) for row in touched_rows
        }

        try:
            # 1. 状态
            for row in touched_rows:
                row["status"] = "已启用"
                row["pending"] = False
                row["abnormal"] = False
                row["启用版本"] = version_number
                row["启用时间"] = enable_time

            # 2. 血缘
            for relation in lineage_rows:
                row = {
                    "id": _next_id(store.rows(LINEAGE_MODULE)),
                    "status": "已确认",
                    "pending": False,
                    "abnormal": False,
                    "关系编号": relation["关系编号"],
                    "上游表名": relation["上游表名"],
                    "下游表名": relation["下游表名"],
                    "上游字段": relation["上游字段"],
                    "下游字段": relation["下游字段"],
                    "字段识别": relation.get("字段识别"),
                    "转换说明": relation["转换说明"],
                    "采集批次": relation["采集批次"],
                    "关系版本": relation["关系版本"],
                    "来源节点": relation["来源节点"],
                    "来源版本": version_number,
                    "原始输入": relation.get("原始输入"),
                    "传递路径": relation.get("传递路径"),
                    "生效状态": "生效中",
                    "启用时间": enable_time,
                }
                store.rows(LINEAGE_MODULE).append(row)
                relation["血缘id"] = row["id"]

            # 3. 版本快照：执行计划与血缘都留在版本里，停用后仍可追溯。
            version = {
                "版本号": version_number,
                "幂等键": idempotency_key or None,
                "节点集合": list(evaluation["requested"]),
                "生效节点": sorted(new_codes),
                "已沿用节点": sorted(evaluation["active_codes"] & set(group)),
                "状态": "生效中",
                "启用时间": enable_time,
                "停用时间": None,
                "停用节点": [],
                "操作人": operator or "值班管理员",
                "计划": plan,
                "血缘": [dict(relation) for relation in lineage_rows],
            }
            store.aux_rows(VERSION_STORE).append(version)

            # 4. 变更审计：和状态、血缘同一次提交完成。
            audit = _append_audit({
                "变更类型": "启用编排投影血缘",
                "变更前值": before_text,
                "变更后值": (
                    f"版本 v{version_number}；计划 {len(plan)} 步；血缘 "
                    f"{'、'.join(item['关系编号'] for item in lineage_rows) or '无'}"
                ),
                "变更原因": (
                    f"转换依赖组 {'、'.join(evaluation['requested'])} 通过准入校验，"
                    f"按依赖图顺序生成执行计划与字段级血缘"
                ),
                "操作人": operator or "值班管理员",
                "操作时间": enable_time,
            })
            version["审计记录"] = audit["记录编号"]
        except Exception:
            # 任何异常都回到提交前：不留下半张血缘图，也不留下半启用的节点。
            del store.rows(LINEAGE_MODULE)[lineage_before:]
            del store.rows(AUDIT_MODULE)[audit_before:]
            del self.versions()[version_before:]
            for row in touched_rows:
                status, pending, abnormal = status_before[id(row)]
                row["status"], row["pending"], row["abnormal"] = status, pending, abnormal
                row.pop("启用版本", None)
                row.pop("启用时间", None)
            raise
        return version

    # ------------------------------------------------------------------
    # 停用：保留历史血缘与当时执行计划
    # ------------------------------------------------------------------
    def disable_node(
        self, entry: dict[str, Any], *, operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        code = str(entry.get("节点编号") or "").strip()
        if entry.get("status") != "已启用":
            return None, f"转换节点 {code} 当前状态是「{entry.get('status')}」，不能停用", None

        active_dependents = [
            node["code"]
            for row in store.rows(MODULE)
            for node in [build_node(row)]
            if node["code"] != code
            and row.get("status") == "已启用"
            and code in node["deps"]
        ]
        if active_dependents:
            return None, (
                f"转换节点 {code} 仍被已启用节点 {'、'.join(sorted(active_dependents))} 依赖，"
                "请先停用下游节点"
            ), None

        disable_time = _now()
        touched_versions = [
            version["版本号"] for version in self.versions()
            if code in set(version.get("生效节点") or []) - set(version.get("停用节点") or [])
        ]
        archived = 0
        for row in store.rows(LINEAGE_MODULE):
            source_nodes = str(row.get("来源节点") or "")
            if code not in [item.strip() for item in source_nodes.split("、") if item.strip()]:
                continue
            if row.get("生效状态") != "生效中":
                continue
            if row.get("来源版本") not in touched_versions:
                continue
            row["生效状态"] = "历史归档"
            row["归档时间"] = disable_time
            archived += 1

        entry["status"] = "已停用"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["停用时间"] = disable_time

        for version in self.versions():
            if version["版本号"] not in touched_versions:
                continue
            disabled_nodes = version.setdefault("停用节点", [])
            if code not in disabled_nodes:
                disabled_nodes.append(code)
            remaining = _live_codes(version)
            version["状态"] = "部分停用" if remaining else "已停用"
            version["停用时间"] = disable_time

        audit = _append_audit({
            "变更类型": "停用编排归档血缘",
            "变更前值": "已启用",
            "变更后值": (
                f"停用节点 {code}；归档血缘 {archived} 条；"
                f"版本 {'、'.join(f'v{item}' for item in touched_versions) or '无'} 保留当时执行计划"
            ),
            "变更原因": "编排停用保留历史血缘与执行计划，便于追溯已产出的数据",
            "操作人": operator or "值班管理员",
            "操作时间": disable_time,
        })
        message = (
            f"转换节点已停用，历史血缘归档 {archived} 条，"
            f"版本 {'、'.join(f'v{item}' for item in touched_versions) or '无'} 的执行计划已保留"
        )
        return entry, message, {"ok": True, "message": message, "entry": {
            "结论": "停用成功",
            "归档血缘数": archived,
            "版本": touched_versions,
            "审计记录": audit["记录编号"],
        }}


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------

def _dedupe_codes(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        items = [item.strip() for item in values.replace("，", ",").split(",")]
    else:
        items = [str(item).strip() for item in values]
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _find_node_row(code: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for row in rows if rows is not None else store.rows(MODULE):
        if str(row.get("节点编号") or "").strip() == str(code).strip():
            return row
    return None


def _live_codes(version: dict[str, Any]) -> set[str]:
    """版本里当前仍生效的节点：生效节点去掉已停用节点。"""
    return set(version.get("生效节点") or []) - set(version.get("停用节点") or [])


def _find_version_by_key(key: str) -> dict[str, Any] | None:
    for version in store.aux_rows(VERSION_STORE):
        if version.get("幂等键") == key:
            return version
    return None


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _first_reason(errors: list[dict[str, Any]]) -> str:
    first = errors[0]
    suffix = f"（共 {len(errors)} 项问题，见启用结论明细）" if len(errors) > 1 else ""
    return f"拒绝启用：{first['说明']}{suffix}"


def _status_before(rows: list[dict[str, Any]]) -> str:
    parts = [f"{row.get('节点编号')}={row.get('status')}" for row in rows]
    return "；".join(parts) or "未启用"


def _version_view(
    version: dict[str, Any], *, conclusion: str = "启用成功",
) -> dict[str, Any]:
    return {
        "结论": conclusion,
        "版本号": version.get("版本号"),
        "状态": version.get("状态"),
        "节点集合": version.get("节点集合"),
        "生效节点": version.get("生效节点"),
        "启用时间": version.get("启用时间"),
        "执行计划": version.get("计划"),
        "血缘": version.get("血缘"),
        "审计记录": version.get("审计记录"),
    }


def _table_fields(table: str) -> list[str]:
    """从数据字典读取表字段；没有登记时返回空列表，由字段映射兜底。"""
    fields: list[str] = []
    for row in store.rows("dictionary"):
        if str(row.get("表名") or "").strip() != table:
            continue
        name = str(row.get("字段名称") or "").strip()
        if name and name not in fields:
            fields.append(name)
    return fields


def _next_audit_code(rows: list[dict[str, Any]]) -> str:
    largest = 0
    for row in rows:
        code = str(row.get("记录编号") or "")
        digits = "".join(char for char in code if char.isdigit())
        if digits:
            largest = max(largest, int(digits))
    return f"AUDI-{largest + 1:04d}"


def _append_audit(values: dict[str, Any]) -> dict[str, Any]:
    rows = store.rows(AUDIT_MODULE)
    record = {
        "id": _next_id(rows),
        "status": "已记录",
        "pending": False,
        "abnormal": False,
        "记录编号": _next_audit_code(rows),
        "操作表名": "lineage",
        "变更类型": values["变更类型"],
        "变更前值": values["变更前值"],
        "变更后值": values["变更后值"],
        "操作人": values["操作人"],
        "操作时间": values["操作时间"],
        "变更原因": values["变更原因"],
    }
    rows.append(record)
    return record
