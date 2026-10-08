#!/bin/bash
# 探測 AdGuard Home 機器目前的部署進度與設定完整度(純唯讀,不改任何設定)
# 用法(從 Mac 端):ssh -o BatchMode=yes <user>@<host> bash -s < probe_state.sh
#
# 輸出:每行一個 KEY=VALUE,給 deploy_pi.py / audit_fleet.py 解析(人工直接看也可讀)。
# 用途有三:
#   1. 斷點續跑——部署中斷後重跑時,知道哪些階段已完成可以跳過
#   2. 交付前總複查——壓測前從頭確認所有設定都到位,避免在半套設定上壓測 10 分鐘
#   3. 服役後艦隊稽核(audit_fleet.py)——定期比對線上機器有沒有偏離標準配置
# 設計:一次 SSH 撈完所有標記(避免多次往返),刻意不用 set -e(探測失敗是正常結果)
#
# 跨平台:樹莓派(systemd)與 macOS(launchd)都能跑。macOS 只輸出「應用層」標記
# (AGH 設定/服務/DNS 功能),Pi 專屬的硬體與系統強化標記直接略過——那些措施是為
# SD 卡壽命與 1GB RAM 設計的,Mac 不適用,硬報只會製造假的「未完成」。
PLATFORM=linux
[ "$(uname -s)" = "Darwin" ] && PLATFORM=macos
if [ "$PLATFORM" = "macos" ]; then
  AGH_DIR=/usr/local/AdGuardHome
else
  AGH_DIR=/opt/AdGuardHome
fi
AGH_YAML="$AGH_DIR/AdGuardHome.yaml"
CFG=/boot/firmware/config.txt

yn() { if "$@" > /dev/null 2>&1; then echo 1; else echo 0; fi; }

echo "PROBE_VERSION=3"
echo "M_PLATFORM=$PLATFORM"

if [ "$PLATFORM" = "macos" ]; then
  # ── macOS 變體:只驗應用層(AGH 安裝/設定/自癒/DNS 功能) ──
  echo "A_BIN=$(yn test -x "$AGH_DIR/AdGuardHome")"
  AV=$("$AGH_DIR/AdGuardHome" --version 2>/dev/null | grep -oE 'v[0-9]+\.[0-9.]+' | head -1)
  echo "A_VER=${AV:-?}"
  echo "A_ACTIVE=$(sudo -n launchctl print system/AdGuardHome 2>/dev/null | grep -q 'state = running' && echo 1 || echo 0)"
  echo "A_ENABLED=$(sudo -n plutil -p /Library/LaunchDaemons/AdGuardHome.plist 2>/dev/null | grep -q '"RunAtLoad" => true' && echo 1 || echo 0)"
  echo "A_WIZARD=$(yn sudo -n grep -q '^users:' "$AGH_YAML")"
  # 自癒:launchd 的 KeepAlive 只救「程序死掉」,救不了「程序活著但不回應 DNS」的殭屍狀態
  echo "HC_KEEPALIVE=$(sudo -n plutil -p /Library/LaunchDaemons/AdGuardHome.plist 2>/dev/null | grep -q '"KeepAlive" => true' && echo 1 || echo 0)"
  echo "HC_SCRIPT=$(yn test -x /usr/local/bin/agh-healthcheck.sh)"
  echo "HC_TIMER=$(sudo -n launchctl print system/agh-healthcheck 2>/dev/null | grep -q 'state' && echo 1 || echo 0)"
  # 自身 DNS 是否解耦(不可只指向自己,否則 AGH 掛掉本機也沒 DNS 可自救)
  echo "H_SELFDNS=$(scutil --dns 2>/dev/null | awk '/nameserver\[0\]/{print $3; exit}')"
fi

if [ "$PLATFORM" = "linux" ]; then
# ── 階段 4:系統強化(對照 harden_pi.sh 實際留下的痕跡) ──
echo "H_SYSCTL=$(yn test -f /etc/sysctl.d/99-pi-dns-tuning.conf)"
echo "H_SWAPPINESS=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo '?')"
echo "H_DIRTY=$(cat /proc/sys/vm/dirty_writeback_centisecs 2>/dev/null || echo '?')"
echo "H_EARLYOOM=$(yn systemctl is-active --quiet earlyoom)"
echo "H_JOURNAL=$(yn grep -q '^SystemMaxUse=50M' /etc/systemd/journald.conf)"
CFGN=$(grep -cE '^gpu_mem=16|^dtoverlay=disable-bt|^dtparam=audio=off' "$CFG" 2>/dev/null)
echo "H_CFGTXT=${CFGN:-0}"          # 期望 3(gpu_mem / disable-bt / audio=off)
echo "H_PWRSAVE=$(yn test -f /etc/NetworkManager/conf.d/wifi-powersave-off.conf)"
echo "H_AUTOCLEAN=$(yn test -f /etc/apt/apt.conf.d/21autoclean)"
echo "H_NOREBOOT=$(yn grep -q '^Unattended-Upgrade::Automatic-Reboot \"false\"' /etc/apt/apt.conf.d/50unattended-upgrades)"
echo "H_DIG=$(yn command -v dig)"

# 每個網路連線設定檔的 DNS 解耦狀態(127.0.0.1 + 9.9.9.10)
NETC=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -E 'wireless|ethernet' | cut -d: -f1)
NETN=0; DECON=0
while IFS= read -r c; do
  [ -z "$c" ] && continue
  NETN=$((NETN + 1))
  D=$(nmcli -g ipv4.dns connection show "$c" 2>/dev/null)
  case "$D" in *127.0.0.1*) DECON=$((DECON + 1)) ;; esac
done <<EOF
$NETC
EOF
echo "H_NETCONNS=$NETN"
echo "H_DNSDECOUPLE=$DECON"

# ── 階段 7:Wi-Fi 連線設定檔(名稱|密碼是否已設|是否自動連線) ──
WIFIC=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep wireless | cut -d: -f1)
while IFS= read -r c; do
  [ -z "$c" ] && continue
  PSK=$(nmcli connection show "$c" 2>/dev/null | grep -c 'psk:.*<hidden>')
  AUTO=$(nmcli -g connection.autoconnect connection show "$c" 2>/dev/null)
  echo "W_PROFILE=$c|${PSK:-0}|${AUTO:-?}"
done <<EOF
$WIFIC
EOF
echo "W_ACTIVE=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | grep wireless | cut -d: -f1 | head -1)"

# ── 階段 5:AdGuard Home 安裝 ──
echo "A_BIN=$(yn test -x /opt/AdGuardHome/AdGuardHome)"
AV=$(/opt/AdGuardHome/AdGuardHome --version 2>/dev/null | grep -oE 'v[0-9]+\.[0-9.]+' | head -1)
echo "A_VER=${AV:-?}"
echo "A_ACTIVE=$(yn systemctl is-active --quiet AdGuardHome)"
echo "A_ENABLED=$(yn systemctl is-enabled --quiet AdGuardHome)"
echo "A_WIZARD=$(yn sudo -n grep -q '^users:' "$AGH_YAML")"   # 有 users: = 設定精靈已完成
fi   # end linux-only

# ── 階段 6:AGH 設定校準(用 yaml 解析,不用 grep 硬拆) ──
if [ -f "$AGH_YAML" ]; then
  sudo -n python3 - "$AGH_YAML" <<'PY'
import sys
try:
    import yaml
    c = yaml.safe_load(open(sys.argv[1])) or {}
except Exception as e:
    print("C_PARSE=0"); print("C_ERR=%s" % str(e)[:80]); raise SystemExit(0)
f  = c.get("filters") or []
d  = c.get("dns") or {}
ql = c.get("querylog") or {}
fl = c.get("filtering") or {}
print("C_PARSE=1")
print("C_FILTERS=%d" % len(f))
print("C_FILTERS_ON=%d" % sum(1 for x in f if x.get("enabled")))
print("C_RATELIMIT=%s" % d.get("ratelimit"))
print("C_OPTIMISTIC=%s" % d.get("cache_optimistic"))
print("C_DNSSEC=%s" % d.get("enable_dnssec"))
print("C_UPSTREAM=%s" % ";".join(d.get("upstream_dns") or []))
print("C_FALLBACK=%d" % len(d.get("fallback_dns") or []))
print("C_BOOTSTRAP=%d" % len(d.get("bootstrap_dns") or []))
import json
print("C_USERRULES=%s" % json.dumps(c.get("user_rules") or [], ensure_ascii=True))   # JSON:規則可含任何字元,不可用會被 splitlines 切斷的分隔字元
print("C_QLFILE=%s" % ql.get("file_enabled"))
print("C_UPDINT=%s" % fl.get("filters_update_interval"))
print("C_SAFEBROWSING=%s" % fl.get("safebrowsing_enabled"))
print("C_PROTECTION=%s" % fl.get("protection_enabled"))
PY
else
  echo "C_PARSE=0"
fi

# ── 規則總量(五年運行閥門:突破 150 萬條要移除 OISD Big) ──
# 沒有這個數字,「閥門」只是文件上的約定,沒人會去數——這是把它變成每次探測都回報的指標。
# 用清單快取檔的行數(含註解行,略高於實際規則數,但作為逐次比對的趨勢指標一致且夠用)。
RULES=$(sudo -n sh -c "cat $AGH_DIR/data/filters/*.txt 2>/dev/null | wc -l" 2>/dev/null | tr -d ' ')
echo "C_RULES_TOTAL=${RULES:-?}"

# ── 階段 6:自癒健康檢查(Linux;macOS 版已在上面輸出) ──
if [ "$PLATFORM" = "linux" ]; then
  echo "HC_SCRIPT=$(yn test -x /usr/local/bin/agh-healthcheck.sh)"
  echo "HC_TIMER=$(yn systemctl is-active --quiet agh-healthcheck.timer)"
fi

# ── DNS 實際功能(解析 + 阻擋) ──
# dig 逾時會回非 0(NXDOMAIN 仍是 0),故用 if 區分「查不到」與「被擋成空回應」
if R=$(dig +short +time=3 +tries=2 @127.0.0.1 google.com 2>/dev/null); then
  echo "D_RESOLVE=$(echo "$R" | grep -m1 -E '^[0-9]' || echo '')"
else
  echo "D_RESOLVE="
fi
if B=$(dig +short +time=3 +tries=2 @127.0.0.1 doubleclick.net 2>/dev/null); then
  if [ -z "$B" ] || echo "$B" | grep -q '0\.0\.0\.0'; then echo "D_BLOCK=1"; else echo "D_BLOCK=0"; fi
else
  echo "D_BLOCK=?"
fi

# ── 機器基本資料 / 壓測歷史 ──
if [ "$PLATFORM" = "macos" ]; then
  echo "M_MODEL=$(sysctl -n hw.model 2>/dev/null)"
  echo "M_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
  echo "M_UPTIME=$(uptime | sed 's/.*up //; s/,[[:space:]]*[0-9][0-9]* users*.*//')"
  echo "M_MEMAVAIL=$(vm_stat 2>/dev/null | awk '/Pages free/{f=$3} /Pages inactive/{i=$3} END{gsub(/\./,"",f); gsub(/\./,"",i); print int((f+i)*4096/1048576)}')"
  echo "M_AGHRSS=$(ps -o rss= -p "$(pgrep -x AdGuardHome | head -1)" 2>/dev/null | awk '{print int($1/1024)}')"
  # NTP:時鐘不準 → DoH 憑證驗證全滅(三層備援設計就是為了這個死結,但前提要看得到)
  echo "M_NTP=$(sudo -n systemsetup -getusingnetworktime 2>/dev/null | grep -qi 'On' && echo yes || echo '?')"
else
  echo "M_MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')"
  echo "M_IP=$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "M_UPTIME=$(uptime -p 2>/dev/null | sed 's/^up //')"
  echo "M_THROTTLE=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
  echo "M_MEMAVAIL=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')"
  echo "M_AGHRSS=$(ps -o rss= -C AdGuardHome 2>/dev/null | head -1 | awk '{print int($1/1024)}')"
  # NTP:Pi 無 RTC,斷電後時鐘不準會讓所有 DoH 上游 TLS 驗證失敗(preflight A7 也驗,
  # 但那要到壓測階段才跑;部署早期就抓到才不會把「時鐘問題」誤診成「DNS 設定問題」)
  echo "M_NTP=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
fi
echo "M_HOSTNAME=$(hostname)"
# 磁碟餘裕:清單快取 + statistics 會長期增長,滿了 AGH 寫設定/更新清單都會失敗
echo "M_DISKFREE_MB=$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')"
HIST="$HOME/preflight-history.log"
if [ -f "$HIST" ]; then
  echo "P_HISTORY_N=$(wc -l < "$HIST" | tr -d ' ')"
  echo "P_HISTORY=$(tail -1 "$HIST")"
else
  echo "P_HISTORY_N=0"
fi
