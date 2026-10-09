@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not defined PYTHON_BIN set "PYTHON_BIN=py -3"
if not defined VENV_DIR set "VENV_DIR=.venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    %PYTHON_BIN% -m venv "%VENV_DIR%"
    if errorlevel 1 exit /b 1
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

if not exist data mkdir data
if not exist logs mkdir logs
if not exist screenshots mkdir screenshots

"%VENV_PY%" -m playwright install chromium
if errorlevel 1 exit /b 1
"%VENV_PY%" -m scripts.seed
if errorlevel 1 exit /b 1

if exist frontend\package.json (
    call npm --prefix frontend install
    if errorlevel 1 exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$py=(Resolve-Path '%VENV_PY%').Path;" ^
  "$root=(Get-Location).Path;" ^
  "$specs=@(" ^
  "  @{Name='AutoHeal Sandbox'; File=$py; Args='-m uvicorn sandbox_app.main:app --host 127.0.0.1 --port 8001'; Work=$root}," ^
  "  @{Name='AutoHeal Backend'; File=$py; Args='-m uvicorn backend.api:app --host 127.0.0.1 --port 8000'; Work=$root}," ^
  "  @{Name='AutoHeal Portal'; File=$py; Args='-m uvicorn itsm_portal.app:app --host 127.0.0.1 --port 8002'; Work=$root}," ^
  "  @{Name='AutoHeal RPA Worker'; File=$py; Args='-m rpa.worker'; Work=$root}" ^
  ");" ^
  "if (Test-Path 'frontend/package.json') {" ^
  "  $specs += @{Name='AutoHeal Frontend'; File='npm.cmd'; Args='--prefix frontend run dev -- --host 127.0.0.1 --port 5173'; Work=$root}" ^
  "};" ^
  "$children=@();" ^
  "foreach ($s in $specs) {" ^
  "  $p=Start-Process -FilePath $s.File -ArgumentList $s.Args -WorkingDirectory $s.Work -PassThru -WindowStyle Hidden;" ^
  "  $children += $p;" ^
  "  Write-Host ('Started {0} (pid {1})' -f $s.Name,$p.Id)" ^
  "};" ^
  "Write-Host 'AutoHeal SANDBOX is running. Press Ctrl+C to stop all started processes.';" ^
  "try { while ($true) { Start-Sleep -Seconds 1 } }" ^
  "finally { foreach ($p in $children) { if (!$p.HasExited) { taskkill.exe /PID $p.Id /T /F | Out-Null } } }"

endlocal