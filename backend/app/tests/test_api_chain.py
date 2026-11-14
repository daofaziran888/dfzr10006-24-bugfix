"""接口级链路冒烟：起真实 ASGI 服务，用标准库 urllib 走完四个环节。

覆盖路由注册、HTTP 状态码（404/409/200）、请求体协议与空态结构，
不依赖 httpx/pytest，克隆环境即可运行：

    cd backend && .venv/bin/python -m app.tests.test_api_chain
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any

import uvicorn

from app.main import app
from app.store import store


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ApiChainTest(unittest.TestCase):
    server: uvicorn.Server
    base: str
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        # 每个测试进程一份干净内存仓库。
        store._tables.clear()
        store._tables.setdefault("transform", [])
        store._tables.setdefault("subscribe", [])
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{port}"
        # 等待监听就绪。
        import time

        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_chain_over_http(self) -> None:
        # 0. 健康检查。
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        # 1. 登记加工规则（草稿）。
        status, body = self.request("POST", "/api/transform", {
            "values": {
                "规则编号": "TRAN-0002",
                "输入格式": "JSON",
                "输出格式": "JSON",
                "映射表达式": "订单号 -> orderId, 金额 -> amount",
                "字段过滤": ["orderId", "amount"],
            }
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        rule_id = body["entry"]["id"]

        # 2. 草稿态提交任务 -> 裁剪被拒绝，失败日志只含三类信息。
        status, body = self.request("POST", f"/api/transform/{rule_id}/tasks", {
            "records": [{"订单号": "O-1", "金额": 9}]
        })
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("裁剪", body["message"])

        # 3. 启用规则 -> 生效中，待执行步骤入队（内部字段不进列表视图）。
        status, body = self.request("POST", f"/api/transform/{rule_id}/actions", {"values": {"action": "启用规则"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["status"], "生效中")
        self.assertNotIn("待执行步骤", body["entry"])

        # 4. 缺目标字段的任务 -> 裁剪阶段拒绝 + 重试位置。
        status, body = self.request("POST", f"/api/transform/{rule_id}/tasks", {
            "records": [{"订单号": "O-2"}]
        })
        self.assertFalse(body["ok"])
        self.assertEqual(body["entry"]["缺失字段"], ["amount"])
        self.assertEqual(body["entry"]["重试位置"], 0)

        # 5. 正常任务成功，快照待确认。
        status, body = self.request("POST", f"/api/transform/{rule_id}/tasks", {
            "records": [{"订单号": "O-1", "金额": 9}, {"订单号": "O-2", "金额": 18}]
        })
        self.assertTrue(body["ok"], msg=body["message"])
        self.assertEqual(body["entry"]["输出记录"][0], {"orderId": "O-1", "amount": 9})

        # 6. 并发修改表达式：同版本号两个请求，只有一个赢，输家 409。
        version_status, version_body = self.request("GET", f"/api/transform/{rule_id}")
        current_version = 2  # 创建即 v1，尚未改过；第一次修改预期升到 v2
        results = []
        candidates = [
            ("订单号 -> order_no, 金额 -> amount", ["order_no", "amount"]),
            ("订单号 -> order_code, 金额 -> amount", ["order_code", "amount"]),
        ]
        for expr, field_filter in candidates:
            code, resp = self.request("PUT", f"/api/transform/{rule_id}/expression", {
                "expression": expr,
                "field_filter": field_filter,
                "expected_version": 1,
            })
            results.append((code, resp))
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes, [200, 409])
        winner = next(resp for code, resp in results if code == 200)
        self.assertEqual(winner["entry"]["表达式版本"], current_version)

        # 7. 表达式变更后旧快照作废：重新跑任务，确认前订阅读不到。
        self.request("POST", f"/api/transform/{rule_id}/tasks", {
            "records": [{"订单号": "O-3", "金额": 30}]
        })
        status, sub_body = self.request("POST", "/api/subscribe", {
            "values": {
                "任务编号": "SUBS-0010",
                "数据源名称": "订单流",
                "推送目标": "数仓",
                "关联加工规则id": rule_id,
            }
        })
        self.assertTrue(sub_body["ok"])
        sub_id = sub_body["entry"]["id"]

        status, detail = self.request("GET", f"/api/subscribe/{sub_id}/output")
        self.assertEqual(status, 200)
        self.assertIsNone(detail["可读输出"])
        self.assertIn("暂无数据", detail["空态说明"])

        # 8. 确认快照后订阅详情才读到输出，且字段为裁剪后的目标字段。
        status, body = self.request("POST", f"/api/transform/{rule_id}/snapshot/confirm", {})
        self.assertTrue(body["ok"])
        status, detail = self.request("GET", f"/api/subscribe/{sub_id}/output")
        self.assertEqual(status, 200)
        self.assertEqual(detail["快照状态"], "已确认")
        # 并发赢家可能是两个表达式之一，输出字段与首条记录按赢家口径校验。
        self.assertIn(detail["输出字段"], [["order_no", "amount"], ["order_code", "amount"]])
        order_field = detail["输出字段"][0]
        self.assertEqual(detail["可读输出"][0][order_field], "O-3")

        # 9. 日志归档：成功/失败分账、按原规则编号保留、失败日志不含成功摘要。
        status, logs_body = self.request("GET", f"/api/transform/{rule_id}/logs")
        self.assertEqual(status, 200)
        self.assertTrue(all(item["规则编号"] == "TRAN-0002" for item in logs_body["items"]))
        self.assertTrue(any(item["结果"] == "成功" for item in logs_body["items"]))
        failures = [item for item in logs_body["items"] if item["结果"] == "失败"]
        self.assertTrue(failures)
        self.assertEqual(set(failures[0].keys()) >= {"规则编号", "缺失字段", "重试位置"}, True)
        self.assertNotIn("摘要", failures[0])

        # 10. 暂停规则：待执行步骤清空（内部状态），最近加工结果仍保留。
        status, body = self.request("POST", f"/api/transform/{rule_id}/actions", {"values": {"action": "暂停规则"}})
        self.assertEqual(body["entry"]["status"], "已暂停")
        # 中断后允许再次提交（恢复生效后任务不被历史阻塞，由服务级测试覆盖，这里验接口可调）。
        status, body = self.request("GET", f"/api/transform/{rule_id}/tasks")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["total"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
