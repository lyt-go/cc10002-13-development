"""数据血缘业务规则：状态流转、字段校验与筛选口径都收在这里。

字段级血缘由转换编排启用时投影写入，这里额外说明血缘的来源版本与生效状态，
停用只归档不删除，历史可一直追溯。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "lineage"
REQUIRED_FIELDS = ["关系编号", "上游表名", "下游表名"]
STATUS_ORDER = ["草稿", "解析中", "已确认", "已废弃"]
ACTION_RULES = {"发起解析": "解析中", "确认关系": "已确认", "废弃关系": "已废弃"}
NEGATIVE_ACTIONS = []


class LineageService:
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
            rows = [row for row in rows if keyword in str(row.get("关系编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def project_view(self, *, up_table: str | None = None, down_table: str | None = None) -> dict[str, Any]:
        """按表名返回字段级血缘与链条路径，只读不写。"""
        rows = store.rows(MODULE)
        if up_table:
            rows = [row for row in rows if str(row.get("上游表名")) == up_table]
        if down_table:
            rows = [row for row in rows if str(row.get("下游表名")) == down_table]
        return {"items": rows, "total": len(rows)}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"血缘关系 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据血缘可执行范围"
        if entry.get("生效状态") == "历史归档":
            return None, (
                f"血缘关系 {entry.get('关系编号')} 已随转换节点停用归档，只读追溯，"
                "不能再执行状态流转"
            )
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"血缘关系已{action}"
