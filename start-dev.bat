@echo off
rem ============================================================
rem aftersale-agent 一键启动（Windows）
rem 用法：
rem   start-dev.bat          首次：自动建 .env、起服务
rem   start-dev.bat rebuild  强制重建镜像
rem   start-dev.bat stop     停止服务
rem
rem 职责（开箱即用三件事）：
rem   1. .env 不存在 -> 从 .env.example 复制，自动生成随机 JWT_SECRET
rem   2. 校验 QWEN key 是否已填（缺失则提示）
rem   3. docker compose up -d --build 一键起 Redis + API（热重载）
rem ============================================================
setlocal
cd /d %~dp0

rem ---------- 0. 检查 Docker ----------
where docker >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未检测到 Docker。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/
  pause
  exit /b 1
)
docker info >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Docker 守护进程未运行。请先启动 Docker Desktop 再重试。
  pause
  exit /b 1
)
echo [OK] Docker 就绪

rem ---------- 1. 准备 .env ----------
if not exist .env (
  echo 首次运行：正在生成 .env ...
  if exist .env.example copy .env.example .env >nul
  rem 生成随机 JWT 密钥（用 PowerShell 生成 48 字符随机串）
  for /f %%i in ('powershell -NoProfile -Command "$chars='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'; -join ((1..48) | ForEach-Object { Get-Random -Maximum $chars.Length | ForEach-Object { $chars[$_] } })"') do set "SECRET=%%i"
  echo AFTERSALE_JWT_SECRET=%SECRET%>> .env
  echo [OK] .env 已生成（JWT_SECRET 已随机化）
) else (
  echo [OK] .env 已存在
)

rem ---------- 2. 校验 QWEN key ----------
findstr /R /C:"^QWEN=." .env >nul 2>nul
if errorlevel 1 (
  echo [WARN] QWEN key 未配置。请编辑 .env 填入：
  echo        QWEN=你的DashScope_API_Key
)

rem ---------- 3. 起服务 ----------
if "%1"=="stop" (
  docker compose down
  echo [OK] 服务已停止
  goto :end
)
if "%1"=="rebuild" (
  docker compose up -d --build
  goto :wait
)
docker compose up -d --build

:wait
echo 等待 API 就绪...
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){try{$r=Invoke-WebRequest -Uri http://127.0.0.1:8000/ -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -eq 200){Write-Host ''; exit 0}}catch{}; Write-Host -NoNewline '.'; Start-Sleep -Seconds 2}"
if errorlevel 1 (
  echo [WARN] 服务未在 60s 内就绪，请查看日志：docker compose logs -f api
)

echo.
echo ==============================================
echo   [Frontend] http://127.0.0.1:8000
echo   [Account ] admin / admin123
echo   [Hot reload] 修改源码自动生效
echo   [Logs    ] docker compose logs -f api
echo   [Stop    ] start-dev.bat stop
echo ==============================================

:end
pause