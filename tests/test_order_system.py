# -*- coding: utf-8 -*-
"""order_system 模块单元测试：种子数据 / 订单查询 / 退货退款边界 / 单号唯一性。

运行：python -m unittest tests.test_order_system -v
零第三方依赖（仅标准库 sqlite3），任何环境可直接跑。
注意：DB 路径必须在 import order_system 前通过环境变量指定为临时文件。
"""
import os
import sys
import tempfile
import unittest

# 模块加载时即建表+种子，必须在 import 前把 DB 指向临时文件
_fd, _tmp_db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["AFTERSALE_ORDER_DB"] = _tmp_db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_system as ods


class OrderSystemTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if os.path.exists(_tmp_db):
            os.remove(_tmp_db)

    def test_seed_orders(self):
        self.assertEqual(len(ods.list_user_orders("alice")), 3)
        self.assertIsNone(ods.get_order("CS00000000000"))

    def test_get_order_with_shipment(self):
        o = ods.get_order("CS20260820001")
        self.assertEqual(o["status"], "delivered")
        self.assertIsNotNone(o.get("shipment"))

    def test_return_only_after_shipped(self):
        # pending 状态不允许退货
        self.assertFalse(ods.create_return_order("CS20260820004", "不想要了")["ok"])
        # shipped 状态允许退货
        r = ods.create_return_order("CS20260820002", "尺码不合适")
        self.assertTrue(r["ok"])
        self.assertEqual(ods.get_order("CS20260820002")["status"], "return_requested")

    def test_refund_amount_boundary(self):
        # 超额拒绝
        self.assertFalse(ods.apply_refund("CS20260820001", 9999.0, "超额测试")["ok"])
        # 正常退款 → 状态 refunded
        r = ods.apply_refund("CS20260820001", 299.0, "正常退款")
        self.assertTrue(r["ok"])
        self.assertEqual(ods.get_order("CS20260820001")["status"], "refunded")

    def test_refund_no_unique(self):
        r1 = ods.apply_refund("CS20260820003", 199.0, "a")
        r2 = ods.apply_refund("CS20260820005", 299.0, "b")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertNotEqual(r1["refund_no"], r2["refund_no"])   # 毫秒+随机后缀防冲突

    def test_invalid_status_rejected(self):
        r = ods.update_order_status("CS20260820003", "非法状态")
        self.assertFalse(r["ok"])

    def test_ownership_check(self):
        # 越权防护：mallory 不能操作/查看 alice 的订单（username 非空时校验归属）
        r = ods.create_return_order("CS20260820001", "测试退货", username="mallory")
        self.assertFalse(r["ok"])
        self.assertIn("不属于", r["error"])
        r = ods.apply_refund("CS20260820001", 100.0, "测试退款", username="mallory")
        self.assertFalse(r["ok"])
        # 查询隔离：非本人查不到（视同不存在，不泄露他人信息）
        self.assertIsNone(ods.get_order("CS20260820001", username="mallory"))
        # 本人可以正常查询
        self.assertIsNotNone(ods.get_order("CS20260820001", username="alice"))
        # username 为空（CLI 演示场景）不校验，保持兼容
        self.assertIsNotNone(ods.get_order("CS20260820001", username=""))


if __name__ == "__main__":
    unittest.main()
