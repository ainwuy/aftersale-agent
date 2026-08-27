# -*- coding: utf-8 -*-
"""
run_demo.py —— 模块 11 综合实战一键入口

用法：
    python -m practice.run_demo             # 桩 LLM（无需 API key，验证流程）
    python -m practice.run_demo --real      # 真实 qwen3.7-plus（需要 .env 里 QWEN key）

流程：跑 benchmark 测试集 → 跑 orchestrator 模拟会话 → 输出 JSON/MD/HTML 报告
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="aftersale-agent 综合实战（模块 11）")
    parser.add_argument("--real", action="store_true",
                        help="用真实 qwen3.7-plus（需 QWEN key）；默认桩 LLM")
    parser.add_argument("--cases", type=int, default=5,
                        help="benchmark 测试集条数（默认 5）")
    args = parser.parse_args()

    # 加载 .env（若有 QWEN key）
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except Exception:
        pass

    llm = None
    if args.real:
        from langchain_openai import ChatOpenAI
        key = os.getenv("QWEN")
        if not key:
            print("❌ --real 需要 QWEN 环境变量（.env 里配置 DashScope key）")
            sys.exit(1)
        llm = ChatOpenAI(
            model="qwen3.7-plus",
            api_key=key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.3,
        )
        print("✅ 使用真实 qwen3.7-plus")
    else:
        print("ℹ️ 使用桩 LLM（无需 API key）——仅验证流程，回答为固定模板")

    # === 1) benchmark：测试集批量跑 ===
    from practice.benchmark import BenchmarkRunner
    runner = BenchmarkRunner(llm=llm)
    runner.run_all()
    md_path = runner.report()

    # === 2) orchestrator：模拟会话（含 reject/approve 分支）===
    from practice.orchestrator import PracticeOrchestrator
    orch = PracticeOrchestrator(llm=llm)
    orch.run_simulated_sessions()
    html_path = orch.generate_report("reports/practice.html")

    print("\n" + "=" * 50)
    print("✅ 模块 11 综合实战完成")
    print(f"   benchmark 报告: {md_path}")
    print(f"   orchestrator 报告: {html_path}")
    print("=" * 50)


if __name__ == "__main__":
    main()
