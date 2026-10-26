"""整理测试集：按业务域分组落盘，业务对的在前，反例在后。

为什么要重排：第一版把"跨业务类型的格式多样性"当成了"稳健性"——
14 张里 9 张是增值税发票、国外样例发票、航空行程单、零售小票。
那些是财务报销与零售场景，不是采购入库。测试结论因此有一半是假拦截
（"发票本来就没有采购订单号"这种不算系统发现问题）。

现在的分组：
    a_hf_procurement   12 张采购订单 / 收货单，来自 HuggingFace 公开数据集，
                       **带字段级 gold**，介质为 scan / photo 降质，许可 Apache-2.0
    b_cn_synthetic      8 张中文单据（采购订单 ×3 + 含税 + 传真 + 复印 + 送货单 + 收货单），
                       仿国内 ERP 打印样式，无 gold 但埋了已知真值
    c_offdomain         9 张旧网图，**不属于本业务域**，仅作为"当初怎么找错数据"的留证

用法：
    python src/export_testset.py              # 全量导出（OCR 文本已存在则跳过）
    python src/export_testset.py --no-ocr
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
DEST = ROOT / "testset"

# ---------------------------------------------------------------- 样本定义
# A 组：文件名来自 samples/hf/manifest.json，类型与语言从该清单读
HF_DIR = ROOT / "samples" / "hf"

# B 组：(文件名, 类型, 语言, 版式/介质说明)
CN = [
    ("cn_po_standard.png", "采购订单（标准）", "zh", "A4 竖版 7 列，中文大写金额，红章压空白区"),
    ("cn_po_tax.png", "采购订单（含税）", "zh", "含税额与价税合计，明细=不含税口径"),
    ("cn_po_fax.png", "采购订单（传真件）", "zh", "同标准单，传真退化：压缩对比/条纹/边缘发黑"),
    ("cn_po_copy.png", "采购订单（复印件）", "zh", "同标准单，复印退化：灰底/黑边/重影"),
    ("cn_grn.png", "收货单", "zh", "订购/实收/拒收三列，**无金额**"),
    ("cn_delivery_note.png", "送货单（横版）", "zh", "A4 横版 7 列，供应商随货单"),
    ("cn_outbound_note.png", "材料出库单（竖版）", "zh", "内部单据，无外部供应商"),
    ("en_packing_list.png", "装箱单", "zh-en", "中英混排，**无金额列**"),
]

# C 组：旧网图，保留作为反例
OFF = [
    ("fapiao_standard.jpg", "增值税电子普通发票", "zh", "财务/税务凭证，非采购单据"),
    ("fapiao_jiangsu.png", "增值税普通发票（扫描）", "zh", "同上"),
    ("fapiao_shanghai.jpg", "增值税普通发票（拍照）", "zh", "同上"),
    ("cz_sample_invoice.png", "发票 FAKTURA", "cs", "国外样例发票，内容是网页制作服务"),
    ("de_rechnung.gif", "发票 Rechnung", "de", "国外样例发票"),
    ("us_invoice_lw.png", "发票 FACTURA", "es", "国外样例发票"),
    ("en_po_form.jpg", "采购预订单", "en", "**唯一业务域正确的一张**"),
    ("photo_po_bahamut.jpg", "商品价目采购单", "zh-Hant", "漫展摊位零售价目，非工业采购"),
    ("air_itinerary.jpg", "航空运输电子客票行程单", "zh", "差旅报销，与采购无关"),
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_probes() -> dict[str, dict]:
    """把几份跑批结果并成一张表：name -> record。"""
    out: dict[str, dict] = {}
    for f in ("out/formats_probe.json", "out/cn_po_probe.json",
              "out/cn_grn_probe.json", "out/hf_eval_all.json"):
        p = ROOT / f
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")):
            r = dict(r)
            r["data"] = r.get("data") or r.get("extract")
            out[r["name"]] = r
    return out


def ocr_text(path: Path) -> tuple[str, dict]:
    from layout import rebuild, to_markdown_table
    from ocr import run_ocr_array
    from preprocess import decode_image, suppress_red

    bgr = decode_image(path.read_bytes())
    bgr, pre = suppress_red(bgr)
    items = run_ocr_array(bgr)
    confs = [float(i.get("score") or 0) for i in items]
    return (to_markdown_table(rebuild(items)) if items else ""), {
        "words": len(items),
        "conf_avg": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "red_suppressed": (pre or {}).get("applied"),
    }


def build(group: str, name: str, kind: str, lang: str, layout: str,
          src: Path, probe: dict | None, gold: dict | None, source: dict | None,
          degradation: dict | None) -> dict | None:
    if not src.exists():
        print(f"  ! 缺文件 {src}")
        return None
    from validate import is_ok, validate

    dst_rel = f"images/{group}/{src.name}"
    dst = DEST / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or sha256(src) != sha256(dst):
        shutil.copy2(src, dst)

    data = (probe or {}).get("data")
    issues = validate(data) if data else []
    verdict = ("无文字" if (probe or {}).get("crash") == "ocr_empty"
               else "崩溃" if (probe or {}).get("crash")
               else "通过" if is_ok(issues) else "拦截") if probe else "未跑"
    return {
        "group": group,
        "name": name,
        "kind": kind,
        "language": lang,
        "layout": layout,
        "file": dst_rel,
        "sha256": sha256(src),
        "bytes": src.stat().st_size,
        "source": source or {},
        "degradation": degradation or {},
        "ground_truth": gold or {},
        "extract": data,
        "verdict": verdict,
        "issues": issues,
        "warnings": (data or {}).get("warnings") or [],
        "ocr": {
            "words": (probe or {}).get("ocr_n"),
            "conf_avg": (probe or {}).get("ocr_conf_avg"),
            "text_file": f"results/ocr_text/{name}.txt",
        },
        "timings_ms": {"total": (probe or {}).get("total_ms")},
    }


def brief(msg: str, n: int = 62) -> str:
    """表格里只放一句话：优先在第一个句号/分号处截断。

    原来的写法是硬切 50 字，结果截出「……合计 423.63 只」这种半截词——
    读者以为句子没写完，其实是切在了「只有」的中间。
    """
    for sep in ("。", "；"):
        k = msg.find(sep)
        if 0 < k <= n:
            return msg[:k]
    return msg[:n] + ("…" if len(msg) > n else "")


def write_readme(m: dict) -> None:
    recs = m["records"]
    A = [r for r in recs if r["group"] == "a_hf_procurement"]
    B = [r for r in recs if r["group"] == "b_cn_synthetic"]
    C = [r for r in recs if r["group"] == "c_offdomain"]

    def tally(rs):
        return (sum(1 for r in rs if r["verdict"] == "通过"),
                sum(1 for r in rs if r["verdict"] == "拦截"),
                sum(1 for r in rs if r["verdict"] == "无文字"))

    L: list[str] = []
    A_ = L.append
    A_("# 采购单据测试集（按业务域分组）")
    A_("")
    A_(f"生成于 {m['generated_at']}，模型 `{m['model']}`，OCR = RapidOCR(onnxruntime)，"
       f"共 {len(recs)} 张。")
    A_("")
    A_("## 先说清楚为什么重排")
    A_("")
    A_("第一版找了 14 张「不同格式」的单据，其中 9 张是增值税发票、国外样例发票、"
       "航空行程单、零售小票——那是**财务报销与零售**，不是采购入库。"
       "拿它们测出来的「11 张拦截」里，至少 4 张是**假拦截**：发票本来就没有采购订单号、"
       "行程单本来就没有供应商、小票本来就没有数量单价。那不是系统发现问题，是喂错了数据。")
    A_("")
    A_("「格式多样性」只有落在**同一个业务对象**上才有意义。所以现在分三组：")
    A_("")
    A_("| 组 | 张数 | 是什么 | 通过/拦截 |")
    A_("|---|---:|---|---|")
    A_(f"| **a_hf_procurement** | {len(A)} | 采购订单 + 收货单，HuggingFace 公开数据集，"
       f"带字段级 gold，scan/photo 降质 | {tally(A)[0]}/{tally(A)[1]} |")
    A_(f"| **b_cn_synthetic** | {len(B)} | 中文自造：仿国内 ERP 打印样式 + 传真/复印退化 | "
       f"{tally(B)[0]}/{tally(B)[1]} |")
    A_(f"| c_offdomain | {len(C)} | 旧网图，**不属于本业务域**，仅作反例留证 | "
       f"{tally(C)[0]}/{tally(C)[1]} |")
    A_("")
    A_("## A 组：真实业务样本（有 gold，能算准确率）")
    A_("")
    A_("来源：`AyoubChLin/Company-document-dataset-v2`（HuggingFace），许可 **Apache-2.0**，"
       "源库 MIT/BSD。类别 `purchase_order` 与 `goods_received_note`，"
       "`scans` 子集是**降质后的页面图**，`params` 记录了退化参数（倾角/模糊/噪声/JPEG 质量）。")
    A_("")
    A_("| 样本 | 类型 | 介质 | 倾角 | 模糊 | JPEG | 判定 | 说明 |")
    A_("|---|---|---|---:|---:|---:|---|---|")
    for r in A:
        d = r["degradation"]
        A_(f"| `{r['name']}` | {r['kind']} | {d.get('profile', '')} | "
           f"{d.get('angle', 0):+.2f} | {d.get('blur', 0):.2f} | {d.get('jpeg', '')} | "
           f"**{r['verdict']}** | {'；'.join(brief(i['msg'], 46) for i in r['issues'] if i['level'] == 'err') or '无 err'} |")
    A_("")
    A_("这批数据的关键价值：**gold 是字段级的，而且生成侧保证过算术自洽**"
       "（行小计、税额、合计都对）。所以在这里「校验报 err」可以直接读成**误报**，"
       "「字段与 gold 不符」可以直接读成**抽取错误**。第一版那批网图两件事都做不到。")
    A_("")
    A_("## B 组：中文自造（业务主场景）")
    A_("")
    A_("公开渠道拿不到中文企业采购单据原件——这类文件含供应商报价与商业条款，不会公开。"
       "所以按国内 ERP 的打印样式自己造，并做了介质退化。")
    A_("")
    A_("| 样本 | 类型 | 版式/介质 | 判定 | 依据 |")
    A_("|---|---|---|---|---|")
    for r in B:
        A_(f"| `{r['name']}` | {r['kind']} | {r['layout']} | **{r['verdict']}** | "
           f"{'；'.join(brief(i['msg']) for i in r['issues'] if i['level'] == 'err') or '无 err'} |")
    A_("")
    A_("## 判定口径")
    A_("")
    A_("| 词 | 含义 |")
    A_("|---|---|")
    A_("| **err** | 阻断级疑点，必须人工确认才能入库 |")
    A_("| **warn** | 提醒级，不阻断（币种非人民币、含税口径、无价单据缺金额、"
       "无价单据却填了币种） |")
    A_("| **通过** | 无 err |")
    A_("| **拦截** | 至少 1 条 err |")
    A_("")
    A_("**通过 ≠ 数据正确，拦截 ≠ 单据有错。** 前者只说明「没查到矛盾」，"
       "后者只说明「系统无法确认它对」。")
    A_("")
    A_("## 这一轮发现并修掉的问题")
    A_("")
    A_("| # | 问题 | 触发它的样本 | 修法 |")
    A_("|---|---|---|---|")
    A_("| 1 | 合计含税/含运费时，「明细之和=合计」必然不成立，原逻辑报 err 并建议"
       "**把含税总额改成税前小计** | A 组 4/6 张法式 PO（Total HT + TVA = TTC） | "
       "差额落在 0.1%~25% 判为口径不同，只 warn、不给目标值 |")
    A_("| 2 | 单价尾差（小数位被多读一位）时，建议「按 q×p 改金额」方向反了——"
       "该改的是单价 | A 组 3/6 张（如 60×6.426 vs 385.20，真实单价 6.42） | "
       "相对差 <0.1% 时只提示核对单价，不给金额 |")
    A_("| 3 | 收货单/装箱单本来没有金额，被当成「漏抽合计」拦截 | A 组 5/6 张收货单 | "
       "明细全无价格信号 ⇒ 判为无价单据，只 warn |")
    A_("| 4 | 有价单据丢日期却照样放行 | B 组 `cn_po_fax`（传真件日期整行没抽到） | "
       "有价单据缺日期 ⇒ err |")
    A_("| 5 | 「单价」这一格里装的其实是行金额（法语 PO 表头 `Montant` 被读进 `price`、"
       "`amount` 全空），老规则报的是「3 行缺金额」——**方向相反**，照做会把金额再乘一次"
       "数量 | A 组 `purchase_order_7732ff5b_photo`（gold 里该行 line_total 正是"
       " 137,58 / 125,79 / 120,00） | 让两个假设竞争解释合计：按「单价」算 Σ(数量×单价)"
       "是合计的 38681 倍，按「行金额」算 Σ金额 只差 9.5%（差额正是税与运费）⇒ 判定为"
       "装错格子，并把每个值作为「移到 amount」的候选给出 |")
    A_("| 6 | 收货单的**件数合计**被填进金额栏（1650 = 550+550+550），"
       "而且四层校验全过：有编号、有供应商、有日期、合计是正数 | "
       "A 组 `goods_received_note_21664797_scan`（gold：`summary.total_accepted` = \"1650\"） | "
       "合计非空但明细无任何价格 ⇒ 与数量之和比对；相等即判「这是件数的合计，不是钱的合计」，"
       "并说明该清空而不是改数 |")
    A_("| 7 | 无价单据上凭空给出币种：5 张同来源的收货单（同为法语页面、同一批供应商）"
       "分别给出 CNY / CNY / EUR / USD / CNY | A 组 5 张 GRN + `cn_grn` + `en_packing_list` | "
       "无金额 ⇒ 单据上不存在可读的币种标识，该值必然是推断；加 warn 提示不要当已核实字段下传 |")
    A_("")
    A_("> 第 5、6 条的做法值得单独说一句：它们**没有引入单据类型分类**，而是让单据自己的数字"
       "说话。同一批数字在两种解释下必然有一种解释与表尾的合计吻合得多——「谁的误差小」"
       "就是判据。所以「把数量当金额」「把金额当单价」这种槽位错位，不需要知道这是采购单还是"
       "收货单也能判。真正需要分类的是**名称**槽位（见下面第 1 条）。")
    A_("")
    A_("## 仍然存在的问题（如实记录）")
    A_("")
    A_("1. **「收货方」与「供应商」抽反，规则层查不出来。**A 组那张收货单的 gold 是 "
       "`parties.issuer.name = Union Sports SAS`（开单方，也就是收货方自己）、"
       "`parties.supplier.name = Compete, Inc.`，而抽取结果把**收货方填成了 supplier**。"
       "这类错完全自洽——字段非空、没有矛盾、勾稽照样平——上面两条新规则能抓「数字装错格子」，"
       "抓不了「名称装错格子」。要判它，必须先知道「谁是买方」：采购订单的供应商是外方，"
       "收货单的供应商也是外方但开单方是自己。**这需要抽取前先分类、按类型选 schema**，"
       "本轮没做。")
    A_("2. **一套 schema 灌所有单据类型，仍会逼出编造值。**金额栏为空时模型把数量当金额"
       "（规则 6 事后抓住）；币种栏无内容可读时模型编一个（规则 7 事后标记）。"
       "规则只能**事后抓**，不能**事前不产生**——正解仍是抽取前先分类。")
    A_("3. **编号里的 O/0 混淆无法自动检出。**"
       "B 组 4 张中文单的编号 `PO20261025001` 全被 OCR 读成 `P020261025001`（字母 O → 数字 0），"
       "原始 OCR 词就是错的，模型照抄。历史画像机制能提示，但依赖历史库，本地为空时不触发。")
    A_("4. **q×p 满足交换律，数量与单价互换查不出来。**"
       "B 组 `cn_po_tax` 首版把「2.5 吨 × 3200 元」抽成「3200 × 2.5」，"
       "乘出来还是 8000，勾稽通过。这类错误只能靠量级合理性（圆钢 3200 吨不合理）或单位字段发现，"
       "而 schema 没有 `unit`。")
    A_("5. **模型会悄悄改精度。**`cn_po_tax` 的 OCR 明明读出 `2.5` 和 `0.5`（置信度 0.64/0.74），"
       "模型却输出 `qty=2` 和 `qty=1`。这次靠勾稽抓住了（2×3200≠8000），"
       "但如果该行金额也一起被改，就抓不住。")
    A_("6. **同图重跑结果会变。**`temperature=0` 也不保证逐字一致，"
       "详见 `results/repeatability.json`（旧版数据）与 `results/hf_eval_all.json`。")
    A_("")
    A_("## 目录")
    A_("")
    A_("```")
    A_("testset/")
    A_("├── README.md")
    A_("├── manifest.json              机器可读总清单（sha256 / gold / 抽取 / 疑点）")
    A_("├── images/")
    A_("│   ├── a_hf_procurement/      12 张：采购订单 + 收货单（有 gold）")
    A_("│   ├── b_cn_synthetic/        8 张：中文自造")
    A_("│   └── c_offdomain/           9 张：旧网图，反例留证")
    A_("├── results/")
    A_("│   ├── hf_eval_all.json       A 组逐张评测（含 gold 比对结果）")
    A_("│   ├── cn_probe.json          B 组逐张抽取结果")
    A_("│   ├── repeatability.json     同图重跑稳定性（旧版数据）")
    A_("│   └── ocr_text/<name>.txt    每张的 OCR 版面文本（确定性，可用于判断"
       "错在识别还是抽取）")
    A_("├── ground_truth/")
    A_("│   ├── hf_gold.json           A 组字段级 gold（由 words+bbox 重建）")
    A_("│   └── cn_expected.json       B 组人工设定真值与埋点")
    A_("└── sources/")
    A_("    └── hf_dataset.json        数据集说明 / 许可 / 下载地址")
    A_("```")
    A_("")
    A_("## 复现")
    A_("")
    A_("```bash")
    A_("python src/fetch_hf_testset.py            # 从 HF parquet 抽样本 + 导出 gold")
    A_("python src/eval_hf.py                     # A 组：跑链路 + 与 gold 比对")
    A_("python src/gen_cn_po_variants.py          # B 组：生成中文单据（含传真/复印退化）")
    A_("python src/probe_formats.py --dir samples/variants --only cn_po --out out/cn_po_probe.json")
    A_("python src/check_slot_rules.py            # 槽位错位两条规则的回归测试（6/6）")
    A_("python src/export_testset.py              # 导出本目录")
    A_("```")
    A_("")
    A_("> `extract` 是**一次运行的快照**。要判断某张图是识别错还是抽取错，"
       "先看 `results/ocr_text/` 里的文本，再看 `manifest.json` 的 `ground_truth`。")
    A_("")
    A_("> 判定列由 `export_testset.py` 在导出时用**当前的** `validate.py` 重算，"
       "不是照抄跑图那一刻的结论。所以改了规则只要重跑导出，全表跟着更新——"
       "这样「规则版本」和「判定结果」不会各说各话。")

    (DEST / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ocr", action="store_true")
    args = ap.parse_args()

    probes = load_probes()
    # 上一版的分组与报告结论已被推翻，留着会误导（读者会以为是同一批数据）
    for stale in ("images/public", "images/variants"):
        shutil.rmtree(DEST / stale, ignore_errors=True)
    for stale in ("results/format_matrix.md", "results/format_matrix.html",
                  "results/formats_probe.json", "ground_truth/variants_expected.json"):
        (DEST / stale).unlink(missing_ok=True)
    for sub in ("results/ocr_text", "ground_truth", "sources"):
        (DEST / sub).mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    hf_manifest = json.loads((HF_DIR / "manifest.json").read_text(encoding="utf-8")) \
        if (HF_DIR / "manifest.json").exists() else []
    hf_gold: dict[str, dict] = {}

    # ---- A 组
    for h in hf_manifest:
        name = h["name"]
        src = ROOT / h["file"]
        gold = {k: v for k, v in h["gold"].items() if not k.endswith("__variants")}
        hf_gold[name] = gold
        kind = "采购订单" if h["type"] == "purchase_order" else "收货单"
        rec = build("a_hf_procurement", name, kind,
                    "en/fr",
                    f"scan/photo 降质（{h['profile']}）",
                    src, probes.get(name), gold, h["source"],
                    {**h["degradation"], "profile": h.get("profile", "")})
        if rec:
            records.append(rec)

    # ---- B 组
    cn_exp = json.loads((ROOT / "samples" / "variants" / "expected.json")
                        .read_text(encoding="utf-8"))
    for fn, kind, lang, layout in CN:
        name = Path(fn).stem
        src = ROOT / "samples" / "variants" / fn
        rec = build("b_cn_synthetic", name, kind, lang, layout, src,
                    probes.get(name), cn_exp.get(name), {"note": "本仓库生成"}, None)
        if rec:
            records.append(rec)

    # ---- C 组（反例）
    off_paths = {p.name: p for p in (ROOT / "samples" / "web").rglob("*") if p.is_file()}
    meta = json.loads((DEST / "sources" / "wikimedia_meta.json").read_text(encoding="utf-8")) \
        if (DEST / "sources" / "wikimedia_meta.json").exists() else {}
    for fn, kind, lang, layout in OFF:
        src = off_paths.get(fn)
        if not src:
            print(f"  ! 反例缺文件 {fn}")
            continue
        name = Path(fn).stem
        rec = build("c_offdomain", name, kind, lang, layout, src,
                    probes.get(name), None, {"note": "非本业务域，仅作反例"}, None)
        if rec:
            records.append(rec)

    # ---- 结果与真值随包
    for src_name, dst_name in (("hf_eval_all.json", "hf_eval_all.json"),
                               ("repeatability.json", "repeatability.json")):
        s = ROOT / "out" / src_name
        if s.exists():
            shutil.copy2(s, DEST / "results" / dst_name)
    cn_all = []
    for f in ("cn_po_probe.json", "cn_grn_probe.json"):
        p = ROOT / "out" / f
        if p.exists():
            cn_all.extend(json.loads(p.read_text(encoding="utf-8")))
    (DEST / "results" / "cn_probe.json").write_text(
        json.dumps(cn_all, ensure_ascii=False, indent=2), encoding="utf-8")
    (DEST / "ground_truth" / "hf_gold.json").write_text(
        json.dumps(hf_gold, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "samples" / "variants" / "expected.json",
                 DEST / "ground_truth" / "cn_expected.json")
    (DEST / "sources" / "hf_dataset.json").write_text(json.dumps({
        "dataset": "AyoubChLin/Company-document-dataset-v2",
        "subsets_used": ["purchase_order", "goods_received_note"],
        "scan_subset": "scans",
        "license": "Apache-2.0",
        "source_licenses": "Northwind (MIT) / AdventureWorks (MIT) / Chinook (MIT) / Sakila (BSD-2)",
        "languages": ["en", "fr"],
        "url": "https://huggingface.co/datasets/AyoubChLin/Company-document-dataset-v2",
        "mirror": "https://hf-mirror.com/datasets/AyoubChLin/Company-document-dataset-v2",
        "note": "合成文档（由示例数据库生成），退化参数见各记录 degradation 字段",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- OCR 全文
    if not args.no_ocr:
        for r in records:
            tf = DEST / "results" / "ocr_text" / f"{r['name']}.txt"
            if tf.exists() and tf.stat().st_size > 0:
                continue
            try:
                t0 = time.time()
                text, info = ocr_text(DEST / r["file"])
                tf.write_text(text or "（OCR 未识别出任何文字）", encoding="utf-8")
                r["ocr"].update(info)
                print(f"  OCR {r['name']}: {info['words']} 词 {int((time.time() - t0) * 1000)}ms")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! OCR {r['name']} 失败：{type(exc).__name__}: {exc}")
                r["ocr"]["text_file"] = None

    # 清掉不属于本批的 OCR 文本（上一版遗留，留着会让人以为也是本批样本）
    keep = {r["name"] for r in records}
    for tf in (DEST / "results" / "ocr_text").glob("*.txt"):
        if tf.stem not in keep:
            tf.unlink()

    m = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": "deepseek-v4.1-flash",
        "ocr": "RapidOCR(onnxruntime)",
        "pipeline": "解码 → 去红章 → OCR → 版面重建 → LLM 抽取 → 三层校验",
        "n": len(records),
        "groups": {
            g: sum(1 for r in records if r["group"] == g)
            for g in ("a_hf_procurement", "b_cn_synthetic", "c_offdomain")},
        "verdicts": {
            v: sum(1 for r in records if r["verdict"] == v)
            for v in ("通过", "拦截", "无文字", "未跑")},
        "note": "extract 是某次运行快照；ocr_text 是确定性产物，可用于区分识别错误与抽取错误。",
        "records": records,
    }
    (DEST / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    write_readme(m)

    print(f"\n导出完成：{DEST}")
    print(f"  {m['n']} 张  {m['groups']}  判定 {m['verdicts']}")


if __name__ == "__main__":
    main()
