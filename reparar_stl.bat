@echo off
chcp 65001 >nul
set "PYTHON_EXE=M:\ComfyUI\python_embeded\python.exe"
set "SCRIPT_PY=%~dp0reparar_stl.py"

if "%~1"=="" (
    echo ========================================================================
    echo Utilitario de Reparo STL v2.0 ^(Alta Fidelidade Estrutural^)
    echo Uso:
    echo   .\reparar_stl.bat arquivo.stl [opcoes]
    echo   Ou arraste um arquivo .stl para cima deste icone no Windows Explorer!
    echo.
    echo Exemplos:
    echo   .\reparar_stl.bat ComfyUI_00020.stl --altura 200
    echo   .\reparar_stl.bat all.stl --referencia referencia.stl --escala-ref base
    echo   .\reparar_stl.bat miniatura_rpg.stl --altura 38 --realce 0.65
    echo   .\reparar_stl.bat busto.stl --base preservar --altura 80
    echo ========================================================================
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_PY%" %*
