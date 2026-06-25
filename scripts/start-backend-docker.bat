@echo off
chcp 65001 >nul
REM Start backend + middleware in Docker, with agent images built.
REM Frontend is NOT started; run `npm run dev:frontend` separately.

setlocal enabledelayedexpansion

REM Resolve repo root from script location
set "REPO_ROOT=%~dp0\.."
cd /d "%REPO_ROOT%" || (
    echo Failed to cd to repo root: %REPO_ROOT%
    exit /b 1
)
for /f "tokens=*" %%a in ('cd') do set "REPO_ROOT=%%a"
echo Repo root: %REPO_ROOT%

REM Check docker is available
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running or not in PATH.
    echo Please start Docker Desktop first.
    exit /b 1
)

echo.
echo [1/3] Building agent images ^(fe-agent, be-agent, slicer-agent^)...
docker compose --profile setup build
if errorlevel 1 (
    echo [ERROR] Agent image build failed.
    exit /b 1
)

echo.
echo [2/3] Building backend image...
docker compose build backend
if errorlevel 1 (
    echo [ERROR] Backend image build failed.
    exit /b 1
)

echo.
echo [3/3] Starting backend + middleware in Docker...
docker compose up -d backend postgres redis mongo
if errorlevel 1 (
    echo [ERROR] Failed to start backend services.
    exit /b 1
)

echo.
echo [OK] Backend services started.
echo.
echo Useful commands:
echo   docker compose ps
echo   docker compose logs -f backend
echo   docker compose down
echo.
echo Next step: start frontend locally with:
echo   npm run dev:frontend
echo Then open http://localhost:3000
