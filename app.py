"""龙虾脑：同一局域网内的单用户 Demo。仅使用标准库运行网页/队列。"""
import argparse
import hmac
import json
import mimetypes
import os
import platform
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
TERMINAL = {"succeeded", "succeeded_manual", "failed", "blocked", "cancelled"}
ACTIVE = {"received", "queued", "running", "needs_input"}


def parse_intent(text):
    """有意使用透明、有限的规则；没有伪装为大模型。"""
    text = text.strip()
    if not text or len(text) > 300:
        return {"kind": "unknown", "error": "请输入 1–300 个字。", "code": "invalid_input"}
    close_text = re.sub(r"[\s，,。.!！]", "", text)
    if re.fullmatch(r"(?:请)?(?:帮我|给我)?(?:关闭|关掉|关了)(?:当前|这个|正在播放的)?(?:电影|视频|播放)?(?:页面|界面|窗口)?(?:吧)?", close_text) or close_text in ("停止播放", "请停止播放", "帮我停止播放"):
        return {"kind": "video_close", "label": "关闭当前视频页面"}
    if any(word in text for word in ("关闭", "关掉", "关了", "结束", "取消", "停止播放")):
        return {"kind": "unknown", "error": "输入“关闭”或“关闭播放界面”可关闭本 Demo 最近一次播放页面。会议结束仍需在对应任务点击“结束会议（电脑确认）”，再由你在客户端全员结束。其他关闭对象暂不支持。", "code": "use_task_action"}
    if "会议" in text and any(x in text for x in ("电影", "预告片")):
        return {"kind": "unknown", "error": "请一次只提交一个任务：电影或会议。", "code": "multiple_intents"}
    if "会议" in text:
        if any(x in text for x in ("加入", "入会", "会议码", "会议号")):
            groups = re.findall(r"(?<!\d)\d[\d -]*\d|(?<!\d)\d(?!\d)", text)
            digits = re.sub(r"\D", "", groups[0]) if groups else ""
            if len(digits) not in (9, 10):
                return {"kind": "meeting_join", "error": "会议号应为 9 位普通会议号或 10 位个人会议号。1234 可能是密码，请向主持人索取完整会议号。", "code": "invalid_meeting_code"}
            return {"kind": "meeting_join", "meeting_code": digits, "label": "加入腾讯会议"}
        if any(x in text for x in ("创建", "发起", "新建", "开个", "开一个")):
            return {"kind": "meeting_create", "label": "创建腾讯会议"}
        if any(x in text for x in ("打开", "启动", "开启")):
            return {"kind": "meeting_open", "label": "打开腾讯会议"}
        return {"kind": "unknown", "error": "请说明要打开、加入还是创建腾讯会议。", "code": "missing_action"}
    if any(x in text for x in ("电影", "视频", "预告", "播放", "《")):
        quoted = re.search(r"[《“\"]([^》”\"]{1,60})[》”\"]", text)
        if quoted:
            title = quoted.group(1).strip()
        else:
            title = re.split(r"[，,。；;]|并|然后", text)[0]
            title = re.sub(r"^(请|帮我|给我|我想|我要|搜一下|搜索|搜|找一下|找|播放|看一下|看)+", "", title)
            title = re.sub(r"^(一部|一个|一段|部|个|段)?(热门|热映|最近热门|最新)?(电影|视频)?[：: ]*", "", title).strip()
            title = re.sub(r"(的)?(相关视频|官方预告片|预告片|预告|视频)$", "", title).strip()
        if not title:
            return {"kind": "movie", "error": "请指定电影名称，例如：播放《流浪地球2》的预告片。", "code": "missing_movie_title"}
        keyword = title + " 电影 官方预告片"
        return {"kind": "movie", "movie_title": title, "keyword": keyword, "label": "搜索并播放电影相关视频", "selection": "优先标题匹配的预告片；不宣称版权来源或实时热度排名"}
    return {"kind": "unknown", "error": "本 Demo 只支持电影相关视频，以及打开、加入或创建腾讯会议。", "code": "unsupported_intent"}


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "tasks.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, created REAL, payload TEXT)")
        for task in self.all():
            if task["status"] in ACTIVE:
                self.update(task["id"], "failed", "程序曾退出，任务没有自动重放。请重新提交。", error_code="server_restarted")
            if task.get("closure", {}).get("status") in ("queued", "running"):
                self.set_closure(task["id"], "unknown", "程序重启，关闭结果尚未确认；没有自动重复发送关闭请求。")
            if task.get("meeting", {}).get("status") == "checking":
                self.set_meeting(task["id"], "unknown", "查询时程序重启。请点击“检查会议状态”重新核验；没有重放入会操作。")

    def connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def all(self):
        with self.lock, self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM tasks ORDER BY created DESC")]

    def get(self, task_id):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def insert(self, text, intent, mode, request_id):
        now = time.time()
        task = dict(id=uuid.uuid4().hex[:12], request_id=request_id, text=text, intent=intent,
                    mode=mode, status="received", created_at=now, updated_at=now, started_at=None,
                    finished_at=None, message="指令已接收，等待执行。", error_code=None, result={},
                    events=[dict(at=now, status="received", message="指令已接收。")])
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?)", (task["id"], now, json.dumps(task, ensure_ascii=False)))
        return task

    def update(self, task_id, status, message, **fields):
        with self.lock:
            task = self.get(task_id)
            if not task or task["status"] in TERMINAL:
                return task  # 取消/超时后，迟到的执行回执不能覆写终态。
            now = time.time()
            task.update(status=status, message=message, updated_at=now, **fields)
            if status == "running" and task["started_at"] is None:
                task["started_at"] = now
            if status in TERMINAL:
                task["finished_at"] = now
            task["events"].append(dict(at=now, status=status, message=message))
            task["events"] = task["events"][-40:]
            with self.connect() as db:
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))
            return task

    def set_closure(self, task_id, status, message, **fields):
        """资源关闭与原任务结果独立，不能覆盖原成功/失败证据。"""
        with self.lock:
            task = self.get(task_id)
            if not task:
                return None
            now = time.time()
            old = task.get("closure", {})
            task["closure"] = dict(old, status=status, message=message, updated_at=now, **fields)
            task["closure"]["events"] = (old.get("events", []) + [dict(at=now, status=status, message=message)])[-20:]
            with self.connect() as db:
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))
            return task

    def set_meeting(self, task_id, status, message, **fields):
        with self.lock:
            task = self.get(task_id)
            if not task:
                return None
            now = time.time()
            old = task.get("meeting", {})
            task["meeting"] = dict(old, status=status, message=message, updated_at=now, **fields)
            if old.get("status") != status or old.get("message") != message:
                task["meeting"]["events"] = (old.get("events", []) + [dict(at=now, status=status, message=message)])[-20:]
            with self.connect() as db:
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))
            return task


def metrics(tasks):
    # 统计整个本地库；预览与真实执行绝不混合。
    live = [t for t in tasks if t["mode"] in ("windows", "mac")]
    attempted = [t for t in live if t["started_at"] is not None]
    auto = [t for t in attempted if t["status"] == "succeeded"]
    manual = [t for t in attempted if t["status"] == "succeeded_manual"]
    resolved = [t for t in attempted if t["status"] in TERMINAL]
    durations = [t["finished_at"] - t["started_at"] for t in auto]
    errors = {}
    for t in live:
        if t.get("error_code"):
            errors[t["error_code"]] = errors.get(t["error_code"], 0) + 1
    return dict(total=len(tasks), live_requests=len(live), preview_requests=len(tasks)-len(live),
                attempted=len(attempted), resolved=len(resolved), auto_success=len(auto), manual_success=len(manual),
                waiting=sum(t["status"] == "needs_input" for t in live),
                validation_rejected=sum(t["started_at"] is None and t["status"] == "failed" for t in live),
                auto_rate=round(100 * len(auto) / len(resolved), 1) if resolved else None,
                avg_seconds=round(sum(durations) / len(durations), 1) if durations else None,
                errors=errors,
                close_requested=sum(bool(t.get("closure")) for t in live),
                close_succeeded=sum(t.get("closure", {}).get("status") == "succeeded" and not t.get("closure", {}).get("manual_action") for t in live),
                meeting_joined=sum(bool(t.get("meeting", {}).get("joined_at")) for t in live),
                meeting_ended=sum(bool(t.get("meeting", {}).get("ended_at")) for t in live),
                meeting_cancelled=sum(t.get("closure", {}).get("verification") == "official_cancel_receipt" for t in live),
                close_attention=sum(t.get("closure", {}).get("status") in ("failed", "unknown") for t in live))


class Application:
    def __init__(self, data_dir, preview=False):
        self.store = Store(data_dir)
        self.preview = preview or platform.system() not in ("Windows", "Darwin")
        self.mode = "preview" if self.preview else ("mac" if platform.system() == "Darwin" else "windows")
        self.pin = str(secrets.randbelow(900000) + 100000)
        self.token = secrets.token_urlsafe(32)
        self.pair_attempts = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.rechecks = set()
        self.meeting_ops = {}
        self.worker = None
        self.executor = None
        if not self.preview:
            from executor import DesktopExecutor
            self.executor = DesktopExecutor(self.store, self.stop)
            self.worker = threading.Thread(target=self.work, daemon=True, name="desktop-executor")
            self.worker.start()

    def work(self):
        while not self.stop.is_set():
            try:
                tasks = self.store.all()
                for task in tasks:
                    if task["status"] == "needs_input" and time.time() - task["updated_at"] > 600:
                        self.store.update(task["id"], "failed", "等待人工处理超过 10 分钟，请修复后重新提交。", error_code="human_timeout")
                with self.lock:
                    closing = next((t for t in tasks if t.get("closure", {}).get("status") == "queued"), None)
                    operation = next(iter(self.meeting_ops.items()), None) if not closing else None
                    if operation:
                        del self.meeting_ops[operation[0]]
                    check = next(iter(self.rechecks), None)
                    if check and not closing and not operation:
                        self.rechecks.remove(check)
                if closing:
                    self.executor.close_resource(closing)
                elif operation:
                    self.executor.meeting_action(self.store.get(operation[0]), operation[1])
                elif check:
                    task = self.store.get(check)
                    if task and task["status"] == "needs_input":
                        self.executor.recheck(task)
                else:
                    queued = [t for t in tasks if t["status"] == "queued"]
                    if queued:
                        self.executor.execute(queued[-1])
                    else:
                        self.executor.poll_meetings()
            except Exception as exc:
                # 未知错误必须有真实失败回执，而不是使线程悄悄消失。
                for task in self.store.all():
                    if task["status"] == "running":
                        self.store.update(task["id"], "failed", "执行器异常：" + str(exc)[:240], error_code="executor_error")
            self.stop.wait(0.5)
        if self.executor:
            self.executor.close()

    def state(self, local=False):
        tasks = self.store.all()
        return dict(device=dict(name=socket.gethostname(), os=platform.system(), mode=self.mode,
                                online=bool(self.worker and self.worker.is_alive()), local_operator=local,
                                reason="预览模式：展示网页和真实接口回执，不执行桌面操作。" if self.preview else "执行端在本机运行，请保持电脑解锁。"),
                    tasks=tasks[:100], metrics=metrics(tasks), server_time=time.time())

    def submit(self, text, request_id):
        with self.lock:
            for t in self.store.all():
                if request_id and t["request_id"] == request_id:
                    return t
                if request_id and request_id in t.get("closure", {}).get("command_requests", []):
                    return t
            intent = parse_intent(text)
            if intent["kind"] == "video_close" and not self.preview:
                # 与按钮走同一个真实关闭队列。仅定位最近执行的视频任务，
                # 不因新建了会议而把“关闭”解释成结束会议或退出整个浏览器。
                target = next((t for t in self.store.all() if t["mode"] != "preview" and t["intent"]["kind"] == "movie" and t["started_at"]), None)
                if target is None:
                    raise ValueError("没有可关闭的视频页面。请先通过本 Demo 播放视频；没有操作电脑上的其他窗口。")
                with self.store.lock:
                    target = self.request_close(target["id"])
                    closure = target["closure"]
                    requests = closure.get("command_requests", []) + ([request_id] if request_id else [])
                    return self.store.set_closure(target["id"], closure["status"], closure["message"], command=text,
                        command_requests=requests, source="natural_language")
            if not self.preview and any(t["status"] in ACTIVE for t in self.store.all()):
                raise ValueError("已有一个任务在执行或等待处理。请先处理或取消它。")
            t = self.store.insert(text, intent, self.mode, request_id)
            if "error" in intent:
                return self.store.update(t["id"], "failed", intent["error"], error_code=intent["code"])
            if self.preview:
                return self.store.update(t["id"], "blocked", "当前是预览模式。已真实接收指令，但没有执行桌面操作。请用正常启动脚本在 Mac 或 Windows 上运行。", error_code="preview_only")
            if not self.worker or not self.worker.is_alive():
                return self.store.update(t["id"], "failed", "执行线程已离线，请重启程序。", error_code="worker_offline")
            return self.store.update(t["id"], "queued", "已加入本机执行队列。")

    def request_close(self, task_id):
        with self.lock:
            task = self.store.get(task_id)
            if self.preview or not task or task["mode"] == "preview":
                raise ValueError("预览任务没有可关闭的真实资源。")
            kind = task["intent"]["kind"]
            if kind != "movie" and not (kind == "meeting_create" and task["result"].get("meeting_id") and task["result"].get("verification") == "official_api"):
                raise ValueError("只能关闭本 Demo 打开的视频或通过官方接口创建的会议。")
            if not task["started_at"]:
                raise ValueError("任务尚未执行，没有可关闭的资源。")
            if task.get("closure", {}).get("status") in ("queued", "running", "succeeded", "unknown"):
                return task  # 重复点击不重复调用外部接口；未知结果需人工核对。
            if not self.worker or not self.worker.is_alive():
                raise ValueError("执行端离线，请重启程序。")
            if task["status"] in ACTIVE:
                if kind != "movie":
                    raise ValueError("请等待会议创建回执后再关闭。")
                self.store.update(task_id, "cancelled", "已请求停止播放任务，随后关闭本任务页面。", error_code="user_cancelled")
            self.rechecks.discard(task_id)
            return self.store.set_closure(task_id, "queued", "关闭请求已接收，等待电脑处理。", manual_action=False, action="cancel_reservation" if kind == "meeting_create" else "close_video")

    def request_meeting_action(self, task_id, action):
        with self.lock:
            task = self.store.get(task_id)
            if self.preview or not task or task["mode"] == "preview" or task["intent"]["kind"] != "meeting_create" or task["result"].get("verification") != "official_api" or not task["result"].get("meeting_id"):
                raise ValueError("该操作仅支持本 Demo 真实创建的会议。")
            if not self.worker or not self.worker.is_alive():
                raise ValueError("执行端离线，请重启程序。")
            if task_id in self.meeting_ops:
                return task
            if action != "meeting-check" and (task.get("meeting", {}).get("status") in ("ended", "cancelled") or task.get("closure", {}).get("status") == "succeeded"):
                raise ValueError("此会议已结束或取消，请创建新会议。")
            self.meeting_ops[task_id] = action
            return self.store.set_meeting(task_id, "checking", "正在查询腾讯会议实际状态。", requested_action=action)


class Handler(BaseHTTPRequestHandler):
    server_version = "LobsterDemo/1.0"

    @property
    def app(self):
        return self.server.app

    @property
    def local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def log_message(self, fmt, *args):
        pass  # 避免在终端记录配对凭据或完整用户指令。

    def send(self, status, data, content_type="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(token, self.app.token)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send(200, dict(ok=True, mode=self.app.mode))
        if path.startswith("/api/"):
            if not self.authorized():
                return self.send(401, dict(error="请先输入电脑终端上的配对码。"))
            if path == "/api/state":
                return self.send(200, self.app.state(self.local))
            if path == "/api/export":
                return self.send(200, dict(exported_at=time.time(), metrics=metrics(self.app.store.all()), tasks=self.app.store.all()))
            return self.send(404, dict(error="接口不存在。"))
        allowed = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/guide": "guide.html", "/plan": "plan.html", "/recording": "recording.html", "/verification": "verification.html", "/guide.html": "guide.html", "/plan.html": "plan.html", "/recording.html": "recording.html", "/verification.html": "verification.html"}
        name = allowed.get(path)
        if not name or not (ROOT / "static" / name).exists():
            return self.send(404, b"Not found", "text/plain")
        file = ROOT / "static" / name
        mime = mimetypes.guess_type(file.name)[0] or "text/plain"
        self.send(200, file.read_bytes(), mime + "; charset=utf-8")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 8192:
                return self.send(400, dict(error="请求大小不正确。"))
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, dict(error="需要 JSON 请求。"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self.send(400, dict(error="请求格式不正确。"))
            path = urlparse(self.path).path
            if path == "/api/pair":
                ip = self.client_address[0]
                with self.app.lock:
                    attempts = [t for t in self.app.pair_attempts.get(ip, []) if time.time() - t < 60]
                    if len(attempts) >= 5:
                        return self.send(429, dict(error="尝试太频繁，请 1 分钟后重试。"))
                    pin = str(body.get("pin", ""))
                    if not hmac.compare_digest(pin, self.app.pin):
                        attempts.append(time.time())
                        self.app.pair_attempts[ip] = attempts
                        return self.send(403, dict(error="配对码不正确，请看运行程序的电脑终端。"))
                    self.app.pair_attempts[ip] = []
                return self.send(200, dict(token=self.app.token))
            if not self.authorized():
                return self.send(401, dict(error="配对已失效，请重新配对。"))
            if path == "/api/tasks":
                command = body.get("text")
                request_id = body.get("request_id", "")
                if not isinstance(command, str) or not isinstance(request_id, str) or len(request_id) > 100:
                    return self.send(400, dict(error="指令格式不正确。"))
                return self.send(201, self.app.submit(command, request_id))
            match = re.fullmatch(r"/api/tasks/([a-f0-9]{12})/(cancel|recheck|confirm|close|meeting-join|meeting-end|meeting-check)", path)
            if not match:
                return self.send(404, dict(error="接口不存在。"))
            task_id, action = match.groups()
            task = self.app.store.get(task_id)
            if not task:
                return self.send(404, dict(error="任务不存在。"))
            if action == "close":
                return self.send(200, self.app.request_close(task_id))
            if action.startswith("meeting-"):
                return self.send(200, self.app.request_meeting_action(task_id, action))
            if task["status"] in TERMINAL:
                return self.send(409, dict(error="任务已结束，不能修改结果。"))
            if action == "cancel":
                if task["status"] == "running" and task["intent"]["kind"] == "meeting_create":
                    return self.send(409, dict(error="会议创建请求正在处理，暂不能取消。请等待官方回执，避免结果不明。"))
                task = self.app.store.update(task_id, "cancelled", "已停止后续调度。已经打开的应用/视频不会自动关闭，请在电脑上处理。", error_code="user_cancelled")
            elif action == "recheck":
                if task["status"] != "needs_input" or self.app.preview:
                    return self.send(409, dict(error="当前任务不支持重新检查。"))
                with self.app.lock:
                    self.app.rechecks.add(task_id)
            else:
                if not self.local or self.app.preview or task["status"] != "needs_input":
                    return self.send(403, dict(error="只有执行电脑的 localhost 页面，才能填写人工结果。"))
                success = body.get("success")
                note = body.get("note", "")
                if not isinstance(success, bool) or not isinstance(note, str) or not 5 <= len(note.strip()) <= 300:
                    return self.send(400, dict(error="请填写 5–300 字的实际结果，例如“会议已进入，会议号…”。"))
                task = self.app.store.update(task_id, "succeeded_manual" if success else "failed",
                                             "电脑操作者报告：" + note.strip(),
                                             result={**task["result"], "verification": "human_report", "note": note.strip()},
                                             error_code=None if success else "human_reported_failure")
            return self.send(200, task)
        except ValueError as exc:
            self.send(400, dict(error=str(exc)[:240]))
        except Exception:
            self.send(500, dict(error="服务暂时出错，请查看电脑端并重试。"))


def lan_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not item[4][0].startswith("127."):
                addresses.add(item[4][0])
    except OSError:
        pass
    # UDP connect 只选择路由，不发送应用数据。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(addresses)


def main():
    parser = argparse.ArgumentParser(description="龙虾脑局域网 Demo")
    parser.add_argument("--preview", action="store_true", help="强制预览，绝不调用桌面执行器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    app = Application(args.data_dir, args.preview)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        app.stop.set()
        print("无法启动，可能已有程序占用端口。关闭之前的窗口，或使用 --port 8766。", exc, flush=True)
        return 1
    server.app = app
    url = "http://127.0.0.1:%s" % args.port
    print("\n=== 龙虾脑 · 90 分钟最小闭环 ===", flush=True)
    print("模式：" + ("预览（无桌面执行能力）" if app.preview else ("Mac 真机执行（不是 Windows）" if app.mode == "mac" else "Windows 真实执行")), flush=True)
    print("电脑打开：" + url, flush=True)
    if args.host != "127.0.0.1":
        for ip in lan_addresses():
            print("手机同一 Wi-Fi 打开：http://%s:%s" % (ip, args.port), flush=True)
    print("配对码：" + app.pin + "  （仅限本次启动）", flush=True)
    print("保持此窗口开启；按 Ctrl+C 停止。仅用于可信局域网。\n", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        if app.worker:
            app.worker.join(timeout=8)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
