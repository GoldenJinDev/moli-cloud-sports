from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import random
import time
import uuid as _uuid
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from gmssl import func, sm2
from gmssl.sm4 import SM4_DECRYPT, SM4_ENCRYPT, CryptSM4

from track_gen import RunSplitPayload, generate_run_data

logger = logging.getLogger(__name__)

VERSION: str = '3.6.6'
DEVICE_MODELS: List[str] = [
    "vivo(V2366GA)", "vivo(V2415A)", "vivo(V2509A)",
    "Xiaomi(24129PN74C)", "Xiaomi(25019PNF3C)", "Xiaomi(25113PN0EC)",
    "Redmi(2407FRK8EC)", "Redmi(2510DRK44C)",
    "OPPO(PKZ110)", "iQOO(V2307A)",
]
PublicKey: str = 'BDdKFsuBf51UObke1pEgfER17biBg/5r8slqE4s8oOa8lVesWgIUxsRc+AmZ72GcuJ56f7avnyJe3CJY4n00LU4='
PUBLIC_KEY: bytes = b64decode(PublicKey)
SCHOOL_LIST_URL: str = 'https://sports.aiyyd.com:9011/api/app/lisshtcool'
SCHOOL_LIST_HOST: str = 'sports.aiyyd.com:9011'
_SESSION: requests.Session | None = None
_TIMEOUT = (10, 30)

def get_http_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        _SESSION.mount("http://", adapter)
        _SESSION.mount("https://", adapter)
        _SESSION.trust_env = False  # 忽略系统/环境变量代理
    return _SESSION


async def close_http_client():
    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


def _url_host(url: str) -> str:
    p = urlparse(url)
    host = p.netloc or p.hostname or ''
    return host


def _bytes_to_hex(b: bytes) -> str:
    return hex(int.from_bytes(b, "big"))[2:].upper()


_sm2_crypt: Optional[sm2.CryptSM2] = None


def get_sm2() -> sm2.CryptSM2:
    global _sm2_crypt
    if _sm2_crypt is None:
        _sm2_crypt = sm2.CryptSM2(
            public_key=_bytes_to_hex(PUBLIC_KEY[1:]),
            private_key="",
            mode=1,
            asn1=True,
        )
    return _sm2_crypt


def generate_sm4() -> str:
    key_bytes = bytes.fromhex(func.random_hex(32))
    return b64encode(key_bytes).decode("utf-8")


def encrypt_sm2(plain_text: str) -> str:
    encrypted_bytes = get_sm2().encrypt(plain_text.encode("utf-8"))
    hex_with_prefix = "04" + encrypted_bytes.hex().upper()
    return b64encode(bytes.fromhex(hex_with_prefix)).decode()


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def encrypt_sm4(value: str | bytes, sm_key: bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    data = _pkcs7_pad(data, 16)
    crypt = CryptSM4()
    crypt.set_key(sm_key, SM4_ENCRYPT)
    return b64encode(crypt.crypt_ecb(data)).decode("utf-8")


def decrypt_sm4(sm4_key: bytes, data_base64: str):
    crypt = CryptSM4()
    crypt.set_key(sm4_key, SM4_DECRYPT)
    try:
        raw = crypt.crypt_ecb(b64decode(data_base64))
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return json.loads(gzip.decompress(raw).decode("utf-8"))
    except Exception:
        try:
            return json.loads(data_base64)
        except Exception:
            return data_base64


def md5_jm(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def requests_headers(uuid: str, device_name: str, token: str = "", host: str = "") -> dict:
    """构建请求头
    """
    utc = str(int(time.time()))
    request_uuid = str(_uuid.uuid4()).upper()
    sign_raw = f"platform=android&utc={utc}&uuid={request_uuid}&appsecret=0h1UIfMDSc7piesRINRXXfkE"
    return {
        "token": token,
        "isApp": "app",
        "deviceId": uuid, #64位hex，非 UUID 这地方是我之前手误 后续就这样吧
        "deviceName": device_name,
        "version": VERSION,
        "sysVersion": "15",
        "platform": "android",
        "uuid": request_uuid,  # 每次请求随机生成的 UUID <-这个才是uuid 不过大写哈
        "utc": utc,
        "sign": md5_jm(sign_raw),
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": "okhttp/4.9.1",
    }


async def creat_request_async(
    url: str,
    data: str | bytes,
    uuid: str,
    device_name: str,
    token: str,
    *,
    max_retries: int = 3,
    base_backoff: float = 1.0,
) -> str | dict | None:
    sm4_key_b64 = generate_sm4()
    sm4_key_bytes = b64decode(sm4_key_b64)

    cipher_key, content = await asyncio.to_thread(
        _encrypt_payload, sm4_key_b64, sm4_key_bytes, data
    )

    encrypted_body = {"cipherKey": cipher_key, "content": content}
    host = _url_host(url)
    headers = requests_headers(uuid=uuid, device_name=device_name, token=token, host=host)
    session = get_http_session()

    def _post() -> requests.Response:
        return session.post(url, json=encrypted_body, headers=headers, timeout=_TIMEOUT)

    last_exc: Exception = RuntimeError("未知错误")
    for attempt in range(max_retries + 1):
        try:
            resp = await asyncio.to_thread(_post)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"服务器错误 {resp.status_code}")
            if resp.status_code >= 400:
                logger.warning("[creat_request] %s 客户端错误 %s", url, resp.status_code)
                return None
            result = await asyncio.to_thread(decrypt_sm4, sm4_key_bytes, resp.text.strip())
            return result
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            last_exc = e
            if attempt < max_retries:
                wait = base_backoff * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "[creat_request] 第%d次重试 url=%s 等待%.1fs 原因=%s",
                    attempt + 1, url, wait, e,
                )
                await asyncio.sleep(wait)
        except Exception as e:
            logger.error("[creat_request] 不可重试错误 url=%s err=%s", url, e)
            return None

    logger.error("[creat_request] 已达最大重试次数 url=%s err=%s", url, last_exc)
    return None


def _encrypt_payload(
    sm4_key_b64: str, sm4_key_bytes: bytes, data: str | bytes
) -> Tuple[str, str]:
    return encrypt_sm2(sm4_key_b64), encrypt_sm4(data, sm4_key_bytes)


def have_device_name() -> str:
    return random.choice(DEVICE_MODELS)


def _endpoint(url: str, path: str) -> str:
    return f"{url.rstrip('/')}/{path.lstrip('/')}"


def _get_login_path(school_code: str) -> str:
    if school_code == "100":
        return "/login/appLoginHGD"
    return "/login/appLogin"


def _get_login_paths(school_code: str, preferred_path: str = "") -> list[str]:
    special_paths = {
        "100": ["/login/appLoginHGD", "/login/appLogin"], # 合工大
        "106": ["/login/appLoginCHZU", "/login/appLogin"], # 滁州学院
    }
    paths = special_paths.get(school_code, [_get_login_path(school_code)]).copy()
    if preferred_path in paths:
        paths.remove(preferred_path)
        paths.insert(0, preferred_path)
    return paths


_login_path_cache: dict[str, str] = {}
_login_path_locks: dict[str, asyncio.Lock] = {}


def _get_login_path_lock(school_code: str) -> asyncio.Lock:
    if school_code not in _login_path_locks:
        _login_path_locks[school_code] = asyncio.Lock()
    return _login_path_locks[school_code]


async def list_schools(uuid: str, device_name: str) -> list[dict]:
    host = _url_host(SCHOOL_LIST_URL)
    headers = requests_headers(uuid=uuid, device_name=device_name, token="", host=host)
    session = get_http_session()

    def _post() -> requests.Response:
        return session.post(SCHOOL_LIST_URL, headers=headers, timeout=_TIMEOUT)

    try:
        resp = await asyncio.to_thread(_post)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 200:
            return result.get("data", [])
        return []
    except Exception as e:
        logger.error("[list_schools] 请求失败: %s", e)
        return []


async def login(
    user: str, password: str, uuid: str, device_name: str,
    school_url: str, school_code: str, preferred_path: str = "",
) -> dict:
    data = json.dumps(
        {"password": password, "schoolId": school_code, "userName": user, "type": "1"},
        separators=(",", ":"),
    )
    lock = _get_login_path_lock(school_code)
    async with lock:
        cached_path = _login_path_cache.get(school_code, "")
        paths = _get_login_paths(school_code, cached_path or preferred_path)
        last_message = "登录失败"
        for path in paths:
            url = _endpoint(school_url, path)
            content = await creat_request_async(url, data, uuid, device_name, token="")
            token = content.get("data", {}).get("token") if isinstance(content, dict) else None
            if token:
                _login_path_cache[school_code] = path
                logger.info("[login] 登录端点成功: %s", path)
                return {"token": token}
            if isinstance(content, dict):
                last_message = content.get("msg", last_message)
            elif content:
                last_message = str(content)
            logger.warning("[login] 登录端点失败，尝试下一个: %s", path)
        return {"msg": last_message}


async def get_student_info(school_url: str, uuid: str, device_name: str, token: str) -> dict:
    url = _endpoint(school_url, "/login/getStudentInfo")
    content = await creat_request_async(url, "", uuid, device_name, token)
    if not content or content.get("code") != 200:
        return {}
    data = content.get("data", {})
    return {
        "nickName": data.get("nickName", ""),
        "sex": {1: "男", 2: "女"}.get(data.get("sex"), "未知"), # 目前没看到位置 有的话记得和我说一下
        "realName": data.get("realName", ""),
        "studentId": data.get("userName", ""),
        "school": data.get("schoolName", ""),
        "campus": data.get("campusName", ""),
        "faculty": data.get("facName", ""),
        "major": data.get("pfsName", ""),
        "className": data.get("className", ""),
    }


async def get_home_run_info(school_url: str, uuid: str, device_name: str, token: str) -> Optional[list]:
    url = _endpoint(school_url, "/run/getHomeRunInfo")
    content = await creat_request_async(url, "", uuid, device_name, token)
    if not content or content.get("code") == 401:
        return None
    r_data = content.get("data", {})
    cra_list = r_data.get("cralist", [])
    if not cra_list:
        raise ValueError("cralist 为空，无法获取跑步活动信息")
    return cra_list


async def get_home_run_stat(school_url: str, uuid: str, device_name: str, token: str) -> dict:
    url = _endpoint(school_url, "/run/getHomeRunInfo")
    content = await creat_request_async(url, "", uuid, device_name, token)
    if not content or content.get("code") == 401:
        return {}
    return content.get("data", {})


async def run_start(
    school_url: str, uuid: str, token: str,
    ra_run_area: str, ra_type: str, run_area_id: int, device_name: str,
) -> dict:
    url = _endpoint(school_url, "/run/start")
    parameter = json.dumps(
        {"raRunArea": ra_run_area, "raType": ra_type, "raId": str(run_area_id)},
        separators=(",", ":"),
    )
    content = await creat_request_async(url, parameter, uuid, device_name, token)
    if not content or content.get("code") != 200:
        return {"id": None, "recordStartTime": None, "ip": None,
                "warnContent": "接口请求失败", "raId": None}
    info = content.get("data", {})
    return {
        k: info.get(k)
        for k in (
            "id", "recordStartTime", "ip", "warnContent", "raId",
            "faceTime", "randomList",  # 刷脸跑时下发:识别时长 + 抽查时间点(秒)
        )
    }


async def split_point_cheating(
    school_url: str, token: str, y_point: RunSplitPayload | dict,
    uuid: str, device_name: str,
):
    url = _endpoint(school_url, "/run/splitPointCheating")
    data = gzip.compress(json.dumps(y_point).encode("utf-8"))
    await creat_request_async(url, data, uuid, device_name, token)


async def is_standard(
    school_url: str, uuid: str, token: str, device_name: str,
    generate_data: str | dict,
):
    url = _endpoint(school_url, "/run/isStandard")
    await creat_request_async(url, generate_data, uuid, device_name, token)


async def finish(
    school_url: str, token: str, generate_data: str | dict,
    uuid: str, device_name: str,
) -> str:
    url = _endpoint(school_url, "/run/finish")
    content = await creat_request_async(url, generate_data, uuid, device_name, token)
    if not content:
        return "finish 请求失败"
    return content.get("msg", "未知结果")



def file_to_base64(path: str) -> str:

    with open(path, "rb") as f:
        return b64encode(f.read()).decode("ascii")


async def start_face_run(
    school_url: str, uuid: str, token: str, ra_run_area: str, device_name: str,
) -> str | None:
    """刷脸跑前校验:run/getRlStatus,返回 runFaceStudentStatus("Y"/"N"/"N0"/"N1")。
    - "Y"  → 已通过,直接 run/start
    - "N"  → 未录入,需先 face_run_add_face
    - "N0" → 认证失败,重新录入
    - "N1" → 审核中,无法跑步
    - 注意：目前我没账号测试 截至2026-09-23
    """
    url = _endpoint(school_url, "/run/getRlStatus")
    parameter = json.dumps({"raRunArea": ra_run_area}, separators=(",", ":"))
    content = await creat_request_async(url, parameter, uuid, device_name, token)
    if not content or content.get("code") != 200:
        return None
    return content.get("data", {}).get("runFaceStudentStatus")


async def face_run_add_face(
    school_url: str, uuid: str, token: str, face_base_data: str, device_name: str,
) -> dict | None:
    """人脸录入"""
    url = _endpoint(school_url, "/run/appFace/runFaceInfo")
    parameter = json.dumps({"faceBaseData": face_base_data}, separators=(",", ":"))
    return await creat_request_async(url, parameter, uuid, device_name, token)


async def face_run_compare_face(
    school_url: str, uuid: str, token: str, face_base_data: str,
    record_id: str, device_name: str,
) -> dict | None:
    """跑中抽查比对，data.status="Y" 表示通过"""
    url = _endpoint(school_url, "/run/appFace/runFaceInfoComparison")
    parameter = json.dumps(
        {"faceBaseData": face_base_data, "recordId": record_id},
        separators=(",", ":"),
    )
    return await creat_request_async(url, parameter, uuid, device_name, token)


async def list_xn_year_xq_by_student_id(
    school_url: str, token: str, uuid: str, device_name: str,
) -> list[dict]:
    url = _endpoint(school_url, "/run/listXnYearXqByStudentId")
    content = await creat_request_async(url, "", uuid, device_name, token)
    return content.get("data", []) if content else []


async def crs_reocord_info_list(
    school_url: str, token: str, uuid: str, device_name: str, table_name: str,
):
    url = _endpoint(school_url, "/run/crsReocordInfoList")
    data = json.dumps({"tableName": table_name}, separators=(",", ":"))
    return await creat_request_async(url, data, uuid, device_name, token)


def build_finish_from_batches(
    batches: list[RunSplitPayload],
    device_name: str,
    ra_run_area: str,
    ra_type: str,
    record_id: str,
    record_start_time: str,
    ra_id: str,
    card_points: Optional[list[str]] = None,
    ra_dislikes: int = 0,
) -> dict:
    all_card_points = [p for batch in batches for p in batch["cardPointList"]]
    last = all_card_points[-1]
    total_mileage_m = float(last["runMileage"])
    total_time_s = int(last["runTime"])
    total_steps = int(last["runStep"])
    total_km = total_mileage_m / 1000.0
    pace = (total_time_s / 60.0) / total_km if total_km > 0 else 0.0
    cadence = int(total_steps / (total_time_s / 60.0)) if total_time_s > 0 else 0
    if card_points:
        if ra_dislikes > 0 and ra_dislikes < len(card_points):
            card_points = random.sample(card_points, ra_dislikes)
        manage_list = [
            {"point": pt, "marked": "Y", "index": str(i)}
            for i, pt in enumerate(card_points)
        ]
    else:
        all_points = [p["point"] for p in all_card_points]
        take_n = min(5, len(all_points))
        selected = (
            [all_points[round(i * (len(all_points) - 1) / (take_n - 1))] for i in range(take_n)]
            if take_n > 1
            else all_points[:1]
        )
        manage_list = [
            {"point": pt, "marked": "Y", "index": str(i)}
            for i, pt in enumerate(selected)
        ]
    return {
        "manageList": manage_list,
        "recordMileage": f"{total_km:.2f}",
        "recodeCadence": str(cadence),
        "recodePace": f"{pace:.2f}",
        "deviceName": device_name,
        "sysEdition": "Android_15",
        "appEdition": VERSION,
        "raIsStartPoint": "Y",
        "raIsEndPoint": "Y",
        "raRunArea": ra_run_area,
        "recodeDislikes": str(len(manage_list)),
        "raId": str(ra_id),
        "raType": ra_type,
        "id": str(record_id),
        "duration": str(total_time_s),
        "recordStartTime": record_start_time,
        "remake": "0|{}",
    }



async def yun_run(
    school_url: str,
    school_code: str,
    uuid: str,
    device_name: str,
    token: str,
    target_distance: int,
    user: str,
    # 配速区间(min/km)：这地方不要改了 就这样了 我现在也不敢瞎改了
    pace_range_min_per_km: Tuple[float, float] = (5.5, 6.7),
    cadence_range_spm: Tuple[float, float] = (145.0, 175.0),
    fast: bool = False,
    preferred_ra_name: Optional[str] = None,
    face_image_path: Optional[str] = None,
) -> dict | None:
    run_info = await get_home_run_info(
        school_url=school_url, uuid=uuid, device_name=device_name, token=token,
    )
    if not run_info:
        raise ValueError("获取跑步信息失败，token 可能已失效")

    if preferred_ra_name:
        item = next(
            (i for i in run_info if i.get("raName") == preferred_ra_name),
            run_info[0],
        )
    elif len(run_info) >= 2:
        item = next(
            (i for i in run_info if "蔚然运动场" in i.get("raName", "")),
            run_info[0],
        )
    else:
        item = run_info[0]
    run_area_id = item["id"]
    ra_run_area = item["raRunArea"]
    ra_type = item["raType"]
    ra_name = item["raName"]
    points_str = item.get("points", "") or ""
    card_points = [p.strip() for p in points_str.split("|") if p.strip()]
    ra_dislikes = int(item.get("raDislikes", 0) or 0)

    #刷脸
    face_data: Optional[str] = None
    run_face_status = item.get("runFaceStatus", "") or ""
    if run_face_status == "Y":
        if not face_image_path:
            raise ValueError(
                "该任务需要刷脸(runFaceStatus=Y),必须提供 face_base64(人脸照片Base64)"
            )
        face_data = file_to_base64(face_image_path)
        status = await start_face_run(
            school_url=school_url, uuid=uuid, token=token,
            ra_run_area=ra_run_area, device_name=device_name,
        )
        if status == "N" or status == "N0":
            await face_run_add_face(
                school_url=school_url, uuid=uuid, token=token,
                face_base_data=face_data, device_name=device_name,
            )
        elif status == "N1":
            raise ValueError("身份认证审核中,无法进行跑步")

    start_info = await run_start(
        school_url=school_url, uuid=uuid, token=token,
        ra_run_area=ra_run_area, ra_type=ra_type,
        run_area_id=run_area_id, device_name=device_name,
    )
    record_id = start_info["id"]
    record_start_time = start_info["recordStartTime"]
    ra_id = start_info["raId"]
    #写到这我的脑海里就想起了 3 2 1 开始跑步 然后
    start_epoch = int(
        datetime.strptime(record_start_time, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=timezone(timedelta(hours=8)))
        .timestamp()
    ) + 6
    # 人脸抽查参数(刷脸跑时下发)
    face_time = start_info.get("faceTime") or 0
    random_list = start_info.get("randomList") or []
    generate_run_data_list = await asyncio.to_thread(
        generate_run_data,
        target_distance=target_distance,
        record_id=record_id,
        user=user,
        ra_name=ra_name,
        school_id=school_code,
        pace_range_min_per_km=pace_range_min_per_km,
        cadence_range_spm=cadence_range_spm,
        route_mode="auto",
        start_epoch_s=start_epoch,
    )

    pending_checks = sorted(float(t) for t in random_list) if random_list else []
    check_idx = 0
    total = len(generate_run_data_list)
    for i, y_point in enumerate(generate_run_data_list, start=1):
        sleep_sec = 0.05 if fast else int(y_point["times"])
        await asyncio.sleep(sleep_sec)
        cum_time = float(y_point["cardPointList"][-1]["runTime"])
        while (
            face_data is not None
            and check_idx < len(pending_checks)
            and cum_time >= pending_checks[check_idx]
        ):
            res = await face_run_compare_face(
                school_url=school_url, uuid=uuid, token=token,
                face_base_data=face_data, record_id=record_id, device_name=device_name,
            )
            if not res or res.get("data", {}).get("status") != "Y":
                raise ValueError(
                    f"人脸比对失败(抽查点 {pending_checks[check_idx]}s): "
                    f"{res and res.get('msg')}"
                )
            check_idx += 1

        await split_point_cheating(
            school_url=school_url, token=token, y_point=y_point,
            uuid=uuid, device_name=device_name,
        )

        if i == total:
            manage_point = json.dumps(
                build_finish_from_batches(
                    batches=generate_run_data_list,
                    ra_id=ra_id,
                    ra_run_area=ra_run_area,
                    device_name=device_name,
                    ra_type=ra_type,
                    record_id=record_id,
                    record_start_time=record_start_time,
                    card_points=card_points,
                    ra_dislikes=ra_dislikes,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await is_standard(
                school_url=school_url, uuid=uuid, token=token,
                device_name=device_name, generate_data=manage_point,
            )
            msg = await finish(
                school_url=school_url, token=token, generate_data=manage_point,
                uuid=uuid, device_name=device_name,
            )
            return {"msg": msg, "start_time": record_start_time}
    return None

async def have_yd_info(school_url: str, token: str, uuid: str, device_name: str):
    xn_list = await list_xn_year_xq_by_student_id(
        school_url=school_url, token=token, uuid=uuid, device_name=device_name,
    )
    if not xn_list:
        return {}
    xn_year = xn_list[0]["value"]
    return await crs_reocord_info_list(
        school_url=school_url, token=token, uuid=uuid, device_name=device_name, table_name=xn_year,
    )


async def get_sport_detail(school_url: str, token: str, uuid: str, device_name: str) -> dict:
    stat = await get_home_run_stat(
        school_url=school_url, uuid=uuid, device_name=device_name, token=token,
    )
    records = await have_yd_info(
        school_url=school_url, token=token, uuid=uuid, device_name=device_name,
    )
    return {"stat": stat, "records": records}
