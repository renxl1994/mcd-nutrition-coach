#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_smoke.py —— 零依赖自测。直接 `python -m unittest discover tests` 或
`python tests/test_smoke.py` 都能跑。

覆盖三类容易出错的地方：
    1. 解析层：MCP 压缩表格 → 结构化数据，列对齐不能错行
    2. 匹配层：口语别名、"薯条"该落到中薯条而不是小薯条
    3. 计算层：配餐是否守住热量上限、体检判定是否正确
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from mcd_nutrition import (  # noqa: E402
    NutritionIndex,
    base_name,
    classify,
    protein_density,
)
from parse_nutrition import parse_rows  # noqa: E402
import meal_planner  # noqa: E402
import order_check  # noqa: E402


class Args(object):
    """给内部函数喂一个轻量 args 对象，避免为了测试去跑 argparse。"""

    def __init__(self, **kwargs):
        self.slots = kwargs.get("slots", "main,side,drink")
        self.kcal_max = kwargs.get("kcal_max", 600)
        self.protein_min = kwargs.get("protein_min", None)
        self.sodium_max = kwargs.get("sodium_max", None)
        self.target_kcal = kwargs.get("target_kcal", None)
        self.include = kwargs.get("include", [])
        self.exclude = kwargs.get("exclude", [])
        self.objective = kwargs.get("objective", "fill")
        self.dedupe = kwargs.get("dedupe", True)
        self.top = kwargs.get("top", 3)
        self.data = kwargs.get("data", None)


class TestParser(unittest.TestCase):
    def test_parse_rows_alignment(self):
        block = (
            "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
            "  巨无霸,null,2148,513,27,26,42,961,213\n"
            "  中薯条,null,1210,289,4,12,38,165,18\n"
        )
        declared, columns, items = parse_rows(block)
        self.assertEqual(declared, 2)
        self.assertEqual(len(columns), 9)
        self.assertEqual(len(items), 2)
        # 列必须严格对齐：巨无霸的钠是 961，不是 213
        self.assertEqual(items[0]["productName"], "巨无霸")
        self.assertEqual(items[0]["energyKcal"], 513)
        self.assertEqual(items[0]["sodium"], 961)
        self.assertEqual(items[0]["calcium"], 213)

    def test_missing_tail_column_becomes_none(self):
        # 上游偶尔会砍掉末尾的钙列，不能因此错位
        block = (
            "[1]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
            "  巨无霸,null,2148,513,27,26,42,961\n"
        )
        _, _, items = parse_rows(block)
        self.assertEqual(items[0]["sodium"], 961)
        self.assertIsNone(items[0]["calcium"])

    def test_null_becomes_none_not_zero(self):
        block = "[1]{productName,energyKcal,protein}:\n  某餐品,null,null\n"
        _, _, items = parse_rows(block)
        self.assertIsNone(items[0]["energyKcal"])
        self.assertIsNone(items[0]["protein"])


class TestMatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = NutritionIndex()

    def test_alias_zero_cola(self):
        # 关键回归：零度可乐必须落到无糖款，差 147 kcal
        item = self.index.find("零度可乐")
        self.assertIsNotNone(item)
        self.assertIn("无糖", item["productName"])

    def test_alias_common_nicknames(self):
        self.assertEqual(self.index.find("薯条")["productName"], "中薯条")
        self.assertEqual(self.index.find("甜筒")["productName"], "圆筒冰淇淋")
        self.assertEqual(self.index.find("麦辣鸡腿堡")["productName"], "麦辣鸡腿汉堡")

    def test_exact_name_wins(self):
        self.assertEqual(self.index.find("巨无霸")["productName"], "巨无霸")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(self.index.find("螺蛳粉"))

    def test_classification(self):
        self.assertEqual(classify("巨无霸"), "汉堡")
        self.assertEqual(classify("中薯条"), "小食")
        self.assertEqual(classify("冰美式中杯"), "饮品")
        self.assertEqual(classify("猪柳蛋麦满分"), "早餐")

    def test_base_name_normalises_specs(self):
        self.assertEqual(base_name("冰美式小杯"), base_name("热美式中杯"))
        self.assertEqual(base_name("大薯条"), base_name("小薯条"))


class TestPlanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = NutritionIndex()

    def test_respects_kcal_ceiling(self):
        args = Args(kcal_max=600, protein_min=25, top=5)
        _, _, chosen, feasible = meal_planner.plan(self.index, args)
        self.assertTrue(feasible)
        self.assertTrue(chosen)
        for record in chosen:
            self.assertLessEqual(record[4]["energyKcal"], 600 + 1e-6)

    def test_no_duplicate_specs_in_result(self):
        # 去重后，同一方案里不应出现"冰美式小杯"和"热美式中杯"这种同款
        args = Args(kcal_max=700, top=3)
        _, _, chosen, _ = meal_planner.plan(self.index, args)
        for record in chosen:
            bases = [base_name(i["productName"]) for i in record[3]]
            self.assertEqual(len(bases), len(set(bases)))

    def test_infeasible_falls_back_instead_of_crashing(self):
        args = Args(slots="main,drink", kcal_max=50, top=2)
        _, _, chosen, feasible = meal_planner.plan(self.index, args)
        self.assertFalse(feasible)
        self.assertTrue(chosen)  # 仍要给出最接近的方案


class TestOrderCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = NutritionIndex()

    def test_totals_and_verdict(self):
        entries = order_check.parse_items("巨无霸*1,中薯条*1,可乐中杯*1")
        matched, unmatched = order_check.match_items(self.index, entries)
        self.assertEqual(unmatched, [])
        _, total = self.index.summarize(matched)
        # 巨无霸 513 + 中薯条 289 + 可乐中杯 147 = 949
        self.assertAlmostEqual(total["energyKcal"], 949, places=1)

        args = Args(kcal_max=750)
        verdict, issues, _ = order_check.judge(total, args, [])
        self.assertEqual(verdict, "超标")
        self.assertTrue(any("热量超标" in i for i in issues))

    def test_within_budget_passes(self):
        entries = order_check.parse_items("麦香鸡,无糖可口可乐中杯")
        matched, _ = order_check.match_items(self.index, entries)
        _, total = self.index.summarize(matched)
        args = Args(kcal_max=750, protein_min=15)
        verdict, issues, _ = order_check.judge(total, args, [])
        self.assertEqual(verdict, "通过")
        self.assertEqual(issues, [])

    def test_quantity_suffix_formats(self):
        self.assertEqual(order_check.parse_items("巨无霸*2"), [("巨无霸", 2)])
        self.assertEqual(order_check.parse_items("巨无霸x2"), [("巨无霸", 2)])
        self.assertEqual(order_check.parse_items("巨无霸"), [("巨无霸", 1)])

    def test_protein_density_monotonic(self):
        lean = {"energyKcal": 100, "protein": 20}
        fatty = {"energyKcal": 100, "protein": 5}
        self.assertGreater(protein_density(lean), protein_density(fatty))


if __name__ == "__main__":
    unittest.main(verbosity=2)
