# AI 上下文解析触发、有效性、窗口与成本分析

> 日期：2026-08-31
>
> 性质：只读生产数据分析，不是改造方案的实施记录
>
> 边界：本次未修改代码、settings、白名单、词表、阈值或数据库；未重启、未部署、未执行 Deepcoin 写入。

## 1. 结论摘要

1. 35 个日历日内有 4,245 条上下文解析 attempt，对应 2,928 条不同原始消息、5,923 次 provider 请求。在同期 6,163 条原始消息中，曾触发上下文解析的消息占 **47.51%**。
2. `multiple_same_source_candidates` 出现 3,906 次，覆盖 **92.01%** 的 attempt，是绝对主因。但它不能直接整体关闭：“仅该触发器”仍有 124 次实质改变。
3. 在 2,767 条有可比较最终决策的 attempt 中，580 条（**20.96%**）实质改变了第一层结论，2,187 条（**79.04%**）未改变。以全部 4,245 条 attempt 为分母，实质改变产出率为 **13.66%**；其余 86.34% 是未改变或未留下可比较决策。
4. 每条持久化请求平均 77.23 KB，其中 `message_context` 平均 68.86 KB，占 **89.16%**；历史消息本身平均 60.78 KB。上下文最大的成本来源是历史消息，不是 reply 链。
5. 盲目收缩为 20 条 / 24 小时，历史回放可将平均持久化请求从 77.23 KB 降到 38.03 KB，加权节省约 **50.75%**；但 580 次真正改变决策的调用中，72 次（**12.41%**）会丢失模型明确引用的支持/反对消息。因此该简单收缩不安全。
6. 上下文解析的 35 日混合均值约为 **3.51–4.67M 输入 token/日**；剔除 8 月 21–23 日网络错误造成的密集双次重试后，正常日代理值约 **1.67–2.22M/日**。主识别按近期 218 条消息/日估算为 **4.3–6.2M 输入 token/日**。这是 UTF-8 字节代理，不是 provider 账单。
7. 在同一不可变备份中，`context_resolution_attempts` 主 B-tree 为 333.951 MB，8 个索引合计 0.741 MB，合计 **334.692 MB，占全库 41.44%**。六个 JSON 列为 329.466 MB，占表本体 98.66%，日增约 9.41 MB。
8. 2026-08-31 陈旧 lifecycle/binding 清理晚于本数据集最后一条 attempt；清理后 30 分钟观测窗口内又没有新消息。因此**没有清理后的触发率分母，不能宣称触发率已下降**。

## 2. 数据源、口径与限制

### 2.1 生产数据源

所有数据库查询均使用生产维护证据中的不可变备份，以 SQLite `-readonly` 方式直接查询：

- 备份：`/var/lib/telegram-kol-maintenance-evidence/unified-claim-alignment-00cda060-20260831T143155Z/research-before.db`
- 字节数：807,567,360（770.156 MiB）
- SHA-256：`d8b1ebd73da9bb2da2af10e1094adad1a0d19d0311a74b1f6d21b5b8eca96a27`
- `page_size=4096`，`page_count=197160`，`freelist_count=0`
- attempt 时间：2026-07-27 09:38:38.230117 至 2026-08-30 01:17:57.357331，横跨 35 个日历日

结构、触发语义和窗口构建逻辑以当前代码为准：

- `src/telegram_kol_research/authoritative_recognition.py`
- `src/telegram_kol_research/contextual_message_window.py`
- `src/telegram_kol_research/context_resolution.py`
- `src/telegram_kol_research/context_resolution_prompt.py`

### 2.2 `reanalysis_triggers_json` 的实际语义

`reanalysis_triggers_json` 不是本次调用前的 8 个确定性触发器。它持久化的是上下文模型返回的“未来在什么事件下重分析”类型，例如 `strategy_state_changed`。

因此，本文的 8 触发器分布不是直接枚举该列，而是对每条 `request_summary_json` 使用当前 `requires_context_resolution()` 逻辑进行精确重建。数据中无无效 request JSON，且每条 attempt 都至少重建出一个触发器。

### 2.3 “实质变化”定义

对比第一层结果与 `decision_json`，构建如下语义签名：

- 动作族：`new` / `no_action` / `manage` / `cancel` / `exit` / `revise`；
- 目标 lifecycle / strategy；
- 决策是否可应用；
- 可静态确定的风险例外。

理由文字和 confidence 数值的变化不算实质变化；同一 `manage` 动作族内的更细 management action 变化也未计入。因此，本文的“改变”是保守下界，“不变”是上界。

## 3. 触发分布

### 3.1 8 个触发器

分母为 4,245 条 attempt。一条 attempt 可同时命中多个触发器，因此占比之和大于 100%。

| 触发器 | 次数 | attempt 占比 |
|---|---:|---:|
| `multiple_same_source_candidates` | 3,906 | 92.01% |
| `management_without_exact_target` | 533 | 12.56% |
| `entered_holder_language` | 515 | 12.13% |
| `revision_language` | 208 | 4.90% |
| `text_image_conflict` | 93 | 2.19% |
| `apparent_entry_may_be_revision` | 70 | 1.65% |
| `cancellation_language` | 17 | 0.40% |
| `reply_target_disagreement` | 7 | 0.16% |

### 3.2 单触发与多触发

| 同时命中数 | attempt 数 | 占比 |
|---:|---:|---:|
| 1 | 3,306 | 77.88% |
| 2 | 780 | 18.37% |
| 3 | 153 | 3.60% |
| 4 | 6 | 0.14% |

没有 0 触发器的 attempt，也没有同时命中 5 个及以上的样本。

### 3.3 触发率与 2026-08-31 清理前后对比

清理前的完整历史口径：

- 同期原始消息：6,163 条；
- 曾触发上下文解析的不同原始消息：2,928 条；
- 按消息计算的触发率：**47.51%**；
- attempt 行：4,245，其中 1,317 行是同一消息的重分析/新 fingerprint；
- provider 请求：5,923 次，即 169.23 次/日、1.395 次/attempt。

2026-08-31 14:41:14Z 终结 35 个陈旧 lifecycle 和 46 个陈旧 binding。但数据集中最后一条 attempt 为 2026-08-30 01:17:57，早于清理。清理后 2026-08-31T16:12:36.995Z–16:42:37Z 的 30 分钟窗口内是 0 条新消息、0 次 AI 调用。

**所以清理后样本不是“少”，而是没有分母，无法计算触发率，也无法得出清理已降低触发率的结论。**

仅作历史反事实上界：4,245 条 attempt 中，1,101 条包含后来被清理的 lifecycle 候选；如从当时窗口删去这些候选，320 条会失去 `multiple_same_source_candidates`，其中最多 232 条在其他简单触发器上也不再命中，历史对应 325 次 provider 请求，即最多约 9.29 次/日、占历史 provider 请求 5.5%。从请求 JSON 删去这些 candidate/active 项的加权字节节省约 1.25%。这是上界估算，不是清理后观测结果。

## 4. 上下文解析的有效性

### 4.1 总体改变率

数据质量：

- attempt 总数：4,245；
- `decision_json` 为空：1,478；
- 有效可比较决策：2,767；
- 无效 request JSON：0；
- 无效非空 decision JSON：0。

| 口径 | 实质改变 | 结论不变/无可比较决策 | 改变率 |
|---|---:|---:|---:|
| 仅 2,767 条有决策 attempt | 580 | 2,187 | 20.96% |
| 全部 4,245 条 attempt | 580 | 3,665 | 13.66% |
| 仅 `completed` 的 2,560 条 | 457 | 2,103 | 17.85% |

第二行中 3,665 包含“结论不变”和“调用没有留下决策”，因此 13.66% 是每条已记录 attempt 的实际改变产出率，不能与 20.96% 混用。

状态分布也说明了为什么有 1,478 条无决策：

| 状态 | attempt 行 | provider 请求 | 留下决策 |
|---|---:|---:|---:|
| `completed` | 2,560 | 2,734 | 2,560 |
| `exhausted` | 1,439 | 2,901 | 25 |
| `superseded` | 178 | 198 | 178 |
| `failed` | 68 | 90 | 4 |

### 4.2 按触发器分组

同一 attempt 可进入多行。“有决策改变率”的分母只是非空决策；“全部触发产出率”以所有该触发器 attempt 为分母，把失败/无决策也算入成本。

| 触发器 | 触发 attempt | 有决策 | 实质改变 | 有决策改变率 | 全部触发产出率 |
|---|---:|---:|---:|---:|---:|
| `multiple_same_source_candidates` | 3,906 | 2,508 | 474 | 18.90% | 12.14% |
| `management_without_exact_target` | 533 | 380 | 377 | 99.21% | 70.73% |
| `entered_holder_language` | 515 | 380 | 119 | 31.32% | 23.11% |
| `revision_language` | 208 | 130 | 22 | 16.92% | 10.58% |
| `text_image_conflict` | 93 | 48 | 8 | 16.67% | 8.60% |
| `apparent_entry_may_be_revision` | 70 | 59 | 23 | 38.98% | 32.86% |
| `cancellation_language` | 17 | 11 | 4 | 36.36% | 23.53% |
| `reply_target_disagreement` | 7 | 7 | 4 | 57.14% | 57.14% |

最接近“几乎从不改变结论”的组合不是整个 `multiple_same_source_candidates`，而是“仅该触发器，没有其他简单触发”：

- 3,005 条 attempt；
- 4,273 次 provider 请求；
- 1,885 条有决策；
- 124 条实质改变；
- 有决策改变率 6.58%，全 attempt 改变产出率 4.13%。

`text_image_conflict` 的全 attempt 改变产出率也只有 8.60%，`revision_language` 为 10.58%。但这些组都不是零，尤其“仅 multiple”仍有 124 次实质改变，因此历史数据不支持直接关闭。

## 5. 上下文窗口的实际规模

### 5.1 字节构成

以持久化 JSON 的 UTF-8 字节数为准；这是线上 request 大小的最佳现有代理，不是 provider tokenizer 结果。

| 组件 | 平均 | P50 | P90 | P95 | 最大 | 其他 |
|---|---:|---:|---:|---:|---:|---:|
| 整个 request | 77,231.90 B | 78,850 | 120,437 | 137,262 | 161,598 | — |
| `message_context` | 68,859.88 B | 70,337 | 106,053 | 121,840 | 142,493 | 占 request 89.16% |
| `recent_messages` | 60,781.81 B | 63,102 | 98,609 | 104,380 | 122,945 | 占 request 78.70% |
| active strategies | 6,088.85 B | 5,500 | 13,057 | 13,972 | 15,193 | — |
| candidates | 3,561.36 B | 2,116 | 10,551 | 10,638 | 11,836 | — |
| first pass | 1,561.40 B | 1,510 | 2,212 | 2,432 | 5,029 | — |
| saved evidence | 1,482.19 B | — | — | — | — | — |

`message_context` 合计 292,310,206 B，其中 `recent_messages` 合计 258,018,789 B。因此窗口优化的主要对象是历史消息文本与附属元数据。

### 5.2 条数和上限利用率

| 窗口项 | 上限 | 平均 | P50 | P90 | P95 | 最大 | 达上限 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 历史消息 | 50 | 36.71（73.42%） | 42 | 50 | 50 | 50 | 1,677（39.51%） |
| reply 链 | 5 | 0.07（1.40%） | 0 | 0 | 1 | 3 | 0 |
| active strategies | 50 | 20.42（40.84%） | 19 | 43 | 46 | 50 | 70（1.65%） |
| candidates | 20 | 6.26（31.30%） | 4 | 20 | 20 | 20 | 539（12.70%） |

72 小时年龄上限的实际利用：

- 最旧 `recent_message` 平均年龄 56.95 小时，为上限的 79.10%；
- P50 62.58 小时（86.92%），P90 71.52，P95 71.75，最大 72；
- 4,240 条可计算年龄的 attempt 中，3,975 条（**93.64%**）包含超过 24 小时的历史。

reply 深度上限 5 从未命中，实际最大只有 3，但 reply 本身的平均字节规模很小，收紧它的节省也很小。

## 6. 20 条 / 24 小时收缩回放

对每条已持久化窗口执行不改变排序规则的静态截断：

| 指标 | 现行窗口 | 20 条 / 24h | 变化 |
|---|---:|---:|---:|
| 历史消息平均条数 | 36.71 | 13.10 | -64.32% |
| 历史消息 P50 | 42 | 14 | — |
| 历史消息 P90/最大 | 50/50 | 20/20 | — |
| 假设 request 平均 | 77,231.90 B | 38,033.72 B | -39,198.18 B |
| 假设 request P50 | 78,850 B | 37,680 B | — |
| 假设 request P90 | 120,437 B | 60,911 B | — |
| 假设 request 最大 | 161,598 B | 89,668 B | — |

按“总删减字节 / 总原字节”计算，节省为 **50.75%**；先对每条计算百分比再取平均则为 48.34%。

对 580 次实质改变决策的调用，以决策中明确持久化的 supporting/opposing message ID 为依赖代理：

| 收缩方式 | 会丢失至少一条引用消息的改变决策 | 占 580 次改变 |
|---|---:|---:|
| 仅限 20 条 | 37 | 6.38% |
| 仅限 24 小时 | 70 | 12.07% |
| 20 条 + 24 小时 | 72 | 12.41% |

580 次改变中，463 次留下了 supporting/opposing ID，117 次没有；另有 1 个引用 ID 无法在当次持久化窗口中匹配。模型引用是当前最强的历史依赖代理，但它不是因果证明；没有引用也不等于没有依赖。因此 12.41% 是可观测的明确风险，不是风险上界。

## 7. Token 成本估算

### 7.1 方法

当前表未持久化 provider 的 prompt/completion token usage，因此不能还原账单。估算方法是：

1. 对持久化 request 及实际 system prompt 计算 UTF-8 字节；
2. 用 3–4 UTF-8 字节/token 给出范围，而不伪造单点精度；
3. 输出只按持久化 decision JSON 估算，不包含未持久化的失败 raw response；
4. 图片 token、provider 内部 thinking token、cache 命中与计价差异都未计入。

### 7.2 上下文解析

- 上下文 system prompt：2,455 B；前缀：67 B；
- 按 provider 调用次数加权的 request：80,351.73 B/次；
- 含 system prompt 后：约 82,873.73 B/次；
- 历史 5,923 次 / 35 日 = 169.23 次/日；
- 输入约 14,024,603 B/日 = **3.51–4.67M token/日**；
- 成功 decision JSON 平均约 493 B，可观测输出约 0.02–0.03M token/日。

35 日均值受 8 月 21–23 日网络故障强烈污染：1,367 条 `network_error` exhausted attempt 产生 2,734 次 provider 请求，每条恰好重试两次。剔除这些行后：

- 2,874 条 attempt，3,185 次 provider 请求；
- 82.11 attempt/日，91 provider 请求/日；
- 73,303 B/请求，6,670,586 B/日；
- 正常日代理值约 **1.67–2.22M 输入 token/日**。

这个正常日值仍然是历史代理，不是 2026-08-31 清理后实测；清理后没有足够样本。

### 7.3 主识别

当前主识别也在调用前构建完整上下文。主识别没有持久化完整 wire request，所以只能用上下文 attempt 中的窗口大小作代理：

- 共享 system prompt 约 7,450 B，MiMo vision 附加 prompt 约 1,401 B，连同分隔符合计约 8,853 B；
- 窗口代理平均 68.9 KB，加当前消息和元数据后估计 78–85 KB/调用；
- 按近期 218 条消息/日、稳定日 prompt invocation 约为消息数 1.01 倍估算，输入约 17–19 MB/日；
- 折算为 **4.3–6.2M 输入 token/日**；
- 已持久化 authoritative output 平均约 776–873 B，对应可观测输出约 0.04–0.07M token/日。

主识别估算的不确定性高于上下文解析：只有已触发第二层的消息才留下完整 request 样本，它们的窗口可能比未触发消息更大；图片 token 也没有计入。

### 7.4 合并看每日成本

| 阶段 | 正常日输入 token 代理 | 35 日故障混合口径 | 置信度 |
|---|---:|---:|---|
| 主识别 | 4.3–6.2M/日 | 未单独还原 | 中低：没有 wire usage |
| 上下文解析 | 1.67–2.22M/日 | 3.51–4.67M/日 | 中：有 request 字节，无 tokenizer usage |
| 合计 | 5.97–8.42M/日 | 不建议混合相加 | 中低 |

正常日口径下，上下文解析约占两层文本输入的 26%–28%；在网络故障混合均值中会接近 45%。这些比例同样不包图片和 thinking token。

## 8. 对数据库体积的贡献

### 8.1 物理体积与全库占比

在与 4,245 条 attempt 完全同时点的不可变备份中：

| 项目 | 字节 | MB（10^6） | 占 807,567,360 B 全库 |
|---|---:|---:|---:|
| 表主 B-tree | 333,950,976 | 333.951 | 41.35% |
| 8 个索引 | 741,376 | 0.741 | 0.09% |
| 表 + 索引 | 334,692,352 | 334.692 | **41.44%** |
| 六个 JSON 的逻辑字节 | 329,466,347 | 329.466 | 40.80% |

六个 JSON 占表主 B-tree 的 98.66%，占表+索引的 98.44%，平均 **77,612.8 B/行**（十进制 77.6 KB，二进制 75.8 KiB）。具体构成：

| JSON 列 | 字节 | 占六列 |
|---|---:|---:|
| `request_summary_json` | 327,849,401 | 99.51% |
| `decision_json` | 1,364,177 | 0.41% |
| `prompt_versions_json` | 195,270 | 0.06% |
| `trigger_event_json` | 32,708 | 0.01% |
| `reanalysis_triggers_json` | 19,060 | <0.01% |
| `rejected_response_diagnostic_json` | 5,731 | <0.01% |

另一个 2026-08-31 运维时点的生产实测为：全库 814,436,352 B，`context_resolution_attempts` 主表 334,036,992 B，占 41.01%。该口径与不可变备份的 41.35% 差异只有 0.34 个百分点；“生产库约 814 MB、该表约 334 MB”的量级是正确的，但不应把不同时点的分子和分母混在一个精确百分比里。

### 8.2 日增速率

4,245 行横跨 35 个日历日：

- 121.29 行/日；
- 六 JSON 合计 9,413,324.2 B/日，即 **9.41 MB/日（8.98 MiB/日）**；
- 表+索引总体按同期线性摊分为 9,562,638.6 B/日，即 **9.56 MB/日**。

这是全期实测平均，不是未来增长保证。请求量会受消息量、重分析和故障重试影响。

### 8.3 与日常管理审计 1.2 GB 内存峰值的关系

生产证据已确认，日常管理审计先构建两个完整私有 SQLite 快照，若两者不同则回退到一次完整 SQLite online copy。HTTP 源完成后的 cgroup 峰值约 1.2 GB，与整库拷贝及 page cache 相符。

`context_resolution_attempts` 占整库约 41%，因此它会按同样比例放大每次整库快照的 I/O 和可能的 page-cache 压力。但不能把 1.2 GB 中的 41% 直接归因为该表驻留内存：

- 峰值是整库复制、页缓存和审计进程共同结果；
- 另一个大表 `pending_tpsl_snapshot_observations` 已占 281,985,024 B；
- 实际 RSS/cgroup 下降取决于 SQLite 快照实现、OS page cache 和并发时序，不会与文件缩小一对一。

可量化的是复制字节：如果某个裁剪方案经过数据库重写后将文件实际缩小 277 MB，两个完整快照每次将少处理约 554 MB 数据；但必须用生产副本重放才能把它换算为可信的内存峰值降幅。

### 8.4 存量裁剪方案（本文仅分析，未执行）

以备份时点 2026-08-31 14:39:29 为基准，“保留 N 天”意味着裁剪早于该截止时间的存量。

#### 选项 A：保留行和小 JSON，只裁剪 `request_summary_json`

| 保留期 | 可裁剪行 | 逻辑可删 `request_summary_json` | 按当前物理/逻辑比例估算的缩库上限* | 两快照少处理字节* |
|---|---:|---:|---:|---:|
| 7 天 | 3,658 | 273.017 MB | 约 277.35 MB | 约 554.69 MB |
| 14 天 | 1,547 | 85.138 MB | 约 86.49 MB | 约 172.98 MB |
| 30 天 | 242 | 10.388 MB | 约 10.55 MB | 约 21.11 MB |

保留：attempt ID、原始消息关联、状态、重试次数、时间、错误、`decision_json`、未来 reanalysis trigger 和 trigger event。
损失：当时完整窗口、first pass、candidate/active strategy 快照、saved evidence 和其他 request 细节；无法按当时输入做完整离线回放或重建 8 触发器。

#### 选项 B：保留行，裁剪六个 JSON

| 保留期 | 逻辑可删六 JSON | 比只删 request 多回收 | 估算缩库上限* |
|---|---:|---:|---:|
| 7 天 | 274.308 MB | 1.292 MB | 约 278.66 MB |
| 14 天 | 85.947 MB | 0.808 MB | 约 87.31 MB |
| 30 天 | 10.500 MB | 0.112 MB | 约 10.67 MB |

相对选项 A 只多回收 0.1–1.3 MB，却额外损失最终决策、模型 reanalysis 意图和拒绝诊断。从空间/可追溯性性价比看，不如只裁剪 `request_summary_json`。

#### 选项 C：整行删除

整行删除的最低可回收量不少于选项 B 的 JSON 字节，还可多回收行头、标量列和索引页。但现有只读数据不能在不重写副本的情况下精确计算整行删除后的 B-tree 页数，不应伪造比选项 B 多几 MB 的精度。

损失最大：连 attempt 是否发生、重试过程、错误和最终决策都无法从本表追溯。不建议作为第一选择。

#### 选项 D：压缩归档后再裁剪

先将超期 `request_summary_json` 按日/周生成带行 ID、hash 和 manifest 的压缩只读归档，校验可恢复后再按选项 A 裁剪主库。主库可回收量与选项 A 相同，归档文件仍占额外空间，具体压缩比没有在本轮实测，不给假数字。

优点是保留完整追溯与离线回放能力；代价是增加归档、校验、恢复和保留期管理的复杂度。

\* SQLite 中把 JSON 更新为 `NULL`/空值后，主库文件通常不会立即缩小；页可能只是变成库内可复用空间。表中的“缩库上限”用当前（表+索引）/六 JSON 物理比 1.015862 估算；只有在生产副本上执行合适的安全重写/VACUUM 演练后，才能确定真正的文件回收量和峰值影响。本轮没有执行任何这类操作。

## 9. 优化选项（按优先级）

识别逻辑是交易核心。除可观测性外，下列任何改动都只是建议，不是授权实施。

| 优先级 | 选项 | 预期节省 | 实施风险 | 不改变系统决策的验证方式 | 类型 |
|---:|---|---|---|---|---|
| P0 | 清理后先观测至少 7 个正常日或 500 条消息 | 直接节省 0；历史反事实上界为 9.29 provider 请求/日、请求字节 1.25% | 很低，只读度量 | 固定分母，同时记录消息数、attempt、provider call、8 触发器和改变率；与清理前同口径对比 | 运维观测，无配置/代码变更 |
| P1 | 持久化真实 8 触发器、阶段、attempt/provider usage 和组件字节 | 直接节省 0；使后续节省可精确归因 | 低，但是 schema/写路径增量 | 只增字段，旧决策链不读新字段；影子期对比计数与 provider 账单 | 需改代码/可能需 schema |
| P2 | 为网络错误增加熔断/指数退避，避免立即重发大 prompt | 本历史中最多可避免 1,367 次第二次请求，占 5,923 次的 23.08%；收益集中在故障日 | 中：174 条 completed attempt 是第二次才成功，不能粗暴取消所有重试 | 故障注入比较新旧最终决策；保留延时 probe、不把未知当成 no-action，并验证 174 个第二次成功样本不丢失 | 需改代码 |
| P3 | 精炼“仅 multiple candidate”触发条件 | 理论涉及 4,273/5,923 provider 请求，但在证明召回率前安全节省为未知 | **高**：该组有 124 次实质改变 | 对 3,005 个历史样本回放，新 gate 对 124 个改变样本必须 100% 召回；生产影子期运行旧决策、只记新 gate 差异 | 需改代码/词汇或阈值属决策变更 |
| P4 | 做“相关性保留”的窗口压缩，不盲目改成 20/24 | 历史 request 字节理论上限约 50.75% | **高**：盲目 20/24 会影响至少 72/580 个改变决策 | 必须保留 direct reply、candidate/lifecycle root、已引用消息和 72 个风险样本；历史回放后再进行旧窗口/新窗口双路影子，比对动作、目标和可应用性 | 需改代码 |
| P5 | 收紧无关新消息导致的 reanalysis | 重分析占 2,424/5,923 provider 请求（40.93%）；仅 `next_same_chat_message` 上限为 217 次（3.66%、约 6.2 次/日） | 中高：可比较 `next_same_chat` 样本中有 2 次改变 | 按 trigger event 回放，保留这 2 次；影子记录“新消息是否真与候选关联”及新旧决策差异 | 需改代码 |
| P6 | 主识别首轮仅发当前消息/图片/直接 reply，需要时再发完整上下文 | 理论可节省约 3–5M 输入 token/日，但精确值未持久化 | **最高**：会直接改变第一层输入和触发基础 | 只能双路影子运行，旧路继续权威；按动作、目标 lifecycle、风险参数和最终订单投影做零差异门槛 | 需改代码，最高风险 |

关于 reanalysis 的补充数据：266 条带 trigger event 的 attempt 中，`next_same_chat_message` 为 163 行/217 次 provider call，exchange event 为 77/89，strategy state 为 21/26。与同一消息的上一决策可比较时，`next_same_chat_message` 51 个样本改变 2 个（3.92%），exchange event 40 个改变 5 个（12.5%），strategy state 16 个改变 0 个。样本不大，只能用来定义影子验证重点，不足以直接删除 reanalysis 路径。

## 10. 纯配置改动与代码改动的边界

### 10.1 现有纯配置手段

| 配置手段 | 能否节省 | 风险/结论 |
|---|---|---|
| `context_resolution_enabled=false` | 可节省所有第二层调用 | 会改变至少 580 个历史决策；不建议 |
| 收窄 `context_resolution_live_chat_ids` | 按被排除 chat 的流量节省 | 会直接改变这些 chat 的决策路径；当前白名单覆盖 33/34 个已启用 chat，现实上几乎不门控 |
| 更换模型/价格配置 | 可能降低金额，不降低 token 量 | 模型变更也可改变决策，需同等最高风险验证 |

因此，**当前没有一个“只改 settings、既有显著节省又能从数据上证明不改决策”的选项**。

### 10.2 需要代码/schema/运维数据政策的选项

- 窗口 50 条 / 72 小时 / reply 5 层 / active strategy 50 条的限制：需改代码，不是现有 settings；
- 8 触发器词汇、multiple candidate 逻辑、网络熔断、reanalysis 关联、主识别输入拆分：都需改代码；
- provider usage/触发器持久化：需改代码，并可能需 schema 变更；
- `context_resolution_attempts` 保留期、JSON 归档/裁剪：属于数据保留政策与受控数据操作，不是现有纯 settings；如要自动化才需增加代码/作业。

## 11. 建议的决策顺序

1. 先获得清理后有分母的真实触发率，不用反事实上界代替观测。
2. 先补 usage/触发器可观测性，再谈精确 token 账单和触发器 ROI。
3. 先处理故障日的重复大 prompt，因为它的节省不需假设某类交易语义没用。
4. 窗口优化必须是“保留相关证据”，不能直接改数字；20/24 的 12.41% 明确引用损失已足以否定盲目上线。
5. 存量体积优先考虑“保留行和 decision，只裁剪/归档过期 `request_summary_json`”；六列全裁剪的额外空间收益很小，却会丢失更多追溯信息。

任何进入实施的方案，都应保留现行路径为权威，先做历史回放和生产影子对比；在动作、目标 lifecycle、风险参数和最终订单投影上没有达到事先定义的零差异/全召回门槛前，不应接管交易决策。

## 12. P0 观测口径与首轮结果

### 12.1 固定窗口与数据完整性

本轮将恢复观察窗口结束时的原始消息水位 `14158` 作为开区间边界，并固定本轮截止时间，避免多条查询使用滑动分母：

- 开始：`2026-08-31T16:42:37Z`；
- 截止：`2026-08-31T21:35:00Z`；
- 时长：4 小时 52 分 23 秒（0.203 个 24 小时日）；
- 原始消息口径：`raw_messages.id > 14158 AND created_at <= '2026-08-31 21:35:00'`；
- 本轮截止时的最大 raw message ID：`14168`；
- 新消息共 10 条，ID 连续为 `14159`–`14168`；10 条的 `created_at` 和 `posted_at` 均晚于窗口起点，没有把维护期旧消息或迟到旧帖算入分母。

所有 SQLite 查询使用 `sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db`，核心链路查询使用同一个只读 `BEGIN/COMMIT` 快照。交易所证据使用 worker 进程的只读 exchange snapshot 与“当前委托” GET 端点；未调用任何 POST、取消、下单或重放端点。

### 12.2 运行状态与身份

2026-08-31T21:44:28Z 的进程自证结果：

| role | PID | systemd 状态 | release | artifact | entry freeze | role-specific health |
|---|---:|---|---|---|---|---|
| web | 3,746,349 | active/running，`NRestarts=0` | `6e2321cecbb3adf61d7a5972d391e662d4aea300` | verified | `false` | event loop `true` |
| ingest | 3,746,355 | active/running，`NRestarts=0` | 同上 | verified | `false` | event loop、live listener、reconcile 均 `true` |
| worker | 3,746,343 | active/running，`NRestarts=0` | 同上 | verified | `false` | event loop、worker command、message processing 均 `true` |

三个进程的 manifest SHA-256 均为 `4d011a9569dde31468db08cce20e1ce4e6570fad3a828f86d20db7832444cbb7`，`loaded_cwd` 均为 `/opt/telegram-kol-analyzer`。systemd 显示三个进程均于主机时间 2026-09-01 00:09:59（UTC `16:09:59Z`）启动，早于本 P0 窗口；当前 PID 与 16:42Z 恢复结束证据一致，因此窗口内无 PID 漂移。

worker 的 management、break-even、reconcile、close、TPSL、protection 和 rescue 循环全部 `fresh=true, successful=true`，management/close/TPSL/rescue 的有效权限均为 `true`，`global_exchange_authority=true`。设置端点实测 `auto_trade_enabled=true`，`worker_command_mode=queue`。

| role | P50 | P95 | P99 | 近期最大 | stall count | watchdog |
|---|---:|---:|---:|---:|---:|---|
| web | 1.260 ms | 1.764 ms | 2.264 ms | 4.543 ms | 0 | attached |
| ingest | 1.296 ms | 1.788 ms | 2.231 ms | 10.383 ms | 0 | attached |
| worker | 0.878 ms | 6.299 ms | 24.336 ms | 312.744 ms | 0 | attached |

web/ingest 的 worker-only 健康位为 `false`，worker 的 ingest-only 健康位为 `false`，这是进程角色隔离，不是循环故障。

### 12.3 monitor 自 16:42Z 起的每个周期

| 周期开始（UTC） | release 自证 | healthy | reason codes | monitor error | audit ran |
|---|---|---|---|---|---|
| 17:01:48 | `6e2321ce...`, verified | true | `[]` | null | false |
| 17:31:43 | 同上 | true | `[]` | null | false |
| 18:00:18 | 同上 | true | `[]` | null | false |
| 18:30:12 | 同上 | true | `[]` | null | false |
| 19:01:21 | 同上 | true | `[]` | null | false |
| 19:31:57 | 同上 | true | `[]` | null | false |
| 20:01:13 | 同上 | true | `[]` | null | false |
| 20:31:53 | 同上 | true | `[]` | null | false |
| 21:00:27 | 同上 | true | `[]` | null | false |
| 21:30:19 | 同上 | true | `[]` | null | false |

10 个完整周期均加载同一 release，无 reason code，没有 SHA 漂移。

### 12.4 真实消息到执行的链路

| 阶段 | 消息数 | 说明 |
|---|---:|---|
| 新原始消息 | 10 | raw ID `14159`–`14168` |
| 进入主识别 | 10 | 10 条均有 `message_recognitions`、authoritative MiMo run 和 `recognition_decisions` |
| 队列 `succeeded` | 10 | 全部 `worker_completed` |
| 产生 signal candidate | 1 条消息 / 1 条 candidate | raw `14166`，candidate `2123` |
| 走 `expired` | 0 | — |
| 仍 `pending/claimed` | 0 | — |

首条恢复后完整交易链路已实际出现，不再是零消息推断：

`raw_message 14166`（posted `19:31:18Z`）→ authoritative recognition `是策略`→ signal candidate `2123`（BTC short, confidence 0.95）→ lifecycle `1037`→ binding `321`→ execution events `3878/3879`→ Deepcoin 当前触发委托精确回读。

lifecycle `1037` 的 `signal_candidate_id` 为空，因此 candidate 到 lifecycle 的追溯使用了精确 `(chat_id, message_id)=(-1002370796392, 3605)` 以及 binding 反向关联，而不是直接外键。这不改变本次交易所一致性结论，但是后续追溯时需保留的数据质量注记。

### 12.5 Deepcoin 写入与 lifecycle/binding 对账

窗口内有 2 笔 Deepcoin 写入，均来自 raw message `14166`。该消息的 `created_at=2026-08-31T19:31:19.021891Z`，比恢复窗口起点晚 2 小时 48 分 42 秒，不是维护期历史消息的重放。

| event | symbol/方向 | 数量 | 触发/委托价 | raw message | 下单时间（UTC） | order ID | 当前交易所状态 |
|---|---|---:|---:|---:|---|---|---|
| 3878 | BTC-USDT-SWAP / sell short | 7 张 | 80,510 | 14166 | 19:31:41.572328 | `1001125071413372` | 当前触发委托中，`Conditional`，未成交 |
| 3879 | BTC-USDT-SWAP / sell short | 7 张 | 81,110 | 14166 | 19:31:41.903314 | `1001125071413427` | 当前触发委托中，`Conditional`，未成交 |

2026-08-31T21:44:29.892471Z 的交易所只读回读：持仓 0，普通当前委托 0，当前触发委托 2。两个 exchange order ID、方向、数量和价格与本地 order legs `555/556` 精确一致，exchange UI 投影均显示“已绑定”。

| lifecycle | binding | 本地状态 | pos ID | 交易所证据 | 一致性 |
|---:|---:|---|---|---|---|
| 1037 | 321 | `pending_entry`; binding `open`; `last_exchange_status=entry_order_pending`; 两条 leg 均 `pending` | 空 | 0 持仓，2 条精确对应的 pending trigger orders | 一致：尚未成交，等待两个入场触发单 |

leg 的 `attribution_status=unassigned` 表示尚未产生可归属的成交持仓；在 pos ID 为空、触发单未成交的当前状态下，它不与 exchange 回读矛盾。

### 12.6 P0 触发率首轮基线

与本文第 3、4 节完全同口径：8 个调用前确定性触发器从 `request_summary_json` 重建，不把 `reanalysis_triggers_json` 误当成这 8 个触发器。

- 固定消息分母：10；
- context-resolution attempt：2；
- 涉及不同原始消息：2；
- provider 请求：2；
- 首轮描述性触发率：2/10 = 20.00%。

该 20.00% 只是 10 条消息的首轮描述，不能与清理前 6,163 条消息的 47.51% 作统计性高低结论。

| 触发器 | 次数 | attempt 占比 |
|---|---:|---:|
| `revision_language` | 1 | 50.00% |
| `cancellation_language` | 0 | 0.00% |
| `entered_holder_language` | 0 | 0.00% |
| `management_without_exact_target` | 1 | 50.00% |
| `multiple_same_source_candidates` | 0 | 0.00% |
| `reply_target_disagreement` | 0 | 0.00% |
| `text_image_conflict` | 0 | 0.00% |
| `apparent_entry_may_be_revision` | 0 | 0.00% |

| 同时命中数 | attempt | 占比 |
|---:|---:|---:|
| 1 | 2 | 100.00% |
| 2–8 | 0 | 0.00% |

| 状态 | attempt | provider 请求 | 留下决策 |
|---|---:|---:|---:|
| `completed` | 2 | 2 | 2 |
| `exhausted` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `failed` | 0 | 0 | 0 |

实质改变口径与第 4.1 节一致。本次 SQL 在历史 `id<=4245` 数据上复算为 2,767 条可比决策、580 条改变、2,187 条不变，与本文原始结果完全相同。首轮结果：

| 分母口径 | 分母 | 实质改变 | 不变/无决策 | 比率 |
|---|---:|---:|---:|---|
| 有可比较 decision | 2 | 1 | 1 | **不报告** |
| 全部 attempt | 2 | 1 | 1 | **不报告** |
| `completed` attempt | 2 | 1 | 1 | **不报告** |

**只有 2 条 attempt，样本不足以支撑改变率结论。** 因此上表仅保留计数，不用 1/2 生成百分比，也不根据该小样本判断哪个触发器更有效。

### 12.7 距离 P0 目标

P0 目标是“7 个正常日或 500 条消息，哪个先到用哪个”。本轮仅覆盖 4 小时 52 分 23 秒和 10 条消息：

- 按连续时长计，还差 6 天 19 小时 7 分 37 秒；当前尚未完成 1 个整的正常日；
- 按消息数计，还差 490 条；
- 完成度为消息目标的 2.00%，不应把本轮命名为“清理后最终基线”。

### 12.8 可重复的原始查询

下列是形成本节最终数字的四组只读查询。后续观测只替换 `start_raw_message_id`、`window_start` 和 `window_end`；触发器和改变率 SQL 不改，才能与清理前和本轮直接比较。

#### A. 运行身份、循环、monitor 和交易所只读证据

```bash
ssh tecent 'set -e
printf "UTC_NOW\n"
date -u +%Y-%m-%dT%H:%M:%SZ
printf "SYSTEMD\n"
for unit in telegram-kol-web.service telegram-kol-ingest.service telegram-kol-worker.service; do
  systemctl show "$unit" --property=Id,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,Result --no-pager
done
printf "IDENTITIES_AND_LOOPS\n"
for port in 8000 8001 8002; do
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/api/runtime/deployment-identity"
  printf "\n"
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/api/runtime/loop-health"
  printf "\n"
done
printf "TRADING_SETTINGS_SAFE_FIELDS\n"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/api/trading-settings | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in (\"auto_trade_enabled\",\"worker_command_mode\")},sort_keys=True))"
printf "MONITOR_RESULTS\n"
journalctl -u telegram-kol-monitor.service --since "2026-08-31 16:42:00 UTC" --until "2026-08-31 21:35:00 UTC" --no-pager -o short-iso-precise | grep -E "runtime-deployment-identity-v1|audit_ran"
printf "EXCHANGE_COUNTS\n"
curl --fail --silent --show-error --max-time 20 http://127.0.0.1:8002/api/runtime-agent/read-only-exchange-snapshot
printf "\nEXCHANGE_PENDING_TRIGGER_ROWS\n"
curl --fail --silent --show-error --max-time 30 http://127.0.0.1:8002/positions-panel/tabs/open-orders | grep -E "data-exchange-tab-(loaded|item-count|captured-at)|BTC-USDT-SWAP|side-badge|条件/开空|exchange-attribution-chip|order 100112507141|Conditional|委托价格|数量|创建时间|更新时间|暂无当前委托" | head -n 120
'
```

#### B. 消息漏斗、真实执行链、lifecycle/binding 和 Deepcoin 写入

以下内容原样通过标准输入传给 `ssh tecent "sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db"`：

```sql
.headers on
.mode list
BEGIN;
SELECT 'window' AS section, 14158 AS start_raw_message_id,
       '2026-08-31T16:42:37Z' AS window_start,
       '2026-08-31T21:35:00Z' AS window_end,
       MAX(id) AS database_max_raw_message_id
FROM raw_messages;
SELECT 'message_funnel' AS section,
       COUNT(*) AS messages,
       COUNT(DISTINCT mr.raw_message_id) AS entered_recognition,
       COUNT(DISTINCT sc.raw_message_id) AS candidate_messages,
       COUNT(DISTINCT CASE WHEN j.status='expired' THEN j.raw_message_id END) AS expired,
       COUNT(DISTINCT CASE WHEN j.status IN ('pending','claimed') THEN j.raw_message_id END) AS pending_or_claimed,
       COUNT(DISTINCT CASE WHEN j.status='succeeded' THEN j.raw_message_id END) AS succeeded
FROM raw_messages r
LEFT JOIN message_processing_jobs j ON j.raw_message_id=r.id AND j.shadow=0
LEFT JOIN message_recognitions mr ON mr.raw_message_id=r.id
LEFT JOIN signal_candidates sc ON sc.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00';
SELECT 'message_chain' AS section, r.id AS raw_message_id, r.chat_id,
       r.message_id, r.posted_at, r.created_at,
       j.status AS job_status, j.last_reason, j.enqueued_at, j.completed_at,
       mr.status AS recognition_status,
       mrr.status AS mimo_run_status, mrr.attempt_count AS mimo_provider_attempts,
       rd.authoritative_status, rd.automation_status,
       COUNT(DISTINCT sc.id) AS signal_candidates,
       COUNT(DISTINCT cra.id) AS context_attempts
FROM raw_messages r
LEFT JOIN message_processing_jobs j ON j.raw_message_id=r.id AND j.shadow=0
LEFT JOIN message_recognitions mr ON mr.raw_message_id=r.id
LEFT JOIN mimo_recognition_runs mrr ON mrr.raw_message_id=r.id AND mrr.became_authoritative=1
LEFT JOIN recognition_decisions rd ON rd.raw_message_id=r.id
LEFT JOIN signal_candidates sc ON sc.raw_message_id=r.id
LEFT JOIN context_resolution_attempts cra ON cra.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
GROUP BY r.id ORDER BY r.id;
SELECT 'candidate' AS section, sc.id, sc.raw_message_id, sc.symbol, sc.side,
       sc.event_type, sc.confidence, sc.review_status, sc.created_at
FROM signal_candidates sc JOIN raw_messages r ON r.id=sc.raw_message_id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
ORDER BY sc.id;
SELECT 'new_lifecycle_binding' AS section, sl.id AS lifecycle_id,
       eb.id AS binding_id, r.id AS raw_message_id, r.created_at AS raw_created_at,
       r.posted_at, sl.chat_id, sl.message_id, sl.symbol, sl.side,
       sl.lifecycle_status, sl.signal_at, sl.execution_binding_id,
       eb.status AS binding_status, eb.last_exchange_status,
       eb.order_id, eb.client_order_id, eb.pos_id
FROM strategy_lifecycles sl
LEFT JOIN execution_bindings eb ON eb.id=sl.execution_binding_id
LEFT JOIN raw_messages r ON r.chat_id=sl.chat_id AND r.message_id=sl.message_id
WHERE sl.created_at>='2026-08-31 16:42:37'
  AND sl.created_at<='2026-08-31 21:35:00'
ORDER BY sl.id;
SELECT 'deepcoin_write' AS section, ee.id AS execution_event_id,
       ee.execution_binding_id, r.id AS raw_message_id, r.created_at AS raw_created_at,
       ee.action, ee.status, ee.symbol, ee.side,
       COALESCE(json_extract(ee.request_json,'$.sz'),json_extract(ee.request_json,'$.size')) AS quantity,
       json_extract(ee.request_json,'$.triggerPrice') AS trigger_price,
       ee.order_id, ee.client_order_id, ee.pos_id,
       ee.created_at AS submitted_at
FROM execution_events ee
LEFT JOIN raw_messages r ON r.chat_id=ee.chat_id AND r.message_id=ee.source_message_id
WHERE ee.created_at>='2026-08-31 16:42:37'
  AND ee.created_at<='2026-08-31 21:35:00'
ORDER BY ee.id;
SELECT 'order_leg' AS section, eol.id, eol.execution_binding_id,
       eol.leg_index, eol.purpose, eol.order_kind, eol.order_id,
       eol.client_order_id, eol.pos_id, eol.attribution_status,
       eol.status, eol.created_at, eol.updated_at,
       json_extract(eol.request_json,'$.instId') AS instrument,
       json_extract(eol.request_json,'$.sz') AS quantity,
       json_extract(eol.request_json,'$.side') AS order_side,
       json_extract(eol.request_json,'$.posSide') AS position_side,
       json_extract(eol.request_json,'$.triggerPrice') AS trigger_price,
       json_extract(eol.request_json,'$.price') AS order_price
FROM execution_order_legs eol
JOIN execution_bindings eb ON eb.id=eol.execution_binding_id
WHERE eb.created_at>='2026-08-31 16:42:37'
  AND eb.created_at<='2026-08-31 21:35:00'
ORDER BY eol.id;
COMMIT;
```

#### C. P0 触发器、状态、provider 请求和实质改变率

以下内容原样通过标准输入传给 `ssh tecent "sqlite3 -readonly /opt/telegram-kol-analyzer/data/research.db"`：

```sql
.headers on
.mode list
BEGIN;
SELECT 'p0_totals' AS section,
       COUNT(DISTINCT r.id) AS messages,
       COUNT(DISTINCT a.id) AS attempts,
       COUNT(DISTINCT a.raw_message_id) AS distinct_raw_messages,
       COALESCE(SUM(a.attempts),0) AS provider_requests
FROM raw_messages r
LEFT JOIN context_resolution_attempts a ON a.raw_message_id=r.id
WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00';

WITH base AS (
  SELECT a.id, a.raw_message_id, a.request_summary_json,
         lower(COALESCE(json_extract(a.request_summary_json,
                                    '$.message_context.current.text'),'')) AS text,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.recognition_result'),'') AS recognition_result,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.lifecycle_event.event_type'),'none') AS event_type,
         json_extract(a.request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS target_lifecycle_id,
         COALESCE(json_array_length(json_extract(a.request_summary_json,
                                                 '$.candidate_strategy_threads')),0) AS candidate_count
  FROM context_resolution_attempts a
  JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), sets AS (
  SELECT b.*,
         COALESCE((
           SELECT group_concat(thread_id, ',') FROM (
             SELECT DISTINCT
                    CAST(json_extract(link.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,
                            '$.message_context.reply_chain[0].strategy_links') AS link
             WHERE json_extract(link.value,'$.strategy_thread_id') IS NOT NULL
             ORDER BY thread_id
           )
         ),'') AS reply_threads,
         COALESCE((
           SELECT group_concat(thread_id, ',') FROM (
             SELECT DISTINCT
                    CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,'$.candidate_strategy_threads') AS c
             WHERE b.target_lifecycle_id IS NOT NULL
               AND CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                   = CAST(b.target_lifecycle_id AS INTEGER)
             UNION
             SELECT DISTINCT
                    CAST(json_extract(s.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
             FROM json_each(b.request_summary_json,
                            '$.message_context.active_strategies') AS s
             WHERE b.target_lifecycle_id IS NOT NULL
               AND CAST(json_extract(s.value,'$.lifecycle_id') AS INTEGER)
                   = CAST(b.target_lifecycle_id AS INTEGER)
               AND json_extract(s.value,'$.strategy_thread_id') IS NOT NULL
             ORDER BY thread_id
           )
         ),'') AS target_threads
  FROM base b
), flags AS (
  SELECT *,
         (instr(text,'更新')>0 OR instr(text,'修改')>0 OR
          instr(text,'改为')>0 OR instr(text,'调整')>0 OR
          instr(text,'replace')>0 OR instr(text,'update')>0) AS revision_language,
         (instr(text,'取消')>0 OR instr(text,'撤销')>0 OR
          instr(text,'撤单')>0 OR instr(text,'cancel')>0) AS cancellation_language,
         (instr(text,'有入场')>0 OR instr(text,'已入场')>0 OR
          instr(text,'持仓')>0 OR instr(text,'保护成本')>0 OR
          instr(text,'保本')>0 OR instr(text,'继续持有')>0) AS entered_holder_language,
         (event_type<>'none' AND target_lifecycle_id IS NULL) AS management_without_exact_target,
         (candidate_count>1) AS multiple_same_source_candidates,
         (reply_threads<>'' AND target_threads<>'' AND
          reply_threads<>target_threads) AS reply_target_disagreement,
         (COALESCE(json_array_length(json_extract(request_summary_json,
                                                   '$.saved_evidence.conflicts')),0)>0) AS text_image_conflict,
         (recognition_result='是策略' AND candidate_count>0 AND (
            (instr(text,'更新')>0 OR instr(text,'修改')>0 OR
             instr(text,'改为')>0 OR instr(text,'调整')>0 OR
             instr(text,'replace')>0 OR instr(text,'update')>0)
            OR EXISTS (
              SELECT 1
              FROM json_each(request_summary_json,
                             '$.candidate_strategy_threads') AS c,
                   json_each(c.value,'$.reasons') AS reason
              WHERE reason.value='overlapping_entry'
            )
          )) AS apparent_entry_may_be_revision
  FROM sets
), trigger_rows AS (
  SELECT id,'revision_language' AS trigger FROM flags WHERE revision_language
  UNION ALL SELECT id,'cancellation_language' FROM flags WHERE cancellation_language
  UNION ALL SELECT id,'entered_holder_language' FROM flags WHERE entered_holder_language
  UNION ALL SELECT id,'management_without_exact_target' FROM flags WHERE management_without_exact_target
  UNION ALL SELECT id,'multiple_same_source_candidates' FROM flags WHERE multiple_same_source_candidates
  UNION ALL SELECT id,'reply_target_disagreement' FROM flags WHERE reply_target_disagreement
  UNION ALL SELECT id,'text_image_conflict' FROM flags WHERE text_image_conflict
  UNION ALL SELECT id,'apparent_entry_may_be_revision' FROM flags WHERE apparent_entry_may_be_revision
), trigger_names(trigger, ordinal) AS (
  VALUES ('revision_language',1),
         ('cancellation_language',2),
         ('entered_holder_language',3),
         ('management_without_exact_target',4),
         ('multiple_same_source_candidates',5),
         ('reply_target_disagreement',6),
         ('text_image_conflict',7),
         ('apparent_entry_may_be_revision',8)
)
SELECT 'trigger_distribution' AS section, n.trigger,
       COUNT(t.id) AS attempts,
       round(100.0*COUNT(t.id)/(SELECT COUNT(*) FROM flags),2) AS attempt_pct
FROM trigger_names n
LEFT JOIN trigger_rows t ON t.trigger=n.trigger
GROUP BY n.trigger,n.ordinal
ORDER BY n.ordinal;

WITH base AS (
  SELECT a.id, a.request_summary_json,
         lower(COALESCE(json_extract(a.request_summary_json,
                                    '$.message_context.current.text'),'')) AS text,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.recognition_result'),'') AS recognition_result,
         COALESCE(json_extract(a.request_summary_json,
                               '$.mimo_first_pass.lifecycle_event.event_type'),'none') AS event_type,
         json_extract(a.request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS target_lifecycle_id,
         COALESCE(json_array_length(json_extract(a.request_summary_json,
                                                 '$.candidate_strategy_threads')),0) AS candidate_count
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), sets AS (
  SELECT b.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT
                  CAST(json_extract(link.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,
                          '$.message_context.reply_chain[0].strategy_links') AS link
           WHERE json_extract(link.value,'$.strategy_thread_id') IS NOT NULL
           ORDER BY thread_id
         )),'') AS reply_threads,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE b.target_lifecycle_id IS NOT NULL
             AND CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(b.target_lifecycle_id AS INTEGER)
           UNION
           SELECT DISTINCT
                  CAST(json_extract(s.value,'$.strategy_thread_id') AS INTEGER) AS thread_id
           FROM json_each(b.request_summary_json,
                          '$.message_context.active_strategies') AS s
           WHERE b.target_lifecycle_id IS NOT NULL
             AND CAST(json_extract(s.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(b.target_lifecycle_id AS INTEGER)
             AND json_extract(s.value,'$.strategy_thread_id') IS NOT NULL
           ORDER BY thread_id
         )),'') AS target_threads
  FROM base b
), flags AS (
  SELECT *,
    (instr(text,'更新')>0 OR instr(text,'修改')>0 OR instr(text,'改为')>0 OR
     instr(text,'调整')>0 OR instr(text,'replace')>0 OR instr(text,'update')>0) AS f1,
    (instr(text,'取消')>0 OR instr(text,'撤销')>0 OR instr(text,'撤单')>0 OR instr(text,'cancel')>0) AS f2,
    (instr(text,'有入场')>0 OR instr(text,'已入场')>0 OR instr(text,'持仓')>0 OR
     instr(text,'保护成本')>0 OR instr(text,'保本')>0 OR instr(text,'继续持有')>0) AS f3,
    (event_type<>'none' AND target_lifecycle_id IS NULL) AS f4,
    (candidate_count>1) AS f5,
    (reply_threads<>'' AND target_threads<>'' AND reply_threads<>target_threads) AS f6,
    (COALESCE(json_array_length(json_extract(request_summary_json,
                                              '$.saved_evidence.conflicts')),0)>0) AS f7,
    (recognition_result='是策略' AND candidate_count>0 AND (
      (instr(text,'更新')>0 OR instr(text,'修改')>0 OR instr(text,'改为')>0 OR
       instr(text,'调整')>0 OR instr(text,'replace')>0 OR instr(text,'update')>0)
      OR EXISTS (SELECT 1 FROM json_each(request_summary_json,
                                         '$.candidate_strategy_threads') c,
                              json_each(c.value,'$.reasons') reason
                 WHERE reason.value='overlapping_entry'))) AS f8
  FROM sets
), counts AS (
  SELECT id, f1+f2+f3+f4+f5+f6+f7+f8 AS trigger_count FROM flags
), bucket_names(simultaneous_triggers, ordinal) AS (
  VALUES ('1',1),('2-8',2)
), observed AS (
  SELECT CASE WHEN trigger_count=1 THEN '1' ELSE '2-8' END AS simultaneous_triggers,
         COUNT(*) AS attempts
  FROM counts GROUP BY simultaneous_triggers
)
SELECT 'trigger_multiplicity' AS section,
       n.simultaneous_triggers,
       COALESCE(o.attempts,0) AS attempts,
       round(100.0*COALESCE(o.attempts,0)/(SELECT COUNT(*) FROM counts),2) AS attempt_pct
FROM bucket_names n
LEFT JOIN observed o ON o.simultaneous_triggers=n.simultaneous_triggers
ORDER BY n.ordinal;

WITH status_names(status, ordinal) AS (
  VALUES ('completed',1),('exhausted',2),('superseded',3),('failed',4)
), observed AS (
  SELECT a.status, COUNT(*) AS attempts, SUM(a.attempts) AS provider_requests,
         SUM(a.decision_json IS NOT NULL) AS decisions
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
  GROUP BY a.status
)
SELECT 'attempt_status' AS section, n.status,
       COALESCE(o.attempts,0) AS attempts,
       COALESCE(o.provider_requests,0) AS provider_requests,
       COALESCE(o.decisions,0) AS decisions
FROM status_names n LEFT JOIN observed o ON o.status=n.status
ORDER BY n.ordinal;

WITH raw AS (
  SELECT a.*,
         json_extract(request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS first_lifecycle_id,
         CASE
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.recognition_result')='是策略' THEN 'new'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='position_update' THEN 'manage'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='cancel_entry' THEN 'cancel'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='exit_position' THEN 'exit'
           ELSE 'no_action'
         END AS first_family,
         CASE
           WHEN json_extract(decision_json,'$.decision') IN ('hold','unresolved')
             OR CAST(json_extract(decision_json,'$.confidence') AS REAL)<0.7 THEN 'no_action'
           WHEN json_extract(decision_json,'$.decision')='new_thread' THEN 'new'
           WHEN json_extract(decision_json,'$.decision')='revise_thread' THEN 'revise'
           WHEN json_extract(decision_json,'$.decision')='manage_thread' THEN 'manage'
           WHEN json_extract(decision_json,'$.decision')='cancel_thread' THEN 'cancel'
           WHEN json_extract(decision_json,'$.decision')='exit_thread' THEN 'exit'
           ELSE 'no_action'
         END AS context_family
  FROM context_resolution_attempts a JOIN raw_messages r ON r.id=a.raw_message_id
  WHERE r.id>14158 AND r.created_at<='2026-08-31 21:35:00'
), normalized AS (
  SELECT raw.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(raw.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(raw.first_lifecycle_id AS INTEGER)
           ORDER BY thread_id
         )),'') AS first_targets,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(t.value AS INTEGER) AS thread_id
           FROM json_each(raw.decision_json,'$.target_thread_ids') AS t
           ORDER BY thread_id
         )),'') AS context_targets
  FROM raw
), scored AS (
  SELECT *, CASE
    WHEN decision_json IS NULL THEN 0
    WHEN first_family<>context_family THEN 1
    WHEN first_family=context_family
      AND first_family IN ('manage','cancel','exit','revise')
      AND first_targets<>context_targets THEN 1
    ELSE 0 END AS changed
  FROM normalized
)
SELECT 'change_rate_comparable_decisions' AS section,
       SUM(decision_json IS NOT NULL) AS denominator,
       SUM(CASE WHEN decision_json IS NOT NULL THEN changed ELSE 0 END) AS changed,
       SUM(CASE WHEN decision_json IS NOT NULL THEN 1-changed ELSE 0 END) AS unchanged_or_no_decision
FROM scored
UNION ALL
SELECT 'change_rate_all_attempts', COUNT(*), SUM(changed), SUM(1-changed) FROM scored
UNION ALL
SELECT 'change_rate_completed_attempts', SUM(status='completed'),
       SUM(CASE WHEN status='completed' THEN changed ELSE 0 END),
       SUM(CASE WHEN status='completed' THEN 1-changed ELSE 0 END)
FROM scored;
COMMIT;
```

#### D. 第 4.1 节历史改变率同口径复算

为证明 C 中改变率定义没有漂移，以下是实际执行的完整历史复算查询：

```sql
WITH raw AS (
  SELECT a.*,
         json_extract(request_summary_json,
                      '$.mimo_first_pass.lifecycle_event.target_lifecycle_id') AS first_lifecycle_id,
         CASE
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.recognition_result')='是策略' THEN 'new'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='position_update' THEN 'manage'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='cancel_entry' THEN 'cancel'
           WHEN json_extract(request_summary_json,
                             '$.mimo_first_pass.lifecycle_event.event_type')='exit_position' THEN 'exit'
           ELSE 'no_action'
         END AS first_family,
         CASE
           WHEN json_extract(decision_json,'$.decision') IN ('hold','unresolved')
             OR CAST(json_extract(decision_json,'$.confidence') AS REAL)<0.7 THEN 'no_action'
           WHEN json_extract(decision_json,'$.decision')='new_thread' THEN 'new'
           WHEN json_extract(decision_json,'$.decision')='revise_thread' THEN 'revise'
           WHEN json_extract(decision_json,'$.decision')='manage_thread' THEN 'manage'
           WHEN json_extract(decision_json,'$.decision')='cancel_thread' THEN 'cancel'
           WHEN json_extract(decision_json,'$.decision')='exit_thread' THEN 'exit'
           ELSE 'no_action'
         END AS context_family
  FROM context_resolution_attempts a
  WHERE a.id<=4245
), normalized AS (
  SELECT raw.*,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(json_extract(c.value,'$.thread_id') AS INTEGER) AS thread_id
           FROM json_each(raw.request_summary_json,'$.candidate_strategy_threads') AS c
           WHERE CAST(json_extract(c.value,'$.lifecycle_id') AS INTEGER)
                 = CAST(raw.first_lifecycle_id AS INTEGER)
           ORDER BY thread_id
         )),'') AS first_targets,
         COALESCE((SELECT group_concat(thread_id, ',') FROM (
           SELECT DISTINCT CAST(t.value AS INTEGER) AS thread_id
           FROM json_each(raw.decision_json,'$.target_thread_ids') AS t
           ORDER BY thread_id
         )),'') AS context_targets
  FROM raw
), scored AS (
  SELECT *, CASE
    WHEN first_family<>context_family THEN 1
    WHEN first_family=context_family
      AND first_family IN ('manage','cancel','exit','revise')
      AND first_targets<>context_targets THEN 1
    ELSE 0 END AS changed
  FROM normalized
)
SELECT COUNT(*) AS comparable_decisions,
       SUM(changed) AS changed,
       SUM(1-changed) AS unchanged
FROM scored
WHERE decision_json IS NOT NULL;
```

输出为 `2767|580|2187`，与本文第 4.1 节一致。A 中交易所 HTML 的 `grep` 含本轮已知 order ID 前缀，它是为了保留本轮原始命令的结果定位条件；下轮可重复观测应用当期新 binding 的 order ID 替换该前缀，不改变 SQL 口径。

## 13. P0 第二个固定窗口 — through 2026-09-01T07:12:19Z

### 13.1 Window and denominator

This checkpoint continues section 12 without changing its denominator contract. The previous fixed cutoff and raw-message high-water mark become the new open boundary:

- incremental start: `2026-08-31T21:35:00Z`, `raw_messages.id > 14168`;
- fixed end: `2026-09-01T07:12:19Z`;
- duration: 9 hours 37 minutes 19 seconds, or 0.4009 24-hour days;
- messages: 64, IDs 14169–14232;
- all 64 `created_at` and `posted_at` values are after the start, so no delayed maintenance-period message entered the denominator;
- attempts: 41 attempts over 36 distinct messages;
- comparable provider requests: 42, using the section-12 legacy `attempts` fallback only for the four pre-observability attempts;
- directly observed provider requests: 38 across 37 attempts; four legacy attempts have NULL in the five new observability fields and are never used to infer triggers, tokens or request-component bytes.

The incremental descriptive context-resolution trigger rate is therefore 36/64 = 56.25%. This is a traffic description, not a stable rate estimate: the cumulative cohort still falls short of both P0 stopping rules.

### 13.2 Direct eight-trigger observations

The following values come only from `invocation_triggers_json`; no request-payload reconstruction is used. The denominator is the 37 directly observed attempts. Trigger categories are non-exclusive, so their percentages do not sum to 100%.

| invocation trigger | attempts | descriptive share of 37 |
|---|---:|---:|
| `revision_language` | 0 | 0.00% |
| `cancellation_language` | 0 | 0.00% |
| `entered_holder_language` | 8 | 21.62% |
| `management_without_exact_target` | 12 | 32.43% |
| `multiple_same_source_candidates` | 33 | 89.19% |
| `reply_target_disagreement` | 0 | 0.00% |
| `text_image_conflict` | 1 | 2.70% |
| `apparent_entry_may_be_revision` | 0 | 0.00% |

These shares are reported only because the fixed P0 specification asks for a descriptive distribution. With 64 messages and 37 observable attempts, they must not be used to rank trigger quality or justify a trigger/threshold change.

### 13.3 Direct provider usage

All 38 `provider_usage_json` entries report `available=true`; there is no imputed token row. The partial-window totals are 510,453 prompt tokens, 53,214 completion tokens and 563,667 total tokens, including 67,584 cached prompt tokens.

| per-provider-request total tokens | value |
|---|---:|
| minimum | 4,800 |
| P50 | 15,065 |
| mean | 14,833.34 |
| P90 | 21,278 |
| P95 | 22,821 |
| maximum | 23,719 |

P50/P90/P95 are empirical nearest-rank values on only 38 requests. They are measurements, not population estimates.

Trigger-group averages are also direct and non-exclusive: a request is counted in every trigger it carried.

| trigger | provider requests | mean total tokens | range |
|---|---:|---:|---:|
| `entered_holder_language` | 9 | 17,264.78 | 14,653–21,278 |
| `management_without_exact_target` | 12 | 11,663.42 | 6,051–15,977 |
| `multiple_same_source_candidates` | 34 | 15,178.79 | 6,051–23,719 |
| `text_image_conflict` | 1 | 4,800.00 | 4,800–4,800 |
| the other four triggers | 0 | not observed | — |

Direct token telemetry begins at `2026-09-01T01:06:27.730519Z`. Dividing 563,667 tokens by the 6 hours 5 minutes 51 seconds from that first directly observed attempt through the fixed end yields a purely exposure-normalized 2.219 million tokens per 24 hours. That value is highly unstable: the window is one quarter of a day, contains only 38 requests, and excludes four earlier legacy requests whose token usage is unknowable. It is not the final daily baseline.

### 13.4 Request component bytes

The 37 directly observed attempts recorded 1,486,391 total canonical request bytes. `message_context_bytes` is a nested aggregate and must not be added to the disjoint components below.

| component | bytes | share of request total | interpretation |
|---|---:|---:|---|
| `message_context_bytes` | 1,199,302 | 80.69% | nested whole message-context aggregate |
| `active_strategies_bytes` | 254,197 | 17.10% | disjoint subcomponent |
| `reply_chain_bytes` | 74 | 0.005% | disjoint subcomponent |
| `current_message_bytes` | 10,658 | 0.72% | disjoint top-level component |
| `remainder_bytes` | 1,218,724 | 81.99% | disjoint remainder, including context other than reply/active |
| canonical structural overhead | 2,738 | 0.18% | total minus the four disjoint components |

The disjoint rows sum to 100%. The nested `message_context_bytes` value independently confirms that historical/context material remains the dominant request component in this live cohort.

### 13.5 Attempt outcomes and network behavior

| status / phase | attempts | distinct messages | direct provider requests |
|---|---:|---:|---:|
| completed / initial resolution | 30 | 30 | 31 |
| completed / reanalysis | 5 | 3 | 5 |
| completed / legacy unobserved | 4 | 4 | 0 directly observed; 4 comparable |
| exhausted / initial resolution | 2 | 2 | 2 |

The two exhausted rows have one successful, usage-bearing provider request each, `error_class=NULL`, `last_error=RuntimeError`, and no scheduled retry; they are not network failures. Attempt 4279 used two provider requests because the first response targeted a thread outside the allowed candidate set (`target_outside_candidate_set`), then completed after the existing contract-correction retry. It is also not a network retry.

Across all 38 direct usage entries there are zero `provider_request_failed` or other unavailable entries, zero `network_error` rows, zero `retry_pending` rows, and zero non-NULL `next_attempt_at` rows at the fixed snapshot. The provider circuit is process-local and the authorized R1 failure/rollback changed the worker PID inside this wider window, so the durable evidence supports the bounded conclusion that no network-error backoff or circuit-open episode was recorded; it is not a claim about unpersisted in-memory state before that restart.

### 13.6 Cumulative distance to P0 target

The cumulative cohort remains anchored at `2026-08-31T16:42:37Z` and raw ID 14158. Through the fixed end it contains 74 messages, 43 attempts and 44 comparable provider requests over 14 hours 29 minutes 42 seconds (0.604 normal days).

- time criterion: 6 days 9 hours 30 minutes 18 seconds remain to 7 full normal days;
- message criterion: 426 messages remain to 500; current completion is 14.80%;
- neither criterion is met, so this is a P0 checkpoint rather than the final baseline.

The small cohort is sufficient to confirm that the five live observability fields are producing internally consistent direct measurements. It is not sufficient to conclude that the 56.25% descriptive trigger rate, any per-trigger token average, or the 2.219-million-token daily normalization is representative. No trigger, word list, threshold, prompt, context window, setting, row, release or service was changed while collecting this checkpoint.

## 14. Token cost ROI, relevance-window replay, and main-recognition instrumentation design

### 14.1 Fixed snapshot and metric contracts

This section is read-only. It changed no code, setting, whitelist, threshold, prompt, database row, release, service or exchange state. The production SQLite database was opened in read-only mode and the core queries ran inside one read transaction. The fixed snapshot is:

- query time: `2026-09-01T10:00:29.583Z`;
- `context_resolution_attempts.id <= 4307`;
- `raw_messages.id <= 14256`;
- current whitelist: 33 chat IDs from `trading_settings.global.context_resolution_live_chat_ids`;
- legacy decision-recall cohort: the already accepted `id <= 4245` cohort, containing the 580 material changes used throughout sections 4 and 6.

The following definitions prevent three common overclaims:

1. **Invocation count** is reported as `context_resolution_attempts` rows and provider requests separately. For legacy rows, provider requests use the persisted `attempts` count; for R1 rows, the exact number of entries in `provider_usage_json` is authoritative.
2. **Material change** uses the unchanged section-4 contract: action family plus target thread set, with the existing confidence/applicability rule. Reason wording alone is not a material change.
3. **Real exchange-write impact** is deliberately strict. It requires, after the attempt timestamp, both a `signal_candidates` row for the same `raw_message_id` and a directly linked `execution_events.source_message_id` whose action is an exchange-write action. Historical execution events that predate the attempt, local reconciliation/audit actions, `auto_trade_skipped`, and reservation-only rows do not count.

Only two attempts satisfy the third contract:

| attempt | chat | candidate | directly linked exchange write |
|---:|---:|---|---|
| 1308 / raw 10901 | -1002337721508 | candidate 1776, `position_update`, 14:36:05.948847Z | event 3505 cancel deferred trigger entry and event 3514 close submit, both 14:36:05.968916Z |
| 1898 / raw 11780 | -1003825498321 | candidate 1898, `close_signal`, 08:10:26.334182Z | event 3630 management close submit, 08:10:26.432135Z |

R1 provides 58 directly measured provider requests across 56 attempt rows: 828,098 total tokens over 2,274,427 canonical request bytes. Rows without usage are estimated with the observed aggregate conversion `0.3640908 total token / canonical request byte`; the per-request ratio has P10 `0.3388752`, median `0.3618721`, and P90 `0.4258418`. This proxy incorporates prompt overhead and completion tokens only through the small R1 cohort. It is not a provider bill and must not be presented as measured usage. Direct telemetry covers only 58/5,987 = **0.97%** of provider requests in this snapshot.

### 14.2 Per-chat context-resolution ROI

`A` below means directly observed tokens and `P` means the calibrated byte proxy. `changed` is material first-layer change; `write impact` is the strict post-attempt lineage defined above. Chats are sorted first by observed exchange-write impact, then by historical token burden. Token totals cover the full stored history through attempt 4307, not one day.

| whitelisted chat | attempts / provider requests | token total (`A + P`) | changed | write impact | token / write impact |
|---|---:|---:|---:|---:|---:|
| 米娅 vip会员群 11分组 (`-1003825498321`) | 100 / 132 | 1.630M (`0.041M + 1.590M`) | 17 | 1 | 1.630M |
| 比特币陈哥会员群-11分组 (`-1002337721508`) | 175 / 210 | 6.318M (`0.017M + 6.302M`) | 43 | 1 | 6.318M |
| 三马哥会员群-11分组 (`-1002199068560`) | 527 / 711 | 28.441M (`0.045M + 28.396M`) | 110 | 0 | not defined |
| 欧阳火箭滚仓班 (`-1003095914903`) | 573 / 765 | 23.673M (`0.235M + 23.438M`) | 102 | 0 | not defined |
| 比特币军长 (`-1002282384698`) | 509 / 736 | 21.088M (`0.104M + 20.984M`) | 104 | 0 | not defined |
| 币圈所长会员群 (`-1002368892075`) | 449 / 586 | 20.806M (`0.224M + 20.582M`) | 23 | 0 | not defined |
| 峰哥高级会员群 (`-1002409877375`) | 385 / 666 | 20.229M (`0 + 20.229M`) | 6 | 0 | not defined |
| 比特币飞扬 (`-1002960443256`) | 484 / 693 | 17.923M (`0.021M + 17.902M`) | 54 | 0 | not defined |
| 大镖客 (`-1003048800035`) | 364 / 518 | 17.046M (`0.042M + 17.004M`) | 51 | 0 | not defined |
| 舒琴会员群 (`-1002370796392`) | 213 / 300 | 8.430M (`0 + 8.430M`) | 7 | 0 | not defined |
| ROSE会员群 (`-1002344190971`) | 48 / 62 | 2.197M (`0 + 2.197M`) | 19 | 0 | not defined |
| 米哥会员群 (`-1002367395169`) | 164 / 192 | 2.100M (`0.074M + 2.026M`) | 18 | 0 | not defined |
| 比特智 智哥 (`-1003344714145`) | 103 / 137 | 1.783M (`0 + 1.783M`) | 18 | 0 | not defined |
| weichang tan (`-1003415020968`) | 37 / 64 | 0.622M (`0 + 0.622M`) | 1 | 0 | not defined |
| 书 shu-crypto (`-1003053031367`) | 21 / 22 | 0.531M (`0.012M + 0.519M`) | 2 | 0 | not defined |
| 提阿非罗 初塔 (`-1002918719121`) | 32 / 51 | 0.458M (`0.005M + 0.453M`) | 0 | 0 | not defined |
| 大漂亮社区 (`-1002805019371`) | 46 / 53 | 0.454M (`0.009M + 0.445M`) | 13 | 0 | not defined |
| 超V社区搬运群 (`-1001716834927`) | 56 / 68 | 0.349M (`0 + 0.349M`) | 0 | 0 | not defined |
| 三姐精准策略群 (`-1003000736304`) | 8 / 8 | 0.046M (`0 + 0.046M`) | 2 | 0 | not defined |
| unlabeled (`-1002451280921`) | 9 / 9 | 0.033M (`0 + 0.033M`) | 6 | 0 | not defined |
| 龚有财策略群 (`-1002458558902`) | 4 / 4 | 0.021M (`0 + 0.021M`) | 1 | 0 | not defined |
| `-1002274336512` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-5025043055` | 0 / 0 | 0 | 0 | 0 | no sample |
| 躺赢社区 (`-1001966634720`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1002270839757` | 0 / 0 | 0 | 0 | 0 | no sample |
| 凉兮 (`-1003585604552`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4781287606` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1003496877217` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-5046697627` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-1002494435354` | 0 / 0 | 0 | 0 | 0 | no sample |
| il Capo Of Crypto (`-1002168259610`) | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4606125205` | 0 / 0 | 0 | 0 | 0 | no sample |
| `-4623349778` | 0 / 0 | 0 | 0 | 0 | no sample |

Totals are 4,307 attempts, 5,987 provider requests, approximately 174.179M total tokens (`0.828M` measured plus `173.351M` proxy), 597 material changes, and two strict exchange-write impacts.

#### Can any zero-impact chat be removed harmlessly?

No current row supports the word **harmless**:

- 31/33 whitelisted chats have zero strict exchange-write impacts, but 19 of them have positive sample and many still materially changed a decision. A no-write or risk-reducing decision can be valuable precisely because it prevents an order; the lineage metric cannot observe that counterfactual benefit.
- The largest zero-impact cohorts have 364–573 attempts. The rule-of-three 95% upper bound for an unseen per-attempt write impact is approximately 0.52%–0.82%, but that bound says nothing about prevented bad writes or safer lifecycle targeting.
- The only positive-sample chats with zero material changes are 超V (56 attempts) and 提阿非罗 (32 attempts). Their corresponding rule-of-three upper bounds are still 5.36% and 9.38% per attempt. That is not enough evidence for a permanent behavior change.
- Twelve whitelisted chats have no attempt at all. Zero observations are absence of evidence, not evidence of zero value.

Changing `context_resolution_live_chat_ids` is a **pure configuration** action and the gate is read dynamically in `trading_settings.py:205-210`; it does not require a code deployment. It still changes the recognition/management decision path. The evidence supports, at most, a future bounded shadow/removal experiment for selected chats. It does not support calling any immediate removal harmless.

### 14.3 Relevance-preserving window replay

The replay retained the current message and existing non-history request components in all variants. The deployable, causal selector was evaluated with information available before the provider call:

- direct reply ancestors;
- full candidate/lifecycle root messages when present in the stored history;
- active-lifecycle source messages;
- messages already linked to a candidate thread;
- message IDs cited by an earlier decision in the same chat;
- for the semantic variant, any first-pass message already classified as a strategy or lifecycle event, plus any message carrying a strategy link.

The result on all stored request bytes through attempt 4307, with explicit-reference recall evaluated on the accepted 580-change legacy cohort, is:

| replay policy | weighted request-byte saving | changed attempts losing at least one observable explicit reference | lost reference IDs | online-causal? |
|---|---:|---:|---:|---|
| accepted blind 20 messages / 24 hours | 50.75% | 72 / 580 | accepted section-6 result | yes, but unsafe |
| structural relevance only | 49.18% | 49 / 580 | 66 | yes |
| structural + first-pass semantic relevance | 41.43% | 34 / 580 | 44 | yes |
| semantic relevance + conservative 20/24 fallback | 26.23% | 8 / 580 | 15 | yes |
| semantic relevance + the **current** decision's explicit references | 41.37% | 0 / 580 matchable | 0; one legacy reference was absent from its stored request | **no; post-hoc oracle** |

The last row gives the requested zero-loss audit ceiling: average request bytes would fall by approximately **41.37%**, and every explicit supporting/opposing message that actually existed in the stored request would remain. It is not deployable as written, because the provider's current `decision_json` does not exist until after the request has already been sent. Treating that row as an online strategy would be look-ahead.

The eight residual causal misses explain which evidence class is still absent. They are strategy-episode continuations that the first pass labelled `非策略/event=none`: paired entry and stop messages, “我的空还在”, profit/status updates, and narrative continuations such as the prior SNDK thesis. They are neither direct replies nor reliably linked lifecycle rows. To reach zero without look-ahead, the selector needs a durable **strategy-episode membership** relation that keeps these false-negative-but-related messages. A keyword-only patch is insufficient: these examples include pronouns and author-specific continuation patterns with no symbol.

Therefore the safe design is:

1. build a non-authoritative selector from reply ancestry, candidate/lifecycle roots, thread links, prior citations, and a strategy-episode relation;
2. run it in shadow while the full window remains authoritative;
3. require 100% recall of all explicit references and zero action/target/applicability differences on a predeclared sample before any cutover;
4. if the selector cannot causally recall all references, retain the full window. The safe deployable saving is then **0%**, not the 41.37% oracle ceiling.

This is materially safer than the blind 20/24 proposal: blind truncation offers about 9.38 percentage points more theoretical byte saving than the post-hoc relevance ceiling, but already loses explicit evidence in 72 material-change attempts. The current causal relevance selector reduces that observed risk to eight attempts, but has not reached the zero-recall target.

#### R1 byte cross-check

The R1 cohort now contains 56 attempt rows (IDs 4252–4307) and 58 provider requests over about nine hours. All 56 stored `request_total_bytes` values exactly equal an independent canonical UTF-8 re-encoding of `request_summary_json`. Mean request size is 39,160.34 B, P50 38,927.5 B, P90 58,034 B; mean `message_context` is 31,558.14 B and contributes 80.59% of aggregate request bytes.

This validates the measurement implementation and independently confirms that context remains the dominant component. It is not enough to replace the historical 89.16% estimate: 56 attempts cover less than one day, only a subset of chats, and a different post-cleanup traffic mix.

### 14.4 Main-recognition daily cost: best available estimate

There is still no true provider usage on the main-recognition path. `ai_prompt_invocations` records invocation identity/status but no tokens; `mimo_recognition_attempts` records provider-attempt status/duration but no request components or provider usage. Consequently every main-recognition token number below is a proxy.

For the fixed 35-calendar-day comparison window `[2026-07-27, 2026-08-31)`:

- 6,163 raw messages arrived;
- `ai_prompt_invocations.feature='message_recognition'` contains 7,035 high-level invocations over 5,821 distinct raw messages, of which 7,000 completed;
- this is 201.0 invocations/day and 1.141 high-level invocations per raw message;
- applying the already accepted 20K–28K token/request byte proxy yields **4.02M–5.63M tokens/day**.

The failure-storm dates 2026-08-21 through 2026-08-23 contain 1,776 invocation records. Removing those three abnormal dates leaves 5,259 invocations over 32 days, or 164.34/day and **3.29M–4.60M proxy tokens/day**. This exclusion describes normal throughput; it is not a billing adjustment, because an HTTP error may or may not have reached the provider and no usage was persisted.

The current MiMo audit tables reinforce the uncertainty: since 2026-08-12 they contain 4,463 completed and 184 HTTP-error provider attempts, but none has usage. Image inputs further weaken any byte-to-token mapping because provider image accounting is not UTF-8 text accounting.

Against the accepted normal context-resolution proxy of 1.67M–2.22M/day, the full-window main estimate is roughly **1.81×–3.37×**. The previously stated 2.5×–3× expectation lies inside that uncertainty band, but is not yet a measurement. Optimization priority should therefore be:

1. instrument main recognition first, because it is probably the larger cost and currently the larger measurement blind spot;
2. keep collecting direct context-resolution usage;
3. only then choose between chat gating and relevance-window work using comparable measured cost and decision-safety metrics.

### 14.5 Additive main-recognition instrumentation design

The correct grain is one provider attempt, not one raw message. The existing durable row is `mimo_recognition_attempts`, which already separates retries. Add nullable R1-shaped fields there:

- `provider_usage_json`: the provider's unmodified usage object wrapped with `available`, request ordinal and an explicit unavailable reason;
- `request_component_bytes_json`: encoding/version, total canonical request bytes, system-prompt bytes, current-message metadata/text bytes, rendered authoritative-context bytes, image data-URL bytes and structural overhead.

The write path is narrowly defined:

1. `recognition_experiments.py:1091-1126` already builds the exact in-memory payload and receives the provider JSON. Measure that same object without mutating or rebuilding it, and copy `data['usage']` when present.
2. Carry the two telemetry objects with the existing provider result/exception metadata through both the v1 authority wrapper and the v2 attempt path.
3. Extend `record_mimo_attempt()` in `mimo_recognition_runs.py:166-242` to persist telemetry on the matching ordinal. Missing provider usage must be recorded as unavailable, never as zero.
4. Keep `ai_prompt_invocations` as the high-level feature audit; do not aggregate retries into it and do not make trading code read the new columns.

Safety properties and acceptance tests:

- the exact payload object passed to `httpx.Client.post(..., json=payload)` is unchanged byte-for-byte in canonical form;
- model, prompt, context builder, image sequence, temperature, JSON mode and thinking mode are unchanged;
- parsed content, canonical decision fingerprint and execution projection are identical with telemetry enabled/disabled;
- one retry produces two attempt rows with two separate usage records;
- provider-omitted usage is explicit `available=false`, while returned prompt/completion/total tokens are preserved exactly;
- component bytes sum to total with a named structural-overhead remainder, and image bytes are reported separately rather than converted to fake text tokens.

This is a **code + nullable L3 schema migration + deployment** change. It is pure additive storage/observability and should not change model input or decision behavior. It is not a configuration-only change. A later dashboard or cost alert could read the fields without touching recognition authority.

### 14.6 Action-type summary

| proposal | category | deployment required? | behavior change? | current evidence decision |
|---|---|---:|---:|---|
| remove selected chat IDs from context whitelist | pure configuration | no | yes | do not call harmless; insufficient causal evidence |
| historical ROI and replay queries | read-only analysis | no | no | completed in this section |
| relevance selector / strategy-episode relation | code | yes | yes, changes model input | shadow only until causal zero-recall and decision-equivalence gates pass |
| main-recognition usage/component telemetry | additive code + nullable schema | yes | intended no | highest-priority implementation candidate |

### 14.7 Validation and limitations

The per-chat totals independently sum to 4,307 attempts, 5,987 provider requests, 597 material changes and two strict trade impacts. The two impacts were manually cross-checked against candidate/event timestamps and direct raw-message lineage. All 56 R1 component rows passed canonical-byte equality. The legacy material-change query remains 580 on `id <= 4245`.

The main limitations are substantive, not formatting details: only 0.97% of historical context provider requests have true usage; only two strict trade impacts exist; prevented trades have no counterfactual outcome; one legacy explicit reference was not present in its stored request; and a current model citation cannot be known before the current call. These limitations prohibit a whitelist removal claim, a production window cutover, or a precise main/context billing ratio from this dataset alone.

## 15. 目标消歧与语义消歧：只读分层抽样（2026-09-01）

### 15.1 快照、口径与结论

本节只读。生产 SQLite 以 URI mode=ro 打开，并在一个只读事务中固定快照；没有修改代码、设置、白名单、词表、阈值、prompt、数据库、release、服务或交易所状态。

- 历史分组沿用已验收的 legacy cohort：context_resolution_attempts.id <= 4245，共 4,245 次 attempt。这样 92.01% 的 multiple_same_source_candidates 与既有 580 次实质改变口径不会漂移。
- 最近样本固定在 2026-09-01T10:51:37.595Z：context_resolution_attempts.id <= 4313、raw_messages.id <= 14264。
- 八个触发器互不排斥，因此各组之和会超过 attempt 总数。
- “语义消歧”定义为第一层与最终结果的动作族不同：new / manage / cancel / exit / no_action；“目标消歧”定义为动作族相同且属于管理类，但 target thread 集合改变。reason 文案变化不算实质改变。
- legacy 行没有真实 provider usage。其 token 只用当前 64 条 R1 实测 provider 请求校准：962,069 tokens / 2,649,223 canonical request bytes = **0.3631514 token/B**。下文明确标为“代理”，不是账单或实测 token。

按触发器统计的最终决策如下。单元格为“次数（占该触发器 attempt）”；missing 表示没有 decision_json。

| 触发器 | new | revise | manage | cancel | exit | hold | unresolved | missing |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| multiple_same_source_candidates（3,906） | 125 (3.2%) | 59 (1.5%) | 269 (6.9%) | 12 (0.3%) | 88 (2.3%) | 1,392 (35.6%) | 563 (14.4%) | 1,398 (35.8%) |
| management_without_exact_target（533） | 3 (0.6%) | 0 | 46 (8.6%) | 6 (1.1%) | 22 (4.1%) | 33 (6.2%) | 270 (50.7%) | 153 (28.7%) |
| entered_holder_language（515） | 26 (5.0%) | 9 (1.7%) | 142 (27.6%) | 2 (0.4%) | 18 (3.5%) | 65 (12.6%) | 118 (22.9%) | 135 (26.2%) |
| revision_language（208） | 12 (5.8%) | 9 (4.3%) | 30 (14.4%) | 2 (1.0%) | 4 (1.9%) | 55 (26.4%) | 18 (8.7%) | 78 (37.5%) |
| text_image_conflict（93） | 4 (4.3%) | 1 (1.1%) | 2 (2.2%) | 0 | 2 (2.2%) | 28 (30.1%) | 11 (11.8%) | 45 (48.4%) |
| apparent_entry_may_be_revision（70） | 36 (51.4%) | 16 (22.9%) | 4 (5.7%) | 0 | 0 | 1 (1.4%) | 2 (2.9%) | 11 (15.7%) |
| cancellation_language（17） | 0 | 0 | 1 (5.9%) | 1 (5.9%) | 1 (5.9%) | 1 (5.9%) | 7 (41.2%) | 6 (35.3%) |
| reply_target_disagreement（7） | 0 | 0 | 4 (57.1%) | 0 | 0 | 0 | 3 (42.9%) | 0 |

### 15.2 multiple_same_source_candidates 到底在做什么

对 3,906 次调用的互斥拆分为：

| 结果类别 | attempts | 占 3,906 | token（代理） | 占该组 token |
|---|---:|---:|---:|---:|
| **目标消歧**：动作族不变，只改变目标 thread | 60 | **1.54%** | 2.182M | 1.33% |
| **语义消歧**：改变“是不是动作 / 是什么动作” | 414 | **10.60%** | 13.763M | 8.39% |
| 目标确认：第一层目标与最终目标相同 | 215 | 5.50% | 5.264M | 3.21% |
| 语义未变 / 其他 | 1,819 | 46.57% | 51.325M | 31.28% |
| 无最终决策 | 1,398 | 35.79% | 91.551M | 55.79% |
| **合计** | **3,906** | **100%** | **164.085M** | **100%** |

在 474 次实质改变内部，严格目标消歧只有 **60 / 474 = 12.66%**，语义消歧为 **414 / 474 = 87.34%**。所以：

1. 触发判据确实只是 len(candidates) > 1，它描述的是“存在目标歧义的可能”，不等于模型最终只做目标选择。
2. 历史结果不支持把整个 multiple_same_source_candidates 解释成纯目标路由：真正改变结果的调用里，大多数改变了动作族。
3. 反过来，3,217 次“未改变或无决策”也不能据此认定上下文必需。日志能证明结果差异，不能提供“如果不用完整上下文会怎样”的因果反事实；这正是下面人工标注与后续 replay 要补的证据。

### 15.3 最近优先、按触发器分层的 60 条人工标注清单

抽样先取快照内最近 60 个不同 raw_message_id，再以最少替换补齐稀有触发器；因此它是“最近优先的分层样本”，不是严格连续的最后 60 条。边际覆盖为：multiple 58、management_without_exact_target 10、entered_holder_language 7、revision_language 3、text_image_conflict 2，其余三个稀有触发器各 1。触发器非互斥；稀有组被有意过采样，不能用此样本计算总体触发率。

60 条中，53 条有真实 usage（合计 816,427 tokens），7 条只能用上述字节代理（约 182,714 tokens）。按既有改变口径，样本含 12 条语义消歧、1 条目标消歧、7 条目标确认、40 条语义未变/其他。该分布同样只用于标注材料，不是总体率估计。

| # | attempt / raw / chat / time | 原始消息与图片摘要 | 触发器 / 候选数 | 第一层 → 上下文后 | 改变分型 | token / 请求字节 | 所有者标注 |
|---:|---|---|---|---|---|---|---|
| 1 | 4313 / 14264<br>币圈所长会员群-11分组<br>2026-09-01 10:43:29.000000Z | 📣 因为比特币提前反弹了一波 这个77450 咱们先不做了 我看77200可能更精准一点，以太币不用改<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / cancel_entry / life=1038<br>→ cancel_thread / threads=[407] / cancel_pending_entry | **否**；目标确认 | 实测 26,244<br>74,102 B × 1 = 74,102 B | □ 是 / □ 否<br>备注：____ |
| 2 | 4312 / 14263<br>币圈所长会员群-11分组<br>2026-09-01 10:42:54.000000Z | 比特币 在这就反弹了 78450附近 空一下<br>止损：15分钟有效突破78800<br>止盈：78000-77800-77450-77200 剩余尾仓如果成功空下来 就继续持有等派发<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：图表中标注价格点78456.1，与文本入场价格'78450附近'相关。；比特币价格走势图，显示下降趋势线与关键价位水平线，并有红色箭头指示潜在走势。 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br>候选 **6** | 是策略 / none<br>→ new_thread / threads=[] | **否**；语义未变/其他 | 实测 23,345<br>65,803 B × 1 = 65,803 B | □ 是 / □ 否<br>备注：____ |
| 3 | 4311 / 14262<br>舒琴会员群-11分组<br>2026-09-01 10:27:22.000000Z | [空文本]<br>**媒体：** 有图 1 张；order_screenshot：0.0089；限价单 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 19,210<br>53,076 B × 1 = 53,076 B | □ 是 / □ 否<br>备注：____ |
| 4 | 4310 / 14261<br>舒琴会员群-11分组<br>2026-09-01 10:27:22.000000Z | 跌跌跌，比特币怎么还不涨？Sol竟然都跌了10%了，还会继续跌吗，谈谈币圈后续走势。<br>1. 首先就是大家最关心的：币圈后面怎么走。说实话，现在比特币走势已经是超乎想象的强劲，短期暴涨30%后，竟然横盘一周都没有什么回调，这本身是一个强势的信号。那后面会怎么走？<br>2. 我个人认为，比特币可能会回踩一下继续涨，最好是回踩一下7.5万支撑，这样走势才会完美。现在一直在上面高位横盘，上不去也下不来，这种行情我不是很喜欢，但是也必须接受。因为币圈走势他不止是上涨和下跌，还有第三种走势：横盘！<br>3. 这里面除了7.5万强支撑外，小支撑可以留意7.7万，这里可以现货买点，合约的话 只能低倍操作，而阻力的话则是8.1万附近。行情要重启就必须要先消化完获利盘，这样才会继续进行。现在比较稳定的操作是做空原油，这个舒琴昨天讲过，就不重复了。<br>4. 那虽然比特币横盘没怎么跌，但是各个小币倒是如期回调，我非常喜欢，比如Sol已经回调了差不多10%，而Pengu双顶后更是跌了近20%！这个舒琴可是明确让大家逃顶Pengu的，欸，那现在都可以逢低接回~<br>5. 所以如图所示，舒琴在0.01逃顶Pengu后，现在在0.008附近开始重新分批买入。这来回一倒腾，又多赚了20%。所以炒币不用慌，跟着本琴慢慢来就好了，每天猛猛操作~<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；order_screenshot：0.009888；限价单 | <code>multiple_same_source_candidates</code><br><code>text_image_conflict</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 19,633<br>54,573 B × 1 = 54,573 B | □ 是 / □ 否<br>备注：____ |
| 5 | 4309 / 14260<br>币圈所长会员群-11分组<br>2026-09-01 10:23:35.000000Z | XAU 已经上下磨了好几遍了，现在开始反弹奔着第一止盈点来，各位可以适当设个保护，因为所长看到左边有个针插过一次，怕后面不够强<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：图表显示XAU价格走势，标注水平线如4420.5、4402.3、4391.9，无具体交易策略参数；水印文字'币圈博主联盟策略实时搬运群 电报:@Tarderfengge QQ:158241758' | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 25,333<br>71,524 B × 1 = 71,524 B | □ 是 / □ 否<br>备注：____ |
| 6 | 4308 / 14259<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 10:14:32.000000Z | 兄弟们，跟上节奏，直接进场‼️<br>🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ manage_thread / threads=[413] | **是**；语义消歧 | 实测 20,206<br>55,718 B × 1 = 55,718 B | □ 是 / □ 否<br>备注：____ |
| 7 | 4307 / 14256<br>米哥会员群-11分组<br>2026-09-01 09:50:48.000000Z | eth和btc都抵达了周线级别boll上轨压力<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 12,007<br>35,562 B × 1 = 35,562 B | □ 是 / □ 否<br>备注：____ |
| 8 | 4306 / 14255<br>米哥会员群-11分组<br>2026-09-01 09:50:38.000000Z | [空文本]<br>**媒体：** 有图 1 张；market_chart：ETH/USDT；2,452.56 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 11,837<br>34,159 B × 1 = 34,159 B | □ 是 / □ 否<br>备注：____ |
| 9 | 4305 / 14254<br>米哥会员群-11分组<br>2026-09-01 09:50:38.000000Z | [空文本]<br>**媒体：** 有图 1 张；market_chart：Bitget交易界面；BTCUSDT | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 11,369<br>32,574 B × 1 = 32,574 B | □ 是 / □ 否<br>备注：____ |
| 10 | 4304 / 14253<br>米哥会员群-11分组<br>2026-09-01 09:48:37.000000Z | https://fxtwitter.com/tradermige/status/2094721376947142725?s=46&t=KKMiMVU2m4rrdItyJ97y2Q<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：VIP策略群联系、返佣信息等广告文本；TradingView (Trader 米哥) | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 10,978<br>31,544 B × 1 = 31,544 B | □ 是 / □ 否<br>备注：____ |
| 11 | 4303 / 14252<br>币圈所长会员群-11分组<br>2026-09-01 09:24:41.000000Z | 🔔🔔所长第23期集训班开始招生了，<br>活动到9月10日，有意向学习提升的同学<br>✍️可以联系 所长<br>🟢投资自己是最好的投资！💵<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；advertisement：图片中无任何交易策略参数（如入场、止损、止盈）或相关标识；所长课堂第23期集训班宣传图，包括课程介绍（K线几何形态学教学班）、价格（活动价588U）、上课时间等，为广告内容 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 23,762<br>67,560 B × 1 = 67,560 B | □ 是 / □ 否<br>备注：____ |
| 12 | 4302 / 14251<br>币圈所长会员群-11分组<br>2026-09-01 09:10:29.000000Z | 止损：15分钟叠穿4360 止损给的小一点<br>止盈：4400-4420-4455<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ unresolved / threads=[] | **否**；语义未变/其他 | 实测 23,712<br>64,832 B × 1 = 64,832 B | □ 是 / □ 否<br>备注：____ |
| 13 | 4301 / 14250<br>币圈所长会员群-11分组<br>2026-09-01 09:08:44.000000Z | XAU 在这多一下 试试<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：图表显示XAU价格走势，标注价格点如4373.0、4378.2、4380.6，但未包含交易策略参数（入场、止损、止盈）；水印文字'币圈博主联盟策略实时搬运群 电报:@Tarderfengge QQ:158241758' | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ unresolved / threads=[] | **否**；语义未变/其他 | 实测 22,353<br>63,365 B × 1 = 63,365 B | □ 是 / □ 否<br>备注：____ |
| 14 | 4300 / 14249<br>比特币陈哥会员群-11分组<br>2026-09-01 09:08:14.000000Z | 限价挂单不要去整数，区间入场正常仓位操作。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 16,923<br>51,507 B × 1 = 51,507 B | □ 是 / □ 否<br>备注：____ |
| 15 | 4299 / 14246<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 08:39:48.000000Z | 🔥到成本价附近直接出局！<br>🔥观察一下行情再进场！<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / exit_position / life=1040<br>→ exit_thread / threads=[409] / exit_full | **否**；目标确认 | 实测 17,754<br>50,302 B × 1 = 50,302 B | □ 是 / □ 否<br>备注：____ |
| 16 | 4298 / 14245<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 08:39:40.000000Z | [空文本]<br>**媒体：** 有图 1 张；market_chart：77947.0；0.0079% | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 16,288<br>48,434 B × 1 = 48,434 B | □ 是 / □ 否<br>备注：____ |
| 17 | 4297 / 14243<br>大漂亮社区 11分组<br>2026-09-01 08:33:20.000000Z | 🧛‍♂️分析师—#Nick<br>大饼77800附近可以止盈30%先<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / position_update / partial_take_profit<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 8,830<br>20,832 B × 1 = 20,832 B | □ 是 / □ 否<br>备注：____ |
| 18 | 4296 / 14241<br>米娅 vip会员群 11分组<br>2026-09-01 08:22:50.000000Z | 等下笔信号！<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 7,221<br>16,957 B × 1 = 16,957 B | □ 是 / □ 否<br>备注：____ |
| 19 | 4295 / 14240<br>米娅 vip会员群 11分组<br>2026-09-01 08:22:43.000000Z | 这两天行情整体还是偏震荡，波动空间比较小，短线操作有利润就先落袋为安，不必过度恋战。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 6,348<br>15,495 B × 1 = 15,495 B | □ 是 / □ 否<br>备注：____ |
| 20 | 4294 / 14239<br>米娅 vip会员群 11分组<br>2026-09-01 08:19:58.000000Z | BTC现价78100，加仓仓位比较大，等效获利1000点，全部仓位止盈出局！<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：78109.5；市场K线图，显示价格在78109.5附近 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / exit_position / full_exit<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 7,622<br>14,886 B × 1 = 14,886 B | □ 是 / □ 否<br>备注：____ |
| 21 | 4293 / 14220<br>米娅 vip会员群 11分组<br>2026-09-01 05:40:15.000000Z | BTC现价79200附近，可加仓同等仓位，入场均价78600<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / position_update / add_position<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 6,351<br>12,088 B × 1 = 12,088 B | □ 是 / □ 否<br>备注：____ |
| 22 | 4292 / 14237<br>比特币军长-11分组<br>2026-09-01 07:32:06.000000Z | 💰如上图，最好是三角震荡直接向上突破，如往下破的话不知多深，止损设好；💰<br>---------------<br>当前参考价格：<br>BTC：78780<br>ETH：2475<br>---------------<br>军长禁言群免费进，电报联系<br>仅为市场观点分享，不构成任何交易建议。<br>不承诺收益，请理性判断并自行承担风险。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 13,623<br>41,081 B × 1 = 41,081 B | □ 是 / □ 否<br>备注：____ |
| 23 | 4291 / 14236<br>比特币军长-11分组<br>2026-09-01 07:32:04.000000Z | 如上图，最好是三角震荡直接向上突破，如往下破的话不知多深，止损设好；<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 12,617<br>37,232 B × 1 = 37,232 B | □ 是 / □ 否<br>备注：____ |
| 24 | 4290 / 14234<br>比特币军长-11分组<br>2026-09-01 07:29:40.000000Z | 💰比特止损设77000，以太止损设2400；如打到不重启💰<br>---------------<br>当前参考价格：<br>BTC：78758<br>ETH：2475<br>---------------<br>军长禁言群免费进，电报联系<br>仅为市场观点分享，不构成任何交易建议。<br>不承诺收益，请理性判断并自行承担风险。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / none<br>→ unresolved / threads=[] | **否**；语义未变/其他 | 实测 12,500<br>35,559 B × 1 = 35,559 B | □ 是 / □ 否<br>备注：____ |
| 25 | 4289 / 14233<br>比特币军长-11分组<br>2026-09-01 07:29:39.000000Z | 比特止损设77000，以太止损设2400；如打到不重启<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / none<br>→ unresolved / threads=[] | **否**；语义未变/其他 | 实测 12,108<br>32,619 B × 1 = 32,619 B | □ 是 / □ 否<br>备注：____ |
| 26 | 4288 / 14224<br>币圈所长会员群-11分组<br>2026-09-01 05:52:01.000000Z | 比特币 突破这个趋势线以后，还是没有下探让我们去做多，那各位就要注意今天视频讲的，积累了空头，会不会上去再打流动性 这个位置 还是可以参考80150这里，先注意一下<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：图表中黄色虚线标注阻力位在80000-80500附近，与文本提及的80150水平相关；比特币价格走势图，显示趋势线、关键水平线（如80000-80500区域）和红色箭头指示潜在走势 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 22,821<br>61,906 B × 1 = 61,906 B | □ 是 / □ 否<br>备注：____ |
| 27 | 4287 / 14223<br>米哥会员群-11分组<br>2026-09-01 05:45:15.000000Z | [空文本]<br>**媒体：** 有图 1 张；unrelated | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 9,572<br>27,618 B × 1 = 27,618 B | □ 是 / □ 否<br>备注：____ |
| 28 | 4286 / 14222<br>米哥会员群-11分组<br>2026-09-01 05:45:05.000000Z | 为了9月盯盘为大家更好的服务，我买入了新装备<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 9,208<br>26,463 B × 1 = 26,463 B | □ 是 / □ 否<br>备注：____ |
| 29 | 4284 / 14219<br>大镖客 11分组<br>2026-09-01 05:39:59.000000Z | 大镖客·Andy<br>跟分析完全一样<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 13,512<br>38,531 B × 1 = 38,531 B | □ 是 / □ 否<br>备注：____ |
| 30 | 4283 / 14217<br>米娅 vip会员群 11分组<br>2026-09-01 05:39:57.000000Z | 止损位上移300点，重设为79700<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / position_update / move_stop_loss<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 6,051<br>10,593 B × 1 = 10,593 B | □ 是 / □ 否<br>备注：____ |
| 31 | 4282 / 14218<br>大镖客 11分组<br>2026-09-01 05:39:56.000000Z | 大镖客·Andy<br>现价进场注意保护利润，注意79200附近压力位，78800突破站稳4个小时可以继续反弹<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：79089.9；24h high: 79230.8, low: 77645.9 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / position_update / life=1041<br>→ manage_thread / threads=[410] / move_stop_to_protect | **否**；目标确认 | 实测 14,887<br>38,177 B × 1 = 38,177 B | □ 是 / □ 否<br>备注：____ |
| 32 | 4281 / 14215<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 05:38:00.000000Z | 分享盈利给我，我看看大家并仓的均价在哪里？<br>❤️❤️❤️❤️❤️❤️❤️❤️❤️❤️❤️❤️<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 17,221<br>51,570 B × 1 = 51,570 B | □ 是 / □ 否<br>备注：____ |
| 33 | 4280 / 14214<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 05:37:46.000000Z | 🔥现目前两笔多单分别获利：1500+600点！<br>🔥持仓收益达到200％➕90％！<br>分批止盈50％！！！<br>推保护价：78000！(成本价)<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / position_update / life=1040 / partial_take_profit, move_stop_to_protect<br>→ manage_thread / threads=[409] / partial_take_profit | **否**；目标确认 | 实测 18,693<br>51,448 B × 1 = 51,448 B | □ 是 / □ 否<br>备注：____ |
| 34 | 4278 / 14213<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 05:37:01.000000Z | [空文本]<br>**媒体：** 有图 1 张；market_chart：+1.38%；79058.5 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 16,481<br>48,481 B × 1 = 48,481 B | □ 是 / □ 否<br>备注：____ |
| 35 | 4277 / 14212<br>提阿非罗 初塔 11分组<br>2026-09-01 05:32:58.000000Z | #tia<br>目前盤口是明顯看漲的<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：78915.6；1h | <code>text_image_conflict</code><br>候选 **0** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 4,800<br>10,580 B × 1 = 10,580 B | □ 是 / □ 否<br>备注：____ |
| 36 | 4276 / 14211<br>三马哥会员群-11分组<br>2026-09-01 04:51:54.000000Z | https://app.binance.com/uni-qr/cspa/45233639565673?l=zh-CN&r=SDR9QGU2&source=host_share&uc=web_square_share_link&us=telegram<br>“我正在币安广场收听语音直播“BTC冲8万”，和我一起在此处收听：…”<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：BTCUSDT；用户评论如'你咋贷的款'、'香港90平就是豪宅吗'、'马哥赚钱能力强'等，内容为闲聊，非策略指令。 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 23,719<br>66,785 B × 1 = 66,785 B | □ 是 / □ 否<br>备注：____ |
| 37 | 4275 / 14210<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 04:23:05.000000Z | 🔥现目前两笔多单分别获利：1200+300点！<br>🔥持仓收益达到180％➕40％！<br>多单继续持有，等待今日份拉升！<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / position_update / life=1040 / hold<br>→ manage_thread / threads=[409] / hold_update | **否**；目标确认 | 实测 16,587<br>46,547 B × 1 = 46,547 B | □ 是 / □ 否<br>备注：____ |
| 38 | 4274 / 14209<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 04:22:18.000000Z | [空文本]<br>**媒体：** 有图 1 张；market_chart：+1.45%；78707.7 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 15,065<br>43,955 B × 1 = 43,955 B | □ 是 / □ 否<br>备注：____ |
| 39 | 4273 / 14208<br>三马哥会员群-11分组<br>2026-09-01 04:07:42.000000Z | BTC  做多     仓位思路强平控制U及以下55000U及以下<br>78650附近市价直接进  100倍 2%保证金<br>再挂77188 100倍 3%保证金<br>第一止盈 80288 止盈70%移动保本<br>第二止盈81388<br>止损75000<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br>候选 **11** | 是策略 / none<br>→ new_thread / threads=[] | **否**；语义未变/其他 | 实测 21,278<br>58,034 B × 1 = 58,034 B | □ 是 / □ 否<br>备注：____ |
| 40 | 4272 / 14207<br>大镖客 11分组<br>2026-09-01 03:52:03.000000Z | 大镖客·Andy<br>https://youtu.be/MBNfDrYWudA<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：20260901；比特币图标 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 13,541<br>35,475 B × 1 = 35,475 B | □ 是 / □ 否<br>备注：____ |
| 41 | 4271 / 14204<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 02:55:12.000000Z | 兄弟们，跟上节奏，直接进场‼️<br>🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️🏎️<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / entry_confirm / life=1040<br>→ manage_thread / threads=[409] | **是**；语义消歧 | 实测 17,899<br>48,171 B × 1 = 48,171 B | □ 是 / □ 否<br>备注：____ |
| 42 | 4270 / 14196<br>欧阳火箭滚仓班🚀 11分组<br>2026-09-01 02:26:19.000000Z | 🔥本轮多单最大获利1700点！🔥<br>🔥持仓收益达到230％！！！🔥<br>继续推保护价持仓：77500！！！<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **2** | 非策略 / position_update / move_stop_to_protect<br>→ manage_thread / threads=[409] / move_stop_to_protect | **是**；目标消歧 | 实测 14,653<br>43,089 B × 1 = 43,089 B | □ 是 / □ 否<br>备注：____ |
| 43 | 4267 / 14193<br>比特币军长-11分组<br>2026-09-01 02:17:41.000000Z | 💰CRV多单止盈💰<br>---------------<br>当前参考价格：<br>CRV：0.3482<br>---------------<br>军长禁言群免费进，电报联系<br>仅为市场观点分享，不构成任何交易建议。<br>不承诺收益，请理性判断并自行承担风险。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / exit_position<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 21,494<br>29,840 B × 2 = 59,680 B | □ 是 / □ 否<br>备注：____ |
| 44 | 4266 / 14201<br>比特币军长-11分组<br>2026-09-01 02:50:08.000000Z | [空文本]<br>**媒体：** 有图 1 张；profit_review：币圈博主跟单策略实时搬运群 @Tarderfengge QQ:158241758；CRV多单盈利 \| 现货涨幅 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / exit_position<br>→ hold / threads=[] | **是**；语义消歧 | 实测 10,998<br>31,091 B × 1 = 31,091 B | □ 是 / □ 否<br>备注：____ |
| 45 | 4265 / 14200<br>币圈所长会员群-11分组<br>2026-09-01 02:45:26.000000Z | https://youtu.be/CkJt8GCVAmU?si=824Ihlz4aOECZdW1<br>#比特幣 #比特币 #以太幣 #以太坊 #btc #eth #bitcoin #美股<br>比特幣高位繼續盤整 \| BTC小級別會不會積累流動性去打 \| 以太幣衝土狗導致變強？\|<br>比特币高位继续盘整 \| BTC小级别会不会积累流动性去打 \| 以太币冲土狗导致变强？\|<br>所长课堂公开频道：https://t.me/suozhangteac...<br>— 币圈所长课堂<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；advertisement：卡通人物和问号，表示疑问，无具体策略信息；比特币卡住了！现在做多还是做空？关键点位别做错 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 21,612<br>60,418 B × 1 = 60,418 B | □ 是 / □ 否<br>备注：____ |
| 46 | 4264 / 14199<br>币圈所长会员群-11分组<br>2026-09-01 02:43:58.000000Z | 【大饼怎么吃？人怎么活？-哔哩哔哩直播】 https://b23.tv/7xaaTj1<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 19,683<br>54,763 B × 1 = 54,763 B | □ 是 / □ 否<br>备注：____ |
| 47 | 4263 / 14198<br>币圈所长会员群-11分组<br>2026-09-01 02:43:58.000000Z | 哔哩哔哩（bilibili）直播，在这里看见最年轻的生活方式，学习、游戏、电竞、宅舞、唱见、绘画、美食等等应有尽有，快来捕捉你最喜欢的up主最真实的一面吧！弹幕，礼物，道具，活动多种玩法，bilibili 直播让您拉进与小伙伴们之间的距离。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 19,851<br>54,208 B × 1 = 54,208 B | □ 是 / □ 否<br>备注：____ |
| 48 | 4262 / 14197<br>币圈所长会员群-11分组<br>2026-09-01 02:43:56.000000Z | 直播来聊会<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 18,576<br>50,340 B × 1 = 50,340 B | □ 是 / □ 否<br>备注：____ |
| 49 | 4259 / 14192<br>比特币军长-11分组<br>2026-09-01 02:17:39.000000Z | CRV多单止盈<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / exit_position<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 10,134<br>27,347 B × 1 = 27,347 B | □ 是 / □ 否<br>备注：____ |
| 50 | 4258 / 14188<br>比特币飞扬 11分组<br>2026-09-01 01:54:29.000000Z | [空文本]<br>**媒体：** 有图 1 张；position_screenshot：@Tarderfengge QQ:158241758；partial_take_profit, move_stop_to_protect | <code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **5** | 非策略 / position_update / partial_take_profit, move_stop_to_protect<br>→ unresolved / threads=[] | **是**；语义消歧 | 实测 11,356<br>29,696 B × 1 = 29,696 B | □ 是 / □ 否<br>备注：____ |
| 51 | 4257 / 14187<br>比特币飞扬 11分组<br>2026-09-01 01:52:41.000000Z | [空文本]<br>**媒体：** 有图 1 张；advertisement：@Tarderfengge QQ:158241758；https://youtu.be/Ho8lnbUvxbU?si=oAJXRKs2lQ1l6JID | <code>multiple_same_source_candidates</code><br>候选 **5** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 9,874<br>27,225 B × 1 = 27,225 B | □ 是 / □ 否<br>备注：____ |
| 52 | 4256 / 14185<br>币圈所长会员群-11分组<br>2026-09-01 01:30:56.000000Z | 🔔 提醒一下可能有些新同学刚来的，我们策略盈利以后，接近第一止盈了以后，建议各位就止盈5层以上，然后设置成本保护，这是最稳妥的方式，后面可以选择5-2-2-1，当然最稳的同学你就直接仓位9-1就完事了。<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **20** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 19,704<br>53,955 B × 1 = 53,955 B | □ 是 / □ 否<br>备注：____ |
| 53 | 4255 / 14184<br>米哥会员群-11分组<br>2026-09-01 01:14:03.000000Z | 我今早去看奥德赛了，分析下午随缘出<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 实测 8,983<br>25,417 B × 1 = 25,417 B | □ 是 / □ 否<br>备注：____ |
| 54 | 4247 / 14162<br>大镖客 11分组<br>2026-08-31 17:46:39.000000Z | 大镖客·Andy<br>买入卖出、分歧点信号已在Bitget更新，及时查看当前信号！<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>revision_language</code><br>候选 **1** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 代理约 11,361<br>31,285 B × 1 = 31,285 B | □ 是 / □ 否<br>备注：____ |
| 55 | 4237 / 13864<br>比特币军长-11分组<br>2026-08-29 23:13:35.000000Z | 今日无视频更新<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>revision_language</code><br><code>multiple_same_source_candidates</code><br>候选 **4** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 代理约 18,661<br>51,385 B × 1 = 51,385 B | □ 是 / □ 否<br>备注：____ |
| 56 | 4234 / 13848<br>欧阳火箭滚仓班🚀 11分组<br>2026-08-29 15:12:54.000000Z | 🔥设置好止盈止损持仓过夜！🔥<br>止盈位：73070！！！<br>止损位：78700！！！<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br>候选 **5** | 非策略 / position_update / life=1030 / set_stop_loss, set_take_profit<br>→ manage_thread / threads=[399] / risk_update | **否**；目标确认 | 代理约 33,519<br>92,299 B × 1 = 92,299 B | □ 是 / □ 否<br>备注：____ |
| 57 | 4218 / 13827<br>大镖客 11分组<br>2026-08-29 14:05:41.000000Z | 大镖客·Andy<br>bg信号已更新，现在分歧点在77600，横盘震荡被磨成了买入信号，短期内注意分歧点的情况，站稳1个小时则有概率延续买入信号，只有跌破才会再次转为卖出<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>revision_language</code><br><code>multiple_same_source_candidates</code><br>候选 **3** | 非策略 / none<br>→ hold / threads=[] | **否**；语义未变/其他 | 代理约 18,330<br>50,475 B × 1 = 50,475 B | □ 是 / □ 否<br>备注：____ |
| 58 | 4194 / 13792<br>三马哥会员群-11分组<br>2026-08-29 07:05:41.000000Z | BTC 中线 做空     仓位思路强平控制在95000U及以上<br>77480附近市价直接空 100倍 3%保证金<br>再挂78888  100倍 3%保证金<br>第一止盈76688 止盈70%仓位移动保本损<br>第二止盈75188<br>第三止盈72000<br>止损79800<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>entered_holder_language</code><br><code>multiple_same_source_candidates</code><br><code>apparent_entry_may_be_revision</code><br>候选 **4** | 是策略 / none<br>→ revise_thread / threads=[403] | **是**；语义消歧 | 代理约 53,266<br>146,676 B × 1 = 146,676 B | □ 是 / □ 否<br>备注：____ |
| 59 | 4170 / 13749<br>比特智 智哥 11分组<br>2026-08-28 21:42:39.000000Z | 空单挂单取消，多上去再找机会空，最近的止盈和方向感无敌了，只是我们过于保守了！<br>@Tarderfengge QQ:158241758<br>**媒体：** 无图 | <code>cancellation_language</code><br><code>management_without_exact_target</code><br><code>multiple_same_source_candidates</code><br>候选 **9** | 非策略 / cancel_entry<br>→ unresolved / threads=[] | **是**；语义消歧 | 代理约 20,512<br>56,483 B × 1 = 56,483 B | □ 是 / □ 否<br>备注：____ |
| 60 | 1673 / 11430<br>三马哥会员群-11分组<br>2026-08-18 14:59:34.000000Z | 走70%仓位利润，多单吃大肉了，汇报！#ET H<br>@Tarderfengge QQ:158241758<br>**媒体：** 有图 1 张；market_chart：ETHUSDT 1,913.53；ETH | <code>multiple_same_source_candidates</code><br><code>reply_target_disagreement</code><br>候选 **20** | 非策略 / position_update / life=713 / partial_take_profit<br>→ manage_thread / threads=[83] / partial_take_profit | **否**；目标确认 | 代理约 27,066<br>74,532 B × 1 = 74,532 B | □ 是 / □ 否<br>备注：____ |

### 15.4 “仅目标消歧”精简 prompt 的量化上界

按需求模拟“候选列表 + 回复链 + 当前消息，不含完整历史消息窗口”，同时保留其余非历史安全字段。只在事后已知为严格目标消歧的 60 次调用上计算：

| 指标 | 现有完整请求 | 精简模拟 | 降幅 |
|---|---:|---:|---:|
| provider-request 加权字节 | 6,007,152 B | 890,141 B | **85.18%** |
| token（同一代理率） | 2,181,502 | 323,253 | **85.18%** |
| 35 天历史合计节省 | — | 1,858,249 tokens | 约 **53,093 tokens/日** |

这是一个**事后上界**，不是可立即上线的节省。严格目标消歧只占 multiple 组 token 的 1.33%；即使这 60 次全部安全精简，对整个 multiple 历史 token 的净节省也只有约 **1.13%**。若人工把大量“目标确认”或“语义未变”样本也判定为只需精简上下文，机会会变大，但必须重新 replay，不能从当前 outcome 自动外推。

主要风险：

- “目标消歧”标签依赖完整模型调用后的结果，线上调用前并不知道某条消息会落入该类；用它直接路由会产生 look-ahead。
- 候选摘要可能不足以表达策略 episode、时间顺序、代词指向、作者习惯或较早管理消息；省掉的历史可能正是选择正确 target 的证据。
- 相同动作族不代表相同风险。选错 lifecycle 仍可能让管理或平仓语义作用到错误对象。
- 当前 60 次只证明完整 prompt 的历史 outcome；不能证明精简 prompt 会生成相同 target、置信度、适用性与拒绝结果。

在行为变更前至少需要：

1. 对全部 60 次严格目标消歧历史调用做同模型、同参数的精简 prompt replay，要求 action family、target thread set、applicability 与 risk-reducing fanout **100% 一致**。
2. 把 414 次语义消歧作为负对照，证明任何调用前 gate 都不会把它们误送到精简路径；否则节省没有安全意义。
3. 对本节 60 条由所有者完成人工标注，先验证机器分型与领域判断是否一致。
4. 生产只能先 shadow：完整 prompt 与旧决策继续权威，精简结果只记录差异；预先声明样本量与零 target/action 差异门槛。
5. 任一目标、动作族、适用性或资金安全决策差异都使方案 fail closed，保留完整 prompt。

分类建议：人工白名单调整属于**配置变更但会改变行为**；精简 prompt 与调用前 gate 属于**代码 + 部署 + 行为变更**；本节统计、标注与历史 replay 属于**只读分析**。本轮没有实施任何一项变更。

### 15.5 交叉复核与限制

同一固定 legacy cohort 由两条独立只读路径复核：SQLite 直接聚合得到 multiple=3906、有可比较决策 2,508、实质改变 474；Python 逐行分类得到语义改变 414、目标改变 60，两者精确相加为 474。八个触发器的 decision 分布逐行求和均等于各组 attempt；样本恰好 60 个不同 raw message，并覆盖全部八个触发器。

限制仍然明确：legacy token 几乎全部是代理；触发器重叠导致各组 token 不可相加；最新 60 条是最近优先分层样本而非概率样本；模型 outcome 不能回答“完整上下文是否因果必需”；图片摘要来自已持久化识别证据，不是本轮重新识图。上述限制使本节足以区分“目标改变”和“动作族改变”，但不足以授权 prompt 精简、窗口变更或触发器调整。

## 16. 候选数阈值与上下文解析有效性：只读全量反事实（2026-09-01）

### 16.1 固定口径与数据质量边界

本节只读。生产 SQLite 以 URI `mode=ro` 打开，历史总体仍固定为 `context_resolution_attempts.id <= 4245`；其中 `multiple_same_source_candidates` 命中 3,906 次，实质改变仍为 474 次（语义改变 414、目标改变 60），与第 15 节完全一致。候选数直接取调用前 `request_summary_json.candidate_strategy_threads`；线上判据仍是 `authoritative_recognition.py:193-194` 的 `len(candidates) > 1`，本轮没有修改它。

token 沿用第 15 节固定代理率 0.3631514 token / canonical request byte，并按持久化 provider 请求次数加权。legacy cohort 没有真实 usage，因此绝对 token 是代理；相对字节节省不依赖 tokenizer，方向更可靠。

有一个需要显式保留的数据定义冲突：按当前八个确切调用前触发器逐行重算，“只有 multiple、没有任何其他触发器”的行是 **2,999**，不能复现第 4 节较早记录的 3,005。八个触发器各自总数均与既有结果一致，冲突只发生在组合归类。为避免高估阈值调整后的真实节省，本节使用更严格、可逐行复现的 2,999；下面同时给出“从 multiple 组消失的毛数”和“没有其他触发器接管、实际可避免调用的净数”。

### 16.2 候选数与实质改变率

| 候选数 | 调用 | 实质改变 | 改变率（Wilson 95% CI） | 平均 token（代理） | 总 token（代理） |
|---|---:|---:|---:|---:|---:|
| 2 | 1,010 | 80 | 7.92%（6.41%–9.75%） | 37,128 | 37.499M |
| 3 | 434 | 48 | 11.06%（8.44%–14.36%） | 29,896 | 12.975M |
| 4–5 | 1,213 | 198 | **16.32%**（14.35%–18.51%） | 40,661 | 49.322M |
| 6–10 | 454 | 55 | 12.11%（9.43%–15.44%） | 41,937 | 19.039M |
| 11–20 | 795 | 93 | 11.70%（9.65%–14.12%） | 56,918 | 45.250M |
| >20 | 0 | 0 | 无样本 | — | 0 |
| **合计** | **3,906** | **474** | **12.14%** | **42,008** | **164.085M** |

`>20` 没有样本不是流量偶然：`strategy_thread_candidates.py:239, 363-364` 默认并硬限制 `max_candidates <= 20`。

候选数与“是否实质改变”的 Pearson 点二列相关为 **r = 0.0109**；把候选数做并列秩后的 Spearman 为 **ρ = 0.0528**。两者都接近 0，而且分桶率在 4–5 个候选达到峰值，不呈单调下降。因此全量数据的答案是：**候选数与有效性基本无单调关系；不是负相关。** 60 条人工样本里“2 个候选有效、20 个候选无效”的观察是真实个案，但不是总体规律。

### 16.3 单纯提高候选阈值的真实代价

“毛排除”表示不再由 multiple 触发的行；其中若还命中其他触发器，解析仍会发生。“净避免”才是系统实际少掉的调用与 token。

| 新阈值 | 毛排除 | 仍由其他触发器调用 | 净避免调用 | 净节省 token（代理） | 丢失实质改变 | 其中语义改变 |
|---|---:|---:|---:|---:|---:|---:|
| `>2` | 1,010 | 198 | 812（20.79%） | 30.875M（18.82%） | 25（占全部改变 5.27%） | **25** |
| `>3` | 1,444 | 295 | 1,149（29.42%） | 41.321M（25.18%） | 41（8.65%） | **41** |
| `>5` | 2,657 | 630 | 2,027（51.89%） | 76.771M（46.79%） | 83（17.51%） | **82** |
| `>10` | 3,111 | 746 | 2,365（60.55%） | 91.560M（55.80%） | 96（20.25%） | **95** |

这不是划算的单变量改动。最温和的 `>2` 虽可省约 18.82% token，却历史上会漏掉 25 次实质改变，而且 25 次全是“是不是动作 / 是什么动作”的语义改变，不是仅仅少选一个 target。阈值越高，节省和漏失几乎一起扩大；候选数本身没有提供一个低风险断点。

### 16.4 第一层结论与非策略消息形态

| 第一层结论 | 调用 | 实质改变 | 改变率（Wilson 95% CI） | 平均 token（代理） | 总 token（代理） |
|---|---:|---:|---:|---:|---:|
| 是策略 | 216 | 28 | 12.96%（9.12%–18.10%） | 44,284 | 9.565M |
| 非策略 | 3,690 | 446 | 12.09%（11.07%–13.18%） | 41,875 | 154.520M |

两组只差 0.87 个百分点，置信区间高度重叠。第一层“是策略 / 非策略”本身几乎没有区分力；上下文解析的大多数有效改变恰恰发生在第一层“非策略”中。

对 3,690 条“非策略”进一步使用可复现但非权威的文本分类：

- “仅图片”：当前文本去空白后为空，且 `media_assets` 至少一行；本 cohort 的 1,088 条空文本全部满足该条件，没有“空文本且无图片”。
- “明确祈使动作词”：固定分析词表命中进场、开仓、做多/做空、买入/卖出、挂单、加减仓、止盈止损、平仓出局、撤单取消、保本/保护、继续持有等。它不是生产判据，也不处理否定、引用或反讽。
- “长篇评论”：非空、不命中上述动作词、Unicode 字符数至少 200。阈值只用于本次描述，不代表应上线的规则。
- “其他文本”：其余非空非策略消息。

| 非策略消息形态 | 调用 | 实质改变 | 改变率 | 平均 token（代理） | 总 token（代理） |
|---|---:|---:|---:|---:|---:|
| 仅图片（也等于全部空文本） | 1,088 | 80 | 7.35% | 40,083 | 43.610M |
| 含明确祈使动作词 | 1,321 | 313 | **23.69%** | 42,565 | 56.229M |
| 长篇评论、无动作词 | 93 | 0 | 0%（Wilson 上界 3.97%） | 44,237 | 4.114M |
| 其他文本 | 1,188 | 53 | 4.46% | 42,564 | 50.567M |

动作词是强信号，但并非充分条件：它会把“止损设好”这类观点、引用和复盘也计入；图片组仍有 80 次改变，不能因为没有文字就跳过；长篇评论 93/93 未改变是值得人工复核的低信号区域，但 0 次观测不等于未来风险为 0。

### 16.5 交易对可用性

生产 `trading_settings.global.allowed_symbols` 的只读 after-image 是 `BTC / ETH / SOL`。本节取第一层当前消息的 `strategy.symbol`，否则取 `lifecycle_event.symbol`；没有精确 symbol 记为 unknown。候选列表里碰巧存在某个 symbol 不算当前消息涉及它。

| 第一层当前 symbol | 调用 | 实质改变 | 改变率（Wilson 95% CI） | 平均 token（代理） | 总 token（代理） |
|---|---:|---:|---:|---:|---:|
| 启用交易对（BTC/ETH/SOL） | 926 | 280 | 30.24%（27.37%–33.27%） | 43,053 | 39.867M |
| 有 symbol、但不精确属于启用集合 | 270 | 113 | **41.85%**（36.12%–47.81%） | 41,126 | 11.104M |
| unknown | 2,710 | 81 | **2.99%**（2.41%–3.70%） | 41,739 | 113.114M |

真正有区分力的是“第一层有没有识别出明确 symbol”，不是“是否在交易白名单”。非启用组反而有最高改变率；上下文仍可能把它解析为管理、拒绝或 lifecycle 语义，不能因无法自动下单就视为无价值。XAU 与 XAUUSD 合计只有 9 次、4 次改变、约 0.280M token，个体样本太小；不能单独对 XAU 下结论。`BTC/ETH` 这类组合字符串也被计入“不精确属于启用集合”，因为它不能直接等同一个 `allowed_symbols` 项。

### 16.6 四个维度的区分度排序

为避免只看最大最小率，使用同一 3,906 行、同一二元 outcome 计算 Cramér's V，并以 outcome entropy 归一化 mutual information 交叉检查。候选数使用第 16.2 节六桶；操作意图使用上述动作词 yes/no；交易对使用 enabled/nonenabled/unknown 三类。

| 排名 | 维度 | Cramér's V | 归一化信息增益 | 解释 |
|---:|---|---:|---:|---|
| 1 | 交易对可用性/明确性 | **0.430** | **22.76%** | 主要来自 known symbol 与 unknown 的巨大差异，不是“只保留白名单” |
| 2 | 明确操作意图 | **0.247** | **8.08%** | 全体含动作词 338/1,520 改变（22.24%），无动作词 136/2,386（5.70%） |
| 3 | 候选数分桶 | 0.098 | 1.31% | 有小幅分层，但无单调阈值 |
| 4 | 第一层是策略/非策略 | 0.006 | 0.005% | 实质上没有区分力 |

排序在两种统计量下完全一致：**symbol 明确性 > 操作意图 > 候选数 > 第一层策略标签**。

### 16.7 数据支持的收紧方向

结论不是“把 `>1` 改成某个更大的数字”。更有依据的方向是仅研究“sole multiple + 第一层非策略 + 无明确动作词 + symbol unknown”的低信号区域，再用文本形态或高候选数做二次筛选：

| 只读历史反事实 | 净避免调用 | 节省 token（代理） | 漏失实质改变 | 其中语义改变 |
|---|---:|---:|---:|---:|
| 仅长篇评论、无动作词 | 84 | 3.834M（占 multiple 2.34%） | **0** | 0 |
| 无动作词 + symbol unknown +（长篇评论或候选数 11–20） | 476 | 26.179M（15.95%） | **2** | **2** |

第一行是最保守、最适合人工标注和 shadow replay 的起点，但 84 个历史零改变只给出约 4.37% 的 Wilson 95% 上界，不能直接称为无害。第二行显示组合信号能获得明显节省，但已经漏掉两次语义改变，因此也不能直接上线。建议顺序是：

1. 优先人工标注 93 条“长篇评论、无动作词”全量记录，并单独查看 sole-multiple 的 84 条；
2. 对第二行漏掉的两次语义改变做个案复核，找出缺少的前置可观测信号；
3. 用旧完整 prompt 权威、候选 gate 只记差异的 shadow replay 验证；
4. 未达到实质改变 100% 召回前，安全可实施节省仍为 0%。

### 16.8 哪些结论已经充分，哪些仍需人工标注

样本充分、可作为当前历史总体描述的结论：

- 候选数没有负向或单调相关；2–20 的每个非空桶都有 434–1,213 条，相关系数接近 0。
- 第一层“是策略/非策略”区分度近零。
- 明确动作词与 symbol 明确性比候选数有更强的历史区分度。
- 单纯提高阈值会漏掉非零且以语义改变为主的结果；表中漏失数是逐行确定值，不是抽样估计。

仍需人工标注或 shadow 才能确定的结论：

- 93 条长篇评论是否真的都不需要上下文；零历史改变不等于完整 prompt 没有防错价值。
- 动作词命中是否为真实祈使语义，还是引用、否定、广告或复盘；词表分类有语义误差。
- unknown symbol 是否可以安全跳过；其中仍有 81 次改变，必须逐案理解。
- 单个非启用交易对，特别是 XAU/XAUUSD，样本不足。
- token 绝对值和未来节省仍是字节代理；需要继续积累真实 provider usage。

本节只给出分析与收紧方向，没有修改 `requires_context_resolution`、任何触发判据、prompt、setting、数据、release 或服务。

## 17. 候选状态与第一层事件联合门禁：只读反事实（2026-09-01）

### 17.1 固定口径与反事实边界

本节继续使用 `context_resolution_attempts.id <= 4245` 的固定 legacy cohort，以及第 15/16 节完全相同的实质改变定义和 0.3631514 token / canonical request byte 代理率。总体仍是 3,906 次 `multiple_same_source_candidates` 调用、474 次实质改变（语义 414、目标 60）、164.085M 代理 token。

候选状态直接取调用时已持久化的 `candidate_strategy_threads[].status`。方向 A 只替换 `len(candidates) > 1` 中的计数集合，不删除传给模型的候选，也不改动其他七个触发器；方向 B 只限制 multiple 是否可单独触发。“毛排除”是 multiple 不再成立的行，“净避免”进一步要求该行没有其他触发器接管。只有净避免行的 token 才算实际节省，其中的实质改变才算真实漏失。

独立 SQL 复核得到同样的 3,906 行、候选状态槽位、11–20 候选分组和联合分组计数。所有改变率均报告 Wilson 95% 区间。历史 token 仍是代理，不是账单实测。

### 17.2 方向 A：候选的 lifecycle 状态构成

3,906 次调用一共持久化 26,375 个候选槽位：

| lifecycle status | 候选槽位 | 槽位占比 | 平均每次调用 | 至少含一个该状态的调用 |
|---|---:|---:|---:|---:|
| `pending_entry` | 3,825 | 14.50% | 0.98 | 1,861（47.64%） |
| `entered` | 6,078 | 23.04% | 1.56 | 2,523（64.59%） |
| `holding` | **0** | 0% | 0 | 0 |
| `expired` | **16,472** | **62.45%** | **4.22** | **2,882（73.78%）** |
| **合计** | **26,375** | **100%** | **6.75** | — |

`expired` 是候选膨胀的主体。历史 cohort 没有任何 `holding` 候选，因此下面的 `entered + holding` 在本数据上实际等于只计 `entered`；不能据此推断未来 `holding` 流量的表现。

候选数 11–20 的调用共有 795 次，其中 **781 / 795 = 98.24%** 的第一层 `strategy.symbol` 为空，候选生成走了完全不做 symbol 过滤的路径。其余 14 次有 symbol。这里能证明的是“781 次处于无过滤路径”，不能反推出若有正确 symbol 时一定会降到 10 个以下。

三种替代计数的反事实如下。毛 token 只是被移出 multiple 组的负载；其中有其他触发器接管的部分仍会调用，所以实际节省只看净 token。

| multiple 候选口径 | 毛排除（毛 token） | 毛排除组改变率（Wilson 95%） | 其他触发器接管 | 净避免 | 净节省 token | 漏失实质改变 | 其中语义改变 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 只计 `entered + holding` | 2,527（101.964M） | 238/2,527 = 9.42%（8.34%–10.62%） | 474 | 2,053 | 83.720M（51.02%） | 64（净避免组 3.12%，2.45%–3.96%） | 63 |
| 计 `entered + holding + pending_entry`，仅排除 `expired` | 1,566（56.413M） | 170/1,566 = 10.86%（9.41%–12.49%） | 344 | 1,222 | 43.603M（26.57%） | 37（3.03%，2.20%–4.15%） | 36 |
| 只计 `pending_entry` | 2,960（110.597M） | 378/2,960 = 12.77%（11.62%–14.02%） | 733 | 2,227 | 83.630M（50.97%） | 98（4.40%，3.62%–5.33%） | 97 |

为与第 16.6 节可比，各口径都按 `0 / 1 / 2 / 3 / 4–5 / 6–10 / 11–20` 分桶，再对“是否实质改变”计算关联：

| 候选数口径 | Cramér's V | 归一化信息增益 | 排名 |
|---|---:|---:|---:|
| 当前全部候选 | 0.098 | 1.31% | 4 |
| 只计 `entered + holding` | 0.120 | 1.94% | 3 |
| 只计 `pending_entry` | 0.153 | 2.52% | 2 |
| **仅排除 `expired`** | **0.162** | **3.24%** | **1** |

因此 A 中区分度最高的是“保留 `entered + holding + pending_entry`、只排除 `expired`”。但它仍会净漏失 37 次实质改变，其中 36 次是语义改变；“区分度最高”不等于可直接上线。

### 17.3 方向 B：第一层 recognition 与 lifecycle event 联合分组

| 第一层 recognition / event_type | 调用 | 实质改变 | 改变率（Wilson 95%） | token（代理） |
|---|---:|---:|---:|---:|
| 是策略 / `entry_confirm` | 3 | 2 | 66.67%（20.77%–93.85%） | 0.107M |
| 是策略 / `none` | 213 | 26 | 12.21%（8.47%–17.28%） | 9.458M |
| 非策略 / `cancel_entry` | 21 | 10 | 47.62%（28.34%–67.63%） | 0.973M |
| 非策略 / `entry_confirm` | 68 | 44 | 64.71%（52.84%–75.00%） | 2.793M |
| 非策略 / `exit_position` | 422 | 202 | 47.87%（43.14%–52.63%） | 17.753M |
| **非策略 / `none`** | **2,765** | **34** | **1.23%（0.88%–1.71%）** | **115.890M** |
| 非策略 / `position_update` | 414 | 156 | 37.68%（33.15%–42.44%） | 17.110M |

联合信号的分层远强于单独的“是策略/非策略”。`非策略 + none` 占 70.79% 调用，却只有 1.23% 历史改变；其余 1,141 次调用有 440 次改变，改变率 38.56%（35.78%–41.42%）。

若 multiple 仅在“`recognition_result == 是策略` 或 `event_type != none`”时允许触发：

- 毛排除 2,765 次、115.890M token；其中 222 次仍由其他触发器调用；
- 净避免 **2,543 次（65.10%）**，净节省 **107.269M token（65.37%）**；
- 净漏失 **25 次实质改变（占全部 474 次的 5.27%）**，全部是语义改变；净避免组漏失率为 0.98%（Wilson 0.67%–1.45%）。

这说明 B 是很强的成本信号，但还不是安全门禁：25 个漏失样本并非随机噪声，包含加仓、平仓、移动止损、保护利润和“第一个打上了”等明确策略延续语义。

### 17.4 A 最优口径与 B 叠加

联合规则为：multiple 只有在“排除 `expired` 后仍有至少两个候选”且“第一层是策略或 event 不为 none”时成立。

- 毛排除 3,221 次、133.460M token；489 次仍由其他触发器接管；
- 净避免 **2,732 次（69.94%）**，净节省 **115.068M token（70.13%）**；
- 净漏失 **53 次实质改变（占全部改变 11.18%）**：52 次语义改变、1 次目标改变；净避免组漏失率 1.94%（Wilson 1.49%–2.53%）；
- 被联合规则保留的 685 次调用有 280 次改变，改变率 40.88%（37.26%–44.60%）。

与 B 单独使用相比，联合规则只多节省约 4.76 个百分点调用和 4.75 个百分点 token，却把漏失从 25 次增加到 53 次。因此历史数据不支持把 A 最优口径直接叠加到 B 作为上线规则。

### 17.5 B 与联合规则的全部漏失样本

下表合并列出联合规则漏失的 53 个 attempt：标记“B + 联合”的 25 行也是 B 单独规则的全部漏失；“仅联合”的 28 行只由 A 的 expired 排除新增。53 个 attempt 对应 47 条不同 raw message，重复行是同一 raw message 的独立历史 attempt，不应去重后当作调用数。

| attempt / raw | 规则漏失 | 原始消息文本 | 候选构成 | 命中触发器 | 第一层 → 上下文后 | 改变类型 |
|---|---|---|---|---|---|---|
| 109 / 8471 | 仅联合 | 大镖客·Andy<br>在63900进的保护利润<br>@Tarderfengge QQ:158241758 | 2（entered=1, expired=1） | multiple only | 非策略/entry_confirm/life=660 → manage_thread/threads=[30]/move_stop_to_protect | 语义 |
| 142 / 8586 | 仅联合 | 大镖客·Andy<br>您64600了，65200进场的保护利润<br>@Tarderfengge QQ:158241758 | 2（entered=1, expired=1） | multiple only | 非策略/position_update/life=669 → unresolved/threads=[] | 语义 |
| 143 / 8586 | 仅联合 | 大镖客·Andy<br>您64600了，65200进场的保护利润<br>@Tarderfengge QQ:158241758 | 2（entered=1, expired=1） | multiple only | 非策略/position_update/life=669 → unresolved/threads=[] | 语义 |
| 236 / 8850 | B + 联合 | [空文本] | 3（entered=1, expired=2） | multiple only | 非策略/none → cancel_thread/threads=[37]/cancel_pending_entry | 语义 |
| 326 / 9079 | B + 联合 | 大镖客·Andy<br>今天的多单，轻仓，探个路<br>@Tarderfengge QQ:158241758 | 4（entered=3, expired=1） | multiple only | 非策略/none → revise_thread/threads=[76] | 语义 |
| 390 / 9171 | B + 联合 | 大镖客·Andy<br>第一止盈位已到，注意锁定利润，及时移动止损！<br>@Tarderfengge QQ:158241758 | 4（entered=3, expired=1） | multiple only | 非策略/none → manage_thread/threads=[76]/move_stop_to_protect | 语义 |
| 497 / 9409 | B + 联合 | XAG 咱们适当开个头仓吧，看样子都不想回调呢<br>@Tarderfengge QQ:158241758 | 12（pending_entry=4, entered=1, expired=7） | multiple only | 非策略/none → revise_thread/threads=[100] | 语义 |
| 502 / 9420 | B + 联合 | ❤️昨天多单浮盈10个点！<br>❤️多单直接出局，掉头布局空单！<br>@Tarderfengge QQ:158241758 | 4（pending_entry=1, entered=3） | multiple only | 非策略/none → exit_thread/threads=[86]/exit_full | 语义 |
| 546 / 9471 | 仅联合 | 比特币行情长话短说，在6.2和6.3万支撑附近做多，分2次入场，6.4止盈，6.1止损。<br>其实我更建议买现货，熊市还有一两个月就结束了，记得逢低买入。<br>做空的话到6.48万阻力区空，空到6.4止盈，小幅突破6.53就止损，在下一个阻力区6.67万再空即可。<br>以太坊做空则是在1920阻力下方做空，之前单子都有发，这些位置来了就干，80%胜率，打不了就止损然后在下一支撑、阻力位重新进场即可。<br>比特币近期一直在盘整，没什么大行情，还是要更多一些耐心。<br>@Tarderfengge QQ:158241758 | 2（pending_entry=1, expired=1） | multiple only | 是策略/none → unresolved/threads=[] | 语义 |
| 743 / 9901 | 仅联合 | [空文本] | 3（expired=3） | multiple only | 非策略/exit_position/life=743 → unresolved/threads=[] | 语义 |
| 755 / 9918 | 仅联合 | KGEN多单止盈<br>@Tarderfengge QQ:158241758 | 4（expired=4） | multiple only | 非策略/exit_position/life=759 → unresolved/threads=[] | 语义 |
| 790 / 9998 | 仅联合 | [空文本] | 4（pending_entry=1, expired=3） | multiple only | 非策略/entry_confirm/life=766 → manage_thread/threads=[136]/hold_update | 语义 |
| 833 / 10078 | 仅联合 | 比特币 早上打了流动性出现了回调 还没有收盘，<br>前面给的63100-62800的多 委托这<br>在挂一个反弹 63600附近 反弹<br>止损：小时级别有效跌破63350<br>止盈：64050-64350-64500<br>@Tarderfengge QQ:158241758 | 3（expired=3） | multiple only | 是策略/none → revise_thread/threads=[125]/replace_entry | 语义 |
| 1015 / 10379 | B + 联合 | 分批追加50%多单，止损不变。<br>@Tarderfengge QQ:158241758 | 3（entered=2, expired=1） | multiple only | 非策略/none → manage_thread/threads=[151]/risk_update | 语义 |
| 1078 / 10486 | 仅联合 | 比特币 昨天开的空 看看能不能 63950附近 再补进来一次空<br>止损：15分钟有效站稳64150 止损比较小了<br>止盈：63600-63350-63050-62650<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 是策略/none → manage_thread/threads=[153]/risk_update | 语义 |
| 1133 / 10616 | B + 联合 | 上次闪迪做多的1358点位到了，继续走30%仓位无脑，不要墨迹。#SNDK<br>@Tarderfengge QQ:158241758 | 15（entered=8, expired=7） | multiple only | 非策略/none → manage_thread/threads=[107]/partial_take_profit | 语义 |
| 1179 / 10703 | 仅联合 | 比特币 所长前面给的这个空单 还有一个点事 64650附近 一直没来 我在这复盘开的话，这里是可以打流动性的位置了，再委托上这里空 能不能来不知道<br>止损：小时级别有效站稳65000<br>止盈：64000-63800-63150-62650<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 是策略/none → revise_thread/threads=[153] | 语义 |
| 1189 / 10727 | 仅联合 | 比特币 昨天空的位置 今天看到了么出现了反馈<br>一天坐不上个单子是挺急的 在这开空<br>止损：就用昨天咱们的空点 63950 小时级别站稳<br>止盈：63550-63200-62850-62650<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 是策略/none → revise_thread/threads=[153]/replace_entry | 语义 |
| 1191 / 10729 | B + 联合 | 第一个打上了<br>@Tarderfengge QQ:158241758 | 20（pending_entry=3, expired=16, entered=1） | multiple only | 非策略/none → manage_thread/threads=[153]/hold_update | 语义 |
| 1194 / 10736 | B + 联合 | 🔥现目前行情跌破进场价！🔥<br>🔥注意设置好止损点位！🔥<br>🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥<br>@Tarderfengge QQ:158241758 | 4（entered=4） | multiple only | 非策略/none → manage_thread/threads=[187]/risk_update | 语义 |
| 1224 / 10779 | 仅联合 | [空文本] | 3（expired=3） | multiple only | 非策略/exit_position/life=510 → unresolved/threads=[] | 语义 |
| 1225 / 10779 | 仅联合 | [空文本] | 3（expired=3） | multiple only | 非策略/exit_position/life=510 → unresolved/threads=[] | 语义 |
| 1265 / 10779 | 仅联合 | [空文本] | 4（pending_entry=1, expired=3） | multiple only | 非策略/exit_position/life=510 → unresolved/threads=[] | 语义 |
| 1266 / 10779 | 仅联合 | [空文本] | 4（entered=1, expired=3） | multiple only | 非策略/exit_position/life=510 → exit_thread/threads=[204]/exit_full | **目标** |
| 1272 / 10841 | B + 联合 | 629附近都可以加仓btc空，我打算加仓到2000个，分批入场<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 非策略/none → manage_thread/threads=[165]/risk_update | 语义 |
| 1277 / 10845 | B + 联合 | 算了先加个100个吧<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 非策略/none → manage_thread/threads=[165] | 语义 |
| 1308 / 10901 | B + 联合 | BTC多单正常持有，剩余半仓挂单也正常挂单，目前行情还是在小级别震荡，如果晚间下探62300附近有机会接一根针，上方关注63800-64100阻力。<br>@Tarderfengge QQ:158241758 | 2（entered=2） | multiple only | 非策略/none → manage_thread/threads=[206]/hold_update | 语义 |
| 1332 / 10928 | B + 联合 | [空文本] | 4（pending_entry=1, expired=3） | multiple only | 非策略/none → manage_thread/threads=[207]/hold_update | 语义 |
| 1396 / 11038 | B + 联合 | [空文本] | 4（pending_entry=1, expired=3） | multiple only | 非策略/none → manage_thread/threads=[207]/hold_update | 语义 |
| 1473 / 11163 | B + 联合 | 昨天开了空单，如果还持有的同学，平仓出来<br>@Tarderfengge QQ:158241758 | 20（expired=19, entered=1） | multiple only | 非策略/none → exit_thread/threads=[219]/exit_full | 语义 |
| 1519 / 11229 | B + 联合 | 63600 加仓btc空，止损改64100，成本价63700<br>@Tarderfengge QQ:158241758 | 2（expired=2） | multiple only | 非策略/none → manage_thread/threads=[165]/risk_update | 语义 |
| 1577 / 11319 | B + 联合 | 这是一笔黄金中长线策略，不要重仓，止损大就少开点！<br>@Tarderfengge QQ:158241758 | 4（pending_entry=1, entered=2, expired=1） | multiple only | 非策略/none → manage_thread/threads=[238]/risk_update | 语义 |
| 1592 / 11338 | 仅联合 | 止盈点来了 💵💵💵 视频也提醒各位了 即便你没开空 希望你没在这里追多<br>@Tarderfengge QQ:158241758 | 20（entered=1, expired=19） | multiple only | 非策略/position_update/life=866 → hold/threads=[] | 语义 |
| 1603 / 11351 | 仅联合 | [空文本] | 4（expired=4） | multiple only | 非策略/exit_position/life=849 → unresolved/threads=[] | 语义 |
| 1652 / 11409 | 仅联合 | 突然拉上去了，小时级别收到 64450上面不要扛单出来，就在64450附近止损出来。四小时如果突破M头那可能还会涨，别扛单！<br>@Tarderfengge QQ:158241758 | 20（pending_entry=1, expired=19） | multiple only | 非策略/exit_position/life=866 → unresolved/threads=[] | 语义 |
| 1653 / 11409 | 仅联合 | 突然拉上去了，小时级别收到 64450上面不要扛单出来，就在64450附近止损出来。四小时如果突破M头那可能还会涨，别扛单！<br>@Tarderfengge QQ:158241758 | 20（pending_entry=1, expired=19） | multiple only | 非策略/exit_position/life=866 → unresolved/threads=[] | 语义 |
| 1682 / 11444 | B + 联合 | BTC65000未突破，目前浮亏500点左右，正常持有中，继续关注64000附近，小级别计划暂不做改变。<br>@Tarderfengge QQ:158241758 | 2（entered=2） | multiple only | 非策略/none → manage_thread/threads=[239]/hold_update | 语义 |
| 1684 / 11448 | B + 联合 | 这单风险大一些，可以小仓位尝试，以小损追求较大的反弹机会。求稳就在下一强支撑1510附近做多。<br>@Tarderfengge QQ:158241758 | 18（pending_entry=4, entered=3, expired=11） | multiple only | 非策略/none → manage_thread/threads=[250]/risk_update | 语义 |
| 1795 / 11589 | 仅联合 | 【三马哥现货Vip】<br>SNDKB/USDT 现货小波段第5批（不带杠杆不带合约，不然插针容易归零）<br>继续布局：<br>1）挂1560附近买入现货仓位的30%<br>2）挂1358附近买入现货仓位的50%<br>止盈目标：1650卖一半、1988清仓。<br>止損：成交后的周线收盘价低于1150美元，周收盘价低于1000直接割肉不犹豫<br>入选理由：美股存储三剑客，风头正劲但需要更严格但入场点才能有利可图。<br>策略供参考交流，控制好仓位，不作为做单依据，如有变更，另行通知。#SNDKB<br>@Tarderfengge QQ:158241758 | 2（pending_entry=1, expired=1） | multiple only | 是策略/none → manage_thread/threads=[261]/hold_update | 语义 |
| 1927 / 11818 | B + 联合 | BTC短线空单，正常仓位操作。<br>@Tarderfengge QQ:158241758 | 2（entered=2） | multiple only | 非策略/none → manage_thread/threads=[277]/risk_update | 语义 |
| 1929 / 11819 | B + 联合 | 市价附近直接入场<br>@Tarderfengge QQ:158241758 | 2（entered=2） | multiple only | 非策略/none → revise_thread/threads=[277]/replace_entry | 语义 |
| 2055 / 12015 | B + 联合 | 名称错了，看价格就知道是eth，抱歉啊！<br>@Tarderfengge QQ:158241758 | 4（pending_entry=1, expired=3） | multiple only | 非策略/none → revise_thread/threads=[284] | 语义 |
| 2080 / 12042 | B + 联合 | 限价空单不要取整不容易触发上下几十点浮动，两个点位各挂半仓，当前点位不建议追高容易空单也要带好止损，一对一指导以及之前私聊陈哥领取免费山寨币中长线多单建议可以止盈60%剩余可以私来我给向上移动止损他。<br>@Tarderfengge QQ:158241758 | 2（pending_entry=1, entered=1） | multiple only | 非策略/none → revise_thread/threads=[289]/replace_entry | 语义 |
| 2116 / 12084 | 仅联合 | [空文本] | 6（pending_entry=1, expired=5） | multiple only | 非策略/entry_confirm/life=924 → manage_thread/threads=[293] | 语义 |
| 2128 / 12096 | 仅联合 | [空文本] | 6（pending_entry=1, expired=5） | multiple only | 非策略/entry_confirm/life=924 → manage_thread/threads=[293] | 语义 |
| 3629 / 12893 | 仅联合 | [空文本] | 6（entered=1, expired=5） | multiple only | 非策略/exit_position/life=963 → unresolved/threads=[] | 语义 |
| 3633 / 12895 | 仅联合 | [空文本] | 5（expired=5） | multiple only | 非策略/exit_position/life=963 → unresolved/threads=[] | 语义 |
| 3661 / 12941 | 仅联合 | 大镖客·Andy<br>第三止盈位已到，注意锁定利润，及时移动止损！<br>@Tarderfengge QQ:158241758 | 2（entered=1, expired=1） | multiple only | 非策略/position_update/life=957 → unresolved/threads=[] | 语义 |
| 3694 / 13000 | 仅联合 | [空文本] | 5（expired=5） | multiple only | 非策略/cancel_entry/life=510 → unresolved/threads=[] | 语义 |
| 4122 / 13693 | B + 联合 | [空文本] | 3（pending_entry=1, expired=2） | multiple only | 非策略/none → manage_thread/threads=[393]/hold_update | 语义 |
| 4146 / 13722 | B + 联合 | 走势短线不太对，BTC的77188也挂上，如果成交成本会变成77888，然后毫不犹豫反弹到77888全部跑掉。往后支撑太远了75000和71188不值得扛到这么久。狗庄只给一次机会逃命，把握住。#BTC<br>@Tarderfengge QQ:158241758 | 20（entered=4, pending_entry=6, expired=10） | multiple only | 非策略/none → exit_thread/threads=[394]/exit_full | 语义 |
| 4154 / 13730 | 仅联合 | [空文本] | 5（expired=5） | multiple only | 非策略/exit_position/life=1003 → unresolved/threads=[] | 语义 |
| 4159 / 13730 | 仅联合 | [空文本] | 5（expired=5） | multiple only | 非策略/exit_position/life=1003 → unresolved/threads=[] | 语义 |

### 17.6 结论与上线证据门槛

历史样本充分支持以下描述性结论：`expired` 是候选数量的主要来源；高候选数几乎都发生在 symbol unknown 的无过滤路径；只排除 `expired` 是 A 中区分度最高的计数；B 的联合第一层信号远强于候选数量本身。

但两个方向当前都不能直接上线：

1. A 最优口径仍漏 37 次改变；联合后新增的 28 次漏失里包含正确撤销、退出、管理，也包含上下文否决第一层错误管理/退出的风险收缩结果。
2. B 虽节省 65.37% token，仍漏 25 条明确的策略延续语义；这证明第一层 `非策略 + none` 不是可靠的“无上下文需要”标签。
3. `holding` 历史样本为零，未来状态分布变化会使 A 的历史反事实外推失真。
4. 空文本行可能依赖图片证据；仅凭表中的空文本不能人工判定“不需要上下文”。

因此人工标注应优先覆盖表中 47 条不同 raw message，并把“上下文是否纠正了第一层漏识别”“是否仅改变目标”“是否风险收缩”分开标记。任何未来 gate 都必须先做全历史回放并对这 53 个 attempt 100% 召回，再以旧完整解析为权威做 shadow replay；在此之前，安全可实施节省仍为 0%。本节没有评估群组白名单或交易标的白名单，也没有修改任何代码、判据、prompt、设置、数据、release 或服务。

## 18. 方向 B 加动作词兜底：只读反事实与帕累托前沿（2026-09-01）

### 18.1 固定口径

本节继续使用 `id <= 4245` 的 3,906 次 multiple cohort、474 次实质改变和 164.085M 代理 token。规则 B 原本毛排除 `非策略 + event_type=none` 的 2,765 次调用；扣除其他触发器接管后净避免 2,543 次、节省 107.269M token（65.37%），漏失 25 次语义改变。

动作词只匹配调用前已有的消息文本；第一层 `input_reading.observed_text`、回复链、图片类型等单列为其他调用前信号。固定转发尾注 `@Tarderfengge QQ:158241758` 在数字、长度和英文 token 分析前移除，避免把固定 QQ 号误当价格、把 `QQ` 误当标的。所有 token 继续使用第 15/16/17 节的固定代理率。

### 18.2 规则 C：B 加当前动作词

第 16 节自建、非生产判据的完整动作词正则是：

```text
进场 | 直接进 | 开仓 | 做多 | 做空 | 买入 | 卖出 | 挂单 | 加仓 | 减仓 |
止盈 | 止损 | 平仓 | 出局 | 撤单 | 取消 | 保本 | 保护价 |
设(?:置)?保护 | 推保护 | 继续持有
```

规则 C 为：“是策略，或 `event_type != none`，或当前消息命中上述动作词，才允许 multiple 触发。”结果为：

- 毛排除 2,114 次、88.698M token；83 次仍由其他触发器接管；
- 净避免 **2,031 次**，净节省 **85.216M token（51.93%）**；
- 从 B 漏掉的 25 次中捞回 **12 次，召回 48.00%（Wilson 30.03%–66.50%）**；
- 仍漏 **13 次语义改变**，净避免组改变率 0.64%（Wilson 0.37%–1.09%）；全部 474 次实质改变的历史召回为 461/474 = 97.26%。

相对 B，动作词兜底用 **13.44 个百分点 token 节省**换回 12/25 次漏失。它有价值，但仍未达到 100% 召回。

### 18.3 规则 C 仍漏掉的 13 次实质改变

这 13 行都只有 `multiple_same_source_candidates` 一个触发器，第一层均为 `非策略 / none`：

| attempt / raw | 原始消息文本 | 命中触发器 | 第一层 → 上下文后 |
|---|---|---|---|
| 236 / 8850 | [空文本；图片] | multiple only | 非策略/none → cancel_thread/threads=[37]/cancel_pending_entry |
| 326 / 9079 | 大镖客·Andy<br>今天的多单，轻仓，探个路<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → revise_thread/threads=[76] |
| 497 / 9409 | XAG 咱们适当开个头仓吧，看样子都不想回调呢<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → revise_thread/threads=[100] |
| 1191 / 10729 | 第一个打上了<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → manage_thread/threads=[153]/hold_update |
| 1277 / 10845 | 算了先加个100个吧<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → manage_thread/threads=[165] |
| 1332 / 10928 | [空文本；图片] | multiple only | 非策略/none → manage_thread/threads=[207]/hold_update |
| 1396 / 11038 | [空文本；图片] | multiple only | 非策略/none → manage_thread/threads=[207]/hold_update |
| 1682 / 11444 | BTC65000未突破，目前浮亏500点左右，正常持有中，继续关注64000附近，小级别计划暂不做改变。<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → manage_thread/threads=[239]/hold_update |
| 1927 / 11818 | BTC短线空单，正常仓位操作。<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → manage_thread/threads=[277]/risk_update |
| 1929 / 11819 | 市价附近直接入场<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → revise_thread/threads=[277]/replace_entry |
| 2055 / 12015 | 名称错了，看价格就知道是eth，抱歉啊！<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → revise_thread/threads=[284] |
| 4122 / 13693 | [空文本；图片] | multiple only | 非策略/none → manage_thread/threads=[393]/hold_update |
| 4146 / 13722 | 走势短线不太对，BTC的77188也挂上，如果成交成本会变成77888，然后毫不犹豫反弹到77888全部跑掉。往后支撑太远了75000和71188不值得扛到这么久。狗庄只给一次机会逃命，把握住。#BTC<br>@Tarderfengge QQ:158241758 | multiple only | 非策略/none → exit_thread/threads=[394]/exit_full |

### 18.4 动作词敏感性

“收窄”只保留核心仓位管理词：

```text
加仓 | 减仓 | 止盈 | 止损 | 平仓 | 出局 | 撤单 | 取消 | 保本 |
保护价 | 设(?:置)?保护 | 推保护 | 继续持有
```

“放宽动作词”在当前词表上补入 B 漏失文本中未命中的八类动作表达：

```text
多单 | 空单 | 头仓 | 打上 | 加个 | 持有 | 入场 | 跑掉
```

`名称错 | 看价格` 是指代/标的纠正信号，不是动作词，因此单独列出，不伪装成仓位管理动词。

| 词表口径（只检查原始正文） | 毛排除 | 其他触发器接管 | 净避免 | 节省 token | 捞回 B 的 25 次 | 仍漏 |
|---|---:|---:|---:|---:|---:|---:|
| 收窄核心词 | 2,314 | 118 | 2,196 | 93.199M（56.80%） | 9/25 = 36%（20.25%–55.48%） | 16 |
| 当前完整词表（规则 C） | 2,114 | 83 | 2,031 | 85.216M（51.93%） | 12/25 = 48%（30.03%–66.50%） | 13 |
| 放宽八类动作表达 | 1,955 | 76 | 1,879 | 78.160M（47.63%） | 20/25 = 80%（60.87%–91.14%） | 5 |
| 再加 `名称错|看价格` | 1,954 | 76 | 1,878 | 78.144M（47.62%） | 21/25 = 84%（65.35%–93.60%） | 4 |

纯文本词表不可能对 25 次达到 100% 召回：attempt 236、1332、1396、4122 的原始正文为空。即使把动作词无限放宽，也没有字符可以命中。以短语级、不过拟合到单字的口径，21 个非空漏失至少需要当前词表加上述八类动作表达，以及 `名称错|看价格` 这一类纠正表达；剩余四个必须依赖非文本信号。

### 18.5 第一层 observed text 与其他调用前信号

第一层 `input_reading.observed_text` 是上下文解析前已经存在的证据，且比“只要有媒体就兜底”精确。四个空正文漏失的 observed text 分别是：

- 236：`ZEC490空单取消吧`；当前词表可命中 `取消`；
- 1332：`触发入场价`；放宽词表可命中 `入场`；
- 1396：`BTC空单……做空。止盈止损不变`；当前词表可命中；
- 4122：仍为空，只剩 `market_chart` 图片类型。

将动作词同时应用于原始正文和 observed text：

| 规则 | 毛排除 | 其他触发器接管 | 净避免 | 节省 token | 捞回 B 漏失 | 仍漏 |
|---|---:|---:|---:|---:|---:|---:|
| 当前词表，正文 + observed text | 1,972 | 79 | 1,893 | 79.148M（48.24%） | 14/25 = 56%（37.07%–73.33%） | 11 |
| 放宽动作词，正文 + observed text | 1,804 | 74 | 1,730 | 71.936M（43.84%） | 23/25 = 92%（75.03%–97.78%） | 2（2055、4122） |
| 再加纠正表达 | 1,803 | 74 | 1,729 | 71.920M（43.83%） | 24/25 = 96%（80.46%–99.29%） | 1（4122） |
| 再保留“正文与 observed text 均空、图片类型为 `market_chart`” | 1,539 | 74 | 1,465 | 60.234M（**36.71%**） | **25/25 = 100%（86.68%–100%）** | **0** |

最后一行是本轮唯一达到历史 100% 召回且仍有显著节省的组合。若再加 `candidate_count == 3` 可把历史节省提高到 42.71%，但它只是在一条样本上拟合数字，候选数此前已被证明区分度弱，因此不作为推荐规则。

其他单一调用前信号的效果如下。“信号召回”只看它自身命中 25 次中的多少；“组合召回/节省”表示把它与当前规则 C 做 OR：

| 其他信号 | 信号自身召回 | 与当前 C 组合后的召回 | 组合后 token 节省 | 判断 |
|---|---:|---:|---:|---|
| 有回复 / reply chain | 2/25 = 8% | 13/25 = 52% | 50.22% | 增益很小 |
| 正文含至少两位连续数字 | 11/25 = 44% | 15/25 = 60% | 37.49% | 价格与普通数量混杂，成本高 |
| 正文非空 | 21/25 = 84% | 21/25 = 84% | 22.31% | 过宽，几乎失去筛选价值 |
| 正文长度 ≥10 | 19/25 = 76% | 19/25 = 76% | 27.49% | 过宽 |
| 正文长度 ≥20 | 16/25 = 64% | 17/25 = 68% | 31.33% | 仍被词表方案支配 |
| 正文长度 ≥40 | 5/25 = 20% | 14/25 = 56% | 37.05% | 长度方向不稳定 |
| 正文含大写英文 token | 6/25 = 24% | 16/25 = 64% | 44.04% | 不用标的白名单，但仍有格式误报 |
| 正文含 hashtag | 2/25 = 8% | 13/25 = 52% | 51.38% | 增益很小 |
| 正文含 emoji | 2/25 = 8% | 12/25 = 48% | 46.81% | 没捞回当前 C 的任何新增漏失，被支配 |
| 有任意媒体 | 7/25 = 28% | 18/25 = 72% | 18.92% | 太宽 |
| 正文为空且有媒体 | 4/25 = 16% | 16/25 = 64% | 29.63% | 比任意媒体好，但仍不如 observed text + image type |

数字和英文 token 均在移除固定转发尾注后计算；否则几乎每条消息都会因 QQ 号和 `QQ` 被误判命中。

### 18.6 历史帕累托前沿

横轴为相对 164.085M token 的节省，纵轴为对 B 漏掉 25 次的召回。下列规则不存在另一个已测规则能同时提供更高召回和更高节省：

| 历史候选规则 | B 漏失召回 | token 节省 | 前沿状态 |
|---|---:|---:|---|
| B，无兜底 | 0/25 = 0%（Wilson 上界 13.32%） | **65.37%** | 前沿：最大节省 |
| B + 收窄核心词 | 9/25 = 36% | 56.80% | 前沿 |
| B + 当前完整词表（规则 C） | 12/25 = 48% | 51.93% | 前沿 |
| 当前词表 + `名称错|看价格` | 13/25 = 52% | 51.92% | 前沿，但明显是历史定向补词 |
| 当前词表 + `打上/加个/持有/名称错或看价格` | 16/25 = 64% | 51.68% | 前沿，历史拟合 |
| 当前词表 + `空单/头仓/打上/加个/持有/入场/跑掉` 及纠正表达 | 20/25 = 80% | 49.76% | 前沿，历史拟合 |
| 当前词表 + 全部八类动作补词及纠正表达 | 21/25 = 84% | 47.62% | 前沿，历史拟合 |
| 放宽动作词，同时检查 observed text | 23/25 = 92% | 43.84% | 前沿 |
| 再加纠正表达 | 24/25 = 96% | 43.83% | 前沿 |
| 再兜底双空 `market_chart` | **25/25 = 100%** | **36.71%** | 前沿：最高历史召回 |

回复、数字、长度、hashtag、emoji、任意媒体等单信号方案均被上表某个词表/observed-text 方案支配：在相同或更高召回下，它们保留的 token 更多。候选数等于 3 的特例虽然在历史数值上更靠前，但属于单样本过拟合，不纳入可推荐前沿。

### 18.7 推荐与证据门槛

推荐进入下一步人工标注和 shadow replay 的候选，不是当前规则 C，而是：

1. 规则 B；
2. 当前动作词加八类扩展动作表达和 `名称错|看价格`；
3. 同时匹配原始正文与第一层 `input_reading.observed_text`；
4. 对两者均为空且图片类型为 `market_chart` 的消息继续解析。

它在本历史 cohort 上召回 25/25 个 B 漏失、全部 474 次实质改变零漏失，理论节省 36.71% token。这个 100% 是对已知 25 条样本的回放结果，Wilson 下界只有 86.68%，而补词和 `market_chart` 条件明显受这批漏失样本影响，不能称为可直接上线。

上线前仍需：对 25 条漏失和同类未改变样本做人工标注；固定词表后重新进行未参与选词的留出回放；生产 shadow 中旧完整解析继续权威，并要求动作族、target thread、适用性和风险收缩结果 100% 一致。任何实质改变召回低于 100% 即保留现状。本节没有评估群组或交易标的白名单，也没有修改任何代码、词表、判据、prompt、设置、数据、release 或服务。
