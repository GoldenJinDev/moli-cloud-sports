@echo off
>nul 2>&1 chcp 65001 >nul
setlocal EnableExtensions

rem 茉莉 · 云运动 一键运行：准备本机 Python 虚拟环境、装依赖、启动客户端
rem 本脚本放在 moli 文件夹外面；它创建的一切都落在 moli 文件夹里
rem 可选参数：deps 强制重装依赖；nogui 只装依赖不启动

set "PROJ=%~dp0moli"
if exist "%PROJ%\moli.py" goto :have_proj
set "PROJ=%~dp0"
rem 去掉 %~dp0 末尾的反斜杠：留着会让 if exist 认不出 .venv 里的文件；盘根目录例外
if not "%PROJ:~-2,1%"==":" set "PROJ=%PROJ:~0,-1%"
:have_proj
cd /d "%PROJ%"

set "PYVER=3.11.9"
set "REQ=requirements.txt"
set "APP=moli.py"
set "VENV=%PROJ%\.venv"
set "PYHOME=%PROJ%\python-%PYVER%"
set "READY=%VENV%\.moli-ready"
set "VENVPY=%VENV%\Scripts\python.exe"
set "WORK=%PROJ%\.tmp"
set "IDX1=https://pypi.tuna.tsinghua.edu.cn/simple/"
set "IDX2=https://mirrors.aliyun.com/pypi/simple/"
set "IDX3=https://pypi.org/simple/"
set "MIRROR1=https://mirrors.huaweicloud.com/python/%PYVER%/python-%PYVER%-amd64.exe"
set "MIRROR2=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"

echo ============================================
echo  茉莉 · 云运动  一键运行
echo ============================================
echo  工作目录：%PROJ%
echo.

if not exist "%PROJ%\%APP%" (
    echo 没找到 %APP%，请把本脚本放在 moli 文件夹的上一级。
    pause
    exit /b 1
)

if /i "%~1"=="deps" (
    echo [提示] 已指定 deps，将重装依赖
    del "%READY%" >nul 2>&1
)

if exist "%READY%" goto :launch
if not exist "%VENVPY%" goto :prepare
echo [1/3] 虚拟环境已存在，校验依赖是否完整…
"%VENVPY%" -c "import PyQt6.QtWebEngineWidgets, pydantic, gmssl, geographiclib, requests" >nul 2>&1
if errorlevel 1 (
    echo     依赖有缺失，重新安装
    del "%READY%" >nul 2>&1
    goto :install
)
echo ok> "%READY%"
goto :launch

:prepare
echo [1/3] 查找可用的 Python（需要 3.10 及以上）…
set "PYEXE="
if exist "%PYHOME%\python.exe" (
    set "PYEXE=%PYHOME%\python.exe"
    echo     使用本目录已下载的 Python：python-%PYVER%
    goto :venv
)
call :try py -3.13
if not defined PYEXE call :try py -3.12
if not defined PYEXE call :try py -3.11
if not defined PYEXE call :try py -3
if not defined PYEXE call :try python
if defined PYEXE (
    echo     找到：%PYEXE%
    goto :venv
)
echo     未找到可用的 Python，开始下载官方安装包（约 27MB）…
call :fetchpython
if not defined PYEXE goto :fail_nopython

:venv
echo [2/3] 创建虚拟环境 .venv …
rem 只有确认它是虚拟环境才清理，避免路径异常时误删源码
if not exist "%VENV%" goto :venv_make
if /i "%VENV%"=="%PROJ%" goto :venv_suspicious
if not exist "%VENV%\pyvenv.cfg" goto :venv_suspicious
echo     先清掉旧的虚拟环境
rmdir /s /q "%VENV%"
goto :venv_make

:venv_suspicious
echo     .venv 目录存在但看起来不是虚拟环境，为安全起见不清理它。
echo     请自己确认 %VENV% 之后删除再重试。
pause
exit /b 1

:venv_make
"%PYEXE%" -m venv "%VENV%"
if not exist "%VENVPY%" (
    echo     虚拟环境创建失败：请确认上面的 Python 可用
    pause
    exit /b 1
)
call :installdeps
if errorlevel 1 goto :end_fail

:launch
if /i "%~1"=="nogui" goto :end_ok
echo [3/3] 启动客户端…
echo.
set "PYTHONUTF8=1"
"%VENVPY%" "%APP%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo 客户端异常退出（返回码 %RC%），完整日志见 moli\logs\app.log
    pause
)
goto :end_ok

rem ---------- 子过程 ----------

:try
rem 探测定位符是否指向 3.10+ 的 Python；conda 版会跳过
rem （conda 的 Python 建出的 venv 里 PyQt6 常因缺 Library\bin 的 MSVC 运行库而加载失败）
%* -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) and 'conda' not in sys.version.lower() and 'conda' not in sys.prefix.lower() else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
for /f "delims=" %%P in ('%* -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
exit /b 0

:install
call :installdeps
if errorlevel 1 goto :end_fail
goto :launch

:installdeps
echo [2/3] 安装依赖（依次尝试清华 / 阿里 / 官方源）…
"%VENVPY%" -m pip install --disable-pip-version-check --upgrade --no-warn-script-location pip -i %IDX1% >nul 2>&1
if errorlevel 1 "%VENVPY%" -m pip install --disable-pip-version-check --upgrade --no-warn-script-location pip -i %IDX2% >nul 2>&1
for %%I in (%IDX1% %IDX2% %IDX3%) do (
    echo     正在使用 %%I
    "%VENVPY%" -m pip install --disable-pip-version-check --no-warn-script-location -i %%I -r "%REQ%"
    if not errorlevel 1 goto :verify
    echo     这个源没成功，换下一个…
)
echo     所有源都失败了，请检查网络或稍后重试
exit /b 1

:verify
echo     依赖装好了，校验能否真正加载…
"%VENVPY%" -c "import PyQt6.QtWebEngineWidgets, pydantic, gmssl, geographiclib, requests" >nul 2>&1
if errorlevel 1 (
    echo     加载失败。若这个 Python 来自 Anaconda/Miniconda，删掉 moli\.venv 重试即可，
    echo     本脚本会改用官网 Python 或自动下载到 moli 目录内。
    exit /b 1
)
echo ok> "%READY%"
exit /b 0

:fetchpython
if not exist "%WORK%" mkdir "%WORK%"
set "SETUP=%WORK%\python-%PYVER%-amd64.exe"
call :download "%MIRROR1%" "%SETUP%"
if not exist "%SETUP%" call :download "%MIRROR2%" "%SETUP%"
if not exist "%SETUP%" (
    echo     下载失败：请手动安装 Python %PYVER%（勾选 Add python.exe to PATH）后重新双击本脚本
    exit /b 1
)
echo     静默安装到 moli\python-%PYVER%
echo     （只装在这里，不写 PATH、不注册 py 启动器、不影响系统里的 Python）
start /wait "" "%SETUP%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_dev=0 AssociateFiles=0 Shortcuts=0 TargetDir="%PYHOME%"
del "%SETUP%" >nul 2>&1
if exist "%PYHOME%\python.exe" (
    set "PYEXE=%PYHOME%\python.exe"
    exit /b 0
)
exit /b 1

:download
rem %~1 下载地址  %~2 保存路径
if exist "%~2" del "%~2" >nul 2>&1
where curl >nul 2>&1
if not errorlevel 1 (
    curl -L --fail --retry 2 --connect-timeout 15 -o "%~2" "%~1" >nul 2>&1
    if exist "%~2" exit /b 0
)
bitsadmin /transfer moli /priority foreground "%~1" "%~2" >nul 2>&1
if exist "%~2" exit /b 0
exit /b 1

:fail_nopython
echo.
echo 没有可用的 Python，自动下载也没成功。
echo 请二选一后重新双击：
echo   1^) 自行安装 Python %PYVER%，安装时勾选 "Add python.exe to PATH"
echo   2^) 把 python-%PYVER%-amd64.exe 装到 moli\python-%PYVER% 文件夹里
pause
goto :end_fail

:end_fail
echo.
echo 环境没准备好，本次未启动客户端。
pause
exit /b 1

:end_ok
endlocal
exit /b 0
