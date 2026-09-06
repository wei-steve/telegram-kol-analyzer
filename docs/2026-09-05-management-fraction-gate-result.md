# 非法减仓比例门禁交付

分支：`codex/management-fraction-gate`。基于止损门禁候选 `d1e3d8582501c54f1b9c105a0058c224e902c824`，仅本地开发；未合入、未推送、未部署。

## 行为与证据

- `_fraction_value` 明确区分三种结果：有效 float、缺失 None、非法 `ManagementFractionInvalid`。注释解释非法返回 None 会错误触发 50% 默认。None / 空白保留原默认；非空无法解析、非有限数、越界、转换下溢均拒绝。
- 裸值必须在 `(0, 1]`；带 `%` / `％` 的值先校验 `(0, 100]` 再换算。例如 `0.5` / `50%` 有效，`0`、负数、`150%`、裸值 `50` 非法。先判断范围，避免巨大指数换算溢出。
- 平仓与保留比例文本共用检查，保留负号、格式异常、跨行和标点后的内容。保留 100% 导致平仓 0，也拒绝。重复百分号或范围/斜杠连接的续值拒绝；无关行情百分比不改变合法减仓数量。已有多个合法比例冲突的 `management_fraction_ambiguous` 行为保留。
- 权威识别在候选/指令投影前检查原始生命周期字段和 instruction parameters，防止归一化丢弃非法输入。非法 target 字段也预检拒绝，未放宽现有 target schema。V1 生命周期入口同样返回拒绝，不继续本地兜底。
- reason_code：`management_fraction_invalid`；Runtime Incident：`management_fraction_rejected`（high）。采用现有 Incident 表，记录原始消息引用、输入来源、错误分类和 `default_applied=false`，不记录原文。V1 记录一般 fraction_inputs / invalid 分类。Incident 捕获独立于可选 AI 开关；写入失败向上传播，不能继续交易。
- 缺失比例的回归测试验证生成 0.5 候选和关联管理指令；非法输入验证识别失败、零候选、零可执行指令、专用 Incident 和持仓生命周期未变化。所有数据来自本地临时 SQLite 测试夹具。

## RED → GREEN

- 初始 RED：28 failed / 3 passed，覆盖 0、负数、150%、无法解析和正文越界的错误兜底及拒绝证据缺失。
- 审查补充 RED：空白负号、分隔符、极大指数 7 failed；跨行 4 failed；跨行负号 1 failed；重复百分号/范围续值 4 failed。修复后全部转绿。
- 最终定向：230 passed（management_directives、message_recognition、strategy_management_contracts、management_stop_price_gate）。
- 完整 pytest：**7444 passed、4 skipped、32 warnings，480.72 秒**。命令 `.venv/bin/python -m pytest -q`；相比基线 7389 passed 增加 55 个通过用例。日志 `/tmp/management-fraction-full-pytest.log`。
- 独立复审：已报告问题全部闭环，无剩余阻断项。

## 边界与回退

未修改 schema、业务数据、生产配置或服务；未调用交易所写接口，未接触 uncertain 6/25/77/191/199、历史消息重放或 ETH 实验仓位/保护单。生产 af8676dca5ce83acfc060a8b856ccf3884f25150 未被本任务操作。没有从 immutable release 导入脚本。止盈幅度与杠杆上限留待单独排期。

回退仅涉及丢弃未合入分支；生产无需回退。拒绝输入需要人工审阅并重新提供明确合法指令，本修复不生成替代数量或重放旧指令。
