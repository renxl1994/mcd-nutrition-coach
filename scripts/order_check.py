#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
order_check.py —— 下单前的「营养体检」。

解决一个很具体的问题：
    MCP 的 calculate-price 能告诉你「这单多少钱」，
    但没人告诉你「这单多少热量、钠是不是爆了、有没有更划算的换法」。
    真到下单那一刻，人是懒得自己算的。

用法示例：
    python scripts/order_check.py --items "巨无霸*1,中薯条*1,可乐中杯*1"
    python scripts/order_check.py --items "麦辣鸡腿汉堡,大薯条,大杯可乐" --kcal-max 700 --sodium-max 800
    python scripts/order_check.py --items "板烧鸡腿堡*2,麦乐鸡5块,冰美式中杯" --json

仅依赖标准库。
"""

import argparse
import json
import sys

from mcd_nutrition import (
    NUTRIENT_KEYS,
    NutritionIndex,
    enable_utf8_stdout,
    format_number,
    protein_density,
)

# 单餐默认参考线（可被命令行覆盖）
DEFAULT_KCAL_MAX = 750.0
DEFAULT_SODIUM_MAX = 800.0
DEFAULT_PROTEIN_MIN = 20.0


def parse_items(raw):
    """
    解析 "名称*数量,名称x数量,名称" 形式的输入。
    支持 * x × X 四种数量分隔符。
    """
    entries = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        quantity = 1
        name = token
        for sep in ("*", "x", "×", "X"):
            if sep in token:
                head, _, tail = token.rpartition(sep)
                tail = tail.strip()
                if head.strip() and tail.isdigit():
                    name, quantity = head.strip(), int(tail)
                break
        entries.append((name, max(1, quantity)))
    if not entries:
        raise SystemExit("没有解析到任何餐品，请用 --items \"巨无霸*1,中薯条*1\" 这样的格式。")
    return entries


def match_items(index, entries):
    """把用户写的名字匹配到营养库，返回 (命中的 (item, qty), 未命中的名字)。"""
    matched, unmatched = [], []
    for name, quantity in entries:
        item = index.find(name)
        if item is None:
            unmatched.append(name)
        else:
            matched.append((item, quantity))
    return matched, unmatched


def suggest_swaps(index, matched):
    """
    给出「换一个更划算」的建议：
    在同一分类里找 蛋白质不低于当前、热量更低 的餐品，按能省下的热量排序。
    """
    suggestions = []
    for item, quantity in matched:
        better = []
        for candidate in index.by_category((item["category"],)):
            if candidate is item or candidate.get("energyKcal") is None or item.get("energyKcal") is None:
                continue
            protein_ok = (candidate.get("protein") or 0) >= (item.get("protein") or 0)
            lighter = candidate["energyKcal"] < item["energyKcal"]
            if protein_ok and lighter:
                better.append(candidate)
        if not better:
            continue
        better.sort(key=lambda c: c["energyKcal"])
        best = better[0]
        saved = (item["energyKcal"] - best["energyKcal"]) * quantity
        suggestions.append(
            {
                "from": item["productName"],
                "to": best["productName"],
                "quantity": quantity,
                "category": item["category"],
                "savedKcal": saved,
                "proteinDelta": round((best.get("protein") or 0) - (item.get("protein") or 0), 1),
            }
        )
    suggestions.sort(key=lambda s: s["savedKcal"], reverse=True)
    return suggestions[:3]


def judge(total, args, missing):
    """按参考线给出判定。阈值传 None 表示不限制该项。"""
    issues, notes = [], []
    kcal_max = getattr(args, "kcal_max", None)
    sodium_max = getattr(args, "sodium_max", None)
    protein_min = getattr(args, "protein_min", None)

    if kcal_max is not None and total["energyKcal"] > kcal_max:
        issues.append(
            "热量超标 %s kcal（%s / 上限 %s）"
            % (
                format_number(total["energyKcal"] - kcal_max, 0),
                format_number(total["energyKcal"], 0),
                format_number(kcal_max, 0),
            )
        )
    if sodium_max is not None and total["sodium"] > sodium_max:
        issues.append(
            "钠超标 %s mg（%s / 上限 %s）"
            % (
                format_number(total["sodium"] - sodium_max, 0),
                format_number(total["sodium"], 0),
                format_number(sodium_max, 0),
            )
        )
    if protein_min is not None and total["protein"] < protein_min:
        notes.append(
            "蛋白质偏低 %s g（%s / 目标 %s）"
            % (
                format_number(protein_min - total["protein"], 0),
                format_number(total["protein"], 0),
                format_number(protein_min, 0),
            )
        )
    if missing:
        notes.append("有 %d 项在营养库里没找到，未计入合计" % len(missing))
    if issues:
        return "超标", issues, notes
    if notes:
        return "注意", issues, notes
    return "通过", issues, notes


def main():
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="下单前的营养体检")
    parser.add_argument("--items", required=True, help='餐品清单，如 "巨无霸*1,中薯条*1,可乐中杯*1"')
    parser.add_argument("--kcal-max", type=float, default=DEFAULT_KCAL_MAX, help="整单热量上限，默认 750 kcal")
    parser.add_argument("--sodium-max", type=float, default=DEFAULT_SODIUM_MAX, help="整单钠上限，默认 800 mg")
    parser.add_argument("--protein-min", type=float, default=DEFAULT_PROTEIN_MIN, help="整单蛋白质下限，默认 20 g")
    parser.add_argument("--data", default=None, help="营养库路径，默认 data/nutrition.json")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    index = NutritionIndex(args.data)
    entries = parse_items(args.items)
    matched, unmatched = match_items(index, entries)
    if not matched:
        raise SystemExit("清单里的餐品一个都没匹配上，请检查名称。未匹配：%s" % "、".join(unmatched))

    details, total = index.summarize(matched)
    verdict, issues, notes = judge(total, args, unmatched)
    swaps = suggest_swaps(index, matched)

    if args.json:
        print(
            json.dumps(
                {
                    "verdict": verdict,
                    "issues": issues,
                    "notes": notes,
                    "unmatched": unmatched,
                    "details": details,
                    "totals": {key: round(total[key], 1) for key in NUTRIENT_KEYS},
                    "thresholds": {
                        "kcalMax": args.kcal_max,
                        "sodiumMax": args.sodium_max,
                        "proteinMin": args.protein_min,
                    },
                    "suggestions": swaps,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    badge = {"通过": "✅ 通过", "注意": "🔸 注意", "超标": "⛔ 超标"}[verdict]
    print("=" * 68)
    print("麦门营养参谋 · 下单前体检  %s" % badge)
    print("=" * 68)
    print("清单：%s" % "，".join("%s×%d" % (i["productName"], q) for i, q in matched))
    if unmatched:
        print("未识别：%s" % "、".join(unmatched))
    print()
    print("%-20s %8s %8s %8s %8s" % ("餐品", "热量", "蛋白", "脂肪", "钠"))
    print("-" * 68)
    for row in details:
        print(
            "%-20s %8s %8s %8s %8s"
            % (
                "%s×%d" % (row["productName"], row["quantity"]) if row["quantity"] > 1 else row["productName"],
                format_number(row["energyKcal"], 0),
                format_number(row["protein"], 0),
                format_number(row["fat"], 0),
                format_number(row["sodium"], 0),
            )
        )
    print("-" * 68)
    print(
        "%-20s %8s %8s %8s %8s"
        % (
            "合计",
            format_number(total["energyKcal"], 0),
            format_number(total["protein"], 0),
            format_number(total["fat"], 0),
            format_number(total["sodium"], 0),
        )
    )
    print("（单位：热量 kcal，其余 g / mg）")
    print()

    if issues:
        for issue in issues:
            print("⛔ %s" % issue)
    for note in notes:
        print("🔸 %s" % note)
    if not issues and not notes:
        print("✅ 各项指标都在参考线内。")

    if swaps:
        print()
        print("可以这样换（不牺牲蛋白质、只减热量）：")
        for swap in swaps:
            delta = "，蛋白质 %s%s g" % ("+" if swap["proteinDelta"] >= 0 else "", format_number(swap["proteinDelta"], 0))
            print(
                "    · %s → %s：省 %s kcal%s"
                % (swap["from"], swap["to"], format_number(swap["savedKcal"], 0), delta)
            )

    print()
    print("-" * 68)
    print("说明：参考线按成人每日 2000 kcal、钠 2000 mg 折算到单餐，仅供点餐参考，")
    print("      不构成医疗或营养建议。营养值以麦当劳官方渠道当日信息为准。")


if __name__ == "__main__":
    main()
