@echo off
REM ====================================================================
REM Aste Giudiziarie - Avvio server Flask + apertura dashboard
REM
REM Riusa il .venv del progetto "trade.fondamentale" (sibling folder)
REM per evitare di duplicare playwright/flask/etc.
REM ====================================================================
echo Aste Giudiziarie - Avvio server...
cd /d "%~dp0"

set VENV_PY=..\trade.fondamentale\.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [ERRORE] Python venv non trovato in: %VENV_PY%
    echo Crea un .venv dedicato qui oppure ripristina trade.fondamentale\.venv
    pause
    exit /b 1
)

echo Avvio server Flask su http://127.0.0.1:5000 ...
echo Apri dashboard.html nel browser per usare l'interfaccia.
echo.
"%VENV_PY%" server.py
pause
