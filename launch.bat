@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ===========================================================================
REM  Project Infinity — 启动菜单（Portable Python）
REM  先确保嵌入式环境就绪，再按数字选择后端启动。
REM ===========================================================================

set "ROOT=%~dp0"
set "PYEXE=%ROOT%python_embeded\python.exe"

echo.
echo ============================================================
echo  Project Infinity 启动器
echo ============================================================
echo.

REM --- 确保环境就绪（setup.bat 幂等） ------------------------------------------
if not exist "%PYEXE%" (
    echo [信息] 未检测到嵌入式 Python，先运行 setup.bat 搭建环境...
    call "%ROOT%setup.bat"
    if not exist "%PYEXE%" (
        echo [错误] 环境搭建未完成，无法启动。请检查网络或按 setup.bat 提示手动安装。
        pause
        exit /b 1
    )
)

:menu
cls
echo ============================================================
echo  Project Infinity — 选择启动项
echo ============================================================
echo   1) 生成世界 (main.py / World Forge)
echo   2) Ollama        (play.py, 本地模型)
echo   3) KoboldCpp     (play_with_kobold.py, 本地/局域网)
echo   4) DeepSeek      (play_with_deepseek.py, 需 DEEPSEEK_API_KEY)
echo   5) OpenAI        (play_with_gpt.py, 需 OPENAI_API_KEY)
echo   6) Gemini        (play_with_gemini.py, 需 GEMINI_API_KEY)
echo   7) Claude        (play_with_claude.py, 需 ANTHROPIC_API_KEY)
echo   L) 语言 / Language
echo   Q) 退出
echo ============================================================
set "CHOICE="
set /p CHOICE=请输入序号 [1-7 / L / Q]：

if /i "%CHOICE%"=="Q" exit /b 0
if /i "%CHOICE%"=="L" goto :lang
if "%CHOICE%"=="1" goto :forge
if "%CHOICE%"=="2" goto :ollama
if "%CHOICE%"=="3" goto :kobold
if "%CHOICE%"=="4" goto :deepseek
if "%CHOICE%"=="5" goto :openai
if "%CHOICE%"=="6" goto :gemini
if "%CHOICE%"=="7" goto :claude
echo 无效输入，请重试。
pause
goto :menu

REM --- 各后端启动 -------------------------------------------------------------
:forge
"%PYEXE%" "%ROOT%main.py"
goto :end

:ollama
"%PYEXE%" "%ROOT%play.py"
goto :end

:kobold
echo.
echo KoboldCpp 地址：仅输入主机或 IP 即可（默认 localhost）
echo 将自动拼接为 http://HOST:5001/v1
set "KOIP="
set /p KOIP=KoboldCpp 主机 [默认 localhost]：
if "!KOIP!"=="" set "KOIP=localhost"
REM If user pasted a full URL (contains ://), use it as-is; otherwise append :5001/v1.
echo !KOIP! | findstr "://" >nul && set "KOURL=!KOIP!" || set "KOURL=http://!KOIP!:5001/v1"
echo 使用端点：!KOURL!
REM Optional extra args (--model / --temperature). Leave empty for defaults.
set "EXTRA="
set /p EXTRA=额外参数（可空）：
"%PYEXE%" "%ROOT%play_with_kobold.py" --base-url "!KOURL!" !EXTRA!
goto :end

REM --- 云后端统一 Key 检查 -----------------------------------------------------
:deepseek
if "%DEEPSEEK_API_KEY%"=="" (
    echo [提示] 未检测到 DEEPSEEK_API_KEY 环境变量。
    echo        请在系统/用户环境变量中设置后重跑，或在此临时输入：
    set "K="
    set /p K=DEEPSEEK_API_KEY=：
    if not "!K!"=="" set "DEEPSEEK_API_KEY=!K!"
)
if "!DEEPSEEK_API_KEY!"=="" (
    echo [错误] 缺少 DEEPSEEK_API_KEY，无法启动 DeepSeek 后端。
    pause
    goto :menu
)
"%PYEXE%" "%ROOT%play_with_deepseek.py"
goto :end

:openai
if "%OPENAI_API_KEY%"=="" (
    echo [提示] 未检测到 OPENAI_API_KEY 环境变量。
    echo        请在系统/用户环境变量中设置后重跑，或在此临时输入：
    set "K="
    set /p K=OPENAI_API_KEY=：
    if not "!K!"=="" set "OPENAI_API_KEY=!K!"
)
if "!OPENAI_API_KEY!"=="" (
    echo [错误] 缺少 OPENAI_API_KEY，无法启动 OpenAI 后端。
    pause
    goto :menu
)
"%PYEXE%" "%ROOT%play_with_gpt.py"
goto :end

:gemini
if "%GEMINI_API_KEY%"=="" (
    echo [提示] 未检测到 GEMINI_API_KEY 环境变量。
    echo        请在系统/用户环境变量中设置后重跑，或在此临时输入：
    set "K="
    set /p K=GEMINI_API_KEY=：
    if not "!K!"=="" set "GEMINI_API_KEY=!K!"
)
if "!GEMINI_API_KEY!"=="" (
    echo [错误] 缺少 GEMINI_API_KEY，无法启动 Gemini 后端。
    pause
    goto :menu
)
"%PYEXE%" "%ROOT%play_with_gemini.py"
goto :end

:claude
if "%ANTHROPIC_API_KEY%"=="" (
    echo [提示] 未检测到 ANTHROPIC_API_KEY 环境变量。
    echo        请在系统/用户环境变量中设置后重跑，或在此临时输入：
    set "K="
    set /p K=ANTHROPIC_API_KEY=：
    if not "!K!"=="" set "ANTHROPIC_API_KEY=!K!"
)
if "!ANTHROPIC_API_KEY!"=="" (
    echo [错误] 缺少 ANTHROPIC_API_KEY，无法启动 Claude 后端。
    pause
    goto :menu
)
"%PYEXE%" "%ROOT%play_with_claude.py"
goto :end

REM --- 语言切换（写入 config/settings.yml，游戏启动时读取） --------------------
:lang
echo.
echo   1) English
echo   2) 中文 (Chinese)
set "LC="
set /p LC=选择语言 / Select language [1-2]：
if "%LC%"=="2" "%PYEXE%" "%ROOT%set_language.py" zh
if "%LC%"=="1" "%PYEXE%" "%ROOT%set_language.py" en
pause
goto :menu

:end
echo.
echo 程序已退出。按任意键返回菜单（或直接关闭窗口）。
pause >nul
goto :menu
