@echo off
cd /d "%~dp0"
python -m cpd_lwc_qc.viewer --cache cache
pause
