# astrbot_plugin_proactive_compress

AstrBot 后台主动上下文压缩插件：**上下文使用率超过阈值（默认 60%）时，后台静默压缩当前对话并原子替换历史，主对话永不停机**；管理员可用 `/compress` 手动兜底。复用 AstrBot 原生 `LLMSummaryCompressor` 模块。

## 设计：冗余替换消除上游竞态

AstrBot 自动压缩只在请求内（82% 阈值）触发，会在那一刻让该会话停顿。本插件把压缩从"主流程内阻塞"改成**旁路异步任务 + 原子替换**：

```
主对话:  [历史 60%]──增长──>[70%]──增长──>[80% 自动压缩兜底]
                          ↑ 无感知，永不停机
后台压缩: 快照@60% ──LLM压缩(不持锁,数秒)──> 完成
                                               ↓
                   短暂持锁原子替换(毫秒级) → [摘要+最近+期间新增]
```

- **竞态消除**：锁只在替换瞬间持有（读+写，毫秒），主对话的新消息在替换瞬间于锁外排队，替换后基于新历史继续——不丢、不串、不停机。
- **冗余合并**：压缩基于独立快照；替换时把压缩期间新增的轮次（快照 vs 当前历史的 diff）追加到结果尾部。
- **陈旧作废**：若压缩完成时历史已经越过快照（例如自动压缩先触发），结果直接丢弃，不写回。
- **去重/冷却**：每对话只允许一个后台压缩在飞，且有冷却间隔。
- **兜底**：80% 自动压缩保留为安全网；管理员 `/compress` 手动强制压缩。

## 安装

把插件目录放到 AstrBot `data/plugins/`，完整重启 AstrBot（或 Dashboard 插件管理里刷新加载）。

## 配置（Dashboard 插件设置）

| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| trigger_ratio | 0.6 | 上下文使用率达到该比例触发后台压缩 |
| min_messages | 20 | 少于该消息数不触发 |
| cooldown_seconds | 300 | 同一对话最小压缩间隔 |
| check_delay_seconds | 5 | LLM 回复后延迟检查秒数 |
| keep_recent_ratio | 0.15 | 压缩保留最近原文比例（0-0.3） |
| compress_instruction | 空 | 摘要提示词；留空用 `llm_compress_instruction` 或内置默认 |
| compress_provider_id | 空 | 压缩模型 ID；留空用 `llm_compress_provider_id` 或默认模型 |
| command_enabled | true | 允许 `/compress` 管理员命令 |

**建议**：把 `compress_provider_id` 指向一个有免费额度的轻量模型（如火山方舟标准 API 的 `deepseek-v4-pro-ga-260813`），压缩走免费额度、不占用主对话的付费订阅与缓存域。

## 命令（管理员）

```
/compress   （别名：/压缩上下文、/summarize）
```

管理员手动触发一次压缩（前台执行，持会话锁，安全）。

## 兼容性

- 最低 AstrBot 4.26.x（使用 `session_lock_manager`、`LLMSummaryCompressor`、`bind/dump_checkpoint_messages`）。
- 压缩请求经由 provider 发出，会出现在 AstrBot 日志的 `[VolcengineCache]` 缓存可见性行中。

## 许可

MIT
