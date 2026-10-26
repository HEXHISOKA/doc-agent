"""从 eval/report.json 挖失败案例与分组趋势，输出可写进报告的细节。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rep = json.loads((ROOT / "eval" / "report.json").read_text(encoding="utf-8"))
gt = json.loads((ROOT / "eval" / "set" / "gt.json").read_text(encoding="utf-8"))
recs = rep["records"]

print("=" * 82)
print("一、单据编号预测对照（doc_no：字符保真的关键字段）")
print("=" * 82)
print(f"{'文件':30s} {'真值':16s} {'A(OCR)':16s} {'B(直读)':16s}")
bad_b = 0
for r in recs:
    t = gt[r["file"]]["doc_no"]
    a = (r["chains"].get("A", {}).get("data") or {}).get("doc_no")
    b = (r["chains"].get("B", {}).get("data") or {}).get("doc_no")
    fa = "OK" if str(a or "").replace("-", "").upper() == t.upper() else "ERR"
    fb = "OK" if str(b or "").replace("-", "").upper() == t.upper() else "ERR"
    if fb == "ERR":
        bad_b += 1
    print(f"{r['file']:30s} {t:16s} {str(a):16s}{fa:4s} {str(b):16s}{fb}")
print(f"\nB 链路编号错误 {bad_b}/{len(recs)}")
errs_b = [((r['chains'].get('B',{}).get('data') or {}).get('doc_no') or '') for r in recs]
print("B 链路出现的编号形态：", sorted({e for e in errs_b if e})[:12])

print()
print("=" * 82)
print("二、明细行错误定位（链路 A）")
print("=" * 82)
for r in recs:
    c = r["chains"].get("A", {})
    rows = c.get("rows") or []
    bad = [x for x in rows if not all(x["ok"].values())]
    if not bad:
        continue
    print(f"\n{r['file']}  ({r['variant']}/{r['labels']}/d{r['dirt']})  错 {len(bad)} 行")
    for x in bad:
        ti = gt[r["file"]]["items"]
        t = ti[x["i"] - 1] if x["i"] - 1 < len(ti) else None
        pw = (c["data"] or {}).get("items") or []
        p = pw[x["i"] - 1] if x["i"] - 1 < len(pw) else None
        print(f"  行{x['i']} 错字段={[k for k, v in x['ok'].items() if not v]}")
        if t:
            print(f"    真值 name={t['物料名称']!r} qty={t['数量']} price={t['单价']} amount={t['金额']}")
        if p:
            print(f"    预测 name={p.get('name')!r} qty={p.get('qty')} price={p.get('price')} amount={p.get('amount')}")

print()
print("=" * 82)
print("三、按脏化等级分组")
print("=" * 82)
print(f"{'dirt':6s} {'n':>3s} {'A全对':>7s} {'A自动入库':>10s} {'A误放行':>8s} {'A正确拦截':>10s} {'B全对':>7s} {'B自动入库':>10s}")
for d in sorted({r["dirt"] for r in recs}):
    rs = [r for r in recs if r["dirt"] == d]
    row = [f"{d:<6d} {len(rs):>3d}"]
    for tag in ("A", "B"):
        q = {"pc": 0, "pw": 0, "fw": 0, "fc": 0}
        full = 0
        for r in rs:
            c = r["chains"].get(tag) or {}
            if not c.get("data"):
                continue
            ok = (c["key_all"] and c["row_cell_ok"] == c["row_cell_total"]
                  and c.get("rows_pred") == r["rows_truth"])
            full += bool(ok)
            if c["pass"] and ok:
                q["pc"] += 1
            elif c["pass"]:
                q["pw"] += 1
            elif not ok:
                q["fw"] += 1
            else:
                q["fc"] += 1
        row.append(f"{full}/{len(rs):<5d} {q['pc']}/{len(rs):<8d} {q['pw']:>8d} {q['fw']:>10d}")
    print("  ".join(row))

print()
print("=" * 82)
print("四、按版式分组")
print("=" * 82)
for v in sorted({r["variant"] for r in recs}):
    rs = [r for r in recs if r["variant"] == v]
    line = [f"{v:8s} n={len(rs):2d}"]
    for tag in ("A", "B"):
        full = 0
        for r in rs:
            c = r["chains"].get(tag) or {}
            if not c.get("data"):
                continue
            full += bool(c["key_all"] and c["row_cell_ok"] == c["row_cell_total"]
                         and c.get("rows_pred") == r["rows_truth"])
        line.append(f"{tag}:{full}/{len(rs)}")
    print("  ".join(line))

print()
print("=" * 82)
print("五、误放行详情（校验通过但结果有错 —— 最危险）")
print("=" * 82)
for tag in ("A", "B"):
    for r in recs:
        c = r["chains"].get(tag) or {}
        if not c.get("data"):
            continue
        ok = (c["key_all"] and c["row_cell_ok"] == c["row_cell_total"]
              and c.get("rows_pred") == r["rows_truth"])
        if c["pass"] and not ok:
            print(f"{tag} {r['file']}  明细{c['row_cell_ok']}/{c['row_cell_total']}  "
                  f"错字段={[k for k, v in c['key_fields'].items() if not v]}")
            print(f"   真值 supplier={gt[r['file']]['supplier']!r}")
            print(f"   预测 supplier={(c['data'] or {}).get('supplier')!r}")
            print(f"   warnings={(c['data'] or {}).get('warnings')}")
