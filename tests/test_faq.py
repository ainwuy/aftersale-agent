# -*- coding: utf-8 -*-
"""cs_supervisor 的 FAQ 解析与分类清洗测试。

运行：python -m unittest tests.test_faq -v
依赖 langgraph/langchain 等（缺失时自动跳过，不会阻塞测试套件）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cs_supervisor as cs
    HAS_DEPS = True
except ImportError as e:
    cs = None
    HAS_DEPS = False
    IMPORT_ERROR = str(e)


@unittest.skipUnless(HAS_DEPS, f"依赖未安装（langgraph/langchain），跳过：{IMPORT_ERROR}")
class FaqTest(unittest.TestCase):
    def test_clean_cat(self):
        # 序号 + 高频标注（原始 FAQ 标题）
        self.assertEqual(cs._clean_cat("四、退换货与售后类（高频）"), "退换货与售后")
        # 自动补全条目（quality_inspector 追加的格式）→ 也应清洗回标准大类
        self.assertEqual(cs._clean_cat("退换货与售后类（自动补全）"), "退换货与售后")
        self.assertEqual(cs._clean_cat("投诉与维权类（自动补全）"), "投诉与维权")
        # 无标注
        self.assertEqual(cs._clean_cat("一、售前咨询类"), "售前咨询")

    def test_cleaned_cat_in_enum(self):
        # 清洗后的分类必须落在 CLEAN_CATEGORIES 里（保证向量按类过滤可命中）
        cleaned = cs._clean_cat("退换货与售后类（自动补全）")
        self.assertIn(cleaned, cs.CLEAN_CATEGORIES)

    def test_faq_loaded(self):
        self.assertGreater(len(cs.FAQ_LIST), 0, "客服FAQ.md 应解析出问答对")
        # 每个条目都要有 question/answer/category
        for f in cs.FAQ_LIST:
            self.assertTrue(f["question"] and f["answer"], f"空条目: {f}")


if __name__ == "__main__":
    unittest.main()
