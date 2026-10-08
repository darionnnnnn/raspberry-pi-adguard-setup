#!/usr/bin/env python3
"""AdGuard Home 艦隊稽核(Day-2 維運):比對線上機器有沒有偏離標準配置。

**為什麼需要這支**:整套 skill 原本只有「部署時把關」——probe_state / 交付前總複查
都只在部署當下跑一次。機器交付上線之後,設定飄移(標準演進了但某台沒跟上、有人從
網頁 UI 改了設定、清單長超過閥門)沒有任何機制會發現。例如 ratelimit 沿用舊值,或 DNS 指回自己形成自我依賴迴圈。

**純唯讀**:只跑 probe_state.sh 收資料,不改任何設定。發現的差異由你決定要不要修
(修的方式就是對那台重跑對應的腳本,全部冪等)。

用法:
    python3 audit_fleet.py                      # 稽核清單檔裡的所有機器
    python3 audit_fleet.py pi-user@192.0.2.10    # 只稽核指定機器(可給多台)
    python3 audit_fleet.py --report <報告路徑>  # 見 README.md 的報告設定

機器清單檔(預設為本 skill 目錄下的 fleet.txt,依腳本位置推算,不進版控;不存在會建範本):
    # 一行一台:user@host  可選的標籤
    pi-user@192.0.2.10          Example Pi
    mac-user@192.0.2.20         Example Mac

離線機器算正常(Pi 常被帶到別的家、或關機待部署),只標記為「未稽核」不算失敗。
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_pi import (C, ok, warn, err, info, hdr, probe_state, _i, _t, _on,
                       _reachable, _pad, _has_std_rules, latest_agh_version)

# 機器清單放 skill 根目錄(是設定不是腳本,跟 README.md 擺一起比較好找)
# 清單檔:PI_FLEET_FILE 環境變數優先,否則依本腳本位置推算(不依賴任何絕對路徑,
# 所以 repo clone 到哪都成立)。可用 PI_FLEET_FILE 指定清單路徑。
FLEET_FILE = os.environ.get("PI_FLEET_FILE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet.txt")

FLEET_TEMPLATE = """# AdGuard Home 艦隊清單(audit_fleet.py 用)
# 一行一台:user@host   後面可加標籤(空白分隔)
# 以 # 開頭的行會被忽略
#
# pi-user@192.0.2.10          Example Pi
# mac-user@192.0.2.20         Example Mac
"""

# 五年運行閥門:規則總量突破此數要移除 OISD Big(僅適用 1GB RAM 的 Pi;
# Mac 記憶體充裕,同一個數字對它沒有意義,故只做資訊性顯示)
RULES_VALVE = 1_500_000


def load_fleet(path):
    """讀機器清單 → [(user, host, label)];檔案不存在則建範本並回傳空清單。"""
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(FLEET_TEMPLATE)
        warn(f"機器清單不存在,已建立範本:{path}")
        info("  請把你的機器填進去(格式見檔案內註解),或直接在命令列指定 user@host")
        return []
    fleet = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        target = parts[0]
        label = parts[1].strip() if len(parts) > 1 else ""
        if "@" not in target:
            warn(f"清單格式錯誤(缺 user@):{line}")
            continue
        user, host = target.split("@", 1)
        fleet.append((user, host, label))
    return fleet


def audit_one(st, latest=None):
    """把一台機器的探測結果評成稽核清單 → [(名稱, ok, 細節)]。

    ok = True 符合標準 / False 偏離標準 / None 純資訊。
    平台差異:Pi 專屬的系統強化只在 linux 檢;macOS 只驗應用層——那些 SD 卡壽命與
    1GB RAM 的措施對 Mac 不適用,硬報只會製造假的「未完成」。
    """
    plat = st.get("M_PLATFORM", "linux")
    rows = []

    # ── 服務活著 ──
    rows.append(("AGH 服務", _on(st, "A_ACTIVE"),
                 f"{st.get('A_VER','?')} · {'執行中' if _on(st,'A_ACTIVE') else '未執行'}"))

    # ── 版本是否落後(五年策略:一律用當下最新穩定版) ──
    cur = st.get("A_VER", "?")
    if latest and cur != "?":
        rows.append(("AGH 版本", cur == latest,
                     f"目前 {cur} · 最新 {latest}" + ("" if cur == latest else " ← 建議更新")))
    else:
        rows.append(("AGH 版本", None, f"目前 {cur}(查不到最新版可比對)"))

    # ── 應用層標準配置(兩平台共通) ──
    if st.get("C_PARSE") != "1":
        rows.append(("標準配置", False, "設定檔讀不到(sudo 權限或路徑問題)"))
    else:
        fon = _i(st, "C_FILTERS_ON", 0)
        items = [
            (f"啟用清單 {fon}", fon >= 14),
            ("ratelimit=50", st.get("C_RATELIMIT") == "50"),
            ("樂觀快取", _t(st, "C_OPTIMISTIC")),
            ("DNSSEC", _t(st, "C_DNSSEC")),
            ("清單12h更新", st.get("C_UPDINT") == "12"),
            ("Cloudflare 上游", "cloudflare" in (st.get("C_UPSTREAM") or "").lower()),
            ("自訂規則", _has_std_rules(st)),
            ("備援鏈≥2", _i(st, "C_FALLBACK", 0) >= 2),
            ("bootstrap≥2", _i(st, "C_BOOTSTRAP", 0) >= 2),
            ("安全瀏覽", _t(st, "C_SAFEBROWSING")),
            ("防護啟用", _t(st, "C_PROTECTION")),
        ]
        # querylog 不寫檔是「SD 卡壽命」措施:Mac 是 SSD,寫檔無妨,不列為偏離
        if plat == "linux":
            items.append(("querylog不寫檔", st.get("C_QLFILE") == "False"))
        miss = [n for n, o in items if not o]
        rows.append(("標準配置", not miss,
                     f"{len(items)-len(miss)}/{len(items)} 項符合"
                     + ("" if not miss else " · 偏離:" + "、".join(miss))))

    # ── 規則總量 vs 五年閥門 ──
    rules = _i(st, "C_RULES_TOTAL", -1)
    if rules < 0:
        rows.append(("規則總量", None, "測不到"))
    elif plat == "linux":
        rows.append(("規則總量", rules < RULES_VALVE,
                     f"{rules:,} 條(閥門 {RULES_VALVE:,})"
                     + ("" if rules < RULES_VALVE else " ← 已超標,依五年策略移除 OISD Big")))
    else:
        rows.append(("規則總量", None, f"{rules:,} 條(Mac 記憶體充裕,閥門不適用)"))

    # ── DNS 實際功能 ──
    resolve = (st.get("D_RESOLVE") or "").strip()
    r_ok = bool(resolve and resolve[0].isdigit())
    b_ok = st.get("D_BLOCK") == "1"
    rows.append(("DNS 功能", r_ok and b_ok,
                 f"解析 {resolve or '失敗'} · 阻擋 {'✓' if b_ok else '未擋'}"))

    # ── 殭屍自癒(程序活著但不回應 DNS;systemd/launchd 的重啟機制都救不了這種) ──
    if plat == "linux":
        hc = _on(st, "HC_SCRIPT") and _on(st, "HC_TIMER")
        rows.append(("殭屍自癒", hc, "timer active(每 2 分鐘)" if hc else "未安裝或 timer 未啟動"))
    else:
        hc = _on(st, "HC_SCRIPT")
        rows.append(("殭屍自癒", hc,
                     "已安裝" if hc else
                     "未安裝 —— launchd KeepAlive"
                     + ("=true 只救『程序死掉』" if _on(st, "HC_KEEPALIVE") else " 未啟用")
                     + ",救不了『程序活著但不回應 DNS』的殭屍狀態"))

    # ── 自身 DNS 解耦(打破自我依賴迴圈:AGH 掛掉時自己還要能校時/更新/自救) ──
    if plat == "linux":
        nets, deco = _i(st, "H_NETCONNS", 0), _i(st, "H_DNSDECOUPLE", 0)
        rows.append(("自身DNS解耦", nets > 0 and deco >= nets, f"{deco}/{nets} 條連線已解耦"))
    else:
        self_dns = (st.get("H_SELFDNS") or "").strip()
        own_ip = (st.get("M_IP") or "").strip()
        # 指到自己的 LAN IP = 最糟:AGH 一掛,這台連 DNS 都沒有(127.0.0.1 至少走 loopback)
        loop = self_dns in (own_ip, "") and self_dns != ""
        rows.append(("自身DNS解耦", not loop,
                     f"nameserver[0]={self_dns or '?'}"
                     + (" ← 指向自己,AGH 掛掉本機無 DNS 可自救" if loop else "")))

    # ── 時間同步(不準會讓所有 DoH 上游 TLS 驗證失敗) ──
    ntp = (st.get("M_NTP") or "").strip()
    rows.append(("時間同步", ntp in ("yes", "1"), f"NTP={ntp or '?'}"))

    # ── 資源餘裕 ──
    mem = _i(st, "M_MEMAVAIL", -1)
    disk = _i(st, "M_DISKFREE_MB", -1)
    mem_ok = None if plat == "macos" else (mem >= 200 if mem >= 0 else None)
    rows.append(("記憶體餘裕", mem_ok,
                 f"可用 {mem}MB · AGH {st.get('M_AGHRSS','?')}MB" if mem >= 0 else "測不到"))
    rows.append(("磁碟餘裕", disk >= 1024 if disk >= 0 else None,
                 f"可用 {disk}MB" if disk >= 0 else "測不到"))

    # ── Pi 專屬:系統強化 ──
    if plat == "linux":
        cfgn = _i(st, "H_CFGTXT", 0)
        hitems = [
            ("earlyoom", _on(st, "H_EARLYOOM")),
            ("journald 50M", _on(st, "H_JOURNAL")),
            ("sysctl 調校", _on(st, "H_SYSCTL") and _i(st, "H_SWAPPINESS", 0) == 100),
            ("config.txt 三項", cfgn >= 3),
            ("Wi-Fi 省電關閉", _on(st, "H_PWRSAVE")),
            ("不自動重開機", _on(st, "H_NOREBOOT")),
        ]
        hmiss = [n for n, o in hitems if not o]
        rows.append(("系統強化", not hmiss,
                     f"{len(hitems)-len(hmiss)}/{len(hitems)} 項"
                     + ("" if not hmiss else " · 缺:" + "、".join(hmiss))))
        thr = st.get("M_THROTTLE")
        if thr and thr != "0x0":
            rows.append(("供電/降頻旗標", None, f"{thr}(細節見 preflight C2 判讀)"))

    return rows


def print_rows(rows):
    for name, o, detail in rows:
        mark = "✅" if o else ("ℹ️ " if o is None else "❌")
        color = C.G if o else (C.B if o is None else C.R)
        print(f"    {color}{mark} {_pad(name, 14)}{C.E}{detail}")


def main():
    ap = argparse.ArgumentParser(description="AdGuard Home 艦隊稽核(純唯讀)")
    ap.add_argument("targets", nargs="*", help="user@host(不給則讀機器清單檔)")
    ap.add_argument("--fleet", default=FLEET_FILE, help=f"機器清單檔(預設 {FLEET_FILE})")
    ap.add_argument("--report", default=None, help="另存 markdown 報告的路徑")
    a = ap.parse_args()

    if a.targets:
        fleet = []
        for t in a.targets:
            if "@" not in t:
                err(f"格式應為 user@host:{t}"); sys.exit(1)
            u, h = t.split("@", 1)
            fleet.append((u, h, ""))
    else:
        fleet = load_fleet(a.fleet)
    if not fleet:
        err("沒有要稽核的機器")
        info(f"  用法:python3 audit_fleet.py user@<IP>")
        info(f"  或把機器填進清單檔:{a.fleet}")
        sys.exit(1)

    hdr("AdGuard Home 艦隊稽核(純唯讀,不會改任何設定)")
    latest = latest_agh_version()
    info(f"AGH 最新穩定版:{latest or '(查不到,略過版本比對)'}")

    results = []   # (label, user, host, rows or None)
    for user, host, label in fleet:
        name = label or host
        print()
        info(f"{C.BOLD}▸ {name}({user}@{host}){C.E}")
        if not _reachable(host, user):
            warn("連不上 —— 未稽核(機器可能關機或帶到別處,屬正常)")
            results.append((name, user, host, None))
            continue
        st = probe_state(host, user)
        if not st:
            err("探測失敗(sudo 權限或系統不支援)")
            results.append((name, user, host, None))
            continue
        info(f"  {st.get('M_PLATFORM','?')} · {st.get('M_MODEL','?')} · 開機 {st.get('M_UPTIME','?')}")
        rows = audit_one(st, latest)
        print_rows(rows)
        results.append((name, user, host, rows))

    # ── 總結 ──
    hdr("稽核總結")
    audited = [r for r in results if r[3] is not None]
    offline = [r for r in results if r[3] is None]
    total_bad = 0
    for name, user, host, rows in audited:
        bad = [n for n, o, _ in rows if o is False]
        total_bad += len(bad)
        if bad:
            err(f"{name}:{len(bad)} 項偏離標準 —— " + "、".join(bad))
        else:
            ok(f"{name}:完全符合標準")
    for name, user, host, _ in offline:
        info(f"  ⏸  {name}:未稽核(連不上)")
    if audited and total_bad == 0:
        ok("艦隊全數符合標準配置")
    elif total_bad:
        print()
        info("修正方式(會動到線上服務,自行挑離峰時間;改設定前先備份 yaml):")
        info("  · 系統強化缺項 → harden_pi.sh(僅 Pi,冪等)")
        info("  · 殭屍自癒未裝 → install_agh_healthcheck.sh(Pi)/ install_agh_healthcheck_macos.sh(Mac)")
        info("  · 自身DNS指向自己 → Pi 跑 harden_pi.sh;Mac 用")
        info("      sudo networksetup -setdnsservers <服務名> 127.0.0.1 9.9.9.10")
        info("  · 標準配置偏離:")
        info("      Pi  → apply_agh_config.py(這台的清單本來就該是標準 14 個,整支重套安全)")
        info("      Mac → ⚠️ 不要整支重套!apply_agh_config.py 會把清單「拉回標準 14 個」")
        info("            (加進使用者刻意排除的清單、並強制 querylog 不寫檔),")
        info("            Mac 的清單是刻意分歧的。只改偏離的那幾個鍵即可,例如:")
        info("            停服務 → sed -i '' 's/^  ratelimit: .*/  ratelimit: 50/' <yaml> → 啟動")

    if a.report:
        os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"# AdGuard Home 艦隊稽核報告", "", f"- **稽核時間**:{now}",
                 f"- **AGH 最新穩定版**:{latest or '?'}",
                 f"- **稽核機器**:{len(audited)} 台線上 / {len(offline)} 台未稽核", ""]
        for name, user, host, rows in results:
            lines += [f"## {name}(`{user}@{host}`)", ""]
            if rows is None:
                lines += ["未稽核(連不上——機器可能關機或帶到別處)", ""]
                continue
            lines += ["| 項目 | 狀態 | 細節 |", "|---|---|---|"]
            for n, o, d in rows:
                mark = "✅ 符合" if o else ("ℹ️ 資訊" if o is None else "❌ 偏離")
                lines.append(f"| {n} | {mark} | {d} |")
            lines.append("")
        lines += ["---", "", "*由 audit_fleet.py 產生 · skill: pi-adguard-setup*"]
        with open(a.report, "w") as f:
            f.write("\n".join(lines))
        ok(f"稽核報告已產生:{a.report}")

    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
