"""数据加工转换业务规则：状态流转、解析裁剪流水线、转换日志与并发版本控制。

链路按四个环节组织：
1. 解析：把原始记录与映射表达式解析成统一的字段清单，登记缺失字段。
2. 裁剪：仅当规则处于「生效中」时，按字段过滤裁剪出目标字段并产出输出字段快照。
3. 日志落库：成功日志与失败日志分账保存；失败日志只写缺失字段、规则编号、重试位置，
   绝不覆盖成功日志；历史日志始终按原规则编号保留。
4. 订阅读取：只有确认后的输出字段快照才允许被数据订阅详情读取，半成品一律不可见。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "transform"
REQUIRED_FIELDS = ["规则编号", "输入格式", "输出格式"]
STATUS_ORDER = ["草稿", "生效中", "已暂停", "已关闭"]
# 动作驱动规则进入目标状态：启用规则把草稿推进到生效中，之后才允许字段裁剪。
ACTION_RULES = {"启用规则": "生效中", "暂停规则": "已暂停", "关闭规则": "已关闭"}
NEGATIVE_ACTIONS = []

# 只有生效中的规则才允许进入字段裁剪环节。
TRIMMABLE_STATUS = "生效中"
# 快照确认后才允许下游（数据订阅）读取。
SNAPSHOT_CONFIRMED = "已确认"
SNAPSHOT_DRAFT = "待确认"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class TransformService:
    """加工规则与转换任务的领域服务，进程内带锁，保证并发修改只有一个生效版本。"""

    def __init__(self) -> None:
        # 同一进程内的并发请求（FastAPI 线程池）通过这把锁串行化关键区间。
        self._lock = threading.RLock()
        # 任务表：task_id -> 任务文档
        self._tasks: dict[int, dict[str, Any]] = {}
        # 规则级表达式版本锁票据：rule_id -> 当前生效版本号
        self._expression_versions: dict[int, int] = {}
        self._task_seq = 0

    # ------------------------------------------------------------- 规则内部结构兜底
    @staticmethod
    def _ensure_internal_fields(entry: dict[str, Any]) -> None:
        """老数据（如种子示例）没有流水线内部字段，首次接触时补齐，避免 KeyError。"""
        entry.setdefault("待执行步骤", [])
        entry.setdefault("最近加工结果", None)
        entry.setdefault("转换日志归档", [])
        entry.setdefault("输出快照", None)
        entry.setdefault("映射表达式", "")
        entry.setdefault("字段过滤", "")

    def _find_rule(self, entry_id: int) -> dict[str, Any] | None:
        """取规则并顺手补齐内部字段，所有写路径统一走这里。"""
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._ensure_internal_fields(entry)
        return entry

    # ------------------------------------------------------------------ 列表/明细
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        # 列表只回显规则主数据，不夹带某一次任务的残留日志；视图用 .get 兜底，老数据也不会报错。
        page_rows = rows[start:start + size]
        for row in page_rows:
            self._ensure_internal_fields(row)
        view = [self._public_rule_view(row) for row in page_rows]
        return view, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = self._find_rule(entry_id)
        if entry is None:
            return None
        return self._public_rule_view(entry)

    def get_rule(self, entry_id: int) -> dict[str, Any] | None:
        """读取规则完整内部状态（含待执行步骤、快照、加工结果），供服务内/测试使用。"""
        entry = self._find_rule(entry_id)
        return dict(entry) if entry is not None else None

    # 列表/明细视图里不对外暴露的内部流水线字段，避免上一版任务的临时数据残留到页面。
    _INTERNAL_FIELDS = ("转换日志归档", "输出快照", "最近加工结果", "待执行步骤")

    @classmethod
    def _public_rule_view(cls, entry: dict[str, Any]) -> dict[str, Any]:
        """规则视图：转换日志只展示该规则最近一次落库结果，不残留上一版任务的临时日志。"""
        view = {key: value for key, value in entry.items() if key not in cls._INTERNAL_FIELDS}
        logs = entry.get("转换日志归档") or []
        if not logs:
            view["转换日志"] = ""
        else:
            latest = logs[-1]
            if latest.get("结果") == "成功":
                view["转换日志"] = latest["摘要"]
            else:
                # 失败日志只有缺失字段、规则编号、重试位置三类信息，据此拼一句可读摘要。
                missing = "、".join(latest.get("缺失字段") or []) or "无"
                view["转换日志"] = (
                    f"规则 {latest.get('规则编号')} 转换失败："
                    f"缺失字段 {missing}，重试位置 {latest.get('重试位置', 0)}"
                )
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 登记时可顺带带上加工配置；缺省给空口径，后续可通过更新接口补齐。
            for optional in ("映射表达式", "字段过滤", "编码设置"):
                entry[optional] = values.get(optional, "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            # 待执行步骤：规则从草稿进入生效中之前，裁剪步骤不得入队。
            entry["待执行步骤"] = []
            # 最近一次完整加工结果：暂停/关闭后仍然保留。
            entry["最近加工结果"] = None
            # 成功/失败日志分账归档，全部按原规则编号保留，失败日志不覆盖成功日志。
            entry["转换日志归档"] = []
            # 输出字段快照：未确认前下游不可读。
            entry["输出快照"] = None
            self._expression_versions[entry["id"]] = 1
            rows.append(entry)
            return self._public_rule_view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None, f"加工规则 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据加工转换可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if target == TRIMMABLE_STATUS:
                # 草稿 -> 生效中：此时才允许裁剪，把解析/裁剪/落库步骤排入待执行队列。
                entry["待执行步骤"] = ["解析字段", "字段裁剪", "日志落库"]
            elif target in ("已暂停", "已关闭"):
                # 暂停或关闭：必须清空待执行步骤，但保留最近一次完整加工结果与历史日志。
                entry["待执行步骤"] = []
            return self._public_rule_view(entry), f"加工规则已{action}"

    # ---------------------------------------------------------- 映射表达式并发修改
    def update_expression(
        self, entry_id: int, expression: str, field_filter: Any = None, *, expected_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str, int]:
        """修改映射表达式。

        两个请求并发改同一条表达式时，只接受一个生效版本：调用方携带 expected_version
        做乐观并发控制，版本不一致的请求被拒绝；未携带时由服务端在锁内串行取号，
        后到者拿到新版本号而不会覆盖前者已生效的内容。
        返回 (规则视图, 消息, 当前版本号)。
        """
        expression = str(expression or "").strip()
        if not expression:
            return None, "映射表达式不能为空", 0
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None, f"加工规则 {entry_id} 不存在或已归档", 0
            current = int(self._expression_versions.get(entry_id, 1))
            if expected_version is not None and int(expected_version) != current:
                return (
                    None,
                    f"映射表达式已被其他请求更新（当前版本 {current}），本次修改未生效，请基于最新版本重试",
                    current,
                )
            entry["映射表达式"] = expression
            if field_filter is not None:
                entry["字段过滤"] = field_filter
            # 表达式变更后旧快照作废，回到待确认，下游在确认前读不到半成品。
            entry["输出快照"] = None
            current += 1
            self._expression_versions[entry_id] = current
            return self._public_rule_view(entry), "映射表达式已更新为唯一生效版本", current

    def get_expression_version(self, entry_id: int) -> int | None:
        with self._lock:
            if self._find_rule(entry_id) is None:
                return None
            return int(self._expression_versions.get(entry_id, 1))

    # -------------------------------------------------------------- 四环节转换链路
    def submit_task(
        self,
        entry_id: int,
        payload: list[dict[str, Any]] | dict[str, Any],
        *,
        keep_fields: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """提交一次转换任务，依次走解析、裁剪、日志落库，产出待确认的输出快照。

        任务可重复提交：被中断（状态异常、步骤未完成）的任务不会阻塞再次提交。
        """
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None, f"加工规则 {entry_id} 不存在或已归档"

            self._task_seq += 1
            task_id = self._task_seq
            rule_code = str(entry.get("规则编号", f"TRAN-{entry_id:04d}"))
            records = payload if isinstance(payload, list) else [payload]
            task: dict[str, Any] = {
                "id": task_id,
                "规则id": entry_id,
                "规则编号": rule_code,
                "阶段": "已提交",
                "中断": False,
                "重试位置": 0,
                "缺失字段": [],
                "成功": False,
                "输入记录数": len(records),
                "输出记录": None,
                "输出快照状态": None,
                "时间": _now(),
            }
            self._tasks[task_id] = task

            # 环节一：解析。统一把每条记录解析成字段清单，同时登记缺失字段。
            parsed, parse_missing, retry_at = self._parse_stage(entry, records)
            task["缺失字段"] = parse_missing
            if parse_missing:
                # 解析阶段即发现缺字段：裁剪环节拒绝执行，写失败日志后返回。
                task["阶段"] = "字段裁剪"
                task["重试位置"] = retry_at
                self._persist_failure_log(entry, task)
                return dict(task), (
                    f"任务 {rule_code} 在字段裁剪阶段被拒绝：缺失字段 {'、'.join(parse_missing)}"
                )

            # 环节二：裁剪。只有生效中的规则才允许裁剪。
            if entry.get("status") != TRIMMABLE_STATUS:
                task["阶段"] = "字段裁剪"
                task["中断"] = True
                task["缺失字段"] = []
                # 失败日志仍只写三类信息；拒绝原因通过返回消息告知，不进日志。
                self._persist_failure_log(entry, task)
                return dict(task), "字段裁剪被拒绝：规则尚未进入生效中，任务可在生效后再次提交"

            output, trim_missing, retry_at = self._trim_stage(entry, parsed, keep_fields)
            if trim_missing:
                task["阶段"] = "字段裁剪"
                task["重试位置"] = retry_at
                task["缺失字段"] = trim_missing
                self._persist_failure_log(entry, task)
                return dict(task), (
                    f"任务 {rule_code} 在字段裁剪阶段被拒绝：缺失字段 {'、'.join(trim_missing)}"
                )

            # 环节三：日志落库（成功）+ 产出待确认输出快照。
            task["阶段"] = "日志落库"
            task["成功"] = True
            task["输出记录"] = output
            snapshot = {
                "状态": SNAPSHOT_DRAFT,
                "规则编号": rule_code,
                "字段清单": list(output[0].keys()) if output else self._target_fields(entry, keep_fields),
                "记录": output,
                "时间": _now(),
            }
            task["输出快照状态"] = SNAPSHOT_DRAFT
            entry["输出快照"] = snapshot
            entry["最近加工结果"] = {
                "任务id": task_id,
                "规则编号": rule_code,
                "输出记录": copy.deepcopy(output),
                "时间": _now(),
            }
            self._persist_success_log(entry, task)
            task["阶段"] = "待确认"
            return dict(task), f"转换任务已完成解析与裁剪，输出字段快照待确认（任务号 {task_id}）"

    def confirm_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认输出字段快照；确认前数据订阅详情读不到任何半成品输出。"""
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None, f"加工规则 {entry_id} 不存在或已归档"
            snapshot = entry.get("输出快照")
            if not snapshot:
                return None, "没有待确认的输出字段快照，请先提交转换任务"
            snapshot["状态"] = SNAPSHOT_CONFIRMED
            snapshot["确认时间"] = _now()
            for task in self._tasks.values():
                if task["规则id"] == entry_id and task["输出快照状态"] == SNAPSHOT_DRAFT:
                    task["输出快照状态"] = SNAPSHOT_CONFIRMED
                    task["阶段"] = "已确认"
            return self._public_rule_view(entry), "输出字段快照已确认，数据订阅可读取本次加工输出"

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_tasks(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            tasks = list(self._tasks.values())
            if entry_id is not None:
                tasks = [task for task in tasks if task["规则id"] == entry_id]
            return [dict(task) for task in tasks]

    def list_logs(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        """历史转换日志按原规则编号保留，成功与失败分账、失败日志不覆盖成功日志。"""
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None, f"加工规则 {entry_id} 不存在或已归档"
            return list(entry.get("转换日志归档") or []), ""

    # ------------------------------------------------------------------ 内部环节
    @staticmethod
    def _parse_stage(
        entry: dict[str, Any], records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """环节一·解析：按映射表达式校验来源字段是否齐全，缺失时回报对应的目标字段名。"""
        source_to_targets = TransformService._expression_mapping(entry.get("映射表达式", ""))
        missing: list[str] = []
        retry_at = 0
        for index, record in enumerate(records):
            for source, target in source_to_targets.items():
                if source not in record or record[source] in (None, ""):
                    if target not in missing:
                        missing.append(target)
                    retry_at = index
        # 解析后的记录做浅拷贝，避免裁剪污染原始输入。
        return [dict(record) for record in records], missing, retry_at

    @staticmethod
    def _expression_mapping(expression: Any) -> dict[str, str]:
        """解析映射表达式，返回 来源字段 -> 目标字段；没有箭头时来源与目标同名。"""
        if isinstance(expression, dict):
            return {str(source).strip(): str(target).strip() for source, target in expression.items()}
        mapping: dict[str, str] = {}
        if isinstance(expression, (list, tuple)):
            for item in expression:
                name = str(item).strip()
                if name:
                    mapping[name] = name
            return mapping
        tokens = (
            str(expression or "")
            .replace("；", ";")
            .replace("，", ",")
            .replace("、", ",")
            .replace(";", ",")
        )
        for chunk in tokens.split(","):
            token = chunk.strip()
            if not token:
                continue
            if "->" in token:
                source, target = token.split("->", 1)
            elif "=>" in token:
                source, target = token.split("=>", 1)
            else:
                source = target = token
            source, target = source.strip(), target.strip()
            if source and target:
                mapping[source] = target
        return mapping

    @staticmethod
    def _trim_stage(
        entry: dict[str, Any], parsed: list[dict[str, Any]], keep_fields: list[str] | None
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """环节二·裁剪：按字段过滤口径保留目标字段，缺目标字段即拒绝并给出重试位置。"""
        targets = TransformService._target_fields(entry, keep_fields)
        source_of = TransformService._target_source_mapping(entry, targets)
        output: list[dict[str, Any]] = []
        missing: list[str] = []
        retry_at = 0
        for index, record in enumerate(parsed):
            row: dict[str, Any] = {}
            for target in targets:
                source = source_of.get(target, target)
                if source not in record or record[source] in (None, ""):
                    if target not in missing:
                        missing.append(target)
                    retry_at = index
                row[target] = record.get(source)
            output.append(row)
        if missing:
            return [], missing, retry_at
        return output, [], 0

    @staticmethod
    def _target_source_mapping(entry: dict[str, Any], targets: list[str]) -> dict[str, str]:
        """根据映射表达式得到 目标字段 -> 来源字段 的对照，供裁剪阶段取值与缺字段判断。"""
        source_to_target = TransformService._expression_mapping(entry.get("映射表达式"))
        # 先按表达式显式建立目标到来源的对照，再为未覆盖的目标补同名字段。
        mapping: dict[str, str] = {}
        for source, target in source_to_target.items():
            mapping[target] = source
        for target in targets:
            mapping.setdefault(target, target)
        return mapping

    @staticmethod
    def _target_fields(entry: dict[str, Any], keep_fields: list[str] | None) -> list[str]:
        if keep_fields:
            return [str(field).strip() for field in keep_fields if str(field).strip()]
        configured = entry.get("字段过滤")
        if isinstance(configured, (list, tuple)) and configured:
            return [str(field).strip() for field in configured if str(field).strip()]
        if isinstance(configured, str) and configured.strip():
            return [part.strip() for part in configured.replace("；", ";").replace("，", ",").replace("、", ",").replace(";", ",").split(",") if part.strip()]
        # 没有显式裁剪口径时，退化为映射表达式声明的目标字段。
        expression = entry.get("映射表达式")
        if isinstance(expression, dict) and expression:
            return [str(value).strip() for value in expression.values()]
        targets: list[str] = []
        for token in str(expression or "").replace("；", ";").replace("，", ",").replace("、", ",").replace(";", ",").split(","):
            token = token.strip()
            if "->" in token:
                token = token.split("->", 1)[1].strip()
            elif "=>" in token:
                token = token.split("=>", 1)[1].strip()
            if token and token not in targets:
                targets.append(token)
        return targets

    def _persist_success_log(self, entry: dict[str, Any], task: dict[str, Any]) -> None:
        """环节三·成功日志落库：独立追加，失败日志永远不会覆盖它。"""
        log = {
            "结果": "成功",
            "规则编号": task["规则编号"],
            "任务id": task["id"],
            "摘要": (
                f"[{_now()}] 规则 {task['规则编号']} 任务 {task['id']} 转换成功："
                f"输入 {task['输入记录数']} 条，输出 {len(task['输出记录'] or [])} 条，输出字段快照待确认"
            ),
            "时间": _now(),
        }
        entry.setdefault("转换日志归档", []).append(log)
        entry["转换日志"] = log["摘要"]

    def _persist_failure_log(self, entry: dict[str, Any], task: dict[str, Any]) -> None:
        """环节三·失败日志落库。

        严格只写三类信息：缺失字段、规则编号、重试位置；单独追加到失败分账，
        不触碰任何已存在的成功日志。任务号与时间仅用于归档定位，不属于加工结果。
        """
        failure_log = {
            "结果": "失败",
            # 只允许这三个关键信息落库。
            "规则编号": task["规则编号"],
            "缺失字段": list(task.get("缺失字段") or []),
            "重试位置": int(task.get("重试位置", 0)),
            "任务id": task["id"],
            "时间": _now(),
        }
        entry.setdefault("转换日志归档", []).append(failure_log)
        # 列表页的「转换日志」列回显失败摘要，但成功日志原文仍完整保留在归档里。
        missing_text = "、".join(failure_log["缺失字段"]) or "无"
        entry["转换日志"] = (
            f"[{_now()}] 规则 {task['规则编号']} 任务 {task['id']} 转换失败："
            f"缺失字段 {missing_text}，重试位置 {failure_log['重试位置']}"
        )

    # ------------------------------------------------------------- 供订阅读取的快照
    def readable_output(self, entry_id: int) -> dict[str, Any] | None:
        """环节四·订阅读取：只返回已确认快照；未确认/无快照时返回 None（由上游呈现空态）。"""
        with self._lock:
            entry = self._find_rule(entry_id)
            if entry is None:
                return None
            snapshot = entry.get("输出快照")
            if not snapshot or snapshot.get("状态") != SNAPSHOT_CONFIRMED:
                return None
            return copy.deepcopy(snapshot)


# 单例：路由层共享同一份任务表与并发锁。
transform_service = TransformService()
