# CDP / LWC / 11-D 資料工具

## 下載

| 檔案 | 說明 |
|---|---|
| [**CDP_LWC_11D_Panel.exe**](https://github.com/ting2303/cdp-lwc-monitor/releases/download/v0.1.3/CDP_LWC_11D_Panel.exe) | 單一檔案,下載後雙擊就能用,不必安裝 Python |
| [CDP_LWC_11D_Panel.zip](https://github.com/ting2303/cdp-lwc-monitor/releases/download/v0.1.3/CDP_LWC_11D_Panel.zip) | 完整版,另附使用說明、開機自動啟動、模擬展示和雲端檢查 |

目前版本 v0.1.3。所有版本見 [Releases](https://github.com/ting2303/cdp-lwc-monitor/releases)。

1. 把 EXE 放到固定的資料夾,例如 `C:\CDP_LWC_Panel`。設定檔會建立在 EXE 旁邊。
2. 雙擊開啟,瀏覽器會打開面板。在「設定資料夾」選 PADS 和 11-D 的資料夾。
3. Windows 若顯示「Windows 已保護您的電腦」,按「其他資訊 → 仍要執行」。這個 EXE 沒有數位簽章。

面板只顯示這台電腦上的儀器資料。要遠端看現場筆電,請用 AnyDesk。

## 從原始碼使用

所有工具都**只讀取原始資料**,除了備份工具會寫入您指定的目的地。

| 想做的事 | 指令 |
|---|---|
| 看 CDP + 11-D | 雙擊 `開啟檢視器.bat`,或 `python -m cpd_lwc_qc.viewer --cache cache` |
| 加入新資料 | `python -m cpd_lwc_qc.minute --data "資料夾" --cache cache` |
| 更新典型範圍 | `python -m cpd_lwc_qc.normal --cache cache` |
| 產生品質報告 | `python -m cpd_lwc_qc --data "資料夾" --out output` |
| 備份 | `python backup.py --config backup_config.json` |

需要 Python 3.10 以上與 `requirements.txt` 列的套件;`backup.py` 只用標準函式庫。

## 部署到現場筆電

下載 Releases 的 zip,解壓縮後照裡面的 `使用說明.txt` 做,不必安裝 Python。
面板會顯示 CDP、LWC-300、11-D,把資料複製到 Google 雲端硬碟,有問題時傳到 Discord 並附建議。

`panel_config.json` 含 Discord Webhook 和心跳網址,已排除在版本控制外,不要上傳。觀測資料也不放在這個專案。

自己打包:

```bash
python -m venv venv && venv/Scripts/pip install numpy pyinstaller
venv/Scripts/python tools/build_exe.py build_tmp dist
```

## 無人值守

| 功能 | 怎麼用 |
|---|---|
| 外部心跳 | 在面板填 Healthchecks.io 的 Ping 網址;筆電整台停掉時由外部通知 |
| 看門狗 | `CDP_LWC_11D_Panel.exe --watchdog`,面板當掉或沒回應就重開 |
| 遠端診斷 | 每小時寫到雲端狀態資料夾,含 COM 埠和螢幕截圖 |
| 維護模式 | 面板左下方,暫停通知,時間到自動結束 |
| 確認上傳 | 另一台電腦跑 `--cloud-check 雲端資料夾` |

## 資料校正

```bash
python -m cpd_lwc_qc.correct --cache cache --out output/corrected
```

原始資料不會被改。依據和限制見 [investigations/history_oddities](investigations/history_oddities/README.md)。

## 模擬各種狀況

```bash
python panel.py --demo
```

用假資料走完整的讀檔和判斷流程,可以點選 48 種情境,看燈號和通知。不碰真實資料,不上傳,不傳 Discord。
`tests/test_demo.py` 會檢查每個情境的燈號,以及每種通知都有對應的情境。

## 即時面板(EXE)

`dist_v2/CDP_LWC_11D_Panel/` 整個資料夾複製到觀測電腦即可使用,不必安裝 Python,操作見裡面的 `使用說明.txt`。旁邊放 `cache` 資料夾時,也能在面板切到歷史瀏覽。
只讀取 PADS 與 11-D 持續寫入的檔案,不控制儀器。

重新打包(在有 numpy 與 pyinstaller 的環境):`python tools/build_exe.py`
沒有儀器時可用 `tools/simulate_live.py` 把舊資料當成持續寫入來測試。

## 歷史瀏覽網站

雙擊 `開啟檢視器.bat`,或執行 `python -m cpd_lwc_qc.viewer --cache cache`。

- **日期:** 按日期按鈕開日曆。有觀測的日子有底色,顏色越深時數越長,角落數字是儀器台數,下方是時數。
- **月份:** 按日曆標題看整年,月份底色代表該月有幾天觀測。
- **前後翻:** `‹`、`›` 跳到前後一個有觀測的日子,「最新」跳到最近一次。
- **全部參數:** 頁面下方依儀器勾選,會加一張時間同步的圖,勾選會記住。
- **結論:** 最上方只列有問題和要注意的項目,其他收合。
- **手動值:** 列出流速、壓力、溫度、線溫等手動輸入的值、估計影響和能否與實測比對;受影響的圖標題有 ⚠。
- **雲事件:** 自動標記,所有折線圖都有灰底,編號對應右側的表,點一列可放大。
- **11-D:** 預設是 15 分鐘中位數的對數圖;粒徑—時間圖和其他分析收在下方。

## 檢視器

完全離線,山上沒有網路也能用。資料只在本機處理。

- **選範圍:** 上方選批次或輸入時間,在任一張圖拖曳可放大,雙擊回上一個範圍。
- **看某一刻:** 游標移到圖上,所有圖同步顯示該時間,右側顯示粒徑分布和數值。
- **粒徑分布:** 同時畫 CDP 和 11-D,色帶是過去的典型範圍。
- **時間:** 都用電腦時間對齊。每格是目前範圍內的平均,最短 1 分鐘。

新增資料後重跑 `cpd_lwc_qc.minute`(只會處理新檔案或大小改變的檔案),再重新開啟檢視器。觀測中的即時畫面仍請看 PADS。

## 備份

```bash
copy backup_config.example.json backup_config.json
python backup.py --config backup_config.json --dry-run
python backup.py --config backup_config.json
```

- 只新增或更新,**不會刪除**目的地的檔案。
- `stable_minutes`(預設 15 分鐘)內有變動的檔案先跳過,下次再備份,避免複製到寫一半的檔案。
- 先寫成 `.part`,核對 SHA-256 後才改名。
- 目的地較新且內容不同時不覆蓋,會回報衝突。
- 紀錄在 `backup_logs/`。
- 目的地放在 Google 雲端硬碟電腦版的 G: 槽時,上傳由雲端硬碟完成;複製完成不代表已上傳完畢,請看它的同步狀態。

每天 02:00 自動執行(請改成實際路徑,在觀測電腦上設定一次):

```bash
schtasks /Create /TN "CDP_LWC_Backup" /SC DAILY /ST 02:00 /TR "python C:\CPD_Backup\backup.py --config C:\CPD_Backup\backup_config.json"
```

PADS 手冊建議收資料時避免執行其他程式,以免影響資料擷取,所以預設一天一次。正在記錄的檔案會一直變動,要等停止記錄 15 分鐘後才會備份。

## 品質報告

參數:`--match` 只處理符合的路徑,`--export` 輸出對齊到整秒的 `.csv.gz`,`--skip-11d` 不檢查 11-D,`--config` 換設定檔。
門檻與測站壓力在 `qc_config.json`,預設值見 `cpd_lwc_qc/qc.py` 的 `Limits`。

## 其他資料夾

- `investigations/`:一次性的分析,包含 LWC 比對、玻璃珠校正、典型範圍。
- `cache/`:每分鐘快取,可隨時刪除重建。
- `output/`:品質報告。

## 測試

```bash
python -m unittest discover -s tests
```
