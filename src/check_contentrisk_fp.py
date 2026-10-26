"""误报检查：在正常单据（eval 评测集 + samples）上跑内容风险检测。

判据：这些图都是程序生成的**合法单据**，没有影子字段、没有格式错乱的金额。
如果检测器在上面报警，那就是误报——误报率高的告警等于没有告警，
会让审核员养成"点掉不看"的习惯，这比不做检测更糟。

用法：
    python src/check_contentrisk_fp.py            # 跑 eval/set + samples
    python src/check_contentrisk_fp.py --verbose  # 打印每条命中
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image

from contentrisk import detect
from layout import rebuild
from ocr import run_ocr_array
from preprocess import suppress_red

ROOT = Path(__file__).parent.parent


def images() -> list[Path]:
    out: list[Path] = []
    for d in (ROOT / "eval" / "set", ROOT / "samples"):
        if d.exists():
            out.extend(sorted(d.glob("*.png")))
    return out


def main() -> None:
    verbose = "--verbose" in sys.argv
    files = images()
    if not files:
        print("没有找到图片")
        return

    total_hits = 0
    dirty: list[tuple[str, int]] = []

    for p in files:
        img = Image.open(p).convert("RGB")
        bgr = np.asarray(img)[:, :, ::-1].copy()
        clean, _ = suppress_red(bgr)
        items = run_ocr_array(clean)
        lay = rebuild(items)
        r = detect(lay.text)
        n = r["total"]
        total_hits += n
        if n:
            dirty.append((p.name, n))
        if verbose or n:
            print(f"{p.name:34s} high={r['high']} mid={r['mid']}  {lay.text[:60]!r}")
            for h in r["numeric"] + r["shadow"] + r["currency"]:
                print(f"    [{h['level']}/{h['kind']}] {h['token']}  <- {h['context'][:80]}")

    print()
    print(f"图片 {len(files)} 张，命中 {len(dirty)} 张，共 {total_hits} 条告警")
    if dirty:
        print("命中清单：")
        for name, n in dirty:
            print(f"  {name}  {n} 条")
    else:
        print("零误报")


if __name__ == "__main__":
    main()
