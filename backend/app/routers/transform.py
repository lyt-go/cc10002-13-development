"""转换编排接口：维护转换节点，覆盖提交编排评审、启用编排、停用编排等动作。

启用不只是改状态：`/enable` 先把依赖闭包做准入校验，再一次性生成执行计划、
字段级血缘、版本快照和变更审计；`/enable/preview` 用于启用前查看同一套结论。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.transform import TransformService

router = APIRouter(prefix="/api/transform", tags=["转换编排"])

service = TransformService()

LIST_FIELDS = ["节点编号", "输入表名", "输出表名", "转换逻辑", "执行顺序", "依赖节点", "失败策略", "调度周期"]
STATUSES = ["草稿", "编排评审中", "已启用", "已停用"]


def _requested_nodes(values: dict[str, Any]) -> list[str]:
    raw = values.get("节点") or values.get("nodes") or values.get("node_codes")
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("，", ",").split(",")
    else:
        parts = [str(item) for item in raw]
    return [part.strip() for part in parts if part and part.strip()]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、编排评审中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按节点编号与状态过滤转换编排列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/enable/preview", response_model=dict)
def preview_enable(
    nodes: str = Query(description="逗号分隔的节点编号，例如 TRAN_CLEAN,TRAN_DWD"),
) -> dict:
    """启用前预演：返回准入结论、按依赖图顺序的执行计划与待生成血缘，不写任何数据。"""
    requested = [item.strip() for item in nodes.replace("，", ",").split(",") if item.strip()]
    return service.validate_nodes(requested)


@router.post("/enable", response_model=ActionResult)
def enable_nodes(
    payload: EntryPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """按依赖组启用转换编排。

    提交的节点会自动扩展到依赖闭包；发现环、缺失依赖、同表多生产者时整组拒绝，
    并在 entry 中给出错误代码、节点与表。成功时返回版本、执行计划与字段级血缘。
    """
    requested = _requested_nodes(payload.values)
    key = str(payload.values.get("幂等键") or idempotency_key or "").strip()
    operator = str(payload.values.get("操作人") or "").strip() or None
    outcome = service.enable_nodes(requested, operator=operator, idempotency_key=key)
    return ActionResult(ok=bool(outcome["ok"]), message=outcome["message"], entry=outcome.get("entry"))


@router.get("/versions", response_model=dict)
def list_versions() -> dict:
    """启用版本列表：每个版本保留当时的执行计划与血缘，停用后也不删除。"""
    items = service.versions()
    return {"items": items, "total": len(items)}


@router.get("/versions/{version_no}", response_model=dict)
def get_version(version_no: int) -> dict:
    """读取单个启用版本的历史执行计划与血缘快照。"""
    for version in service.versions():
        if int(version.get("版本号", 0)) == version_no:
            return version
    raise HTTPException(status_code=404, detail=f"启用版本 v{version_no} 不存在")


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出转换编排清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "transform", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条转换节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"转换节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条转换节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="转换节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """对单条转换节点执行提交编排评审、启用编排、停用编排；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    operator = str(payload.values.get("操作人") or "").strip() or None
    key = str(payload.values.get("幂等键") or idempotency_key or "").strip()
    entry, message, outcome = service.run_action(
        entry_id, action, operator=operator, idempotency_key=key,
    )
    if entry is None:
        detail = outcome.get("entry") if outcome else None
        return ActionResult(ok=False, message=message, entry=detail)
    return ActionResult(ok=True, message=message, entry=outcome.get("entry") if outcome else entry)
