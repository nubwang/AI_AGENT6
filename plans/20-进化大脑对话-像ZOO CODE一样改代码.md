# 规划 20：进化大脑对话 —— 像 ZOO CODE 一样，你与进化大脑对话后修改代码

## 一、用户核心想法（原文提炼）

> "agent对话这个导航功能，我希望是我与进化大脑的对话，进化大脑就像ZOO CODE一样与我对话最后修改代码"

即：前端的"Agent 对话"页不再是占位，而是**你 ↔ 进化大脑**的对话界面：

- 你提问/下指令（如"为什么昨天推荐的股票没涨？""帮我优化形态识别"）；
- 进化大脑结合 次日验证归因 / 回测健康诊断 / 系统图谱 / 当前参数，给出专业回答与可操作建议；
- 若需改代码，进化大脑给出改动方案 → 你点"生成代码提案" → 生成提案卡片 → 你确认后应用（走 备份/编译/回滚 工程护栏）。

## 二、现状与差距

- 现状：路由 `/agent`（"Agent对话"）→ [`Placeholder.vue`](ai-quant-agent/frontend/src/views/Placeholder.vue)（占位）；后端 [`agent.py`](ai-quant-agent/backend/app/api/agent.py) `/ask` 返回占位文本"开发中"。
- 差距：无真实对话、无"对话→改代码"闭环。

## 三、实现

### 3.1 后端 [`agent.py`](ai-quant-agent/backend/app/api/agent.py:1) —— 进化大脑对话

`POST /api/v1/agent/ask`：

- 注入上下文：`evolution_summarizer.build_l0_text()`（次日验证/环节归因/回测诊断）+ `backtest_ctl.health_text()`（回测健康诊断）+ `system_map.build_map_txt(short=True)`（系统图谱）；
- 进化大脑系统提示：像 ZOO CODE 一样能回答也能改代码，唯一宪法=提高每日推荐上涨概率（D+1 买入后上涨+主升浪），建议要指向具体文件/参数；
- 返回 JSON：`{"answer": 对话回复, "suggest_code_change": bool, "code_target": 目标方向}`。

### 3.2 前端 [`AgentChat.vue`](ai-quant-agent/frontend/src/views/AgentChat.vue:1)（新建）

- **聊天界面**：用户/进化大脑消息气泡（滚动区），输入框 + Ctrl+Enter 发送；
- **建议改代码**：回复 `suggest_code_change=true` 时显示"💡 建议改代码：{code_target}" + 按钮"让进化大脑生成代码提案"（复用 `triggerEvolveCodePropose("manual")` 走 [`code_writer`](ai-quant-agent/backend/app/agents/code_writer.py:1) 提案流程）；
- **待确认提案卡片**：列出 `pending_patches`（file/reason/evidence）+ "应用"按钮（`applyEvolveCode`，含备份/编译/回滚护栏）；
- **历史与回滚**：展示进化改动历史 + "回滚最近一次"按钮；
- **快捷指令**：常用问题一键发送。

### 3.3 路由 / 接口

- [`router/index.ts`](ai-quant-agent/frontend/src/router/index.ts:33)：`/agent` → `AgentChat.vue`，改名"进化大脑对话"；
- [`api/index.ts`](ai-quant-agent/frontend/src/api/index.ts:118)：新增 `askAgent(question)`。

## 四、对话 → 改代码闭环

```
你提问（如"为什么昨天推荐没涨？"）
   │
   ▼
进化大脑（注入 L0/回测诊断/图谱）→ 结构化回答 + suggest_code_change
   │
   ▼
前端显示回答；若建议改代码 → "让进化大脑生成代码提案"
   │
   ▼
code_writer.propose_code_evolution(mode=manual) → 提案卡片（待确认）
   │
   ▼
你点"应用" → apply_code_patch（备份→编译→回滚护栏）→ 待重启生效
   │
   ▼
效果回滚门（plans/19）：改后命中率连续下降自动回滚 .bak
```

## 五、验证

- 后端 `agent.py` 编译通过；前端 `vue-tsc + vite build` 通过（index 782KB）；
- 真实对话冒烟（重启后 `curl /agent/ask`）：
  - 提问"为什么昨天推荐的股票没涨？帮我分析问题环节"
  - 进化大脑回答：定位 **形态识别环节（17 例失败占 85%）**，建议 `form_classifier.py BREAKOUT_PCT 1.03→1.05 + 成交量确认`、`condition_score 引入市场因子`、`daily_lambda_exclude` 调整；
  - 返回 `suggest_code_change=True, code_target=形态识别`；
- 后端已重启生效（新 PID 99368，健康 `ok`）。

## 六、改造文件清单

| 文件                                                                 | 改动                                                                                               |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| [`agent.py`](ai-quant-agent/backend/app/api/agent.py:1)              | `/ask` 改造为进化大脑对话（注入 L0/回测诊断/图谱）                                                 |
| [`AgentChat.vue`](ai-quant-agent/frontend/src/views/AgentChat.vue:1) | **新建**：对话界面 + 提案卡片（含参数型）+ 历史回滚；propose/apply 显示真实失败原因                |
| [`api/index.ts`](ai-quant-agent/frontend/src/api/index.ts:118)       | 新增 `askAgent`                                                                                    |
| [`router/index.ts`](ai-quant-agent/frontend/src/router/index.ts:33)  | `/agent` → `AgentChat.vue`，改名"进化大脑对话"                                                     |
| [`code_writer.py`](ai-quant-agent/backend/app/agents/code_writer.py) | **锚点容错**（`_find_anchor` 行级归一化）+ **参数提案**（`_propose_param`/`apply_param_proposal`） |
| `plans/20-进化大脑对话-像ZOO CODE一样改代码.md`                      | 本文档                                                                                             |

## 七、修复："为什么不能改 form_classifier / daily_lambda_exclude"（用户反馈）

**问题**：进化大脑对话生成提案时，无法修改 `form_classifier.py`（加颈线/成交量确认、降相似度权重）和 `daily_lambda_exclude`（0.5）。

**根因 1（代码改动失败）—— 锚点校验过严**：full 模式本就不需要预置锚点，但 `apply_code_patch` 要求 LLM 生成的 `anchor_old` 必须 `count==1` **精确匹配**文件内容；LLM 生成的锚点常因缩进/空白细微差异 → 提案被拒（"锚点不唯一/不存在"）。

**根因 2（参数走错路径）**：`daily_lambda_exclude` 是 `evolution_config` 的 **tier=1 热生效参数**，应走参数热生效（`evolution_config.set_param`），而当前流程只能生成代码提案（要锚点），自然失败。

**修复（已实施）**：

1. **锚点容错**：[`_find_anchor()`](ai-quant-agent/backend/app/agents/code_writer.py:300) —— 精确唯一优先；失败时按**行去首尾空白归一化**匹配找唯一位置。LLM 给近似锚点（缩进/空白差异）也能正确定位 → 改 `form_classifier.py` 加确认条件不再失败。实测：带 8 空格缩进差异的锚点仍能定位 A 形态确认代码。
2. **参数提案（param 模式）**：进化大脑可输出 `{"mode":"param","param":"daily_lambda_exclude","value":0.65}` → [`_propose_param`](ai-quant-agent/backend/app/agents/code_writer.py:493) 校验参数在可进化表 + 范围 → [`apply_param_proposal`](ai-quant-agent/backend/app/agents/code_writer.py:533) 调 `evolution_config.set_param` **热生效，无需锚点/重启**。实测：`daily_lambda_exclude` 0.5→0.6→0.5 热生效成功。
3. **前端显示真实失败**：`AgentChat.vue` 的 `proposeCode`/`apply` 检查 `ok===false` 时用 `ElMessage.error(message)` 显示真实失败原因；提案卡片支持参数型显示（`参数 daily_lambda_exclude = 0.65`）。

> 结论：**没有预置锚点也能改代码**（full 模式自由生成 + 锚点容错）；**调参数不需要锚点**（param 模式热生效）。改动记录见 [`code_writer.py`](ai-quant-agent/backend/app/agents/code_writer.py:300)。

## 八、函数级编辑 —— 真正"像 ZOO CODE 一样，无需锚点"（用户：为什么还要锚点？ZOO CODE 怎么不需要？）

**解释**：ZOO CODE 不是"真不需要定位"，而是它能力更强——能读全文件 + 交互式反复确认，且以"整文件/函数级"为单位修改，几乎不依赖"锚点字符串精确匹配"。我们的进化大脑此前要求 LLM 提供 `anchor_old` 精确字符串，是**单次 LLM 调用的定位手段**，失败率高、体验差。

**新增 `function` 模式（彻底摆脱锚点字符串）**：

- LLM 只需输出 `{"mode":"function","file":"app/backtest/form_classifier.py","function":"classify_current","new_code":"<完整新函数>"}`；
- [`_find_function()`](ai-quant-agent/backend/app/agents/code_writer.py:333) 用 **AST** 自动定位函数/类方法的源码块（含 def/装饰器/函数体），整体替换；
- 无需提供任何 anchor_old 字符串，像 ZOO CODE 一样"说改哪个函数就改哪个函数"。

**四种模式（进化大脑按需选择）**：
| 模式 | 用途 | 是否需要锚点 |
| --- | --- | --- |
| `function` | 改一个函数/方法（推荐） | **否**（AST 自动定位） |
| `param` | 调可热生效参数 | **否**（热生效） |
| `full` | 整文件重写 | 否 |
| `anchor` | 改一个代码块 | 是（已加容错，近似即可） |

**验证**：AST 成功定位 `form_classifier.classify_current`（79 行函数体），函数级替换后 `py_compile` 通过；四种模式全链路可用（param 热生效已实测 0.5→0.6→0.5）。后端已重启生效（PID 16499）。

## 九、一句话对话 → 自动修改代码（ZOO CODE 式体验，用户："只用一段话就能修改相应的代码？"）

**答案：是的。** 现在进化大脑**一句话对话即可驱动改代码**：

```
你：帮我在 form_classifier 的 A 形态突破加成交量确认，并把 daily_lambda_exclude 调到 0.6 加强负向抑制
▼
进化大脑（/ask，注入 L0/回测诊断/图谱）→ 回复具体方案
  + suggest_code_change=true 时后端**自动调用 propose_code_evolution** 生成代码提案
▼
前端自动展示提案卡片（你无需再手动点"生成提案"）
▼
你点一次"应用" → 走 备份/编译/回滚/效果回滚门 护栏 → 修改生效
```

**实现**：

- [`agent.py`](ai-quant-agent/backend/app/api/agent.py:73) `/ask`：当进化大脑判定需改代码，后端自动 `propose_code_evolution(mode="manual")`，返回 `proposal_generated/proposal_message/proposal`；
- [`AgentChat.vue`](ai-quant-agent/frontend/src/views/AgentChat.vue:58) `send()`：收到 `proposal_generated=true` → 自动刷新并提示"进化大脑已自动生成代码提案，请在下方确认后点应用"；
- 提案可为 `function`（改函数，无需锚点）/ `anchor`（容错定位）/ `param`（参数热生效）任一模式。

**实测（真实 LLM，2026-08-21）**：对话上述内容 → 返回具体方案 + `proposal_generated=True`，自动生成代码提案（锚点容错定位），前端一键应用即可。

> 与 ZOO CODE 的唯一差别：我们保留"应用前确认"（安全护栏：备份/编译/回滚 + 效果回滚门防改坏），ZOO CODE 同样会确认后再改。

## 十、你说意图、进化大脑自己找代码（用户："让我主动说哪个函数名感觉不靠谱"）

**改进**：用户只需自然语言描述意图（**无需报函数名/文件/代码片段**），进化大脑自己定位并生成提案。

- [`propose_code_evolution`](ai-quant-agent/backend/app/agents/code_writer.py:553) 新增 `instruction` 参数；[`_propose_full`](ai-quant-agent/backend/app/agents/code_writer.py:641) 把用户自然语言指令注入提案 LLM 的 prompt 最前部，并要求"严格按此意图执行，自动定位要改的函数/文件/参数"；
- [`agent.py`](ai-quant-agent/backend/app/api/agent.py:74) `/ask` 自动生成提案时把 `req.question`（用户原话）+ 进化大脑建议方向一并传入。

**实测（真实 LLM，纯自然语言，2026-08-21）**：

> 用户：**"形态识别太松了，经常把普通横盘误判成启动，帮我收紧确认条件：突破要放量、突破当日成交量明显放大，减少假突破"**（未提任何函数名）
> 进化大脑：`suggest_code_change=True`，`proposal_generated=True`，提案**自动定位 `form_classifier.py` 的 `classify_current` 函数**（`function` 模式，AST 定位，无需锚点），reason"收紧形态确认：突破必须放量（成交量>20日均量\*1.5），减少普通横盘误判为启动"。

**结论**：进化大脑现在像 ZOO CODE 一样——**你说一句意图，它自己找到该改的函数/文件/参数并生成改动，你确认即生效**，无需你懂代码细节或报函数名。

## 十一、回测价值与改进方向（用户："降相似度权重说明回测没用？回测历史肯定有研究价值"）

**用户决策（重要）**：回测历史有重要研究价值，**不要通过"降低回测产物权重/降相似度权重/调弱负向抑制"来妥协**——那等于放弃回测成果。当实盘命中低于回测时，优先**改进回测逻辑本身**及**回测→每日推荐的传导一致性**，让更好的回测真正服务于每日推荐。

**已实施**：

1. **完整回测逻辑纳入可进化范围**：[`EVOLVABLE_FILES`](ai-quant-agent/backend/app/agents/code_writer.py:489) 扩充至 28 个文件，新增回测核心：`runner / outcome_tracker / pattern_miner / failure_miner / feature_extractor / threshold_analyzer / vector_store / loader / extra_feature_loader / data_validator / monitor / metrics / cluster / validator / attribution / rule_forms` —— 进化大脑可自由改进完整回测流水线；
2. **行为约束注入**：`_CODE_SYSTEM_FULL`（提案）与 `_ASK_SYSTEM`（对话）均加入"重要原则"——不降权妥协，优先改进回测逻辑（结局分流/特征拟合/模式库/条件表/归一化）与传导一致性（daily_scan 加载口径、特征同源）；
3. **传导校验**（配合 plans/19 `backtest_health`）：`backtest_decouple`（回测准实盘低=产物脱节）时指向"检查 daily_scan 加载口径/特征同源"——即改进**衔接**而非降权。

**逻辑**：回测 0.80 accuracy 证明历史模式库本身有预测力（价值在）；实盘低是"回测产物没被每日推荐用好"（传导/口径问题）或"回测样本与实盘目标不完全同源"。进化大脑应把力气花在**让回测产物被每日推荐正确、充分地使用**（口径一致、特征同源、结局分流合理），而非把相似度权重调小（等于扔掉研究价值）。
