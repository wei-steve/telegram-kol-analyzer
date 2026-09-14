# AI 提供商 / AI模型选择 两页浏览器验证（阶段 4）

日期：2026-09-13。设计：`docs/plans/2026-09-13-ai-provider-model-routing-design.md` §5。
进度与决策：`docs/ai-provider-model-routing-status.md`。

## 怎么复现

```bash
cp config/ai_recognition.example.yaml data/ai-routing-preview.yaml
cp config/groups.example.yaml       data/ai-routing-preview-groups.yaml
uv run telegram-kol-research web --runtime-role web --host 127.0.0.1 --port 8099 \
  --database-path data/ai-routing-preview.db \
  --config-path data/ai-routing-preview-groups.yaml \
  --ai-recognition-config-path data/ai-routing-preview.yaml
```

`.claude/launch.json` 里的 `ai-routing-preview` 就是这条命令。
`data/` 已 gitignore，整个验证不碰真实的 `config/ai_recognition.yaml`。
起始文件是 **v1 形态**（example.yaml），所以这一遍同时验了迁移。

## 关于截图

**这一轮没有 PNG 截图。** 本会话的浏览器工具只能把截图返回到会话里，不能写文件，
所以这里留下的是同等可核对的东西：两页渲染后的实际结构、两个 API 的原样响应、
以及保存后磁盘上 YAML 的内容。每一条都能用上面的命令重新跑出来。
下一轮如果需要 PNG，请在能落盘截图的环境里按这份清单再走一遍。

## 实际操作过的每一步

| # | 操作 | 结果 |
|---|---|---|
| 1 | ⚙ 设置菜单 | 六项：AI识别提示词 / **AI提供商** / **AI模型选择** / 识别 Profile / 交易设置 / 交易持仓。改名生效 |
| 2 | 「更多工具」底部快捷入口 | 出现 **AI 提供商** 与 **AI 模型选择** 两个按钮 |
| 3 | 打开「AI提供商」 | 三张卡片 deepseek / zhipu / mimo，各自带 Base URL、超时、启用开关，Key 显示为 `已配置 ****_key（留空保持不变）` |
| 4 | 点「自定义（OpenAI 兼容）」 | 追加一张空白提供商卡片 |
| 5 | 填 `unreachable` / `http://127.0.0.1:9/v1` / `sk-test`，加模型 `unreachable-vision`（文本+图片） | — |
| 6 | 点「保存提供商与模型」 | 状态变为「提供商与模型已保存」；重绘后新卡片的 Key 显示 `已配置 ****test（留空保持不变）` |
| 7 | 点该卡片的「测试连接」 | 红色 **`不可用（provider_unavailable）`** —— 不可达地址返回明确失败 |
| 8 | 打开「AI模型选择」 | 7 行，每行：中文名、能力标签、生产路径标签、一句话说明、有序链、添加备用、当前生效 |
| 9 | 在 `authoritative_recognition` 的「添加备用」里选 `unreachable-vision`，点「添加」 | 出现「备用 1  不可达多模态 / unreachable-vision」，不是灰的 |
| 10 | 点「保存环节模型」 | 状态变为「环节模型已保存」 |
| 11 | `GET /api/ai-stages` 回读 | `authoritative_recognition: ["mimo-v2.5", "unreachable-vision"]`，role 为 主用 / 备用 1 |
| 12 | 读磁盘 `data/ai-routing-preview.yaml` | `schema_version: 2`，stages 与页面一致，**mimo 的 Key 原样保留**（第 6 步留空 = 不变） |

## 页面渲染后的实际结构（第 11 步时）

### AI提供商

| 卡片 | Base URL | Key 输入框的 value | Key 占位符 | 模型 |
|---|---|---|---|---|
| deepseek | https://api.deepseek.com | `""` | 已配置 ****_key（留空保持不变） | deepseek-v4-flash |
| zhipu | https://open.bigmodel.cn/api/paas/v4 | `""` | 已配置 ****_key（留空保持不变） | glm-ocr |
| mimo | https://api.xiaomimimo.com/v1 | `""` | 已配置 ****_key（留空保持不变） | mimo-v2.5 |
| unreachable | http://127.0.0.1:9/v1 | `""` | 已配置 ****test（留空保持不变） | unreachable-vision |

**Key 的 value 全部是空字符串**：Key 从来不进 DOM，页面上只有末 4 位。

### AI模型选择

| stage_key | 中文名 | 标签 | 链 | 当前生效 |
|---|---|---|---|---|
| authoritative_recognition | 单条消息权威识别（MiMo 多模态，v1 / v2 合同共用） | 文本 + 图片 / 生产路径：是，主路径 | 主用 mimo-v2.5、备用 1 unreachable-vision | MiMo V2.5 |
| context_resolution | 上下文结合分析（第二层） | 文本 / 生产路径：是（权威识别判定需要时调用；另有重分析队列） | 主用 deepseek-v4-flash | DeepSeek V4 Flash |
| semantic_review | 语义分歧复核（只读顾问） | 文本 / 生产路径：是（worker semantic_review 单例循环） | 主用 deepseek-v4-flash | DeepSeek V4 Flash |
| strategy_alert | 策略提醒分类（Telegram 提醒 bot） | 文本 / 生产路径：是，当 bot token 配置时 | （空） | 未绑定，沿用环境变量 TELEGRAM_KOL_ALERT_LLM_MODEL / TELEGRAM_KOL_LLM_* |
| research_chat | Web 群消息问答 | 文本 / 非生产路径 | （空） | 未绑定，沿用环境变量 TELEGRAM_KOL_LLM_* |
| batch_text_recognition | 离线/批量文本识别（V1 recognize_message_now，含生命周期事件 AI） | 文本 / 非生产路径 | 主用 deepseek-v4-flash | DeepSeek V4 Flash |
| batch_image_recognition | 离线/批量图片识别（V1；GLM-OCR 走 layout_parsing，其他走多模态 chat） | 图片 / 非生产路径 | 主用 glm-ocr | GLM-OCR |

页首写着：「运行时事故代理（runtime_incident_agent）使用独立配置，不在此处设置。」

## 两个 API 的原样响应

- `api-ai-providers.json` —— `GET /api/ai-providers`（Key 只有 `api_key_configured` 与末 4 位）
- `api-ai-stages.json` —— `GET /api/ai-stages`（definitions + stages + effective + routable_model_ids）

## 走这一遍抓到的三个问题（都已修）

1. 「主用 / 备用 n」标签渲染不出来：chip 里的 span 只设了 class，没设
   `data-ai-stage-role`，`redraw()` 找不到它。
2. 两页只在首次绑定时读一次数据：在提供商页加完模型再切到模型选择页，下拉里看不到新模型。
   两页改为在 tab 被点开时重新读。
3. 刚添加、还没保存的备用被画成灰色「已绑定，未参与路由」：原来的「能不能路由」是拿
   每个环节的 `effective` 列表判断的，而那里只有**已经绑定**的成员。改为由
   `/api/ai-stages` 另给一个 `routable_model_ids`（启用 + provider 启用且有 base_url）。

---

# 阶段 6 追加验证（2026-09-14）

同一条启动命令（`.claude/launch.json` 的 `ai-routing-preview`，或直接跑
`uv run telegram-kol-research web ... --ai-recognition-config-path data/ai-routing-preview.yaml`）。
起始文件同样是 v1 的 example 快照。同样没有 PNG：浏览器工具仍然只能把截图返回到会话里。

| # | 操作 | 结果 |
|---|---|---|
| 1 | 打开「AI提供商」 | 预设按钮分四组渲染：**国内 9**（DeepSeek / 智谱 GLM / 小米 MiMo / 阿里百炼（通义 Qwen）/ 月之暗面 Kimi / 硅基流动 / 阶跃星辰 / MiniMax / 火山方舟（豆包））、**国际 7**（OpenAI / Anthropic / Google Gemini / xAI Grok / OpenRouter / Groq / Mistral）、**本地 2**（Ollama（本机）/ LM Studio（本机））、**自定义 1** |
| 2 | 说明行 | 「添加提供商：预设来自 models.dev（2026-09-14），只是起步，模型名以「拉取模型列表」为准。」 |
| 3 | 每张卡片的按钮 | 测试连接 / **拉取模型列表** / 添加模型 / **补充预设模型** |
| 4 | 点「阿里百炼（通义 Qwen）」 | 新卡片：id `alibaba-cn`、名称「阿里百炼（通义 Qwen）」、Base URL `https://dashscope.aliyuncs.com/compatible-mode/v1`、Key 占位符 `sk-...`（新卡片没有已存 Key），8 个 Qwen 系模型 |
| 5 | 图片能力标注 | `qwen3.8-flash` / `qwen3.8-max` / `qwen3.7-flash` / `qwen3.7-plus` / `qwen3.6-flash` 勾了「图片」；`glm-5.2` / `qwen3.7-max` / `deepseek-v4-pro` 没勾 |
| 6 | 点「保存提供商与模型」 | 「提供商与模型已保存」；磁盘上 `providers` 增加 `alibaba-cn`，8 个模型写入，**mimo 的 Key 原样保留** |
| 7 | 对 DeepSeek 卡片点「拉取模型列表」（Key 是 example 里的占位串） | 红色 **`拉取失败（HTTP 401，provider_unavailable）`** —— 真的打到了 `https://api.deepseek.com/v1/models` 并拿回 401，不是崩溃，也说明 §9.3 的端点拼接是对的 |
| 8 | 对 DeepSeek 卡片点「补充预设模型」 | 「已补充 3 个预设模型，记得保存。」，模型列表从 1 条变成 4 条（`deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` / `deepseek-flash` / `deepseek-v4-pro`），已存在的那条没有重复 |
| 9 | Ollama 预设 | `requires_api_key: false`，预设不带模型（本机跑什么只有本机知道） |

- `api-ai-provider-presets.json` —— `GET /api/ai-provider-presets` 的原样响应（19 个提供商、113 个模型）。

