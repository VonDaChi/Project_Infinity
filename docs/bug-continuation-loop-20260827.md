# Bug 报告：GM 续写循环失控（前端永久无回复）

- **提交日期**：2026-08-27
- **组件**：`game_engine.py`（GM 主循环 / checkpoint-resume 协议）
- **严重度**：高（功能不可用——任意一次用户输入后前端永久无回复）
- **状态**：已修复（组合方案：根因修复 + 安全上限 + 中间渲染）
- **关联日志**：`output/electronistu_weave_debug_20260827_180709.log`（失控现场）、`output/pui_weave_debug_20260826_190535.log`（同模型/后端未复现对照）

---

## 1. 背景（Background）

Project Infinity 是一个文字版 D&D 5e 跑团引擎：LLM 担任 GM（游戏主持人），MCP 工具负责掷骰与状态变更。`game_engine.py` 实现了一套 "checkpoint / resume" 续写协议（规范见 `GameMaster_MCP.md` v16）：

模型在每回合机械结算（掷骰、库存、金币等）审计通过后，应**单独**发出同步标记 `{{_NEED_AN_OTHER_PROMPT}}`（两种拼写 `{{_NEED_ANOTHER_PROMPT}}` 等效）表示"交还控制权"，引擎随后注入 `{{_CONTINUE_EXECUTION}}` 让模型产出剧情。该机制旨在支持超长单回合的分段生成。

---

## 2. 根因分析（Root Cause Analysis）

Bug 有**两层**，缺一不可：

### 2.1 子串误判（直接触发点）
`game_engine.py` 中 `chat_with_tools` 闭包原本用 `token in content` 做**子串**匹配（原 `:436`）：

```python
if any(token in (content or "") for token in ["{{_NEED_AN_OTHER_PROMPT}}", "{{_NEED_ANOTHER_PROMPT}}"]):
    return "__SYSTEM_PAUSE__"
```

只要回复里**出现过**该标记（哪怕夹在剧情结尾，如 `"你推开门。{{_NEED_AN_OTHER_PROMPT}}"`）就命中。命中后直接 `return "__SYSTEM_PAUSE__"`，把整段剧情**丢弃**，只回一个哨兵串。

### 2.2 历史污染（自激放大器）
闭包在 token 检测**之前**就把含标记的 `content` 原样 `messages.append` 落进对话历史。主控循环见到 `__SYSTEM_PAUSE__` 便自动注入 `{{_CONTINUE_EXECUTION}}`，模型被催出更多"剧情 + 结尾标记"，这些又被原样存入历史……
形成正反馈：

- 模型在自己上下文里不断看到样例："assistant 每段都以该标记结尾 → user 回 continue"；
- 本地小模型（koboldcpp）对"最近上下文样例"的权重高于系统提示词，**越陷越深、永不退出**；
- 而真正的界面渲染位于恢复循环**结束之后**——循环永不结束，渲染永远执行不到，于是前端一直"转圈"却无回复。

### 2.3 为什么本地模型会触发
协议 v16 冗长严格（约 169 行），koboldcpp 这类本地/小模型对"独立发出标记"的语义遵循差，倾向于把标记当成"段落结束符"追加在剧情末尾。一旦某次用户回合（尤其含多处状态变更的回合，如"买别墅"涉及金币/库存）模型走了这个捷径，循环即被点燃。

---

## 3. 复现条件（Reproduction）

### 3.1 必要条件
- 后端为本地模型（koboldcpp 已复现）；
- 该模型在某次用户回合的回复里，把 `{{_NEED_AN_OTHER_PROMPT}}` **内联/结尾追加**进剧情，而非独立发出。

### 3.2 加剧因素
- 角色存档规模大（electronistu 为 5 级法师，玩家库 + 时间线上下文远大于 pui 的 1 级全新档），更大的上下文削弱指令遵循，更易偏离协议。

### 3.3 对照（pui，未复现）
pui 仅在开场 Awakening **单独**发了一次合法标记（独立、无剧情），被续写一次后产出不带标记的剧情，循环一次退出；其用户回合（单纯一次 `perform_check`）始终在协议内，从未内联标记。说明这是"何时触发"，不是"模型固有"——**任何存档都潜伏该风险**。

### 3.4 最小复现脚本
用 koboldcpp 起一局，诱导模型回 `……你推开门。{{_NEED_AN_OTHER_PROMPT}}`，即可观察循环不终止、界面无回复。

---

## 4. 影响范围（Impact）

| 维度 | 影响 |
|---|---|
| 功能 | 用户任意一次输入后可能陷入无限续写，前端永久无回复（仅 debug 模式能看到原始 `DEBUG RESPONSE` 在刷） |
| 资源 | `prompt_eval_count` 每轮上涨（日志实测 14700 → 42116），持续烧本地算力与上下文；对话历史被含标记的脏数据污染，模型行为进一步退化 |
| 数据 | 世界/角色存档（`*.wwf` / `*.player`）由独立工具写盘，**不受影响**，可安全中止进程 |
| 范围边界 | 逻辑完全集中在 `game_engine.py`；7 个适配器（`play_with_*.py`）与 `forge/` TUI 均未重复实现，修复面极小 |

---

## 5. 修改方案（Proposed Fix）— 组合方案

### 5.1 A. 根因修复（独立标记判定 + 内联剥离）
- 新增模块级 `PAUSE_TOKENS`、`_is_pure_pause_token(text)`、`_strip_pause_tokens(text)`。
- token 检测改为"仅当 `content` 去除空白后**恰好等于**某个 token（独立出现）才算暂停"；内联出现的 token 一律剥离后当作正常剧情渲染，且**不**入历史（落历史前即剥离）。
- 覆盖 `chat_with_tools` 主路径与 thinking 重试边路（`_is_pure_pause_token` 复检）。

### 5.2 B. 安全上限（MAX_RESUMES）
- 两处恢复 `while` 循环（开场、主循环）加 `MAX_RESUMES = 3` 计数上限，计数器按"每个用户回合"重置（主循环每轮用户输入重新进入该段，天然重置）。
- 超限仍为暂停时，兜底调用 `_emit_narrative(tr('gm.resume_fallback'))`，保证界面不空白、不卡死、不抛异常。

### 5.3 C. 中间渲染（统一 helper）
- 新增 `run_game` 内 `_emit_narrative(text, title)` helper：剥离 token → 渲染 Panel → 按段递增 `narrative_counter` → 在 `image_frequency` 节奏点生成场景图。
- 两处恢复循环改由 `_emit_narrative` 统一渲染，删除原先重复的 Panel 与图像块。恢复循环期间本应被丢弃的剧情段现在会被正常渲染出来。

### 5.4 同步改动
- timeline checkpoint 的 token 剥离统一为 `_strip_pause_tokens`（DRY，行为不变）。
- `i18n.py` 新增 `gm.resume_fallback`（en + zh）；token 字面量保持英文，不进 `tr()`。

### 5.5 关键代码位置（修复后）
| 改动 | 位置 |
|---|---|
| `PAUSE_TOKENS` / `MAX_RESUMES` / `_is_pure_pause_token` / `_strip_pause_tokens` | `game_engine.py` 模块级常量区 |
| 落历史前检测+剥离 | `chat_with_tools` 闭包（原 `:355-366` 区段） |
| 删除旧子串检测 | 原 `:436-440` |
| thinking 重试边路复检 | 原 `:391-400` 区段 |
| `_emit_narrative` helper | `run_game` 内 `_auto_generate_image` 之后 |
| 开场恢复循环 | `run_game` 开场段（原 `:590-611`） |
| 主循环恢复循环 | `run_game` 主循环（原 `:650-674`） |
| timeline 剥离统一 | `run_game` timeline checkpoint（原 `:685-688`） |
| `gm.resume_fallback` | `i18n.py` |

---

## 6. 自测方法（Test Plan）

1. **正常局**：koboldcpp 走完开场 Awakening → 正常指令，界面显示、图像按 `image_frequency` 触发。
2. **独立 token**：诱导模型单独回 `{{_NEED_ANOTHER_PROMPT}}`，确认暂停并自动续写（验证 Awakening 合法流程不被破坏）。
3. **内联 token**：诱导回 `……你推开门。{{_NEED_AN_OTHER_PROMPT}}`，确认该句原样渲染、token 不残留界面/历史。
4. **上限兜底**：临时 `MAX_RESUMES=1` 并让模型连续纯暂停，确认超限后界面出现 `gm.resume_fallback` 提示、不空白、不卡死、不抛异常。
5. **历史洁净**：`--debug` 下搜 `messages` 中不应再出现 `{{_NEED_AN_...}}`。
6. **时间线**：跑够 `TIMELINE_INTERVAL` 回合，确认 `*.timeline.md` 写入不含 token。

---

## 7. 回归风险与取舍（Trade-offs）

- 严格 A 下，内联 token 的回复会被当作**正常剧情渲染并结束本回合**，模型不再被续写（这是选定语义）。若未来希望"内联也续写"，只需把 A 的判定改回子串匹配、其余不变。
- `narrative_counter` 现只在 `_emit_narrative` 递增，图像节奏按"渲染段数"计，与原"按回合最终回复"基本等价；`image_frequency` 较小时更细，属预期改善。
- 开场 Awakening 单独发标记属独立标记典型场景，新判定**正确**命中暂停并续写，不受影响。
