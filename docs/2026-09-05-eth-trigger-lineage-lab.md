# ETH 多空触发单字段实验

状态：实验工具和请求草案已准备；未下单、撤单、平仓、启动私有采集或修改生产系统。不能把准备完成当作实盘实验完成。

## 真实测试入口（后续更新，取代仅只读准备阶段的操作步骤）

用户要求自行粘贴一条命令，实际进行下单实验。新增 `scripts/deepcoin_trigger_lineage_live.py`，与只读采集器分开。真实入口只有显式 `--execute-eth-minimum-pair` 才可用。助手未执行这个入口。

Mac 终端，由用户亲自执行：

```bash
ssh -t tecent 'python3 -B /var/lib/telegram-kol-cutover-evidence/eth-minimum-test-20260905-v2/deepcoin_trigger_lineage_live.py --execute-eth-minimum-pair'
```

它会实际提交两笔 ETH-USDT-SWAP 触发限价入场，使用服务器现有 worker 的凭据。最小数量动态计算且每侧不超过 0.01 ETH、名义额不超过50 USDT；不增加杠杆。存在ETH仓位、行情过期、最小数量超限或身份校验失败时，会在下单前停止。默认使用已说明的cross/split。

基准价为执行时的最新成交价 P；多入场P-1、SL=P-11；空入场P+1、SL=P+11。止损市价，止损距离以预设限价计算。每侧0.01 ETH时，价格恰好亏损10 USDT对应0.10 USDT毛亏损，这是算术示例，不含手续费、滑点或资金费，也不是最大损失保证。

程序仅尝试一多一空，不因不支持tag/clOrdId、拒绝或超时增加样本。父ordId仅取交易所回执，自定义字段是否被忽略或传播由证据判断。两侧并发不保证原子接受。请求、回执、毫秒UTC时间、所有GET原文保存至该服务器目录内live-<run_id>。

正常观察约5分钟，随后只撤销仍明确挂着的本次父触发单，输出撤销结果和剩余仓位/挂单。**已触发子挂单、仓位和附带止损不会自动撤销或平仓；用户需在Deepcoin核对并处理。** 终端中按Ctrl-C会请求停止观察并进入相同收尾，但机器或网络强制终止不能保证收尾发生。

一次真实尝试后写入LIVE-ATTEMPT.json，重复粘贴被阻止，不会重复下单。不要删除它重试；先查原始结果。一侧拒绝/未知仍做短窗口只读采集，始终保留未知状态，不重提。

38项focused模拟测试通过，独立审查已覆盖误撤、历史候选、短暂止损、未知结果证据及收尾失败状态。本轮未运行真实POST；旧只读命令仍保持只读。

服务器安装完成，已仅做AST语法解析和文件SHA256核对，没有导入或执行真实入口。live脚本SHA256=`7bc20f387466eb622a84b54b72bc773c7b595a0ca7a5f0b14e16babe55417c95`；只读依赖SHA256=`81454a60ebdd00314f7d477780213dce58ae05fcec25ca81342d1472df7c65c6`。安装记录在上述目录installation.json，live_executed=false。

### 首次用户执行结果与 v2 修复

用户自行运行的首次实验 run_id=ad64023bdc61，窗口2026-09-05T15:01:45.670Z至15:02:02.008Z。两侧于15:01:50.748Z发出，分别于15:01:50.914Z和15:01:50.918Z收到HTTP200、业务code=51、msg="The tdMode field is required"，均明确rejected，无ordId。因此本次没有成功创建实验订单，不能检验父子字段继承。

根因是实验草案构造器遗漏tdMode，isCrossMargin=1不能替代它；生产build_deepcoin_trigger_order_payload原本同时填写两个字段，本次没有修改生产构造器。新增回归断言先复现KeyError:tdMode，再补tdMode=cross，38项focused测试通过。

旧证据保留在`/var/lib/telegram-kol-cutover-evidence/eth-minimum-test-20260905/live-ad64023bdc61/`，不删除或重置旧LIVE-ATTEMPT.json。修复版放入独立`eth-minimum-test-20260905-v2`目录；依赖SHA256更新为`9979db4eb248350fb8535b8fda8404258aa1afddd70f5626e3f688168ea817fd`，live入口脚本未改变。已验证远端语法和文件哈希，未运行修复后的真实入口。上方命令已更新为v2，由用户自行执行。

## 服务器启动入口（后续更新：2026-09-05 14:40 UTC）

用户已要求直接使用服务器项目的凭据。已在独立证据目录安装 collector.py 和 launch.py，未修改项目 checkout、不可变 release、服务配置或运行进程。launcher 只从经过身份核验的 worker 进程环境读取三项 Deepcoin 凭据，留在服务器内存，不打印、不下载、不复制到配置文件。

Mac 终端单命令启动 300 秒只读观察：

```bash
ssh tecent 'python3 -B /var/lib/telegram-kol-cutover-evidence/eth-trigger-lineage-lab-20260905-01a071e6/launch.py'
```

每次启动创建独立 observation 目录。ID 清单在同目录 ids.json，可更新已知 ordIds/clOrdIds/posIds。该清单初始为空，不带入已过期草案的 ID。

私有连接检查已实际通过：ETH positions 查询返回 0 行，ETH trigger-orders-pending 查询返回 2 行。这仅是本次成功查询的计数，不是完整账户或全部历史结论。该次 worker release_commit=9501a5f39f0c5f196cc29f24f3e3b8786267126b，loaded_artifact_verified=true，接口 PID 与 systemd MainPID 一致。

证据：`/var/lib/telegram-kol-cutover-evidence/eth-trigger-lineage-lab-20260905-01a071e6/observation-20260905T144032Z-41baa9/raw.jsonl`。collector SHA-256：`81454a60ebdd00314f7d477780213dce58ae05fcec25ca81342d1472df7c65c6`。

本次只验证两个私有 GET；尚未运行完整 300 秒窗口，未真实下单。下文“私有采集尚未实测”属于之前准备阶段记录，以上更新取代其连接验证状态。

## 已确认参数

ETH-USDT-SWAP。同一最新成交价 P，多单触发价和限价 P-1，空单 P+1。多单止损 P-11，空单 P+11，以 last 触发后市价执行（slOrdPx=-1）。没有止盈。

止损相对于**预设委托限价**；实际成交可能有价格改善，实际成交均价到止损价的差不保证为 10。两侧请求具有同一行情基准，不保证原子提交或两侧都触发。不会为了完成观测人为触发止损。

初次生成快照：P=2454.02，最小 sz=0.1 张，ctVal=0.1 ETH/张，因此每侧名义数量 0.01 ETH。lotSz=0.1、tickSz=0.01。真实执行前必须重新生成，旧草案只作证据，程序标记行情时间后 15 秒到期。

| 方向 | 触发价=委托限价 | 附带止损 |
| --- | --- | --- |
| long | 2453.02 | 2443.02 |
| short | 2455.02 | 2465.02 |

草案显式选择项目常用 cross/split；账户杠杆未核验，不会通过此工具更改账户模式、杠杆或已有保护。执行者需核对这些账户参数。ordId 由服务端生成，不能作为自定义提交字段；clOrdId 和 tag 分方向唯一，都是待测能力，不承诺服务端接受或传播。

## 文件与使用

- `scripts/deepcoin_trigger_lineage_lab.py`：独立标准库程序，无项目生产模块导入，无交易写入路径。
- `data/deepcoin_trigger_lineage_lab/20260905-confirmed-eth-pair/manifest.json`：初次真实公开行情生成的草案。
- 同目录 `public.jsonl`：公开规格和行情完整原始响应、本地起止时间。
- 同目录 `ids.json`：采集期间可更新的 ordIds、clOrdIds、posIds 清单。
- 同目录 `submission-record-template.json`：两侧提交记录模板；现在的时间、HTTP 状态和响应均为 null，表示**没有执行**。

从仓库目录刷新草案（输出目录必须是新的）：

```bash
python3 -B scripts/deepcoin_trigger_lineage_lab.py prepare \
  --output data/deepcoin_trigger_lineage_lab/eth-pair-fresh --variant both
```

prepare 仅调用公开 GET，没有 API 凭据需求，不提交 manifest 中的 POST 请求。

由执行者在具备私有查询凭据的环境启动只读采集，并在提交前确认第一轮证据已经写入。凭据通过既有安全环境提供 DEEPCOIN_API_KEY、DEEPCOIN_API_SECRET、DEEPCOIN_API_PASSPHRASE，不在命令行、日志或本说明中填入值：

```bash
python3 -B scripts/deepcoin_trigger_lineage_lab.py observe \
  --ids data/deepcoin_trigger_lineage_lab/eth-pair-fresh/ids.json \
  --output data/deepcoin_trigger_lineage_lab/eth-pair-observation \
  --seconds 300 --interval 5
```

真实请求由用户自行执行。必须从实际提交客户端保留两侧准确的请求、发送/接收时间、原始 HTTP/业务回执，填写提交记录，**不要保存鉴权头或密钥**。不能用事后回忆的时间代替真实发送时间，也不能用数据库富化对象代替原始响应。把返回的父 ordId 加入 ids.json；后续已知子单、止损单及 posId 可继续加入清单。编辑文件采用临时文件后替换，避免采集时读到半份 JSON。

观察不会执行任何订单清理。真实交易的提交、撤销与平仓均由用户处理。若用户手动收尾，也需要保存相同格式的实际请求/回执及精确 ID。

## 全面实验矩阵

首组 `--variant both` 对两侧设置独立 clOrdId/tag，符合本次要求。若要区分字段问题，可分别生成 `baseline`（均不带）、`client`（仅 clOrdId）、`tag`（仅 tag）的两侧草案。这只是备选诊断组，不自动增加真实交易次数。

对每一组、每一方向分别记录：

1. 请求是否被明确接受或拒绝；超时是结果未知，不能据此重提。
2. 父触发回执是否回显自定义字段。
3. 父单触发前活动快照、触发后历史是否保留这些字段或明确子引用。
4. 普通执行单详情、活动/历史、fills 是否有父 ordId、自定义 clOrdId 或 tag；同一 ordId 的不同端点值是否一致。
5. 是否出现自动附带止损；止损是否带有父/子单或仓位的明确 ID 引用。
6. 如果自然发生止损触发，另检查它生成的执行单和 fills；未发生则此项为未验证。
7. 对父 ID、子 ID、自定义 clOrdId 的详情查询结果分别保留；空结果不能等同订单不存在。tag 没有已确认查询参数，不伪造按 tag 查询。

“回显成功”“继承成功”“在本次样本中可唯一关联”“官方保证长期唯一关联”是不同结论。单个两侧样本不能证明任意多次触发、部分成交或重建场景。订单→多笔成交应保留全部 tradeId/billId，不假设只有一笔成交。不得以相邻订单号、价格数量相同、时间接近或 clOrdId 恰等于子单自身 ID 作为父子证明。

## 采集范围与局限

raw.jsonl 保存白名单 GET 的完整响应正文和解析结果、请求路径和查询参数、UTC 开始/结束时间、耗时与 HTTP 状态；交易所原有时间字段原样保留。frames.jsonl 保存每轮数量和缺口，summary.json 保存窗口状态。

每个私有 GET 至少间隔 0.75 秒。interval=5 是轮次最小间隔，实际每轮可能更长，不声称 5 秒内能看见所有状态。默认 300 秒、最多 1800 秒；默认超时每请求 15 秒，窗口截止时已在途 GET 最多延后约 15 秒。单次 GET 失败最多重试一次，仍失败即退出；最后一轮被截止打断会明确标记 incomplete，已经保存的证据不删除。

普通历史和成交最多 3 页，每页 100，按官方 before 向更旧数据翻页；成交查询从观察开始前 60 秒过滤，普通历史遇到整页早于该边界停止。这个边界只用于限制观测成本，不是全账户历史穷尽声明。活动单、新近普通历史和成交中的订单 ID 加入详情候选；候选最多 100，已知 ID 优先。超过上限、ID/时间缺失、重复页或历史未覆盖到窗口边界，会明确记录不完整并停止。触发历史/活动接口没有在当前公开契约确认可用的分页参数，返回 100 条时不声称完整。

REST 轮询会错过短暂状态。已知父/子/止损 ID 的精确详情、触发历史与 fills 查询补充证据；未知订单之间仍须证明归属。没有原始提交回执时无法完整恢复“请求是否传入自定义字段”这一段。凭据/IP 白名单、账户并发交易、私有 API 的当前行为，本轮均未验证。

## 验证记录

15 项 focused tests 通过：价格方向、数量换算与步长、非有限/过期行情、tick 不符拒绝、字段组与 ID 唯一、只读路由限制、原始错误和时间保留、分页上限/重复页、观测窗口边界、二次失败停止。

真实公开 prepare 成功。私有采集尚未实测；无实盘父子归属结论。未修改生产代码、配置、数据库或服务，因此没有部署和全生产测试。

官方资料：

- https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrder
- https://www.deepcoin.com/docs/DeepCoinTrade/order
- https://www.deepcoin.com/docs/DeepCoinTrade/ordersHistory
- https://www.deepcoin.com/docs/DeepCoinTrade/tradeFills
- https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrdersHistory
- https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrdersPending
