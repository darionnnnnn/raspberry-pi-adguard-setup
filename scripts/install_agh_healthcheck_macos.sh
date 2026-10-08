#!/bin/bash
# 安裝 AdGuard Home 自癒健康檢查(macOS / launchd 版,每 2 分鐘)
# 用法(從另一台 Mac):ssh <user>@<host> bash -s < install_agh_healthcheck_macos.sh
#
# 為什麼需要:launchd 的 KeepAlive 只處理「程序死掉」——程序活著但不回應 DNS 的
# **殭屍狀態**它不會介入,對 DNS 伺服器來說那跟死掉一樣嚴重(全家上不了網),
# 卻不會觸發任何自動修復。這支補的就是這個缺口,與 Pi 版 install_agh_healthcheck.sh
# 完全對應(那邊用 systemd timer,這邊用 launchd StartInterval)。
set -e

AGH_BIN=/usr/local/AdGuardHome/AdGuardHome
if [ ! -x "$AGH_BIN" ]; then
  echo "❌ 找不到 $AGH_BIN —— 這台不是 macOS 版 AdGuard Home 主機?"
  exit 1
fi

sudo tee /usr/local/bin/agh-healthcheck.sh > /dev/null <<'EOF'
#!/bin/bash
# 連續兩次(間隔 5 秒)查詢失敗才重啟——單次失敗可能只是瞬間負載
check() { dig +time=3 +tries=1 @127.0.0.1 google.com > /dev/null 2>&1; }
check && exit 0
sleep 5
check && exit 0
# launchd 版沒有 systemd 的 is-active 概念,用 pgrep 判斷程序是否還在:
# 程序在但不回應 = 殭屍狀態,要主動踢;程序不在則交給 KeepAlive 自己拉起來
if pgrep -x AdGuardHome > /dev/null 2>&1; then
  logger -t agh-healthcheck "DNS 連續兩次無回應但程序存在(殭屍狀態),執行重啟"
  launchctl kickstart -k system/AdGuardHome
else
  logger -t agh-healthcheck "DNS 無回應且程序不存在,交由 launchd KeepAlive 處理,不介入"
fi
EOF
sudo chmod +x /usr/local/bin/agh-healthcheck.sh

sudo tee /Library/LaunchDaemons/agh-healthcheck.plist > /dev/null <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>agh-healthcheck</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/agh-healthcheck.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>120</integer>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
sudo chown root:wheel /Library/LaunchDaemons/agh-healthcheck.plist
sudo chmod 644 /Library/LaunchDaemons/agh-healthcheck.plist

sudo launchctl bootout system/agh-healthcheck 2>/dev/null || true
sudo launchctl bootstrap system /Library/LaunchDaemons/agh-healthcheck.plist

echo '=== 驗證 ==='
sudo launchctl print system/agh-healthcheck 2>/dev/null | grep -E 'state|program' | head -3
echo '自癒健康檢查已安裝(macOS/launchd 版):每 2 分鐘檢查,殭屍狀態自動 kickstart'
echo '介入紀錄查詢:log show --predicate '"'"'process == "logger"'"'"' --last 1d | grep agh-healthcheck'
