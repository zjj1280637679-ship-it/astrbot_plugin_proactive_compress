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

Admin command:  /compress   (alias: /压缩上下文, /summarize)  — manual on-demand
"""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter

from astrbot.core.agent.context.compressor import LLMSummaryCompressor
from astrbot.core.agent.message import (
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.utils.session_lock import session_lock_manager

VERSION = "0.1.1"

DEFAULT_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary of key takeaways and/or project progress.\n"
    "1. Systematically cover all core topics discussed and the final conclusion/outcome for each; clearly highlight the latest primary focus.\n"
    "2. If any tools were used, summarize tool usage (total call count) and extract the most valuable insights from tool outputs.\n"
    "3. If any materials (files, documents, code, references) were read during the conversation that may be helpful for subsequent work, list each one with its scope and path.\n"
    "4. If there was an initial user goal, state it first and describe the current progress/status.\n"
    "5. Write the summary in the user's language.\n"
)


@star.register(
    "astrbot_plugin_proactive_compress",
    "羊魔大人",
    "后台主动上下文压缩：上下文使用率超过阈值（默认60%）时后台静默压缩并原子替换，主对话不停机；管理员可用 /compress 手动兜底。",
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
        value = self.config.get(key, default)
        return bool(value)

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

    # ---------- background trigger: after every LLM response ----------

    @filter.on_llm_response()
    async def maybe_compress(self, event: AstrMessageEvent, response: LLMResponse) -> None:
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
        # Cheap estimate: mixed CJK/ASCII text ~0.5 token per character.
        chars = sum(
            len(str(m.get("content", "")))
            for m in history
            if isinstance(m, dict) and m.get("role") != "system"
        )
        return int(chars / 2)

    def _resolve_provider(self, umo: str):
        ps: dict = {}
        try:
            cfg = self.context.get_config(umo=umo)
            ps = dict(cfg.get("provider_settings", {})) if cfg else {}
        except Exception:
            pass
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
            instruction = self._cfg_str(
                "compress_instruction", ""
            ) or self._read_compress_instruction(umo)
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

    def _read_compress_instruction(self, umo: str) -> str | None:
        try:
            cfg = self.context.get_config(umo=umo)
            ps = dict(cfg.get("provider_settings", {})) if cfg else {}
            return str(ps.get("llm_compress_instruction") or "").strip() or None
        except Exception:
            return None

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

    # ---------- manual admin command (foreground fallback) ----------

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

        provider = self._resolve_provider(umo)
        if provider is None:
            yield event.plain_result("❌ 未找到压缩用模型（检查 compress_provider_id / llm_compress_provider_id）。")
            return
        keep_ratio = self._cfg_float("keep_recent_ratio", 0.15)
        instruction = self._cfg_str("compress_instruction", "") or self._read_compress_instruction(umo)

        try:
            active_event_registry.request_agent_stop_all(umo, exclude=event)
        except Exception as exc:
            logger.debug("Manual compress: stop request failed: %s", exc)

        yield event.plain_result(f"⏳ 正在压缩上下文（保留最近 {keep_ratio:.0%}）…")

        async with session_lock_manager.acquire_lock(umo):
            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv:
                yield event.plain_result("❌ 未找到对话。")
                return
            try:
                history = json.loads(conv.history or "[]")
            except (TypeError, ValueError):
                history = []
            if not isinstance(history, list) or not history:
                yield event.plain_result("ℹ️ 当前对话没有可压缩的历史。")
                return
            before = len(history)
            try:
                messages = bind_checkpoint_messages(history)
            except Exception as exc:
                logger.error("Manual compress: history bind failed: %s", exc)
                yield event.plain_result("❌ 历史解析失败，未修改。")
                return
            compressor = LLMSummaryCompressor(
                provider=provider,
                keep_recent_ratio=keep_ratio,
                instruction_text=instruction,
            )
            try:
                new_messages = await compressor(messages)
            except Exception as exc:
                logger.error("Manual compress: LLM compression failed: %s", exc)
                yield event.plain_result("❌ 压缩失败，未修改历史。")
                return
            if new_messages is messages:
                yield event.plain_result("ℹ️ 对话太短（没有需要汇总的旧轮次），无需压缩。")
                return
            new_history = dump_messages_with_checkpoints(new_messages)
            await conv_mgr.update_conversation(umo, cid, new_history)
            logger.info(
                "[ManualCompress] %s: %d -> %d messages",
                umo,
                before,
                len(new_history),
            )
            yield event.plain_result(
                f"✅ 压缩完成：{before} 条消息 → {len(new_history)} 条\n"
                "（保留最近上下文，下次请求生效）"
            )
