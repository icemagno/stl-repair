@echo off
chcp 65001 >nul
set "PYTHON_EXE=M:\ComfyUI\python_embeded\python.exe"
set "SCRIPT_PY=%~dp0reparar_stl.py"

if "%~1"=="" (
    echo ========================================================================
    echo Uso:
    echo   reparar_stl.bat arquivo.stl [opcoes]
    echo   Ou arraste um arquivo .stl para cima deste icone no Windows Explorer!
    echo.
    echo Exemplos:
    echo   reparar_stl.bat all.stl --referencia referencia.stl --escala-ref base
    echo   reparar_stl.bat monstro_sem_base.stl --altura 60 --diagnostico
    echo   reparar_stl.bat busto.stl --base preservar --altura 50
    echo ========================================================================
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_PY%" %*
