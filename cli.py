# -*- coding: utf-8 -*-
"""命令行入口：直接调用业务层，无需 GUI。

用法：
    python cli.py schools [--find 关键词]
    python cli.py login --user <学号> [--school-code 106]
    python cli.py run --distance 1200 [--ra-name 名称] [--fast] [--face auto|照片.jpg]
    python cli.py records

登录态（token/uuid/device_name 等）保存在 data/cli_session.json。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import logging
import sys
from pathlib import Path

import api
import services
from services import AppError

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / "data" / "cli_session.json"
FACES_DIR = ROOT / "data" / "faces"


def load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session(data: dict) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def require_session() -> dict:
    s = load_session()
    if not s.get("token"):
        raise AppError("本机无登录态，请先执行: python cli.py login --user <学号>")
    return s


def next_face_file() -> Path:
    """从 data/faces 轮询取下一张照片。"""
    files = sorted(FACES_DIR.glob("*.jpg"), key=lambda x: x.name)
    if not files:
        raise AppError(f"{FACES_DIR} 下没有人脸照片")
    cursor = FACES_DIR / ".cursor"
    try:
        idx = int(cursor.read_text())
    except Exception:
        idx = 0
    cursor.write_text(str(idx + 1))
    return files[idx % len(files)]


def parse_range(text: str) -> tuple[float, float]:
    lo, _, hi = text.partition(",")
    return float(lo), float(hi)


async def cmd_schools(args) -> None:
    data = await api.schools()
    schools = data["schools"]
    if args.find:
        key = args.find
        schools = [s for s in schools if key in s["schoolName"] or key == s["schoolCode"]]
    for s in schools:
        print(f"{s['schoolCode']:>6}  {s['schoolName']}")
    print(f"共 {len(schools)} 所")


async def cmd_login(args) -> None:
    old = load_session()
    data = await api.login(api.LoginRequest(
        user=args.user,
        password=args.password,
        school_code=args.school_code,
        uuid=old.get("uuid"),            # 沿用原设备，避免频繁换设备
        device_name=old.get("device_name"),
    ))
    session = {k: data[k] for k in ("user", "school_code", "uuid", "device_name", "token")}
    session["run_areas"] = [a.get("raName", "") for a in data.get("run_areas") or []]
    save_session(session)
    print(f"登录成功: {data.get('realName') or args.user} @ {data.get('school', '')}")
    print(f"设备: {session['device_name']}  学校编码: {session['school_code']}")
    print("跑区:", "、".join(n for n in session["run_areas"] if n) or "（未获取到）")


async def cmd_run(args) -> None:
    s = require_session()
    ra_name = args.ra_name or (s.get("run_areas") or [""])[0]

    face_base64 = None
    if args.face:
        path = next_face_file() if args.face == "auto" else Path(args.face)
        if not path.is_file():
            raise AppError(f"照片不存在: {path}")
        face_base64 = base64.b64encode(path.read_bytes()).decode()

    req = services.YunRunRequest(
        user=s["user"],
        school_code=s["school_code"],
        uuid=s["uuid"],
        device_name=s["device_name"],
        token=s["token"],
        target_distance=args.distance,
        ra_name=ra_name,
        fast=args.fast,
        face_base64=face_base64,
        pace_range_min_per_km=parse_range(args.pace) if args.pace else None,
        cadence_range_spm=parse_range(args.cadence) if args.cadence else None,
    )
    result = await services.api_yun_run(req)

    if args.fast:
        print("完成:", result.get("result"))
        return

    task_id = result["task_id"]
    last = ""
    while True:
        task = await services.api_get_task(task_id)
        status = task["status"]
        if status != last:
            print(f"任务 {task_id[:8]}… 状态: {status}", flush=True)
            last = status
        if status == "done":
            print("完成:", task.get("result"))
            return
        if status == "failed":
            raise AppError(f"跑步失败: {task.get('result')}")
        await asyncio.sleep(5)


async def cmd_records(args) -> None:
    s = require_session()
    data = await services.api_have_yd_info(services.HaveYdInfoRequest(
        school_code=s["school_code"], uuid=s["uuid"],
        device_name=s["device_name"], token=s["token"],
    ))
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="云运动 · 命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schools", help="列出可用学校")
    p.add_argument("--find", default="", help="按学校名称或编码过滤")
    p.set_defaults(func=cmd_schools)

    p = sub.add_parser("login", help="登录并保存本机登录态")
    p.add_argument("--user", required=True, help="学号")
    p.add_argument("--password", help="密码（不传则交互式输入）")
    p.add_argument("--school-code", default="106")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("run", help="提交一次跑步")
    p.add_argument("--distance", type=int, required=True, help="目标距离（米，≤4500）")
    p.add_argument("--ra-name", default="", help="跑区名称，默认取登录时首个跑区")
    p.add_argument("--fast", action="store_true", help="快速模式：同步完成，不真实等待")
    p.add_argument("--face", default="", help="刷脸任务：auto 轮询 data/faces，或指定 jpg 路径")
    p.add_argument("--pace", default="", help="配速区间 min/km，如 5.5,6.7")
    p.add_argument("--cadence", default="", help="步频区间 spm，如 145,175")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("records", help="查看本学期运动记录")
    p.set_defaults(func=cmd_records)
    return parser


async def _main(args) -> None:
    await api.startup()
    try:
        await args.func(args)
    finally:
        await api.shutdown()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.command == "login" and not args.password:
        args.password = getpass.getpass("密码: ")
    for handler in logging.getLogger().handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.WARNING)

    try:
        asyncio.run(_main(args))
    except AppError as e:
        print(f"错误: {e.detail}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
