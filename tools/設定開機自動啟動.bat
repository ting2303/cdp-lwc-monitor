@echo off
chcp 65001 >nul
rem 在目前使用者的「啟動」資料夾放一個小檔案,登入後自動開面板。不需要系統管理員權限。
set "EXE=%~dp0CDP_LWC_11D_Panel.exe"
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CDP_LWC_Panel_Start.bat"
if not exist "%EXE%" (
  echo 找不到 CDP_LWC_11D_Panel.exe,請把這個檔案放在 EXE 旁邊。
  pause
  exit /b 1
)
> "%TARGET%" echo @echo off
>> "%TARGET%" echo chcp 65001 ^>nul
>> "%TARGET%" echo start "CDP LWC 11-D Panel" /min "%EXE%" --watchdog --no-browser
if errorlevel 1 (
  echo 設定失敗。
  pause
  exit /b 1
)
echo 完成。下次登入 Windows 時,面板會自動在背景啟動。
echo 面板要能運作,筆電也要設定成開機後自動登入,或由你登入一次。
echo 要取消時,執行「取消開機自動啟動.bat」。
pause
