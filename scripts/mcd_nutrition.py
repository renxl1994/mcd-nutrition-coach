#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd_nutrition.py —— 麦门营养参谋的共享内核。

职责：
    1. 载入 data/nutrition.json（由 parse_nutrition.py 从 MCP 生成）
    2. 按名称把餐品归类到「主食 / 小食 / 饮品 / 甜品」四类
    3. 名称模糊匹配（用户说的"巨无霸"要能命中"巨无霸"）
    4. 营养汇总、达标校验、同类替换建议

只依赖标准库。所有营养字段单位见 nutrition.json 的 units 字段。
"""

import json
import os
import re

DEFAULT_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nutrition.json"
)

# ---------------------------------------------------------------------------
# 归类规则
# 顺序有意义：先命中的分类生效，所以「特定的」要排在「宽泛的」前面。
# 例如"麦辣鸡腿汉堡"含"鸡"也含"汉堡"，必须让汉堡规则先命中小食规则。
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ("早餐", ["麦满分", "早安营养卷", "粥", "油条", "薯饼", "炒双蛋", "双蛋堡"]),
    ("汉堡", ["巨无霸", "汉堡", "牛堡", "鸡腿堡", "鸡排", "麦香鸡", "麦香鱼", "鳕鱼堡", "鱼排堡", "脆汁鸡", "板烧", "卷"]),
    ("小食", ["薯条", "麦乐鸡", "鸡翅", "V翅", "鸡球", "玉米杯", "苹果片", "沙拉", "香肠"]),
    ("甜品", ["冰淇淋", "新地", "麦旋风", "派", "拉明顿", "阿芙佳朵", "奶冻"]),
    ("饮品", ["可乐", "雪碧", "咖啡", "美式", "奶铁", "拿铁", "玛奇朵", "卡布奇诺", "红茶", "绿茶", "抹茶", "牛奶", "豆浆", "果汁", "柠檬", "怡泉", "雪冰", "黑巧", "燕麦", "纯悦", "阳光橙", "朱古力"]),
]

CATEGORIES = [name for name, _ in CATEGORY_RULES] + ["其他"]
SLOT_ALIASES = {
    "main": "主食",
    "主食": "主食",
    "side": "小食",
    "小食": "小食",
    "drink": "饮品",
    "饮品": "饮品",
    "dessert": "甜品",
    "甜品": "甜品",
    "breakfast": "早餐",
    "早餐": "早餐",
}
# 「主食槽位」实际会把 汉堡 + 早餐 + 其他 都算进来
MAIN_CATEGORIES = ("汉堡", "早餐", "其他")

NUTRIENT_KEYS = ("energyKcal", "protein", "fat", "carbohydrate", "sodium", "calcium")
NUTRIENT_LABELS = {
    "energyKcal": "热量(kcal)",
    "protein": "蛋白质(g)",
    "fat": "脂肪(g)",
    "carbohydrate": "碳水(g)",
    "sodium": "钠(mg)",
    "calcium": "钙(mg)",
}

# 口语 → 菜单正式名。
# 不做这层映射的话，"零度可乐"会被模糊匹配猜成普通可乐，
# 一次就多算 147 kcal —— 这类错误在这个工具里不能接受。
ALIASES = {
    "零度": "无糖可口可乐中杯",
    "零度可乐": "无糖可口可乐中杯",
    "无糖可乐": "无糖可口可乐中杯",
    "健怡可乐": "无糖可口可乐中杯",
    "可乐": "可乐中杯",
    "雪碧": "雪碧中杯",
    "薯条": "中薯条",
    "中薯": "中薯条",
    "大薯": "大薯条",
    "小薯": "小薯条",
    "麦辣鸡腿堡": "麦辣鸡腿汉堡",
    "麦辣堡": "麦辣鸡腿汉堡",
    "板烧": "板烧鸡腿堡",
    "甜筒": "圆筒冰淇淋",
    "圆筒": "圆筒冰淇淋",
    "玉米杯": "小杯玉米杯",
    "麦乐鸡": "麦乐鸡5块",
    "鸡块": "麦乐鸡5块",
    "鲜萃": "小杯鲜萃咖啡",
    "美式": "冰美式中杯",
    "奶铁": "冰奶铁中杯",
}


class NutritionIndex(object):
    def __init__(self, path=None):
        self.path = path or DEFAULT_DATA_PATH
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.meta = payload
        self.items = payload.get("items", [])
        for item in self.items:
            item["category"] = classify(item.get("productName") or "")

    # -- 查询 ---------------------------------------------------------------
    def all(self):
        return list(self.items)

    def by_category(self, categories):
        wanted = set(categories)
        return [i for i in self.items if i["category"] in wanted]

    def find(self, name, allow_fuzzy=True):
        """按名称找餐品。优先别名 → 精确匹配 → 子串匹配 → 相似度兜底。"""
        keyword = (name or "").strip()
        if not keyword:
            return None

        canonical = ALIASES.get(keyword)
        if canonical:
            for item in self.items:
                if item["productName"] == canonical:
                    return item

        for item in self.items:
            if item["productName"] == keyword:
                return item
        hits = [i for i in self.items if keyword in i["productName"]]
        if not hits:
            hits = [i for i in self.items if i["productName"] in keyword]
        if hits:
            # 名字越短的越可能是用户真正想点的那个（避免命中"双层XX"）
            return sorted(hits, key=lambda i: len(i["productName"]))[0]
        if allow_fuzzy:
            return self._closest(keyword)
        return None

    def _closest(self, keyword):
        best, best_score = None, 0.0
        for item in self.items:
            score = _similarity(keyword, item["productName"])
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= 0.5 else None

    # -- 汇总 ---------------------------------------------------------------
    def summarize(self, entries):
        """
        entries: [(item, quantity), ...]
        返回 (逐项明细, 合计字典)，缺失值按 0 计但在明细里标注。
        """
        lines = []
        total = {key: 0.0 for key in NUTRIENT_KEYS}
        missing = []
        for item, quantity in entries:
            row = {"productName": item["productName"], "category": item["category"], "quantity": quantity}
            for key in NUTRIENT_KEYS:
                value = item.get(key)
                if value is None:
                    missing.append("%s.%s" % (item["productName"], key))
                    row[key] = None
                else:
                    row[key] = value * quantity
                    total[key] += value * quantity
            lines.append(row)
        if missing:
            total["_missingFields"] = missing
        return lines, total


def classify(product_name):
    for category, keywords in CATEGORY_RULES:
        for keyword in keywords:
            if keyword in product_name:
                return category
    return "其他"


def resolve_slots(raw_slots):
    """把 CLI 传入的槽位名（main/side/drink/主食...）翻译成实际分类集合。"""
    resolved = []
    for raw in raw_slots:
        key = raw.strip()
        if not key:
            continue
        mapped = SLOT_ALIASES.get(key.lower(), SLOT_ALIASES.get(key))
        if mapped is None:
            raise SystemExit("未知的槽位：%s（可用：main/side/drink/dessert/早餐）" % key)
        if mapped == "主食":
            resolved.append(("主食", MAIN_CATEGORIES))
        else:
            resolved.append((mapped, (mapped,)))
    return resolved


def similarity(a, b):
    """简易相似度：公共字符占比。中文短词场景足够用。"""
    if not a or not b:
        return 0.0
    common = len(set(a) & set(b))
    return common / float(max(len(set(a)), len(set(b))))


# _similarity 为兼容旧调用保留别名
_similarity = similarity

# 杯型/冷热规格词。去重时剥掉它们，"冰美式小杯"和"热美式中杯"才能认出是同一个商品。
# 顺序有意义：先剥多字词（大杯/中杯/小杯），再剥单字（大/中/小），否则"小杯玉米杯"
# 会被单字规则先切坏。
SPEC_TOKENS = ("超大杯", "大杯", "中杯", "小杯", "迷你", "冰", "热")
SPEC_PREFIXES = ("大", "中", "小")


def base_name(product_name):
    """
    取「规格无关」的商品基名，用于识别同一商品的重复规格。

        >>> base_name("冰美式小杯")
        '美式'
        >>> base_name("热美式中杯")
        '美式'
        >>> base_name("大薯条")
        '薯条'
        >>> base_name("迷你朱古力新地")
        '朱古力新地'
    """
    name = product_name
    for token in SPEC_TOKENS:
        name = name.replace(token, "")
    # 处理"大薯条/中薯条/小薯条"这类没有"杯"字的规格前缀
    while name and name[0] in SPEC_PREFIXES and len(name) > 2:
        name = name[1:]
    return name.strip() or product_name


def format_number(value, digits=1):
    if value is None:
        return "?"
    if digits == 0:
        return str(int(round(value)))
    text = ("%." + str(digits) + "f") % value
    return text.rstrip("0").rstrip(".") if "." in text else text


def protein_density(item):
    """每 100 kcal 能提供多少克蛋白质 —— 本项目的核心排序指标。"""
    kcal = item.get("energyKcal") or 0
    protein = item.get("protein")
    if not kcal or protein is None:
        return 0.0
    return protein / kcal * 100.0


def describe(item, quantity=1):
    return "%s×%d（%s kcal）" % (
        item["productName"],
        quantity,
        format_number((item.get("energyKcal") or 0) * quantity, 0),
    )


def enable_utf8_stdout():
    """Windows 控制台默认可能是 GBK，中文输出前统一切到 UTF-8。"""
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    enable_utf8_stdout()
    index = NutritionIndex()
    print("营养库：%s" % index.path)
    print("餐品数：%d" % len(index.items))
    counter = {}
    for item in index.items:
        counter[item["category"]] = counter.get(item["category"], 0) + 1
    for name in CATEGORIES:
        if counter.get(name):
            print("  %s：%d 项" % (name, counter[name]))
