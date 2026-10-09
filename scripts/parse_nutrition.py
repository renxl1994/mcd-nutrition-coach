#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_nutrition.py —— 把麦当劳 MCP `list-nutrition-foods` 的原始返回解析成结构化营养库。

为什么需要它：
    `list-nutrition-foods` 返回的是一段「压缩表格字符串」，形如
        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          中薯条,null,1210,289,4,12,38,165,18
          ...
    每次对话都让大模型重新按列数对齐解析，既慢又容易错行。
    本脚本一次性把它落成 JSON，之后所有营养计算都基于本地结构化数据，稳定且零依赖。

用法：
    # 直接喂 MCP 的 JSON-RPC 原始响应（推荐，原始文件留档可复现）
    python scripts/parse_nutrition.py data/raw/nutrition-raw.json -o data/nutrition.json

    # 也支持喂纯文本块（只有 [160]{...}: 那一段）
    python scripts/parse_nutrition.py data/raw/nutrition-raw.txt -o data/nutrition.json

仅使用标准库，无第三方依赖。
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))

HEADER_RE = re.compile(r"\[(\d+)\]\{([^}]+)\}:")
# 数值列（energyKj 起）的列名，用于把缺失值补齐成 None
NUMERIC_HINTS = ("energy", "protein", "fat", "carbohydrate", "sodium", "calcium", "kj", "kcal")


def _to_number(raw):
    """把 'null' / '' / '1288' 统一转成 float 或 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() in ("null", "none", "na", "-"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    # 整数就返回 int，避免 JSON 里出现 308.0 这种噪声
    return int(value) if value.is_integer() else value


def _extract_data_string(payload_text):
    """
    从 MCP 返回的完整文本里抠出真正的 data 字符串。

    MCP 的文本内容包含一段「响应结构说明」+ 一段 Original Response 的 JSON。
    这里的策略是从后往前找 `{"success"` 开头的那个 JSON 对象，再取它的 data 字段。
    """
    idx = payload_text.find('{"success"')
    if idx == -1:
        # 没有 JSON 包装，那它本身就是压缩表格文本
        return payload_text

    decoder = json.JSONDecoder()
    # 允许后面还有尾巴，用 raw_decode 从 idx 开始解
    obj, _ = decoder.raw_decode(payload_text[idx:])
    data = obj.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        # 万一上游改成返回数组，序列化成一行行文本处理
        return "\n".join(
            ",".join("" if v is None else str(v) for v in row.values())
            if isinstance(row, dict) else str(row)
            for row in data
        )
    return payload_text


def parse_rows(block_text):
    """把 `[N]{columns}:` 后面的行解析成 dict 列表。"""
    match = HEADER_RE.search(block_text)
    if not match:
        raise ValueError("没找到 `[数量]{列名}:` 形式的表头，无法解析。请确认输入是 list-nutrition-foods 的返回。")

    declared_count = int(match.group(1))
    columns = [c.strip() for c in match.group(2).split(",") if c.strip()]
    body = block_text[match.end():]

    items = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # 处理可能的行首编号（"1. xxx" / "1、xxx"）
        line = re.sub(r"^\d+\s*[.、:：]\s*", "", line)
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue  # 不是数据行

        # 列数不足时右侧用 None 补齐（上游偶尔会砍掉末尾的钙）
        if len(parts) < len(columns):
            parts += [""] * (len(columns) - len(parts))

        row = {}
        for name, value in zip(columns, parts):
            lowered = name.lower()
            if any(hint in lowered for hint in NUMERIC_HINTS):
                row[name] = _to_number(value)
            else:
                text = value.strip()
                row[name] = None if text.lower() in ("", "null", "none") else text

        if not row.get("productName"):
            continue
        items.append(row)

    return declared_count, columns, items


def load_input(path):
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    # 输入可能就是 JSON-RPC 响应体
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            outer = json.loads(stripped)
        except json.JSONDecodeError:
            outer = None
        if isinstance(outer, dict):
            # JSON-RPC: {"result":{"content":[{"text":"..."}]}}
            result = outer.get("result")
            if isinstance(result, dict):
                contents = result.get("content") or []
                for chunk in contents:
                    if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                        return _extract_data_string(chunk["text"])
            # 已经是 {"success":...,"data":"..."} 形态
            if isinstance(outer.get("data"), str):
                return _extract_data_string(stripped)
            if isinstance(outer.get("data"), list):
                return _extract_data_string(stripped)

    return _extract_data_string(text)


def main():
    parser = argparse.ArgumentParser(description="解析麦当劳 MCP 营养数据为结构化 JSON")
    parser.add_argument("input", help="MCP 原始返回文件（JSON-RPC 响应 或 纯文本表格块）")
    parser.add_argument("-o", "--output", default="data/nutrition.json", help="输出 JSON 路径")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结果摘要")
    args = parser.parse_args()

    data_text = load_input(args.input)
    declared, columns, items = parse_rows(data_text)

    if declared != len(items):
        # 不致命，但要说清楚：上游加了行或我们漏了行
        print(
            "⚠️  表头声明 %d 项，实际解析出 %d 项，请人工核对。" % (declared, len(items)),
            file=sys.stderr,
        )

    payload = {
        "source": "McDonald's China MCP Server",
        "tool": "list-nutrition-foods",
        "endpoint": "https://mcp.mcd.cn",
        "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "declaredCount": declared,
        "itemCount": len(items),
        "columns": columns,
        "units": {
            "energyKj": "kJ",
            "energyKcal": "kcal",
            "protein": "g",
            "fat": "g",
            "carbohydrate": "g",
            "sodium": "mg",
            "calcium": "mg",
        },
        "disclaimer": "营养数据来自麦当劳中国 MCP 实时接口，仅供点餐参考，不作为医疗或营养建议；以官方渠道当日信息为准。",
        "items": items,
    }

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    if args.quiet:
        print("ok %d items -> %s" % (len(items), args.output))
    else:
        print("✅ 解析完成：%d 项餐品营养数据 → %s" % (len(items), args.output))
        print("   列：%s" % ", ".join(columns))
        sample = items[:3]
        for row in sample:
            print(
                "   · %s：%s kcal / 蛋白 %s g / 脂肪 %s g / 碳水 %s g / 钠 %s mg"
                % (
                    row.get("productName"),
                    row.get("energyKcal"),
                    row.get("protein"),
                    row.get("fat"),
                    row.get("carbohydrate"),
                    row.get("sodium"),
                )
            )


if __name__ == "__main__":
    main()
