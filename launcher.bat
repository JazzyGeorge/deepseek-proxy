@echo off
REM Launcher for thinking_proxy.py — reads secrets from env file before starting.
REM Used by NSSM Windows Service.
REM
REM Looks for the env file at %USERPROFILE%\Secrets\Anthropic_DeepSeek_env or
REM %USERPROFILE%\Secrets\Anthropic_DeepSeek.env (both are tried).

REM Source the user's env file if it exists
if exist "%USERPROFILE%\Secrets\Anthropic_DeepSeek_env" (
    set "ENV_FILE=%USERPROFILE%\Secrets\Anthropic_DeepSeek_env"
) else if exist "%USERPROFILE%\Secrets\Anthropic_DeepSeek.env" (
    set "ENV_FILE=%USERPROFILE%\Secrets\Anthropic_DeepSeek.env"
)

if defined ENV_FILE (
    for /f "usebackq tokens=1,* delims== " %%a in ("%ENV_FILE%") do (
        if "%%a"=="export" (
            set "%%b"
        ) else if "%%a"=="set" (
            set "%%b"
        ) else if "%%a"=="$env:" (
            set "%%b"
        )
    )
)

REM Also read ANTHROPIC_AUTH_TOKEN directly if not already set
if not defined ANTHROPIC_AUTH_TOKEN (
    for /f "usebackq tokens=2 delims==" %%a in (`findstr /c:"ANTHROPIC_AUTH_TOKEN" "%ENV_FILE%" 2^>nul`) do (
        set "ANTHROPIC_AUTH_TOKEN=%%~a"
    )
)

"%~dp0venv\Scripts\python.exe" "%~dp0thinking_proxy.py"
