@echo off
chcp 65001 >nul
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CDP_LWC_Panel_Start.bat"
if exist "%TARGET%" (
  del "%TARGET%"
  echo 已取消開機自動啟動。正在執行的面板不受影響。
) else (
  echo 目前沒有設定開機自動啟動。
)
pause
