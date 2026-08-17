# CHANGELOG

## 0.1.0 (2026-08-18)

- 初始版本：后台主动上下文压缩。
  - 每 LLM 回复后延迟检查上下文使用率，超过 `trigger_ratio`（默认 60%）即后台压缩。
  - 复用 AstrBot 原生 `LLMSummaryCompressor`（轮次切分 / 摘要提示词 / sanitize / 保留最近）。
  - 原子替换：短暂持 per-UMO 会话锁，合并压缩期间新增消息，主对话不停机。
  - 冗余作废：压缩结果落后于快照时丢弃，不写回。
  - 每对话去重 + 冷却间隔。
  - 管理员 `/compress` 手动兜底（前台，持锁，安全）。
  - 兼容 `bind/dump_checkpoint_messages`，保留 WebChat checkpoint 记忆。
