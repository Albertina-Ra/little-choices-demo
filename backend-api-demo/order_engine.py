"""Local CSV order cleanup. Demo records are synthetic; no network access."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

FIELDS = ["店铺", "流水号", "订单号", "商品编号", "日期", "类型", "数量", "金额"]


def text(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def safe_cell(value):
    """Quote formula-like CSV text. Numeric values are written as numbers."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def cents(value):
    s = text(value)
    s = re.sub(r"^(?:CNY|RMB|¥|￥)\s*", "", s, flags=re.I)
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", s):
        raise ValueError("金额须为非负人民币数值，最多两位小数")
    try:
        n = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("金额无效") from exc
    if n > Decimal("999999999.99"):
        raise ValueError("金额超出本工具处理范围")
    return int(n * 100)


def normalize(row):
    r = {k: text(row.get(k)) for k in FIELDS}
    for k in ("店铺", "流水号", "订单号", "商品编号"):
        if not r[k]:
            raise ValueError(f"缺少{k}")
    date_value = r["日期"]
    if not re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", date_value):
        raise ValueError("日期须为年/月/日或年-月-日")
    try:
        r["日期"] = datetime.strptime(date_value.replace("/", "-"), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("日期不存在") from exc
    if r["类型"] not in ("销售", "退款"):
        raise ValueError("类型须为销售或退款，其他状态需先约定")
    if not re.fullmatch(r"\d+", r["数量"]):
        raise ValueError("数量须为非负整数")
    r["数量"] = int(r["数量"])
    if r["类型"] == "销售" and r["数量"] == 0:
        raise ValueError("销售数量不能为零")
    r["金额分"] = cents(r.pop("金额"))
    return r


def read_csv(path):
    data = path.read_bytes()
    content = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            content = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise ValueError(f"无法识别文件编码: {path.name}")
    reader = csv.DictReader(io.StringIO(content, newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise ValueError(f"重复列名: {path.name}")
    if set(headers) != set(FIELDS):
        raise ValueError(f"{path.name}: 列名必须恰好为 {FIELDS}，请先完成字段映射")
    result = []
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"{path.name}: 第 {reader.line_num} 物理行的列数不正确")
        result.append({"source": path.name, "line": reader.line_num, "raw": row})
    return result


def analyze(records):
    groups, issues, clean, duplicates = defaultdict(list), [], [], []
    for record in records:
        key = tuple(text(record["raw"].get(k)) for k in ("店铺", "流水号"))
        if not all(key):
            issues.append({**record, "reason": "店铺或流水号缺失，无法确定交易主键"})
        else:
            groups[key].append(record)
    for group in groups.values():
        valid, failures = [], []
        for record in group:
            try:
                valid.append((record, normalize(record["raw"])))
            except ValueError as exc:
                failures.append(str(exc))
        signatures = {json.dumps(v, ensure_ascii=False, sort_keys=True) for _, v in valid}
        if failures or len(signatures) != 1:
            reason = "；".join(sorted(set(failures))) if failures else "同店铺同流水号存在字段冲突，全部保留待核对"
            if len(group) > 1 and failures:
                reason = "同主键存在无效版本，全部保留待核对；" + reason
            issues.extend({**r, "reason": reason} for r in group)
        else:
            original, normalized = valid[0]
            clean.append({**normalized, "来源文件": original["source"], "来源物理行": original["line"]})
            duplicates.extend({**r, "reason": f"与 {original['source']} 第 {original['line']} 物理行规范化结果一致"} for r, _ in valid[1:])
    sales = sum(r["金额分"] for r in clean if r["类型"] == "销售")
    refunds = sum(r["金额分"] for r in clean if r["类型"] == "退款")
    assert len(records) == len(clean) + len(duplicates) + len(issues)
    return {"synthetic": False, "rules": "一行是一笔独立销售或退款流水；店铺+流水号为主键；金额为非负人民币；退款另列；冲突主键全部隔离。", "sources": sorted({r["source"] for r in records}), "input_rows": len(records), "clean": clean, "duplicates": duplicates, "issues": issues, "sales_cents": sales, "refunds_cents": refunds, "net_cents": sales-refunds}


def write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows([[safe_cell(v) for v in row] for row in rows])


def make_demo(output):
    rows = []
    for i in range(1, 19):
        rows.append([("春日杂货", "木白文具", "南风小店")[(i-1)%3], f"T{i:04d}", f"000{i:05d}", f"SKU-{i%4+1:02d}", f"2026-11-{(i-1)%6+1:02d}", "销售", i%3+1, f"{(i*1379)%18000/100:.2f}"])
    rows.extend([
        ["春日杂货", "R0001", "00000001", "SKU-02", "2026/11/7", "退款", 1, "13.79"],
        ["木白文具", "R0002", "00000002", "SKU-03", "2026-11-7", "退款", 0, "0.00"],
        ["春日杂货", "T0020", "00000001", "SKU-02", "2026-11-8", "销售", 1, "25.00"],
    ])
    rows.append(rows[0].copy())
    rows.append(["春日杂货", "C0001", "00000030", "SKU-01", "2026-11-8", "销售", 1, "80.00"])
    rows.append(["春日杂货", "C0001", "00000030", "SKU-01", "2026-11-8", "销售", 1, "90.00"])
    rows.append(["南风小店", "E0001", "00000040", "SKU-01", "2026-02-30", "销售", 1, "20.00"])
    rows.append(["南风小店", "E0002", "00000041", "SKU-02", "2026-11-8", "销售", 1, ""])
    paths = [output / "演示_订单A.csv", output / "演示_订单B.csv"]
    for path, part in zip(paths, (rows[:14], rows[14:])):
        write_csv(path, FIELDS, part)
    return paths


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo", action="store_true")
    p.add_argument("--input", nargs="+", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if bool(a.demo) == bool(a.input):
        p.error("请选择 --demo 或 --input，两者不能同时使用")
    a.output.mkdir(parents=True, exist_ok=True)
    paths = make_demo(a.output) if a.demo else a.input
    resolved = [x.resolve() for x in paths]
    if len(resolved) != len(set(resolved)) or len({x.name for x in paths}) != len(paths):
        p.error("输入文件重复或存在重名文件，请先区分来源")
    report = analyze([r for path in paths for r in read_csv(path)])
    report["synthetic"] = a.demo
    (a.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    headers = FIELDS + ["来源文件", "来源物理行"]
    data = [[r.get(k) if k != "金额" else f"{Decimal(r['金额分'])/100:.2f}" for k in headers] for r in report["clean"]]
    write_csv(a.output / "整理明细.csv", headers, data)
    for key, name in (("issues", "待核对.csv"), ("duplicates", "重复记录.csv")):
        write_csv(a.output/name, ["来源文件", "来源物理行", "原因"]+FIELDS, [[r["source"], r["line"], r["reason"]]+[r["raw"].get(k) for k in FIELDS] for r in report[key]])
    print(json.dumps({"input_rows": report["input_rows"], "clean": len(report["clean"]), "duplicates": len(report["duplicates"]), "issues": len(report["issues"]), "sales_cents": report["sales_cents"], "refunds_cents": report["refunds_cents"], "net_cents": report["net_cents"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
