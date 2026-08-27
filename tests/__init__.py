# -*- coding: utf-8 -*-
"""aftersale-agent 单元测试包。

运行全部测试：
    python -m unittest discover -s tests -v

说明：
- test_order_system : 零依赖（标准库），任何环境可跑
- test_auth / test_rate_limit : 需 fastapi + PyJWT + pydantic
- test_faq : 需 langgraph/langchain（完整依赖）
缺失依赖的模块会自动跳过（skip），不阻塞其余测试。
"""
