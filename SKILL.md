---
name: 麦门营养参谋
description: 在下单之前，先把这一餐的热量、蛋白质和钠算清楚。基于麦当劳中国 MCP 的真实菜单与营养数据，帮你在每日热量预算内配出蛋白质最优的一餐，并对已选好的清单做"下单前体检"，给出不牺牲蛋白质的替换建议。适用于：想知道某餐多少热量、想控制体重期间点麦当劳、想对比不同搭配方案、下单前想确认这单值不值。
---

# 麦门营养参谋（MCD Nutrition Coach）

## 这个 Skill 解决什么问题

麦当劳的 MCP 已经有 `calculate-price` 告诉你"这单多少钱"，但没有任何一个接口告诉你
"这单多少热量、钠是不是爆了、有没有更划算的换法"。而人到了点餐页面，是不会自己算的。

本 Skill 把「营养知情」这件事挪到下单之前。

**设计原则：它是参谋，不是推销员。** 它不劝你吃或不吃，只把代价摆清楚，
让你自己决定——以及，如果你想少一点，它能立刻给出不牺牲蛋白质的具体换法。

## 能力清单

| 能力 | 说明 | 依赖的 MCP Tool |
|---|---|---|
| 营养速查 | 任意餐品的热量 / 蛋白质 / 脂肪 / 碳水 / 钠 / 钙 | `list-nutrition-foods` |
| 预算配餐 | 给定热量上限与蛋白质下限，从真实菜单里求出最优组合 | `list-nutrition-foods` + `query-meals` |
| 下单前体检 | 对已选清单汇总营养、按参考线判定、给出替换建议 | `list-nutrition-foods` |
| 门店与在售核对 | 确认餐品在目标门店真的在售、多少钱 | `query-nearby-stores` / `query-meals` / `calculate-price` |

## 工作流

### 场景 A：用户要"在预算内配一餐"

1. **先问清楚，别猜。** 必须确认三件事：
   - 这一餐的热量额度（多少 kcal）——用户说"随便吃点"时不猜，直接问；
   - 蛋白质目标（可选，默认不设下限）；
   - 有没有忌口或排除项（如"不要油炸"）。
   - 如果用户说自己有体重管理需求但没给数字，用「按每日 2000 kcal 折算三餐」给一个
     建议值（早餐 400 / 午餐 700 / 晚餐 600），**并明确说明这是折算值，让他确认或改**。

2. **配餐。** 在项目根目录运行：

   ```bash
   python scripts/meal_planner.py --slots main,side,drink \
       --kcal-max 650 --protein-min 30 --top 5
   ```

   - `--objective fill`（默认）会尽量把预算吃满，不浪费额度；改用 `density` 则纯追蛋白质密度。
   - 结果里带 `⚠️ 超标` 的方案说明无解，脚本会自动给"最接近"的兜底并提示放宽哪个条件。
   - 需要结构化结果给后续流程用，加 `--json`。

3. **把结果讲成人话。** 至少包含：每项多少 kcal、整餐合计、热量余额、蛋白质达标情况。
   如果合计钠超过 800 mg，必须把这条警告一起说出来——这不是可选建议，
   一餐 1200 mg 钠已经接近每日上限的六成。

4. **落到真实门店再确认。** 配餐用的是营养库全集，不保证目标门店当天在售。
   用户表示要去下单时，先用 `query-nearby-stores` 拿 `storeCode` / `beCode`，
   再用 `query-meals` 核对在售与价格。

### 场景 B：用户已经想好点什么，要"看一眼再下单"

1. 从用户的话里提取清单（"巨无霸、大薯条、可乐大杯"），然后运行：

   ```bash
   python scripts/order_check.py --items "巨无霸*1,大薯条*1,可乐大杯*1"
   ```

   - 数量写 `名称*2`；口语叫法（"零度可乐""甜筒"）会自动映射到菜单正式名。
   - 阈值可用 `--kcal-max` / `--sodium-max` / `--protein-min` 覆盖，默认 750 kcal / 800 mg / 20 g。

2. **把判定和替换建议原样转达给用户**，包括"省多少 kcal、蛋白质有没有掉"。
   不要只报总数就催他下单，也不要替他决定。

3. 用户确认后再走真实下单链路：
   `query-nearby-stores` → `query-meals` → `query-store-coupons`
   → `calculate-price`（让用户看到真实金额）→ 用户确认 → `create-order`。

### 场景 C：只问"XX 多少热量"

直接查营养库，一句话回答，别展开成一份报告：

```bash
python scripts/order_check.py --items "巨无霸" --kcal-max 100000
```

或直接读 `data/nutrition.json`。**答完就停**，不要顺势推荐套餐。

## 硬性约束

- **不要劝食，也不要劝不食。** 不出现"多吃点""别吃了""放心吃"这类引导。
  只描述数据与后果，把决定权留给用户。
- **不做诊断。** 用户提到具体疾病（糖尿病、高血压、肾病等）时，不给该病专属的饮食方案，
  只提供数据，并建议咨询医生或临床营养师。
- **每餐钠超 800 mg 必须主动提示。** 这是本项目发现的最普遍问题，隐瞒它等于没做参谋。
- **数据有来源。** 引用营养数值时说明来自麦当劳中国 MCP 的 `list-nutrition-foods`，
  且"以官方渠道当日信息为准"。
- **下单前必须显式确认。** 任何 `create-order` / `cancel-order` 之前，
  把门店、餐品、金额完整列给用户，等一个明确的"确认下单"。

## 目录结构

```
mcd-nutrition-coach/
├── SKILL.md                  # 本文件：Skill 定义与工作流
├── README.md                 # 项目介绍、安装、示例
├── MCP_INTEGRATION.md        # 用到的 MCP Tool、调用流程与业务价值
├── mcp-config.example.json   # 脱敏配置示例（只含环境变量占位符）
├── CONTEST_DECLARATION.md    # 参赛声明（官方原文，不可修改）
├── data/
│   ├── nutrition.json        # 结构化营养库（160 项，由 MCP 生成）
│   └── raw/                  # MCP 原始返回，留档可复现
├── scripts/
│   ├── parse_nutrition.py    # MCP 返回 → 结构化 JSON
│   ├── mcd_nutrition.py      # 共享内核：归类、匹配、汇总、换法建议
│   ├── meal_planner.py       # 预算配餐
│   └── order_check.py        # 下单前体检
└── tests/
    └── test_smoke.py         # 零依赖自测（16 项）
```

## 维护提示

营养数据会随菜单调整变化。建议每次开工前先刷新一次：

```bash
# 让 MCP 重新拉取，然后落盘
python scripts/parse_nutrition.py data/raw/nutrition-raw.json -o data/nutrition.json
```

`data/raw/nutrition-raw.json` 里保存的是 MCP 的原始响应，
所以任何时候都能不依赖网络复现同一份营养库。
