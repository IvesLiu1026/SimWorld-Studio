@echo off
REM SimWorld Studio - Windows Launch Script
REM Launches: Cirrus (Pixel Streaming) + UE Editor + Web Server
REM Usage: SimWorld-Studio.bat [--port PORT] [--mcp-port PORT] [--gpu INDEX] ...

setlocal EnableDelayedExpansion

REM ============================================================
REM  USER CONFIGURATION - Edit these paths for your environment
REM ============================================================

REM Path to UE installation root (containing Engine\Binaries\Win64\UnrealEditor.exe)
set "UE_ROOT=C:\Program Files\Epic Games\UE_5.3"

REM Path to UE project file (.uproject)
set "UE_PROJECT=E:\UE\SimWorld\SimWorld.uproject"

REM Workspace directory (where web/, logs/, tmp/ live)
set "WORKSPACE=%~dp0simworld_studio_workspace"

REM ============================================================
REM  DEFAULT SETTINGS (override via command-line args)
REM ============================================================
set "WEB_PORT=3002"
set "MCP_PORT=55557"
set "CIRRUS_HTTP_PORT=8685"
set "CIRRUS_WS_PORT=8686"
set "CIRRUS_SFU_PORT=8989"
set "GPU_INDEX=0"
set "UE_MAP=/Game/Maps/Empty"
set "RENDER_OFFSCREEN="

REM ============================================================
REM  PARSE ARGUMENTS
REM ============================================================
:parse_args
if "%~1"=="" goto :done_args

if "%~1"=="--port" (
    set "WEB_PORT=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--mcp-port" (
    set "MCP_PORT=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--gpu" (
    set "GPU_INDEX=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--cirrus-http-port" (
    set "CIRRUS_HTTP_PORT=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--cirrus-ws-port" (
    set "CIRRUS_WS_PORT=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--cirrus-sfu-port" (
    set "CIRRUS_SFU_PORT=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--map" (
    set "UE_MAP=%~2"
    shift & shift
    goto :parse_args
)
if "%~1"=="--render-offscreen" (
    set "RENDER_OFFSCREEN=-RenderOffScreen"
    shift
    goto :parse_args
)
if "%~1"=="--help" goto :show_help
if "%~1"=="-h" goto :show_help

echo Unknown option: %~1 (use --help for usage)
exit /b 1

:show_help
echo SimWorld Studio Launcher (Windows)
echo.
echo Usage: %~nx0 [OPTIONS]
echo.
echo Options:
echo   --port PORT               Web UI port (default: 3002)
echo   --mcp-port PORT           UE MCP port (default: 55560)
echo   --gpu INDEX               GPU index (default: 0)
echo   --cirrus-http-port PORT   Cirrus HTTP port (default: 8685)
echo   --cirrus-ws-port PORT     Cirrus WebSocket port (default: 8686)
echo   --cirrus-sfu-port PORT    Cirrus SFU port (default: 8989)
echo   --map MAP_PATH            UE map to open (default: /Game/Maps/Empty)
echo   --render-offscreen        Run UE without display window
echo   --help                    Show this help
echo.
echo Configuration:
echo   Edit UE_ROOT, UE_PROJECT, and WORKSPACE at the top of this script.
exit /b 0

:done_args

REM ============================================================
REM  VALIDATE
REM ============================================================
set "UE_EDITOR=%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor.exe"

if not exist "%UE_EDITOR%" (
    echo [!!] UnrealEditor.exe not found at:
    echo      %UE_EDITOR%
    echo.
    echo      Edit UE_ROOT in %~nx0
    exit /b 1
)

if not exist "%UE_PROJECT%" (
    echo [!!] Project file not found at:
    echo      %UE_PROJECT%
    echo.
    echo      Edit UE_PROJECT in %~nx0
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [!!] Node.js not found. Please install Node.js first.
    exit /b 1
)

REM Check npm dependencies
if not exist "%WORKSPACE%\web\node_modules" (
    echo [INFO] Installing npm dependencies...
    pushd "%WORKSPACE%\web"
    call npm install --no-audit --no-fund
    popd
)

REM Create directories
if not exist "%WORKSPACE%\logs" mkdir "%WORKSPACE%\logs"
if not exist "%WORKSPACE%\tmp\screens" mkdir "%WORKSPACE%\tmp\screens"

REM ============================================================
REM  GENERATE MCP CONFIG
REM ============================================================
set "MCP_SERVER_JS=%WORKSPACE%\web\server\mcp-server.js"
set "MCP_CONFIG=%WORKSPACE%\web\mcp.json"

echo {"mcpServers":{"simworld":{"command":"node","args":["%MCP_SERVER_JS:\=\\%"],"env":{"UNREAL_HOST":"127.0.0.1","UNREAL_PORT":"%MCP_PORT%"}}}} > "%MCP_CONFIG%"

echo.
echo =====================================================
echo   SimWorld Studio (Windows)
echo =====================================================
echo.
echo   [OK] UE Editor : %UE_EDITOR%
echo   [OK] Project   : %UE_PROJECT%
echo   [OK] Workspace : %WORKSPACE%
echo.

REM ============================================================
REM  STEP 1: START CIRRUS (Pixel Streaming signaling server)
REM ============================================================
set "CIRRUS_DIR=%UE_ROOT%\Engine\Plugins\Media\PixelStreaming\Resources\WebServers\SignallingWebServer"
set "CIRRUS_JS=%CIRRUS_DIR%\cirrus.js"

if exist "%CIRRUS_JS%" (
    REM Generate cirrus config
    set "CIRRUS_CONFIG=%WORKSPACE%\cirrus-config.json"
    echo {"UseFrontend":false,"UseMatchmaker":false,"HttpPort":%CIRRUS_HTTP_PORT%,"StreamerPort":%CIRRUS_WS_PORT%,"SFUPort":%CIRRUS_SFU_PORT%} > "!CIRRUS_CONFIG!"

    echo   Starting Cirrus signaling server...
    start "Cirrus" /min cmd /c "cd /d !CIRRUS_DIR! && node cirrus.js --configFile=!CIRRUS_CONFIG! > !WORKSPACE!\logs\cirrus.log 2>&1"
    ping -n 3 127.0.0.1 >nul
    echo   [OK] Cirrus ^(HTTP :%CIRRUS_HTTP_PORT%, WS :%CIRRUS_WS_PORT%^)
) else (
    echo   [!!] Cirrus not found at %CIRRUS_DIR%
    echo        Pixel Streaming may not work.
)

REM ============================================================
REM  STEP 2: LAUNCH UE EDITOR
REM ============================================================
echo   Launching Unreal Engine...

start "UnrealEditor" "%UE_EDITOR%" "%UE_PROJECT%" ^
    %UE_MAP% ^
    -MCPPort=%MCP_PORT% ^
    -Unattended -NOSPLASH -NOSOUND -Messaging ^
    -ResX=1280 -ResY=720 ^
    -FPSMAX=15 ^
    -graphicsadapter=%GPU_INDEX% ^
    %RENDER_OFFSCREEN% ^
    -EditorPixelStreamingRes=1280x720 ^
    -EditorPixelStreamingStartOnLaunch=true ^
    -EditorPixelStreamingUseRemoteSignallingServer=true ^
    -PixelStreamingURL=ws://127.0.0.1:%CIRRUS_WS_PORT% ^
    -log

echo   [OK] UE Editor launched (GPU: %GPU_INDEX%, MCP: %MCP_PORT%)

REM ============================================================
REM  STEP 3: WAIT FOR MCP PORT
REM ============================================================
echo   Waiting for MCP port %MCP_PORT%...

set "WAIT_COUNT=0"
:wait_mcp
set /a WAIT_COUNT+=1
if %WAIT_COUNT% gtr 60 (
    echo.
    echo   [!!] Timeout waiting for MCP port %MCP_PORT%.
    echo        UE may have crashed. Check logs\ue.log
    exit /b 1
)

powershell -Command "try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', %MCP_PORT%); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    ping -n 3 127.0.0.1 >nul
    <nul set /p="."
    goto :wait_mcp
)
echo.
echo   [OK] MCP port %MCP_PORT% ready!

REM ============================================================
REM  STEP 4: START WEB SERVER
REM ============================================================
echo   Starting web server on port %WEB_PORT%...

set "PORT=%WEB_PORT%"
set "UNREAL_HOST=127.0.0.1"
set "UNREAL_PORT=%MCP_PORT%"
set "PIXEL_STREAMING_URL=http://127.0.0.1:%CIRRUS_HTTP_PORT%"

set "SERVER_ENTRY=%WORKSPACE%\web\server\index.js"

start "SimWorld-Web" /min cmd /c "cd /d %WORKSPACE%\web && node server\index.js"

ping -n 3 127.0.0.1 >nul

echo   [OK] Web server started

REM ============================================================
REM  DONE
REM ============================================================
echo.
echo =====================================================
echo   SimWorld Studio is running!
echo.
echo   Open: http://localhost:%WEB_PORT%
echo.
echo   GPU: %GPU_INDEX%  ^|  MCP: %MCP_PORT%  ^|  Web: %WEB_PORT%  ^|  Cirrus: HTTP:%CIRRUS_HTTP_PORT% WS:%CIRRUS_WS_PORT%
echo =====================================================
echo.
echo   Try: "Set up a sunset scene with 4 houses and trees"
echo.
echo   To stop: close this window and the UE/Cirrus/Web windows,
echo   or use Task Manager to end UnrealEditor.exe / node.exe
echo.
pause
