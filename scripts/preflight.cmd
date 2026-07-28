@echo off
setlocal ENABLEDELAYEDEXPANSION

set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%" >nul

set "BASH_EXE="

for %%P in (
    "%ProgramFiles%\Git\bin\bash.exe"
    "%ProgramFiles%\Git\usr\bin\bash.exe"
    "%ProgramFiles%\Git\bin\sh.exe"
    "%ProgramFiles%\Git\usr\bin\sh.exe"
    "%ProgramFiles(x86)%\Git\bin\bash.exe"
    "%ProgramFiles(x86)%\Git\usr\bin\bash.exe"
    "%ProgramFiles(x86)%\Git\bin\sh.exe"
    "%ProgramFiles(x86)%\Git\usr\bin\sh.exe"
) do (
    if exist %%~P (
        set "BASH_EXE=%%~P"
        goto :run
    )
)

where bash >nul 2>nul
if not errorlevel 1 (
    set "BASH_EXE=bash"
    goto :run
)

where sh >nul 2>nul
if not errorlevel 1 (
    set "BASH_EXE=sh"
    goto :run
)

echo [preflight] Unable to find bash/sh.
echo [preflight] Install Git for Windows, then rerun this command.
popd >nul
exit /b 2

:run
set "EXTRA_ARG="
if /I "%~1"=="--all" set "EXTRA_ARG=--all"

"%BASH_EXE%" scripts/preflight.sh %EXTRA_ARG%
set "RC=%ERRORLEVEL%"

popd >nul
exit /b %RC%
