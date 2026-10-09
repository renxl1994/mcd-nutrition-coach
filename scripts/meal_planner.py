#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meal_planner.py —— 在热量预算内，从真实菜单里配出「蛋白质最优」的一餐。

和市面上「帮我点餐」类工具的差别：
    它不会问你"想吃什么"，而是先问你"这一餐的额度是多少"，
    然后在额度内帮你把蛋白质拉满、把钠压住。

用法示例：
    # 午餐：主食 + 小食 + 饮品，600 kcal 以内，蛋白质至少 25 g
    python scripts/meal_planner.py --slots main,side,drink --kcal-max 600 --protein-min 25

    # 早餐：只要主食 + 饮品，450 kcal 以内
    python scripts/meal_planner.py --slots 早餐,drink --kcal-max 450 --top 3

    # 排除油炸、限定只看某几类
    python scripts/meal_planner.py --slots main,drink --kcal-max 500 --exclude 薯条,脆汁鸡,V翅

    # 输出 JSON 交给上层（比如 WorkBuddy / 点餐流程）继续处理
    python scripts/meal_planner.py --slots main,side,drink --kcal-max 600 --json

仅依赖标准库。
"""

import argparse
import itertools
import json
import sys

from mcd_nutrition import (
    NUTRIENT_KEYS,
    NutritionIndex,
    base_name,
    enable_utf8_stdout,
    format_number,
    protein_density,
    resolve_slots,
)

DEFAULT_SLOTS = "main,side,drink"

# 一餐钠的参考线。中国居民膳食指南建议成人每日钠 < 2000 mg，
# 按三餐折算约 660 mg/餐，这里取 800 mg 作为提醒阈值（不是硬约束）。
SODIUM_PER_MEAL_HINT = 800


def dedupe_by_base(candidates, preferred_specs=("中杯", "中")):
    """
    同一商品的多个规格只保留一个。
    优先保留"中杯/中"这类最常见规格；没有的话保留热量最低的那个（通常是最小杯）。
    """
    groups = {}
    for item in candidates:
        groups.setdefault(base_name(item["productName"]), []).append(item)

    kept = []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # 与未去重前保持相对顺序：按传入顺序找第一个命中的优先规格
        winner = next(
            (i for spec in preferred_specs for i in group if spec in i["productName"]),
            None,
        )
        if winner is None:
            winner = sorted(group, key=lambda i: i.get("energyKcal") or 0)[0]
        kept.append(winner)
    return kept


def build_pools(index, slots, include, exclude, dedupe=True):
    """按槽位准备候选池，并应用 include / exclude 关键词过滤。"""
    pools = []
    for label, categories in slots:
        candidates = index.by_category(categories)
        if exclude:
            candidates = [i for i in candidates if not any(k in i["productName"] for k in exclude)]
        if include:
            candidates = [i for i in candidates if any(k in i["productName"] for k in include)]
        # 缺数据的餐品没法参与计算，直接剔除（避免"?"混进结果）
        candidates = [i for i in candidates if i.get("energyKcal") is not None]
        if dedupe:
            candidates = dedupe_by_base(candidates)
        # 同一槽位按蛋白质密度排序，取前 25 个，避免组合爆炸同时保证质量
        candidates.sort(key=protein_density, reverse=True)
        pools.append((label, candidates[:25]))
    return pools


def totals_of(combo):
    total = {key: 0.0 for key in NUTRIENT_KEYS}
    for item in combo:
        for key in NUTRIENT_KEYS:
            total[key] += item.get(key) or 0
    return total


def violates(total, args):
    """返回违反约束的严重度（0 表示全部满足），用于兜底推荐。"""
    kcal_max = getattr(args, "kcal_max", None)
    protein_min = getattr(args, "protein_min", None)
    sodium_max = getattr(args, "sodium_max", None)

    penalty = 0.0
    if kcal_max and total["energyKcal"] > kcal_max:
        penalty += total["energyKcal"] - kcal_max
    if protein_min and total["protein"] < protein_min:
        penalty += (protein_min - total["protein"]) * 10
    if sodium_max and total["sodium"] > sodium_max:
        penalty += (total["sodium"] - sodium_max) / 10.0
    return penalty


def rank_key(record, objective, target):
    """按目标函数生成排序键（全为升序，越小的越好）。"""
    penalty, density, drift, combo, total = record
    if objective == "light":
        return (penalty, total["energyKcal"], -density)
    if objective == "density":
        return (penalty, -density, drift)
    # 默认 fill：把预算吃满、不浪费额度，其次才看蛋白质密度
    return (penalty, drift, -density)


def pick_diverse(records, top):
    """
    取前 N 个方案，并保证「主食」不重样（同一主食最多出现 2 次）。
    否则前 5 名很可能全是同一种汉堡配不同饮料，看不出选择空间。
    """
    picked, counter = [], {}
    for record in records:
        main = record[3][0]
        key = base_name(main["productName"])
        if counter.get(key, 0) >= 2:
            continue
        counter[key] = counter.get(key, 0) + 1
        picked.append(record)
        if len(picked) >= top:
            return picked
    # 不够就放宽限制，宁可重样也要给够方案
    for record in records:
        if record in picked:
            continue
        picked.append(record)
        if len(picked) >= top:
            break
    return picked


def plan(index, args):
    slots = resolve_slots(args.slots.split(","))
    pools = build_pools(index, slots, args.include, args.exclude, dedupe=args.dedupe)

    for label, pool in pools:
        if not pool:
            raise SystemExit("槽位「%s」在过滤后没有候选餐品，请放宽 --exclude / --include。" % label)

    target = args.target_kcal or args.kcal_max
    results, fallback = [], []

    for combo in itertools.product(*[pool for _, pool in pools]):
        total = totals_of(combo)
        penalty = violates(total, args)
        density = protein_density(total)
        drift = abs(total["energyKcal"] - target) if target else 0
        record = (penalty, density, drift, combo, total)
        if penalty == 0:
            results.append(record)
        else:
            fallback.append(record)

    results.sort(key=lambda r: rank_key(r, args.objective, target))
    fallback.sort(key=lambda r: (r[0], abs(r[4]["energyKcal"] - (target or 0)) if target else 0))

    feasible = bool(results)
    pool_records = results if feasible else fallback
    chosen = pick_diverse(pool_records, args.top)
    return slots, pools, chosen, feasible


def render_text(index, args, slots, pools, chosen, feasible):
    print("=" * 68)
    print("麦门营养参谋 · 配餐结果")
    print("=" * 68)
    print("槽位：" + " + ".join(label for label, _ in slots))
    budget = []
    if args.kcal_max:
        budget.append("热量 ≤ %d kcal" % args.kcal_max)
    if args.protein_min:
        budget.append("蛋白质 ≥ %d g" % args.protein_min)
    if args.sodium_max:
        budget.append("钠 ≤ %d mg" % args.sodium_max)
    print("约束：" + ("，".join(budget) if budget else "无"))
    if args.exclude:
        print("排除：" + "、".join(args.exclude))
    print("候选池：" + "，".join("%s %d 项" % (label, len(pool)) for label, pool in pools))
    print()

    if not feasible:
        print("⚠️  在现有候选池里没有完全满足约束的组合。下面是「最接近」的几组，")
        print("    建议放宽热量上限，或去掉某个槽位（比如先不要小食）。")
        print()

    for rank, (penalty, density, drift, combo, total) in enumerate(chosen, 1):
        flag = "✅ 达标" if penalty == 0 else "⚠️ 超标 %.0f 分" % penalty
        print("【方案 %d】%s  蛋白质密度 %s g/100kcal" % (rank, flag, format_number(density, 2)))
        for item in combo:
            print(
                "    · %-18s %4s kcal   蛋白 %3s g   脂肪 %3s g   钠 %4s mg"
                % (
                    item["productName"],
                    format_number(item.get("energyKcal"), 0),
                    format_number(item.get("protein"), 0),
                    format_number(item.get("fat"), 0),
                    format_number(item.get("sodium"), 0),
                )
            )
        line = "    合计：%s kcal | 蛋白 %s g | 脂肪 %s g | 碳水 %s g | 钠 %s mg"
        print(
            line
            % (
                format_number(total["energyKcal"], 0),
                format_number(total["protein"], 0),
                format_number(total["fat"], 0),
                format_number(total["carbohydrate"], 0),
                format_number(total["sodium"], 0),
            )
        )
        if args.kcal_max:
            left = args.kcal_max - total["energyKcal"]
            print("    热量余额：%s kcal" % format_number(left, 0))
        if args.protein_min:
            gap = total["protein"] - args.protein_min
            print("    蛋白质：%s %.0f g" % ("超出" if gap >= 0 else "还差", abs(gap)))
        if total["sodium"] > SODIUM_PER_MEAL_HINT:
            print(
                "    ⚠️ 钠 %s mg，已超过一餐参考线 %d mg（膳食指南按三餐折算约 660 mg/餐），"
                "建议下一餐清淡些。" % (format_number(total["sodium"], 0), SODIUM_PER_MEAL_HINT)
            )
        print()

    print("-" * 68)
    print("说明：以上为膳食搭配参考，不构成医疗或营养建议；")
    print("      餐品是否在售、实际价格与营养值以麦当劳官方渠道当日信息为准。")
    print("      确定方案后，请让 AI 调用 query-meals / calculate-price 核对真实供应与价格再下单。")


def main():
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="在热量预算内配出蛋白质最优的一餐")
    parser.add_argument("--slots", default=DEFAULT_SLOTS, help="槽位，逗号分隔：main/side/drink/dessert/早餐")
    parser.add_argument("--kcal-max", type=float, default=None, help="整餐热量上限（kcal）")
    parser.add_argument("--protein-min", type=float, default=None, help="整餐蛋白质下限（g）")
    parser.add_argument("--sodium-max", type=float, default=None, help="整餐钠上限（mg）")
    parser.add_argument("--target-kcal", type=float, default=None, help="期望吃到的热量（默认等于上限），用于并列时优选")
    parser.add_argument("--include", default="", help="只要名称含这些关键词的餐品（逗号分隔）")
    parser.add_argument("--exclude", default="", help="排除名称含这些关键词的餐品（逗号分隔）")
    parser.add_argument(
        "--objective",
        choices=("fill", "density", "light"),
        default="fill",
        help="配餐目标：fill=把预算吃满(默认) / density=蛋白质密度最高 / light=热量最低",
    )
    parser.add_argument("--no-dedupe", dest="dedupe", action="store_false", help="不做规格去重（保留冰/热、大/中/小杯全部）")
    parser.add_argument("--top", type=int, default=5, help="返回方案数，默认 5")
    parser.add_argument("--data", default=None, help="营养库路径，默认 data/nutrition.json")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于上层程序消费")
    args = parser.parse_args()

    args.include = [k.strip() for k in args.include.split(",") if k.strip()]
    args.exclude = [k.strip() for k in args.exclude.split(",") if k.strip()]

    index = NutritionIndex(args.data)
    slots, pools, chosen, feasible = plan(index, args)

    if args.json:
        payload = {
            "feasible": feasible,
            "objective": args.objective,
            "slotLabels": [label for label, _ in slots],
            "solutions": [
                {
                    "rank": rank,
                    "feasible": penalty == 0,
                    "proteinPer100kcal": round(density, 2),
                    "items": [
                        {
                            "productName": item["productName"],
                            "category": item["category"],
                            "energyKcal": item.get("energyKcal"),
                            "protein": item.get("protein"),
                            "fat": item.get("fat"),
                            "sodium": item.get("sodium"),
                        }
                        for item in combo
                    ],
                    "totals": {key: round(total[key], 1) for key in NUTRIENT_KEYS},
                    "sodiumWarning": total["sodium"] > SODIUM_PER_MEAL_HINT,
                }
                for rank, (penalty, density, drift, combo, total) in enumerate(chosen, 1)
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    render_text(index, args, slots, pools, chosen, feasible)


if __name__ == "__main__":
    main()
