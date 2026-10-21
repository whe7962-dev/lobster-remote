"""腾讯官方 tmeet 的窄封装。仅用于授权状态、创建、查询和取消本 Demo 创建的会议。"""
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def executable():
    system, machine = platform.system(), platform.machine().lower()
    name = {"Darwin": "tmeet-macOS-AppleSilicon" if machine in ("arm64", "aarch64") else "tmeet-macOS-Intel",
            "Windows": "tmeet-Windows-x86_64.exe"}.get(system)
    if name:
        for folder in (ROOT / "bin", ROOT / "tools/node_modules/@tencentcloud/tmeet/dist"):
            if (folder / name).is_file():
                return str(folder / name)
    return shutil.which("tmeet")


def environment():
    env = os.environ.copy()
    folder = ROOT / ".local-auth"
    folder.mkdir(exist_ok=True)
    env["TMEET_CLI_CONFIG_DIR"] = str(folder / "config")
    env["TMEET_CLI_DATA_DIR"] = str(folder / "data")
    return env


def run(args, timeout=45):
    binary = executable()
    if not binary:
        raise FileNotFoundError("未安装腾讯官方 tmeet 工具，请运行安装脚本。")
    response = subprocess.run([binary] + args, env=environment(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    try:
        data = json.loads(response.stdout)
    except ValueError:
        data = {"message": (response.stderr or response.stdout)[:400]}
    if response.returncode:
        raise RuntimeError(json.dumps(data, ensure_ascii=False)[:500])
    return data


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def meeting_info(data):
    return next((obj for obj in objects(data) if obj.get("meeting_code") and obj.get("meeting_id")), None)


if __name__ == "__main__":
    import sys
    binary = executable()
    if not binary:
        print("请先运行安装脚本安装腾讯会议工具。")
        raise SystemExit(1)
    args = ["auth", "login"] if len(sys.argv) == 1 else sys.argv[1:]
    raise SystemExit(subprocess.call([binary] + args, env=environment()))
