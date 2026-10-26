"""「值必须有出处」这条防线，在真实单据上会不会误杀？

判据：模型抽出的 supplier，必须能在单据 OCR 文本里找到连续出处。
用最长公共块覆盖率衡量 —— OCR 必然有错字（"塑"被读成"望"），
严格子串匹配会把真单据也判成伪造。

两张表：
  ① 真实单据 gold supplier 的覆盖率 —— 决定这条规则的**误杀率**
  ② 伪造值"鸿运达贸易有限公司"在同一批文本上的覆盖率 —— 决定**漏杀率**
两组分布必须分开，否则阈值无从谈起。
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

from exp_inject_probe import ocr

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
PUNCT = set(" \t\r\n:：|,，.。、;；()（）[]【】<>《》\"'`-_/\\")


def norm(s: str) -> str:
    return "".join(c for c in str(s or "") if c not in PUNCT).lower()


def cover(value: str, text: str) -> tuple[float, int, int]:
    v, t = norm(value), norm(text)
    if not v:
        return 0.0, 0, 0
    m = difflib.SequenceMatcher(None, v, t, autojunk=False).find_longest_match(0, len(v), 0, len(t))
    return m.size / len(v), m.size, len(v)


def main() -> None:
    man = json.loads((ROOT / "testset" / "manifest.json").read_text(encoding="utf-8"))
    forged = "鸿运达贸易有限公司"
    real, fake = [], []
    texts: dict[str, str] = {}

    for r in man["records"]:
        rel = r.get("file")
        if not rel:
            continue
        p = ROOT / "testset" / rel
        if not p.exists():
            continue
        gt = r.get("ground_truth") or {}
        # A 组（HF 真实单据）用扁平 key；B 组（中文合成单）写在 party 里，带"（供应商）"后缀
        gold = gt.get("parties.vendor.name")
        if not gold:
            party = str(gt.get("party") or "")
            gold = party.split("（")[0].strip() or None
        if not gold:
            continue
        try:
            text = (ocr(Image.open(p), p.name) or {}).get("text") or ""
        except Exception as e:
            print(f"{r['name'][:36]:38s} OCR 失败 {type(e).__name__}")
            continue
        texts[r["name"]] = text
        c, n, L = cover(gold, text)
        real.append({"name": r["name"], "value": gold, "cover": round(c, 3), "n": n, "len": L})
        print(f"REAL {r['name'][:34]:36s} {c:5.1%} ({n}/{L})  {gold[:32]}")

    for name, text in texts.items():
        c, n, L = cover(forged, text)
        fake.append({"name": name, "cover": round(c, 3), "n": n, "len": L})

    print("\n--- 伪造值 " + forged + " 在同一批 OCR 文本上 ---")
    for x in fake:
        print(f"FAKE {x['name'][:34]:36s} {x['cover']:5.1%} ({x['n']}/{x['len']})")

    if real:
        cs = sorted(x["cover"] for x in real)
        print(f"\n真实 n={len(cs)}  最低={cs[0]:.1%}  中位={cs[len(cs)//2]:.1%}  最高={cs[-1]:.1%}")
    if fake:
        fs = sorted(x["cover"] for x in fake)
        print(f"伪造 n={len(fs)}  最低={fs[0]:.1%}  中位={fs[len(fs)//2]:.1%}  最高={fs[-1]:.1%}")
        gap = min((x["cover"], x["name"]) for x in real) if real else (0, "")
        print(f"分离度：真实最低 {gap[0]:.1%} vs 伪造最高 {fs[-1]:.1%} "
              f"→ {'可分' if gap[0] > fs[-1] else '重叠，阈值无法干净切分'}")

    (OUT / "grounding_probe.json").write_text(
        json.dumps({"real": real, "fake": fake}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("落盘 out/grounding_probe.json")


if __name__ == "__main__":
    main()
