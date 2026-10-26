"""注入检测的误报率测试：对 20 张正常评测集全量扫描。

为什么必须做这一步：
  注入检测最怕的不是漏报，是误报。漏报一次是"没发现攻击"，
  误报一次是"把正常单据当攻击拦下来"——演示时后者当场就废了。
  而且这 20 张里 d2/d3 带印章、有手写批注、有 OCR 噪声，是最容易触发误报的样本。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from guard import detect  # noqa: E402
from layout import rebuild  # noqa: E402
from ocr import run_ocr  # noqa: E402

SET = Path(__file__).parent.parent / "eval" / "set"


def main() -> None:
    gt = json.loads((SET / "gt.json").read_text(encoding="utf-8"))
    files = sorted(gt.keys())
    fp = 0
    rows = []
    for i, fn in enumerate(files, 1):
        items = run_ocr(SET / fn)
        text = rebuild(items).text
        g = detect(text)
        rows.append((fn, g["hit"], g["risk"], [h["category"] for h in g["hits"]]))
        if g["hit"]:
            fp += 1
        print(f"[{i:2d}/{len(files)}] {fn:32s} hit={str(g['hit']):5s} risk={g['risk']:4s} "
              f"{[h['category'] for h in g['hits']] if g['hit'] else ''}")

    print("\n" + "=" * 60)
    print(f"正常单据 {len(files)} 张，误报 {fp} 张（{fp / len(files) * 100:.1f}%）")
    if fp:
        print("!! 以下单据被误判，需要收紧规则：")
        for fn, hit, risk, cats in rows:
            if hit:
                print(f"   {fn}  {risk}  {cats}")


if __name__ == "__main__":
    main()
