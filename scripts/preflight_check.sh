#!/bin/bash
# 交付前硬體檢查與穩定性測試
# 用法(從 Mac 端):ssh -o BatchMode=yes <user>@<host> bash -s -- [壓測分鐘數] < preflight_check.sh
# 預設壓測 10 分鐘(足以到達熱飽和平台期);新電源/外殼組合或複驗時可帶 15
# 總時間約 = 壓測時間 + 5-6 分鐘(前置檢查 + 降溫觀察)
# 注意:Bash tool 呼叫時 timeout 要設足(壓測分鐘數 + 8 分鐘的餘裕)
set -u
SOAK_MIN=${1:-10}
FAIL=0
note_fail() { echo "  ❌ FAIL: $1"; FAIL=1; }
note_warn() { echo "  ⚠️  WARN: $1"; }
note_ok()   { echo "  ✅ $1"; }

decode_throttled() {
  local v=$1
  [ "$v" = "0x0" ] && return 0
  local n=$((v))
  (( n & 0x1 ))     && echo "     - 目前正在低電壓!"
  (( n & 0x2 ))     && echo "     - 目前頻率被限制"
  (( n & 0x4 ))     && echo "     - 目前正在過熱降頻"
  (( n & 0x10000 )) && echo "     - 開機以來發生過低電壓(電源供應器可能不夠力)"
  (( n & 0x20000 )) && echo "     - 開機以來發生過頻率限制"
  (( n & 0x40000 )) && echo "     - 開機以來發生過過熱降頻(散熱不足)"
  return 1
}

echo "===== A. 硬體基礎檢查 ====="

echo "[A0] 測試工具依賴(memtester/stress-ng)"
# 缺工具時壓測會「看起來有跑、實際沒負載」:stress-ng 不存在 → CPU 從未滿載,
# 溫度/降頻/丟包數據全是待機值,卻照樣產出 PASS/FAIL 判定(曾遇到,
# 全新機器首次開機 apt 鎖被 unattended-upgrades 佔住,舊版在這裡安靜吞掉安裝失敗)。
# 所以:裝不上就中止,絕不帶病跑出誤導報告。DPkg::Lock::Timeout 等鎖釋放最多 3 分鐘。
# 偵測必須把 sbin 補進 PATH:非互動 SSH 的 PATH 沒有 /usr/sbin,而 memtester 正裝在那——
# 少了這行會把「裝好了」誤判成「沒裝」,明明能跑卻假警報中止(曾遇到)
_tool_missing() {
  local t
  for t in memtester stress-ng; do
    PATH="$PATH:/usr/sbin:/sbin:/usr/local/sbin" command -v "$t" >/dev/null || echo "$t"
  done
}
MISSING_TOOLS=$(_tool_missing)
if [ -n "$MISSING_TOOLS" ]; then
  sudo apt-get -o DPkg::Lock::Timeout=180 install -y -qq $MISSING_TOOLS >/dev/null 2>&1 \
    || { sudo apt-get -o DPkg::Lock::Timeout=180 update -qq >/dev/null 2>&1
         sudo apt-get -o DPkg::Lock::Timeout=180 install -y -qq $MISSING_TOOLS >/dev/null 2>&1; } \
    || true
  STILL_MISSING=$(_tool_missing)
  if [ -n "$STILL_MISSING" ]; then
    note_fail "測試工具裝不上:$(echo $STILL_MISSING)——沒有它們壓測不會真的滿載,結果無意義"
    echo ""
    echo "🔴 依賴缺失,體檢中止(未執行壓測,本次無有效結果)"
    echo "   請先手動執行:sudo apt-get install -y memtester stress-ng 再重跑本測試"
    exit 1
  fi
  note_ok "已自動補裝:$(echo $MISSING_TOOLS)"
else
  note_ok "memtester、stress-ng 皆已就緒"
fi

echo "[A1] 供電/降頻旗標(壓測前基準)"
T0=$(vcgencmd get_throttled | cut -d= -f2)
if decode_throttled "$T0"; then note_ok "throttled=$T0(無異常紀錄)"; else note_fail "throttled=$T0(見上,開機至今已有異常)"; fi

echo "[A2] 溫度(待機)"
TEMP0=$(vcgencmd measure_temp | grep -oE '[0-9.]+')
awk -v t="$TEMP0" 'BEGIN{exit !(t<65)}' && note_ok "待機 ${TEMP0}°C" || note_fail "待機溫度 ${TEMP0}°C 偏高(>65°C,檢查散熱/環境)"

echo "[A3] SD 卡讀取速度"
SPEED=$(sudo dd if=/dev/mmcblk0 of=/dev/null bs=4M count=100 iflag=direct 2>&1 | grep -oE '[0-9.]+ MB/s' | head -1)
SPEED_NUM=$(echo "$SPEED" | grep -oE '^[0-9.]+')
awk -v s="${SPEED_NUM:-0}" 'BEGIN{exit !(s>=20)}' && note_ok "讀取 $SPEED" || note_fail "讀取僅 ${SPEED:-無法測得}(<20MB/s,卡片品質或讀取異常)"

echo "[A3b] SD 卡寫入速度(100MB + fsync)"
WFILE="$HOME/.preflight_write_test"
WSPEED=$(dd if=/dev/zero of="$WFILE" bs=4M count=25 conv=fsync 2>&1 | grep -oE '[0-9.]+ MB/s' | head -1)
rm -f "$WFILE"
WSPEED_NUM=$(echo "$WSPEED" | grep -oE '^[0-9.]+')
awk -v s="${WSPEED_NUM:-0}" 'BEGIN{exit !(s>=8)}' && note_ok "寫入 $WSPEED" || note_fail "寫入僅 ${WSPEED:-無法測得}(<8MB/s,卡片老化或品質不佳,建議換卡)"

echo "[A3c] 檔案系統健康(ext4 錯誤計數)"
ROOTDEV=$(findmnt -n -o SOURCE /)
FS_STATE=$(sudo tune2fs -l "$ROOTDEV" 2>/dev/null | awk -F: '/Filesystem state/{gsub(/^ +/,"",$2); print $2}')
FS_ERRS=$(sudo tune2fs -l "$ROOTDEV" 2>/dev/null | awk -F: '/FS Error count/{gsub(/^ +/,"",$2); print $2}')
if [ "$FS_STATE" = "clean" ] && [ "${FS_ERRS:-0}" = "0" ]; then
  note_ok "state=clean, error count=0"
else
  note_fail "state=${FS_STATE:-未知}, error count=${FS_ERRS:-未知}(檔案系統有歷史損傷,建議 fsck 或重灌)"
fi

echo "[A4] 核心錯誤掃描(dmesg)"
ERRS=$(sudo dmesg | grep -icE 'mmc.*(error|timeout)|I/O error|under-?voltage' || true)
[ "$ERRS" -eq 0 ] && note_ok "無 mmc/IO/電壓錯誤" || { note_fail "dmesg 有 $ERRS 筆相關錯誤,節錄:"; sudo dmesg | grep -iE 'mmc.*(error|timeout)|I/O error|under-?voltage' | tail -5; }

echo "[A5] Wi-Fi 訊號(目前連線)"
SIG=$(nmcli -t -f IN-USE,SIGNAL,SSID device wifi list --rescan no 2>/dev/null | grep '^\*' | cut -d: -f2 | head -1)
if [ -n "${SIG:-}" ]; then
  [ "$SIG" -ge 50 ] && note_ok "訊號 ${SIG}%" || note_fail "訊號僅 ${SIG}%(<50%,部署位置建議調整)"
else
  echo "  ⚠️  目前非 Wi-Fi 連線(有線中),略過"
fi

echo "[A6] 記憶體快篩(memtester 100MB x1)"
if sudo memtester 100M 1 > /tmp/memtest.log 2>&1; then note_ok "100MB 測試通過"; else note_fail "memtester 未通過,詳見 /tmp/memtest.log"; fi

echo "[A7] NTP 時間同步(DoH 憑證驗證的前提)"
NTP_SYNC=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
[ "$NTP_SYNC" = "yes" ] && note_ok "已同步" || note_fail "未同步(NTPSynchronized=$NTP_SYNC)——時間不準會導致 DoH 上游全部失效"

echo "[A8] 硬體看門狗(核心死當自救)"
WD=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null)
if [ -n "$WD" ] && [ "$WD" != "0" ] && [ "$WD" != "infinity" ]; then
  note_ok "RuntimeWatchdogUSec=$WD"
else
  note_fail "看門狗未啟用(RuntimeWatchdogUSec=${WD:-空})——核心死當只能手動拔電"
fi

echo "[A9] 備援上游可達性(交付前驗證備胎不是理論值)"
# 重試 3 次:單次查詢易受瞬間丟包影響誤報(實測踩過)
G8OK=0
for try in 1 2 3; do
  dig +short +time=3 +tries=1 @8.8.8.8 google.com >/dev/null 2>&1 && { G8OK=1; break; }
  sleep 2
done
[ "$G8OK" -eq 1 ] \
  && note_ok "最終備援 8.8.8.8(純DNS)可用" \
  || note_fail "8.8.8.8 連續 3 次查詢失敗——最終備援不可用"
# 備援 DoH = Quad9(2026-09-05 起 Cloudflare 升主上游,主上游由下方 dig 測試實際走過)
CF_CODE=$(curl -s -o /dev/null -m 8 -w '%{http_code}' https://dns10.quad9.net/dns-query 2>/dev/null)
if [ -n "$CF_CODE" ] && [ "$CF_CODE" != "000" ]; then
  note_ok "備援 Quad9 DoH 端點可達(HTTP $CF_CODE,TLS/網路通)"
else
  note_fail "Quad9 DoH 端點連不上——備援上游不可用(也可能是時鐘不準導致 TLS 失敗,對照 A7)"
fi

echo ""
echo "===== B. 穩定性壓測(${SOAK_MIN} 分鐘:CPU 滿載 + DNS 負載) ====="

: > /tmp/dnsfail.cnt; : > /tmp/dnstotal.cnt; rm -f /tmp/gwping.log
# 背景:對閘道器持續 ping(抓高負載下 Wi-Fi 掉包——供電紋波/干擾只在壓力下現形)
GATEWAY=$(ip route | awk '/^default/{print $3; exit}')
if [ -n "${GATEWAY:-}" ]; then
  ping -i 1 -w $((SOAK_MIN*60)) "$GATEWAY" > /tmp/gwping.log 2>&1 &
  GWPING_PID=$!
fi
# 記憶體洩漏哨兵:壓測前記下 AGH RSS,壓測後比對(五年不重開,緩慢爬升是 B3 尖峰測抓不到的故障)
AGH_RSS_START=$(ps -o rss= -C AdGuardHome 2>/dev/null | head -1)
AGH_RSS_START=$(( ${AGH_RSS_START:-0} / 1024 ))
# 背景:CPU 滿載
stress-ng --cpu "$(nproc)" --timeout "${SOAK_MIN}m" --quiet &
STRESS_PID=$!
# 啟動驗證:stress-ng 若沒真的跑起來(裝了但壞掉/參數不合),整段壓測只是待機空轉,
# 數據看似正常實則無效——寧可中止也不產出誤導報告
sleep 3
if ! kill -0 "$STRESS_PID" 2>/dev/null; then
  kill "${GWPING_PID:-}" 2>/dev/null || true
  note_fail "stress-ng 啟動後即退出——CPU 未滿載,壓測結果無效"
  echo ""
  echo "🔴 壓測引擎啟動失敗,體檢中止(本次無有效結果)"
  exit 1
fi
# 背景:DNS 查詢負載(混合快取命中與未命中)
DNS_FAILS=0; DNS_TOTAL=0
( END=$(( $(date +%s) + SOAK_MIN*60 ))
  DOMAINS=(google.com wikipedia.org github.com cloudflare.com example.com)
  while [ "$(date +%s)" -lt "$END" ]; do
    for d in "${DOMAINS[@]}"; do
      dig +time=2 +tries=1 @127.0.0.1 "$d" >/dev/null 2>&1 || echo x >> /tmp/dnsfail.cnt
      echo x >> /tmp/dnstotal.cnt
    done
    dig +time=2 +tries=1 @127.0.0.1 "t$(date +%s%N).example.org" >/dev/null 2>&1 || true
    sleep 1
  done ) &
DNSLOAD_PID=$!

# 前景:每 30 秒採樣溫度
MAXTEMP=$TEMP0
for i in $(seq 1 $((SOAK_MIN*2))); do
  sleep 30
  T=$(vcgencmd measure_temp | grep -oE '[0-9.]+')
  awk -v a="$T" -v b="$MAXTEMP" 'BEGIN{exit !(a>b)}' && MAXTEMP=$T
  echo "  [$((i*30))s] ${T}°C"
done
wait "$STRESS_PID" 2>/dev/null; wait "$DNSLOAD_PID" 2>/dev/null
[ -n "${GWPING_PID:-}" ] && wait "$GWPING_PID" 2>/dev/null

DNS_TOTAL=$(wc -l < /tmp/dnstotal.cnt 2>/dev/null || echo 0)
DNS_FAILS=$(wc -l < /tmp/dnsfail.cnt 2>/dev/null || echo 0)
# 記憶體洩漏哨兵:壓測後 AGH RSS(此時尚未重啟,反映真實運行後的記憶體)
AGH_RSS_END=$(ps -o rss= -C AdGuardHome 2>/dev/null | head -1)
AGH_RSS_END=$(( ${AGH_RSS_END:-0} / 1024 ))

echo ""
echo "===== B2. 突發並發查詢(模擬全家裝置同時喚醒) ====="
# 重要:AdGuard Home 有 ratelimit(預設每秒每來源 20 筆)。測試若超過這個速率,
# 被擋下的查詢是「防濫用機制正常運作」,不是效能不足——所以測試速率要貼齊設定值,
# 否則會量到自己的 ratelimit 而誤判 FAIL(踩過這個坑)。
RATELIMIT=$(sudo grep -E '^\s+ratelimit:' /opt/AdGuardHome/AdGuardHome.yaml 2>/dev/null | grep -oE '[0-9]+' | head -1)
RATELIMIT=${RATELIMIT:-20}
# 並發程序數上限 25:每個 dig 約 8MB,太多並發會在 1GB 機型上吃掉數百 MB 瞬時記憶體
[ "$RATELIMIT" -gt 25 ] && RATELIMIT=25
: > /tmp/burstfail.cnt
BURST_DOMAINS=(google.com wikipedia.org github.com cloudflare.com example.com)
BURST_TOTAL=0
# 分 5 輪,每輪送 ratelimit 筆並發、間隔 1 秒 → 貼齊上限但不觸發限流
for round in $(seq 1 5); do
  for w in $(seq 1 "$RATELIMIT"); do
    d=${BURST_DOMAINS[$(( w % 5 ))]}
    ( dig +time=3 +tries=1 @127.0.0.1 "$d" >/dev/null 2>&1 || echo x >> /tmp/burstfail.cnt ) &
    BURST_TOTAL=$((BURST_TOTAL+1))
  done
  wait
  sleep 1
done
BURST_FAILS=$(wc -l < /tmp/burstfail.cnt 2>/dev/null || echo 0)
BURST_PCT=$(( BURST_FAILS * 100 / BURST_TOTAL ))
[ "$BURST_PCT" -le 2 ] \
  && note_ok "$BURST_TOTAL 筆並發查詢(貼齊 ratelimit=$RATELIMIT/秒),失敗 $BURST_FAILS(${BURST_PCT}%)" \
  || note_fail "並發失敗率 ${BURST_PCT}%(>2%)——若遠高於此,檢查是否 ratelimit 設太低或服務異常"

echo ""
echo "===== B3. 清單解析尖峰(重啟服務=每12小時更新的最重記憶體事件) ====="
sudo systemctl restart AdGuardHome
PEAK_RSS=0; READY=0; RECOVER=0
for i in $(seq 1 30); do
  sleep 2
  RSS_KB=$(ps -o rss= -C AdGuardHome 2>/dev/null | head -1)
  RSS_MB=$(( ${RSS_KB:-0} / 1024 ))
  [ "$RSS_MB" -gt "$PEAK_RSS" ] && PEAK_RSS=$RSS_MB
  if dig +time=1 +tries=1 @127.0.0.1 google.com >/dev/null 2>&1; then
    READY=1; RECOVER=$((i*2)); break
  fi
done
if [ "$READY" -eq 1 ]; then
  note_ok "服務於 ${RECOVER} 秒內恢復,解析尖峰 RSS ${PEAK_RSS}MB"
else
  note_fail "重啟後 60 秒服務仍未恢復(規則解析卡住或記憶體不足)"
fi
OOM_CNT=$(sudo dmesg | grep -icE 'oom-killer|Out of memory' || true)
EARLYOOM_KILLS=$(sudo journalctl -u earlyoom --since '-15 min' 2>/dev/null | grep -ci 'sending SIG' || true)
if [ "$OOM_CNT" -eq 0 ] && [ "$EARLYOOM_KILLS" -eq 0 ]; then
  note_ok "無 OOM / earlyoom 介入紀錄"
else
  note_fail "偵測到記憶體壓力事件(kernel OOM:$OOM_CNT 筆、earlyoom 砍程序:$EARLYOOM_KILLS 筆)——1GB RAM 撐不住目前清單量,考慮移除 OISD Big"
fi

echo ""
echo "===== C. 壓測後判定 ====="
echo "[C1] 壓測期間最高溫度"
if awk -v t="$MAXTEMP" 'BEGIN{exit !(t<80)}'; then
  note_ok "最高 ${MAXTEMP}°C"
elif awk -v t="$MAXTEMP" 'BEGIN{exit !(t<85)}'; then
  note_warn "最高 ${MAXTEMP}°C(80-85°C 熱軟限速區)——DNS 日常負載遠低於滿載,可正常服役;但建議加散熱片/風扇換取夏季高溫餘裕"
else
  note_fail "最高 ${MAXTEMP}°C(≥85°C 硬降頻線)——散熱不足,必須處理"
fi

echo "[C2] 供電/降頻旗標(壓測後,依位元區分問題來源)"
T1=$(vcgencmd get_throttled | cut -d= -f2)
N1=$((T1))
UV=$(( N1 & 0x10001 ))    # 低電壓位 → 供電問題
HARD=$(( N1 & 0x40004 ))  # 過熱硬降頻位(85°C)→ 散熱問題
if [ "$N1" -eq 0 ]; then
  note_ok "throttled=0x0(壓力下供電與散熱皆穩)"
elif [ "$UV" -ne 0 ]; then
  decode_throttled "$T1" || true
  note_fail "throttled=$T1 含低電壓位——**供電問題**:換 5V/2.5A 以上原廠等級電源,並檢查 USB 線材(細線/長線的壓降是常見主因)"
elif [ "$HARD" -ne 0 ]; then
  decode_throttled "$T1" || true
  note_fail "throttled=$T1 含過熱硬降頻位(≥85°C)——**散熱問題**,必須處理"
else
  decode_throttled "$T1" || true
  note_warn "throttled=$T1 僅熱軟限速位(80°C 軟限,無低電壓位)——**供電正常**,屬散熱餘裕議題,對照 C1 的溫度判讀"
fi

echo "[C3] 壓力下 DNS 服務"
if [ "$DNS_TOTAL" -gt 0 ]; then
  PCT=$(( DNS_FAILS * 100 / DNS_TOTAL ))
  [ "$PCT" -le 2 ] && note_ok "$DNS_TOTAL 次查詢,失敗 $DNS_FAILS(${PCT}%)" || note_fail "$DNS_TOTAL 次查詢失敗 $DNS_FAILS 次(${PCT}% > 2%,壓力下服務不穩)"
else
  note_fail "DNS 負載測試未執行到任何查詢"
fi

echo "[C4] 壓力下閘道器連線品質"
if [ -s /tmp/gwping.log ]; then
  GWLOSS=$(grep -oE '[0-9.]+% packet loss' /tmp/gwping.log | grep -oE '^[0-9.]+')
  awk -v l="${GWLOSS:-100}" 'BEGIN{exit !(l<=2)}' && note_ok "閘道器丟包 ${GWLOSS}%" || note_fail "閘道器丟包 ${GWLOSS}%(>2%,高負載下網路不穩——常見原因:供電紋波影響 Wi-Fi 模組、訊號邊緣)"
else
  echo "  ⚠️  無閘道器 ping 紀錄,略過"
fi

echo "[C5] 服務與記憶體狀態"
systemctl is-active AdGuardHome --quiet && note_ok "AdGuardHome active" || note_fail "AdGuardHome 不在運作!"
AVAIL=$(free -m | awk '/^Mem:/{print $7}')
[ "$AVAIL" -ge 200 ] && note_ok "可用記憶體 ${AVAIL}MB" || note_fail "可用記憶體僅 ${AVAIL}MB(<200MB)"

echo "[C6] 降溫回落(90 秒觀察,驗證散熱路徑)"
sleep 90
TEND=$(vcgencmd measure_temp | grep -oE '[0-9.]+')
if awk -v e="$TEND" -v m="$MAXTEMP" 'BEGIN{exit !(e <= m-3 || e < 65)}'; then
  note_ok "壓測峰值 ${MAXTEMP}°C → 90 秒後 ${TEND}°C"
else
  note_fail "90 秒後仍 ${TEND}°C(峰值 ${MAXTEMP}°C 幾乎沒降)——散熱路徑受阻,檢查外殼通風/散熱片"
fi

echo "[C7] 記憶體洩漏哨兵(壓測前後 AGH RSS 對比 — 五年不重開的長期趨勢)"
if [ "${AGH_RSS_START:-0}" -gt 0 ] && [ "${AGH_RSS_END:-0}" -gt 0 ]; then
  RSS_DELTA=$(( AGH_RSS_END - AGH_RSS_START ))
  if [ "$RSS_DELTA" -le 30 ]; then
    note_ok "AGH RSS ${AGH_RSS_START}→${AGH_RSS_END}MB(壓測增長 ${RSS_DELTA}MB,屬正常快取/查詢緩衝填充)"
  elif [ "$RSS_DELTA" -le 60 ]; then
    note_warn "AGH RSS ${AGH_RSS_START}→${AGH_RSS_END}MB(增長 ${RSS_DELTA}MB 偏高)——單次不一定是洩漏,靠年檢逐年比對趨勢(history.log 的 aghRSS 欄)"
  else
    note_fail "AGH RSS ${AGH_RSS_START}→${AGH_RSS_END}MB(增長 ${RSS_DELTA}MB 異常)——疑似記憶體洩漏,記下 AGH 版本,考慮回報上游或降回前一穩定版"
  fi
else
  RSS_DELTA="?"
  echo "  ⚠️  無法取得 AGH RSS(壓測期間服務可能異常),略過"
fi

echo "[C8] 記憶體餘裕評估(1G 機型的長期健康指標)"
# 危險門檻:可用記憶體 <150MB 代表逼近 earlyoom 介入區
if [ "${AVAIL:-0}" -ge 350 ]; then
  note_ok "壓測後可用 ${AVAIL}MB,距 earlyoom 介入區(~150MB)還有充足餘裕"
elif [ "${AVAIL:-0}" -ge 200 ]; then
  note_warn "壓測後可用 ${AVAIL}MB——尚可,但餘裕縮小,對照清單總量是否接近 150 萬條閥門"
else
  note_fail "壓測後可用 ${AVAIL}MB(<200MB)——記憶體吃緊,檢查清單量是否超標(見 config-rationale 記憶體預算)"
fi

# 年檢趨勢紀錄(逐年比對硬體老化)
HIST="$HOME/preflight-history.log"
echo "$(date +%F) soak=${SOAK_MIN}m idle=${TEMP0}C peak=${MAXTEMP}C throttled=${T1} sd_r=${SPEED_NUM:-?}MB/s sd_w=${WSPEED_NUM:-?}MB/s peakRSS=${PEAK_RSS}MB aghRSS=${AGH_RSS_START:-?}->${AGH_RSS_END:-?}MB avail=${AVAIL:-?}MB dns_fail=${DNS_FAILS}/${DNS_TOTAL} burst_fail=${BURST_PCT}% wifi=${SIG:-N/A}% result=$([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)" >> "$HIST"
echo "(趨勢紀錄已附加至 $HIST,年檢時逐年比對)"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "🟢 全部通過——接著執行重開機測試(由 Mac 端指引),通過後即可清理交付"
else
  echo "🔴 有項目未通過,先排除問題再交付(見上方 FAIL 標記)"
  exit 1
fi
