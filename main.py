"""Proactive background context compression plugin for AstrBot.

Design: redundant-replacement asynchronous compression.

When a conversation's estimated context usage passes ``trigger_ratio``
(default 60%), the plugin compresses it in the *background* with AstrBot's own
``LLMSummaryCompressor`` (same round splitting / summary prompt / sanitization),
working on an independent snapshot.  When the summary is ready, it is swapped
in atomically (a millisecond-scale replace under the per-UMO session lock)
*merging any turns that arrived while the compression was running*.  The main
conversation never pauses; the automatic 82% in-request compression remains as
a safety net.  A stale background result (history already changed past the
snapshot, e.g. the auto-compression fired first) is discarded.

Admin triggers:
- ``/compress`` slash command (alias /压缩上下文 /summarize)
- Natural-language phrases (default patterns match e.g. "压缩一下上下文",
  "总结我们的对话") — admin-only, short message, consumed so it never reaches
  the main LLM.

Compression weighting: the admin may set ``emphasis_instruction`` (e.g.
"在摘要中额外保留之前遇到的问题及解决方案、未完成事项、用户的偏好") which is
appended to the summary instruction so the compression model keeps that content
with higher priority.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import (
    AstrMessageEvent,
    EventMessageType,
    EventResultType,
    MessageChain,
    MessageEventResult,
    filter,
)
from astrbot.core.message.components import Plain

from astrbot.core.agent.context.compressor import LLMSummaryCompressor
from astrbot.core.agent.message import (
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.utils.session_lock import session_lock_manager

VERSION = "0.2.0"

DEFAULT_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary of key takeaways and/or project progress.\n"
    "1. Systematically cover all core topics discussed and the final conclusion/outcome for each; clearly highlight the latest primary focus.\n"
    "2. If any tools were used, summarize tool usage (total call count) and extract the most valuable insights from tool outputs.\n"
    "3. If any materials (files, documents, code, references) were read during the conversation that may be helpful for subsequent work, list each one with its scope and path.\n"
    "4. If there was an initial user goal, state it first and describe the current progress/status.\n"
    "5. Write the summary in the user's language.\n"
)

DEFAULT_NL_PATTERNS = (
    r"压缩.*(上下文|对话|聊天|记录)",
    r"(上下文|对话|聊天|记录).*压缩",
    r"总结.*(上下文|对话|聊天)",
    r"(上下文|对话|聊天).*总结",
    r"整理.*(上下文|对话|聊天)",
)

# Guard against our own reply text re-triggering the natural-language path.
_SELF_MARKERS = ("✅ 压缩完成", "⏳ 正在压缩", "❌", "ℹ️")


@star.register(
    "astrbot_plugin_proactive_compress",
    "羊魔大人",
    "后台主动上下文压缩：超过阈值（默认60%）后台静默压缩并原子替换，主对话不停机；管理员可用 /compress 或自然语言触发，压缩时按管理员指令加权保留关键内容。",
    VERSION,
)
class ProactiveCompressPlugin(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._running: dict[str, asyncio.Task] = {}  # per-UMO in-flight compression
        self._last_compress: dict[str, float] = {}

    # ---------- config helpers ----------

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        return str(self.config.get(key, default) or "").strip() or default

    def _cfg_list(self, key: str, default: list) -> list:
        value = self.config.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return list(default)

    def _read_provider_settings(self, umo: str) -> dict:
        try:
            cfg = self.context.get_config(umo=umo)
            return dict(cfg.get("provider_settings", {})) if cfg else {}
        except Exception:
            return {}

    def _build_instruction(self, umo: str) -> str:
        """Combine the configured summary instruction with the admin emphasis."""
        base = self._cfg_str("compress_instruction", "") or str(
            self._read_provider_settings(umo).get("llm_compress_instruction", "")
        ).strip()
        if not base:
            base = DEFAULT_INSTRUCTION
        emphasis = self._cfg_str("emphasis_instruction", "")
        if emphasis:
            base = (
                f"{base}\n\n<emphasis_priority>\n"
                "在摘要中特别保留、重点覆盖以下内容：\n"
                f"{emphasis}\n"
                "</emphasis_priority>"
            )
        return base

    # ---------- background trigger: after every LLM response ----------

    @filter.on_llm_response()
    async def maybe_compress(self, event: AstrMessageEvent, response) -> None:
        if not self._cfg_bool("enabled", True):
            return
        umo = event.unified_msg_origin
        try:
            asyncio.get_running_loop().create_task(self._background_check(umo))
        except Exception as exc:  # never break the pipeline
            logger.debug("Proactive compress schedule failed: %s", exc)

    async def _background_check(self, umo: str) -> None:
        try:
            await asyncio.sleep(self._cfg_int("check_delay_seconds", 5))
        except asyncio.CancelledError:
            return
        try:
            await self._maybe_run_compression(umo)
        except Exception as exc:
            logger.debug("Proactive compress check failed: %s", exc)

    async def _maybe_run_compression(self, umo: str) -> None:
        now = time.monotonic()
        if now - self._last_compress.get(umo, 0.0) < self._cfg_int("cooldown_seconds", 300):
            return
        if umo in self._running and not self._running[umo].done():
            return  # one background compression per conversation

        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return
        conv = await conv_mgr.get_conversation(umo, cid)
        if not conv:
            return
        try:
            history = json.loads(conv.history or "[]")
        except (TypeError, ValueError):
            return
        if not isinstance(history, list) or len(history) < self._cfg_int("min_messages", 20):
            return

        provider = self._resolve_provider(umo)
        if provider is None:
            return
        max_ctx = provider.provider_config.get("max_context_tokens", 0)
        if not isinstance(max_ctx, int) or max_ctx <= 0:
            return  # no known window -> skip (avoid runaway)

        est = self._estimate_tokens(history)
        if est <= 0 or est / max_ctx < self._cfg_float("trigger_ratio", 0.6):
            return

        self._last_compress[umo] = time.monotonic()
        task = asyncio.get_running_loop().create_task(
            self._run_background_compression(umo, cid, history, provider)
        )
        self._running[umo] = task
        task.add_done_callback(
            lambda t, u=umo: self._running.pop(u, None)
            if self._running.get(u) is t
            else None
        )

    @staticmethod
    def _estimate_tokens(history: list) -> int:
        chars = sum(
            len(str(m.get("content", "")))
            for m in history
            if isinstance(m, dict) and m.get("role") != "system"
        )
        return int(chars / 2)

    def _resolve_provider(self, umo: str):
        ps = self._read_provider_settings(umo)
        pid = self._cfg_str("compress_provider_id", "") or str(
            ps.get("llm_compress_provider_id", "")
        ).strip()
        provider = None
        if pid:
            provider = self.context.get_provider_by_id(pid)
        if provider is None:
            default_id = str(ps.get("default_provider_id", "")).strip()
            if default_id:
                provider = self.context.get_provider_by_id(default_id)
        return provider

    async def _run_background_compression(self, umo: str, cid: str, snapshot: list, provider) -> None:
        try:
            keep_ratio = self._cfg_float("keep_recent_ratio", 0.15)
            instruction = self._build_instruction(umo)
            messages = bind_checkpoint_messages(snapshot)
            compressor = LLMSummaryCompressor(
                provider=provider,
                keep_recent_ratio=keep_ratio,
                instruction_text=instruction,
            )
            new_messages = await compressor(messages)
            if new_messages is messages:
                return  # nothing worth summarizing
            compressed = dump_messages_with_checkpoints(new_messages)
            await self._atomic_replace(umo, cid, snapshot, compressed)
            logger.info(
                "[ProactiveCompress] %s: %d -> %d messages (background)",
                umo,
                len(snapshot),
                len(compressed),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Proactive compress failed for %s: %s", umo, exc)

    async def _atomic_replace(self, umo: str, cid: str, snapshot: list, compressed: list) -> None:
        async with session_lock_manager.acquire_lock(umo):
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                return
            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv:
                return
            try:
                current = json.loads(conv.history or "[]")
            except (TypeError, ValueError):
                return
            n = len(snapshot)
            if len(current) < n or current[:n] != snapshot:
                # Stale redundant result: history already moved past our
                # snapshot (e.g. auto-compression fired first). Discard.
                logger.info("[ProactiveCompress] stale result discarded for %s", umo)
                return
            extra = current[n:]
            new_history = compressed + extra
            await conv_mgr.update_conversation(umo, cid, new_history)

    # ---------- shared compression core ----------

    async def _do_compress(self, umo: str, cid: str, exclude_event=None) -> tuple[str, str]:
        """Stop the running agent, then atomically compress under the session
        lock.  Returns (status, final_message)."""
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return "fail", "❌ 无法访问会话管理器。"

        provider = self._resolve_provider(umo)
        if provider is None:
            return "fail", "❌ 未找到压缩用模型（检查 compress_provider_id / llm_compress_provider_id）。"
        keep_ratio = self._cfg_float("keep_recent_ratio", 0.15)
        instruction = self._build_instruction(umo)

        try:
            active_event_registry.request_agent_stop_all(umo, exclude=exclude_event)
        except Exception as exc:
            logger.debug("Compress: stop request failed: %s", exc)

        async with session_lock_manager.acquire_lock(umo):
            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv:
                return "fail", "❌ 未找到对话。"
            try:
                history = json.loads(conv.history or "[]")
            except (TypeError, ValueError):
                history = []
            if not isinstance(history, list) or not history:
                return "no_history", "ℹ️ 当前对话没有可压缩的历史。"
            before = len(history)
            try:
                messages = bind_checkpoint_messages(history)
            except Exception as exc:
                logger.error("Compress: history bind failed: %s", exc)
                return "fail", "❌ 历史解析失败，未修改。"
            compressor = LLMSummaryCompressor(
                provider=provider,
                keep_recent_ratio=keep_ratio,
                instruction_text=instruction,
            )
            try:
                new_messages = await compressor(messages)
            except Exception as exc:
                logger.error("Compress: LLM compression failed: %s", exc)
                return "fail", "❌ 压缩失败，未修改历史。"
            if new_messages is messages:
                return "short", "ℹ️ 对话太短（没有需要汇总的旧轮次），无需压缩。"
            new_history = dump_messages_with_checkpoints(new_messages)
            await conv_mgr.update_conversation(umo, cid, new_history)
            logger.info("[Compress] %s: %d -> %d messages", umo, before, len(new_history))
            return "ok", (
                f"✅ 压缩完成：{before} 条消息 → {len(new_history)} 条\n"
                "（保留最近上下文，下次请求生效）"
            )

    # ---------- natural-language admin trigger ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=900000)
    async def nl_compress(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("nl_enabled", True):
            return
        if not self._cfg_bool("enabled", True):
            return
        if not event.is_admin():
            return
        text = event.message_str or ""
        text = text.strip()
        if not text or text.startswith("/"):
            return
        if len(text) > 40:
            return  # short intent phrases only, avoid false positives
        if any(marker in text for marker in _SELF_MARKERS):
            return  # ignore our own replies

        patterns = self._cfg_list("nl_patterns", DEFAULT_NL_PATTERNS)
        if not any(re.search(p, text) for p in patterns):
            return

        umo = event.unified_msg_origin
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return
        keep_ratio = self._cfg_float("keep_recent_ratio", 0.15)
        try:
            await event.send(
                MessageChain([Plain(f"⏳ 正在压缩上下文（保留最近 {keep_ratio:.0%}）…")])
            )
        except Exception as exc:
            logger.debug("NL compress: status send failed: %s", exc)
        _, final = await self._do_compress(umo, cid, exclude_event=event)
        try:
            event.set_result(
                MessageEventResult()
                .message(final)
                .set_result_type(EventResultType.STOP)
            )
        except Exception as exc:
            logger.debug("NL compress: result set failed: %s", exc)

    # ---------- manual admin command ----------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("compress", alias={"压缩上下文", "summarize"})
    async def compress_command(self, event: AstrMessageEvent, _=None):
        if not self._cfg_bool("command_enabled", True):
            yield event.plain_result("ℹ️ 手动压缩命令已关闭。")
            return
        umo = event.unified_msg_origin
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            yield event.plain_result("❌ 无法访问会话管理器。")
            return
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            yield event.plain_result("❌ 当前没有对话。")
            return
        keep_ratio = self._cfg_float("keep_recent_ratio", 0.15)
        yield event.plain_result(f"⏳ 正在压缩上下文（保留最近 {keep_ratio:.0%}）…")
        _, final = await self._do_compress(umo, cid, exclude_event=event)
        yield event.plain_result(final)
