"""希罗酱の日记 —— 单文件「日记本」模式的定时日记插件。

设计要点：
- 所有日记追加到同一本 `日记本.md`，模拟真实日记本；文件永不删除，超过阈值自动分卷。
- 不依赖 `computer_use_runtime`，文件由插件自己写，模型只负责生成正文。
- 聊天素材只取目标 UMO（默认是配置里填的那个会话）当天的记录。
- 定时任务用 `context.cron_manager.add_basic_job(persistent=False)`，
  每次插件初始化时重新注册，避免重启后 handler 丢失。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_workspaces_path,
)

try:  # 4.29 起才有，缺失时退回本地实现
    from astrbot.core.workspace import normalize_umo_for_workspace
except Exception:  # pragma: no cover
    normalize_umo_for_workspace = None

PLUGIN_NAME = "astrbot_plugin_diary"
JOB_DAILY = "diary_daily"
JOB_CATCHUP = "diary_catchup"
JOB_SHARE = "diary_share"

DEFAULT_UMO = "aiocqhttp:FriendMessage:10000000"
DEFAULT_CRON = "30 23 * * *"
DEFAULT_CATCHUP_CRON = "5 0 * * *"
DEFAULT_SHARE_CRON = "30 23 * * *"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_ALIAS = "他"
DEFAULT_CHAT_TITLE = "和他的聊天"
DEFAULT_BOOK_FILE = "日记本.md"
DEFAULT_PREFIX = "这是今天的「希罗酱の日记」，想给你看一下"

LLM_TIMEOUT_SECONDS = 240
PROVIDER_WAIT_SECONDS = 120
MAX_CONVERSATIONS = 5
TAIL_BYTES = 65536
BOOK_SEPARATOR = "\n---\n\n"

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 用户消息里会附带 <system_reminder>Current datetime: 2026-11-02 22:53 (CST)...</system_reminder>
_DATETIME_RE = re.compile(r"Current datetime:\s*(\d{4}-\d{2}-\d{2})")
_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LEADING_DATE_RE = re.compile(r"^\s*#*\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n]*\n+")
_DATE_ARG_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")

DIARY_SYSTEM_PROMPT = """你现在要写一篇日记。这是只给自己看的私密记录，不是聊天。

【格式要求，优先于人格设定里的对话格式限制】
- 写日记时允许使用 Markdown 二级标题。
- 不要发表情包，不要 @ 任何人，不要调用任何工具，不要使用『伪证』格式。
- 只输出日记正文：不要前言，不要解释，不要问句，不要写日期标题。{kaomoji_rule}

【正文结构】
## 我的生活
写 1 到 2 件今天真实发生的小事：吃了什么、做了什么、和艾玛一起做了什么。
控制在 10 到 30 个字，允许少量超出，但不要超过 40 个字。
只记事，不抒情，不要写「今天很安静」这类空话。

## {chat_title}
用第一人称总结今天和{alias}聊了什么。
一般 10 到 100 字；如果聊的是重要或深入的话题，可以适当多写。
称呼对方为「{alias}」，不要用「主人」「您」之类的称呼。
如果今天没有聊天记录，整个部分都不要写，也不要提到「今天没聊天」。
{thought_block}"""

KAOMOJI_RULE = (
    "\n- 可以自然地用颜文字表达情绪，比如开心 (≧◡≦)、无奈 (-_-;)、"
    "害羞 (*/ω＼*)、发呆 ( ๑ • ̯ • ๑ )；一篇里用 1 到 2 个就够，"
    "不要每句都加，也不要用 emoji。"
)
NO_KAOMOJI_RULE = "\n- 不要使用颜文字或 emoji。"

THOUGHT_BLOCK = """
最后空一行，另起一段，写 1 到 2 句只有自己知道的心里话：今天真实的心情、后悔或得意的小事、想说又没说出口的话。
10 到 40 个字，口语一点，可以带一点情绪。
这一段是日记的最后一段，不要加小标题，不要写「心里想的」「心里话」之类的字样，也不要用「——」开头。
不要总结今天，不要复述前面写过的事，不要写成给别人看的句子。"""


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _safe_workspace_name(umo: str) -> str:
    if normalize_umo_for_workspace is not None:
        try:
            return normalize_umo_for_workspace(umo)
        except Exception:  # noqa: BLE001
            pass
    return re.sub(r"[^A-Za-z0-9._-]", "_", umo)


def _safe_filename(name: str, fallback: str) -> str:
    name = (name or "").strip().strip("/\\")
    if not name or ".." in Path(name).parts:
        return fallback
    return name


def _split_message(text: str, limit: int) -> list[str]:
    """按段落切分长消息，尽量不把一句话拆断。"""
    if limit <= 0:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:  # 超长单行，硬切
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks or [text]


@register("astrbot_plugin_diary", "MXJ", "单文件日记本：定时写希罗日记并可选分享", "0.3.2")
class DiaryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.umo = str(self.config.get("umo") or DEFAULT_UMO).strip()
        self.cron_expression = str(self.config.get("cron_expression") or DEFAULT_CRON).strip()
        self.catchup_cron = str(self.config.get("catchup_cron") or DEFAULT_CATCHUP_CRON).strip()
        self.share_cron = str(self.config.get("share_cron") or DEFAULT_SHARE_CRON).strip()
        self.share_umo = str(self.config.get("share_umo") or "").strip() or self.umo
        self.persona_id = str(self.config.get("persona_id") or "").strip()
        self.provider_id = str(self.config.get("provider_id") or "").strip()
        self.extra_instruction = str(self.config.get("extra_instruction") or "").strip()
        self.user_alias = str(self.config.get("user_alias") or DEFAULT_ALIAS).strip() or DEFAULT_ALIAS
        self.chat_section_title = (
            str(self.config.get("chat_section_title") or DEFAULT_CHAT_TITLE).strip()
            or DEFAULT_CHAT_TITLE
        )
        self.kaomoji_enabled = _as_bool(self.config.get("kaomoji_enabled"), True)
        self.thought_enabled = _as_bool(self.config.get("thought_enabled"), True)
        self.notify_prefix = str(self.config.get("notify_prefix") or DEFAULT_PREFIX).strip()

        self.overwrite = False  # 追加模式，保留字段避免旧配置报错
        self.notify = _as_bool(self.config.get("notify"), True)
        self.share_enabled = _as_bool(self.config.get("share_enabled"), False)
        self.instant_trigger = _as_bool(self.config.get("instant_trigger"), False)
        self.max_context_chars = _as_int(self.config.get("max_context_chars"), 8000)
        self.max_message_chars = _as_int(self.config.get("max_message_chars"), 1000)
        self.catchup_days = _as_int(self.config.get("catchup_days"), 0)
        self.book_max_mb = max(1, _as_int(self.config.get("book_max_mb"), 500))

        self.diary_dir = _safe_filename(self.config.get("diary_dir") or "diary", "diary")
        self.book_file = _safe_filename(
            self.config.get("book_file") or DEFAULT_BOOK_FILE, DEFAULT_BOOK_FILE
        )

        tz_name = str(self.config.get("timezone") or DEFAULT_TZ).strip()
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            logger.warning("[diary] invalid timezone %r, fallback to %s", tz_name, DEFAULT_TZ)
            self.tz = ZoneInfo(DEFAULT_TZ)

        self._lock = asyncio.Lock()
        self._state_path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "state.json"

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            logger.error("[diary] cron_manager 不可用，日记功能不会定时运行")
            return

        await self._remove_jobs(manager)

        try:
            if self.share_enabled:
                # 开启分享时，写入与发送合并到分享任务，避免同一天写两篇
                await manager.add_basic_job(
                    name=JOB_SHARE,
                    cron_expression=self.share_cron,
                    handler=self._job_share,
                    description="写当天日记并分享到目标会话",
                    timezone=str(self.tz),
                    persistent=False,
                )
                logger.info(
                    "[diary] 已注册 %s：cron=%r → %s（写入+分享）",
                    JOB_SHARE,
                    self.share_cron,
                    self.share_umo,
                )
            else:
                await manager.add_basic_job(
                    name=JOB_DAILY,
                    cron_expression=self.cron_expression,
                    handler=self._job_daily,
                    description="每天生成希罗日记",
                    timezone=str(self.tz),
                    persistent=False,
                )
                logger.info(
                    "[diary] 已注册 %s：cron=%r tz=%s umo=%s",
                    JOB_DAILY,
                    self.cron_expression,
                    self.tz,
                    self.umo,
                )
        except Exception as e:  # noqa: BLE001
            logger.error("[diary] 注册写入任务失败：%s", e)

        if self.catchup_days > 0:
            try:
                await manager.add_basic_job(
                    name=JOB_CATCHUP,
                    cron_expression=self.catchup_cron,
                    handler=self._job_catchup,
                    description=f"补写最近 {self.catchup_days} 天缺失的日记",
                    timezone=str(self.tz),
                    persistent=False,
                )
                logger.info("[diary] 已注册 %s：cron=%r", JOB_CATCHUP, self.catchup_cron)
            except Exception as e:  # noqa: BLE001
                logger.error("[diary] 注册补写任务失败：%s", e)

        await self._maybe_trigger_instant()

    async def terminate(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is not None:
            await self._remove_jobs(manager)

    async def _remove_jobs(self, manager) -> None:
        try:
            jobs = await manager.list_jobs(job_type="basic")
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 读取任务列表失败：%s", e)
            return
        for job in jobs:
            if getattr(job, "name", "") in {JOB_DAILY, JOB_CATCHUP, JOB_SHARE}:
                try:
                    await manager.delete_job(job.job_id)
                    logger.info("[diary] 已清理旧任务 %s(%s)", job.name, job.job_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[diary] 删除旧任务 %s 失败：%s", job.name, e)

    # ------------------------------------------------------------------ 定时入口

    async def _job_daily(self, **_kwargs) -> None:
        today = datetime.now(self.tz).date()
        ok, info = await self._ensure_daily_entry(today, send=self.notify)
        if not ok:
            logger.info("[diary] 每日任务跳过：%s", info)

    async def _job_share(self, **_kwargs) -> None:
        if not self.share_enabled:
            return
        today = datetime.now(self.tz).date()
        ok, info = await self._ensure_daily_entry(today, send=True)
        if ok:
            logger.info("[diary] 已分享 %s → %s", today.isoformat(), self.share_umo)
        else:
            logger.warning("[diary] 分享失败：%s", info)

    async def _job_catchup(self, **_kwargs) -> None:
        if self.catchup_days <= 0:
            return
        today = datetime.now(self.tz).date()
        for offset in range(1, self.catchup_days + 1):
            target = today - timedelta(days=offset)
            if self._has_daily_entry(self._date_header(target)):
                continue
            logger.info("[diary] 补写 %s", target.isoformat())
            ok, info = await self._ensure_daily_entry(target, send=False)
            if not ok:
                logger.info("[diary] 补写 %s 失败：%s", target.isoformat(), info)
            await asyncio.sleep(5)

    # ------------------------------------------------------------------ 即时日记

    async def _maybe_trigger_instant(self) -> None:
        state = self._load_state()
        previous = bool(state.get("instant_trigger"))
        if self.instant_trigger and not previous:
            logger.info("[diary] 检测到「现在立刻写一则日记」开关被打开，开始写")
            asyncio.create_task(self._run_instant())
        state["instant_trigger"] = self.instant_trigger
        self._save_state(state)

    async def _run_instant(self) -> None:
        now = datetime.now(self.tz)
        try:
            ok, info = await self._ensure_instant_entry(now, send=True)
            if ok:
                logger.info("[diary] 即时日记完成：%s", info)
            else:
                logger.warning("[diary] 即时日记失败：%s", info)
        except Exception as e:  # noqa: BLE001
            logger.error("[diary] 即时日记异常：%s", e, exc_info=True)

    async def _ensure_instant_entry(self, now: datetime, *, send: bool) -> tuple[bool, str]:
        header = f"{self._date_header(now.date())} {now:%H:%M}"
        async with self._lock:
            body = await self._generate_body(now.date())
            if not body:
                return False, "模型返回了空内容"
            path = self._append_entry(header, body)
            entry = f"{header}\n\n{body}"
            logger.info("[diary] 已追加即时日记 %s → %s", header, path)
            if send:
                await self._notify(entry)
            return True, str(path)

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 写入状态文件失败：%s", e)

    # ------------------------------------------------------------------ 手动入口

    @filter.command("diary")
    async def diary_command(self, event: AstrMessageEvent):
        """手动生成日记：/diary [YYYY-MM-DD]，仅管理员可用。"""
        if not event.is_admin():
            yield event.plain_result("只有主人能用这个指令。")
            return

        target = datetime.now(self.tz).date()
        match = _DATE_ARG_RE.search(event.message_str or "")
        if match:
            try:
                target = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                yield event.plain_result("日期不合法，格式示例：/diary 2026-11-02")
                return

        if self._lock.locked():
            yield event.plain_result("正在写另一篇日记，稍等一下。")
            return

        yield event.plain_result(f"开始处理 {target.isoformat()} 的日记。")

        ok, info = await self._ensure_daily_entry(target, send=True)
        if not ok:
            yield event.plain_result(f"没写成：{info}")

    # ------------------------------------------------------------------ 日记本

    def _diary_root(self) -> Path:
        return Path(get_astrbot_workspaces_path()) / _safe_workspace_name(self.umo) / self.diary_dir

    def _book_path(self) -> Path:
        """当前应写入的日记本文件；超过阈值时自动切到下一卷。"""
        base = self._diary_root() / self.book_file
        limit = self.book_max_mb * 1024 * 1024
        if not base.exists() or base.stat().st_size < limit:
            return base

        stem, suffix = base.stem, base.suffix
        index = 2
        while True:
            candidate = base.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists() or candidate.stat().st_size < limit:
                return candidate
            index += 1

    @staticmethod
    def _date_header(target: date) -> str:
        return f"{target.year}年{target.month:02d}月{target.day:02d}日"

    def _tail_text(self, path: Path, size: int = TAIL_BYTES) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                total = handle.tell()
                handle.seek(max(0, total - size))
                return handle.read().decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 读取日记本失败 %s：%s", path, e)
            return ""

    def _has_daily_entry(self, header: str) -> bool:
        path = self._book_path()
        if not path.exists():
            return False
        for line in self._tail_text(path).splitlines():
            if line.strip() == header:
                return True
        return False

    def _read_entry(self, header: str) -> str | None:
        path = self._book_path()
        if not path.exists():
            return None
        for chunk in reversed(self._tail_text(path).split(BOOK_SEPARATOR)):
            text = chunk.strip("\n")
            if not text:
                continue
            first_line = text.split("\n", 1)[0].strip()
            if first_line == header:
                return text.strip()
        return None

    def _append_entry(self, header: str, body: str) -> Path:
        path = self._book_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = BOOK_SEPARATOR if path.exists() and path.stat().st_size > 0 else ""
        entry = f"{header}\n\n{body.strip()}\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + entry)
        self._relax_permissions(path)
        return path

    @staticmethod
    def _relax_permissions(path: Path) -> None:
        """让同机的网站容器（非 root）也能读写日记本。

        日记由容器里的 root 创建，默认 644；网站以 node 运行，只能读不能写。
        这里把目录放开到 777、文件放开到 666，网站才能做「编辑」并原子替换。
        """
        try:
            os.chmod(path.parent, 0o777)
            os.chmod(path, 0o666)
        except OSError as e:
            logger.warning("[diary] 调整日记本权限失败 %s：%s", path, e)

    # ------------------------------------------------------------------ 生成主流程

    async def _ensure_daily_entry(self, target: date, *, send: bool) -> tuple[bool, str]:
        header = self._date_header(target)
        if self._lock.locked():
            return False, "另一个日记任务正在运行"

        async with self._lock:
            if self._has_daily_entry(header):
                entry = self._read_entry(header)
                if entry and send:
                    await self._notify(entry)
                return True, "已存在，跳过生成"

            body = await self._generate_body(target)
            if not body:
                return False, "模型返回了空内容"

            try:
                path = self._append_entry(header, body)
            except Exception as e:  # noqa: BLE001
                logger.error("[diary] 写入日记本失败：%s", e, exc_info=True)
                return False, f"写入失败：{e}"

            entry = f"{header}\n\n{body}"
            logger.info(
                "[diary] 已追加 %s → %s（正文 %d 字）",
                header,
                path,
                len(body),
            )

            if send:
                await self._notify(entry)

            return True, str(path)

    async def _notify(self, entry: str, umo: str | None = None) -> None:
        target = umo or self.share_umo
        text = f"{self.notify_prefix}\n{entry}" if self.notify_prefix else entry
        for chunk in _split_message(text, self.max_message_chars):
            try:
                await self.context.send_message(target, MessageChain(chain=[Plain(chunk)]))
            except Exception as e:  # noqa: BLE001
                logger.error("[diary] 发送失败：%s", e)
                return
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ 取素材

    async def _load_day_messages(self, target: date) -> list[tuple[str, str]]:
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            logger.warning("[diary] conversation_manager 不可用，按无聊天记录处理")
            return []

        start = int(datetime(target.year, target.month, target.day, tzinfo=self.tz).timestamp())
        end = start + 86400

        conversations = []
        try:
            conversations = await cm.get_conversations(self.umo)
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 读取会话列表失败：%s", e)

        picked = [
            c
            for c in conversations
            if start <= _as_int(getattr(c, "updated_at", 0), 0) < end
        ]
        if not picked:
            # 时间戳异常时退回当前会话，至少不丢当天的尾部对话
            try:
                cid = await cm.get_curr_conversation_id(self.umo)
                if cid:
                    conv = await cm.get_conversation(self.umo, cid)
                    if conv is not None:
                        picked = [conv]
            except Exception as e:  # noqa: BLE001
                logger.warning("[diary] 读取当前会话失败：%s", e)

        picked.sort(key=lambda c: _as_int(getattr(c, "created_at", 0), 0))

        result: list[tuple[str, str]] = []
        for conv in picked[-MAX_CONVERSATIONS:]:
            try:
                history = json.loads(getattr(conv, "history", "[]") or "[]")
            except Exception as e:  # noqa: BLE001
                logger.warning("[diary] 解析历史失败：%s", e)
                continue
            for item in self._extract_day(history, target.isoformat()):
                if result and result[-1] == item:
                    continue
                result.append(item)
        return result

    @staticmethod
    def _extract_day(history: list, target_iso: str) -> list[tuple[str, str]]:
        """从会话历史里抽出某一天的消息。

        历史条目形如 {"role": "user", "content": [{"type": "text", "text": "..."}]}，
        日期只存在于用户消息的 system_reminder 文本里，助手消息沿用上一条的日期。
        """
        out: list[tuple[str, str]] = []
        current_date: str | None = None

        for msg in history or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue

            pieces: list[str] = []
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    pieces.append(str(part.get("text") or ""))
                elif ptype == "image":
                    pieces.append("[图片]")
                elif ptype == "file":
                    pieces.append("[文件]")
                elif ptype == "record":
                    pieces.append("[语音]")
                elif ptype == "video":
                    pieces.append("[视频]")
                elif ptype == "at":
                    pieces.append(f"@{part.get('name') or part.get('user_id') or ''}")

            joined = "".join(pieces)
            match = _DATETIME_RE.search(joined)
            if match:
                current_date = match.group(1)

            cleaned = _REMINDER_RE.sub("", joined).strip()
            if not cleaned or current_date != target_iso:
                continue

            out.append(("我" if role == "assistant" else "", cleaned))

        return out

    def _render_transcript(self, messages: list[tuple[str, str]], budget: int) -> str:
        lines = [
            f"{speaker or self.user_alias}：{text}" for speaker, text in messages
        ]
        if budget > 0:
            total = sum(len(line) + 1 for line in lines)
            while lines and total > budget:
                total -= len(lines[0]) + 1
                lines.pop(0)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 模型

    def _resolve_provider(self):
        ctx = self.context
        if self.provider_id:
            getter = getattr(ctx, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(self.provider_id)
                    if provider is not None:
                        return provider
                except Exception as e:  # noqa: BLE001
                    logger.warning("[diary] 指定 provider %r 不可用：%s", self.provider_id, e)

        try:
            provider = ctx.get_using_provider(umo=self.umo)
        except TypeError:
            provider = ctx.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 获取默认 provider 失败：%s", e)
            provider = None

        if provider is None:
            try:
                provider = ctx.get_using_provider()
            except Exception:  # noqa: BLE001
                provider = None
        return provider

    async def _persona_prompt(self) -> str:
        if not self.persona_id:
            return ""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        try:
            persona = manager.get_persona_v3_by_id(self.persona_id)
            if persona:
                return str(persona.get("prompt") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 读取人格 %r 失败：%s", self.persona_id, e)
        try:
            persona = await manager.get_persona(self.persona_id)
            return str(getattr(persona, "system_prompt", "") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("[diary] 人格 %r 不存在：%s", self.persona_id, e)
            return ""

    def _system_prompt(self, persona_prompt: str) -> str:
        thought_block = THOUGHT_BLOCK if self.thought_enabled else ""
        diary_prompt = DIARY_SYSTEM_PROMPT.format(
            kaomoji_rule=KAOMOJI_RULE if self.kaomoji_enabled else NO_KAOMOJI_RULE,
            thought_block=thought_block,
            chat_title=self.chat_section_title,
            alias=self.user_alias,
        )
        parts = [persona_prompt, diary_prompt]
        if self.extra_instruction:
            parts.append(self.extra_instruction)
        return "\n\n".join(part for part in parts if part)

    async def _wait_provider(self, timeout: float = PROVIDER_WAIT_SECONDS):
        """等模型提供方就绪。

        AstrBot 启动时插件 initialize() 早于 provider 加载完成，此时取不到模型；
        「现在立刻写一则日记」在启动阶段触发就会直接失败，所以这里轮询等待。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            provider = self._resolve_provider()
            if provider is not None:
                return provider
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(5)

    async def _generate_body(self, target: date) -> str:
        provider = await self._wait_provider()
        if provider is None:
            logger.warning("[diary] 等不到可用的模型提供方，放弃这次生成")
            raise RuntimeError("没有可用的模型提供方")

        messages = await self._load_day_messages(target)
        transcript = self._render_transcript(messages, self.max_context_chars)

        weekday = WEEKDAYS[target.weekday()]
        prompt_lines = [
            f"今天是 {target.year}年{target.month:02d}月{target.day:02d}日（{weekday}）。"
        ]
        if transcript:
            prompt_lines.append(f"\n【今天和{self.user_alias}的聊天记录】\n{transcript}")
        else:
            prompt_lines.append(
                f"\n今天没有任何聊天记录。这次只写「## 我的生活」一个部分，"
                f"不要出现「## {self.chat_section_title}」这个标题，"
                "也不要在正文里交代「今天没聊天」这件事。"
            )
        prompt_lines.append("\n请写今天的日记。")
        prompt = "\n".join(prompt_lines)

        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=self._system_prompt(await self._persona_prompt()),
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        text = getattr(response, "completion_text", None) or ""
        body = self._normalize_body(text)
        if not transcript:
            body = self._strip_chat_section(body)
        self._warn_section_lengths(body, bool(transcript))
        return body

    @staticmethod
    def _normalize_body(text: str) -> str:
        text = (text or "").strip()
        text = _CODE_FENCE_RE.sub("", text).strip()
        text = _LEADING_DATE_RE.sub("", text).strip()
        return text

    def _strip_chat_section(self, text: str) -> str:
        """无聊天记录时，兜底删掉模型自行补出来的聊天段落。"""
        pattern = re.compile(
            r"\n#{1,6}\s*" + re.escape(self.chat_section_title) + r"[^\n]*\n"
        )
        match = pattern.search(text)
        if match:
            return text[: match.start()].rstrip()
        return text

    @staticmethod
    def _section_text(body: str, title: str) -> str:
        pattern = re.compile(
            r"#{1,6}\s*" + re.escape(title) + r"[^\n]*\n(.*?)(?=\n#{1,6}\s|\Z)", re.S
        )
        match = pattern.search(body)
        return match.group(1).strip() if match else ""

    def _warn_section_lengths(self, body: str, has_chat: bool) -> None:
        life = self._section_text(body, "我的生活")
        if life and len(life) > 40:
            logger.warning("[diary] 「我的生活」有 %d 字，超出预期（目标 10-30）", len(life))
        if self.thought_enabled:
            tail = body.strip().split("\n\n")[-1].strip()
            if tail and len(tail) > 60:
                logger.warning(
                    "[diary] 结尾的心里话有 %d 字，超出预期（目标 10-40）", len(tail)
                )
        if has_chat:
            chat = self._section_text(body, self.chat_section_title)
            if chat and len(chat) > 200:
                logger.warning(
                    "[diary] 「%s」有 %d 字，超出预期（目标 10-100）",
                    self.chat_section_title,
                    len(chat),
                )
