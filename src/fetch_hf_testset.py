"""从 HuggingFace `AyoubChLin/Company-document-dataset-v2` 抽取采购 / 到货类单据。

为什么换数据源：上一版用 Wikimedia 的图，14 张里 9 张是增值税发票、国外样例发票、
航空行程单、零售小票——那些是**财务报销与零售**场景，不是采购入库。
拿它们测出来的"12 张拦截"大部分是"发票本来就没有采购订单号"这种假拦截。

这个数据集的类别与业务同构：
    purchase_order       采购订单（发行方向供应商下单）
    goods_received_note  收货单（对照 PO 核对 订购/实收/拒收/验收）
而且 `scans` 子集是**降质后的页面图**（scan: 轻微倾斜+噪声+JPEG；photo: 透视+光照+模糊），
比"设计稿样张"更接近线上真实。每张还带 words + 字段 bbox，能算准确率而不只是"拦没拦"。

许可：Apache-2.0（生成文档与代码），源库 MIT/BSD。属可分发的公开数据。
语言：只有英语与法语，**没有中文**。所以它的定位是"测跨版式/跨语言/跨退化的算法能力"，
中文国内版式另由 gen_format_variants.py 造。

用法：
    python src/fetch_hf_testset.py                       # 默认每类 6 张
    python src/fetch_hf_testset.py --per-type 4 --refresh
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "hf"
OUT = ROOT / "samples" / "hf"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
REPO = "AyoubChLin/Company-document-dataset-v2"
MIRROR = "https://hf-mirror.com/datasets/" + REPO + "/resolve/main"

# 只用采购到货闭环里的两类。invoice / receipt / payslip 虽然同一数据集里有，
# 但那是财务与零售，不是本项目的业务对象——这正是上一版栽的坑。
TYPES = {
    "purchase_order": "po_test.parquet",
    "goods_received_note": "grn_test.parquet",
}
NUMERIC = {"number", "issue_date", "line_total", "total", "total_ordered",
           "total_received", "total_rejected", "total_accepted"}


def ensure_parquet(name: str, local: str) -> Path:
    p = DATA / local
    if p.exists() and p.stat().st_size > 1_000_000:
        return p
    DATA.mkdir(parents=True, exist_ok=True)
    url = f"{MIRROR}/scans/{name}/test-00000.parquet"
    print(f"  下载 {name} …")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r, p.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return p


def _center(b: list[float]) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def assemble(words: list[dict], box: list[float]) -> str:
    """把落在 box 内的词按「先行后列」拼成字段值。

    直接按 (y, x) 排会出错：同一行两个词的字高略有差异时，y 会被排成两行，
    得到 "Sports SAS Union" 这种词序颠倒的结果（实测过）。所以先按 y 中心聚类成行。
    """
    hit = [w for w in words
           if box[0] - 2 <= _center(w["bbox"])[0] <= box[2] + 2
           and box[1] - 2 <= _center(w["bbox"])[1] <= box[3] + 2]
    if not hit:
        return ""
    hit.sort(key=lambda w: _center(w["bbox"])[1])
    lines: list[list] = []
    for w in hit:
        yc = _center(w["bbox"])[1]
        if lines and abs(yc - _center(lines[-1][-1]["bbox"])[1]) <= 8:
            lines[-1].append(w)
        else:
            lines.append([w])
    return " ".join(" ".join(x["text"] for x in sorted(ln, key=lambda z: z["bbox"][0]))
                    for ln in lines)


def norm_gold(field: str, raw: str) -> str:
    """数字类字段去掉拼接空格（金额常被 OCR 拆成 "385" "," "20" 三个词）。"""
    key = field.split(".")[-1]
    if key in NUMERIC or field in {"issue_date"}:
        return re.sub(r"\s+", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def gold_from_row(words_json: str, boxes_json: str) -> dict:
    words = json.loads(words_json)
    boxes = json.loads(boxes_json)
    out: dict = {}
    for field, bl in boxes.items():
        bs = bl if (bl and isinstance(bl[0], list)) else [bl]
        vals = [assemble(words, b) for b in bs]
        # 同一字段多个 bbox（如单号在页眉与页脚各出现一次）：取第一个非空，
        # 若全部相同则去重；不同值的话保留第一个并在 raw 里留痕
        vals = [v for v in vals if v]
        if not vals:
            out[field] = ""
        else:
            uniq = list(dict.fromkeys(vals))
            out[field] = norm_gold(field, uniq[0])
            if len(uniq) > 1:
                out[field + "__variants"] = uniq
    return out


def to_payload(row, i: int, doc_type: str) -> dict:
    im = Image.open(io.BytesIO(row["image"]["bytes"]))
    return {
        "bytes": row["image"]["bytes"],
        "image": im,
        "doc_id": row["doc_id"],
        "page": row["page"],
        "profile": row["profile"],
        "wh": [im.width, im.height],
        "params": json.loads(row["params"]),
        "gold": gold_from_row(row["words"], row["boxes"]),
        "row": i,
    }


def pick_rows(table, per_type: int) -> list[dict]:
    """按 profile 交替取样，且 doc_id 去重——同一个 doc 的两页不能算两个样本。"""
    by_prof: dict[str, list[int]] = {}
    for i in range(table.num_rows):
        by_prof.setdefault(table.column("profile")[i].as_py(), []).append(i)
    picked, used_docs = [], set()
    cursors = {k: 0 for k in by_prof}
    profiles = sorted(by_prof)
    while len(picked) < per_type and any(cursors[k] < len(by_prof[k]) for k in profiles):
        for prof in profiles:
            while cursors[prof] < len(by_prof[prof]):
                i = by_prof[prof][cursors[prof]]
                cursors[prof] += 1
                doc = table.column("doc_id")[i].as_py()
                if doc in used_docs:
                    continue
                used_docs.add(doc)
                picked.append(i)
                break
            if len(picked) >= per_type:
                break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=6)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    manifest = []
    for doc_type, local in TYPES.items():
        p = ensure_parquet(doc_type, local)
        table = pq.read_table(p)
        rows = pick_rows(table, args.per_type)
        print(f"{doc_type}: 共 {table.num_rows} 行，取 {len(rows)} 张")

        for i in rows:
            rec = to_payload(
                {c: table.column(c)[i].as_py() for c in table.column_names}, i, doc_type)
            name = f"{doc_type}_{rec['doc_id'][:8]}_{rec['profile']}"
            d = OUT / doc_type
            d.mkdir(parents=True, exist_ok=True)
            fp = d / f"{name}.jpg"
            if args.refresh or not fp.exists():
                rec["image"].save(fp, quality=93, subsampling=0)
            manifest.append({
                "name": name,
                "type": doc_type,
                "file": str(fp.relative_to(ROOT)).replace("\\", "/"),
                "doc_id": rec["doc_id"],
                "page": rec["page"],
                "profile": rec["profile"],
                "wh": rec["wh"],
                "degradation": rec["params"],
                "gold": rec["gold"],
                "source": {
                    "dataset": REPO,
                    "subset": "scans",
                    "split": "test",
                    "row": rec["row"],
                    "license": "Apache-2.0",
                    "url": f"https://huggingface.co/datasets/{REPO}",
                },
            })
            print(f"  {name:44s} {rec['wh']} 折角{rec['params']['angle']:+.2f} "
                  f"模糊{rec['params']['blur']:.2f} jpeg{rec['params']['jpeg']} "
                  f"字段{len([k for k in rec['gold'] if not k.endswith('__variants')])}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n导出 {len(manifest)} 张 -> {OUT}")
    print("gold 值示例：")
    for m in manifest[:2]:
        print(f"  [{m['name']}]")
        for k, v in m["gold"].items():
            print(f"    {k:26s} {v!r}")


if __name__ == "__main__":
    main()
