"""转换链路端到端校验：解析 -> 裁剪 -> 日志落库 -> 订阅读取。

只用标准库，直接跑领域服务，覆盖任务里的每条约束：
1. 规则从草稿进入生效中后才允许裁剪；
2. 暂停/关闭清空待执行步骤但保留最近完整加工结果；
3. 失败日志只写缺失字段、规则编号、重试位置，且不覆盖成功日志；
4. 并发修改同一映射表达式只接受一个生效版本；
5. 输出快照未确认前，订阅详情读不到半成品，只读已确认快照；
6. 历史转换日志按原规则编号保留；
7. 无可读输出时给空态与「暂无数据」说明；
8. 任务受中断影响后可以再次提交。

运行：cd backend && .venv/bin/python -m app.tests.test_transform_chain
"""
from __future__ import annotations

import threading
import unittest

from app.services.subscribe import EMPTY_OUTPUT_HINT, SubscribeService
from app.services.transform import (
    SNAPSHOT_CONFIRMED,
    SNAPSHOT_DRAFT,
    TRIMMABLE_STATUS,
    TransformService,
)
from app.store import store


class TransformChainTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都用全新的服务与内存仓库，互不污染。
        self.service = TransformService()
        self.subscribe = SubscribeService()
        store._tables.clear()
        store._tables.setdefault("transform", [])
        store._tables.setdefault("subscribe", [])
        rule, missing = self.service.create_entry(
            {
                "规则编号": "TRAN-0002",
                "输入格式": "JSON",
                "输出格式": "JSON",
                "映射表达式": "订单号 -> orderId, 客户名 -> customer, 金额 -> amount",
                "字段过滤": ["orderId", "customer", "amount"],
            }
        )
        self.assertEqual(missing, [])
        self.rule_id = rule["id"]

        self.records = [
            {"订单号": "O-1", "客户名": "张三", "金额": 100, "内部备注": "x"},
            {"订单号": "O-2", "客户名": "李四", "金额": 200, "内部备注": "y"},
        ]

    # ------------------------------------------------ 1. 草稿态裁剪必须被拒绝
    def test_draft_rule_cannot_trim(self) -> None:
        rule = self.service.get_rule(self.rule_id)
        self.assertEqual(rule["status"], "草稿")
        self.assertEqual(rule["待执行步骤"], [])

        task, message = self.service.submit_task(self.rule_id, self.records)
        self.assertIsNotNone(task)
        self.assertFalse(task["成功"])
        self.assertTrue(task["中断"])
        self.assertIn("裁剪", message)
        # 拒绝时不允许产出任何半成品快照。
        self.assertIsNone(self.service.get_rule(self.rule_id)["输出快照"])

    # ------------------------------------------------ 2. 生效后才允许裁剪并产出快照
    def test_active_rule_trims_to_target_fields(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        rule = self.service.get_rule(self.rule_id)
        self.assertEqual(rule["status"], TRIMMABLE_STATUS)
        self.assertEqual(rule["待执行步骤"], ["解析字段", "字段裁剪", "日志落库"])

        task, message = self.service.submit_task(self.rule_id, self.records)
        self.assertTrue(task["成功"], msg=message)
        self.assertEqual(task["阶段"], "待确认")
        # 裁剪口径生效：内部备注被去掉，只保留目标字段。
        self.assertEqual(
            task["输出记录"],
            [
                {"orderId": "O-1", "customer": "张三", "amount": 100},
                {"orderId": "O-2", "customer": "李四", "amount": 200},
            ],
        )
        snapshot = self.service.get_rule(self.rule_id)["输出快照"]
        self.assertEqual(snapshot["状态"], SNAPSHOT_DRAFT)

    # ------------------------------------------------ 3. 缺字段 -> 裁剪阶段拒绝 + 失败日志
    def test_missing_field_rejected_and_failure_log_shape(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        bad_records = [
            {"订单号": "O-1", "客户名": "张三"},  # 缺「金额」，重试位置应定位到第 0 条
        ]
        task, message = self.service.submit_task(self.rule_id, bad_records)
        self.assertFalse(task["成功"])
        self.assertIn("字段裁剪阶段被拒绝", message)
        self.assertEqual(task["缺失字段"], ["amount"])
        self.assertEqual(task["重试位置"], 0)

        logs, _ = self.service.list_logs(self.rule_id)
        failure = [log for log in logs if log["结果"] == "失败"]
        self.assertEqual(len(failure), 1)
        # 失败日志只允许三类关键信息（规则编号、缺失字段、重试位置）+ 任务定位标识。
        self.assertEqual(failure[0]["规则编号"], "TRAN-0002")
        self.assertEqual(failure[0]["缺失字段"], ["amount"])
        self.assertEqual(failure[0]["重试位置"], 0)
        self.assertEqual(
            set(failure[0].keys()),
            {"结果", "规则编号", "缺失字段", "重试位置", "任务id", "时间"},
        )
        for forbidden in ("输出记录", "成功摘要", "堆栈", "原因"):
            self.assertNotIn(forbidden, failure[0])

    # ------------------------------------------------ 4. 失败日志不得覆盖成功日志
    def test_failure_log_never_overwrites_success_log(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        ok_task, ok_msg = self.service.submit_task(self.rule_id, self.records)
        self.assertTrue(ok_task["成功"], msg=ok_msg)
        self.service.confirm_snapshot(self.rule_id)

        # 再提交一份缺字段的失败任务。
        bad_task, _ = self.service.submit_task(self.rule_id, [{"订单号": "O-9"}])
        self.assertFalse(bad_task["成功"])

        logs, _ = self.service.list_logs(self.rule_id)
        successes = [log for log in logs if log["结果"] == "成功"]
        failures = [log for log in logs if log["结果"] == "失败"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        # 成功日志原文完整保留，顺序上先成功后失败。
        self.assertEqual(logs[0]["结果"], "成功")
        self.assertEqual(logs[1]["结果"], "失败")
        self.assertIn("转换成功", successes[0]["摘要"])
        # 最近加工结果仍是上一次完整成功结果，不被失败任务冲掉。
        latest = self.service.get_rule(self.rule_id)["最近加工结果"]
        self.assertEqual(len(latest["输出记录"]), 2)

    # ------------------------------------------------ 5. 暂停/关闭清步骤但保留加工结果
    def test_pause_and_close_clear_steps_but_keep_result(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        task, msg = self.service.submit_task(self.rule_id, self.records)
        self.assertTrue(task["成功"], msg=msg)
        kept_result = self.service.get_rule(self.rule_id)["最近加工结果"]

        self.service.run_action(self.rule_id, "暂停规则")
        paused = self.service.get_rule(self.rule_id)
        self.assertEqual(paused["status"], "已暂停")
        self.assertEqual(paused["待执行步骤"], [])
        self.assertEqual(paused["最近加工结果"], kept_result)

        self.service.run_action(self.rule_id, "关闭规则")
        closed = self.service.get_rule(self.rule_id)
        self.assertEqual(closed["status"], "已关闭")
        self.assertEqual(closed["待执行步骤"], [])
        self.assertEqual(closed["最近加工结果"], kept_result)
        # 历史日志同样保留。
        logs, _ = self.service.list_logs(self.rule_id)
        self.assertTrue(any(log["结果"] == "成功" for log in logs))

    # ------------------------------------------------ 6. 并发改表达式只接受一个生效版本
    def test_concurrent_expression_update_single_winner(self) -> None:
        version = self.service.get_expression_version(self.rule_id)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[bool, str, int]] = []

        def worker(expr: str) -> None:
            barrier.wait()  # 尽量让两个请求同时进入临界区。
            _entry, message, ver = self.service.update_expression(
                self.rule_id, expr, expected_version=version
            )
            outcomes.append((_entry is not None, message, ver))

        t1 = threading.Thread(target=worker, args=("订单号 -> orderId",))
        t2 = threading.Thread(target=worker, args=("订单号 -> order_no",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [item for item in outcomes if item[0]]
        losers = [item for item in outcomes if not item[0]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("已被其他请求更新", losers[0][1])
        # 生效版本只可能是两版表达式中的一个，且版本号严格递增一次。
        self.assertEqual(winners[0][2], version + 1)
        final_rule = self.service.get_rule(self.rule_id)
        self.assertIn(final_rule["映射表达式"], ("订单号 -> orderId", "订单号 -> order_no"))
        # 表达式变更后旧快照作废，下游在确认前读不到。
        self.assertIsNone(final_rule["输出快照"])

    def test_serial_updates_each_get_new_version(self) -> None:
        v0 = self.service.get_expression_version(self.rule_id)
        _e, _m, v1 = self.service.update_expression(self.rule_id, "a -> A", expected_version=v0)
        self.assertEqual(v1, v0 + 1)
        _e, _m, v2 = self.service.update_expression(self.rule_id, "b -> B", expected_version=v1)
        self.assertEqual(v2, v1 + 1)

    # ------------------------------------------------ 7. 快照未确认前订阅读不到半成品
    def test_subscribe_blocks_unconfirmed_snapshot(self) -> None:
        sub, _ = self.subscribe.create_entry(
            {"任务编号": "SUBS-0010", "数据源名称": "订单流", "推送目标": "数仓",
             "关联加工规则id": self.rule_id}
        )
        sub_id = sub["id"]

        # 还没有任何输出：空态 + 暂无数据说明。
        detail, msg = self.subscribe.read_output(sub_id)
        self.assertEqual(msg, "")
        self.assertIsNone(detail["可读输出"])
        self.assertEqual(detail["输出字段"], [])
        self.assertEqual(detail["空态说明"], EMPTY_OUTPUT_HINT)
        self.assertTrue(detail["可再次提交"])

        self.service.run_action(self.rule_id, "启用规则")
        self.service.submit_task(self.rule_id, self.records)
        # 快照仍是待确认：订阅详情必须继续呈现空态，不能泄露半成品字段。
        detail, _ = self.subscribe.read_output(sub_id)
        self.assertIsNone(detail["可读输出"])
        self.assertEqual(detail["空态说明"], EMPTY_OUTPUT_HINT)

        self.service.confirm_snapshot(self.rule_id)
        detail, _ = self.subscribe.read_output(sub_id)
        self.assertEqual(detail["快照状态"], SNAPSHOT_CONFIRMED)
        self.assertEqual(detail["输出字段"], ["orderId", "customer", "amount"])
        self.assertEqual(detail["可读输出"][0]["orderId"], "O-1")
        self.assertEqual(detail["空态说明"], "")
        self.assertEqual(detail["来源规则编号"], "TRAN-0002")

    # ------------------------------------------------ 8. 历史日志按原规则编号保留
    def test_history_logs_keep_original_rule_code(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        self.service.submit_task(self.rule_id, self.records)
        self.service.submit_task(self.rule_id, [{"客户名": "王五", "金额": 1}])  # 失败任务
        logs, _ = self.service.list_logs(self.rule_id)
        self.assertGreaterEqual(len(logs), 2)
        self.assertTrue(all(log["规则编号"] == "TRAN-0002" for log in logs))

    # ------------------------------------------------ 9. 中断任务后可再次提交
    def test_interrupted_task_can_resubmit(self) -> None:
        # 草稿态提交 -> 中断。
        task, _ = self.service.submit_task(self.rule_id, self.records)
        self.assertTrue(task["中断"])

        # 生效后再次提交同一批数据，应成功，不被历史中断阻塞。
        self.service.run_action(self.rule_id, "启用规则")
        retry_task, retry_msg = self.service.submit_task(self.rule_id, self.records)
        self.assertTrue(retry_task["成功"], msg=retry_msg)
        self.assertFalse(retry_task["中断"])

    # ------------------------------------------------ 10. 列表页不残留上一版转换日志
    def test_list_view_has_no_stale_task_log(self) -> None:
        self.service.run_action(self.rule_id, "启用规则")
        self.service.submit_task(self.rule_id, self.records)
        items, total = self.service.list_entries()
        self.assertEqual(total, 1)
        view = items[0]
        # 列表项只带最近一条日志摘要，不携带任务级临时数据。
        self.assertNotIn("输出快照", view)
        self.assertNotIn("缺失字段", view)
        self.assertIn("转换成功", view["转换日志"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
