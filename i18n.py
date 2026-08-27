"""Lightweight dictionary-based i18n layer for Project Infinity.

Design notes:
- No gettext: the project has only en/zh and ~60 UI strings; a dict is enough
  and has zero extra dependencies (important for the embedded interpreter).
- Missing keys fall back to English, then to the key itself. Never raises.
- Language state is a module-global, switchable at runtime (see /lang).
- Persistence: config/settings.yml ({"language": "en"|"zh"}), resolved against
  this file's directory so it works regardless of the launch cwd.

Things that MUST stay in English (never add to _STRINGS):
- Protocol tokens: {{_SYNC_DATABASE}}, {{_CONTINUE_EXECUTION}}, etc.
- Slash command names: /help /stats /save /sync /quit /lang
- Timeline format markers: **Key Events** etc. (game_engine validates them)
- TIMELINE_PROMPT, argparse help texts, verbose/debug output
- D&D data values from config/ (spell names, item names, stats...)
"""
import os

import yaml

_SUPPORTED = ("en", "zh")
_current = "en"
_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "config", "settings.yml")


def get_lang():
    return _current


def set_lang(lang, persist=True):
    """Switch language. Unknown values fall back to 'en'."""
    global _current
    _current = lang if lang in _SUPPORTED else "en"
    if persist:
        _save(_current)


def _save(lang):
    os.makedirs(os.path.dirname(_SETTINGS), exist_ok=True)
    with open(_SETTINGS, "w", encoding="utf-8") as f:
        yaml.safe_dump({"language": lang}, f, allow_unicode=True)


def load_saved():
    """Load persisted preference. Missing/corrupt/invalid -> 'en'."""
    try:
        with open(_SETTINGS, "r", encoding="utf-8") as f:
            lang = (yaml.safe_load(f) or {}).get("language", "en")
        set_lang(lang, persist=False)
    except Exception:
        set_lang("en", persist=False)


def tr(k, **kw):
    """Translate key k to the current language; fallback en -> key itself.

    First parameter is named `k` (not `key`) so callers can safely use
    `key=...` as a format placeholder, e.g. tr('entry.apikey_missing', key=...).
    """
    entry = _STRINGS.get(k, {})
    s = entry.get(_current) or entry.get("en") or k
    return s.format(**kw) if kw else s


_STRINGS = {
    # ── game_engine.py ─────────────────────────────────────────────
    "err.prefix":      {"en": "Error:", "zh": "错误："},
    "err.no_wwf":      {"en": "No .wwf files found in the output directory.",
                        "zh": "output 目录中没有找到 .wwf 世界文件。"},
    "world.title":     {"en": " Infinity Project: World Selection ",
                        "zh": " Infinity Project：世界选择 "},
    "world.prompt":    {"en": "Select a world file (number)",
                        "zh": "请选择世界文件（输入编号）"},
    "world.invalid":   {"en": "Invalid selection. Defaulting to first file.",
                        "zh": "无效选择，将默认使用第一个文件。"},
    "world.selected":  {"en": "Selected world:", "zh": "已选择世界："},
    "mode.verbose":    {"en": "Verbose mode enabled", "zh": "详细模式已开启"},
    "mode.debug":      {"en": "Debug mode enabled", "zh": "调试模式已开启"},
    "gm.malformed":    {"en": "The GM stumbles over their words... (malformed response)",
                        "zh": "GM 语无伦次……（响应格式异常）"},
    "gm.deep_thought": {"en": "The GM pauses, deep in thought...",
                        "zh": "GM 陷入了沉思……"},
    "gm.thinking":     {"en": "GM is thinking...", "zh": "GM 思考中……"},
    "gm.awakens":      {"en": "The Game Master Awakens", "zh": "游戏主持人降临"},
    "gm.title":        {"en": "Game Master", "zh": "游戏主持人"},
    "gm.resume_fallback": {"en": "The GM is still gathering its thoughts…",
                           "zh": "GM 仍在整理思绪……"},
    "gm.error":        {"en": "Error communicating with GM: {e}",
                        "zh": "与 GM 通信出错：{e}"},
    "help.title":      {"en": "Help", "zh": "帮助"},
    "help.body": {
        "en": ("[bold white]Available Commands:[/bold white]\n\n"
               "  [cyan]/help[/cyan]  - Show this help message\n"
               "  [cyan]/stats[/cyan] - Display current player stats\n"
               "  [cyan]/save[/cyan]  - Overwrite your .player file with your current character sheet (active effects are cleared/reverted)\n"
               "  [cyan]/sync[/cyan]  - Force a database sync with the GM\n"
               "  [cyan]/lang[/cyan]  - Switch interface language (/lang en, /lang zh)\n"
               "  [cyan]/quit[/cyan]  - Exit the game\n\n"
               "[dim]Type anything else to send as an action to the Game Master.[/dim]"),
        "zh": ("[bold white]可用指令：[/bold white]\n\n"
               "  [cyan]/help[/cyan]  - 显示本帮助\n"
               "  [cyan]/stats[/cyan] - 显示当前角色状态\n"
               "  [cyan]/save[/cyan]  - 将当前角色卡写回 .player 文件（生效中的效果会被清除/还原）\n"
               "  [cyan]/sync[/cyan]  - 强制与 GM 进行数据库同步\n"
               "  [cyan]/lang[/cyan]  - 切换界面语言（/lang en、/lang zh）\n"
               "  [cyan]/quit[/cyan]  - 退出游戏\n\n"
               "[dim]输入其它任意内容将作为行动发送给 Game Master。[/dim]"),
    },
    "sync.start":      {"en": "Synchronizing database...", "zh": "正在同步数据库……"},
    "sync.done":       {"en": "Database synchronized.", "zh": "数据库已同步。"},
    "save.done":       {"en": "Character sheet saved to {path}",
                        "zh": "角色卡已保存到 {path}"},
    "save.reverted":   {"en": "Reverted effects for save: {names}",
                        "zh": "存档时已还原的效果：{names}"},
    "save.fail":       {"en": "Save failed — could not read database.",
                        "zh": "保存失败——无法读取数据库。"},
    "cmd.unknown":     {"en": "Unknown command: {cmd}", "zh": "未知指令：{cmd}"},
    "cmd.hint_help":   {"en": "Type /help for available commands.",
                        "zh": "输入 /help 查看可用指令。"},
    "inject.world":    {"en": "Injecting World Data (The Key)...",
                        "zh": "正在注入世界数据（The Key）……"},
    "game.started":    {"en": "--- Game Started. Type /help for commands. ---",
                        "zh": "--- 游戏开始。输入 /help 查看指令。 ---"},
    "prompt.action":   {"en": "Your Action:", "zh": "你的行动:"},
    "quit.goodbye":    {"en": "Closing connection to the void... Goodbye.",
                        "zh": "正在关闭与虚空之门的连接……再见。"},
    "game.interrupted": {"en": "Interrupted. Type /quit to exit.",
                         "zh": "已中断。输入 /quit 退出。"},
    "game.bye":        {"en": "Game interrupted. Goodbye.", "zh": "游戏已中断，再见。"},
    "game.fatal":      {"en": "Fatal error: {e}", "zh": "致命错误：{e}"},
    "game.ended":      {"en": "The game session has ended unexpectedly.",
                        "zh": "游戏会话意外结束。"},
    "img.generating":  {"en": "Generating image...", "zh": "正在生成场景图……"},
    "lang.current":    {"en": "Current language: {lang}", "zh": "当前语言：{lang}"},
    "lang.usage":      {"en": "Usage: /lang en  or  /lang zh",
                        "zh": "用法：/lang en 或 /lang zh"},
    "lang.switched":   {"en": "Interface language switched to {name}. Saved to config/settings.yml.",
                        "zh": "界面语言已切换为{name}，并保存到 config/settings.yml。"},
    "lang.invalid":    {"en": "Unknown language '{lang}'. Use /lang en or /lang zh.",
                        "zh": "未知语言“{lang}”。请使用 /lang en 或 /lang zh。"},
    "lang.name.en":    {"en": "English", "zh": "英语 (English)"},
    "lang.name.zh":    {"en": "Chinese (Simplified)", "zh": "中文（简体）"},

    # ── display.py ─────────────────────────────────────────────────
    "stats.player_title": {"en": "Player Stats", "zh": "玩家状态"},
    "stats.fail":        {"en": "Could not retrieve player stats.",
                          "zh": "无法获取角色状态。"},
    "stats.char":        {"en": "⚔️ Character", "zh": "⚔️ 角色"},
    "stats.combat":      {"en": "🛡️ Combat", "zh": "🛡️ 战斗"},
    "stats.stats":       {"en": "📊 Stats", "zh": "📊 属性"},
    "stats.spellcasting": {"en": "🔮 Spellcasting", "zh": "🔮 施法"},
    "stats.profs":       {"en": "🎯 Skills & Proficiencies", "zh": "🎯 技能与熟练项"},
    "stats.inventory":   {"en": "🎒 Inventory & Consumables", "zh": "🎒 物品与消耗品"},
    "stats.reputation":  {"en": "🏆 Reputation", "zh": "🏆 声望"},
    "stats.effects":     {"en": "🌀 Active Effects", "zh": "🌀 生效效果"},
    "stats.known":       {"en": "Known:", "zh": "已知:"},
    "stats.prepared":    {"en": "Prepared:", "zh": "已准备:"},
    "stats.spellbook":   {"en": "Spellbook:", "zh": "法术书:"},
    "stats.skills":      {"en": "Skills:", "zh": "技能:"},
    "stats.saves":       {"en": "Saves:", "zh": "豁免:"},
    "stats.armor":       {"en": "Armor:", "zh": "护甲:"},
    "stats.weapons":     {"en": "Weapons:", "zh": "武器:"},
    "stats.tools":       {"en": "Tools:", "zh": "工具:"},
    "stats.features":    {"en": "Features:", "zh": "特性:"},
    "stats.languages":   {"en": "Languages:", "zh": "语言:"},
    "stats.char_line2":  {"en": "⭐ Level {level}  💰 Gold {gold}  ✨ XP {xp}",
                          "zh": "⭐ 等级 {level}  💰 金币 {gold}  ✨ 经验 {xp}"},
    "stats.combat_line1": {"en": "❤️ {hp_cur}/{hp_max} HP  🛡️ AC {ac}  🏃 Speed {speed}",
                           "zh": "❤️ {hp_cur}/{hp_max} HP  🛡️ AC {ac}  🏃 速度 {speed}"},
    "stats.combat_line2": {"en": "⭐ Proficiency +{prof}  🎲 Hit Dice {hd_count}d{hd_size}",
                           "zh": "⭐ 熟练加值 +{prof}  🎲 生命骰 {hd_count}d{hd_size}"},
    "stats.spell_line1": {"en": "🔮 {ability}  📿 DC {dc}  🪄 Attack +{atk}",
                          "zh": "🔮 {ability}  📿 DC {dc}  🪄 攻击 +{atk}"},

    # ── entry scripts (play.py / play_with_*.py) ────────────────────
    "entry.llm_title":    {"en": " Infinity Project: LLM Selection{suffix} ",
                           "zh": " Infinity Project：模型选择{suffix} "},
    "entry.select_model": {"en": "Select a model (number)", "zh": "请选择模型（输入编号）"},
    "entry.select_llm":   {"en": "Select an LLM (number)", "zh": "请选择 LLM（输入编号）"},
    "entry.model_selected": {"en": "Model selected:", "zh": "已选择模型："},
    "entry.invalid_model": {"en": "Invalid selection. Defaulting to first model.",
                            "zh": "无效选择，将默认使用第一个模型。"},
    "entry.apikey_missing": {"en": "{key} environment variable not set.",
                             "zh": "未设置环境变量 {key}。"},
    "entry.apikey_hint": {"en": "Set it with: export {key}=your-api-key",
                          "zh": "请设置：export {key}=你的密钥"},
    "entry.interrupted": {"en": "Interrupted by user. Exiting...",
                          "zh": "已被用户中断，正在退出……"},
    "entry.goodbye":     {"en": "Goodbye.", "zh": "再见。"},
    # play.py (Ollama) only
    "ollama.validating":  {"en": "Validating model availability...",
                           "zh": "正在验证模型可用性……"},
    "ollama.unavailable": {"en": "Model '{model}' is not available in Ollama.",
                           "zh": "模型“{model}”在 Ollama 中不可用。"},
    "ollama.ensure":      {"en": "Please ensure Ollama is running and the model is downloaded.",
                           "zh": "请确认 Ollama 已运行且该模型已下载。"},
    "ollama.validate_fail": {"en": "Could not validate model: {e}",
                             "zh": "无法验证模型：{e}"},
    "ollama.proceed":     {"en": "Proceeding anyway...", "zh": "仍将继续……"},
    "ollama.validated":   {"en": "Model validated:", "zh": "模型验证通过："},
    # play_with_nano.py titles
    "nano.gm_title":  {"en": "Infinity Project: GameMaster Model Selection (Nano Banana)",
                       "zh": "Infinity Project：GM 模型选择（Nano Banana）"},
    "nano.img_title": {"en": "Infinity Project: Image Generation Model Selection (Nano Banana)",
                       "zh": "Infinity Project：图像生成模型选择（Nano Banana）"},
    # play_with_deepseek.py / play_with_kobold.py startup panels
    "deepseek.panel": {"en": "Model: {model}  |  Context: {ctx} tokens  |  api.deepseek.com",
                       "zh": "模型：{model}  |  上下文：{ctx} tokens  |  api.deepseek.com"},
    "kobold.panel":   {"en": "Model: {model}  |  Context: {ctx} tokens ({ctx_label})  |  max_output: {max_out}",
                       "zh": "模型：{model}  |  上下文：{ctx} tokens（{ctx_label}）|  max_output：{max_out}"},
    "kobold.ctx.override": {"en": "override", "zh": "手动指定"},
    "kobold.ctx.backend":  {"en": "from KoboldCpp backend", "zh": "来自 KoboldCpp 后端"},
    "kobold.ctx.fallback": {"en": "backend unreachable, fallback default",
                            "zh": "后端不可达，回退默认"},
}
