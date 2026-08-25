@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ===========================================================================
REM  Project Infinity — Portable (Embedded) Python Setup
REM  参照 ComfyUI Portable 思路：把独立 Python 与依赖装进 python_embeded/
REM  下载优先走国内镜像（清华），镜像不可达时自动回退官方源。
REM ===========================================================================

set "ROOT=%~dp0"
set "PYDIR=%ROOT%python_embeded"
set "PYEXE=%PYDIR%\python.exe"
set "PTH=%PYDIR%\python311._pth"

REM --- 镜像 / 官方源配置（集中管理，便于更换） ---------------------------------
REM 注意：清华镜像的 CPython 二进制仓库名为 python（不是 python-release）
set "PY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/python"
set "PY_MIRROR2=https://mirrors.huaweicloud.com/python"
set "PY_OFFICIAL=https://www.python.org/ftp/python"
set "GETPIP_MIRROR=https://mirrors.aliyun.com/pypi/get-pip.py"
set "GETPIP_OFFICIAL=https://bootstrap.pypa.io/get-pip.py"
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_OFFICIAL=https://pypi.org/simple"

set "PYVER=3.11.9"
set "ZIP_NAME=python-%PYVER%-embed-amd64.zip"
set "TMP_ZIP=%ROOT%%ZIP_NAME%"
set "REQ=%ROOT%requirements.txt"

echo.
echo ============================================================
echo  Project Infinity — Portable Python 环境搭建
echo ============================================================
echo.

REM --- 已存在则跳过下载/安装 ---------------------------------------------------
if exist "%PYEXE%" (
    echo [OK] 检测到 %PYEXE% 已存在，环境就绪，跳过下载与安装。
    echo      如需强制重建，请先删除 python_embeded\ 目录后重跑本脚本。
    goto :done
)

REM --- 1. 下载 embedded Python zip（多镜像依次尝试 + 校验为真实 zip） ----------
echo [1/4] 下载 Python %PYVER% embedded (amd64) ...
set "DL_OK=0"
REM 依次尝试的源列表（镜像优先，官方最后）
set "SRC1=%PY_MIRROR%/%PYVER%/%ZIP_NAME%"
set "SRC2=%PY_MIRROR2%/%PYVER%/%ZIP_NAME%"
set "SRC3=%PY_OFFICIAL%/%PYVER%/%ZIP_NAME%"

if "%DL_OK%"=="0" call :try_download "%SRC1%" "清华镜像"
if "%DL_OK%"=="0" call :try_download "%SRC2%" "华为云镜像"
if "%DL_OK%"=="0" call :try_download "%SRC3%" "官方源"
if "%DL_OK%"=="0" goto :download_fail
echo       [OK] 已下载到 %TMP_ZIP%
goto :after_download

REM --- 下载尝试函数：用 certutil 下载（Win 内置，无需 PowerShell 引号地狱）------
REM     下载后校验文件头是否为 PK（真实 zip），否则删除重试
:try_download
set "URL=%~1"
set "LABEL=%~2"
echo       [尝试] %LABEL%: %URL%
if exist "%TMP_ZIP%" del /q "%TMP_ZIP%"
certutil -urlcache -split -f "%URL%" "%TMP_ZIP%"
if errorlevel 1 (
    echo       [诊断] certutil 退出码=%errorlevel% （非 0 表示网络/证书/代理不通）
)
call :is_zip "%TMP_ZIP%"
if "%IS_ZIP%"=="1" (
    echo       [成功] %LABEL% 校验通过（真实 zip）
    set "DL_OK=1"
) else (
    echo       [失败] %LABEL% 下载或校验未通过，尝试下一个源...
    if exist "%TMP_ZIP%" del /q "%TMP_ZIP%"
    set "DL_OK=0"
)
exit /b 0

REM --- 校验文件是否为真实 zip（前 2 字节 PK）。结果写入 IS_ZIP (0/1) -----------
:is_zip
set "IS_ZIP=0"
if not exist "%~1" exit /b 0
powershell -NoProfile -Command "$f='%~1'; $ok=$false; try{$b=[System.IO.File]::ReadAllBytes($f); if($b.Length -ge 2 -and $b[0]-eq 80 -and $b[1]-eq 75){$ok=$true}}catch{}; if($ok){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 set "IS_ZIP=1"
exit /b 0

:after_download

REM --- 2. 解压（用 PowerShell Expand-Archive，路径含空格也安全） -----------------
echo [2/4] 解压到 %PYDIR% ...
if not exist "%PYDIR%" mkdir "%PYDIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%TMP_ZIP%' -DestinationPath '%PYDIR%' -Force" >nul 2>&1
if not exist "%PYEXE%" (
    echo [ERROR] 解压失败，未找到 %PYEXE%
    goto :download_fail
)
del /q "%TMP_ZIP%" 2>nul

REM --- 3. 启用 import site（修改 python311._pth） ------------------------------
echo [3/4] 启用 import site ...
if not exist "%PTH%" (
    echo [ERROR] 未找到 %PTH%
    goto :download_fail
)
REM 去掉 #import site 前的注释符，并追加 Lib\site-packages
powershell -NoProfile -Command "$p='%PTH%'; $t=Get-Content $p -Raw; $t=$t -replace '#import site','import site'; if($t -notmatch 'Lib\\site-packages'){ $t=$t.TrimEnd()+[Environment]::NewLine+'Lib\site-packages'+[Environment]::NewLine }; Set-Content $p $t -NoNewline"
echo       [OK] 已修改 %PTH%

REM --- 4. 引导 pip + 安装依赖（镜像优先 -> 官方回退） --------------------------
echo [4/4] 引导 pip 并安装依赖（国内镜像优先）...
set "GETPIP=%ROOT%get-pip.py"
set "GURL=%GETPIP_MIRROR%"
if exist "%GETPIP%" del /q "%GETPIP%"
certutil -urlcache -split -f "%GURL%" "%GETPIP%" >nul 2>&1
if not exist "%GETPIP%" (
    echo       [镜像不可达] 回退官方 get-pip 源...
    set "GURL=%GETPIP_OFFICIAL%"
    certutil -urlcache -split -f "%GURL%" "%GETPIP%" >nul 2>&1
)
if not exist "%GETPIP%" (
    echo [ERROR] 无法下载 get-pip.py，请检查网络后重试。
    goto :download_fail
)
"%PYEXE%" "%GETPIP%" --no-warn-script-location >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip 引导失败。
    goto :download_fail
)
del /q "%GETPIP%" 2>nul

REM 安装依赖：优先清华 PyPI 镜像
"%PYEXE%" -m pip install -r "%REQ%" --no-cache-dir -i "%PIP_MIRROR%"
if errorlevel 1 (
    echo       [镜像不可达/部分失败] 回退官方 PyPI 源重试...
    "%PYEXE%" -m pip install -r "%REQ%" --no-cache-dir -i "%PIP_OFFICIAL%"
)
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查网络后重试。
    goto :download_fail
)

echo.
echo [OK] 全部完成！Python %PYVER% 与依赖已装入 python_embeded\
goto :done

:download_fail
echo.
echo ============================================================
echo  [失败] 自动下载/安装未完成。
echo  可能原因：当前环境无外网访问，或镜像/官方源均不可达。
echo.
echo  手动恢复步骤 (Manual recovery):
echo    [1] Download zip via browser:
echo        %PY_OFFICIAL%/%PYVER%/%ZIP_NAME%
echo        or %PY_MIRROR%/%PYVER%/%ZIP_NAME%
echo    [2] Extract the zip into the python_embeded\ folder of this directory
echo    [3] Open python_embeded\python311._pth with Notepad
echo        remove the # before "#import site", and add a new line: Lib\site-packages
echo    [4] Download %GETPIP_OFFICIAL% and save as get-pip.py
echo    [5] Open a terminal inside python_embeded\ and run:
echo        python.exe get-pip.py
echo        python.exe -m pip install -r ..\requirements.txt -i %PIP_MIRROR%
echo    [6] Re-run setup.bat (it will skip once python.exe is detected)
echo ============================================================
exit /b 1

:done
echo.
echo 接下来请运行 launch.bat 选择要启动的后端。
endlocal
