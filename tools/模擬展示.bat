@echo off
chcp 65001 >nul
rem 用假資料展示各種狀況的燈號和通知,不碰真實資料,不上傳,不傳 Discord。
"%~dp0CDP_LWC_11D_Panel.exe" --demo
