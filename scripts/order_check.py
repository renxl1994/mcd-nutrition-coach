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
    base_name,
    enable_utf8_stdout,
    format_number,
    protein_density,
    size_rank,
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


# 不作为替换目标的餐品：
# "把大杯可乐换成瓶装水，省 224 kcal" —— 这不是建议，是常识。
# 换法的价值在于给出用户没想到的替换，而不是让他一眼就翻白眼的那种。
TRIVIAL_SWAP_TARGETS = ("纯悦", "矿泉水", "纯净水")

# 把规格改小要付的价格（kcal 当量）。
# 大杯无糖可乐、中杯无糖可乐、小杯无糖可乐都是 0 kcal，三者省下的热量一模一样；
# 如果只按"加钠的代价"排序，就会推荐把大杯换成小杯 —— 用户白白少喝三分之二，
# 换来的只是少 28 mg 钠。份量是用户自己选的，缩小它不该算成这个换法的功劳，
# 所以给它标个价：只比"加 250 mg 钠"的代价略高一点点，够用来打破规格上的平局。
DOWNSIZE_PRICE = 5.0


def _same_family(origin_name, candidate_name):
    """
    判断两个餐品是否同族（同一商品线的不同版本）。
    "可乐" 与 "无糖可口可乐" 同族；"可乐" 与 "纯悦" 不同族。
    同族替换更容易被真正采纳 —— 想喝可乐的人会接受无糖可乐，不会接受白开水。
    """
    a, b = base_name(origin_name), base_name(candidate_name)
    if a in b or b in a:
        return True
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return False
    overlap = len(set_a & set_b)
    return overlap >= max(1, min(len(set_a), len(set_b)) * 0.5)


def suggest_swaps(index, matched):
    """
    给出「换一个更划算」的建议：
    在同一分类里找「蛋白质不低于当前、热量更低」的餐品。

    排序分三层，逐层决定：
      1. 同族优先 —— 可乐换成无糖可乐，而不是换成白开水；
      2. 综合评分 —— 省下的热量，扣掉「加钠的代价」（每 50 mg 钠折算 1 kcal）
         与「把份量改小的代价」（DOWNSIZE_PRICE）。前者避免"热量降了但钠涨了
         500 mg"的建议，后者避免"大杯可乐换小杯无糖可乐"这种白少喝的换法；
      3. 同分时热量更低的优先。
    返回里带上 sodiumDelta，让上层如实展示这个取舍，而不是替用户藏起来。
    """
    suggestions = []
    for item, quantity in matched:
        item_kcal = item.get("energyKcal")
        item_sodium = item.get("sodium") or 0
        item_size = size_rank(item["productName"])
        if item_kcal is None:
            continue

        better = []
        for candidate in index.by_category((item["category"],)):
            if candidate is item or candidate.get("energyKcal") is None:
                continue
            if any(token in candidate["productName"] for token in TRIVIAL_SWAP_TARGETS):
                continue
            if (candidate.get("protein") or 0) < (item.get("protein") or 0):
                continue
            if candidate["energyKcal"] >= item_kcal:
                continue

            saved = item_kcal - candidate["energyKcal"]
            sodium_delta = (candidate.get("sodium") or 0) - item_sodium
            candidate_size = size_rank(candidate["productName"])
            downsized = (
                item_size is not None and candidate_size is not None and candidate_size < item_size
            )
            score = saved - max(0.0, sodium_delta) / 50.0 - (DOWNSIZE_PRICE if downsized else 0.0)
            family_rank = 0 if _same_family(item["productName"], candidate["productName"]) else 1
            better.append((family_rank, -score, candidate["energyKcal"], candidate, saved, sodium_delta))

        if not better:
            continue
        better.sort(key=lambda entry: entry[:3])
        _, _, _, best, saved_kcal, sodium_delta = better[0]
        suggestions.append(
            {
                "from": item["productName"],
                "to": best["productName"],
                "quantity": quantity,
                "category": item["category"],
                "savedKcal": saved_kcal * quantity,
                "sodiumDelta": round(sodium_delta * quantity, 0),
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
        print("可以这样换（蛋白质不降低为前提，尽量少减热量、少加钠）：")
        for swap in swaps:
            protein_delta = swap["proteinDelta"]
            sodium_delta = swap["sodiumDelta"]
            parts = ["省 %s kcal" % format_number(swap["savedKcal"], 0)]
            parts.append("蛋白质 %s%s g" % ("+" if protein_delta >= 0 else "-", format_number(abs(protein_delta), 0)))
            if sodium_delta:
                # 钠上涨的替换必须显式标出来，不能藏在"省了多少热量"后面
                parts.append("钠 %s%s mg" % ("+" if sodium_delta > 0 else "-", format_number(abs(sodium_delta), 0)))
            print("    · %s → %s：%s" % (swap["from"], swap["to"], "，".join(parts)))

    print()
    print("-" * 68)
    print("说明：参考线按成人每日 2000 kcal、钠 2000 mg 折算到单餐，仅供点餐参考，")
    print("      不构成医疗或营养建议。营养值以麦当劳官方渠道当日信息为准。")


if __name__ == "__main__":
    main()
