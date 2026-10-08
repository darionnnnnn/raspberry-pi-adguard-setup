#!/usr/bin/env python3
"""校準 AdGuard Home 設定為標準配置。

用法(在目標機器上,先停止 AdGuardHome 服務):
    sudo python3 apply_agh_config.py [--has-ipv6] [--config PATH]

預設 config 路徑為樹莓派的 /opt/AdGuardHome/AdGuardHome.yaml;
macOS 請加 --config /usr/local/AdGuardHome/AdGuardHome.yaml。

冪等:重複執行不會重複加清單,已存在的設定會被覆蓋成標準值。
不會動到 users(管理員帳密)區塊。
"""
import argparse
import sys

try:
    import yaml
except ImportError:
    sys.exit("需要 python3-yaml(Raspberry Pi OS 內建;若無:sudo apt install python3-yaml)")

STANDARD_FILTERS = [
    # 廣告主力
    ("AdGuard DNS filter", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt"),
    ("StevenBlack Unified Hosts", "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"),
    ("OISD Big", "https://big.oisd.nl/"),
    ("Peter Lowe's Blocklist", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_3.txt"),
    # 中文/區域廣告(台灣使用情境)
    ("CHN: anti-AD", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_21.txt"),
    ("AWAvenue Ads Rule", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_53.txt"),
    # 資安(詐騙/惡意/釣魚)
    ("Scam Blocklist by DurableNapkin", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_10.txt"),
    ("Malicious URL Blocklist (URLHaus)", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_11.txt"),
    ("Phishing URL Blocklist (PhishTank and OpenPhish)", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_30.txt"),
    ("Dandelion Sprout's Anti-Malware List", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_12.txt"),
    ("ShadowWhisperer's Malware List", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_42.txt"),
    # 輕量補強(合計 <2 千條,幾乎零記憶體成本)
    ("Perflyst and Dandelion Sprout's Smart-TV Blocklist", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_7.txt"),
    ("HaGeZi's Badware Hoster Blocklist", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_55.txt"),
    ("HaGeZi's The World's Most Abused TLDs", "https://adguardteam.github.io/HostlistsRegistry/assets/filter_56.txt"),
]

# 已淘汰的清單(套用時自動移除):AdAway 已從官方目錄下架,且幾乎全被 StevenBlack 覆蓋
REMOVED_FILTER_URLS = {
    "https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt",
}

# 上游鏈:Cloudflare 主、Quad9 備,純 DNS 作最終備援。
UPSTREAM = ["https://security.cloudflare-dns.com/dns-query"]
# 備援鏈:Quad9 DoH → 8.8.8.8 純 DNS(補時鐘不準導致 DoH 全滅的死結)
FALLBACK = ["https://dns10.quad9.net/dns-query", "8.8.8.8"]

# bootstrap 跟著主上游走:開機解析 DoH 主機名不該卡在已降級的供應商路徑上
BOOTSTRAP_V4 = ["1.1.1.1", "1.0.0.1", "9.9.9.10"]
BOOTSTRAP_V6 = ["2606:4700:4700::1111", "2620:fe::10"]

# 標準自訂規則(只補標準規則、不動使用者其他規則)
STANDARD_USER_RULES = [
    # Pangle(ByteDance 廣告 SDK)繞過清單、直查 CNAME 目標 pangle-shared-api.bytewlb.akadns.net,
    # 連 HaGeZi Pro++ 都沒收此別名,加清單無解。若使用 TikTok 系 app 出問題時,
    # 改窄成 ||pangle-shared-api.bytewlb.akadns.net^
    "||bytewlb.akadns.net^",
    # 預設放行 Blogspot,可依需求縮小範圍($important 優先)。
    "@@||blogspot.com^$important",
    # 嵌入影音:影片頁、串流、縮圖與無 Cookie 嵌入都需要放行。
    "@@||youtube.com^$important",
    "@@||youtube-nocookie.com^$important",
    "@@||googlevideo.com^$important",
    "@@||ytimg.com^$important",
    "@@||tiktok.com^$important",
    "@@||tiktokcdn.com^$important",
    "@@||tiktokv.com^$important",
    "@@||ibytedtos.com^$important",
    # Blogger 圖片與台灣社群常用圖床。
    "@@||blogger.googleusercontent.com^$important",
    "@@||imgur.com^$important",
    "@@||imgbb.com^$important",
    "@@||ibb.co^$important",
    "@@||postimages.org^$important",
    "@@||sm.ms^$important",
]

# 公開版本不移除既有的使用者自訂規則。
REMOVED_STANDARD_USER_RULES = set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--has-ipv6", action="store_true",
                    help="此網路可用 IPv6(ping6 2620:fe::10 通)才加這個參數")
    ap.add_argument("--config", default="/opt/AdGuardHome/AdGuardHome.yaml")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # --- 過濾清單:移除已淘汰的、補齊標準清單並全部啟用(冪等,以 URL 判重) ---
    filters = cfg.setdefault("filters", [])
    filters[:] = [f for f in filters if f["url"] not in REMOVED_FILTER_URLS]
    existing_urls = {f["url"] for f in filters}
    max_id = max((f["id"] for f in filters), default=0)
    for name, url in STANDARD_FILTERS:
        if url in existing_urls:
            for f in filters:
                if f["url"] == url:
                    f["enabled"] = True
        else:
            max_id += 1
            filters.append({"enabled": True, "url": url, "name": name, "id": max_id})

    # --- filtering ---
    filt = cfg.setdefault("filtering", {})
    filt["filters_update_interval"] = 12   # 每 12 小時更新清單
    filt["filtering_enabled"] = True
    filt["protection_enabled"] = True
    filt["safebrowsing_enabled"] = True    # 惡意/釣魚網站防護

    # --- dns ---
    dns = cfg.setdefault("dns", {})
    dns["upstream_dns"] = list(UPSTREAM)
    dns["fallback_dns"] = list(FALLBACK)
    dns["upstream_mode"] = "load_balance"
    dns["bootstrap_dns"] = BOOTSTRAP_V4 + (BOOTSTRAP_V6 if args.has_ipv6 else [])
    dns["cache_optimistic"] = True         # 過期快取先回應、背景更新
    dns["enable_dnssec"] = True
    # ratelimit 預設 20/秒且以 /24 子網聚合——整個家用區網共享同一個 20 QPS 配額,
    # 裝置多的晚上高峰可能集體撞限流(症狀:間歇性查詢失敗)。調到 50 保留防濫用又有實際餘裕
    dns["ratelimit"] = 50

    # --- 紀錄保留期(減少磁碟寫入) ---
    # querylog 逐筆查詢寫檔是 SD 卡最大的「小而頻繁」寫入源(NAND 寫入放大最傷)。
    # file_enabled=False → 只存記憶體(size_memory 筆,儀表板仍可看最近查詢,重開機清空),
    # 完全不落 SD 卡。statistics 是聚合資料、寫入頻率低且對看阻擋效果有價值,保留寫檔。
    ql = cfg.setdefault("querylog", {})
    ql["interval"] = "14d"        # 僅在 file_enabled 時相關,保留無害
    ql["file_enabled"] = False    # 關鍵:不寫檔,消除最大持續寫入源
    cfg.setdefault("statistics", {})["interval"] = "7d"

    # --- 自訂規則:只補缺的,保留使用者自己加的 ---
    rules = [r for r in (cfg.get("user_rules") or [])
             if r not in REMOVED_STANDARD_USER_RULES]
    added = [r for r in STANDARD_USER_RULES if r not in rules]
    cfg["user_rules"] = rules + added

    with open(args.config, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

    print(f"已套用標準配置 → {args.config}")
    print(f"  過濾清單:{len(filters)} 個(啟用 {sum(1 for x in filters if x['enabled'])})")
    print(f"  上游:{dns['upstream_dns']} → 備援 {dns['fallback_dns']}")
    print(f"  bootstrap_dns:{dns['bootstrap_dns']}")
    print(f"  自訂規則:{len(cfg['user_rules'])} 條(本次補上 {len(added)} 條)")
    print("  記得重新啟動 AdGuardHome 服務,並等 10-15 秒再驗證(規則載入期間 53 埠尚未綁定)")


if __name__ == "__main__":
    main()
