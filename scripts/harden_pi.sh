#!/bin/bash
# 樹莓派 DNS 伺服器系統強化腳本
# 用法(從 Mac 端):ssh -o BatchMode=yes <user>@<host> bash -s < harden_pi.sh
# 前提:該帳號有免密碼 sudo(Raspberry Pi OS 預設使用者即是)
set -e

echo '=== 1/9 移除未使用的舊 swap 檔(實際 swap 走 zram) ==='
sudo rm -fv /var/swap || true

echo '=== 2/9 安裝 earlyoom / unattended-upgrades / dnsutils / 壓測工具 ==='
# memtester/stress-ng 在此提前裝好:preflight 壓測那一刻才裝,遇上 apt 鎖(全新機器
# 首次開機 unattended-upgrades 常佔住)就會失敗;preflight 的 A0 仍會複驗,雙保險
sudo apt-get -o DPkg::Lock::Timeout=180 update -qq
sudo apt-get -o DPkg::Lock::Timeout=180 install -y earlyoom unattended-upgrades dnsutils memtester stress-ng 2>&1 | tail -3

echo '=== 3/9 journald 日誌上限 50M ==='
if ! grep -q '^SystemMaxUse=50M' /etc/systemd/journald.conf; then
  sudo sed -i '/^\[Journal\]/a SystemMaxUse=50M' /etc/systemd/journald.conf
fi
sudo systemctl restart systemd-journald

echo '=== 4/9 unattended-upgrades:僅安全性更新、不自動重開機 ==='
sudo sed -i 's|^        "origin=Debian,codename=${distro_codename},label=Debian";|//      "origin=Debian,codename=${distro_codename},label=Debian";|' /etc/apt/apt.conf.d/50unattended-upgrades
sudo sed -i 's|^//Unattended-Upgrade::Automatic-Reboot "false";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades

echo '=== 5/9 Wi-Fi 省電關閉、無限重試、有線優先 ==='
sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf > /dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
for c in $(nmcli -t -f NAME,TYPE connection show | grep wireless | cut -d: -f1); do
  sudo nmcli connection modify "$c" connection.autoconnect-retries 0 ipv4.route-metric 600 ipv6.route-metric 600
  echo "  Wi-Fi [$c]:無限重試 + metric 600"
done
ETH_CONN=$(nmcli -t -f NAME,TYPE connection show | grep ethernet | cut -d: -f1 | head -1)
if [ -n "$ETH_CONN" ]; then
  sudo nmcli connection modify "$ETH_CONN" ipv4.route-metric 100 ipv6.route-metric 100
  echo "  有線 [$ETH_CONN]:metric 100(優先)"
else
  echo '  (無有線連線設定檔,略過)'
fi
# 重啟 NM 讓 powersave 設定生效;SSH-over-WiFi 會短暫停頓。
# deploy_pi.py 會傳 --no-nm-restart:透過 Wi-Fi 部署時重啟 NM 會斷掉部署連線,
# 而 powersave 於尾端 reboot 也會生效,故 deploy 情境跳過中途重啟(手動跑不傳=即時生效)。
if [ "${1:-}" = "--no-nm-restart" ]; then
  echo '  (--no-nm-restart:略過 NM 重啟,powersave 於下次 reboot 生效)'
else
  sudo systemctl restart NetworkManager
  sleep 5
fi

echo '=== 6/9 apt 每週 autoclean ==='
echo 'APT::Periodic::AutocleanInterval "7";' | sudo tee /etc/apt/apt.conf.d/21autoclean > /dev/null

echo '=== 7/9 無頭伺服器資源優化(需重開機生效) ==='
CFG=/boot/firmware/config.txt
# GPU 記憶體降到最小(無螢幕不需要,釋放約 60MB 給系統——1GB 機型最有感的一項)
grep -q '^gpu_mem=' "$CFG" || echo 'gpu_mem=16' | sudo tee -a "$CFG" > /dev/null
sudo sed -i 's/^gpu_mem=.*/gpu_mem=16/' "$CFG"
# 關閉藍牙(DNS 伺服器用不到:省資源、減攻擊面)
grep -q '^dtoverlay=disable-bt' "$CFG" || echo 'dtoverlay=disable-bt' | sudo tee -a "$CFG" > /dev/null
sudo systemctl disable --now bluetooth 2>/dev/null || true
sudo systemctl disable --now hciuart 2>/dev/null || true
# 關閉音訊(無頭無喇叭)
sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CFG"
# swappiness 調高到 100(zram 是 RAM 速度的壓縮 swap,交換成本極低,積極利用換取更多快取空間)
# + SD 卡寫入批次化:預設每 5 秒刷髒頁,拉長到 15 秒讓零散小寫入合併,減少 SD 卡實際寫入次數
#   (只用 VM 層 sysctl,零開機風險;不改 fstab commit 避免開機風險)。取捨:斷電丟失窗口 5s→15s
sudo tee /etc/sysctl.d/99-pi-dns-tuning.conf > /dev/null <<'EOF'
vm.swappiness = 100
vm.dirty_writeback_centisecs = 1500
vm.dirty_expire_centisecs = 6000
EOF
# 移除舊檔名(冪等:若先前版本用過 99-zram-swappiness.conf)
sudo rm -f /etc/sysctl.d/99-zram-swappiness.conf
sudo sysctl -w vm.swappiness=100 vm.dirty_writeback_centisecs=1500 vm.dirty_expire_centisecs=6000 > /dev/null
echo '  gpu_mem=16、disable-bt、audio=off 已寫入 config.txt(重開機生效);swappiness=100 + SD卡寫入批次化 已立即生效'

echo '=== 8/9 Pi 自身 DNS 解耦(打破自我依賴迴圈) ==='
# 問題:Pi 的 resolv.conf 若從 DHCP 拿到「路由器」,而路由器 DNS 又指回這台 Pi——
# AGH 停擺期間 Pi 自己也沒 DNS(校時、apt、下載 AGH 更新全掛),形成自我依賴迴圈。
# 解法:自己的查詢走 127.0.0.1(享受阻擋),備援直指 9.9.9.10(AGH 掛掉時 refused 秒切,不繞路由器)
for c in $(nmcli -t -f NAME,TYPE connection show | grep -E 'wireless|ethernet' | cut -d: -f1); do
  sudo nmcli connection modify "$c" ipv4.dns "127.0.0.1 9.9.9.10" ipv4.ignore-auto-dns yes
  echo "  [$c]:DNS = 127.0.0.1 + 9.9.9.10(忽略 DHCP 派發)"
done

echo '=== 9/9 驗證 ==='
echo -n 'earlyoom: '; sudo systemctl is-active earlyoom
echo -n 'journald 上限: '; grep '^SystemMaxUse' /etc/systemd/journald.conf | head -1
echo -n '看門狗: '; systemctl show -p RuntimeWatchdogUSec | head -1
echo -n 'unattended-upgrades 重開機設定: '; grep '^Unattended-Upgrade::Automatic-Reboot' /etc/apt/apt.conf.d/50unattended-upgrades | head -1
echo -n 'swappiness / dirty_writeback: '; echo "$(cat /proc/sys/vm/swappiness) / $(cat /proc/sys/vm/dirty_writeback_centisecs)cs"
echo -n 'config.txt 優化: '; grep -cE '^gpu_mem=16|^dtoverlay=disable-bt|^dtparam=audio=off' /boot/firmware/config.txt
echo -n '自身 DNS: '; nmcli -t -f NAME,TYPE connection show | grep -E 'wireless|ethernet' | head -1 | cut -d: -f1 | xargs -I{} nmcli -g ipv4.dns connection show {}
echo '=== 系統強化完成(config.txt 項目需重開機生效,可與後續重開機測試合併) ==='
