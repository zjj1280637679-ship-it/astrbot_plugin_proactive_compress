# CHANGELOG

## 0.2.1 (2026-08-18)

- 修复：`EventMessageType` 不从 `astrbot.api.event` 导出 → 改用 `filter.EventMessageType`。
- 修复：`MessageEventResult` 无 `set_result_type`（文档示例过时）→ 改用 `result.stop_event()`。
- 验证：在 AstrBot 运行时 venv 中完整加载插件模块（含全部装饰器）通过。

## 0.2.0 (2026-08-18)

- 新增：管理员自然语言触发压缩。管理员发送命中 `nl_patterns` 的短句
  （如"压缩一下上下文""总结我们的对话"）即触发，消息被拦截（STOP 结果），
  不会进入主对话 LLM。默认 5 组正则，可配置。
- 新增：压缩加权指令 `emphasis_instruction`。管理员可配置摘要中额外保留/
  重点覆盖的内容（如"之前遇到的问题及解决方案、未完成事项、用户偏好"），
  追加到压缩提示词，压缩模型按更高权重保留。
- 重构：压缩流程收敛到共享 `_do_compress` 核心（停 agent + 持锁 + 压缩 + 写回），
  `/compress` 与自然语言触发共用。
- 自然语言触发防误判：仅管理员、≤40 字、非 `/` 命令、非插件自身回复。

## 0.1.1 (2026-08-18)

- 修复：移除 `LLMResponse` 从 `astrbot.api.event` 的错误导入（该名称实际定义于
  `astrbot.core.provider.entities`；注解因 `from __future__ import annotations`
  为字符串，无需运行时导入）。修复后插件可正常加载。

## 0.1.0 (2026-08-18)

- 初始版本：后台主动上下文压缩。
  - 每 LLM 回复后延迟检查上下文使用率，超过 `trigger_ratio`（默认 60%）即后台压缩。
  - 复用 AstrBot 原生 `LLMSummaryCompressor`（轮次切分 / 摘要提示词 / sanitize / 保留最近）。
  - 原子替换：短暂持 per-UMO 会话锁，合并压缩期间新增消息，主对话不停机。
  - 冗余作废：压缩结果落后于快照时丢弃，不写回。
  - 每对话去重 + 冷却间隔。
  - 管理员 `/compress` 手动兜底（前台，持锁，安全）。
  - 兼容 `bind/dump_checkpoint_messages`，保留 WebChat checkpoint 记忆。
