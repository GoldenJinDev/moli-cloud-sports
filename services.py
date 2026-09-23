"""
services.py
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import string
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict


_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务错误。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


_tasks: dict[str, dict] = {}
_running_users: set[str] = set()  # 仅防同一账号重复提交，单事件循环内无需锁



_school_cache: dict[str, dict] = {}  
_cache_loaded: bool = False
_cache_lock = asyncio.Lock()

# 学校列表服务不可用时的兜底，保证核心学校仍可登录
_FALLBACK_SCHOOLS: dict[str, dict] = {
    "106": {"schoolName": "滁州学院", "schoolUrl": "https://tiyu.chzu.edu.cn:8010/m-api"},
}


async def refresh_school_cache():
    """刷新学校内存缓存，启动时和定时任务调用。"""
    global _school_cache, _cache_loaded
    temp_uuid = gen_device_id()
    device_name = have_device_name()
    schools = await list_schools(uuid=temp_uuid, device_name=device_name)
    async with _cache_lock:
        online = {
            s["schoolCode"]: {
                "schoolName": s.get("schoolName", ""),
                "schoolUrl":  s.get("schoolUrl", ""),
            }
            for s in schools if s.get("schoolUrl")
        }
        # 在线列表拿不到时用内置学校，保证核心学校仍可登录
        _school_cache = {**_FALLBACK_SCHOOLS, **online}
        _cache_loaded = True
    logger.info("[cache] 学校缓存刷新完成，共 %d 所（在线 %d 所）", len(_school_cache), len(online))


def get_school_info(school_code: str) -> dict | None:
    """从内存缓存获取学校信息。"""
    return _school_cache.get(school_code)


def get_all_school_names() -> list[dict]:
    """返回所有学校名称列表"""
    return [
        {"schoolName": v["schoolName"], "schoolCode": code}
        for code, v in _school_cache.items()
    ]


from upstream import (
    have_device_name,
    login,
    get_student_info,
    get_home_run_info,
    yun_run,
    have_yd_info,
    get_sport_detail,
    close_http_client,
    list_schools,
    creat_request_async,
    list_xn_year_xq_by_student_id,
    _endpoint,
)


async def startup():
    # 学校列表只保存在内存；但轨迹仍从 data/tracks 目录读取
    await refresh_school_cache()


async def shutdown():
    await close_http_client()
    logger.info("内存状态已释放")
 


import json as _json  # 防止顶部同名冲突，此处显式引入

def gen_device_id() -> str:
    """生成一个随机设备 ID（16 位十六进制），并返回其 SHA256 哈希值。"""
    while True:
        android_id = "".join(random.choices("0123456789abcdef", k=16))
        if android_id != "9774d56d682e549c":
            return hashlib.sha256(android_id.encode("utf-8")).hexdigest()


def gen_user_id() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=6))


def is_valid_token(value: str) -> bool:
    pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(pattern, str(value), re.IGNORECASE))


def _login_token_or_raise(raw) -> str:
    """提取 token；失败时透传服务端真实 msg 并抛 401。"""
    token = raw.get("token") if isinstance(raw, dict) else str(raw)
    if not is_valid_token(token):
        detail = raw.get("msg") if isinstance(raw, dict) else "登录失败"
        raise AppError(detail or "用户名或密码错误")
    return token


async def _mark_task(task_id: str, status: str, result: str):
    if task_id in _tasks:
        _tasks[task_id].update(status=status, result=result)



class LoginRequest(BaseModel):
    user: str
    password: str
    school_code: str = "106"
    uuid: Optional[str] = None
    device_name: Optional[str] = None


class YunRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str
    school_code: str
    uuid: str
    device_name: str
    token: str
    target_distance: int
    pace_range_min_per_km: Optional[Tuple[float, float]] = None
    cadence_range_spm: Optional[Tuple[float, float]] = None
    fast: Optional[bool] = None
    ra_name: str
    face_base64: Optional[str] = None


class HaveYdInfoRequest(BaseModel):
    school_code: str
    uuid: str
    device_name: str
    token: str


class TrackUploadRequest(BaseModel):
    school_code: str
    ra_name: str
    track: list


class RunRecordDetailRequest(BaseModel):
    school_code: str
    uuid: str
    device_name: str
    token: str
    record_id: str
    table_name: Optional[str] = None  # 这个不需要传，保持这样



async def api_login(req: LoginRequest):
    """登录 获取 token、学生信息以及跑区列表。"""
    start = datetime.now()
    logger.info("[/login] user=%s school_code=%s", req.user, req.school_code)

    school_info = get_school_info(req.school_code)
    if not school_info and not _cache_loaded:
        await refresh_school_cache()
        school_info = get_school_info(req.school_code)
    if not school_info:
        raise AppError(f"学校编码 {req.school_code} 不存在")
    school_url = school_info["schoolUrl"]

    device_uuid = (req.uuid or "").strip() or gen_device_id()
    device_name = (req.device_name or "").strip() or have_device_name()
    raw = await login(
        req.user, req.password, device_uuid, device_name,
        school_url=school_url, school_code=req.school_code,
    )
    token = _login_token_or_raise(raw)
    info = await get_student_info(
        school_url=school_url, uuid=device_uuid, device_name=device_name, token=token,
    )
    result = {
        **info,
        "user": req.user,
        "school_code": req.school_code,
        "uuid": device_uuid,
        "device_name": device_name,
        "token": token,
    }
    uuid_used, device_used = device_uuid, device_name
    try:
        run_areas = await get_home_run_info(
            school_url=school_url, uuid=uuid_used, device_name=device_used, token=token,
        ) or []
    except Exception as e:
        logger.warning("[/login] 获取跑区失败 user=%s err=%s", req.user, e)
        run_areas = []

    result["run_areas"] = run_areas

    logger.info("[/login] 完成 耗时=%.2fs user=%s", (datetime.now() - start).total_seconds(), req.user)
    return result


async def api_list_schools():
    """返回所有可用学校名称列表。"""
    if not _cache_loaded:
        await refresh_school_cache()
    return {"schools": get_all_school_names()}


async def resolve_school_url(school_code: str) -> str:
    """根据学校编码在内存缓存中解析学校接口地址。"""
    school_info = get_school_info(school_code)
    if not school_info and not _cache_loaded:
        await refresh_school_cache()
        school_info = get_school_info(school_code)
    if not school_info:
        raise AppError(f"学校编码 {school_code} 不存在")
    return school_info["schoolUrl"]


async def _do_yun_run(task_id: str, user: str, kwargs: dict):
    """后台任务：执行一次跑步提交，全程 await。"""
    await _mark_task(task_id, "running", _json.dumps(kwargs, ensure_ascii=False))
    try:
        result = await yun_run(**kwargs)
        status, result_str = "done", str(result)
    except AppError as e:
        status, result_str = "failed", e.detail
    except Exception as e:
        status, result_str = "failed", str(e)
        logger.exception("[_do_yun_run] task_id=%s 异常", task_id)
    finally:
        _running_users.discard(user)
        temp_face_path = kwargs.get("face_image_path")
        if temp_face_path:
            import os
            if os.path.exists(temp_face_path):
                os.remove(temp_face_path)

    await _mark_task(task_id, status, result_str)
    logger.info("[/yun_run] task_id=%s status=%s", task_id, status)


async def api_yun_run(req: YunRunRequest):
    start = datetime.now()
    logger.info("[/yun_run] user=%s target=%s fast=%s", req.user, req.target_distance, req.fast)

    if req.target_distance > 4500:
        raise AppError("target_distance 不能超过 4500")

    actual_distance = min(req.target_distance + random.randint(0, 150), 4500)
    school_url = await resolve_school_url(req.school_code)

    kwargs = dict(
        school_url=school_url,
        school_code=req.school_code,
        uuid=req.uuid,
        device_name=req.device_name,
        token=req.token,
        target_distance=actual_distance,
        user=req.user,
    )
    kwargs["preferred_ra_name"] = req.ra_name
    if req.pace_range_min_per_km is not None:
        kwargs["pace_range_min_per_km"] = tuple(req.pace_range_min_per_km)
    if req.cadence_range_spm is not None:
        kwargs["cadence_range_spm"] = tuple(req.cadence_range_spm)
    if req.fast is not None:
        kwargs["fast"] = req.fast
    # 人脸照片路径
    face_image_path = None
    if req.face_base64:
        face_image_path = _resolve_face_file(req.user, None, req.face_base64, None, persist=False)
        kwargs["face_image_path"] = face_image_path

    if req.user in _running_users:
        raise AppError("该账号当前正在跑步，请稍后再试")

    if req.fast:
        _running_users.add(req.user)
        try:
            result = await yun_run(**kwargs)
        finally:
            _running_users.discard(req.user)
            if face_image_path:
                import os
                if os.path.exists(face_image_path):
                    os.remove(face_image_path)
        logger.info("[/yun_run] fast完成 耗时=%.2fs", (datetime.now() - start).total_seconds())
        return {"result": result}
    else:
        task_id = str(uuid.uuid4())
        _tasks[task_id] = {
            "task_id": task_id, "user": req.user, "status": "pending",
            "result": None, "created_at": datetime.now(), "updated_at": datetime.now(),
        }
        _running_users.add(req.user)
        asyncio.create_task(_do_yun_run(task_id, req.user, kwargs))
        logger.info("[/yun_run] 任务已创建 task_id=%s", task_id)
        return {"task_id": task_id, "status": "pending"}

async def api_get_task(task_id: str):
    row = _tasks.get(task_id)
    if not row:
        raise AppError("任务不存在")
    return row


async def api_have_yd_info(req: HaveYdInfoRequest):
    start = datetime.now()
    logger.info("[/have_yd_info] school_code=%s", req.school_code)
    school_url = await resolve_school_url(req.school_code)
    result = await have_yd_info(
        school_url=school_url,
        token=req.token,
        uuid=req.uuid,
        device_name=req.device_name,
    )
    logger.info("[/have_yd_info] 完成 耗时=%.2fs", (datetime.now() - start).total_seconds())
    return result


async def api_sport_info(req: HaveYdInfoRequest):
    start = datetime.now()
    logger.info("[/sport_info] school_code=%s", req.school_code)
    school_url = await resolve_school_url(req.school_code)
    result = await get_sport_detail(
        school_url=school_url,
        token=req.token,
        uuid=req.uuid,
        device_name=req.device_name,
    )
    logger.info("[/sport_info] 完成 耗时=%.2fs", (datetime.now() - start).total_seconds())
    return result


async def api_run_record_detail(req: RunRecordDetailRequest):
    start = datetime.now()
    logger.info("[/run_record_detail] school_code=%s record_id=%s", req.school_code, req.record_id)
    school_url = await resolve_school_url(req.school_code)
    table_name = (req.table_name or "").strip()
    if not table_name:
        xn_list = await list_xn_year_xq_by_student_id(
            school_url=school_url, token=req.token, uuid=req.uuid, device_name=req.device_name,
        )
        if not xn_list:
            raise AppError("无可用学期")
        table_name = xn_list[0]["value"]
    parameter = _json.dumps({"id": req.record_id, "tableName": table_name}, separators=(",", ":"))
    url = _endpoint(school_url, "/run/crsReocordInfo")
    content = await creat_request_async(url, parameter, req.uuid, req.device_name, req.token)
    if not isinstance(content, dict) or content.get("code") != 200:
        msg = content.get("msg") if isinstance(content, dict) else None
        raise AppError(msg or "获取记录详情失败")
    logger.info("[/run_record_detail] 完成 耗时=%.2fs", (datetime.now() - start).total_seconds())
    return content


def _resolve_face_file(user: str, school_id, face_base64, face_image_path, persist: bool) -> str:
    """将 Base64 照片写入临时文件，接口结束后删除。"""
    import base64
    import os
    import tempfile

    if face_base64:
        b64 = face_base64
        if b64.startswith("data:") and "," in b64:   # 兼容 data URI
            b64 = b64.split(",", 1)[1]
        data = base64.b64decode(b64)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        return path
    raise AppError("必须提供 face_base64 照片内容")


async def api_track_upload(req: TrackUploadRequest):
    from pathlib import Path

    # 防路径穿越
    for name, val in (("school_code", req.school_code), ("ra_name", req.ra_name)):
        if not val or val in (".", "..") or any(c in val for c in ("/", "\\")):
            raise AppError(f"{name} 非法,不能含路径分隔符")
    if not isinstance(req.track, list) or len(req.track) < 2:
        raise AppError("轨迹点数量不足(至少 2 个)")
    cleaned = []
    for idx, pt in enumerate(req.track):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise AppError(f"第 {idx} 个点格式错误,应为 [经度, 纬度]")
        try:
            lon, lat = float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            raise AppError(f"第 {idx} 个点坐标无法解析")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise AppError(f"第 {idx} 个点坐标越界")
        cleaned.append([lon, lat])
    tracks_dir = Path(__file__).resolve().parent / "data" / "tracks" / req.school_code
    tracks_dir.mkdir(parents=True, exist_ok=True)
    target = tracks_dir / f"{req.ra_name}.json"
    target.write_text(_json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "[/track/upload] school=%s ra=%s points=%d -> %s",
        req.school_code, req.ra_name, len(cleaned), target,
    )
    return {
        "school_code": req.school_code,
        "ra_name": req.ra_name,
        "path": str(target),
        "points": len(cleaned),
    }
