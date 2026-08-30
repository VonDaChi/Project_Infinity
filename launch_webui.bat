@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ===========================================================================
REM  Project Infinity - WebUI 启动器
REM  浏览器界面，局域网可访问。端口与 PIN 在 config/webui.yml 里调整。
REM ===========================================================================

set "ROOT=%~dp0"
set "PYEXE=%ROOT%python_embeded\python.exe"

echo.
echo ============================================================
echo  Project Infinity - WebUI
echo ============================================================
echo.

REM --- 确保嵌入式环境就绪（setup.bat 幂等） --------------------------------
if not exist "%PYEXE%" (
    echo [信息] 未检测到嵌入式 Python，先运行 setup.bat 搭建环境...
    call "%ROOT%setup.bat"
    if not exist "%PYEXE%" (
        echo [错误] 环境搭建未完成，无法启动。请检查网络或按 setup.bat 提示手动安装。
        pause
        exit /b 1
    )
)

cd /d "%ROOT%"
REM 内嵌 Python 为隔离模式，cwd 不在 sys.path，故用脚本式入口而非 -m webui
"%PYEXE%" webui\__main__.py

echo.
echo 服务已停止。
pause
