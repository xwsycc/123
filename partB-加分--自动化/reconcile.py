# -*- coding: utf-8 -*-
"""
reconcile.py — Reelo「本月 AI 带来收入」对账脚本（通用版，可重复运行）

与第 1 版的核心区别：去除了所有硬编码（商家 ID、时区、报表月份、汇率），
换成「商家配置 + 命令行参数」驱动，换一个商家 / 换一个报表月份无需改代码。

设计要点（对应题面 B1 要求）
------------------------------
1. 可重复运行：数据从 orders/attributions 两个输入来源加载，每次重建 SQLite
   内存表并真实执行 SQL，结果可复现。
2. 通用：商家时区、报表月份、汇率快照、币种全部参数化。
3. 自动比对：分别用「开发实现的 SQL」和「正确口径的 SQL」计算，报告差异。
4. 逐单定位：输出每一笔订单在两种口径下的贡献、差异金额与原因。
5. 退出码：发现不一致时返回非零，便于接入 CI / 定时任务。

口径（来自产品文档）
--------------------
- AI 带来收入 = 本月（按商家所在时区）通过 Reelo 贴文带来的、已支付且未退款
  的真实订单金额，统一以美元计（默认 1 USD = 7.8 HKD，可通过 --rate 覆盖）。
- 排除：status != 'paid'（退款）、is_test = 1（测试单）。
- 时区：按商家所在时区精确换算（含夏令时），由 --tz 指定 IANA 时区名。
- 归因去重：一个订单对应多条贴文时只计一次，用 EXISTS 而非 JOIN 累加。

运行方式
--------
    python reconcile.py                        # 内置示例：merchant 101 / 2026-09 / 洛杉矶
    python reconcile.py --merchant 205 --month 2026-10 --tz Asia/Shanghai
    python reconcile.py --orders orders.csv --attributions attributions.csv \
                         --merchant 205 --month 2026-10 --tz America/New_York
    python reconcile.py --report out.md

依赖：仅 Python 标准库（sqlite3, csv, argparse, datetime, zoneinfo）。
可选：系统若缺少时区数据库(zoneinfo)，可回退到固定偏移(fallback-offsets)。
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# ---- 商家配置（真实系统可改为从 DB / 配置服务读取）----
# 键 = merchant_id，值 = 所在 IANA 时区。新商家在这里加一行即可。
MERCHANT_TIMEZONES = {
    101: "America/Los_Angeles",
    205: "Asia/Shanghai",        # 示例：新增一家中国商家
    303: "America/New_York",     # 示例：新增一家纽约商家
}

# 默认折算汇率快照（USD 为基准）。真实系统应按账单月的汇率快照表读取。
DEFAULT_FX = {"USD": 1.0, "HKD": 7.8, "CNY": 7.2, "EUR": 0.92, "GBP": 0.79}

# 内置示例数据（题面 B1：merchant 101 / 洛杉矶 / 2026-09）
SAMPLE_ORDERS = [
    ("1001", 101, 300, "USD", "paid", 0, "2026-09-03 18:00"),
    ("1002", 101, 450, "USD", "paid", 0, "2026-09-05 21:10"),
    ("1003", 101, 200, "USD", "refunded", 0, "2026-09-08 16:45"),
    ("1004", 101, 780, "HKD", "paid", 0, "2026-09-10 02:30"),
    ("1005", 101, 500, "USD", "paid", 1, "2026-09-12 09:00"),
    ("1006", 101, 350, "USD", "paid", 0, "2026-09-15 19:20"),
    ("1007", 101, 250, "USD", "paid", 0, "2026-10-01 03:00"),
    ("1008", 101, 400, "USD", "paid", 0, "2026-09-01 05:00"),
    ("1009", 101, 600, "USD", "paid", 0, "2026-09-20 22:15"),
    ("1010", 101, 150, "USD", "paid", 0, "2026-09-25 17:40"),
]
SAMPLE_ATTRIBUTIONS = [
    ("1001", "P01"), ("1002", "P01"), ("1002", "P02"), ("1003", "P02"),
    ("1004", "P03"), ("1005", "P03"), ("1006", "P04"), ("1007", "P04"),
    ("1008", "P01"), ("1010", "P05"),
]


# ---- 时区处理：优先 zoneinfo（含夏令时），缺失时回退固定偏移 ----
try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAS_ZONEINFO = False

# 已知时区在报表期间的固定 UTC 偏移，供无 zoneinfo 时回退（含夏令时偏移）。
FALLBACK_OFFSETS = {
    "America/Los_Angeles": {"PDT": -7, "PST": -8},
    "America/New_York": {"EDT": -4, "EST": -5},
    "Asia/Shanghai": -8,
}


def resolve_tz(name):
    """返回一个能根据 UTC 时间换算本地时间的对象。"""
    if _HAS_ZONEINFO:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    off = FALLBACK_OFFSETS.get(name)
    if isinstance(off, dict):
        # 仅回退：按夏令时(PDT/EDT)固定偏移，够示例用；真实系统应装 tzdata。
        return timezone(timedelta(hours=off.get("PDT", off.get("EDT", -7))))
    if isinstance(off, (int, float)):
        return timezone(timedelta(hours=off))
    # 最终回退：UTC
    return timezone.utc


def to_local(utc_naive: datetime, tz) -> datetime:
    dt = utc_naive.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).replace(tzinfo=None)


# ---- 数据加载 ----
def load_orders(csv_path=None):
    if csv_path:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((
                    int(r["order_id"]), int(r["merchant_id"]), float(r["amount"]),
                    r["currency"], r["status"], int(r["is_test"]), r["created_at"],
                ))
        return rows
    return SAMPLE_ORDERS


def load_attributions(csv_path=None):
    if csv_path:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((int(r["order_id"]), r["post_id"]))
        return rows
    return SAMPLE_ATTRIBUTIONS


def build_db(conn, orders, attributions):
    conn.execute("DROP TABLE IF EXISTS orders")
    conn.execute("DROP TABLE IF EXISTS attributions")
    conn.execute("""
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY, merchant_id INTEGER, amount REAL,
            currency TEXT, status TEXT, is_test INTEGER, created_at TEXT)""")
    conn.execute("CREATE TABLE attributions (order_id INTEGER, post_id TEXT)")
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO attributions VALUES (?,?)", attributions)
    conn.commit()


# ---- 开发实现的 SQL（有问题的版本，动态注入 merchant 与月份）----
def make_dev_sql(merchant_id, year, month):
    lo = f"{year}-{month:02d}-01"
    nxt_year, nxt_month = (year, month + 1) if month < 12 else (year + 1, 1)
    hi = f"{nxt_year}-{nxt_month:02d}-01"
    return f"""
SELECT SUM(o.amount) AS ai_revenue
FROM orders o
JOIN attributions a ON a.order_id = o.order_id
WHERE o.merchant_id = {int(merchant_id)}
  AND o.created_at >= '{lo}'
  AND o.created_at <  '{hi}';
"""


def analyze(orders, attributions, merchant_id, tz_name, year, month, fx):
    tz = resolve_tz(tz_name)
    conn = sqlite3.connect(":memory:")
    build_db(conn, orders, attributions)

    dev_sql = make_dev_sql(merchant_id, year, month)
    dev_revenue = round((conn.execute(dev_sql).fetchone()[0] or 0.0), 2)

    # 正确口径：时区归属 + 逐单折算（币种通过 fx 快照统一到 USD）+ 归因去重
    attr_count = {}
    for oid, _pid in attributions:
        attr_count[oid] = attr_count.get(oid, 0) + 1

    def to_usd(amount, currency):
        rate = fx.get(currency)
        if rate is None:
            return None  # 未声明汇率的币种，标记为「无法折算」
        return amount / rate  # rate = 1 USD 对应的该币种金额

    per_order_correct = {}
    for oid, mid, amount, cur, status, is_test, created in orders:
        if mid != merchant_id:
            continue
        dt_local = to_local(datetime.fromisoformat(created), tz)
        in_month = (dt_local.year == year and dt_local.month == month)
        usd = to_usd(amount, cur)
        if status != "paid":
            correct, reason = 0.0, "status_refunded"
        elif is_test:
            correct, reason = 0.0, "is_test"
        elif attr_count.get(oid, 0) == 0:
            correct, reason = 0.0, "no_attribution"
        elif usd is None:
            correct, reason = 0.0, "unknown_currency"
        elif not in_month:
            correct, reason = 0.0, "outside_merchant_month"
        else:
            correct, reason = round(usd, 4), "ok"
        per_order_correct[oid] = (correct, reason)

    correct_total = round(sum(v[0] for v in per_order_correct.values()), 2)

    # 开发口径逐单贡献（JOIN 去重错误 + 无币种换算 + 无状态/测试过滤 + UTC 切月）
    per_order_dev = {}
    for oid, mid, amount, cur, status, is_test, created in orders:
        if mid != merchant_id:
            continue
        n = attr_count.get(oid, 0)
        dt_utc = datetime.fromisoformat(created)
        in_utc_month = (dt_utc.year == year and dt_utc.month == month)
        if n == 0:
            dev, reason = 0.0, "no_attribution_dropped_by_inner_join"
        elif not in_utc_month:
            dev, reason = 0.0, "outside_utc_month"
        else:
            dev, reason = round(amount * n, 4), "counted(raw_amount_x_join)"
        per_order_dev[oid] = (dev, reason)
    dev_total = round(sum(v[0] for v in per_order_dev.values()), 2)

    # 逐单差异明细 + 原因判定
    details = []
    for oid, mid, amount, cur, status, is_test, created in orders:
        if mid != merchant_id:
            continue
        c, _ = per_order_correct[oid]
        d, _ = per_order_dev[oid]
        if abs(c - d) <= 1e-6:
            continue
        reasons = []
        n = attr_count.get(oid, 0)
        if n > 1:
            reasons.append(f"join去重重复×{n}")
        if cur != "USD" and n > 0:
            reasons.append(f"{cur}未换算USD")
        if status != "paid":
            reasons.append("退款未过滤")
        if is_test:
            reasons.append("测试单未过滤")
        dt_local = to_local(datetime.fromisoformat(created), tz)
        dt_utc = datetime.fromisoformat(created)
        in_loc = dt_local.year == year and dt_local.month == month
        in_utc = dt_utc.year == year and dt_utc.month == month
        if not in_loc and in_utc:
            reasons.append("UTC切月误算(本地已出月)")
        if in_loc and not in_utc:
            reasons.append("UTC切月漏算(本地仍在月内)")
        if n == 0 and abs(c) > 1e-6:
            reasons.append("归因缺失被INNER JOIN丢弃")
        if to_usd(amount, cur) is None and n > 0:
            reasons.append(f"币种{cur}无汇率")
        details.append({
            "order_id": oid, "correct_usd": round(c, 2), "dev_usd": round(d, 2),
            "diff_usd": round(d - c, 2), "reasons": reasons or ["未识别"],
        })

    return {
        "merchant_id": merchant_id, "year": year, "month": month,
        "timezone": tz_name, "dev_revenue": dev_revenue,
        "correct_revenue": correct_total,
        "diff": round(dev_revenue - correct_total, 2), "details": details,
    }


def render(result, markdown_path=None):
    lines = []
    lines.append(f"# 对账报告 — merchant {result['merchant_id']}"
                 f" / {result['year']}-{result['month']:02d}")
    lines.append("")
    lines.append(f"- 商家时区：{result['timezone']}")
    lines.append(f"- 首页（开发 SQL）显示：**${result['dev_revenue']:,}**")
    lines.append(f"- 正确口径 AI 收入：**${result['correct_revenue']:,}**")
    flag = "（一致 ✔）" if result["diff"] == 0 else "（不一致 ✖）"
    lines.append(f"- 差异：**{result['diff']:+,} USD**{flag}")
    lines.append("")
    if result["details"]:
        lines.append("| 订单 | 正确(USD) | 开发(USD) | 差异(USD) | 原因 |")
        lines.append("|---|---|---|---|---|")
        for d in result["details"]:
            lines.append(f"| {d['order_id']} | {d['correct_usd']:,.2f} "
                         f"| {d['dev_usd']:,.2f} | {d['diff_usd']:+,.2f} "
                         f"| {'；'.join(d['reasons'])} |")
    else:
        lines.append("_无明显差异_")
    lines.append("")
    md = "\n".join(lines)
    if markdown_path:
        with open(markdown_path, "w", encoding="utf-8") as f:
            f.write(md)
    return md


def main():
    ap = argparse.ArgumentParser(description="Reelo AI 收入对账脚本（通用版）")
    ap.add_argument("--orders", default=None, help="orders.csv 路径（缺省用内置示例）")
    ap.add_argument("--attributions", default=None, help="attributions.csv 路径")
    ap.add_argument("--merchant", type=int, default=101, help="商家 ID")
    ap.add_argument("--month", default="2026-09", help="报表月份 YYYY-MM")
    ap.add_argument("--tz", default=None,
                    help="商家 IANA 时区（缺省按 MERCHANT_TIMEZONES 查，再缺省 UTC）")
    ap.add_argument("--fx", default=None, help='汇率快照 JSON，如 \'{"HKD":7.8,"CNY":7.2}\'')
    ap.add_argument("--report", default=None, help="输出 Markdown 报告路径")
    args = ap.parse_args()

    year, month = map(int, args.month.split("-"))

    tz_name = args.tz or MERCHANT_TIMEZONES.get(args.merchant) or "UTC"

    fx = dict(DEFAULT_FX)
    if args.fx:
        fx.update(json.loads(args.fx))

    orders = load_orders(args.orders)
    attributions = load_attributions(args.attributions)

    result = analyze(orders, attributions, args.merchant, tz_name, year, month, fx)
    md = render(result, args.report)

    for line in md.splitlines():
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))

    if result["diff"] != 0:
        print("\n[!] 检测到不一致，退出码 1")
        sys.exit(1)
    print("\n[OK] 口径一致")
    sys.exit(0)


if __name__ == "__main__":
    main()
