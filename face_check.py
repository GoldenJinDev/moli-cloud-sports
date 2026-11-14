# -*- coding: utf-8 -*-
"""
    录入：拍照 → Exif旋转+前置镜像 → JPEG(100%) → Luban(100KB) → MyUCrop 3:4 裁剪 → Base64
    比对：拍照 → Exif旋转+前置镜像 → FaceImageCompressor(720px + 150KB) → Base64
所以一张合格的池内照片应当是：JPEG、能解析出尺寸、竖版 3:4、大小落在 50~150KB。
入参是 bytes：界面上传的 base64 解码后先校验，合格才写盘。

置信度与姿态（yaw/pitch/roll）那一层需要额外的推理库与模型文件，本项目没有引入；
那一层挡的是"脸糊/侧脸/图里没人脸"，本模块挡的是"根本不是规范照片"。
"""
import struct

MIN_BYTES = 50 * 1024
SOFT_MAX_BYTES = 150 * 1024
ASPECT_TARGET = 3 / 4
ASPECT_TOLERANCE = 0.05

SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def check_jpeg(data: bytes) -> tuple[bool, str, tuple[int, int] | None]:
    """检测 JPEG 格式并解析尺寸。返回 (是否JPEG, 说明, (宽,高) 或 None)。"""
    if data[:3] != b"\xff\xd8\xff":
        return False, f"不是 JPEG(文件头 {data[:3].hex()})", None

    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in SOF_MARKERS:
            height = struct.unpack(">H", data[i + 5:i + 7])[0]
            width = struct.unpack(">H", data[i + 7:i + 9])[0]
            return True, "JPEG", (width, height)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len

    return True, "JPEG(未解析到尺寸)", None


def check_aspect_ratio(w: int, h: int) -> tuple[bool, str]:
    """比例是否接近 3:4 竖版（对应客户端的 MyUCrop）。返回 (是否合格, 说明)。"""
    if w <= 0 or h <= 0:
        return False, "尺寸异常"
    ratio = w / h
    if abs(ratio - ASPECT_TARGET) < ASPECT_TOLERANCE:
        return True, f"3:4 竖版(宽高比 {ratio:.2f})"
    if abs(ratio - 1.0) < 0.02:
        return False, f"正方形(宽高比 {ratio:.2f})，应为 3:4 竖版"
    if ratio < 1:
        return False, f"竖版但比例不是 3:4(宽高比 {ratio:.2f})"
    return False, f"横版(宽高比 {ratio:.2f})，应为 3:4 竖版"


def fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f}KB"


def validate(data: bytes) -> dict:
    """校验一张待入库照片。返回 {ok, errors, warning, detail}。"""
    detail = {"bytes": len(data)}

    is_jpeg, jpeg_info, size = check_jpeg(data)
    detail["format"] = jpeg_info
    if not is_jpeg:
        return {"ok": False, "errors": [f"格式错误：{jpeg_info}（需 JPEG）"],
                "warning": "", "detail": detail}

    if not size:
        return {"ok": False, "errors": ["无法从照片里解析出尺寸，请重新拍摄或选择"],
                "warning": "", "detail": detail}
    w, h = size
    detail["width"], detail["height"] = w, h

    ratio_ok, ratio_msg = check_aspect_ratio(w, h)
    detail["ratio"] = ratio_msg
    errors = [] if ratio_ok else [f"照片比例不合格：{ratio_msg}"]

    warning = ""
    if len(data) < MIN_BYTES:
        errors.append(f"照片仅 {fmt_kb(len(data))}，低于 50KB，细节不够容易被判不过")
    elif len(data) > SOFT_MAX_BYTES:
        warning = f"照片 {fmt_kb(len(data))} 超过建议的 150KB，仍可保存"

    return {"ok": not errors, "errors": errors, "warning": warning, "detail": detail}
