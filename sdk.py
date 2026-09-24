# -*- coding: utf-8 -*-
"""sdk.py —— 二次开发入口（无需 PyQt6，不依赖 GUI）

    import asyncio, sdk

    async def main():
        c = await sdk.login(user="学号", password="密码", school_code="106")
        print(c.real_name, c.run_areas)
        await c.run(distance=1200, fast=True)          # 快速：同步跑完
        r = await c.run(distance=1200)                  # 慢速：先拿 task_id
        print(await c.wait(r["task_id"]))
        print(await c.records())
        await sdk.close()

    asyncio.run(main())

约定：
- 业务错误只抛 sdk.AppError（`.detail` 为中文原因）；网络异常仍来自 requests。
- 慢速跑步的任务表活在当前进程内，必须与提交时同一事件循环才能 wait()。
- 会话文件与 cli.py 同为 data/cli_session.json 格式，可互相接管。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import pathlib
import time
from typing import Any, Iterable, Sequence

ROOT = pathlib.Path(__file__).resolve().parent


@contextlib.contextmanager
def _silent_import():
    """屏蔽 services 在 import 期的两处副作用：改写 root 日志、在包目录建 logs/。

    被 import 的模块在顶层执行 basicConfig(force=True)，直接引用会清掉调用方自己的
    日志配置；其预建的日志目录固定在包目录，只读安装位置会抛 PermissionError。
    """
    real_basic_config, real_file_handler, real_mkdir = (
        logging.basicConfig, logging.FileHandler, pathlib.Path.mkdir,
    )
    try:
        logging.basicConfig = lambda **kw: None
        logging.FileHandler = lambda *a, **k: logging.NullHandler()
        pathlib.Path.mkdir = lambda self, *a, **k: None
        yield
    finally:
        logging.basicConfig, logging.FileHandler = real_basic_config, real_file_handler
        pathlib.Path.mkdir = real_mkdir


with _silent_import():
    import services
    import upstream

AppError = services.AppError
LoginRequest = services.LoginRequest
YunRunRequest = services.YunRunRequest
HaveYdInfoRequest = services.HaveYdInfoRequest
TrackUploadRequest = services.TrackUploadRequest
RunRecordDetailRequest = services.RunRecordDetailRequest

SessionData = dict[str, Any]

__all__ = [
    "AppError", "Client", "HaveYdInfoRequest", "LoginRequest", "RunRecordDetailRequest",
    "TrackUploadRequest", "YunRunRequest", "close", "devices", "login", "schools",
    "session_file", "session_from_file",
]


async def close() -> None:
    """释放上游连接池，进程退出前调用。"""
    await services.shutdown()


async def schools() -> list[dict]:
    """全部可用学校：[{"schoolName": ..., "schoolCode": ...}]"""
    return (await services.api_list_schools())["schools"]


def devices() -> dict[str, str]:
    """生成一组随机设备身份，供 login(uuid=, device_name=) 固定复用。"""
    return {"uuid": services.gen_device_id(), "device_name": upstream.have_device_name()}


def session_file() -> pathlib.Path:
    return ROOT / "data" / "cli_session.json"


def session_from_file(path: pathlib.Path | str | None = None) -> SessionData:
    """读取 cli.py 留下的登录态。"""
    p = pathlib.Path(path) if path else session_file()
    if not p.is_file():
        raise AppError(f"本机无会话文件 {p}，请先执行 python cli.py login --user <学号>")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not data.get("token"):
        raise AppError("会话文件里没有 token")
    return data


class Client:
    """持有一份登录态，免去每次重复传 token/uuid/device_name。"""

    def __init__(self, session: SessionData):
        self.user: str = session["user"]
        self.school_code: str = session["school_code"]
        self.uuid: str = session["uuid"]
        self.device_name: str = session["device_name"]
        self.token: str = session["token"]
        self.real_name: str = session.get("realName") or ""
        self.school: str = session.get("school") or ""
        self.run_areas: list[str] = [
            a if isinstance(a, str) else a.get("raName", "")
            for a in (session.get("run_areas") or [])
        ]
        self._raw = dict(session)

    @classmethod
    def from_session_file(cls, path: pathlib.Path | str | None = None) -> "Client":
        return cls(session_from_file(path))

    def to_dict(self) -> SessionData:
        out = dict(self._raw)
        out["run_areas"] = self.run_areas
        return out

    def save_session(self, path: pathlib.Path | str | None = None) -> pathlib.Path:
        """存成 cli.py 可直接复用的 data/cli_session.json。"""
        p = pathlib.Path(path) if path else session_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @property
    def _ids(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in ("school_code", "uuid", "device_name", "token")}

    async def run(self, distance: int, ra_name: str = "", fast: bool = False,
                  face_jpg: str | pathlib.Path | None = None,
                  pace: Sequence[float] | None = None,
                  cadence: Sequence[float] | None = None) -> dict:
        """提交一次跑步。fast=True 同步跑完，否则返回 {"task_id": ...} 需再 wait()。"""
        named = [a for a in self.run_areas if a]
        req = YunRunRequest(
            user=self.user, **self._ids,
            target_distance=distance,
            ra_name=ra_name or (named[0] if named else ""),
            fast=fast,
            face_base64=_read_jpg_base64(face_jpg) if face_jpg else None,
            pace_range_min_per_km=tuple(pace) if pace else None,
            cadence_range_spm=tuple(cadence) if cadence else None,
        )
        return await services.api_yun_run(req)

    async def wait(self, task_id: str, timeout: float = 1800, poll: float = 5) -> Any:
        """轮询慢速任务到完成，返回上游结果。"""
        deadline = time.monotonic() + timeout
        while True:
            task = await services.api_get_task(task_id)
            if task["status"] == "done":
                return task.get("result")
            if task["status"] == "failed":
                raise AppError(f"跑步失败: {task.get('result')}")
            if time.monotonic() > deadline:
                raise AppError(f"任务 {task_id} 等待超时，状态 {task['status']}")
            await asyncio.sleep(poll)

    async def run_and_wait(self, **kwargs) -> Any:
        """慢速模式的便捷组合：提交后原地等完。"""
        kwargs.pop("fast", None)
        return await self.wait((await self.run(fast=False, **kwargs))["task_id"])

    async def records(self) -> dict:
        """本学期运动记录（上游原始按月分组结构）。"""
        return await services.api_have_yd_info(HaveYdInfoRequest(**self._ids))

    async def sport_info(self) -> dict:
        """运动概况与达标统计。"""
        return await services.api_sport_info(HaveYdInfoRequest(**self._ids))

    async def record_detail(self, record_id: str, table_name: str = "") -> dict:
        """单条记录详情，table_name 留空则自动取当前学期。"""
        return await services.api_run_record_detail(RunRecordDetailRequest(
            **self._ids, record_id=record_id, table_name=table_name or None,
        ))

    async def save_track(self, ra_name: str, points: Iterable[Sequence[float]]) -> dict:
        """把跑区轨迹存到本机 data/tracks/<school_code>/<ra_name>.json。"""
        return await services.api_track_upload(TrackUploadRequest(
            school_code=self.school_code, ra_name=ra_name,
            track=[list(p) for p in points],
        ))


def _read_jpg_base64(path: str | pathlib.Path) -> str:
    p = pathlib.Path(path)
    if not p.is_file():
        raise AppError(f"照片不存在: {p}")
    return base64.b64encode(p.read_bytes()).decode()


async def login(user: str, password: str, school_code: str = "106",
                uuid: str | None = None, device_name: str | None = None) -> Client:
    """登录并返回带会话的 Client；不传 uuid/device_name 则自动生成新设备身份。"""
    data = await services.api_login(LoginRequest(
        user=user, password=password, school_code=school_code,
        uuid=uuid, device_name=device_name,
    ))
    return Client(data)


if __name__ == "__main__":
    import sys

    if not session_file().is_file():
        sys.exit("本机无 data/cli_session.json，请先: python cli.py login --user <学号>")
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")

    async def _demo():
        print("学校数:", len(await schools()))
        c = Client.from_session_file()
        print(f"已接管会话: {c.real_name or c.user} @ {c.school} 设备={c.device_name}")
        print("跑区:", "、".join(c.run_areas) or "（无）")
        print("记录:", json.dumps(await c.records(), ensure_ascii=False)[:200])
        await close()

    try:
        asyncio.run(_demo())
    except AppError as e:
        print(f"错误: {e.detail}", file=sys.stderr)
        sys.exit(1)
