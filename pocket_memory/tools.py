from __future__ import annotations

import calendar
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

logger = logging.getLogger(__name__)


class ToolRegistry:
    """本地小工具注册中心：倒计时、番茄钟、日历提醒。

    - 倒计时/番茄钟：内存计时器，进程退出即失效
    - 日历提醒：SQLite 持久化，重启后自动恢复
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS schedules (
        id TEXT PRIMARY KEY,
        message TEXT NOT NULL,
        due_at TEXT NOT NULL,
        repeat TEXT NOT NULL DEFAULT 'none',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "tools.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        # 内存计时器：{timer_id: {expire_at, message, kind, remaining_seconds}}
        self._timers: dict[str, dict] = {}
        self._timer_threads: dict[str, threading.Timer] = {}
        # 到期待通知队列：前端轮询时消费，关窗期间也不丢失
        self._due_queue: list[dict] = []
        self._notify_showing = False  # 防止同时弹多个系统消息框
        self._watcher_stop = threading.Event()
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()

        self.tools = {
            "set_timer": self.set_timer,
            "pomodoro_start": self.pomodoro_start,
            "schedule": self.schedule,
        }

    def close(self) -> None:
        self._watcher_stop.set()
        with self._lock:
            for thread in self._timer_threads.values():
                thread.cancel()
            self._conn.close()

    # ---------- 工具实现 ----------

    def set_timer(self, days: float = 0, hours: float = 0, minutes: float = 0, seconds: int = 0, message: str = "") -> dict:
        """倒计时：N 天/小时/分/秒后提醒。各字段可任意组合。"""
        total_seconds = (
            float(days) * 86400
            + float(hours) * 3600
            + float(minutes) * 60
            + float(seconds)
        )
        if total_seconds <= 0:
            return {"ok": False, "error": "倒计时时长必须大于 0"}
        timer_id = f"timer_{uuid.uuid4().hex[:8]}"
        expire_at = datetime.now() + timedelta(seconds=total_seconds)
        with self._lock:
            self._timers[timer_id] = {
                "expire_at": expire_at.isoformat(),
                "message": message or "倒计时到了",
                "kind": "timer",
                "remaining_seconds": int(total_seconds),
                "status": "running",
            }
        return {
            "ok": True,
            "timer_id": timer_id,
            "seconds": round(total_seconds, 1),
            "message": self._timers[timer_id]["message"],
            "expire_at": expire_at.isoformat(),
        }

    def pomodoro_start(self, work_minutes: int = 25, break_minutes: int = 5) -> dict:
        """番茄钟：工作 N 分钟，休息 M 分钟。"""
        timer_id = f"pomodoro_{uuid.uuid4().hex[:8]}"
        expire_at = datetime.now() + timedelta(minutes=work_minutes)
        with self._lock:
            self._timers[timer_id] = {
                "expire_at": expire_at.isoformat(),
                "message": f"工作时间到，休息 {break_minutes} 分钟",
                "kind": "pomodoro_work",
                "remaining_seconds": work_minutes * 60,
                "status": "running",
                "work_minutes": work_minutes,
                "break_minutes": break_minutes,
                "phase": "work",
            }
        return {
            "ok": True,
            "timer_id": timer_id,
            "phase": "work",
            "work_minutes": work_minutes,
            "break_minutes": break_minutes,
            "expire_at": expire_at.isoformat(),
            "message": f"番茄钟开始，专注 {work_minutes} 分钟",
        }

    REPEAT_TYPES = ("none", "daily", "weekdays", "weekly", "monthly", "quarterly", "yearly")

    def schedule(self, datetime_str: str = "", message: str = "", repeat: str = "none") -> dict:
        """日历提醒：指定时间点提醒，支持重复。

        repeat 可选：none / daily / weekdays(工作日) / weekly(每周) /
        monthly(每月) / quarterly(每季度) / yearly(每年)
        """
        if not datetime_str or not message:
            return {"ok": False, "error": "时间和提醒内容都不能为空"}
        try:
            # 兼容 "2026-07-29 15:00" 和 "2026-07-29 15:00:00"
            due_at = datetime.fromisoformat(datetime_str.replace("T", " ").strip())
        except ValueError as exc:
            return {"ok": False, "error": f"时间格式无法解析: {exc}"}
        if repeat not in self.REPEAT_TYPES:
            repeat = "none"
        schedule_id = f"sch_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO schedules (id, message, due_at, repeat, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (schedule_id, message, due_at.isoformat(), repeat, now_iso),
            )
            self._conn.commit()
        return {
            "ok": True,
            "schedule_id": schedule_id,
            "due_at": due_at.isoformat(),
            "message": message,
            "repeat": repeat,
        }

    @staticmethod
    def _advance_repeat(due_at: datetime, repeat: str, now: datetime) -> datetime | None:
        """基于原 due_at 推进到下一个提醒时间点。

        - 基于 due_at 而非 now 推进，避免长期漂移（如每天 9:00 不会变成 9:00:05）
        - 若推进后仍在过去（关机多天），循环推进直到未来
        返回 None 表示无需重复（none 类型）。
        """
        if repeat == "none":
            return None
        next_due = due_at
        # 安全上限：最多循环 366 次，防止异常数据死循环
        for _ in range(366):
            if repeat == "daily":
                next_due = next_due + timedelta(days=1)
            elif repeat == "weekdays":
                next_due = next_due + timedelta(days=1)
                # 跳过周六(5)周日(6)
                while next_due.weekday() >= 5:
                    next_due = next_due + timedelta(days=1)
            elif repeat == "weekly":
                next_due = next_due + timedelta(weeks=1)
            elif repeat == "monthly":
                next_due = ToolRegistry._add_months(next_due, 1)
            elif repeat == "quarterly":
                next_due = ToolRegistry._add_months(next_due, 3)
            elif repeat == "yearly":
                try:
                    next_due = next_due.replace(year=next_due.year + 1)
                except ValueError:
                    # 2月29日在非闰年不存在，降级到2月28日
                    next_due = next_due.replace(year=next_due.year + 1, day=28)
            else:
                return None
            if next_due > now:
                return next_due
        return next_due

    @staticmethod
    def _add_months(dt: datetime, months: int) -> datetime:
        """加 N 个月，自动处理月末天数（如 1月31日 + 1月 = 2月28/29日）。"""
        month_index = dt.month - 1 + months
        year = dt.year + month_index // 12
        month = month_index % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(dt.day, last_day)
        return dt.replace(year=year, month=month, day=day)

    # ---------- 状态查询与到期检查 ----------

    def _watch_loop(self) -> None:
        """后台守护线程：每秒检查到期计时器，弹出系统级消息框（最小化也可见）。"""
        while not self._watcher_stop.wait(1.0):
            try:
                newly_due = self._process_due()
                if newly_due:
                    self._show_system_notification(newly_due)
            except Exception:
                logger.warning("timer watcher error", exc_info=True)

    def _show_system_notification(self, due_items: list[dict]) -> None:
        """右下角抽屉式提醒窗口，最小化也能弹出。

        用 tkinter 创建无边框置顶窗口，定位在屏幕右下角，
        鼠标悬停 2 秒后自动关闭。合并多条提醒到一个窗口。
        """
        if self._notify_showing:
            return
        self._notify_showing = True

        def _popup():
            try:
                import tkinter as tk
                # 播放柔和提示音
                if winsound:
                    try:
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except Exception:
                        pass

                root = tk.Tk()
                root.overrideredirect(True)  # 无边框
                root.attributes("-topmost", True)  # 置顶

                # 窗口尺寸与定位（右下角，留 20px 边距）
                win_w, win_h = 360, 140
                screen_w = root.winfo_screenwidth()
                screen_h = root.winfo_screenheight()
                x = screen_w - win_w - 20
                y = screen_h - win_h - 60  # 避开任务栏
                root.geometry(f"{win_w}x{win_h}+{x}+{y}")

                # 配色与样式
                root.configure(bg="#2D3748")
                # 标题栏
                title_frame = tk.Frame(root, bg="#4A5568", height=32)
                title_frame.pack(fill="x")
                title_label = tk.Label(
                    title_frame, text="  ⏰ 提醒", fg="#FFFFFF", bg="#4A5568",
                    font=("Microsoft YaHei UI", 11, "bold"), anchor="w",
                )
                title_label.pack(fill="x", padx=0, pady=0)

                # 提醒内容（友好文案）
                lines = []
                for item in due_items[:3]:
                    msg = item.get("message", "时间到了")
                    kind = item.get("kind", "")
                    if kind == "pomodoro_work":
                        lines.append(f"🍅 工作时间到啦！{msg}")
                    elif kind == "pomodoro_break":
                        lines.append(f"☕ 休息结束！{msg}")
                    elif "due_at" in item:
                        lines.append(f"📅 日历提醒：{msg}")
                    else:
                        lines.append(f"⏰ 时间到啦！{msg}，快去看看吧～")
                if len(due_items) > 3:
                    lines.append(f"…还有 {len(due_items) - 3} 条提醒")
                body_text = "\n".join(lines)

                body_label = tk.Label(
                    root, text=body_text, fg="#F7FAFC", bg="#2D3748",
                    font=("Microsoft YaHei UI", 10), anchor="w", justify="left",
                    wraplength=330,
                )
                body_label.pack(fill="both", expand=True, padx=12, pady=8)

                hint_label = tk.Label(
                    root, text="鼠标悬停 2 秒后自动关闭", fg="#A0AEC0", bg="#2D3748",
                    font=("Microsoft YaHei UI", 8), anchor="e",
                )
                hint_label.pack(fill="x", padx=12, pady=(0, 6))

                # 滑入动画：从屏幕右侧滑入
                current_x = screen_w
                target_x = x
                def slide_in():
                    nonlocal current_x
                    if current_x > target_x:
                        current_x = max(target_x, current_x - 30)
                        root.geometry(f"{win_w}x{win_h}+{current_x}+{y}")
                        root.after(15, slide_in)
                slide_in()

                # 鼠标悬停 2 秒自动关闭：鼠标移入后开始计时，持续 2 秒才关闭
                hover_start = [None]  # 记录鼠标移入的时间戳
                closing = [False]  # 防重入：避免多次 slide_out 同时执行
                def on_enter(event):
                    hover_start[0] = root.after(2000, slide_out)  # 2 秒后关闭
                def on_leave(event):
                    if hover_start[0]:
                        root.after_cancel(hover_start[0])
                        hover_start[0] = None
                def slide_out():
                    if closing[0]:
                        return
                    closing[0] = True
                    nonlocal current_x
                    if current_x < screen_w:
                        current_x = min(screen_w, current_x + 40)
                        root.geometry(f"{win_w}x{win_h}+{current_x}+{y}")
                        closing[0] = False  # 动画进行中，允许下一次调用
                        root.after(12, slide_out)
                    else:
                        root.destroy()

                root.bind("<Enter>", on_enter)
                root.bind("<Leave>", on_leave)
                # 点击也可关闭
                root.bind("<Button-1>", lambda e: slide_out())
                # 30 秒后自动关闭（无人操作时的安全兜底）
                root.after(30000, slide_out)

                root.mainloop()
            except Exception:
                # 降级：用 winsound 至少出声
                if winsound:
                    try:
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except Exception:
                        pass
            finally:
                self._notify_showing = False

        threading.Thread(target=_popup, daemon=True).start()

    def _process_due(self) -> list[dict]:
        """检查到期计时器，将新到期的加入待通知队列。返回本次新到期列表。"""
        with self._lock:
            now = datetime.now()
            newly_due = []
            for timer_id, info in list(self._timers.items()):
                if info["status"] != "running":
                    continue
                expire_at = datetime.fromisoformat(info["expire_at"])
                remaining = (expire_at - now).total_seconds()
                if remaining > 0:
                    info["remaining_seconds"] = max(0, int(remaining))
                    continue
                info["status"] = "due"
                due_item = {
                    "id": timer_id,
                    "kind": info["kind"],
                    "message": info["message"],
                    "expire_at": info["expire_at"],
                }
                newly_due.append(due_item)
                self._due_queue.append(due_item)
                if info.get("kind") == "pomodoro_work":
                    self._start_pomodoro_break(timer_id, info)
                else:
                    self._timers.pop(timer_id, None)
            # 日历提醒到期也加入队列
            sched_due = self._check_due_schedules(now)
            for item in sched_due:
                self._due_queue.append(item)
            return newly_due + sched_due

    def list_active(self) -> dict:
        """返回活动中的计时器和待触发的日历提醒（消费待通知队列）。"""
        with self._lock:
            now = datetime.now()
            # 先处理到期（以防 watcher 还没跑到）
            self._process_due()
            # 收集活动中的计时器
            active_timers = []
            for timer_id, info in self._timers.items():
                if info["status"] == "running":
                    active_timers.append({
                        "id": timer_id,
                        "kind": info["kind"],
                        "message": info["message"],
                        "remaining_seconds": info["remaining_seconds"],
                        "expire_at": info["expire_at"],
                        "phase": info.get("phase", ""),
                    })
            # 消费待通知队列
            due_timers = []
            due_schedules = []
            for item in self._due_queue:
                if "kind" in item:
                    due_timers.append(item)
                else:
                    due_schedules.append(item)
            self._due_queue.clear()
            return {
                "active_timers": active_timers,
                "due_timers": due_timers,
                "due_schedules": due_schedules,
            }

    def _start_pomodoro_break(self, timer_id: str, info: dict) -> None:
        """番茄钟工作结束，启动休息阶段。"""
        break_minutes = info.get("break_minutes", 5)
        break_expire = datetime.now() + timedelta(minutes=break_minutes)
        info["expire_at"] = break_expire.isoformat()
        info["status"] = "running"
        info["kind"] = "pomodoro_break"
        info["phase"] = "break"
        info["message"] = f"休息结束，开始下一个番茄钟"
        info["remaining_seconds"] = break_minutes * 60

    def _check_due_schedules(self, now: datetime) -> list[dict]:
        """检查到期的日历提醒，返回待通知列表并处理重复。"""
        due = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE status = 'pending' AND due_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                due.append({
                    "id": row["id"],
                    "message": row["message"],
                    "due_at": row["due_at"],
                    "repeat": row["repeat"],
                })
                # 处理重复：基于原 due_at 推进，避免时间漂移
                due_at = datetime.fromisoformat(row["due_at"])
                next_due = self._advance_repeat(due_at, row["repeat"], now)
                if next_due is not None:
                    self._conn.execute(
                        "UPDATE schedules SET due_at = ? WHERE id = ?",
                        (next_due.isoformat(), row["id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE schedules SET status = 'done' WHERE id = ?",
                        (row["id"],),
                    )
            if rows:
                self._conn.commit()
        return due

    def list_schedules(self) -> list[dict]:
        """列出所有日历提醒（前端展示用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE status = 'pending' ORDER BY due_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def cancel_timer(self, timer_id: str) -> bool:
        """取消计时器。"""
        with self._lock:
            if timer_id in self._timers:
                self._timers.pop(timer_id, None)
                thread = self._timer_threads.pop(timer_id, None)
                if thread:
                    thread.cancel()
                return True
            return False

    def execute(self, tool_name: str, args: dict) -> dict:
        """执行工具。"""
        fn = self.tools.get(tool_name)
        if not fn:
            return {"ok": False, "error": f"未知工具: {tool_name}"}
        try:
            return fn(**args)
        except TypeError as exc:
            return {"ok": False, "error": f"参数错误: {exc}"}
