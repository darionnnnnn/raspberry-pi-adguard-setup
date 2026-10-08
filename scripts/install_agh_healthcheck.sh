#!/bin/bash
# 安裝 AdGuard Home 自癒健康檢查(systemd timer,每 2 分鐘)
# 用途:補「殭屍程序」缺口——AGH 程序活著(systemd Restart 不會觸發)但不回應 DNS 時,自動重啟服務
# 用法(從 Mac 端):ssh -o BatchMode=yes <user>@<host> bash -s < install_agh_healthcheck.sh
# 前提:AdGuard Home 已安裝(在部署流程的階段 5 之後執行)
set -e

sudo tee /usr/local/bin/agh-healthcheck.sh > /dev/null <<'EOF'
#!/bin/bash
# 連續兩次(間隔 5 秒)查詢失敗且服務顯示 active 才重啟——單次失敗可能只是瞬間負載
check() { dig +time=3 +tries=1 @127.0.0.1 google.com > /dev/null 2>&1; }
check && exit 0
sleep 5
check && exit 0
if systemctl is-active --quiet AdGuardHome; then
  logger -t agh-healthcheck "DNS 連續兩次無回應但服務 active(殭屍狀態),執行重啟"
  systemctl restart AdGuardHome
else
  logger -t agh-healthcheck "DNS 無回應且服務非 active,交由 systemd Restart 機制處理,不介入"
fi
EOF
sudo chmod +x /usr/local/bin/agh-healthcheck.sh

sudo tee /etc/systemd/system/agh-healthcheck.service > /dev/null <<'EOF'
[Unit]
Description=AdGuard Home health check (self-heal zombie state)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/agh-healthcheck.sh
EOF

sudo tee /etc/systemd/system/agh-healthcheck.timer > /dev/null <<'EOF'
[Unit]
Description=Run AdGuard Home health check every 2 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now agh-healthcheck.timer

echo '=== 驗證 ==='
systemctl is-active agh-healthcheck.timer
systemctl list-timers agh-healthcheck.timer --no-pager | head -3
echo '自癒健康檢查已安裝:每 2 分鐘檢查,殭屍狀態自動重啟;介入紀錄可用 journalctl -t agh-healthcheck 查詢'
