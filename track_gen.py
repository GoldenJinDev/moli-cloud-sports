import json
import math
import os
import random
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, TypedDict

from geographiclib.geodesic import Geodesic

GPSPoint = Tuple[float, float]
RouteMode = Literal["auto", "loop", "out_and_back"]


class TrackPoint(TypedDict):
    longitude: float
    latitude: float
    run_mileage_m: float
    run_time: int
    run_step: int
    pace: float
    ts: str


class CardPoint(TypedDict):
    isFence: str
    isMock: bool
    point: str
    runMileage: float
    runStatus: str
    runStep: int
    runTime: int
    speed: str
    ts: str


class RunSplitPayload(TypedDict):
    StepNumber: int
    a: int
    b: Optional[Any]
    c: Optional[Any]
    cardPointList: List[CardPoint]
    crsRunRecordId: str
    mileage: float
    orientationNum: int
    runSteps: float
    schoolId: str
    simulateNum: int
    speeds: float
    strides: float
    times: int
    userName: str



# 本部分参数不理解的话慎改
DEFAULT_CHUNK_SIZE: int = 61
DEFAULT_POINT_INTERVAL_S: int = 2
DEFAULT_TRACKS_DIR: str = str(Path(__file__).resolve().parent / "data" / "tracks")
DEFAULT_ROUTE_MODE: RouteMode = "auto"
VALID_ROUTE_MODES = {"auto", "loop", "out_and_back"}
DEFAULT_PACE_RANGE_MIN_PER_KM: Tuple[float, float] = (5.25, 6.83)
DEFAULT_CADENCE_RANGE_SPM: Tuple[float, float] = (164.0, 176.0)
DEFAULT_STRIDE_RANGE: Tuple[float, float] = (0.9, 1.12)
MAX_TRACK_CLOSURE_GAP_M: float = 5.0
MIN_CLOSURE_SEGMENT_M: float = 0.5
LAP_SHIFT_LAT: float = 0.0000065
LAP_SHIFT_LON: float = 0.0000065
WAVE_LAT: float = 0.000011
WAVE_LON: float = 0.000013
LOCAL_JITTER_LAT: float = 0.000003
LOCAL_JITTER_LON: float = 0.000003
WGS84_GEODESIC = Geodesic.WGS84
GPS_SOLVER_TOLERANCE_M: float = 1e-8
GPS_SOLVER_FAST_ITERATIONS: int = 12
GPS_SOLVER_FALLBACK_ITERATIONS: int = 32


def validate_range_tuple(
    name: str,
    value: Tuple[float, float],
) -> None:
    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError(f"{name} 必须为长度为 2 的 tuple")
    low, high = value
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, (int, float))
        or not isinstance(high, (int, float))
    ):
        raise TypeError(f"{name} 的两个值必须为数字")
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError(f"{name} 的值必须为有限数字")
    if low <= 0 or high <= 0:
        raise ValueError(f"{name} 的值必须大于 0")
    if low > high:
        raise ValueError(f"{name} 必须满足下限 <= 上限")


def validate_generate_run_data_params(
    target_distance: int,
    record_id: str,
    user: str,
    ra_name: str,
    school_id: str,
    chunk_size: int,
    point_interval_s: int,
    json_dir: str,
    cadence_range_spm: Tuple[float, float],
    pace_range_min_per_km: Tuple[float, float],
    random_seed: Optional[int],
    start_epoch_s: Optional[int],
    route_mode: RouteMode,
) -> None:
    if isinstance(target_distance, bool) or not isinstance(target_distance, int):
        raise TypeError("target_distance 必须为 int")
    if target_distance <= 0:
        raise ValueError("target_distance 必须大于 0")

    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("record_id 不能为空字符串")

    if not isinstance(user, str) or not user.strip():
        raise ValueError("user 不能为空字符串")

    if not isinstance(ra_name, str) or not ra_name.strip():
        raise ValueError("ra_name 不能为空字符串")

    if not isinstance(school_id, str) or not school_id.strip():
        raise ValueError("school_id 不能为空字符串")

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")

    if isinstance(point_interval_s, bool) or not isinstance(point_interval_s, int):
        raise TypeError("point_interval_s 必须为 int")
    if point_interval_s != DEFAULT_POINT_INTERVAL_S:
        raise ValueError(
            f"软件固定每 {DEFAULT_POINT_INTERVAL_S} 秒取点，"
            f"point_interval_s 必须为 {DEFAULT_POINT_INTERVAL_S}"
        )

    if not isinstance(json_dir, str) or not json_dir.strip():
        raise ValueError("json_dir 不能为空字符串")

    validate_range_tuple("cadence_range_spm", cadence_range_spm)
    validate_range_tuple("pace_range_min_per_km", pace_range_min_per_km)

    if random_seed is not None and (
        isinstance(random_seed, bool) or not isinstance(random_seed, int)
    ):
        raise TypeError("random_seed 必须为 int 或 None")

    if start_epoch_s is not None and (
        isinstance(start_epoch_s, bool) or not isinstance(start_epoch_s, int)
    ):
        raise TypeError("start_epoch_s 必须为 int 或 None")

    if not isinstance(route_mode, str) or route_mode not in VALID_ROUTE_MODES:
        raise ValueError(
            f"route_mode 必须是以下值之一: {sorted(VALID_ROUTE_MODES)}"
        )




def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_pace_min_per_km(distance_m: float, duration_s: float) -> float:
    if distance_m <= 0 or duration_s <= 0:
        return 0.0
    return (duration_s / 60.0) / (distance_m / 1000.0)


def choose_json_file_by_ra_name(ra_name: str, json_dir: str) -> str:
    """
    按跑区名称匹配 tracks/{school_code}/ 下的轨迹文件。
    """
    if not os.path.isdir(json_dir):
        raise FileNotFoundError(f"轨迹目录不存在: {json_dir}，请为该学校配置轨迹文件")
    files = sorted(
        (f for f in os.listdir(json_dir) if f.casefold().endswith(".json")),
        key=str.casefold,
    )
    if not files:
        raise FileNotFoundError(f"轨迹目录为空: {json_dir}")
    normalized_ra_name = ra_name.strip().casefold()
    exact_matches = [
        f for f in files
        if Path(f).stem.casefold() == normalized_ra_name
    ]
    if exact_matches:
        return os.path.join(json_dir, exact_matches[0])
    contained_matches = [
        f for f in files
        if Path(f).stem.casefold() in normalized_ra_name
    ]
    if contained_matches:
        longest_length = max(len(Path(f).stem) for f in contained_matches)
        longest_matches = [
            f for f in contained_matches
            if len(Path(f).stem) == longest_length
        ]
        if len(longest_matches) == 1:
            return os.path.join(json_dir, longest_matches[0])
        raise ValueError(
            f"跑区名称匹配到多个轨迹文件: {ra_name} -> {longest_matches}"
        )
    for fallback in ("通用.json", "default.json"):
        if fallback in files:
            return os.path.join(json_dir, fallback)
    raise FileNotFoundError(
        f"未找到与跑区名称匹配的轨迹文件: {ra_name}，目录: {json_dir}"
    )


def load_track_points_from_json(json_path: str) -> List[GPSPoint]:
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"轨迹文件不存在: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"轨迹文件顶层必须为 list: {json_path}")
    if len(raw) < 2:
        raise ValueError(f"轨迹点数量不足: {json_path}")
    points: List[GPSPoint] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"第 {idx} 个轨迹点格式错误: {item}")
        lon = float(item[0])
        lat = float(item[1])
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError(f"第 {idx} 个点包含非有限坐标: {item}")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"第 {idx} 个点经度超范围: {lon}")
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"第 {idx} 个点纬度超范围: {lat}")
        point = (lon, lat)
        if points and point == points[-1]:
            continue
        points.append(point)

    if len(points) < 2:
        raise ValueError(f"轨迹有效点数量不足: {json_path}")

    return points


def geodesic_distance_m(point1: GPSPoint, point2: GPSPoint) -> float:
    result = WGS84_GEODESIC.Inverse(
        point1[1],
        point1[0],
        point2[1],
        point2[0],
        Geodesic.DISTANCE,
    )
    distance = float(result["s12"])
    if not math.isfinite(distance) or distance < 0:
        raise ValueError(f"无法计算有效测地距离: {point1} -> {point2}")
    return distance


def ensure_closed_loop(
    points: Sequence[GPSPoint],
    max_closure_gap_m: float = MAX_TRACK_CLOSURE_GAP_M,
) -> List[GPSPoint]:
    if len(points) < 3:
        raise ValueError("闭环轨迹至少需要 3 个有效点")

    closed_points = list(points)
    closure_gap_m = geodesic_distance_m(closed_points[-1], closed_points[0])
    if closure_gap_m > max_closure_gap_m:
        raise ValueError(
            f"轨迹未闭合，首尾相距 {closure_gap_m:.2f} 米，"
            f"超过允许值 {max_closure_gap_m:.2f} 米"
        )

    if closure_gap_m > 0:
        closed_points.append(closed_points[0])
    else:
        closed_points[-1] = closed_points[0]

    while (
        len(closed_points) >= 4
        and geodesic_distance_m(closed_points[-2], closed_points[0])
        < MIN_CLOSURE_SEGMENT_M
    ):
        del closed_points[-2]

    return closed_points


def prepare_open_route(points: Sequence[GPSPoint]) -> List[GPSPoint]:
    # 开发路段
    forward_points: List[GPSPoint] = []
    for point in points:
        if not forward_points or point != forward_points[-1]:
            forward_points.append(point)
    if (
        len(forward_points) >= 3
        and forward_points[-1] == forward_points[0]
    ):
        forward_points.pop()
    if len(forward_points) < 2:
        raise ValueError("开放路线至少需要 2 个不同坐标点")
    forward_length_m = sum(
        geodesic_distance_m(forward_points[i - 1], forward_points[i])
        for i in range(1, len(forward_points))
    )
    if forward_length_m <= 0:
        raise ValueError("开放路线长度必须大于 0")

    return forward_points


def prepare_route_points(
    points: Sequence[GPSPoint],
    route_mode: RouteMode = DEFAULT_ROUTE_MODE,
) -> Tuple[List[GPSPoint], RouteMode]:
    if not isinstance(route_mode, str) or route_mode not in VALID_ROUTE_MODES:
        raise ValueError(
            f"route_mode 必须是以下值之一: {sorted(VALID_ROUTE_MODES)}"
        )
    if len(points) < 2:
        raise ValueError("路线至少需要 2 个有效点")

    closure_gap_m = geodesic_distance_m(points[-1], points[0])
    resolved_mode: RouteMode
    if route_mode == "auto":
        resolved_mode = (
            "loop"
            if len(points) >= 3
            and closure_gap_m <= MAX_TRACK_CLOSURE_GAP_M
            else "out_and_back"
        )
    else:
        resolved_mode = route_mode

    if resolved_mode == "loop":
        return ensure_closed_loop(points), resolved_mode

    return prepare_open_route(points), resolved_mode


def build_cumulative_distances(points: Sequence[GPSPoint]) -> List[float]:
    if len(points) < 2:
        raise ValueError("points 数量不足")

    cumulative: List[float] = [0.0]
    total = 0.0
    for i in range(1, len(points)):
        d = geodesic_distance_m(points[i - 1], points[i])
        total += d
        cumulative.append(total)

    if total <= 0:
        raise ValueError("轨迹总长度必须大于 0")

    return cumulative


def interpolate_point_along_polyline(
    points: Sequence[GPSPoint],
    cumulative_distances: Sequence[float],
    target_distance_m: float,
) -> GPSPoint:
    if not points or not cumulative_distances:
        raise ValueError("points 或 cumulative_distances 不能为空")
    if len(points) != len(cumulative_distances):
        raise ValueError("points 与 cumulative_distances 长度不一致")

    total_length = cumulative_distances[-1]
    if total_length <= 0:
        return points[0]

    target = clamp(target_distance_m, 0.0, total_length)

    right_index = bisect_left(cumulative_distances, target)
    if right_index <= 0:
        return points[0]
    if right_index >= len(points):
        return points[-1]

    left_index = right_index - 1
    left_d = cumulative_distances[left_index]
    right_d = cumulative_distances[right_index]
    p1 = points[left_index]
    p2 = points[right_index]
    seg_len = max(right_d - left_d, 1e-9)
    ratio = (target - left_d) / seg_len

    lon = p1[0] + (p2[0] - p1[0]) * ratio
    lat = p1[1] + (p2[1] - p1[1]) * ratio
    return (lon, lat)


def advance_on_closed_loop_by_gps_distance(
    points: Sequence[GPSPoint],
    cumulative_distances: Sequence[float],
    current_loop_distance_m: float,
    target_gps_distance_m: float,
) -> Tuple[float, GPSPoint, float]:
    if (
        not math.isfinite(target_gps_distance_m)
        or target_gps_distance_m <= 0
    ):
        raise ValueError("target_gps_distance_m 必须为有限正数")
    if not math.isfinite(current_loop_distance_m):
        raise ValueError("current_loop_distance_m 必须为有限数值")

    loop_length_m = cumulative_distances[-1]
    if loop_length_m <= 0:
        raise ValueError("闭环轨迹总长度必须大于 0")

    current_loop_distance_m %= loop_length_m
    start_point = interpolate_point_along_polyline(
        points,
        cumulative_distances,
        current_loop_distance_m,
    )

    probe_step_m = max(target_gps_distance_m / 4.0, 0.05)
    low_advance_m = 0.0
    high_advance_m = probe_step_m
    max_advance_m = loop_length_m / 2.0

    while high_advance_m <= max_advance_m:
        candidate_loop_distance_m = (
            current_loop_distance_m + high_advance_m
        ) % loop_length_m
        candidate = interpolate_point_along_polyline(
            points,
            cumulative_distances,
            candidate_loop_distance_m,
        )
        candidate_distance_m = geodesic_distance_m(start_point, candidate)
        if candidate_distance_m >= target_gps_distance_m:
            break
        low_advance_m = high_advance_m
        high_advance_m += probe_step_m
    else:
        raise ValueError(
            f"轨迹尺寸过小，无法生成相距 {target_gps_distance_m:.3f} 米的"
            "连续采样点"
        )

    for _ in range(40):
        middle_advance_m = (low_advance_m + high_advance_m) / 2.0
        middle_loop_distance_m = (
            current_loop_distance_m + middle_advance_m
        ) % loop_length_m
        middle_point = interpolate_point_along_polyline(
            points,
            cumulative_distances,
            middle_loop_distance_m,
        )
        middle_distance_m = geodesic_distance_m(start_point, middle_point)
        if middle_distance_m < target_gps_distance_m:
            low_advance_m = middle_advance_m
        else:
            high_advance_m = middle_advance_m

    next_loop_distance_m = (
        current_loop_distance_m + high_advance_m
    ) % loop_length_m
    next_point = interpolate_point_along_polyline(
        points,
        cumulative_distances,
        next_loop_distance_m,
    )
    actual_gps_distance_m = geodesic_distance_m(start_point, next_point)
    return next_loop_distance_m, next_point, actual_gps_distance_m


def advance_on_continuous_route_by_gps_distance(
    points: Sequence[GPSPoint],
    cumulative_distances: Sequence[float],
    current_route_distance_m: float,
    target_gps_distance_m: float,
) -> Tuple[float, GPSPoint, float]:
    if (
        not math.isfinite(target_gps_distance_m)
        or target_gps_distance_m <= 0
    ):
        raise ValueError("target_gps_distance_m 必须为有限正数")
    if not math.isfinite(current_route_distance_m):
        raise ValueError("current_route_distance_m 必须为有限数值")
    if len(points) != len(cumulative_distances) or len(points) < 2:
        raise ValueError("连续路线点与累计距离数量不一致或不足")

    total_length_m = cumulative_distances[-1]
    if total_length_m <= 0:
        raise ValueError("连续路线总长度必须大于 0")
    if not 0.0 <= current_route_distance_m < total_length_m:
        raise ValueError("current_route_distance_m 超出连续路线范围")

    start_point = interpolate_point_along_polyline(
        points,
        cumulative_distances,
        current_route_distance_m,
    )
    available_advance_m = total_length_m - current_route_distance_m
            # 弧长恒≥直线距离，arc<target 区间无解；平滑轨迹通常再探 1~2 步
    probe_step_m = max(target_gps_distance_m * 0.02, 0.05)
    low_advance_m = 0.0
    low_distance_m = 0.0
    high_advance_m = min(target_gps_distance_m, available_advance_m)
    high_point: GPSPoint
    high_distance_m: float

    while True:
        high_point = interpolate_point_along_polyline(
            points,
            cumulative_distances,
            current_route_distance_m + high_advance_m,
        )
        high_distance_m = geodesic_distance_m(start_point, high_point)
        if high_distance_m >= target_gps_distance_m:
            break
        if math.isclose(
            high_advance_m,
            available_advance_m,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "连续路线剩余长度不足，无法生成目标 GPS 采样距离"
            )
        low_advance_m = high_advance_m
        low_distance_m = high_distance_m
        high_advance_m = min(
            high_advance_m + probe_step_m,
            available_advance_m,
        )

    if abs(high_distance_m - target_gps_distance_m) <= GPS_SOLVER_TOLERANCE_M:
        return (
            current_route_distance_m + high_advance_m,
            high_point,
            high_distance_m,
        )

    for _ in range(GPS_SOLVER_FAST_ITERATIONS):
        distance_span_m = high_distance_m - low_distance_m
        if distance_span_m <= 0:
            fraction = 0.5
        else:
            fraction = (
                (target_gps_distance_m - low_distance_m)
                / distance_span_m
            )
        fraction = clamp(fraction, 0.1, 0.9)
        middle_advance_m = low_advance_m + (
            high_advance_m - low_advance_m
        ) * fraction
        middle_point = interpolate_point_along_polyline(
            points,
            cumulative_distances,
            current_route_distance_m + middle_advance_m,
        )
        middle_distance_m = geodesic_distance_m(start_point, middle_point)
        if (
            abs(middle_distance_m - target_gps_distance_m)
            <= GPS_SOLVER_TOLERANCE_M
        ):
            return (
                current_route_distance_m + middle_advance_m,
                middle_point,
                middle_distance_m,
            )
        if middle_distance_m < target_gps_distance_m:
            low_advance_m = middle_advance_m
            low_distance_m = middle_distance_m
        else:
            high_advance_m = middle_advance_m
            high_point = middle_point
            high_distance_m = middle_distance_m

    for _ in range(GPS_SOLVER_FALLBACK_ITERATIONS):
        middle_advance_m = (low_advance_m + high_advance_m) / 2.0
        middle_point = interpolate_point_along_polyline(
            points,
            cumulative_distances,
            current_route_distance_m + middle_advance_m,
        )
        middle_distance_m = geodesic_distance_m(start_point, middle_point)
        if (
            abs(middle_distance_m - target_gps_distance_m)
            <= GPS_SOLVER_TOLERANCE_M
        ):
            return (
                current_route_distance_m + middle_advance_m,
                middle_point,
                middle_distance_m,
            )
        if middle_distance_m < target_gps_distance_m:
            low_advance_m = middle_advance_m
            low_distance_m = middle_distance_m
        else:
            high_advance_m = middle_advance_m
            high_point = middle_point
            high_distance_m = middle_distance_m

    if (
        abs(high_distance_m - target_gps_distance_m)
        > GPS_SOLVER_TOLERANCE_M
    ):
        raise RuntimeError(
            "连续路线 GPS 采样距离求解未达到精度要求: "
            f"{high_distance_m:.9f}/{target_gps_distance_m:.9f} 米"
        )
    return (
        current_route_distance_m + high_advance_m,
        high_point,
        high_distance_m,
    )


def advance_on_out_and_back_route(
    points: Sequence[GPSPoint],
    cumulative_distances: Sequence[float],
    current_phase_m: float,
    travel_distance_m: float,
) -> Tuple[float, GPSPoint, float]:
    if not math.isfinite(travel_distance_m) or travel_distance_m <= 0:
        raise ValueError("travel_distance_m 必须为有限正数")
    if not math.isfinite(current_phase_m):
        raise ValueError("current_phase_m 必须为有限数值")

    route_length_m = cumulative_distances[-1]
    if route_length_m <= 0:
        raise ValueError("开放路线长度必须大于 0")

    cycle_length_m = route_length_m * 2.0
    current_phase_m %= cycle_length_m
    current_route_position_m = (
        current_phase_m
        if current_phase_m <= route_length_m
        else cycle_length_m - current_phase_m
    )
    current_point = interpolate_point_along_polyline(
        points,
        cumulative_distances,
        current_route_position_m,
    )

    next_phase_m = (current_phase_m + travel_distance_m) % cycle_length_m
    next_route_position_m = (
        next_phase_m
        if next_phase_m <= route_length_m
        else cycle_length_m - next_phase_m
    )
    next_point = interpolate_point_along_polyline(
        points,
        cumulative_distances,
        next_route_position_m,
    )
    gps_displacement_m = geodesic_distance_m(current_point, next_point)
    if gps_displacement_m > travel_distance_m + 1e-4:
        raise RuntimeError(
            "开放路线 GPS 位移不能大于实际运动距离: "
            f"{gps_displacement_m:.9f}/{travel_distance_m:.9f} 米"
        )

    return next_phase_m, next_point, gps_displacement_m


def apply_lap_variation(
    base_points: Sequence[GPSPoint],
    lap_no: int,
    rng: Optional[random.Random] = None,
    shared_shift: Optional[GPSPoint] = None,
    base_cumulative_distances: Optional[Sequence[float]] = None,
) -> List[GPSPoint]:
    if len(base_points) < 3:
        raise ValueError("base_points 数量不足")
    if base_points[0] != base_points[-1]:
        raise ValueError("base_points 必须是闭合轨迹")
    if isinstance(lap_no, bool) or not isinstance(lap_no, int) or lap_no < 0:
        raise ValueError("lap_no 必须为非负整数")

    random_source = rng or random.Random()

    if shared_shift is None:
        shift_lon = random_source.uniform(-LAP_SHIFT_LON, LAP_SHIFT_LON)
        shift_lat = random_source.uniform(-LAP_SHIFT_LAT, LAP_SHIFT_LAT)
    else:
        shift_lon, shift_lat = shared_shift
        if not math.isfinite(shift_lon) or not math.isfinite(shift_lat):
            raise ValueError("shared_shift 必须包含有限数值")

    lap_phase = lap_no * math.pi * (3.0 - math.sqrt(5.0))
    phase1 = random_source.uniform(0.0, math.pi * 2.0) + lap_phase
    phase2 = random_source.uniform(0.0, math.pi * 2.0) - lap_phase * 0.63
    phase3 = random_source.uniform(0.0, math.pi * 2.0) + lap_phase * 1.37
    freq1 = random_source.choice((1, 2))
    freq2 = random_source.choice((2, 3))
    freq3 = random_source.choice((4, 5, 6))
    amplitude_scale = random_source.uniform(0.85, 1.15)

    varied: List[GPSPoint] = []
    unique_points = base_points[:-1]
    if base_cumulative_distances is None:
        base_cumulative = build_cumulative_distances(base_points)
    else:
        if len(base_cumulative_distances) != len(base_points):
            raise ValueError("base_cumulative_distances 数量与轨迹点不一致")
        base_cumulative = list(base_cumulative_distances)
        if (
            not math.isclose(base_cumulative[0], 0.0, abs_tol=1e-12)
            or base_cumulative[-1] <= 0
            or any(
                not math.isfinite(value)
                for value in base_cumulative
            )
            or any(
                right <= left
                for left, right in zip(
                    base_cumulative,
                    base_cumulative[1:],
                )
            )
        ):
            raise ValueError("base_cumulative_distances 必须严格递增且有效")
    base_total_length = base_cumulative[-1]

    for i, (lon, lat) in enumerate(unique_points):
        t = base_cumulative[i] / base_total_length
        # sin^4 包络让扰动在圈界处的值、一阶和二阶导数都回到 0。
        endpoint_envelope = math.sin(math.pi * t) ** 4

        wave_lon = (
            math.sin(t * math.pi * 2.0 * freq1 + phase1) * WAVE_LON
            + math.sin(t * math.pi * 2.0 * freq2 + phase2) * (WAVE_LON * 0.35)
        ) * endpoint_envelope * amplitude_scale
        wave_lat = (
            math.cos(t * math.pi * 2.0 * freq1 + phase2) * WAVE_LAT
            + math.cos(t * math.pi * 2.0 * freq2 + phase1) * (WAVE_LAT * 0.35)
        ) * endpoint_envelope * amplitude_scale

        jitter_lon = (
            math.sin(t * math.pi * 2.0 * freq3 + phase3)
            * LOCAL_JITTER_LON
            * endpoint_envelope
            * amplitude_scale
        )
        jitter_lat = (
            math.cos(t * math.pi * 2.0 * freq3 + phase3)
            * LOCAL_JITTER_LAT
            * endpoint_envelope
            * amplitude_scale
        )

        varied_lon = lon + shift_lon + wave_lon + jitter_lon
        varied_lat = lat + shift_lat + wave_lat + jitter_lat
        if not (-180.0 <= varied_lon <= 180.0):
            raise ValueError(f"扰动后经度超范围: {varied_lon}")
        if not (-90.0 <= varied_lat <= 90.0):
            raise ValueError(f"扰动后纬度超范围: {varied_lat}")
        varied.append((varied_lon, varied_lat))

    varied.append(varied[0])

    return varied


def append_closed_lap_to_continuous_route(
    continuous_points: List[GPSPoint],
    continuous_cumulative: List[float],
    lap_points: Sequence[GPSPoint],
) -> None:
    if len(lap_points) < 3 or lap_points[0] != lap_points[-1]:
        raise ValueError("lap_points 必须是有效闭环")

    if not continuous_points:
        continuous_points.append(lap_points[0])
        continuous_cumulative.append(0.0)
    elif continuous_points[-1] != lap_points[0]:
        raise RuntimeError("相邻圈的共享锚点不一致")

    for point in lap_points[1:]:
        segment_length_m = geodesic_distance_m(continuous_points[-1], point)
        if segment_length_m <= 0:
            raise RuntimeError("连续多圈轨迹出现零长度段")
        continuous_points.append(point)
        continuous_cumulative.append(
            continuous_cumulative[-1] + segment_length_m
        )


def apply_open_route_variation(
    base_points: Sequence[GPSPoint],
    rng: Optional[random.Random] = None,
) -> List[GPSPoint]:
    if len(base_points) < 2:
        raise ValueError("开放路线至少需要 2 个点")

    random_source = rng or random.Random()
    shift_lon = random_source.uniform(-LAP_SHIFT_LON, LAP_SHIFT_LON)
    shift_lat = random_source.uniform(-LAP_SHIFT_LAT, LAP_SHIFT_LAT)
    phase1 = random_source.uniform(0.0, math.pi * 2.0)
    phase2 = random_source.uniform(0.0, math.pi * 2.0)

    cumulative = build_cumulative_distances(base_points)
    total_length = cumulative[-1]
    varied: List[GPSPoint] = []

    for i, (lon, lat) in enumerate(base_points):
        t = cumulative[i] / total_length
        endpoint_envelope = math.sin(math.pi * t) ** 2
        wave_lon = (
            math.sin(t * math.pi * 2.0 + phase1) * WAVE_LON
            + math.sin(t * math.pi * 4.0 + phase2) * LOCAL_JITTER_LON
        ) * endpoint_envelope
        wave_lat = (
            math.cos(t * math.pi * 2.0 + phase2) * WAVE_LAT
            + math.cos(t * math.pi * 4.0 + phase1) * LOCAL_JITTER_LAT
        ) * endpoint_envelope

        varied_lon = lon + shift_lon + wave_lon
        varied_lat = lat + shift_lat + wave_lat
        if not (-180.0 <= varied_lon <= 180.0):
            raise ValueError(f"扰动后经度超范围: {varied_lon}")
        if not (-90.0 <= varied_lat <= 90.0):
            raise ValueError(f"扰动后纬度超范围: {varied_lat}")
        varied.append((varied_lon, varied_lat))

    return varied


def pace_range_to_speed_range(
    pace_range_min_per_km: Tuple[float, float],
) -> Tuple[float, float]:

    pace_low, pace_high = pace_range_min_per_km
    min_speed_ms = 1000.0 / (pace_high * 60.0)
    max_speed_ms = 1000.0 / (pace_low * 60.0)
    if (
        not math.isfinite(min_speed_ms)
        or not math.isfinite(max_speed_ms)
        or min_speed_ms <= 0
        or max_speed_ms <= 0
    ):
        raise ValueError("配速区间换算出的速度必须为有限正数")
    return min_speed_ms, max_speed_ms


def init_runner_profile(
    stride_range: Tuple[float, float],
    pace_range_min_per_km: Tuple[float, float],
    rng: Optional[random.Random] = None,
) -> Dict[str, float]:
    random_source = rng or random.Random()
    pace_low, pace_high = pace_range_min_per_km
    pace_mid = (pace_low + pace_high) / 2.0
    base_pace = random_source.uniform(pace_low, pace_mid)
    base_speed_ms = 1000.0 / (base_pace * 60.0)
    stride = random_source.uniform(stride_range[0], stride_range[1])
    mid_dip = random_source.uniform(0.04, 0.07)
    variability = random_source.uniform(0.015, 0.03)
    phase = random_source.uniform(0.0, math.pi * 2.0)

    return {
        "base_speed_ms": base_speed_ms,
        "stride": stride,
        "mid_dip": mid_dip,
        "variability": variability,
        "phase": phase,
    }


def simulate_speed_ms(
    profile: Dict[str, float],
    progress: float,
    point_no: int,
    lap_progress: float,
    speed_range_ms: Tuple[float, float],
    rng: Optional[random.Random] = None,
) -> float:
    random_source = rng or random.Random()
    base_speed = profile["base_speed_ms"]
    mid_dip = profile["mid_dip"]
    variability = profile["variability"]
    phase = profile["phase"]

    min_speed_ms, max_speed_ms = speed_range_ms

    trend = 1.0 - mid_dip * math.sin(progress * math.pi)

    wave1 = math.sin(point_no / 9.0 + phase) * variability
    wave2 = math.sin(point_no / 20.0 + phase * 0.55) * (variability * 0.55)

    lap_wave = math.sin(lap_progress * math.pi * 2.0) * 0.008

    noise = random_source.uniform(-0.008, 0.008)

    speed_ms = base_speed * trend * (1.0 + wave1 + wave2 + lap_wave + noise)
    return clamp(speed_ms, min_speed_ms, max_speed_ms)


def simulate_step_increment(
    speed_ms: float,
    duration_s: int,
    profile: Dict[str, float],
    previous_step_inc: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    random_source = rng or random.Random()
    stride = profile["stride"]
    expected_steps = speed_ms * duration_s / stride  # 步数增量 = 里程/步幅
    noisy_steps = expected_steps + random_source.uniform(-0.1, 0.1)
    if previous_step_inc is not None:
        noisy_steps = previous_step_inc * 0.70 + noisy_steps * 0.30

    return max(0.0, noisy_steps)


def build_fixed_interval_distance_plan(
    target_distance: float,
    point_interval_s: int,
    profile: Dict[str, float],
    speed_range_ms: Tuple[float, float],
    movement_cycle_length: float,
    rng: random.Random,
) -> Tuple[List[int], List[float]]:
    min_speed_ms, max_speed_ms = speed_range_ms
    if (
        not math.isfinite(min_speed_ms)
        or not math.isfinite(max_speed_ms)
        or min_speed_ms <= 0
        or max_speed_ms <= 0
    ):
        raise ValueError("固定采样区间的运动距离必须为有限正数")
    if not math.isfinite(movement_cycle_length) or movement_cycle_length <= 0:
        raise ValueError("movement_cycle_length 必须为有限正数")
    max_iterations = math.ceil(target_distance / (min_speed_ms * 2.0)) + 2

    intervals: List[int] = []
    raw_distances: List[float] = []
    cumulative_distance = 0.0
    movement_progress_distance = 0.0

    for point_no in range(max_iterations):
        if point_no == 0:
            interval_s = 6
        else:
            interval_s = int(rng.choices([2, 3, 4], weights=[3, 5, 1])[0])
        intervals.append(interval_s)

        progress = cumulative_distance / target_distance
        lap_progress = movement_progress_distance / movement_cycle_length
        speed_ms = simulate_speed_ms(
            profile=profile,
            progress=progress,
            point_no=point_no,
            lap_progress=lap_progress,
            speed_range_ms=speed_range_ms,
            rng=rng,
        )
        segment_distance_m = speed_ms * interval_s
        segment_distance_m *= rng.uniform(0.996, 1.004)
        segment_distance_m = clamp(
            segment_distance_m,
            min_speed_ms * interval_s,
            max_speed_ms * interval_s,
        )
        if not math.isfinite(segment_distance_m) or segment_distance_m <= 0:
            raise RuntimeError("距离规划产生了非有限或非正运动段")
        raw_distances.append(segment_distance_m)

        cumulative_distance += segment_distance_m
        movement_progress_distance = (
            movement_progress_distance + segment_distance_m
        ) % movement_cycle_length
        if cumulative_distance >= target_distance - 1e-9:
            break
    else:
        raise RuntimeError(
            f"距离规划未达到目标: {cumulative_distance:.6f}/"
            f"{target_distance} 米"
        )

    excess_distance_m = cumulative_distance - target_distance
    adjustable_capacity = sum(
        max(0.0, distance - min_speed_ms * intervals[i])
        for i, distance in enumerate(raw_distances)
    )

    if excess_distance_m <= adjustable_capacity + 1e-9:
        if adjustable_capacity > 0:
            adjusted_distances = [
                distance
                - excess_distance_m
                * max(0.0, distance - min_speed_ms * intervals[i])
                / adjustable_capacity
                for i, distance in enumerate(raw_distances)
            ]
        else:
            adjusted_distances = list(raw_distances)
    else:
        scale = target_distance / cumulative_distance
        adjusted_distances = [
            distance * scale for distance in raw_distances
        ]

    residual = target_distance - sum(adjusted_distances)
    adjusted_distances[-1] += residual
    if any(
        not math.isfinite(distance) or distance <= 0
        for distance in adjusted_distances
    ):
        raise RuntimeError("距离规划产生了非有限或非正运动段")

    return intervals, adjusted_distances



# 终于可以轨迹生成了
def generate_track_points(
    target_distance: int,
    ra_name: str,
    json_dir: str,
    point_interval_s: int,
    stride_range: Tuple[float, float],
    pace_range_min_per_km: Tuple[float, float],
    random_seed: Optional[int] = None,
    start_epoch_s: Optional[int] = None,
    route_mode: RouteMode = DEFAULT_ROUTE_MODE,
) -> List[TrackPoint]:
    if point_interval_s != DEFAULT_POINT_INTERVAL_S:
        raise ValueError(
            f"软件固定每 {DEFAULT_POINT_INTERVAL_S} 秒取点，"
            f"point_interval_s 必须为 {DEFAULT_POINT_INTERVAL_S}"
        )
    if target_distance <= 0:
        raise ValueError("target_distance 必须大于 0")
    if not isinstance(route_mode, str) or route_mode not in VALID_ROUTE_MODES:
        raise ValueError(
            f"route_mode 必须是以下值之一: {sorted(VALID_ROUTE_MODES)}"
        )

    json_path = choose_json_file_by_ra_name(ra_name, json_dir)
    base_route, resolved_route_mode = prepare_route_points(
        load_track_points_from_json(json_path),
        route_mode=route_mode,
    )
    random_source = random.Random(random_seed)

    speed_range_ms = pace_range_to_speed_range(pace_range_min_per_km)
    profile = init_runner_profile(
        stride_range=stride_range,
        pace_range_min_per_km=pace_range_min_per_km,
        rng=random_source,
    )

    if resolved_route_mode == "loop":
        base_route_cumulative = build_cumulative_distances(base_route)
        variation_seed = (
            None
            if random_seed is None
            else random_seed ^ 0x5DEECE66D
        )
        variation_rng = random.Random(variation_seed)
        shared_loop_shift = (
            variation_rng.uniform(-LAP_SHIFT_LON, LAP_SHIFT_LON),
            variation_rng.uniform(-LAP_SHIFT_LAT, LAP_SHIFT_LAT),
        )
        route_points: List[GPSPoint] = []
        route_cumulative: List[float] = []
        first_lap_points = apply_lap_variation(
            base_route,
            lap_no=0,
            rng=variation_rng,
            shared_shift=shared_loop_shift,
            base_cumulative_distances=base_route_cumulative,
        )
        append_closed_lap_to_continuous_route(
            route_points,
            route_cumulative,
            first_lap_points,
        )
        movement_cycle_length = route_cumulative[-1]
        next_lap_no = 1
    else:
        route_points = apply_open_route_variation(
            base_route,
            rng=random_source,
        )
        route_cumulative = build_cumulative_distances(route_points)
        movement_cycle_length = route_cumulative[-1] * 2.0

        # 6s是因为有三秒倒计时加gps还要三秒 最近才想明白 之前没注意
    intervals, segment_distances = build_fixed_interval_distance_plan(
        target_distance=target_distance,
        point_interval_s=point_interval_s,
        profile=profile,
        speed_range_ms=speed_range_ms,
        movement_cycle_length=movement_cycle_length,
        rng=random_source,
    )

    elapsed_time = 0
    run_step = 0
    step_accumulator = 0.0
    cumulative_distance = 0.0
    movement_progress_distance = 0.0
    previous_step_inc: Optional[float] = None
    track_points: List[TrackPoint] = []

    for segment_index, segment_distance_target_m in enumerate(segment_distances):
        interval_s = intervals[segment_index]
        if resolved_route_mode == "loop":
            route_buffer_m = max(
                movement_cycle_length,
                segment_distance_target_m * 8.0,
            )
            while (
                route_cumulative[-1] - movement_progress_distance
                < route_buffer_m
            ):
                next_lap_points = apply_lap_variation(
                    base_route,
                    lap_no=next_lap_no,
                    rng=variation_rng,
                    shared_shift=shared_loop_shift,
                    base_cumulative_distances=base_route_cumulative,
                )
                append_closed_lap_to_continuous_route(
                    route_points,
                    route_cumulative,
                    next_lap_points,
                )
                next_lap_no += 1

            (
                movement_progress_distance,
                point,
                actual_gps_distance_m,
            ) = advance_on_continuous_route_by_gps_distance(
                points=route_points,
                cumulative_distances=route_cumulative,
                current_route_distance_m=movement_progress_distance,
                target_gps_distance_m=segment_distance_target_m,
            )
            if not math.isclose(
                actual_gps_distance_m,
                segment_distance_target_m,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                raise RuntimeError(
                    "GPS 采样距离求解误差过大: "
                    f"{actual_gps_distance_m:.9f}/"
                    f"{segment_distance_target_m:.9f} 米"
                )
        else:
            (
                movement_progress_distance,
                point,
                _,
            ) = advance_on_out_and_back_route(
                points=route_points,
                cumulative_distances=route_cumulative,
                current_phase_m=movement_progress_distance,
                travel_distance_m=segment_distance_target_m,
            )

        segment_distance_m = segment_distance_target_m

        expected_step_inc = simulate_step_increment(
            speed_ms=segment_distance_m / interval_s,
            duration_s=interval_s,
            profile=profile,
            previous_step_inc=previous_step_inc,
            rng=random_source,
        )
        step_accumulator += expected_step_inc
        run_step = max(run_step, int(round(step_accumulator)))

        cumulative_distance += segment_distance_m
        if segment_index == len(segment_distances) - 1:
            cumulative_distance = float(target_distance)
        elapsed_time += interval_s
        pace = compute_pace_min_per_km(segment_distance_m, interval_s)

        track_points.append({
            "longitude": float(point[0]),
            "latitude": float(point[1]),
            "run_mileage_m": cumulative_distance,
            "run_time": int(elapsed_time),
            "run_step": int(run_step),
            "pace": float(pace),
            "ts": "",
        })

        previous_step_inc = expected_step_inc

    if not track_points:
        raise RuntimeError("未生成任何轨迹点")
    if not math.isclose(
        cumulative_distance,
        target_distance,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise RuntimeError(
            f"轨迹生成未达到目标距离: {cumulative_distance:.6f}/"
            f"{target_distance} 米"
        )

    start_time = (
        start_epoch_s
        if start_epoch_s is not None
        else int(time.time()) - elapsed_time
    )
    for track_point in track_points:
        track_point["ts"] = str(start_time + track_point["run_time"])

    return track_points

def split_track_points(
    track_points: Sequence[TrackPoint],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> List[List[TrackPoint]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not track_points:
        return []

    chunks: List[List[TrackPoint]] = []
    for i in range(0, len(track_points), chunk_size):
        chunk = list(track_points[i:i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks


def build_split_payload(
    user_name: str,
    school_id: str,
    crs_run_record_id: str,
    points: Sequence[TrackPoint],
    previous_last_point: Optional[TrackPoint] = None,
) -> RunSplitPayload:
    if not points:
        raise ValueError("points 不能为空")

    card_point_list: List[CardPoint] = []
    for p in points:
        pace = float(p["pace"])
        if pace <= 0.0:
            speed_str = "0.0"
        else:
            speed_str = f"{max(1.0, min(900.0, pace)):.2f}"
        card_point_list.append({
            "isFence": "Y",
            "isMock": False,
            "point": f"{p['longitude']},{p['latitude']}",
            "runMileage": p["run_mileage_m"],
            "runStatus": "1",
            "runStep": p["run_step"],
            "runTime": p["run_time"],
            "speed": speed_str,
            "ts": p["ts"],
        })

    prev_run_step = previous_last_point["run_step"] if previous_last_point else 0
    prev_run_time = previous_last_point["run_time"] if previous_last_point else 0
    prev_run_mileage = previous_last_point["run_mileage_m"] if previous_last_point else 0.0

    last_point = points[-1]

    step_number = int(last_point["run_step"] - prev_run_step)
    times = int(last_point["run_time"] - prev_run_time)
    mileage = float(last_point["run_mileage_m"] - prev_run_mileage)
    if step_number < 0 or times <= 0 or mileage <= 0:
        raise ValueError(
            "批次累计值必须递增: "
            f"steps={step_number}, times={times}, mileage={mileage}"
        )
    speeds = compute_pace_min_per_km(mileage, times)
    strides = mileage / step_number if step_number > 0 else 0.0
    run_steps = step_number / (times / 60.0) if times > 0 else 0.0

    return {
        "StepNumber": step_number,
        "a": 0,
        "b": None,
        "c": None,
        "cardPointList": card_point_list,
        "crsRunRecordId": crs_run_record_id,
        "mileage": mileage,
        "orientationNum": 0,
        "runSteps": run_steps,
        "schoolId": school_id,
        "simulateNum": 0,
        "speeds": speeds,
        "strides": strides,
        "times": times,
        "userName": user_name,
    }




def generate_run_data(
    target_distance: int,
    record_id: str,
    user: str,
    ra_name: str,
    school_id: str,
    cadence_range_spm: Tuple[float, float] = DEFAULT_CADENCE_RANGE_SPM,
    pace_range_min_per_km: Tuple[float, float] = DEFAULT_PACE_RANGE_MIN_PER_KM,
    stride_range: Tuple[float, float] = DEFAULT_STRIDE_RANGE,
    json_dir: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    point_interval_s: int = DEFAULT_POINT_INTERVAL_S,
    random_seed: Optional[int] = None,
    start_epoch_s: Optional[int] = None,
    route_mode: RouteMode = DEFAULT_ROUTE_MODE,
) -> List[RunSplitPayload]:
    if not json_dir:
        if not isinstance(school_id, str) or not school_id.strip():
            raise ValueError("school_id 不能为空字符串")
        tracks_root = Path(DEFAULT_TRACKS_DIR).resolve()
        school_tracks_dir = (tracks_root / school_id).resolve()
        try:
            school_tracks_dir.relative_to(tracks_root)
        except ValueError as exc:
            raise ValueError("school_id 不能越出 tracks 目录") from exc
        json_dir = str(school_tracks_dir)

    validate_generate_run_data_params(
        target_distance=target_distance,
        record_id=record_id,
        user=user,
        ra_name=ra_name,
        school_id=school_id,
        chunk_size=chunk_size,
        point_interval_s=point_interval_s,
        json_dir=json_dir,
        cadence_range_spm=cadence_range_spm,
        pace_range_min_per_km=pace_range_min_per_km,
        random_seed=random_seed,
        start_epoch_s=start_epoch_s,
        route_mode=route_mode,
    )

    track_points = generate_track_points(
        target_distance=target_distance,
        ra_name=ra_name,
        json_dir=json_dir,
        point_interval_s=point_interval_s,
        stride_range=stride_range,
        pace_range_min_per_km=pace_range_min_per_km,
        random_seed=random_seed,
        start_epoch_s=start_epoch_s,
        route_mode=route_mode,
    )

    chunks = split_track_points(track_points, chunk_size=chunk_size)

    payloads: List[RunSplitPayload] = []
    previous_last_point: Optional[TrackPoint] = None

    for chunk in chunks:
        payload = build_split_payload(
            user_name=user,
            school_id=school_id,
            crs_run_record_id=record_id,
            points=chunk,
            previous_last_point=previous_last_point,
        )
        payloads.append(payload)
        previous_last_point = chunk[-1]

    total_mileage = round(sum(item["mileage"] for item in payloads), 6)
    if not math.isclose(
        total_mileage,
        target_distance,
        rel_tol=0.0,
        abs_tol=1e-6,   # 末点强制对齐 target，mileage 原生相减累加应精确等于 target
    ):
        raise RuntimeError(
            f"批次里程合计错误: {total_mileage}/{target_distance} 米"
        )

    return payloads

if __name__ == "__main__":
    # 本地自检：学号/记录ID 换成自己的真实值再跑 其实你不用管
    from datetime import datetime, timedelta, timezone

    user = "your_student_id"
    record_id = "your_record_id"
    school_id = "106"
    record_start_time = "2026-01-01 08:00:00"
    start_epoch = int(
        datetime.strptime(record_start_time, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=timezone(timedelta(hours=8)))
        .timestamp()
    ) + 6

    data = generate_run_data(
        target_distance=1600,
        record_id=record_id,
        user=user,
        ra_name="蔚然运动场",
        school_id=school_id,
        pace_range_min_per_km=(5.5, 6.7),
        stride_range=(1.0, 1.12),
        start_epoch_s=start_epoch,
        route_mode="auto",
    )

    points = data[0]["cardPointList"]
    deltas = {int(p["ts"]) - p["runTime"] for p in points}
    rts = [p["runTime"] for p in points]
    gaps = [rts[i] - rts[i - 1] for i in range(1, len(rts))]
    print(
        f"batch={len(data)} 首batch点数={len(points)} 首点runTime={rts[0]} "
        f"间隔={ {g: gaps.count(g) for g in sorted(set(gaps))} } "
        f"ts-runTime={deltas} (应为 {{{start_epoch}}})"
    )
