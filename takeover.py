# -*- coding: utf-8 -*-
"""takeover.py —— 从自己的安卓真机接管登录态（供界面调用）

只做三件事：定位 adb → 读设备身份（deviceId / deviceName）→ 从云运动 App 打印到
Android 调试日志(logcat) 的明文里取出登录 token。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PKG = "com.yunzhi.tiyu"
DEVICE_ID_PATH = f"/sdcard/Android/data/{PKG}/files/.sys_device_id"
PLAT_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"

TOKEN_RE = re.compile(r'"token"\s*:\s*"(.*?)"')
TT_RE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+[VDIWEAF]\s+snow\s*:\s?(.*)$")
HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

STEP_LABELS = [("adb", "查找 adb"), ("device", "连接手机"), ("identity", "读取设备身份"), ("token", "提取 token")]
TOKEN_WAIT_S = 180


def _run(args: list[str], timeout: int = 20) -> tuple[str, str, int]:
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "", str(e), -1
    return (r.stdout or "").strip(), (r.stderr or "").strip(), r.returncode


def locate_adb() -> str | None:
    for cand in (shutil.which("adb"), str(ROOT / "platform-tools" / "adb.exe")):
        if cand and Path(cand).is_file():
            return cand
    return None


class Job:
    """一次接管流程的进度快照，界面轮询读取。"""

    def __init__(self, clear_log: bool = True):
        self.clear_log = clear_log
        self.status = "running"
        self.steps = [{"key": k, "label": v, "state": "pending", "detail": ""} for k, v in STEP_LABELS]
        self.log: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error = ""
        self.device: dict[str, str] = {}
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    def _set(self, key: str, state: str, detail: str = "") -> None:
        with self._lock:
            for s in self.steps:
                if s["key"] == key:
                    s["state"], s["detail"] = state, detail
                    break
            if detail:
                self.log.append(f"{datetime.now():%H:%M:%S} {detail}")

    def note(self, text: str) -> None:
        with self._lock:
            self.log.append(f"{datetime.now():%H:%M:%S} {text}")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status, "steps": [dict(s) for s in self.steps],
                "log": self.log[-40:], "result": self.result, "error": self.error,
            }

    def finish(self, status: str, error: str = "") -> None:
        with self._lock:
            self.status, self.error = status, error


_JOB: Job | None = None
_JOB_LOCK = threading.Lock()


def start(clear_log: bool = True) -> dict:
    global _JOB
    with _JOB_LOCK:
        if _JOB and _JOB.status == "running":
            return _JOB.snapshot()
        _JOB = Job(clear_log)
        threading.Thread(target=_worker, args=(_JOB,), daemon=True, name="moli-takeover").start()
        return _JOB.snapshot()


def poll() -> dict:
    with _JOB_LOCK:
        if not _JOB:
            return {"status": "idle", "steps": [], "log": [], "result": None, "error": ""}
        return _JOB.snapshot()


def cancel() -> dict:
    with _JOB_LOCK:
        if _JOB and _JOB.status == "running":
            _JOB.cancel.set()
        return _JOB.snapshot() if _JOB else poll()


def install_adb() -> dict:
    """下载 Google 官方 platform-tools 并解压到本目录，供 locate_adb 兜底发现。"""
    if locate_adb():
        return {"ok": True, "path": locate_adb(), "message": "adb 已存在"}
    dest = ROOT / "platform-tools"
    zip_path = Path(os.environ.get("TEMP", ".")) / "platform-tools.zip"
    try:
        urllib.request.urlretrieve(PLAT_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ROOT)
        return {"ok": True, "path": str(dest / "adb.exe"), "message": "adb 安装完成"}
    except Exception as e:
        return {"ok": False, "path": "", "message": f"adb 安装失败：{e}"}
    finally:
        zip_path.unlink(missing_ok=True)


def _worker(job: Job) -> None:
    try:
        adb = _step_adb(job)
        _step_device(job, adb)
        identity = _step_identity(job, adb)
        token = _step_token(job, adb, identity["pid"])
        if job.cancel.is_set():
            return job.finish("cancelled")
        job.result = {**identity, "token": token}
        job.finish("done")
    except Abort as e:
        job.finish("failed", str(e))
    except Exception as e:
        job.finish("failed", f"未预期错误：{e}")


class Abort(Exception):
    pass


def _die(job: Job, key: str, detail: str):
    job._set(key, "failed", detail)
    raise Abort(detail)


def _step_adb(job: Job) -> str:
    adb = locate_adb()
    if not adb:
        _die(job, "adb", "未找到 adb。请安装 Android platform-tools，或点「自动下载」")
    out, err, code = _run([adb, "version"])
    if code:
        _die(job, "adb", f"adb 无法执行：{(err or out)[:80] or '未知错误'}")
    job._set("adb", "ok", (out.splitlines() or ["adb"])[0].strip()[:48])
    return adb


def _step_device(job: Job, adb: str) -> None:
    out, err, code = _run([adb, "get-state"])
    if code or out.strip() != "device":
        text = (out + err).lower()
        if "offline" in text or "unauthorized" in text:
            detail = "设备处于 offline/unauthorized：请在手机弹窗上勾选「一律允许」"
        elif "no device" in text or "not found" in text:
            detail = "未检测到设备：请用数据线连接手机，并确认已开启 USB 调试"
        elif "more than one" in text or "multiple" in text:
            detail = "同时连接了多台设备：请只保留一台（或在开发者选项里断开其余 adb 会话）"
        elif code == -1:
            detail = "adb 响应超时：请重新插拔数据线"
        else:
            detail = f"设备不可用：{(out + err).strip()[:80] or '未连接'}"
        _die(job, "device", detail)
    mfr = _shell(adb, "getprop ro.product.manufacturer")
    model = _shell(adb, "getprop ro.product.model")
    release = _shell(adb, "getprop ro.build.version.release")
    if not mfr or not model:
        _die(job, "device", "读不到设备属性：手机可能未授权调试，或系统限制 adb shell getprop")
    job._set("device", "ok", f"{mfr}({model}) · Android {release}")
    job.device = {"device_name": f"{mfr}({model})", "sys_edition": f"Android_{release}"}


def _step_identity(job: Job, adb: str) -> dict:
    pid = _shell(adb, f"pidof {PKG}").split()
    out, err, code = _run([adb, "shell", "cat", DEVICE_ID_PATH])
    found = HEX64_RE.search(out) or HEX64_RE.search(err)
    if not found:
        detail = _read_error(out, err, code)
        _die(job, "identity", f"读不到 deviceId：{detail}")
    identity = {**job.device, "uuid": found.group(0), "pid": pid[0] if pid else ""}
    job._set("identity", "ok", f"deviceId={found.group(0)[:12]}… {identity['device_name']}")
    if not identity["pid"]:
        job.note("云运动进程未运行，token 抓取时可能混入同名标签")
    return identity


def _step_token(job: Job, adb: str, pid: str) -> str:
    if job.clear_log:
        _run([adb, "logcat", "-c"])
        job.note("已清空设备日志缓冲，避免抓到历史会话的旧 token")
    found = _scan(_dump(job, adb, pid))
    if found:
        return _accept(job, found)
    job.note("现有日志里还没有 token，请在手机上打开云运动并登录（最长等 3 分钟）")
    end = time.time() + TOKEN_WAIT_S
    while time.time() < end:
        if job.cancel.wait(3):
            return ""
        found = _scan(_dump(job, adb, pid))
        if found:
            return _accept(job, found)
    _die(job, "token", "3 分钟内未在调试日志中找到 token")
    return ""


def _accept(job: Job, token: str) -> str:
    job._set("token", "ok", f"{token[:6]}…{token[-4:]}（共 {len(token)} 字符）")
    return token


def _shell(adb: str, cmd: str) -> str:
    out, _, _ = _run([adb, "shell"] + cmd.split())
    return out.strip()


def _dump(job: Job, adb: str, pid: str) -> list[str]:
    args = [adb, "logcat", "-d", "-v", "threadtime"]
    if pid:
        args += ["--pid", pid]
    out, _, code = _run(args + ["-s", "snow:D"], timeout=30)
    if code:
        job.note("logcat 读取失败，稍后重试")
        return []
    return out.splitlines()


def _scan(lines: list[str]) -> str:
    """返回最新一条含 token 的日志里的 token；带时间戳则校验是否过旧。"""
    hit = ""
    for line in lines:
        m = TOKEN_RE.search(line)
        if not m:
            continue
        ts = TT_RE.match(line.strip())
        if ts and _too_old(ts.group(1)):
            continue
        hit = m.group(1)
    return hit


def _too_old(ts: str) -> bool:
    try:
        now = datetime.now()
        t = datetime.strptime(f"{now.year}-{ts.split('.')[0]}", "%Y-%m-%d %H:%M:%S")
        if t > now:
            t = t.replace(year=now.year - 1)
        return now - t > timedelta(minutes=10)
    except ValueError:
        return False


def _read_error(out: str, err: str, code: int) -> str:
    text = (out + err).lower()
    if "offline" in text or "unauthorized" in text or "no device" in text or code == -1:
        return "设备连接异常（offline/unauthorized/超时）"
    if "permission denied" in text:
        return "读取被拒：该文件属应用私有外部目录，部分系统版本需 root"
    if "no such file" in text or "not found" in text or "does not exist" in text:
        return "文件不存在：云运动可能未安装或未启动过"
    return (out + err).strip()[:80] or "未知错误"
