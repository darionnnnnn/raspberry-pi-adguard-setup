#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  樹莓派 AdGuard Home 一鍵部署  ·  雙擊即可執行
#  - 不用記 IP、不用打 hostname:啟動後自動掃描區網,列出樹莓派讓你選,
#    Enter 直接選排最前面的候選(等待期間才上線的新機會自動排最前)
#  - SSH 帳號現場詢問(每台燒錄的帳號可能不同,不寫死)
#  - 斷電/中斷不用從頭來:第一題選「s = 卡已燒好」,程式會偵測這台做到哪、
#    跳過已完成的階段直接接續(全新機器則一路照舊,不會多問);也可選擇只
#    重設特定項目(忘記 AGH 密碼、Wi-Fi 設錯要清掉重來)
#  - 壓測前會從頭總複查一次所有設定,缺的當場補跑,確保壓測測的是最終成品
#  - 壓測時長預設 10 分鐘,執行到那一步時可當場改成別的數字
#  - 部署完成後會問要不要刪除安裝測試 Wi-Fi 並關機(會先確認刪除後仍連得上網)
#  - 報告自動存到本資料夾的 report/ 子資料夾
#  - 密碼(SSH/Wi-Fi/AGH)一律由你自己輸入,腳本不經手
# ═══════════════════════════════════════════════════════════
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/scripts/deploy_pi.py"

# 可調參數 ───────────────────────────────────────────────
SOAK_MIN="10"         # 穩定度壓測分鐘數的預設值(執行到壓測那一步時仍可當場改成別的數字或跳過)
# ─────────────────────────────────────────────────────────

if [ ! -f "$SCRIPT" ]; then
  echo "❌ 找不到部署腳本:$SCRIPT"
  echo "   請確認下載完整 repo,並保留 scripts/ 資料夾。"
  echo ""; read -n 1 -s -r -p "按任意鍵關閉…"; exit 1
fi

# 挑一個「真的能跑」的 python3:/usr/bin/python3 只是轉接器,Xcode 裝好/更新後若還沒同意授權,
# 它會直接拒絕執行(2026-09-19 實際踩到,整個一鍵部署因此失效)。依序試到能跑的為止。
PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Developer/CommandLineTools/usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到可執行的 python3。"
  echo "   若曾安裝或更新 Xcode,請在終端機執行:sudo xcodebuild -license accept"
  echo ""; read -n 1 -s -r -p "按任意鍵關閉…"; exit 1
fi

echo "═══ 樹莓派 AdGuard Home 一鍵部署 ═══"
echo "報告將存到:$HERE/report/"
echo ""

# 不傳 --user:deploy_pi.py 會現場詢問這台的 SSH 帳號
"$PY" "$SCRIPT" --soak "$SOAK_MIN" --report-dir "$HERE/report"

echo ""
echo "═══════════════════════════════════════════"
echo " 部署流程結束。報告在:$HERE/report/"
echo "═══════════════════════════════════════════"
read -n 1 -s -r -p "按任意鍵關閉此視窗…"
echo ""
