# -*- coding: utf-8 -*-
"""
logger.py —— 结构化 JSON 日志（模块 10 核心组件 2）

设计：一行一个 JSON 的日志（机器可读、可被 ELK/Loki 等采集），
同时保留人类可读的控制台输出。

日志级别：DEBUG < INFO < WARNING < ERROR < CRITICAL
用法：
    log = setup_logger("aftersale")
    log.info("session_start", session_id="s1", category="退货")
    log.warning("session_rejected", session_id="s1", reason="成稿过长")
"""
import json
import logging
import sys
import time
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """把 log record 格式化成单行 JSON。

    record 里除标准字段外，调用时传入的 extra kwargs（如 session_id）
    会被 JSON 化后合并进输出。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),      # 事件名，如 "session_start"
            "file": record.filename,
            "line": record.lineno,
        }
        # extra kwargs 合并进来（logger.info("x", extra={...})）
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def _json_extras(extra: dict) -> dict:
    """把调用方传的 extra 包一层，供 JsonFormatter 读取。"""
    return {"extra_fields": extra}


def setup_logger(name: str = "aftersale", log_dir: str = "logs",
                 level: int = logging.INFO) -> logging.Logger:
    """创建结构化 JSON 日志器（文件 + 控制台双输出）。

    参数：
        name    : logger 名（不同模块用不同名便于过滤）
        log_dir : 日志文件目录（自动创建）
        level   : 日志级别
    返回：
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # 防重复添加 handler（热重载/多次调用场景）
    if logger.handlers:
        return logger

    # 1) 文件 handler：JSON 格式，一行一条
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    logger.addHandler(fh)

    # 2) 控制台 handler：简化格式，人类可读
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(sh)

    return logger


# 便捷包装：让调用方写 log.info("event", session_id="x") 而不是传 extra 字典
def event_logger(logger: logging.Logger):
    """返回一个包装函数 ev(logger, event, **fields)，简化调用。"""
    def ev(event: str, **fields):
        logger.info(event, extra=_json_extras(fields))
    return ev
