# -*- coding: utf-8 -*-
"""
reporter.py —— 报告生成器（模块 10 核心组件 4）

设计：把 MetricsCollector 快照 + SessionEvaluator 汇总 → 输出三种格式：
- JSON     : 机器可读，可喂给前端/ELK
- Markdown : 人类可读，可贴到 README/PR 说明
- HTML     : 浏览器查看，做演示面板

模块 11（practice）在跑完测试集后调用本组件生成综合报告 → 观测闭环。
"""
import json
from datetime import datetime
from typing import Optional


class ReportGenerator:
    """报告生成器。

    用法：
        rg = ReportGenerator(title="aftersale-agent 观测报告")
        report = rg.build(metrics_snapshot, eval_summary, extra={...})
        rg.save(report, "reports/2026-08-27.json", fmt="json")
        rg.save(report, "reports/2026-08-27.md", fmt="markdown")
        rg.save(report, "reports/2026-08-27.html", fmt="html")
    """

    def __init__(self, title: str = "aftersale-agent 观测评估报告"):
        self.title = title

    # ---------- 组装 ----------
    def build(self, metrics: dict, eval_summary: dict,
              extra: Optional[dict] = None) -> dict:
        """把指标 + 评估汇总 + 额外信息合并成一份报告 dict。"""
        return {
            "title": self.title,
            "generated_at": datetime.now().isoformat(),
            "metrics": metrics,
            "evaluation": eval_summary,
            "extra": extra or {},
        }

    # ---------- 三格式输出 ----------
    @staticmethod
    def to_json(report: dict) -> str:
        """JSON 格式（缩进 2，保证中文可读）。"""
        return json.dumps(report, ensure_ascii=False, indent=2)

    def to_markdown(self, report: dict) -> str:
        """Markdown 格式：标题 + 指标表 + 评估表 + 问题清单。"""
        lines = [f"# {report['title']}", "", f"> 生成时间：{report['generated_at']}", ""]

        # 指标表
        m = report["metrics"]
        lines += ["## 1. 运行指标", "",
                  "| 指标 | 值 |", "|------|-----|"]
        for k, v in m.get("counters", {}).items():
            lines.append(f"| counter:{k} | {v} |")
        for k, v in m.get("timings", {}).items():
            lines.append(f"| latency:{k} | {v} |")
        for k, v in m.get("gauges", {}).items():
            lines.append(f"| gauge:{k} | {v} |")

        # 评估汇总
        ev = report["evaluation"]
        lines += ["", "## 2. 会话评估汇总", "",
                  f"- 会话数：{ev.get('count', 0)}",
                  f"- 平均分：{ev.get('avg_score', 'N/A')}",
                  f"- 通过率：{ev.get('pass_rate', 'N/A')}",
                  ""]
        if ev.get("top_issues"):
            lines += ["### 高频问题", ""]
            for iss, cnt in ev["top_issues"]:
                lines.append(f"- {iss}（{cnt} 次）")
        lines.append("")
        return "\n".join(lines)

    def to_html(self, report: dict) -> str:
        """HTML 格式：内联样式，双击即可在浏览器打开。"""
        md = self.to_markdown(report)
        # 把 markdown 简单换行转 HTML（够演示用，不引第三方库）
        body = md.replace("\n", "<br>").replace("| ", "&nbsp;|&nbsp;")
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{self.title}</title>
<style>
  body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;max-width:900px;margin:20px auto;
        padding:0 20px;color:#1f2d3d;line-height:1.7}}
  h1{{color:#185FA5;border-bottom:2px solid #185FA5;padding-bottom:8px}}
  h2{{color:#0F6E56;margin-top:28px}}
  .box{{background:#f5f7fa;border:1px solid #e3e8ef;border-radius:10px;padding:14px 18px}}
  code{{background:#eef0f4;padding:1px 5px;border-radius:4px}}
</style></head><body>
<div class="box">{body}</div>
</body></html>"""

    # ---------- 落盘 ----------
    @staticmethod
    def save(report: dict, path: str, fmt: str = "json") -> None:
        """把报告写到文件（自动建目录）。fmt: json|markdown|html"""
        import os
        from pathlib import Path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rg = ReportGenerator(report["title"])
        text = {"json": rg.to_json, "markdown": rg.to_markdown, "html": rg.to_html}[fmt](report)
        Path(path).write_text(text, encoding="utf-8")
        print(f"[reporter] 已保存报告 → {path}")
