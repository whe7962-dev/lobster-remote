"""有限桌面执行器。真实浏览器观测；腾讯会议打开与入会严格区分。"""
import json
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent


class Cancelled(Exception):
    pass


class DesktopExecutor:
    def __init__(self, store, stop):
        self.store, self.stop = store, stop
        self.playwright = None
        self.browser = None
        self.pages = {}
        from meeting_lifecycle import Lifecycle
        self.lifecycle = Lifecycle(self)
        self.config = {}
        if (ROOT / "config.json").exists():
            self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def check(self, task_id):
        t = self.store.get(task_id)
        if self.stop.is_set() or not t or t["status"] not in ("queued", "running", "needs_input"):
            raise Cancelled()

    def progress(self, task_id, message, **kwargs):
        self.check(task_id)
        self.store.update(task_id, "running", message, **kwargs)

    def execute(self, task):
        task_id = task["id"]
        try:
            self.progress(task_id, "电脑执行端已领取任务。")
            if task["intent"]["kind"] == "movie":
                self.movie(task)
            else:
                self.meeting(task)
        except Cancelled:
            return
        except ImportError:
            self.store.update(task_id, "failed", "浏览器依赖未安装。请运行安装脚本后重启。", error_code="missing_dependency")
        except Exception as exc:
            self.store.update(task_id, "failed", "执行失败：" + str(exc)[:300], error_code="execution_error")

    def get_browser(self):
        if self.browser:
            try:
                self.browser.pages
                return self.browser
            except Exception:
                self.browser = None
        from playwright.sync_api import sync_playwright
        if not self.playwright:
            self.playwright = sync_playwright().start()
        channel = self.config.get("browser_channel") or ("chrome" if platform.system() == "Darwin" else "msedge")
        self.browser = self.playwright.chromium.launch_persistent_context(
            str(self.store.folder / "browser-profile"), channel=channel, headless=False,
            viewport={"width": 1280, "height": 800}, timeout=30000,
            args=["--no-first-run"])
        self.browser.set_default_timeout(15000)
        return self.browser

    def movie(self, task):
        task_id = task["id"]
        self.progress(task_id, "正在打开浏览器，搜索电影相关视频。")
        browser = self.get_browser()
        # 仅关闭本程序上一条任务创建的页面，防止多段视频同时播放。
        for old_id, old_page in list(self.pages.items()):
            if old_id != task_id:
                try:
                    old_page.close()
                except Exception:
                    pass
        page = browser.new_page()
        self.pages[task_id] = page
        search_url = "https://search.bilibili.com/all?keyword=" + quote(task["intent"]["keyword"])
        result = dict(search_url=search_url, keyword=task["intent"]["keyword"], verification="pending", selection=task["intent"]["selection"])
        self.store.update(task_id, "running", "已生成搜索词：" + task["intent"]["keyword"], result=result)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
        except Exception:
            self.check(task_id)
            self.store.update(task_id, "needs_input", "搜索页加载未完成。请在电脑检查网络、登录或验证码，然后点“重新检查”。", error_code="search_load_failed", result=result)
            return
        self.advance_movie(task_id)

    def advance_movie(self, task_id):
        page = self.pages.get(task_id)
        self.check(task_id)
        task = self.store.get(task_id)
        result = dict(task["result"])
        if not page or page.is_closed():
            self.store.update(task_id, "failed", "任务浏览器已关闭，请重新提交。", error_code="browser_closed")
            return
        if not self.is_video_url(page.url):
            self.progress(task_id, "正在查找搜索结果中的可播放视频。")
            try:
                page.wait_for_selector('a[href*="bilibili.com/video/"]', timeout=20000)
                links = page.locator('a[href*="bilibili.com/video/"]').evaluate_all("els => els.map(a => ({url:a.href, title:a.getAttribute('title') || a.textContent.trim()}))")
                candidates = [x for x in links if self.is_video_url(x["url"])]
                if not candidates:
                    raise ValueError("没有可用视频结果")
                title = task["intent"].get("movie_title", "")
                matched = [x for x in candidates if title and title.replace(" ", "") in x["title"].replace(" ", "")]
                if title and not matched:
                    raise ValueError("搜索结果标题没有匹配指定电影")
                def score(x):
                    return (4 if "官方" in x["title"] else 0) + (2 if "预告" in x["title"] else 0) - (8 if any(w in x["title"] for w in ("作业", "混剪", "解说")) else 0)
                chosen = max(matched or candidates, key=score)
                result.update(video_url=chosen["url"].split("?")[0], title=chosen["title"][:200])
                self.progress(task_id, "已找到视频，正在打开播放器。", result=result)
                page.goto(result["video_url"], wait_until="domcontentloaded", timeout=35000)
            except Cancelled:
                raise
            except Exception:
                self.store.update(task_id, "needs_input", "没有读到可用视频结果。请在电脑处理登录/验证码，或手动打开搜索结果里的视频，再点“重新检查”。", error_code="search_needs_help", result=result)
                return
        self.progress(task_id, "正在读取播放器的真实播放进度。")
        result.update(video_url=page.url.split("?")[0])
        try:
            page.wait_for_selector("video", state="attached", timeout=15000)
            playback = page.evaluate("""async () => {
              const v = [...document.querySelectorAll('video')].find(v => v.currentSrc) || document.querySelector('video');
              if (!v) return {error:'no_video'};
              const play = () => Promise.race([v.play(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('play_timeout')),8000))]);
              try { await play(); } catch(e) {
                v.muted = true;
                try { await play(); } catch(e2) { return {error:'play_blocked'}; }
              }
              return {time:v.currentTime, paused:v.paused, muted:v.muted, ready:v.readyState};
            }""")
            page.wait_for_timeout(1800)  # 两次媒体时钟读数，用于证明播放确实向前。
            after = page.evaluate("""() => {
              const v = [...document.querySelectorAll('video')].find(v => v.currentSrc) || document.querySelector('video');
              return v ? {time:v.currentTime, paused:v.paused, muted:v.muted, ready:v.readyState} : {};
            }""")
            self.check(task_id)
            result.update(title=page.title()[:200], playback_start=playback, playback_end=after,
                          observed_at=time.time(), verification="media_clock")
            if not playback.get("error") and not after.get("paused", True) and after.get("ready", 0) >= 2 and after.get("time", 0) - playback.get("time", 0) >= 0.3:
                suffix = "（静音播放，可在电脑打开声音）" if after.get("muted") else ""
                self.store.update(task_id, "succeeded", "已检测到视频正在播放" + suffix + "。依据：播放器进度连续前进；未验证整部电影或实时热度排名。", result=result, error_code=None)
            else:
                self.store.update(task_id, "needs_input", "视频页已打开，但尚未检测到播放。请在电脑点击播放、处理登录或等待缓冲，再点“重新检查”。", result=result, error_code="playback_unverified")
        except Cancelled:
            raise
        except Exception:
            self.store.update(task_id, "needs_input", "播放器不可用或页面需要登录。请在电脑处理后重新检查；不会把打开网页记为播放成功。", result=result, error_code="player_unavailable")

    @staticmethod
    def is_video_url(url):
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.hostname in ("www.bilibili.com", "bilibili.com") and bool(re.match(r"^/video/(BV[\w]+|av\d+)", parsed.path))

    def meeting_path(self):
        configured = self.config.get("meeting_path")
        if configured and Path(configured).exists():
            return str(Path(configured))
        if platform.system() == "Darwin":
            for path in (Path("/Applications/TencentMeeting.app"), Path.home() / "Applications/TencentMeeting.app"):
                if path.exists():
                    return str(path)
            return None
        candidates = []
        for key in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(key)
            if base:
                root = Path(base) / "Tencent" / "WeMeet"
                candidates.extend(root.glob("*/wemeetapp.exe"))
                candidates.extend(root.glob("wemeetapp.exe"))
        if candidates:
            return str(sorted(candidates)[-1])
        return None

    def meeting_running(self):
        if platform.system() == "Darwin":
            proc = subprocess.run(["/usr/bin/pgrep", "-x", "TencentMeeting"], capture_output=True, text=True, timeout=8)
            return proc.returncode == 0
        proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wemeetapp.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace", timeout=8)
        return "wemeetapp.exe" in proc.stdout.lower()

    def meeting(self, task):
        task_id = task["id"]
        if task["intent"]["kind"] == "meeting_create":
            return self.create_meeting(task)
        path = self.meeting_path()
        if not path:
            self.store.update(task_id, "failed", "没有找到腾讯会议。请安装官方客户端，或在 config.json 填写 meeting_path，然后重启。", error_code="meeting_not_installed")
            return
        self.progress(task_id, "正在向系统请求打开腾讯会议。")
        self.check(task_id)
        if platform.system() == "Darwin":
            opened = subprocess.run(["/usr/bin/open", "-a", path], capture_output=True, text=True, timeout=12)
            if opened.returncode:
                raise RuntimeError("系统未能打开腾讯会议：" + opened.stderr[:150])
        else:
            subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        running = False
        for _ in range(12):
            self.check(task_id)
            if self.meeting_running():
                running = True
                break
            self.stop.wait(0.5)
        result = dict(application="腾讯会议", launch_requested=True, process_observed=running,
                      verification="process_observed" if running else "launch_receipt_only", observed_at=time.time(),
                      meeting_code=task["intent"].get("meeting_code"), joined_verified=False)
        if not running:
            self.store.update(task_id, "needs_input", "系统已接收打开请求，但未能确认腾讯会议进程。请在电脑检查权限和安装情况。", result=result, error_code="meeting_process_unverified")
        elif task["intent"]["kind"] == "meeting_open":
            self.store.update(task_id, "succeeded", "已请求打开腾讯会议，并检测到客户端进程。这个结果仅表示打开应用，不代表已入会。", result=result, error_code=None)
        elif task["intent"]["kind"] == "meeting_join":
            self.store.update(task_id, "needs_input", "腾讯会议已打开。请在电脑点击“加入会议”，输入 " + task["intent"]["meeting_code"] + "，检查麦克风/摄像头并完成入会；之后在电脑网页填写实际结果。", result=result, error_code="meeting_human_step")
        else:
            self.store.update(task_id, "needs_input", "腾讯会议已打开。请在电脑检查音视频设置并点击“快速会议”，创建后在电脑网页填写实际会议号。当前版本不会自动宣称创建成功。", result=result, error_code="meeting_human_step")

    def create_meeting(self, task):
        import meeting_cli
        task_id = task["id"]
        if not meeting_cli.executable():
            self.store.update(task_id, "needs_input", "需要安装腾讯官方会议工具。请在电脑运行安装脚本后点“重新检查”。", error_code="meeting_cli_missing")
            return
        self.progress(task_id, "正在检查腾讯会议官方工具的授权状态。")
        try:
            auth = meeting_cli.run(["auth", "status"], timeout=12)
            # 各版本 auth status 字段不同；未授权时的明确标志必须挡住创建。
            serialized = json.dumps(auth).lower()
            if 'not logged in' in serialized or '"logged_in": false' in serialized or '"authenticated": false' in serialized:
                raise RuntimeError("not logged in")
        except Exception:
            self.store.update(task_id, "needs_input", "腾讯会议客户端登录与工具授权是两回事。请在电脑双击“授权腾讯会议.command”（Windows 用对应 bat），完成官方授权后点“重新检查”。", error_code="meeting_auth_required")
            return
        self.check(task_id)
        start = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=1)
        end = start + timedelta(minutes=30)
        subject = "龙虾脑 Demo " + task_id[-6:]
        self.progress(task_id, "正在通过腾讯官方接口创建一场 30 分钟会议。")
        # 创建请求绝不自动重试，避免超时后重复创建。
        try:
            created = meeting_cli.run(["meeting", "create", "--subject", subject,
                "--start", start.isoformat(timespec="seconds"), "--end", end.isoformat(timespec="seconds"),
                "--timezone", "Asia/Shanghai", "--auto-record-type", "none", "--auto-asr=false"], timeout=45)
        except subprocess.TimeoutExpired:
            self.store.update(task_id, "needs_input", "创建请求超时，结果未知。请在腾讯会议查看主题“" + subject + "”。不要直接重试创建，以免产生重复会议。", error_code="meeting_create_unknown", result={"subject": subject, "creation_unknown": True})
            return
        except Exception as exc:
            self.store.update(task_id, "failed", "腾讯会议创建失败：" + str(exc)[:300], error_code="meeting_api_error")
            return
        info = meeting_cli.meeting_info(created)
        if not info:
            self.store.update(task_id, "needs_input", "官方接口已返回，但没有读到会议号。请在腾讯会议核对主题“" + subject + "”，不要重复创建。", error_code="meeting_create_unknown", result={"subject": subject, "creation_unknown": True})
            return
        result = dict(verification="official_api", meeting_code=str(info["meeting_code"]), meeting_id=str(info["meeting_id"]),
                      join_url=info.get("join_url", ""), subject=subject, start=start.isoformat(), end=end.isoformat(),
                      observed_at=time.time(), joined_verified=False)
        # 查询是只读；即使查询失败，创建响应已提供真实会议号，仍保留证据。
        try:
            verified = meeting_cli.meeting_info(meeting_cli.run(["meeting", "get", "--meeting-id", result["meeting_id"]], timeout=20))
            result["read_back_verified"] = bool(verified and str(verified["meeting_code"]) == result["meeting_code"])
        except Exception:
            result["read_back_verified"] = False
        self.store.update(task_id, "succeeded", "腾讯会议已真实创建，会议号 " + result["meeting_code"] + "。此步骤完成创建，未自动开启音视频，也不代表已进入会议。", result=result, error_code=None)
        # 自动打开本机客户端，便于用户查看新会议；不自动接通音视频。
        path = self.meeting_path()
        if path:
            try:
                if platform.system() == "Darwin":
                    subprocess.run(["/usr/bin/open", "-a", path], capture_output=True, timeout=10)
                else:
                    subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def recheck(self, task):
        try:
            if task.get("result", {}).get("creation_unknown"):
                self.store.update(task["id"], "needs_input", "创建结果未知，禁止自动再次创建。请在客户端查找主题“" + task["result"].get("subject", "") + "”，再填写人工结果。")
                return
            if task["intent"]["kind"] == "movie":
                self.advance_movie(task["id"])
            elif task["intent"]["kind"] in ("meeting_open", "meeting_create"):
                self.meeting(task)
            else:
                self.store.update(task["id"], "needs_input", "当前没有腾讯会议入会状态接口。请在电脑网页据实填写人工结果；重新检查不能证明已经入会。")
        except Cancelled:
            pass
        except Exception as exc:
            self.store.update(task["id"], "failed", "重新检查失败：" + str(exc)[:240], error_code="recheck_failed")

    def close_resource(self, task):
        """在执行器线程关闭精确归属的资源，不退出整个浏览器或会议客户端。"""
        task_id = task["id"]
        self.store.set_closure(task_id, "running", "电脑正在处理关闭请求。")
        try:
            if task["intent"]["kind"] == "movie":
                page = self.pages.get(task_id)
                if page is None:
                    self.store.set_closure(task_id, "unknown", "没有找到本次运行管理的播放页（可能已重启）。请在电脑核对；没有操作其他网页。")
                    return
                already_closed = page.is_closed()
                if not already_closed:
                    page.close(run_before_unload=False)
                if not page.is_closed():
                    raise RuntimeError("尚未观测到播放页关闭")
                self.store.set_closure(task_id, "succeeded", "已确认视频页面关闭，播放已停止。" if not already_closed else "已确认本任务视频页面此前已关闭。", verification="page_closed", observed_at=time.time())
            else:
                import meeting_cli
                meeting_id = task["result"].get("meeting_id")
                if task["intent"]["kind"] != "meeting_create" or task["result"].get("verification") != "official_api" or not meeting_id:
                    raise ValueError("缺少本 Demo 创建会议的官方凭据")
                from meeting_lifecycle import read_state
                state = read_state(task["result"])
                if state['ended_verified'] or state['cancelled_verified']:
                    self.lifecycle.record(task, state)
                    if state['cancelled_verified']:
                        self.store.set_closure(task_id, "succeeded", "官方查询确认此预约此前已经取消。", verification="official_cancel_receipt")
                    return
                if state['detail_status'] in ('会议中', 'MEETING_STATE_STARTED') or state['search_status'] in ('进行中', 'ongoing'):
                    self.store.set_closure(task_id, "failed", "会议正在进行，不能用取消预约代替结束。请点“结束会议（电脑确认）”。")
                    return
                response = meeting_cli.run(["meeting", "cancel", "--meeting-id", str(meeting_id), "--meeting-type", "0"], timeout=30)
                # CLI 的退出码加官方 success 回执共同证明取消，不能只凭调用返回。
                if response.get("message", "").lower() != "success":
                    self.store.set_closure(task_id, "unknown", "取消接口未返回明确成功回执，请在腾讯会议核对；不会自动重试。")
                    return
                self.store.set_closure(task_id, "succeeded", "腾讯官方接口确认：本任务创建的会议已取消。腾讯会议客户端仍可继续使用。", verification="official_cancel_receipt", observed_at=time.time())
        except subprocess.TimeoutExpired:
            self.store.set_closure(task_id, "unknown", "关闭请求超时，结果未知。请在电脑核对；不会自动重复取消会议。")
        except Exception as exc:
            self.store.set_closure(task_id, "failed", "关闭未完成：" + str(exc)[:240] + "。如果会议已经开始或结束，请在腾讯会议客户端处理。")

    def meeting_action(self, task, action):
        self.lifecycle.action(task, action)

    def poll_meetings(self):
        self.lifecycle.poll()

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
