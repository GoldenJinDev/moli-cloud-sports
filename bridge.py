# -*- coding: utf-8 -*-
"""JS ↔ Python 调用层；出错返回 {"__error": msg}。"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path

import api
import services
from services import AppError

FACES_DIR = Path(__file__).resolve().parent / "data" / "faces"

_LOOP = asyncio.new_event_loop()
_READY = threading.Event()


def _loop_worker() -> None:
    async def _serve() -> None:
        await api.startup()
        _READY.set()
        try:
            await asyncio.Event().wait()
        finally:
            await api.shutdown()

    try:
        _LOOP.run_until_complete(_serve())
    except Exception:
        _READY.set()


threading.Thread(target=_loop_worker, daemon=True, name="moli-loop").start()


class Bridge:
    def __init__(self) -> None:
        _READY.wait(timeout=30)

    @staticmethod
    def _payload(payload) -> dict:
        if payload is None:
            return {}
        if isinstance(payload, str):
            try:
                return json.loads(payload or "{}")
            except Exception:
                return {}
        if isinstance(payload, dict):
            return dict(payload)
        return {}

    @staticmethod
    def _err(msg) -> dict:
        return {"__error": str(msg)}

    def _call(self, coro, timeout: float | None = 1800):
        if not _LOOP.is_running():
            return self._err("本地服务循环未运行，请重启应用")
        future = asyncio.run_coroutine_threadsafe(coro, _LOOP)
        try:
            return future.result(timeout)
        except AppError as e:
            return self._err(e.detail)
        except Exception as e:
            return self._err(getattr(e, "message", None) or e)


    def status(self, payload=None):
        return self._call(api.status())

    def schools(self, payload=None):
        return self._call(api.schools())


    def login(self, payload=None):
        p = self._payload(payload)
        try:
            req = api.LoginRequest(**p)
        except Exception as e:
            return self._err(f"登录参数错误: {e}")
        return self._call(api.login(req))

    def sport_info(self, payload=None):
        p = self._payload(payload)
        try:
            req = services.HaveYdInfoRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(services.api_sport_info(req))

    def have_yd_info(self, payload=None):
        p = self._payload(payload)
        try:
            req = services.HaveYdInfoRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(services.api_have_yd_info(req))

    def yun_run(self, payload=None):
        p = self._payload(payload)
        try:
            req = services.YunRunRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(services.api_yun_run(req))

    def task(self, payload=None):
        p = self._payload(payload)
        return self._call(services.api_get_task(str(p.get("task_id", ""))))

    def run_record_detail(self, payload=None):
        p = self._payload(payload)
        try:
            req = services.RunRecordDetailRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(services.api_run_record_detail(req))

    def validate(self, payload=None):
        p = self._payload(payload)
        try:
            req = api.ValidateRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(api.validate(req))

    def upload(self, payload=None):
        p = self._payload(payload)
        try:
            req = api.UploadRequest(**p)
        except Exception as e:
            return self._err(f"参数错误: {e}")
        return self._call(api.upload(req))

    def track_query(self, payload=None):
        p = self._payload(payload)
        return self._call(api.track_query(str(p.get("school_code", "")), str(p.get("ra_name", ""))))

    # ── 人脸照片（data/faces/*.jpg 真实文件，轮询游标存 data/faces/.cursor） ──
    @staticmethod
    def _face_files() -> list[Path]:
        if not FACES_DIR.is_dir():
            return []
        return sorted(FACES_DIR.glob("*.jpg"), key=lambda x: x.name)

    def face_list(self, payload=None):
        return {"photos": [{"name": p.name, "size": p.stat().st_size} for p in self._face_files()]}

    def face_add(self, payload=None):
        p = self._payload(payload)
        b64 = str(p.get("data_base64", "")).split(",")[-1].strip()
        if not b64:
            return self._err("照片数据为空")
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return self._err("照片数据无法解析")
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        name = f"face_{int(time.time() * 1000)}.jpg"
        (FACES_DIR / name).write_bytes(raw)
        return {"name": name}

    def face_delete(self, payload=None):
        p = self._payload(payload)
        name = Path(str(p.get("name", ""))).name  # 防路径穿越
        f = FACES_DIR / name
        if not name or not f.is_file():
            return self._err("文件不存在")
        f.unlink()
        return {"ok": True}

    def face_get(self, payload=None):
        p = self._payload(payload)
        name = Path(str(p.get("name", ""))).name
        f = FACES_DIR / name
        if not name or not f.is_file():
            return self._err("文件不存在")
        return {"name": name, "data_base64": base64.b64encode(f.read_bytes()).decode()}

    def face_next(self, payload=None):
        files = self._face_files()
        if not files:
            return self._err("没有人脸照片")
        cursor = FACES_DIR / ".cursor"
        try:
            idx = int(cursor.read_text())
        except Exception:
            idx = 0
        pick = files[idx % len(files)]
        try:
            cursor.write_text(str(idx + 1))
        except Exception:
            pass
        return {"name": pick.name, "data_base64": base64.b64encode(pick.read_bytes()).decode()}
