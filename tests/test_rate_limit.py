# -*- coding: utf-8 -*-
"""rate_limit 模块单元测试：滑动窗口限流核心逻辑（不依赖 FastAPI 请求链路）。

运行：python -m unittest tests.test_rate_limit -v
依赖：fastapi（缺失时测试自动跳过）。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from rate_limit import RateLimiter
    HAS_DEPS = True
    IMPORT_ERROR = ""
except ImportError as e:
    RateLimiter = None
    HAS_DEPS = False
    IMPORT_ERROR = str(e)


@unittest.skipUnless(HAS_DEPS, f"依赖未安装（fastapi），跳过：{IMPORT_ERROR}")
class RateLimiterTest(unittest.TestCase):
    def test_window_allows_up_to_limit(self):
        rl = RateLimiter(limit=3, window=60)
        for _ in range(3):
            ok, remaining, wait = rl.allow("k1")
            self.assertTrue(ok)
        ok, remaining, wait = rl.allow("k1")   # 第 4 次被拒
        self.assertFalse(ok)
        self.assertEqual(remaining, 0)
        self.assertGreater(wait, 0)

    def test_window_expiry(self):
        rl = RateLimiter(limit=2, window=1)
        self.assertTrue(rl.allow("k2")[0])
        self.assertTrue(rl.allow("k2")[0])
        self.assertFalse(rl.allow("k2")[0])
        time.sleep(1.1)                        # 窗口过期 → 配额恢复
        self.assertTrue(rl.allow("k2")[0])

    def test_keys_isolated(self):
        rl = RateLimiter(limit=5, window=60)
        rl.allow("u1")
        rl.allow("u1")
        self.assertEqual(rl.remaining("u1"), 3)
        self.assertEqual(rl.remaining("u2"), 5)   # 不同 key 互不影响

    def test_remaining_does_not_consume(self):
        rl = RateLimiter(limit=5, window=60)
        self.assertEqual(rl.remaining("u3"), 5)
        self.assertEqual(rl.remaining("u3"), 5)   # 查询不消耗配额


if __name__ == "__main__":
    unittest.main()
