"""数据订阅推送业务规则：状态流转、字段校验与加工输出读取口径。

订阅读取环节（转换链路第四环）的约束：
- 数据订阅详情只能读取加工规则「已确认」的输出字段快照；
- 输出字段快照未确认前，任何半成品都不得出现在订阅详情里；
- 没有可读输出时返回空态结构与「暂无数据」说明，而不是报错或返回半成品；
- 转换任务受中断影响时，订阅侧允许重新拉取/再次提交，不做硬失败。
"""
from __future__ import annotations

from typing import Any

from app.services.transform import transform_service
from app.store import store

MODULE = "subscribe"
REQUIRED_FIELDS = ["任务编号", "数据源名称", "推送目标"]
STATUS_ORDER = ["草稿", "推送中", "已暂停", "已完成"]
ACTION_RULES = {"启动推送": "草稿", "暂停推送": "推送中", "归档任务": "已暂停"}
NEGATIVE_ACTIONS = []

EMPTY_OUTPUT_HINT = "暂无数据：关联加工规则尚未产出已确认的输出字段快照"


class SubscribeService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 可关联一条加工规则作为输出来源；未关联时详情输出天然为空态。
        entry["关联加工规则id"] = values.get("关联加工规则id")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据订阅推送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"订阅任务已{action}"

    # ------------------------------------------------------------- 环节四：订阅读取
    def read_output(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """读取订阅详情里的加工输出。

        返回 (详情文档, 消息)。详情文档始终带 ``可读输出`` 与 ``空态说明`` 两个键：
        快照未确认或不存在时 ``可读输出`` 为 None，由前端渲染空态，绝不暴露半成品。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅任务 {entry_id} 不存在或已归档"

        rule_id = entry.get("关联加工规则id")
        detail = dict(entry)
        snapshot = transform_service.readable_output(int(rule_id)) if rule_id else None

        if snapshot is None:
            # 未确认/无快照/未关联规则：统一空态，不泄露半成品字段。
            detail["可读输出"] = None
            detail["输出字段"] = []
            detail["空态说明"] = EMPTY_OUTPUT_HINT
            detail["可再次提交"] = True
            return detail, ""

        detail["可读输出"] = snapshot.get("记录", [])
        detail["输出字段"] = snapshot.get("字段清单", [])
        detail["快照状态"] = snapshot.get("状态")
        detail["来源规则编号"] = snapshot.get("规则编号")
        detail["空态说明"] = ""
        detail["可再次提交"] = False
        return detail, ""
