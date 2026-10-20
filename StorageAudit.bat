@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0auditoria_armazenamento_universal.py"
if not exist "%SCRIPT%" (
    echo ERRO: Arquivo auditoria_armazenamento_universal.py nao encontrado.
    echo Coloque este .bat na mesma pasta do script Python.
    echo.
    pause
    exit /b 1
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%SCRIPT%" %*
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERRO: Python nao foi encontrado neste computador.
        echo Instale o Python 3 e tente novamente.
        echo.
        pause
        exit /b 1
    )
    python "%SCRIPT%" %*
)

set "CODIGO=%ERRORLEVEL%"
echo.
if not "%CODIGO%"=="0" echo A execucao terminou com erro (codigo %CODIGO%).
pause
exit /b %CODIGO%
