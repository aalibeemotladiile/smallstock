@echo off
rem Cattle & Small Stock System - Windows launcher.
rem Double-click this file. Keep this window open while you work;
rem closing it stops the app.
title Cattle and Small Stock System
cd /d "%~dp0"

set PYTHON=
where py >nul 2>nul && set PYTHON=py -3
if not defined PYTHON where python >nul 2>nul && set PYTHON=python

if not defined PYTHON (
  echo.
  echo Python is not installed, or not on the PATH.
  echo Install Python 3.9 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

%PYTHON% run_desktop.py %*
if errorlevel 1 (
  echo.
  echo The app stopped with an error. The lines above say why.
  pause
)
