# CHANGELOG

## 0.3.0 (2026-08-18)

- 修复：后台触发的 token 估算改为复用 AstrBot 原生 `EstimateTokenCounter`，
  通过 `bind_checkpoint_messages` 后统一统计文本、thinking、tool calls、图片和音频；
  不再把多模态 base64/data URL 当普通字符串计算。
- 修复：后台检查与压缩合并为每 UMO 唯一的单一生命周期任务，消除
  “检查通过后、注册压缩任务前”可能重复进入的 TOCTOU 窗口。
- 修复：新增 `terminate()`，插件禁用/重载时取消并回收全部后台任务，
  避免旧插件实例延迟醒来继续压缩或写回。
- 修复：`_atomic_replace()` 改为返回是否真正写回；stale / 会话消失 /
  历史解析失败时不再记录“后台压缩成功”日志。
- 修复：冷却语义拆分为 `last_attempt` 与 `last_success`：
  - 成功写回才进入 `cooldown_seconds`（默认 300 秒）；
  - 失败、空结果、未变化或 stale 仅受 `retry_cooldown_seconds`
    （默认 30 秒）限制。
- 修复：模拟测试发现首次后台压缩会把“尚无冷却记录”误当成时间戳 0；
  现在只有真实存在 `last_success` / `last_attempt` 时才执行冷却判断，
  启动后的第一次符合阈值压缩不会被错误跳过。
- 修复：`trigger_ratio` 运行时强制限制为 0.1–0.95，
  `keep_recent_ratio` 强制限制为 0–0.3；负数延迟/冷却按 0 处理。
- 修复：管理员自然语言触发中的无效正则会被忽略并记录警告，
  不再可能因为 `re.error` 打断消息处理。
- 元数据：版本升级至 0.3.0，声明 `astrbot_version: ">=4.27,<5"`；
  移除错误的仅 `aiocqhttp` 平台限制（本插件本身与平台适配器无关）。
- 测试：新增固定 AstrBot 4.27.3 的 pytest 模拟集成闭环与 GitHub Actions；
  覆盖压缩期间增量消息合流、stale 作废、多模态 token、后台任务唯一性、
  `terminate()`、失败短冷却/成功长冷却和配置边界。最终 8 项测试通过。
- 文档：明确多模态计数、唯一后台任务、双冷却语义、生命周期行为和测试方法。

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
