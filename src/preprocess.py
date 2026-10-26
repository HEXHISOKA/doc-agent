"""图像预处理：解码 + 红色印章抑制。

为什么"去红章"必须放在 OCR **之前**，而不是之后去过滤识别错的词：
    公章是红色油墨，正文是黑色印刷，在 RGB 空间里这两者物理可分：
        红章 (206, 28, 38) → R 远大于 G/B
        黑字 ( 25, 25, 25) → R≈G≈B
    把"R 明显占优"的像素替换掉，印章就从图里消失，而黑字一个像素都不动。

    如果放到 OCR 之后处理，就必须面对一个无法判定的事实：
    "限公"、"554"、"1.00" 这些碎片，与真实的一个字/一段数字长得完全一样。
    实测更糟：印章碎片的置信度（0.60~0.75）反而**高于**被印章压住的真实
    单元格（0.51~0.58），任何按 score 阈值过滤的规则都会先误删真数据。
    **能消除污染的地方在图像层，不在文本层。**

填充值为什么用 R 通道而不是纸色（实测结论，非理论推演）：
    抹成纸色      → 明细 9 格命中 6/9
    用 R 通道填充 → 明细 9 格命中 8/9
    原因：油墨是半透明的，被盖住的黑字在 R 通道里仍留下"比纸暗"的残迹
    （黑字叠 80% 红 ≈ R=170），抹成纸色会把这份残迹一起扔掉；
    纯红章的 R 很高（≈206，接近纸色所以看不见），两者差值恰好够 OCR 认回来。

代价（如实记录）：印章是**油墨覆盖**，被完全盖死的笔画像素上已不存在，
本层消除的是"污染"，不是"遮挡"。遮挡导致的数据缺失由下游用
金额÷单价 反推并标记「推导值待复核」，不在图像层假装能还原。

依赖策略：本模块只用 numpy（rapidocr 必然已带），解码优先 cv2、兜底 PIL。
沙箱里有没有 Pillow 不确定，所以不把它作为硬依赖。
"""
from __future__ import annotations

import numpy as np

# 红色判定：R 比 G/B 中较大者至少高这么多（0~255 尺度）
# 25 是经验值：能吃掉抗锯齿边缘与扫描偏色，又不会误伤黑色印刷（差值≈0）
RED_DOMINANCE = 25
# R 的绝对下限，避免把暗灰色噪点当成红章
RED_MIN_R = 70
# 红色像素占比低于此值就认为没有印章，整层跳过（不动图，零风险）
MIN_RATIO = 0.0004


def decode_image(blob: bytes) -> np.ndarray:
    """把上传的字节解码成 BGR uint8 数组。

    容错顺序（按"能救回来"的优先级，不是按库的便利性）：
      1. cv2 直接解（绝大多数情况，最快）；
      2. cv2 解不出来时，用 PIL 允许"截断图"再解一次。
         真实场景里常见的是**尾部几十字节缺失**（微信转发中断、下载未完成、
         拍照时存储写满），图像主体完好，只是文件不完整。这类图 cv2 会直接
         判死，PIL 打开 LOAD_TRUNCATED_IMAGES 却能读出来——对用户来说，
         "能识别但提示图片可能不完整"远好于"上传失败，请重试"（重试也是同一张图）。

    为什么要显式切换这个全局开关而不是永久设 True：它只在本次解码期间生效，
    避免影响同进程内其它 PIL 调用对"损坏文件"的判定。
    """
    try:
        import cv2

        arr = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        if arr is not None:
            return arr
    except Exception:  # noqa: BLE001
        pass
    try:
        import io

        from PIL import Image, ImageFile

        prev = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            im = im.convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = prev
        return np.asarray(im)[:, :, ::-1].copy()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法解码图片: {exc}") from exc


def _dilate(mask: np.ndarray, k: int = 5) -> np.ndarray:
    """形态学膨胀。优先 cv2，没有则用 numpy 移位实现（结果一致）。"""
    try:
        import cv2

        kernel = np.ones((k, k), np.uint8)
        return cv2.dilate(mask.astype(np.uint8), kernel, iterations=1) > 0
    except Exception:  # noqa: BLE001
        r = k // 2
        out = mask.copy()
        h, w = mask.shape
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dy == 0 and dx == 0:
                    continue
                shifted = np.zeros_like(mask)
                ys0, ys1 = max(0, dy), min(h, h + dy)
                xs0, xs1 = max(0, dx), min(w, w + dx)
                shifted[ys0:ys1, xs0:xs1] = mask[ys0 - dy : ys1 - dy, xs0 - dx : xs1 - dx]
                out |= shifted
        return out


def suppress_red(bgr: np.ndarray) -> tuple[np.ndarray, dict]:
    """把红色印章像素替换为 R 通道值，其余像素转为灰度。

    返回 (BGR 三通道图像, 元信息)。未检出红色时**返回原图**，
    因此灰度单据、纯黑扫描件这条路径上不产生任何副作用。
    """
    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)

    mask = (r - np.maximum(g, b) > RED_DOMINANCE) & (r > RED_MIN_R)
    ratio = float(mask.mean())
    meta = {"applied": False, "red_ratio": round(ratio, 5), "red_pixels": int(mask.sum())}
    if ratio < MIN_RATIO:
        return bgr, meta

    # 膨胀 5px：抗锯齿边缘像素"红度"不足，不膨胀会留一圈粉色轮廓，
    # 这些残留恰好会被 OCR 当成笔画吃掉，等于没处理。
    mask_d = _dilate(mask, 5)

    gray = (0.114 * b + 0.587 * g + 0.299 * r).round().astype(np.int16)
    paper = int(np.percentile(gray[~mask_d], 90)) if (~mask_d).any() else 250
    paper = max(paper, 200)  # 兜底，避免整图偏暗时填成灰块

    out = gray.copy()
    out[mask_d] = np.minimum(r[mask_d], paper)
    out = out.clip(0, 255).astype(np.uint8)

    meta.update(
        {
            "applied": True,
            "paper_value": paper,
            "mask_ratio": round(float(mask_d.mean()), 5),
            "fill": "r_channel",
        }
    )
    return np.dstack([out, out, out]), meta
