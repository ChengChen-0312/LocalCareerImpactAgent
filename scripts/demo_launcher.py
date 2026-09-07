"""Small local/private-demo launcher; never downloads models or cloudflared."""

from __future__ import annotations

import csv
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000"
RUN_DIR = ROOT / "var" / "run"
STATE = RUN_DIR / "remote-demo.json"
PYTHON = ROOT / ".venv" / "bin" / "python"
APP_COMMAND = [
    str(PYTHON), "-m", "uvicorn", "localcareerimpact.app.main:app",
    "--host", "127.0.0.1", "--port", "8000", "--workers", "1",
    "--timeout-graceful-shutdown", "20", "--no-access-log",
]


def prepare() -> dict[str, str]:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    from localcareerimpact.app.config import AppSettings, ConfigurationError

    settings = AppSettings.from_repository_root(ROOT)
    settings.validate_users_csv()
    settings.load_model_paths()
    with settings.users_csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    names = set()
    for number, row in enumerate(rows, start=2):
        if (
            set(row) != {"username", "password", "enabled"}
            or not row.get("username") or not row.get("password")
            or row.get("enabled") not in {"true", "false"}
            or row["username"] in names
        ):
            raise ConfigurationError(f"账户 CSV 第 {number} 行格式无效或用户名重复。")
        names.add(row["username"])
    if not any(row["enabled"] == "true" for row in rows):
        raise ConfigurationError("账户 CSV 至少需要一个 enabled=true 的账户。")
    for name in ("embedding", "asr"):
        if not (ROOT / "environments" / name / ".venv" / "bin" / "python").is_file():
            raise ConfigurationError(f"缺少 {name} 的本地 Python 环境。")
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 8000))
        except OSError as exc:
            raise RuntimeError("本地 8000 端口已占用；请先停止已有应用。") from exc
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("缺少 npm，无法构建网页。请先准备前端运行环境。")
        if not (ROOT / "web" / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=ROOT / "web", stdout=sys.stderr, check=True)
        subprocess.run([npm, "run", "build"], cwd=ROOT / "web", stdout=sys.stderr, check=True)
    environment = os.environ.copy()
    environment["LOCALCAREERIMPACT_REPOSITORY_ROOT"] = str(ROOT)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return environment


def identity(pid: int) -> dict[str, object] | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,lstart=,stat=,command="],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    fields = result.stdout.split(None, 7)
    if len(fields) != 8 or fields[6].startswith("Z"):
        return None
    return {
        "pid": pid, "uid": int(fields[0]),
        "started": " ".join(fields[1:6]), "command": fields[7].strip(),
    }


def write_state(records: dict[str, object]) -> None:
    temporary = RUN_DIR / "remote-demo.json.tmp"
    temporary.write_text(json.dumps({"root": str(ROOT), "processes": records}), encoding="utf-8")
    temporary.replace(STATE)


@contextmanager
def register_before_interrupt():
    """Finish recording a newly owned child before handling an operator stop."""
    pending = False
    previous = {item: signal.getsignal(item) for item in (signal.SIGINT, signal.SIGTERM)}

    def remember_stop(_signum: int, _frame: object) -> None:
        nonlocal pending
        pending = True

    try:
        for item in previous:
            signal.signal(item, remember_stop)
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)
    if pending:
        raise KeyboardInterrupt


def stop_records(records: dict[str, object]) -> bool:
    if not set(records) <= {"application", "tunnel"}:
        raise RuntimeError("进程记录含未知类型；没有发送终止信号。")
    complete = True
    for name in ("tunnel", "application"):
        if name not in records:
            continue
        record = records.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"pid", "uid", "started", "command"}
            or type(record.get("pid")) is not int or record["pid"] <= 1
        ):
            raise RuntimeError("进程记录损坏；没有发送终止信号。")
        pid = record["pid"]
        current = identity(pid)
        if current is None:
            records.pop(name)
            continue
        if current != record or current["uid"] != os.getuid():
            print(f"{name} 的进程身份不匹配，保留记录且不终止该 PID。", file=sys.stderr)
            complete = False
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            records.pop(name)
            continue
        deadline = time.monotonic() + (60 if name == "application" else 15)
        while identity(pid) == record and time.monotonic() < deadline:
            time.sleep(0.25)
        if identity(pid) == record:
            print(f"{name} 尚未退出，保留记录供稍后重新停止。", file=sys.stderr)
            complete = False
        else:
            records.pop(name)
    if records:
        write_state(records)
    else:
        STATE.unlink(missing_ok=True)
    return complete


def wait_ready(application: subprocess.Popen[bytes], lsof: str) -> None:
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        if application.poll() is not None:
            raise RuntimeError("应用启动失败；请查看 var/run/application.log。")
        try:
            with urlopen(URL + "/api/health", timeout=2) as response:
                status = json.load(response)
            if all(status.get(key) == "ready" for key in ("database", "qwen30b", "qwen4b")):
                listeners = subprocess.run(
                    [lsof, "-nP", "-iTCP:8000", "-sTCP:LISTEN", "-t"],
                    capture_output=True, text=True, check=False,
                )
                if set(listeners.stdout.split()) != {str(application.pid)}:
                    raise RuntimeError("本地监听进程与本次启动记录不符，拒绝连接 tunnel。")
                return
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise RuntimeError("等待本地模型就绪超时；请查看 var/run/application.log。")


def start_remote() -> None:
    cloudflared = shutil.which("cloudflared")
    lsof = shutil.which("lsof")
    if cloudflared is None or lsof is None:
        raise RuntimeError("远程演示需要预先安装 cloudflared，并能使用系统 lsof。")
    if not sys.stdin.isatty():
        raise RuntimeError("远程演示必须在交互终端手动确认；不接受管道或自动确认。")
    print(
        "即将把本地登录页面通过临时公网地址开放。仅用于私下演示。\n"
        "Quick Tunnel 不支持实时事件流；远程进度需普通请求刷新。\n"
        "确认启用请输入 ENABLE REMOTE，其他输入取消：",
        file=sys.stderr,
    )
    if input().strip() != "ENABLE REMOTE":
        raise RuntimeError("已取消远程演示。")

    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if STATE.exists():
        raise RuntimeError("已有远程进程记录；请先运行 stop-remote-demo.sh。")
    if any((Path.home() / ".cloudflared" / name).exists() for name in ("config.yaml", "config.yml")):
        raise RuntimeError("已有 cloudflared 配置可能影响 Quick Tunnel；请自行检查，启动器不改动该配置。")
    environment = prepare()
    records: dict[str, object] = {}
    children: list[subprocess.Popen[bytes]] = []
    try:
        with register_before_interrupt():
            with (RUN_DIR / "application.log").open("ab") as log:
                application = subprocess.Popen(
                    APP_COMMAND, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True,
                )
            children.append(application)
            record = identity(application.pid)
            if record is None:
                raise RuntimeError("应用在登记进程身份前退出。")
            records["application"] = record
            write_state(records)
        wait_ready(application, lsof)
        tunnel_log = RUN_DIR / "tunnel.log"
        with register_before_interrupt():
            with tunnel_log.open("wb") as log:
                tunnel = subprocess.Popen(
                    [cloudflared, "tunnel", "--no-autoupdate", "--url", URL],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            children.append(tunnel)
            record = identity(tunnel.pid)
            if record is None:
                raise RuntimeError("tunnel 在登记进程身份前退出。")
            records["tunnel"] = record
            write_state(records)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if tunnel.poll() is not None or application.poll() is not None:
                raise RuntimeError("远程演示启动失败；请查看 var/run/ 下的日志。")
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", tunnel_log.read_text(errors="replace"))
            if match:
                print(match.group(0), flush=True)
                print("用完后运行 scripts/stop-remote-demo.sh；地址和日志仅保存在本机。", file=sys.stderr)
                return
            time.sleep(1)
        raise RuntimeError("没有获得临时 tunnel 地址；启动器将清理本次进程。")
    except BaseException:
        # These are child handles from this invocation, not discovered process names.
        registered = {record["pid"] for record in records.values()}
        for child in children:
            if child.pid not in registered and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    record = identity(child.pid)
                    if record is not None:
                        records["application" if child is children[0] else "tunnel"] = record
        stop_records(records)
        for child in children:
            child.poll()
        raise


def main() -> int:
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in {"local", "remote", "stop"}:
        raise RuntimeError("请使用 run-local.sh、run-remote-demo.sh 或 stop-remote-demo.sh。")
    action = sys.argv[1]
    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUN_DIR / "remote-demo.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有本地应用或启动/停止操作；请先结束该操作。") from exc
        if action == "local":
            if STATE.exists():
                raise RuntimeError("已有远程进程记录；请先运行 stop-remote-demo.sh。")
            environment = prepare()
            print(URL, flush=True)
            os.chdir(ROOT)
            os.set_inheritable(lock.fileno(), True)
            os.execve(PYTHON, APP_COMMAND, environment)
        elif action == "remote":
            start_remote()
        elif STATE.exists():
            if STATE.stat().st_uid != os.getuid() or STATE.stat().st_mode & 0o077:
                raise RuntimeError("进程记录的所有者或权限不符；没有发送终止信号。")
            state = json.loads(STATE.read_text(encoding="utf-8"))
            if state.get("root") != str(ROOT) or not isinstance(state.get("processes"), dict):
                raise RuntimeError("进程记录不属于当前项目；没有发送终止信号。")
            if not stop_records(state["processes"]):
                return 1
            print("远程演示已停止，本次应用和 tunnel 的进程记录已清理。")
        else:
            print("没有本启动器登记的远程演示进程。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"启动器：{exc}", file=sys.stderr)
        raise SystemExit(1)
