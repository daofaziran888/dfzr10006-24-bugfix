"""数据加工转换接口：维护加工规则，覆盖状态流转、映射表达式并发修改与转换任务链路。

链路接口：
- POST /{id}/tasks            提交转换任务（解析 -> 裁剪 -> 日志落库）
- POST /{id}/snapshot/confirm 确认输出字段快照
- GET  /{id}/tasks            查看任务（含中断标记，可再次提交）
- GET  /{id}/logs             历史转换日志（按原规则编号保留，成功/失败分账）
- PUT  /{id}/expression       并发安全地修改映射表达式（乐观版本号）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.transform import transform_service

router = APIRouter(prefix="/api/transform", tags=["数据加工转换"])

LIST_FIELDS = ["规则编号", "输入格式", "输出格式", "映射表达式", "字段过滤", "编码设置", "转换日志", "规则状态"]
STATUSES = ["草稿", "生效中", "已暂停", "已关闭"]


class TransformTaskPayload(BaseModel):
    """转换任务入参：records 支持单条对象或对象数组。"""

    records: Any = Field(default_factory=list)
    keep_fields: list[str] | None = None


class ExpressionPayload(BaseModel):
    """映射表达式修改入参，expected_version 用于并发冲突检测。"""

    expression: str
    field_filter: Any = None
    expected_version: int | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤数据加工转换列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = transform_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据加工转换清单：返回当前过滤条件下的全量数据。"""
    items, total = transform_service.list_entries(page=1, size=10000)
    return {"module": "transform", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条加工规则明细；不存在时给出可读的错误说明。"""
    entry = transform_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"加工规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条加工规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = transform_service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="加工规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条加工规则执行启用规则、暂停规则、关闭规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = transform_service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.put("/{entry_id}/expression", response_model=ActionResult)
def update_expression(entry_id: int, payload: ExpressionPayload) -> ActionResult:
    """修改映射表达式；并发修改同一表达式时只接受一个生效版本，冲突请求明确拒绝。"""
    entry, message, version = transform_service.update_expression(
        entry_id,
        payload.expression,
        payload.field_filter,
        expected_version=payload.expected_version,
    )
    if entry is None:
        raise HTTPException(status_code=409, detail=message)
    entry = dict(entry)
    entry["表达式版本"] = version
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/tasks", response_model=ActionResult)
def submit_task(entry_id: int, payload: TransformTaskPayload) -> ActionResult:
    """提交转换任务：解析 -> 字段裁剪 -> 日志落库；被拒绝或中断时返回原因，任务可再次提交。"""
    task, message = transform_service.submit_task(entry_id, payload.records, keep_fields=payload.keep_fields)
    if task is None:
        raise HTTPException(status_code=404, detail=message)
    ok = bool(task.get("成功"))
    return ActionResult(ok=ok, message=message, entry=task)


@router.post("/{entry_id}/snapshot/confirm", response_model=ActionResult)
def confirm_snapshot(entry_id: int) -> ActionResult:
    """确认输出字段快照；确认前数据订阅详情读取不到半成品输出。"""
    entry, message = transform_service.confirm_snapshot(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/tasks")
def list_tasks(entry_id: int) -> dict[str, Any]:
    """查看规则下的转换任务，含中断标记；中断任务不阻塞再次提交。"""
    if transform_service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"加工规则 {entry_id} 不存在或已归档")
    tasks = transform_service.list_tasks(entry_id)
    return {"规则id": entry_id, "total": len(tasks), "items": tasks}


@router.get("/{entry_id}/logs")
def list_logs(entry_id: int) -> dict[str, Any]:
    """历史转换日志按原规则编号保留；成功与失败分账，失败日志不覆盖成功日志。"""
    logs, message = transform_service.list_logs(entry_id)
    if logs is None:
        raise HTTPException(status_code=404, detail=message)
    return {"规则id": entry_id, "total": len(logs), "items": logs}
