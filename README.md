# astrbot_plugin_proactive_compress

AstrBot 后台主动上下文压缩插件：**上下文使用率超过阈值（默认 60%）时，后台静默压缩当前对话并原子替换历史，主对话永不停机**；管理员可用 `/compress` 手动兜底。复用 AstrBot 原生 `LLMSummaryCompressor` 和 `EstimateTokenCounter`。

## 设计：冗余替换消除上游竞态

AstrBot 原生 LLM 摘要压缩默认在请求内达到约 82% 阈值时触发。本插件把提前压缩从“主流程内阻塞”改成**旁路异步任务 + 原子替换**：

```text
主对话:  [历史 60%]──增长──>[70%]──增长──>[82% 原生压缩兜底]
                          ↑ 无感知，永不停机
后台压缩: 快照@60% ──LLM压缩(不持锁,数秒)──> 完成
                                               ↓
                   短暂持锁原子替换(毫秒级) → [摘要+最近+期间新增]
```

- **竞态消除**：锁只在替换瞬间持有（读+写，毫秒），主对话的新消息在替换瞬间于锁外排队，替换后基于新历史继续——不丢、不串、不停机。
- **冗余合并**：压缩基于独立快照；替换时把压缩期间新增的轮次（快照 vs 当前历史的 diff）追加到结果尾部。
- **陈旧作废**：若压缩完成时历史已经越过快照（例如原生压缩或手动压缩先触发），结果直接丢弃，不写回，也不会记录为成功。
- **后台唯一任务**：同一 UMO 同时只保留一个“延迟检查→压缩→换入”后台任务，避免高频回复产生重复并行压缩。
- **生命周期安全**：插件禁用或重载时取消并回收所有后台任务，避免旧实例继续写回。
- **多模态计数**：复用 AstrBot 原生 `EstimateTokenCounter`；文本、thinking、tool calls、图片和音频按各自策略估算，避免把 base64/data URL 当成普通文本导致误触发。
- **双冷却语义**：成功写回使用完整 `cooldown_seconds`；失败、空结果或陈旧结果只使用较短的 `retry_cooldown_seconds`。
- **兜底**：AstrBot 原生请求内压缩继续保留为安全网；管理员 `/compress` 可手动强制压缩。

## 安装

把插件目录放到 AstrBot `data/plugins/`，完整重启 AstrBot（或 Dashboard 插件管理里刷新加载）。

## 配置（Dashboard 插件设置）

| 项 | 默认 | 说明 |
|---|---:|---|
| enabled | true | 总开关 |
| trigger_ratio | 0.6 | 上下文使用率达到该比例触发后台压缩；运行时限制 0.1–0.95 |
| min_messages | 20 | 少于该消息数不触发 |
| cooldown_seconds | 300 | **成功写回后**的完整冷却时间 |
| retry_cooldown_seconds | 30 | 失败、空结果或 stale 后的短重试间隔 |
| check_delay_seconds | 5 | LLM 回复后延迟检查秒数 |
| keep_recent_ratio | 0.15 | 压缩保留最近原文比例；运行时限制 0–0.3 |
| compress_instruction | 空 | 摘要提示词；留空用 `llm_compress_instruction` 或内置默认 |
| compress_provider_id | 空 | 压缩模型 ID；留空用 `llm_compress_provider_id` 或默认模型 |
| command_enabled | true | 允许 `/compress` 管理员命令 |
| nl_enabled | true | 允许管理员自然语言短句触发 |
| nl_patterns | 内置 5 条 | 自然语言触发正则；无效正则会忽略并记录警告 |
| emphasis_instruction | 空 | 摘要时额外提高保留权重的内容 |

`compress_provider_id` 可以使用与主模型同级的独立压缩模型，也可以直接沿用 AstrBot 的 `llm_compress_provider_id`。插件不要求压缩模型必须更轻量。

## 命令（管理员）

```text
/compress   （别名：/压缩上下文、/summarize）
```

管理员手动触发一次压缩（前台执行，持会话锁，安全）。

## 测试闭环

测试不依赖真实 QQ/NapCat，也不调用真实付费模型。边界使用假的 `ConversationManager` 与 Provider，但消息模型、token 计数器、`LLMSummaryCompressor` 和会话锁来自固定版本的真实 AstrBot。

本地运行：

```bash
python -m pip install -r requirements-test.txt
python -m compileall -q main.py tests
ruff check main.py tests
pytest -q
```

当前自动化覆盖：

- 压缩进行中插入新消息，完成后验证新消息原样合流；
- 快照前缀被其它压缩器改写时，验证 stale 结果整份作废且不写库；
- 20 万字符的图片 data URL 不按普通文本 token 计数；
- 同一 UMO 高频触发时始终只有一个后台生命周期任务；
- 插件 `terminate()` 能取消并回收后台任务；
- 压缩失败只进入短重试冷却，成功写回进入完整冷却；
- 配置范围裁剪与错误正则不会打断处理。

`.github/workflows/test.yml` 会在 `main` push、Pull Request 和手动触发时，以 Python 3.12 + AstrBot 4.27.3 重跑同一套测试。

## 兼容性

- AstrBot `>=4.27,<5`。
- 依赖当前 AstrBot 的 `session_lock_manager`、`LLMSummaryCompressor`、`EstimateTokenCounter`、`bind_checkpoint_messages` / `dump_messages_with_checkpoints`。
- 压缩请求经由配置的 provider 发出，因此会正常出现在对应 provider 的请求日志中。

## 许可

MIT
