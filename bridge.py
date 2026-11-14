# -*- coding: utf-8 -*-
"""JS ↔ Python 调用层；出错返回 {"__error": msg}。"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
from pathlib import Path

import api
import face_check
import services
import takeover
import upstream
from services import AppError

FACES_DIR = Path(__file__).resolve().parent / "data" / "faces"

VERSION_BASELINE: str = upstream.VERSION
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

_SESSION_CODE: str = ""
_SESSION_STALE: str = ""


def _note_school_change(cache: dict | None) -> None:
    global _SESSION_STALE
    if not isinstance(cache, dict) or not _SESSION_CODE:
        return
    if _SESSION_CODE in (cache.get("moved") or []):
        _SESSION_STALE = f"学校 {_SESSION_CODE} 的接口地址已变更"
        services.logger.warning("[bridge] %s", _SESSION_STALE)


def _clear_school_stale(code: str) -> None:
    global _SESSION_CODE, _SESSION_STALE
    _SESSION_CODE = code or ""
    _SESSION_STALE = ""

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
        d = self._call(api.status())
        if isinstance(d, dict):
            d["cache"] = services.school_cache_status()
            d["stale"] = _SESSION_STALE
            d["version"] = upstream.VERSION
        return d

    def schools(self, payload=None):
        d = self._call(api.schools())
        if isinstance(d, dict):
            _note_school_change(d.get("cache"))
            d["stale"] = _SESSION_STALE
        return d

    def set_app_version(self, payload=None):
        p = self._payload(payload)
        v = str(p.get("version", "")).strip().lstrip("vV")
        if not v:
            old, upstream.VERSION = upstream.VERSION, VERSION_BASELINE
            services.logger.info("[version] 上报版本恢复内置基线 %s（原 %s）", VERSION_BASELINE, old)
            return {"version": VERSION_BASELINE, "warning": ""}
        if not _VERSION_RE.match(v):
            return self._err(f"版本号格式不对，应形如 3.6.7（收到 {v!r}）")
        old = upstream.VERSION
        upstream.VERSION = v
        services.logger.warning("[version] 上报版本 %s → %s（来源: 设置页手改）", old, v)
        warning = ""
        if v.split(".")[0] != VERSION_BASELINE.split(".")[0]:
            warning = (f"与内置基线 {VERSION_BASELINE} 不同大版本，协议可能一起变了；"
                       f"跑不通就点「恢复默认」")
        return {"version": v, "previous": old, "warning": warning}

    def device_models(self, payload=None):
        """内置机型列表：登录页的设备下拉用它，前端不另抄一份。"""
        return {"models": list(upstream.DEVICE_MODELS), "version": upstream.VERSION}


    def login(self, payload=None):
        p = self._payload(payload)
        try:
            req = api.LoginRequest(**p)
        except Exception as e:
            return self._err(f"登录参数错误: {e}")
        d = self._call(api.login(req))
        if isinstance(d, dict) and d.get("token"):
            _clear_school_stale(str(d.get("school_code") or req.school_code))
        return d

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

    def resume(self, payload=None):
        p = self._payload(payload)
        code = str(p.get("school_code", ""))
        d = self._call(api.resume(code, p.get("run_areas") or []))
        if isinstance(d, dict) and not d.get("__error"):
            _clear_school_stale(str(d.get("school_code") or code))
        return d

    def track_query(self, payload=None):
        p = self._payload(payload)
        return self._call(api.track_query(str(p.get("school_code", "")), str(p.get("ra_name", ""))))

    def track_status(self, payload=None):
        p = self._payload(payload)
        names = p.get("ra_names") or []
        if not isinstance(names, list):
            return self._err("ra_names 必须是数组")
        return self._call(api.track_status(str(p.get("school_code", "")), [str(n) for n in names]))

    # ── 同设备登录：从自己的安卓真机接管会话 ──
    def takeover_start(self, payload=None):
        p = self._payload(payload)
        return takeover.start(bool(p.get("clear_log", True)))

    def takeover_poll(self, payload=None):
        return takeover.poll()

    def takeover_cancel(self, payload=None):
        return takeover.cancel()

    def takeover_install_adb(self, payload=None):
        return takeover.install_adb()

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
        check = face_check.validate(raw)
        if not check["ok"]:
            return self._err("；".join(check["errors"]))
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        name = f"face_{int(time.time() * 1000)}.jpg"
        (FACES_DIR / name).write_bytes(raw)
        return {"name": name, "warning": check["warning"]}

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
