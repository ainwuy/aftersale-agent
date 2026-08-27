# -*- coding: utf-8 -*-
"""
metrics.py —— 指标采集器（模块 10 核心组件 1）

设计：线程安全的"计数器 + 耗时分布"采集器，供会话全链路打点。
- counter(name)         : 累计计数（请求数、成功/失败数、token 数）
- timing(name, seconds) : 记录一次耗时，维护分布（min/max/avg/p50/p95/p99）
- gauge(name, value)    : 瞬时值（当前连接数、队列长度）
- snapshot()            : 导出当前所有指标（供报告/日志/API 输出）

为什么用 dict + lock 而不是外部库：零第三方依赖，演示观测原理；
生产可替换为 Prometheus client（接口对齐，改动小）。
"""
import threading
import time
import statistics
from typing import Dict, List


class MetricsCollector:
    """线程安全指标采集器。

    用法：
        mc = MetricsCollector()
        mc.counter("requests")                    # 计数 +1
        mc.counter("tokens", 128)                 # 计数 +128
        mc.timing("latency", 0.42)                # 记录一次耗时（秒）
        mc.gauge("connected_clients", 3)          # 设置瞬时值
        mc.snapshot()                             # 导出全部指标
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._timings: Dict[str, List[float]] = {}
        self._gauges: Dict[str, float] = {}

    # ---------- 三类打点 ----------
    def counter(self, name: str, delta: int = 1) -> None:
        """累计计数器：请求数、错误数、token 消耗等。"""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + delta

    def timing(self, name: str, seconds: float) -> None:
        """记录一次耗时（秒），追加进分布列表。"""
        with self._lock:
            self._timings.setdefault(name, []).append(seconds)

    def gauge(self, name: str, value: float) -> None:
        """瞬时值：当前连接数、队列长度等。"""
        with self._lock:
            self._gauges[name] = value

    # ---------- 汇总统计 ----------
    @staticmethod
    def _percentile(sorted_vals: List[float], p: float) -> float:
        """计算百分位（p=0.95 表示 95% 请求耗时 ≤ 该值）。"""
        if not sorted_vals:
            return 0.0
        idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
        return sorted_vals[idx]

    def _timing_stats(self, name: str) -> dict:
        """把耗时列表压缩成统计摘要（min/avg/p50/p95/p99/max）。"""
        vals = sorted(self._timings.get(name, []))
        if not vals:
            return {"count": 0}
        return {
            "count": len(vals),
            "min": round(vals[0], 4),
            "avg": round(statistics.mean(vals), 4),
            "p50": round(self._percentile(vals, 0.50), 4),
            "p95": round(self._percentile(vals, 0.95), 4),
            "p99": round(self._percentile(vals, 0.99), 4),
            "max": round(vals[-1], 4),
        }

    def snapshot(self) -> dict:
        """导出全部指标（线程安全地加锁拷贝）。"""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "timings": {k: self._timing_stats(k) for k in self._timings},
                "gauges": dict(self._gauges),
            }

    # ---------- 派生指标（报告常用）----------
    def throughput(self, window_seconds: float = 60.0) -> dict:
        """计算 QPS / 成功率 / 平均耗时等派生指标。

        前提：counter 里登记了 requests / errors / tokens 三类键。
        """
        snap = self.snapshot()
        reqs = snap["counters"].get("requests", 0)
        errs = snap["counters"].get("errors", 0)
        toks = snap["counters"].get("tokens", 0)
        lat = snap["timings"].get("latency", {})
        return {
            "requests": reqs,
            "qps": round(reqs / window_seconds, 2) if window_seconds else 0,
            "error_rate": round(errs / reqs, 4) if reqs else 0.0,
            "tokens_total": toks,
            "latency": lat,
        }


# 全局单例：供 practice 包和 api_server 直接 import 使用
GLOBAL_METRICS = MetricsCollector()
