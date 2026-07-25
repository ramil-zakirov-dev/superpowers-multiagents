@echo off
if "%~1"=="" exit /b 1
bash "%~dp0%~1"
