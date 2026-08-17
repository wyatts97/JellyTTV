@echo off
echo Building Jellyfin.Plugin.JellyTTV...
dotnet publish -c Release -o bin/publish
if %ERRORLEVEL% neq 0 (
    echo Build failed.
    exit /b 1
)
echo.
echo Build successful. Output in: bin\publish\
echo.
echo To install: copy contents of bin\publish\ to your Jellyfin plugins directory:
echo   %%LOCALAPPDATA%%\jellyfin\plugins\JellyTTV\
echo.
echo Or with Docker, uncomment the volume mount in docker-compose.yml:
echo   - ./jellyfin-plugin-jellyttv/bin/publish:/config/plugins/JellyTTV
