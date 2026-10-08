# Raspberry Pi AdGuard Setup

以互動腳本部署 Raspberry Pi AdGuard Home DNS 廣告阻擋伺服器，包含系統強化、設定校準、自癒健康檢查、斷點續跑與硬體壓測。

本專案採精簡維護流程：變更腳本時同步更新使用說明，並驗證語法與受影響功能。

## 環境需求

- 執行端以 macOS 為主要支援環境，需要可執行的 Python 3、OpenSSH、`ssh-copy-id`、`arp`、`dig` 與 `curl`。
- 目標機器為 Raspberry Pi 3B 或以上、Raspberry Pi OS Lite 64-bit、systemd 與 NetworkManager。
- 執行端與目標機器須能在區網互通，目標機器須啟用 SSH。流程會檢查並引導設定免密碼 sudo。
- 燒錄 SD 卡須安裝 Raspberry Pi Imager，由使用者選擇卡片並確認清空。
- `apply_agh_config.py` 在目標機器執行，需有 `python3-yaml`；若沒有，可在目標機器執行 `sudo apt install python3-yaml`。

## 開始使用

下載完整 repo，保留 `scripts/` 資料夾。macOS 可雙擊 `一鍵部署.command`，或在 repo 根目錄執行：

```bash
bash 一鍵部署.command
```

命令列模式：

```bash
python3 scripts/deploy_pi.py
python3 scripts/deploy_pi.py --help
```

已燒好卡可用 `--skip-flash`；`--host`、`--user` 可指定自己的目標機器與帳號。壓測預設 10 分鐘，可用 `--soak 15` 調整。

流程包含系統修改、服務安裝與重啟、重開機。選卡、帳密設定、重新安裝及 Wi-Fi 刪除等步驟會保留互動確認；執行前須確認選到正確機器。

## 檔案與設定

| 檔案 | 用途 |
|---|---|
| `一鍵部署.command` | macOS 雙擊入口，使用 repo 內的腳本 |
| `使用說明.md` | 部署流程、維運與常見排解 |
| `scripts/deploy_pi.py` | 串接部署各階段 |
| `scripts/harden_pi.sh` | 記憶體、SD 卡、網路與系統可靠性設定 |
| `scripts/apply_agh_config.py` | AdGuard Home 過濾清單、上游、快取與自訂規則 |
| `scripts/probe_state.sh` | 唯讀探測目前設定與部署進度 |
| `scripts/preflight_check.sh` | 壓測、供電、散熱及硬體檢查 |
| `scripts/install_agh_healthcheck.sh` | systemd 自癒健康檢查 |
| `scripts/audit_fleet.py` | 唯讀稽核多台機器的設定差異 |
| `scripts/install_agh_healthcheck_macos.sh` | 可選的 macOS 自癒工具，不屬於 Pi 部署流程 |
| `fleet.txt.example` | 使用文件範例位址的機器清單範本 |

| 環境變數 | 用途 |
|---|---|
| `PI_REPORT_DIR` | 命令列模式的報告目錄，預設 repo 下的 `report/`；啟動器另以 `--report-dir` 指定同一位置 |
| `PI_SSH_KEY` | 要安裝到目標的 SSH 公鑰所對應的私鑰路徑，預設 `~/.ssh/id_ed25519`，程式讀取其 `.pub` 檔 |
| `PI_FLEET_FILE` | 自訂機器清單路徑，預設 repo 根目錄的 `fleet.txt` |

若使用自訂 SSH key，請同時在自己的 SSH 設定中指定 `IdentityFile`，確保後續 SSH 與 SCP 使用相同金鑰。

## 隱私與本機資料

公開版本只包含程式、通用說明與註解範例，不包含部署紀錄、實際帳號、內網 IP、Wi-Fi 名稱、設備 MAC、密碼或金鑰。

執行後產生的報告會包含目標 IP、帳號、MAC 與環境狀態；目標機器上的 `~/SETUP.md` 也會記錄環境資訊。Wi-Fi 密碼會在互動終端中顯示並經 stdin 傳給遠端，請避免錄影或儲存終端逐字稿。

`.gitignore` 已排除 `report/`、`fleet.txt`、YAML 設定、日誌、金鑰及常見本機檔案。自行新增或移動紀錄後，推送前仍須檢查 `git diff --cached`；忽略規則不會保護被強制加入或已追蹤的檔案。

## 標準配置

預設採 Cloudflare 資安 DoH、Quad9 DoH 備援與 Google 純 DNS 最終備援，使用 14 份過濾清單。自訂規則預設放行部分影音、Blogspot 與圖床網域；這會同時放行其下的相關查詢，請依自己的需求調整 `STANDARD_USER_RULES`。修改時須同步 `deploy_pi.py` 與 `apply_agh_config.py` 的清單。

上游服務、過濾清單及最新版軟體會隨時間變化。完整部署與硬體壓測需在實際目標機器驗證。
