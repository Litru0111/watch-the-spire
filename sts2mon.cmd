@echo off
REM Watch the Spire —— 杀戮尖塔2 数据监控
REM 用法:  sts2mon.cmd [doctor^|extract^|ingest^|serve^|watch^|demo^|stats] [参数...]
REM
REM 两个注意点（都踩过）：
REM   1. 必须用 `-m sts2mon` 而不是直接跑 __main__.py，
REM      否则包内相对导入失效（attempted relative import with no known parent package）
REM   2. 必须把**项目根**放进 PYTHONPATH，否则从别的目录调用时找不到包；
REM      这里不改工作目录，用户跑 `ingest`/`serve` 时相对路径仍然按自己的位置算
REM
REM 优先用 Windows 的 py 启动器（能自动选到 3.x），退回 PATH 里的 python。
setlocal
set PYTHONIOENCODING=utf-8
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -X utf8 -m sts2mon %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -X utf8 -m sts2mon %*
  exit /b %errorlevel%
)

echo [x] 找不到 Python。请安装 Python 3.10+ 并确保 py 或 python 在 PATH 中。
echo     下载: https://www.python.org/downloads/
exit /b 1
