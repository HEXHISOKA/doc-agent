"""OCR 封装。

RapidOCR 有两个包名，接口不同，这里统一成一种输出：
    [{'box': [[x, y] * 4], 'text': str, 'score': float}, ...]

- rapidocr-onnxruntime（旧）→ engine(img) 返回 (result, elapse)
- rapidocr（新）            → engine(img) 返回对象，含 .boxes / .txts / .scores

为什么用 RapidOCR 而不是 PaddleOCR：模型已转成 ONNX，只依赖 onnxruntime，
不需要 PaddlePaddle 框架，体积从几百 MB 降到几十 MB，云端安装不会卡在编译上。
"""
from __future__ import annotations

from pathlib import Path

_ENGINE = None
_BACKEND = None


def _load_engine():
    global _ENGINE, _BACKEND
    if _ENGINE is not None:
        return _ENGINE

    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        _ENGINE = RapidOCR()
        _BACKEND = "rapidocr_onnxruntime"
        return _ENGINE
    except Exception as first_err:  # noqa: BLE001
        try:
            from rapidocr import RapidOCR  # type: ignore

            _ENGINE = RapidOCR()
            _BACKEND = "rapidocr"
            return _ENGINE
        except Exception as second_err:  # noqa: BLE001
            raise RuntimeError(
                "RapidOCR 不可用。旧包错误: %s；新包错误: %s" % (first_err, second_err)
            ) from second_err


def backend_name() -> str:
    _load_engine()
    return _BACKEND or "unknown"


def to_items(raw, backend: str) -> list[dict]:
    """把两种后端的返回统一成 list[dict]。"""
    items: list[dict] = []
    if raw is None:
        return items

    if backend == "rapidocr_onnxruntime":
        # raw 形如 (result, elapse)，result 可能为 None
        result = raw[0] if isinstance(raw, tuple) else raw
        if not result:
            return items
        for entry in result:
            box, text, score = entry[0], entry[1], entry[2]
            items.append(
                {
                    "box": [[float(p[0]), float(p[1])] for p in box],
                    "text": str(text),
                    "score": float(score),
                }
            )
        return items

    # 新包：属性式返回
    boxes = getattr(raw, "boxes", None)
    txts = getattr(raw, "txts", None)
    scores = getattr(raw, "scores", None)
    if boxes is None or txts is None:
        return items
    for i, text in enumerate(txts):
        box = boxes[i]
        score = float(scores[i]) if scores is not None else 1.0
        items.append(
            {
                "box": [[float(p[0]), float(p[1])] for p in box],
                "text": str(text),
                "score": score,
            }
        )
    return items


def run_ocr(image_path: str | Path) -> list[dict]:
    """对文件路径识别。"""
    engine = _load_engine()
    path = str(image_path)
    if not Path(path).exists():
        raise FileNotFoundError(path)
    raw = engine(path)
    return to_items(raw, backend_name())


def run_ocr_array(bgr) -> list[dict]:
    """对内存中的 BGR ndarray 识别（预处理后不落盘，用户单据不写磁盘）。

    RapidOCR 的 numpy 入参按 **BGR** 解释（内部走 cv2），所以调用方必须传 BGR，
    不能传 PIL 的 RGB——不转换会让红/蓝通道对调，红色油墨与蓝色手写批注的
    识别结果与"文件路径入参"不一致，属于会埋很久才爆的坑。
    """
    engine = _load_engine()
    raw = engine(bgr)
    return to_items(raw, backend_name())


def run_ocr_pil(image) -> list[dict]:
    """对 PIL 图像识别（RGB → BGR 后转交 run_ocr_array）。"""
    import numpy as np

    arr = np.asarray(image.convert("RGB"))[:, :, ::-1]
    return run_ocr_array(arr)
