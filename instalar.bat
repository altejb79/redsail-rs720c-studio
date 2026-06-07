@echo off
setlocal enabledelayedexpansion
title Redsail RS720C Studio v4 - Instalador
color 0B

echo.
echo  =====================================================
echo    REDSAIL RS720C STUDIO v4.0 - Instalador
echo  =====================================================
echo.

python --version >nul 2>&1
if not errorlevel 1 ( echo  [OK] Python encontrado. & goto :deps )

echo  [..] Descargando Python 3.11...
set PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
set PY_INS=%TEMP%\python_installer.exe
powershell -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INS%' -UseBasicParsing" 2>nul
"%PY_INS%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_tcltk=1
timeout /t 6 /nobreak >nul
for /f "tokens=*" %%i in ('powershell -Command "[System.Environment]::GetEnvironmentVariable(\"PATH\",\"User\")"') do set PATH=%%i;%PATH%
python --version >nul 2>&1
if errorlevel 1 ( echo  [!] Reinicia e intenta de nuevo. & pause & exit /b 1 )
echo  [OK] Python instalado.

:deps
echo.
echo  [..] Instalando dependencias...
python -m pip install --upgrade pip --quiet
python -m pip install pyserial Pillow cairosvg PyMuPDF pyinstaller --quiet
if errorlevel 1 ( echo  [!] Error instalando. Verifica tu conexion. & pause & exit /b 1 )
echo  [OK] Dependencias instaladas.

echo.
echo  [..] Compilando Redsail_RS720C_Studio.exe (2-4 min)...

pyinstaller --onefile --windowed ^
  --name "Redsail_RS720C_Studio" ^
  --hidden-import serial ^
  --hidden-import serial.tools.list_ports ^
  --hidden-import tkinter ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageTk ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import cairosvg ^
  --hidden-import fitz ^
  --hidden-import xml.etree.ElementTree ^
  redsail_rs720c.py

if errorlevel 1 ( echo  [!] Error al compilar. & pause & exit /b 1 )

if exist dist\Redsail_RS720C_Studio.exe (
  copy /y dist\Redsail_RS720C_Studio.exe Redsail_RS720C_Studio.exe >nul
  rmdir /s /q dist 2>nul
)
if exist build   rmdir /s /q build 2>nul
if exist Redsail_RS720C_Studio.spec del /q Redsail_RS720C_Studio.spec 2>nul

echo.
echo  =====================================================
echo   [OK] Redsail_RS720C_Studio.exe listo!
echo        Funciona sin Python ni instalaciones.
echo  =====================================================
echo.
set /p ABRIR= ^> Abrir ahora? (S/N): 
if /i "!ABRIR!"=="S" start "" "Redsail_RS720C_Studio.exe"
pause
