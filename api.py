"""
api.py  ——  会话与轨迹校验层

持有当前登录用户的轨迹校验数据，提供轨迹几何校验与保存。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import services
from services import AppError
from track_gen import choose_json_file_by_ra_name

ROOT = Path(__file__).resolve().parent

# 当前登录用户的轨迹校验数据
CURRENT: dict[str, Any] | None = None


async def startup() -> None:
    """应用启动：加载学校缓存。"""
    await services.startup()


async def shutdown() -> None:
    """应用退出：释放业务层资源。"""
    await services.shutdown()


# ─────────────────────────── 请求模型 ───────────────────────────
class LoginRequest(BaseModel):
    user: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    school_code: str = Field(min_length=1, max_length=64)
    uuid: str | None = Field(default=None, max_length=128)
    device_name: str | None = Field(default=None, max_length=256)


class TrackPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lng: float
    lat: float


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_area_index: int = Field(ge=0)
    points: list[TrackPoint] = Field(min_length=2)
    route_type: Literal["loop", "out_and_back"] = "loop"


class UploadRequest(ValidateRequest):
    name: str = Field(min_length=1, max_length=80)


def current_session() -> dict[str, Any]:
    if not CURRENT:
        raise AppError("尚未登录，请先登录")
    return CURRENT


def distance_m(a: TrackPoint, b: TrackPoint) -> float:
    lat = math.radians((a.lat + b.lat) / 2)
    dlat = math.radians(b.lat - a.lat)
    dlng = math.radians(b.lng - a.lng)
    x = dlng * math.cos(lat)
    y = dlat
    return 6_378_137 * math.sqrt(x * x + y * y)


def track_distance(points: list[TrackPoint]) -> float:
    return sum(distance_m(a, b) for a, b in zip(points, points[1:]))


def normalize_points(points: list[TrackPoint], route_type: str = "loop") -> list[TrackPoint]:
    clean: list[TrackPoint] = []
    for point in points:
        if not (-180 <= point.lng <= 180 and -90 <= point.lat <= 90):
            raise AppError("存在无效经纬度")
        if not clean or distance_m(clean[-1], point) >= 0.5:
            clean.append(point)
    if route_type == "loop":
        if len(clean) >= 2 and distance_m(clean[0], clean[-1]) > 5:
            raise AppError("轨迹未闭合：首尾距离必须不超过 5 米")
        if len(clean) >= 2 and distance_m(clean[0], clean[-1]) > 0.01:
            clean.append(clean[0])
    return clean


def selected_area(session: dict[str, Any], index: int) -> dict[str, Any]:
    areas = session["run_areas"]
    if index >= len(areas):
        raise AppError("跑区不存在")
    return areas[index]


def validate_track(session: dict[str, Any], index: int, points: list[TrackPoint], route_type: str = "loop") -> dict[str, Any]:
    clean = normalize_points(points, route_type)
    if route_type == "loop":
        if len(clean) < 4:
            raise AppError("至少需要 3 个不同轨迹点，并闭合成一圈")
    else:
        if len(clean) < 2:
            raise AppError("折返路线至少需要 2 个轨迹点")
    area = selected_area(session, index)
    meters = track_distance(clean)
    return {"points": [point.model_dump() for point in clean], "distance_m": round(meters, 2), "area": area}



async def status() -> dict[str, Any]:
    """返回本机引擎状态：北京时间。"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    return {"server_time": now.strftime("%Y-%m-%d %H:%M:%S")}


async def schools() -> dict[str, Any]:
    return await services.api_list_schools()


async def login(request: LoginRequest) -> dict[str, Any]:
    global CURRENT
    school_code = request.school_code.strip()
    data = await services.api_login(services.LoginRequest(
        user=request.user,
        password=request.password,
        school_code=school_code,
        uuid=request.uuid,
        device_name=request.device_name,
    ))
    areas = data.get("run_areas") or []
    CURRENT = {"school_code": school_code, "run_areas": areas}
    return {**data, "run_areas": areas}


async def validate(request: ValidateRequest) -> dict[str, Any]:
    session = current_session()
    return validate_track(session, request.run_area_index, request.points, request.route_type)


async def upload(request: UploadRequest) -> dict[str, Any]:
    session = current_session()
    checked = validate_track(session, request.run_area_index, request.points, request.route_type)
    data = await services.api_track_upload(services.TrackUploadRequest(
        school_code=session["school_code"],
        ra_name=request.name,
        track=[[point["lng"], point["lat"]] for point in checked["points"]],
    ))
    return {"name": request.name, "distance_m": checked["distance_m"], "message": data}


async def track_query(school_code: str, ra_name: str) -> dict[str, Any]:
    """查询某学校某跑区是否已录入轨迹文件，并返回轨迹点供界面载入。"""
    for name, val in (("school_code", school_code), ("ra_name", ra_name)):
        if not val or val in (".", "..") or any(c in val for c in ("/", "\\")):
            raise AppError(f"{name} 非法")
    tracks_dir = ROOT / "data" / "tracks" / school_code
    if not tracks_dir.is_dir():
        return {"found": False, "message": f"学校 {school_code} 暂无任何轨迹，请去录入"}
    try:
        path = choose_json_file_by_ra_name(ra_name, str(tracks_dir))
        try:
            points = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            points = []
        return {
            "found": True,
            "path": str(path),
            "file": Path(path).stem,
            "points": points,
            "message": "该跑区已有轨迹",
        }
    except FileNotFoundError:
        return {"found": False, "message": "该跑区暂无轨迹，请去录入"}
    except ValueError as e:
        return {"found": False, "message": str(e)}
