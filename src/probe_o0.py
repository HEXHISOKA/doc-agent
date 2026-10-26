"""O/0 不可判根因探针：三个实验钉死「PO 被读成 P0」的原因。

背景（2026-10-26 用户追问）：旧口径 A(OCR) 单据编号 100%、B(直读) 30%，
必须找到原因才能取长补短。本脚本回答三件事：

  实验 1  字形可分性：SimSun 里字母 O 与数字 0 逐像素差多少？
          若 XOR≈0 或降质后≈0 ⇒ 图上不存在区分二者的像素证据，
          任何读者的输出都是先验猜测，不是感知。
  实验 2  OCR 先验对照（控制实验）：把真值分别为 PO… 与 P0… 的两张
          合成图喂给与线上一致的 OCR（部署端 /api/ocr，RapidOCR）。
          若两者 OCR 输出相同 ⇒ OCR 的「正确」是语言模型先验猜对，
          不是读对——A 的 100% 从来不是 OCR 的功劳。
  实验 3  视觉模型对照：同一对合成图直读。若图上真有可分像素时模型
          能读对 PO ⇒ 模型是「看不见」而不是「不会」；若仍读 P0
          ⇒ 是先压倒性偏置。

只做只读探测：合成图不进业务库；/api/ocr 是服务端本地 OCR（零 token）。
用法：
    python probe_o0.py --local            # 只跑实验 1 + 存证图
    python probe_o0.py                    # 全部三个实验（打线上 OCR + 模型）
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "o0_probe"
OUT.mkdir(parents=True, exist_ok=True)

SONG = "C:/Windows/Fonts/simsun.ttc"
W_DOC, H_DOC = 1240, 1754
INK = (25, 25, 25)
DOC_NO = "PO20261024001"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(SONG, size)


# ---------- 实验 1：字形可分性 ----------

def glyph_img(ch: str, size: int) -> Image.Image:
    img = Image.new("L", (size * 3, size * 3), 255)
    ImageDraw.Draw(img).text((size, size), ch, font=font(size), fill=0, anchor="mm")
    return img


def bbox_and_diff(a: Image.Image, b: Image.Image) -> dict:
    ba, bb = a.getbbox(), b.getbbox()
    # 对齐包围盒左上角后逐像素比
    ca = a.crop(ba)
    cb = b.crop(bb)
    w = max(ca.width, cb.width)
    h = max(ca.height, cb.height)
    ca = ca.resize((w, h)) if ca.size != (w, h) else ca
    cb = cb.resize((w, h)) if cb.size != (w, h) else cb
    pa, pb = ca.load(), cb.load()
    diff = same = 0
    diff_px = []
    for y in range(h):
        for x in range(w):
            va, vb = pa[x, y], pb[x, y]
            if abs(va - vb) > 64:          # 明显不同（抗锯齿容差）
                diff += 1
                diff_px.append((x, y))
            else:
                same += 1
    return {
        "bbox_O": ba, "bbox_0": bb, "bbox_same": ba == bb,
        "w_O": ba[2] - ba[0], "h_O": ba[3] - ba[1],
        "w_0": bb[2] - bb[0], "h_0": bb[3] - bb[1],
        "diff_px": diff, "same_px": same,
        "diff_ratio": round(diff / max(1, diff + same), 4),
    }


def degrade(img: Image.Image, blur: float = 0.55, noise: int = 9000, seed: int = 42) -> Image.Image:
    """近似 dirty() 的降质：噪声 + 模糊（不含旋转，避免引入额外形变）。L/RGB 均支持。"""
    import random
    rnd = random.Random(seed)
    px = img.load()
    w, h = img.size
    for _ in range(noise):
        x, y = rnd.randrange(w), rnd.randrange(h)
        v = rnd.randint(130, 215)
        px[x, y] = v if img.mode == "L" else (v, v, v)
    return img.filter(ImageFilter.GaussianBlur(blur))


def exp1() -> None:
    print("== 实验 1：SimSun 字母 O vs 数字 0 的像素可分性 ==")
    for size in (20, 26, 40):
        m = bbox_and_diff(glyph_img("O", size), glyph_img("0", size))
        print(f"  {size}px  bbox相同={m['bbox_same']}  O={m['w_O']}x{m['h_O']} "
              f"0={m['w_0']}x{m['h_0']}  明显差异像素={m['diff_px']} / {m['diff_px'] + m['same_px']}"
              f"  ({m['diff_ratio']:.2%})")
    # 降质后：把整串编号按 dirty 流程处理后，O/0 还能不能分开
    # 降质后：把 O/0 单字按 dirty 流程处理后，还剩多少可分像素
    size = 20
    for label, proc in (("干净", lambda im: im), ("降质(blur0.55+noise)", lambda im: degrade(im))):
        m = bbox_and_diff(proc(glyph_img("O", size)), proc(glyph_img("0", size)))
        print(f"  {size}px[{label}]  明显差异像素={m['diff_px']}  ({m['diff_ratio']:.2%})")

    # 存证图：O 与 0 并排 8x 放大
    combo = Image.new("RGB", (480, 200), "white")
    d = ImageDraw.Draw(combo)
    for i, (ch, label) in enumerate((("O", "字母O"), ("0", "数字0"))):
        g = glyph_img(ch, 20).crop(glyph_img(ch, 20).getbbox()).resize((160, 160), Image.NEAREST)
        combo.paste(g.convert("RGB"), (40 + i * 220, 20))
        d.text((100 + i * 220, 185), f"SimSun 20px {label}", fill="black", anchor="ma")
    combo.save(OUT / "exp1_O_vs_0_20px_8x.png")
    print(f"  存证图 -> {OUT / 'exp1_O_vs_0_20px_8x.png'}")


# ---------- 实验 2/3 的合成图 ----------

def make_strip(text: str, size: int, fname: str, degrade_it: bool = False) -> Path:
    img = Image.new("RGB", (560, 120), (253, 253, 251))
    ImageDraw.Draw(img).text((30, 60), text, font=font(size), fill=INK, anchor="lm")
    if degrade_it:
        img = degrade(img.convert("RGB")).convert("RGB")
    p = OUT / fname
    img.save(p)
    return p


def make_full_doc(doc_no: str, size: int = 20, fname: str | None = None) -> Path:
    """复刻 gen_eval_set.draw_doc 的编号排版（右上角、宋体 size、反锚点）。"""
    img = Image.new("RGB", (W_DOC, H_DOC), (253, 253, 251))
    d = ImageDraw.Draw(img)
    d.text((W_DOC // 2, 90), "XX精密制造有限公司", font=ImageFont.truetype(
        "C:/Windows/Fonts/simhei.ttf", 38), fill=INK, anchor="ma")
    d.text((W_DOC // 2, 146), "采 购 订 单", font=ImageFont.truetype(
        "C:/Windows/Fonts/simhei.ttf", 46), fill=INK, anchor="ma")
    d.text((W_DOC - 90, 218), f"单据编号：{doc_no}", font=font(size), fill=INK, anchor="ra")
    fname = fname or f"doc_{doc_no[:2].lower()}_osize{size}px.png"
    p = OUT / fname
    img.save(p)
    return p


# ---------- 实验 2：OCR 对照（打线上 /api/ocr，服务端本地 RapidOCR） ----------

def ocr_online(p: Path) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from eval_live import Client  # 复用会话/重试逻辑
    cli = Client("https://your-app.example.com", "YOUR_SHARECODE")
    b64 = base64.b64encode(p.read_bytes()).decode()
    d, _, err = cli.call("/api/ocr", {"filename": p.name, "data_b64": b64})
    if err or not d.get("ok"):
        return {"err": err or d.get("error")}
    text = d.get("text") or ""
    n_words = d.get("words")
    hit = [seg for seg in text.splitlines() if ("2026" in seg or "P0" in seg or "PO" in seg)]
    return {"text_hit": hit, "words": n_words, "text_len": len(text)}


# ---------- 实验 3：视觉模型直读对照 ----------

def vlm_read(p: Path) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from cloud import CloudClient
    cloud = CloudClient("https://your-app.example.com",
                        "wbpk_PLACEHOLDER_REPLACE_ME")
    b64 = base64.b64encode(p.read_bytes()).decode()
    text, usage = cloud.chat(
        [{"role": "user", "content": [
            {"type": "text", "text":
             "逐字符读出这张图里「单据编号：」后面的完整编号。只输出编号本身，不要解释。"
             "注意区分字母 O 和数字 0，按你看到的字形输出。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
        model="deepseek-v4.1-flash", temperature=0, timeout=120, json_mode=False)
    return {"read": (text or "").strip()[:80], "usage": usage}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="只跑实验1+存证图")
    args = ap.parse_args()

    exp1()

    if args.local:
        return

    print("\n== 实验 2/3：PO 真值 vs P0 真值 的对照（OCR / VLM 同图对比） ==")
    cases = [
        ("strip_po_20px", make_strip(f"单据编号：{DOC_NO}", 20, "strip_po_20px.png")),
        ("strip_p0_20px", make_strip(f"单据编号：P020261024001", 20, "strip_p0_20px.png")),
        ("strip_po_40px", make_strip(f"单据编号：{DOC_NO}", 40, "strip_po_40px.png")),
        ("strip_p0_40px", make_strip(f"单据编号：P020261024001", 40, "strip_p0_40px.png")),
        ("doc_po_osize20px", make_full_doc(DOC_NO, 20)),
        ("doc_p0_osize20px", make_full_doc("P020261024001", 20)),
        ("doc_po_osize44px", make_full_doc(DOC_NO, 44)),
    ]
    results = {}
    for name, p in cases:
        o = ocr_online(p)
        results[name] = {"ocr": o}
        print(f"  {name:20s} OCR命中: {o.get('text_hit') or o.get('err')}")
    for name, _ in cases:
        v = vlm_read(OUT / f"{name}.png")
        results[name]["vlm"] = v
        print(f"  {name:16s} VLM直读: {v.get('read')}")

    (OUT / "probe_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果 -> {OUT / 'probe_result.json'}")


if __name__ == "__main__":
    main()
