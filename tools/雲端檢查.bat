@echo off
chcp 65001 >nul
rem 在家裡或辦公室的電腦執行,確認現場資料真的上傳到雲端。請把下面的路徑改成這台電腦的雲端硬碟資料夾。
"%~dp0CDP_LWC_11D_Panel.exe" --cloud-check "G:\我的雲端硬碟"
pause
