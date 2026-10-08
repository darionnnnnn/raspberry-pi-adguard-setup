#!/usr/bin/env python3
"""樹莓派 AdGuard Home 一鍵部署 orchestrator。

把部署的各階段串成一條龍:燒錄引導 → 連線 → 進度偵測 → 系統強化 → 安裝 AGH → 校準 →
自癒健康檢查 → 正式Wi-Fi(可加多組) → 功能驗證 → 交付前總複查 → 穩定度測試 → 重開機測試 →
完整報告 → 收尾(關機)。呼叫 repo 內的各階段腳本。

設計原則(與整套 skill 一致):
  - 密碼由使用者親自輸入:SSH 登入密碼交給 ssh-copy-id 自己讀(繼承 TTY,Python 不碰);
    AGH 管理員帳密在瀏覽器設定精靈輸入;Wi-Fi 密碼就地明碼輸入(方便當場確認打對,
    避免拿到部署地才發現連不上),經 stdin 傳給遠端 nmcli、不進遠端 ps/history
  - 破壞性/GUI 關卡停下來引導,其餘全自動
  - **斷點續跑**:連上後先跑 probe_state.sh 偵測機器現況,已完成的重階段(系統強化/
    安裝 AGH/校準)可直接跳過——斷電或中途中斷不必從頭來過。全新機器不會多問任何一句
  - **選擇性重設**(2026-07-28 新增):偵測到已完成階段時,除了跳過/全部重跑,也可以
    選擇只重設特定項目(如忘記 AGH 管理員密碼、Wi-Fi 設錯要清掉重來)。AGH 重灌與
    Wi-Fi 刪除是真正的破壞性動作,需輸入 yes 二次確認;系統強化/校準/健康檢查本來就
    是冪等覆寫,直接重跑即等於還原
  - **壓測前總複查**:壓測是驗「成品」扛不扛得住,故壓測前再從頭驗一次所有設定,
    有缺項就地補跑(全部腳本冪等)再複查,確認齊備才進壓測,不在半套設定上白跑 10 分鐘
  - **收尾關機**(2026-07-28 新增):部署完成後詢問要不要刪除安裝測試 Wi-Fi 並關機、
    只關機、或不動作;會先確認刪除後是否仍有可用連線,避免刪成孤兒機
  - 每階段擷取結果,最後彙整成 markdown 報告

用法(host/user 都可不給,流程中會互動詢問/掃描;桌面雙擊 .command 即用此模式):
  python3 deploy_pi.py                              # 全互動:掃描選機 + 問帳號
  python3 deploy_pi.py --host <IP> --user <帳號> --skip-flash   # 指定、卡已燒好
  python3 deploy_pi.py --skip-test                 # 部署但不壓測(快速)

參數(全部可選):
  --host        目標 IP/hostname;不給則掃描區網等機器上線讓你選
  --user        SSH 使用者;不給則燒錄後互動詢問(每台可不同帳號)
  --soak        壓測分鐘數預設值(預設 10;執行到該步驟時仍可當場改成別的數字或 s 跳過)
  --skip-flash  跳過燒錄引導(卡已燒好、機器已開機)
  --skip-test   跳過穩定度測試
  --skip-reboot 跳過重開機測試(對服役中機器避免斷線)
  --no-resume   不做斷點續跑,已完成的階段照樣重跑一遍(冪等,單純多花時間)
  --report-dir  報告輸出目錄(預設讀 $PI_REPORT_DIR,未設定則 repo 根目錄下的 report/)
  --has-ipv6    此網路有 IPv6(預設純 IPv4 bootstrap)
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

PI_MAC_PREFIXES = ("b8:27:eb", "dc:a6:32", "e4:5f:01", "2c:cf:67", "28:cd:c1", "d8:3a:dd")

# 五年運行閥門:規則總量突破此數要移除 OISD Big(1GB RAM 機型的記憶體預算上限)
RULES_VALVE = 1_500_000

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = SKILL_DIR  # 腳本都在同目錄

# ── 輸出小工具 ────────────────────────────────────────────────
class C:
    G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[94m"; BOLD = "\033[1m"; E = "\033[0m"

def hdr(msg):  print(f"\n{C.B}{C.BOLD}{'='*60}\n{msg}\n{'='*60}{C.E}")
def ok(msg):   print(f"{C.G}  ✅ {msg}{C.E}")
def warn(msg): print(f"{C.Y}  ⚠️  {msg}{C.E}")
def err(msg):  print(f"{C.R}  ❌ {msg}{C.E}")
def info(msg): print(f"  {msg}")
def pause(msg): input(f"{C.Y}{C.BOLD}⏸  {msg} —— 完成後按 Enter 繼續…{C.E}")

def ask(question, options=()):
    """互動輸入的統一排版。

    舊寫法把問題、全部選項、預設值塞在同一行 input() 的括號裡,跟上下的說明文字
    混在一起,使用者常分不清「現在輪到我打字了嗎?要打什麼?」。改成固定三段式:
      · 空一行與前文隔開,問題用醒目色單獨一行
      · 每個選項獨立一行縮排列出(Enter 的預設行為永遠列成第一個選項)
      · 輸入行只剩一個「❯」游標——看到 ❯ 就是輪到你打字
    """
    print()
    print(f"{C.B}{C.BOLD}  ❓ {question}{C.E}")
    for opt in options:
        print(f"     {opt}")
    try:
        return input(f"{C.BOLD}  ❯ {C.E}").strip()
    except EOFError:
        return ""

# ── SSH 執行封裝 ─────────────────────────────────────────────
# accept-new:新機首次連自動接受 host key(BatchMode 無法互動,不加這個新機必失敗)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]

def _hostkey_changed(out):
    return "IDENTIFICATION HAS CHANGED" in out or "host key verification failed" in out.lower()

def _clear_hostkey(host):
    subprocess.run(["ssh-keygen", "-R", host], capture_output=True)

def _has_known_hostkey(host):
    """known_hosts 裡是否已有這台的指紋——用來辨識「這個 IP 之前是別台機器用的」。"""
    r = subprocess.run(["ssh-keygen", "-F", host], capture_output=True, text=True)
    return r.returncode == 0 and bool((r.stdout or "").strip())

def _diagnose_ssh_failure(host, user, out, terse=False):
    """把 SSH 失敗訊息翻譯成「該查什麼」——三種失敗長得像但處方完全不同。

    實戰踩過:同一個 IP 換了另一台樹莓派,畫面只寫 Permission denied,使用者以為
    是「它堅持要用金鑰、不讓我打密碼」,實際上多半是【帳號打錯】——每台機器的帳號
    都不同,而 Imager 進階選項的欄位會沿用上一次的值,很容易帶到別台的帳號。
    """
    low = (out or "").lower()
    if _hostkey_changed(out):
        warn("主機指紋不符 —— 這個 IP 之前是別台機器用的(換機重用 IP 很正常,不是被攻擊)")
        info(f"  處方:ssh-keygen -R {host}  清掉舊指紋後重連(本工具會自動處理)")
        return "hostkey"
    if "permission denied" in low:
        warn(f"認證被拒 —— 機器連得到,但 `{user}` 這個帳號密碼登不進去")
        if not terse:   # 完整說明只講一次,重試時不重複洗版
            info(f"  最常見的原因是【帳號打錯】,不是密碼錯:每台機器的帳號都不一樣,")
            info(f"  而 Raspberry Pi Imager 進階選項的『使用者名稱』欄位會沿用上次的值。")
            info(f"  請確認這台燒錄時填的帳號真的是「{user}」")
        return "auth"
    if "connection refused" in low:
        warn("連線被拒 —— SSH 服務還沒起來(機器可能還在開機),或 Imager 沒勾選啟用 SSH")
        return "refused"
    if "逾時" in (out or "") or "timed out" in low or "no route" in low:
        warn("連線逾時 —— 機器可能還在開機,或這個 IP 上根本沒有機器(DHCP 換位址了?)")
        return "timeout"
    return "unknown"

def ssh(host, user, cmd, timeout=60, check=False):
    """回傳 (exit_code, stdout+stderr)。自動處理新機 host key 與 IP 重用的 key 變更。"""
    def _run():
        full = ["ssh"] + SSH_OPTS + [f"{user}@{host}", cmd]
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "(SSH 逾時)"
    code, out = _run()
    if _hostkey_changed(out):  # IP 重用導致舊 key 不符,清掉重試一次
        _clear_hostkey(host); code, out = _run()
    if check and code != 0:
        err(f"遠端指令失敗(exit {code}): {out.strip()[:200]}")
    return code, out

def ssh_script(host, user, local_script, args="", timeout=900):
    """把本地腳本透過 stdin 餵給遠端 bash 執行。"""
    with open(local_script) as f:
        content = f.read()
    def _run():
        full = ["ssh"] + SSH_OPTS + [f"{user}@{host}", f"bash -s -- {args}"]
        try:
            r = subprocess.run(full, input=content, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "(腳本執行逾時)"
    code, out = _run()
    if _hostkey_changed(out):
        _clear_hostkey(host); code, out = _run()
    return code, out

def scan_pi_candidates():
    """在本機掃 ARP 找樹莓派候選(基金會 MAC 前綴),回傳 [(ip, mac), …]。"""
    try:
        # -n 不做 hostname 反解:在 DNS/反解慢的網路(如 mesh 路由器)避免 arp 卡住/timeout
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return []
    cands = []
    for line in out.splitlines():
        low = line.lower()
        if any(p in low for p in PI_MAC_PREFIXES):
            ip = re.search(r"\(([\d.]+)\)", line)
            mac = re.search(r"at ([0-9a-f:]+)", low)
            if ip:
                cands.append((ip.group(1), mac.group(1) if mac else "?"))
    return cands

def _mdns_hostnames(ips):
    """並行 mDNS 反查 hostname——讓候選清單直接顯示機器名,不必對照 MAC 猜是哪台。

    直接對多播位址 224.0.0.251:5353 問(樹莓派 OS 預設跑 avahi,幾乎都答得出來),
    不經路由器 DNS——所以 mesh 路由器反解慢/壞掉都不影響(這正是 scan 用 arp -an
    不做反解的原因;mDNS 這條路沒有那個坑)。單台 1 秒逾時且並行查,不拖慢選單。
    """
    def one(ip):
        try:
            r = subprocess.run(
                ["dig", "+short", "+time=1", "+tries=1", "-p", "5353",
                 "@224.0.0.251", "-x", ip],
                capture_output=True, text=True, timeout=4)
            for line in (r.stdout or "").splitlines():
                line = line.strip().rstrip(".")
                # dig 的逾時/錯誤註解(「;; connection timed out…」)也走 stdout,
                # 不濾掉會被當成 hostname 顯示出來
                if line and not line.startswith(";"):
                    return ip, (line[:-6] if line.endswith(".local") else line)
        except Exception:
            pass
        return ip, ""
    if not ips:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, ips))

def _port_open(host, port=22, timeout=3):
    """22 埠有沒有在聽——用來分辨「服務還沒起來」與「帳密不對」。

    這是關鍵前置判斷:Connection refused 代表**根本沒有 SSH 服務可以認證**,
    此時不管帳號密碼多正確都不會出現密碼提示。先問這一句,才不會叫使用者去輸入
    一個永遠不會出現的密碼提示(踩過:機器還在開機時工具說「請輸入密碼」但直接失敗)。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def _wait_for_ssh(host, max_wait=180):
    """等 SSH 服務起來(22 埠開始接受連線),回傳是否等到。

    全新卡首次開機要自動擴充分區,SSH 常要 1-3 分鐘才 listen,這段期間一律是
    Connection refused(機器 ping 得到、服務還沒起來)。原本只能讓使用者反覆按
    Enter 重試,看起來像「一直連不上」——改成自動等,並說明燈號該是什麼樣子。
    """
    info(f"等 SSH 服務起來(最多 {max_wait // 60} 分鐘)…")
    info("  全新卡首次開機要擴充分區比較久;紅燈恆亮 + 綠燈閃爍 = 正常開機中")
    waited = 0
    while waited < max_wait:
        if _port_open(host):
            ok(f"SSH 服務已就緒(等了 {waited} 秒)")
            return True
        time.sleep(5)
        waited += 5
        if waited % 30 == 0:
            info(f"  仍在等({waited} 秒)…")
    return False

def _probe_ssh(host, user):
    """試連一次,回傳 (是否成功, 原始輸出)——輸出留著給 _diagnose_ssh_failure 判讀。"""
    code, out = ssh(host, user, "echo up", timeout=8)
    return code == 0, out

def _reachable(host, user):
    ok_, _ = _probe_ssh(host, user)
    return ok_

def _throttle_verdict(t):
    """位元判讀 throttled(與 preflight C2 一致):區分供電/散熱/正常,避免『非0x0就警告』的誤導。"""
    if not t or t == "?":
        return "?"
    try:
        n = int(t, 16) if str(t).startswith("0x") else int(t)
    except ValueError:
        return str(t)
    if n == 0:
        return "✅ 0x0(供電與散熱皆乾淨)"
    if n & 0x10001:   # 低電壓位(現在/曾經)
        return f"⚠️ {t}(含低電壓位 → 供電問題,換 5V/2.5A 電源或粗一點的線)"
    if n & 0x40004:   # 過熱硬降頻位(85°C)
        return f"⚠️ {t}(含硬降頻位 → 散熱問題,須處理)"
    return f"🟡 {t}(僅 80°C 熱軟限速歷史位,供電正常;屬散熱餘裕議題,見壓測 C1/C2)"

# 公鑰路徑:PI_SSH_KEY 環境變數優先,
# 未設定時退回各平台通用的預設位置。
PUBKEY = os.path.expanduser(os.environ.get("PI_SSH_KEY", "~/.ssh/id_ed25519") + ".pub")

# 強制走密碼認證:不先試金鑰(新機本來就沒有我們的公鑰),確保密碼提示一定會出現
PW_OPTS = ["-o", "StrictHostKeyChecking=accept-new",
           "-o", "PubkeyAuthentication=no",
           "-o", "PreferredAuthentications=password,keyboard-interactive"]

def _install_key_via_password(host, user):
    """備援:不經 ssh-copy-id,直接用密碼登入把公鑰附加到遠端 authorized_keys。

    ssh-copy-id 各版本的內部探測行為(會先用 publickey 試一次)在某些情況下會
    搶先失敗、連密碼提示都不給。這條路徑完全不試金鑰,只走密碼,行為可預測。
    密碼仍由 ssh 自己從 TTY 讀取(Python 不碰);公鑰內容走 stdin,不進遠端 ps/history。
    """
    if not os.path.exists(PUBKEY):
        err(f"找不到公鑰 {PUBKEY}")
        info("  先產生一把:ssh-keygen -t ed25519 -N \"\" -f ~/.ssh/id_ed25519")
        return False
    with open(PUBKEY) as f:
        pub = f.read()
    remote = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
              "cat >> ~/.ssh/authorized_keys && "
              "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys")
    r = subprocess.run(["ssh"] + PW_OPTS + [f"{user}@{host}", remote],
                       input=pub, text=True)
    return r.returncode == 0

def _ssh_copy_id(host, user):
    """就地把公鑰裝到目標機:密碼由 ssh 自己互動讀取(繼承終端機),Python 不碰密碼。

    **IP 重用是這裡的常態**——DHCP 會把前一台樹莓派用過的位址發給新機。而
    `StrictHostKeyChecking=accept-new` 只自動接受「全新的」主機,對「指紋變了」是
    硬性拒絕直接中止,**連密碼提示都不會出現**,症狀看起來就是「一直要用金鑰登入、
    不讓我打帳密」。所以這裡先主動清掉舊指紋,不能依賴前面的 ssh() 剛好清過——
    ssh() 只在「有讀到 host key 錯誤訊息」時才清,一旦它是逾時(機器還在開機時很常見)
    就不會清,舊指紋原封不動留著擋住這一步。
    """
    print()
    if _has_known_hostkey(host):
        warn(f"{host} 有舊的主機指紋 —— 這個 IP 之前是別台機器用的")
        info("  已清除舊指紋(換機重用 IP 屬正常,不是遭到攻擊);接著用帳號密碼登入")
        _clear_hostkey(host)
    info(f"就地設定金鑰——請在下方輸入 {host} 的【登入密碼】(燒錄時設的那組,只需這一次):")
    # 不 capture_output:讓 ssh 繼承本終端機的 TTY,密碼提示直接顯示、你直接輸入
    r = subprocess.run(["ssh-copy-id"] + PW_OPTS + [f"{user}@{host}"])
    if r.returncode == 0:
        return True
    # ssh-copy-id 失敗不一定是密碼錯——改走行為可預測的備援路徑再試一次
    warn("ssh-copy-id 沒成功,改用直接附加公鑰的方式再試一次…")
    info(f"  請再輸入一次 {host} 的登入密碼:")
    return _install_key_via_password(host, user)

def stage_sudo(host, user, report):
    """確認免密碼 sudo;沒有就就地設定(密碼由 ssh -t 繼承終端機讀取,Python 不碰)。

    後面每個階段都是非互動 SSH 跑 sudo——沒有免密碼 sudo 就全部卡在
    「a terminal is required to read the password」。舊版 Raspberry Pi OS 的 Imager
    會附 /etc/sudoers.d/010_pi-nopasswd;**2026 trixie 映像不再附**(曾遇到:
    強化、AGH 安裝全失敗)。這裡補回同一個檔,等於恢復舊版預設。
    """
    code, _ = ssh(host, user, "sudo -n true")
    if code == 0:
        return True
    warn(f"「{user}」執行 sudo 需要密碼(新版 Raspberry Pi OS 預設)——後面的自動化全靠免密碼 sudo")
    info("  將建立 /etc/sudoers.d/010_pi-nopasswd(即舊版系統的預設設定),先用 visudo 驗過才生效")
    if ask("要設定嗎?", ["[Enter] 設定(請在下方輸入這台的登入密碼,只需這一次)",
                         "[n]     不設定,中止部署"]).lower() == "n":
        report.append(("免密碼 sudo", "使用者拒絕", "後續階段無法自動執行"))
        return False
    line = f"{user} ALL=(ALL) NOPASSWD: ALL"
    remote = (f"echo '{line}' > /tmp/010_pi-nopasswd && sudo visudo -cf /tmp/010_pi-nopasswd "
              f"&& sudo install -m 440 -o root -g root /tmp/010_pi-nopasswd /etc/sudoers.d/010_pi-nopasswd; "
              f"rm -f /tmp/010_pi-nopasswd")
    # 不 capture_output、加 -t:sudo 的密碼提示直接出現在本終端機
    subprocess.run(["ssh", "-t"] + SSH_OPTS + [f"{user}@{host}", remote])
    code, _ = ssh(host, user, "sudo -n true")
    if code == 0:
        ok("免密碼 sudo 已設定")
        report.append(("免密碼 sudo", "已設定", "/etc/sudoers.d/010_pi-nopasswd"))
        return True
    err("免密碼 sudo 設定失敗(密碼錯誤?)——後續階段無法自動執行")
    report.append(("免密碼 sudo", "失敗", "sudo -n 仍需密碼"))
    return False


# ── 進度偵測與評估(斷點續跑 + 交付前總複查共用) ─────────────
def probe_state(host, user):
    """跑 probe_state.sh 撈機器現況,回傳 {KEY: value}(重複 key 收集成 list)。

    一次 SSH 撈完所有標記(約 1-2 秒)。偵測失敗回傳 {}(呼叫端當「未知」處理,
    一律照全新流程跑——偵測只用來省時間,絕不用來決定「不做」某件必要的事)。
    """
    code, out = ssh_script(host, user, os.path.join(SCRIPTS, "probe_state.sh"), timeout=90)
    st = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k):   # 濾掉 stderr 雜訊
            continue
        if k in st:
            st[k] = st[k] + [v] if isinstance(st[k], list) else [st[k], v]
        else:
            st[k] = v
    return st if st.get("PROBE_VERSION") else {}

def _i(st, k, default=-1):
    try:
        return int(st.get(k, ""))
    except (TypeError, ValueError):
        return default

def _t(st, k):   # 遠端 python 印出的布林是 "True"/"False"/"None"
    return st.get(k) == "True"

def _on(st, k):  # shell 的 1/0 旗標
    return st.get(k) == "1"

# 必須與 apply_agh_config.py 的 STANDARD_USER_RULES 一致(那支 import 時需要 yaml,本機不一定有,故不直接 import)
STANDARD_USER_RULES = [
    "||bytewlb.akadns.net^",
    "@@||blogspot.com^$important",
    "@@||youtube.com^$important",
    "@@||youtube-nocookie.com^$important",
    "@@||googlevideo.com^$important",
    "@@||ytimg.com^$important",
    "@@||tiktok.com^$important",
    "@@||tiktokcdn.com^$important",
    "@@||tiktokv.com^$important",
    "@@||ibytedtos.com^$important",
    "@@||blogger.googleusercontent.com^$important",
    "@@||imgur.com^$important",
    "@@||imgbb.com^$important",
    "@@||ibb.co^$important",
    "@@||postimages.org^$important",
    "@@||sm.ms^$important",
]


def _has_std_rules(st):
    try:
        have = set(json.loads(st.get("C_USERRULES") or "[]"))
    except (ValueError, TypeError):
        return False
    return all(r in have for r in STANDARD_USER_RULES)


def latest_agh_version():
    """查 AGH 官方最新穩定版 tag(如 v0.107.79);失敗回 None——沒網路不該讓部署或稽核整個失敗。"""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/AdguardTeam/AdGuardHome/releases/latest",
            headers={"User-Agent": "pi-adguard-setup"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r).get("tag_name")
    except Exception:
        return None


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _plist(st, k):
    v = st.get(k)
    return [] if v in (None, "") else (v if isinstance(v, list) else [v])

def evaluate(st):
    """把 probe 結果評成分階段檢查清單。

    回傳 [(stage_key, 名稱, ok, 細節), …];ok = True 通過 / False 未完成 / None 純資訊。
    stage_key 對應可補跑的階段(harden / agh / calibrate / healthcheck),
    wifi 與 dns 為驗證項(wifi 刻意不算失敗——機器常是帶去別處才設)。
    """
    checks = []

    # 系統強化:逐項對照 harden_pi.sh 的痕跡,缺哪項直接講明
    nets, deco = _i(st, "H_NETCONNS", 0), _i(st, "H_DNSDECOUPLE", 0)
    cfgn = _i(st, "H_CFGTXT", 0)
    items = [
        ("earlyoom", _on(st, "H_EARLYOOM")),
        ("journald 50M", _on(st, "H_JOURNAL")),
        ("sysctl 調校", _on(st, "H_SYSCTL") and _i(st, "H_SWAPPINESS", 0) == 100),
        ("config.txt 三項", cfgn >= 3),
        ("Wi-Fi 省電關閉", _on(st, "H_PWRSAVE")),
        ("DNS 解耦", nets > 0 and deco >= nets),
        ("不自動重開機", _on(st, "H_NOREBOOT")),
        ("dnsutils", _on(st, "H_DIG")),
    ]
    miss = [n for n, o in items if not o]
    if len(miss) >= len(items):
        h_detail = "尚未套用(全新機器)"
    else:
        h_detail = (f"config.txt {cfgn}/3 · DNS解耦 {deco}/{nets} · swappiness {st.get('H_SWAPPINESS','?')}"
                    + ("" if not miss else " · 缺:" + "、".join(miss)))
    checks.append(("harden", "系統強化", not miss, h_detail))

    # AdGuard Home 安裝(含設定精靈是否走完)
    a_ok = _on(st, "A_BIN") and _on(st, "A_WIZARD") and _on(st, "A_ACTIVE")
    if not _on(st, "A_BIN"):
        a_detail = "尚未安裝"
    else:
        a_miss = ([] if _on(st, "A_WIZARD") else ["設定精靈未完成"]) + \
                 ([] if _on(st, "A_ACTIVE") else ["服務未執行"])
        a_detail = (f"{st.get('A_VER','?')} · 服務{'active' if _on(st,'A_ACTIVE') else '未執行'}"
                    + ("" if not a_miss else " · " + "、".join(a_miss)))
    checks.append(("agh", "AdGuard Home", a_ok, a_detail))

    # 設定校準:逐項比對標準值
    if st.get("C_PARSE") != "1":
        checks.append(("calibrate", "校準設定", False, "尚未安裝 AGH 或設定檔讀不到"))
    else:
        fon = _i(st, "C_FILTERS_ON", 0)
        citems = [
            (f"啟用清單 {fon}", fon >= 14),
            ("ratelimit=50", st.get("C_RATELIMIT") == "50"),
            ("樂觀快取", _t(st, "C_OPTIMISTIC")),
            ("DNSSEC", _t(st, "C_DNSSEC")),
            ("querylog 不寫檔", st.get("C_QLFILE") == "False"),
            ("清單12h更新", st.get("C_UPDINT") == "12"),
            ("Cloudflare 上游", "cloudflare" in (st.get("C_UPSTREAM") or "").lower()),
            ("自訂規則", _has_std_rules(st)),
            ("備援鏈", _i(st, "C_FALLBACK", 0) >= 2),
            ("bootstrap", _i(st, "C_BOOTSTRAP", 0) >= 2),
            ("安全瀏覽", _t(st, "C_SAFEBROWSING")),
            ("防護啟用", _t(st, "C_PROTECTION")),
        ]
        cmiss = [n for n, o in citems if not o]
        checks.append(("calibrate", "校準設定", not cmiss,
                       f"清單 {fon} 個啟用 · ratelimit {st.get('C_RATELIMIT','?')}"
                       + ("" if not cmiss else " · 缺:" + "、".join(cmiss))))

    # 自癒健康檢查
    hc = _on(st, "HC_SCRIPT") and _on(st, "HC_TIMER")
    checks.append(("healthcheck", "自癒健康檢查", hc,
                   "timer active(每 2 分鐘)" if hc else "未安裝或 timer 未啟動"))

    # 正式 Wi-Fi:資訊性(刻意不算失敗——常是帶到部署地才設)
    profs = _plist(st, "W_PROFILE")
    if profs:
        desc = " · ".join(f"{p.split('|')[0]}{'(密碼✓)' if p.split('|')[1:2] == ['1'] else '(無密碼)'}"
                          for p in profs)
    else:
        desc = "無 Wi-Fi 連線設定檔(僅有線?)"
    checks.append(("wifi", "Wi-Fi 設定檔", None, desc))

    # DNS 實際功能
    resolve = (st.get("D_RESOLVE") or "").strip()
    r_ok = bool(resolve and resolve[0].isdigit())
    b_ok = st.get("D_BLOCK") == "1"
    checks.append(("dns", "DNS 功能", r_ok and b_ok,
                   f"解析 {resolve or '失敗'} · 阻擋 {'✓' if b_ok else ('未擋' if st.get('D_BLOCK')=='0' else '測不到')}"))

    # 時間同步:資訊性但要顯眼——Pi 無 RTC,斷電後時鐘不準會讓所有 DoH 上游 TLS 驗證
    # 失敗,症狀是「DNS 全掛」,很容易被誤診成 DNS 設定問題。早點看到才不會查錯方向。
    # (刻意不算失敗:壓測 A7 才是硬性把關,這裡只求診斷時第一眼就看得到)
    ntp = (st.get("M_NTP") or "").strip()
    checks.append(("ntp", "時間同步", None,
                   "NTP 已同步" if ntp in ("yes", "1")
                   else f"⚠️ NTP={ntp or '?'} —— 時鐘不準會讓 DoH 上游全部失效,先確認這項"))

    # 規則總量 vs 五年運行閥門:沒有這個數字,「突破 150 萬移除 OISD Big」只是文件上的
    # 約定,實際沒人會去數。列成每次都看得到的指標,年檢時也能逐次比對增長趨勢。
    rules = _i(st, "C_RULES_TOTAL", -1)
    if rules >= 0:
        over = rules >= RULES_VALVE
        checks.append(("rules", "規則總量", None,
                       f"{rules:,} 條(五年閥門 {RULES_VALVE:,})"
                       + (" ← 已超標,依五年策略應移除 OISD Big" if over else "")))

    # 磁碟餘裕:清單快取與 statistics 會長期增長,滿了 AGH 連寫設定/更新清單都會失敗
    disk = _i(st, "M_DISKFREE_MB", -1)
    if disk >= 0:
        checks.append(("disk", "磁碟餘裕", None,
                       f"可用 {disk}MB" + (" ← 偏低,清理或換大卡" if disk < 1024 else "")))
    return checks

def _pad(s, width=16):
    """中日韓字元佔兩格,用顯示寬度對齊(直接用 :<14 會歪掉)。"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in s)
    return s + " " * max(1, width - w)

def print_checks(checks):
    for _, name, ok, detail in checks:
        mark = "✅" if ok else ("ℹ️ " if ok is None else "❌")
        color = C.G if ok else (C.B if ok is None else C.R)
        print(f"  {color}{mark} {_pad(name)}{C.E}{detail}")

# ── 各階段 ───────────────────────────────────────────────────
def stage_flash(report):
    hdr("步驟 1/13 · 燒錄 SD 卡(GUI 引導)")
    # 中斷後重跑的入口:卡早就燒好了,不必再走一次燒錄引導(Enter 維持原本行為)
    if ask("這是全新的卡、需要燒錄嗎?",
           ["[Enter] 是,帶我燒錄",
            "[s]     否,卡已燒好,接續部署"]).lower() == "s":
        info("跳過燒錄——直接連線並偵測這台機器目前的進度")
        report.append(("燒錄 SD 卡", "跳過(卡已燒好)", "接續先前的部署"))
        return
    info("已幫你開好 Raspberry Pi Imager,依以下設定燒錄:")
    info("  1. 作業系統 → 選「Raspberry Pi OS (other)」→「Lite (64-bit)」← 千萬別選桌面版")
    info("  2. 儲存裝置 → 用『容量』對照選對那張卡(選錯會清空別的硬碟!)")
    info("  3. 點右下齒輪 ⚙ 進階選項,設定:")
    info("     • 主機名稱:自訂(例 raspberrypi;多台部署建議每台不同好分辨)")
    info("     • 使用者/密碼:自訂(等下腳本會問你這個帳號)")
    info("     • Wi-Fi:填【你現在這個位置連得到的】Wi-Fi(讓機器一開機就找得到;")
    info("       部署地的正式 Wi-Fi 之後階段再加)")
    info("     • SSH:選『使用密碼登入』(公鑰欄位常失效,開機後腳本會就地幫你補金鑰)")
    info("     • 地區設定:時區選 Asia/Taipei")
    subprocess.run(["open", "-a", "Raspberry Pi Imager"], capture_output=True)
    pause("燒錄完成、把卡插回樹莓派、接上電源開機")
    report.append(("燒錄 SD 卡", "完成(使用者確認)", "Raspberry Pi OS Lite 64-bit"))

def _connected(host, user, report, note=""):
    _, hn = ssh(host, user, "hostname")
    ok(f"連線成功:{user}@{host}({hn.strip()})")
    report.append(("連線", "金鑰登入成功" + (f"({note})" if note else ""),
                   f"{user}@{host} · {hn.strip()}"))
    return host, user

def _interactive_login(host, user, report):
    """金鑰登入失敗時的帳密後援:當場輸入帳號密碼登入,並把金鑰裝上去。

    **帳號可以當場改掉是這裡的重點**。每台機器的帳號都不同,而 Raspberry Pi Imager
    進階選項的「使用者名稱」欄位會沿用上一次燒錄的值——打錯帳號與打錯密碼的症狀
    一模一樣(都是 Permission denied),但處方完全不同。原本帳號只在流程最開頭問一次,
    打錯就只能整個重來;現在每次重試都能改。

    回傳 (是否成功, 實際可用的帳號)。
    """
    # 沒有 SSH 服務就沒有密碼提示可言——先擋掉,別叫使用者去等一個不會出現的提示
    if not _port_open(host):
        warn(f"{host} 的 SSH 服務沒在聽(22 埠拒絕連線)—— 這種狀態不會有密碼提示")
        info("  帳號密碼再正確也沒用,因為連線根本建立不起來。先處理這個:")
        info("  · 機器還在開機(全新卡首次開機要擴充分區,SSH 要 1-3 分鐘才起來)→ 等一下再試")
        info("  · 燒錄時 Imager 進階選項沒勾『啟用 SSH』→ 只能重新燒錄")
        return False, user
    for attempt in range(1, 4):
        print()
        info(f"改用帳號密碼登入 {host}(成功後會自動裝上金鑰,之後就不必再打密碼)")
        u = ask(f"這台的 SSH 帳號是?(燒錄時 Imager 進階選項填的那個)",
                [f"[Enter] 沿用「{user}」",
                 "[直接輸入] 改用別的帳號",
                 "[q]     放棄這台"]) or user
        if u.lower() == "q":           # 別把 q 當成帳號——那幾乎一定是想中止
            info("放棄這台,回到選擇")
            return False, user
        user = u                       # 記住這次輸入的,當下一輪的預設值
        # 密碼由 ssh 自己從終端機讀取,Python 全程不碰
        if _ssh_copy_id(host, u):
            ok("金鑰已裝上")
        okc, out = _probe_ssh(host, u)
        if okc:
            return True, u
        kind = _diagnose_ssh_failure(host, u, out, terse=(attempt > 1))
        if attempt < 3:
            hintmsg = "帳號或密碼再確認一次?" if kind == "auth" else "再試一次?"
            if ask(hintmsg,
                   ["[Enter] 再試一次(可改帳號)",
                    "[s]     放棄這台"]).lower() == "s":
                break
    report.append(("連線", "帳密登入未成功", f"{user}@{host}"))
    return False, user

def stage_connect(host, user, report):
    """回傳 (實際連上的 host, 實際可用的帳號);放棄則回傳 (None, user)。

    帳號會跟著回傳,是因為連線階段允許使用者當場更正帳號(見 _interactive_login),
    更正後的帳號必須讓後續所有階段沿用,否則接下來每一步都會用錯帳號。
    """
    hdr("步驟 2/13 · 等待開機並建立連線")
    scan_mode = host is None   # 沒指定 host → 全程走掃描選機,Enter 預設選候選而非重試
    baseline = set()
    if host:
        # 先試指定的 host(約 60 秒;裝置通常已在線,不必等 3 分鐘)
        info(f"嘗試連線 {host}(最多約 60 秒)…")
        for _ in range(12):
            if _reachable(host, user):
                return _connected(host, user, report)
            time.sleep(5)
        warn(f"{host} 連不上。若是 .local,常見於 mesh 路由器 mDNS 解析不順——改用 IP 最穩。")
    else:
        # 掃描模式:自動輪詢等樹莓派上線(全新機器開機需 1-2 分鐘,不逼使用者一直手動)
        # 先記下等待前就已在線的機器(多半是服役中的舊機):等下候選排序時排到後面,
        # 讓「等待期間才上線」的新機——幾乎必然就是這次要部署的目標——排最前面。
        baseline = {ip for ip, _ in scan_pi_candidates()}
        info("掃描區網、等待樹莓派上線(全新機器開機約 1-2 分鐘,請稍候…)")
        for i in range(30):  # 最多約 2.5 分鐘
            if scan_pi_candidates():
                ok("偵測到樹莓派上線")
                break
            if i and i % 4 == 0:
                info(f"  仍在掃描中({i*5} 秒)——機器還在開機,請再等等")
            time.sleep(5)
    while True:
        cands = scan_pi_candidates()
        if scan_mode:
            # 新上線的排前面(0),等待前就在線的舊機排後面(1)——同一層再依 IP 排序求穩定
            cands.sort(key=lambda c: (0 if c[0] not in baseline else 1, c[0]))
        if cands:
            names = _mdns_hostnames([ip for ip, _ in cands])
            info("區網掃到這些樹莓派候選(有 hostname 直接認名字;沒有的對照分享器 app 的 MAC):")
            for i, (ip, mac) in enumerate(cands, 1):
                tag = "  🆕 新上線" if (scan_mode and ip not in baseline) else ""
                hn = names.get(ip) or "(hostname 查不到)"
                info(f"    [{i}] {ip:<15}  {hn:<16}  MAC {mac}{tag}")
        else:
            info("ARP 暫時掃不到樹莓派,請從分享器 app 讀 IP 後直接輸入")
        opts = []
        if host:
            opts.append(f"[Enter] 重試 {host}")
        elif cands:
            opts.append(f"[Enter] 選 [1] {cands[0][0]}" + ("(新上線,通常就是這台)" if scan_mode and cands[0][0] not in baseline else ""))
        else:
            opts.append("[Enter] 重新掃描")
        if cands:
            opts.append("[編號] 選上面清單裡的其他台")
        opts.append("[直接輸入] IP 或 hostname")
        if scan_mode:
            opts.append("[r] 重新掃描")
        opts.append("[p] 強制用帳號密碼登入並重設金鑰(換機重用同一個 IP 時用這個)")
        opts.append("[q] 放棄")
        choice = ask("要連哪一台?", opts)
        if choice.lower() == "q":
            err("使用者放棄連線"); return None, user
        if scan_mode and choice.lower() == "r":
            continue           # 手動重新掃描(候選已存在時 Enter 不再代表這個動作)
        if choice.lower() == "p":
            # 明確的逃生門:直接走帳密登入,不必先讓金鑰失敗一輪
            t = ask("要用帳密登入哪一台?",
                    ["[Enter] 取消,回到選單",
                     "[編號/IP] 指定機器"])
            if t.isdigit() and cands and 1 <= int(t) <= len(cands):
                t = cands[int(t) - 1][0]
            if not t:
                continue
            okp, user = _interactive_login(t, user, report)
            if okp:
                return _connected(t, user, report, note="帳密登入後重設金鑰")
            continue
        if not choice:
            if host:
                target = host          # 有指定 host → Enter 重試它
            elif cands:
                target = cands[0][0]   # 掃描模式 → Enter 預設選排最前面的候選(推薦新上線的那台)
            else:
                continue               # 沒候選也沒 host → Enter 重新掃描
        elif choice.isdigit() and cands and 1 <= int(choice) <= len(cands):
            target = cands[int(choice) - 1][0]   # 用編號選候選
        else:
            target = choice        # 直接輸入的 IP 或 hostname
        info(f"連線 {target} …")
        okc, out = _probe_ssh(target, user)
        if okc:
            return _connected(target, user, report, note="掃描選機")
        # 先講清楚是哪一種失敗——四種長得像但處方完全不同(指紋/帳號/沒起來/逾時)
        kind = _diagnose_ssh_failure(target, user, out)
        # SSH 服務還沒起來:開機中很常見,自動等它起來,不要叫使用者一直按 Enter 重試
        if kind in ("refused", "timeout"):
            if ask("要自動等 SSH 起來嗎?",
                   ["[Enter] 等(最多 3 分鐘,開機中很常見)",
                    "[s]     不等,回選單"]).lower() == "s":
                continue
            if not _wait_for_ssh(target):
                err("等了 3 分鐘 SSH 還是沒起來 —— 這通常不是網路問題,是機器上沒有 SSH:")
                info("  · 燒錄時 Imager 進階選項沒勾『啟用 SSH』(最常見)→ 只能重新燒錄那張卡")
                info("  · 或這個 IP 上的裝置不是你要部署的那台 → 對照分享器 app 的 MAC 確認")
                continue
            okc, out = _probe_ssh(target, user)
            if okc:
                return _connected(target, user, report, note="等 SSH 起來後")
            kind = _diagnose_ssh_failure(target, user, out)   # 服務起來了,再看是哪種認證問題
        # 金鑰登入失敗(新機沒公鑰、或帳號打錯)→ 直接落到帳密登入,帳號當場可改
        if kind != "auth":
            warn(f"{target} 用金鑰連不上——若確定它在線,多半是金鑰還沒設定")
        okc2, user = _interactive_login(target, user, report)
        if okc2:
            return _connected(target, user, report, note="帳密登入後裝金鑰")
        warn("仍連不上,回到選擇(可換編號/IP、p=再試帳密登入,或 q 放棄)")

RESUMABLE = {"harden": "系統強化", "agh": "安裝 AdGuard Home", "calibrate": "校準設定"}

# 可「選擇性重設」的項目(比 RESUMABLE 多 healthcheck/wifi)。
# harden/calibrate/healthcheck 本來就是冪等覆寫腳本,直接重跑即等於還原,不需要額外
# 的「先復原」動作;agh/wifi 則是真正的破壞性操作(見 _select_reset_items)。
RESETTABLE = {
    "harden": "系統強化",
    "calibrate": "校準設定",
    "healthcheck": "自癒健康檢查",
    "agh": "AdGuard Home(重灌,含管理員帳密與統計)",
    "wifi": "Wi-Fi 連線設定檔(重新設定)",
}

def _select_reset_items(host, user, present, report):
    """互動選擇要「還原重設」的項目。

    回傳應從 done 集合排除、讓主流程重跑的 key 集合。agh/wifi 屬破壞性動作,
    在這裡就地執行實際還原(解除安裝/刪連線檔),需輸入 yes 二次確認;
    healthcheck 若單獨選取(未連動 calibrate)也在這裡直接重跑,因為主流程
    平常只在 stage_calibrate 內順帶安裝它,沒有獨立的重跑入口。
    """
    order = [k for k in ("harden", "calibrate", "healthcheck", "agh", "wifi") if k in present]
    if not order:
        warn("目前沒有已完成的項目可重設")
        return set()
    print()
    info("可重設的項目(僅列出這台機器目前已完成的):")
    for i, k in enumerate(order, 1):
        info(f"    [{i}] {RESETTABLE[k]}")
    raw = ask("要重設哪幾項?",
              ["[Enter] 取消,不重設",
               "[編號]  可多選,以逗號分隔(如 1,3)"])
    if not raw:
        info("取消重設")
        return set()
    idxs = [int(p) - 1 for p in raw.split(",") if p.strip().isdigit() and 1 <= int(p.strip()) <= len(order)]
    sel = {order[i] for i in idxs}
    if not sel:
        warn("輸入無法辨識,取消重設")
        return set()

    if "agh" in sel and "calibrate" not in sel:
        info("AGH 重灌會一併清掉校準設定與自癒健康檢查,已自動一併選取「校準設定」")
        sel.add("calibrate")

    destructive = [k for k in ("agh", "wifi") if k in sel]
    if destructive:
        warn("以下項目將實際清除既有設定,清掉後無法復原:")
        if "agh" in sel:
            warn("  · AGH 重灌 —— 管理員帳密、統計資料、目前的校準設定全部清空,稍後需重新走設定精靈")
        if "wifi" in sel:
            warn("  · Wi-Fi 重設 —— 選定的連線設定檔會被刪除,稍後需重新輸入 SSID/密碼")
        if ask("確定執行?",
               ["[yes]  確定,執行清除",
                "[其他] 取消"]).lower() != "yes":
            info("取消重設")
            return set()

    if "agh" in sel:
        info("停止並解除安裝 AGH…")
        ssh(host, user, "sudo systemctl stop AdGuardHome 2>/dev/null; "
                        "sudo /opt/AdGuardHome/AdGuardHome -s uninstall 2>/dev/null; "
                        "sudo rm -rf /opt/AdGuardHome", timeout=60)
        ok("AGH 已解除安裝——稍後將重新安裝並走設定精靈")
        report.append(("重設項目", "AGH 重灌", "已解除安裝,將重新安裝並走設定精靈"))

    if "healthcheck" in sel and "calibrate" not in sel:
        info("重新安裝自癒健康檢查…")
        code3, _ = ssh_script(host, user, os.path.join(SCRIPTS, "install_agh_healthcheck.sh"), timeout=60)
        (ok if code3 == 0 else err)("自癒健康檢查重新安裝" + ("完成" if code3 == 0 else "失敗"))
        report.append(("重設項目", "自癒健康檢查", "完成" if code3 == 0 else "失敗"))

    if "wifi" in sel:
        existing = _wifi_profiles(host, user)
        _, active_out = ssh(host, user,
            "nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | grep wireless | cut -d: -f1")
        # 只認得「已知設定檔名單裡」的名字:ssh() 會把 stderr 混進輸出,nmcli 的
        # Warning 行若被當成 active_name,保護機制會靜默失效(誤刪 SSH 使用中的連線)
        known = {name for name, _, _ in existing}
        active_name = next((l.strip() for l in active_out.splitlines() if l.strip() in known), None)
        if not existing:
            info("目前沒有 Wi-Fi 連線設定檔可刪")
        else:
            info("目前的 Wi-Fi 連線設定檔:")
            for i, (name, ssid, has) in enumerate(existing, 1):
                tag = "  ← 目前 SSH 使用中,無法在此刪除" if name == active_name else ""
                info(f"    [{i}] {name}(SSID {ssid or '?'}){tag}")
            raw2 = ask("要刪除哪幾組?",
                       ["[Enter] 全選",
                        "[編號]  可多選,以逗號分隔(如 1,2)"])
            if raw2:
                widxs = [int(p) - 1 for p in raw2.split(",")
                         if p.strip().isdigit() and 1 <= int(p.strip()) <= len(existing)]
            else:
                widxs = list(range(len(existing)))
            targets = [existing[i][0] for i in widxs]
            if active_name in targets:
                warn(f"「{active_name}」是目前 SSH 使用中的連線,刪除會立刻斷線導致部署中斷——已自動排除")
                info("  (如需清掉這組,請留到步驟 13 收尾階段——屆時會先排程關機再刪,不怕斷線)")
                targets = [t for t in targets if t != active_name]
            for name in targets:
                ssh(host, user, f"sudo nmcli connection delete '{name}'")
                ok(f"已刪除連線設定檔:{name}")
            report.append(("重設項目", "Wi-Fi 重設", f"已刪除:{', '.join(targets) if targets else '無'}"))

    return sel

def stage_probe(host, user, report, no_resume=False):
    """偵測這台機器目前做到哪,回傳「可跳過的階段 key 集合」。

    全新機器(什麼都沒做)不會多問任何一句,直接往下——新機部署體感與過去完全一致。
    只有偵測到「已完成的重階段」時才停下來問要不要跳過。
    """
    hdr("步驟 3/13 · 進度偵測(這台機器目前做到哪)")
    st = probe_state(host, user)
    if not st:
        warn("偵測不到機器狀態(舊系統或權限問題)——照全新流程從頭跑,結果一樣正確")
        report.append(("進度偵測", "無法偵測", "照全新流程從頭執行"))
        return set()
    if st.get("M_MODEL"):
        info(f"機型:{st['M_MODEL']} · 開機時間 {st.get('M_UPTIME','?')}")
    checks = evaluate(st)
    print_checks(checks)
    if _i(st, "P_HISTORY_N", 0) > 0:
        info(f"壓測歷史 {st['P_HISTORY_N']} 筆,最近一次:{st.get('P_HISTORY','')[:90]}")

    done = {k for k, _, ok, _ in checks if ok and k in RESUMABLE}
    if not done:
        ok("全新機器(尚未套用任何設定)—— 從頭開始")
        report.append(("進度偵測", "全新機器", "從頭執行全部階段"))
        return set()
    names = "、".join(RESUMABLE[k] for k in RESUMABLE if k in done)
    if no_resume:
        warn(f"偵測到已完成:{names}(--no-resume:仍全部重跑)")
        report.append(("進度偵測", "已完成部分階段", f"{names}(--no-resume 全部重跑)"))
        return set()
    print()
    warn(f"偵測到這台機器已經完成:{names}")
    choice = ask("接下來怎麼跑?",
                 ["[Enter] 跳過已完成的階段,從第一個未完成的接續(推薦,省下重跑時間)",
                  "[a]     全部重跑一遍(腳本冪等、不會弄壞,只是多花幾分鐘)",
                  "[r]     選擇要還原重設的項目(忘記 AGH 密碼、Wi-Fi 設錯要清掉重來…)"]).lower()
    if choice == "a":
        info("選擇全部重跑")
        report.append(("進度偵測", "已完成部分階段(選擇全部重跑)", names))
        return set()
    if choice == "r":
        resettable_present = {k for k, _, ok, _ in checks if ok and k in RESETTABLE}
        if _plist(st, "W_PROFILE"):
            resettable_present.add("wifi")
        reset_keys = _select_reset_items(host, user, resettable_present, report)
        if not reset_keys:
            ok(f"未重設任何項目——接續部署,跳過:{names}")
            report.append(("進度偵測", "接續先前部署(取消重設)", f"跳過已完成:{names}"))
            return done
        remaining_done = done - reset_keys
        remaining_names = "、".join(RESUMABLE[k] for k in remaining_done) or "無"
        reset_names = "、".join(RESETTABLE[k] for k in reset_keys)
        ok(f"已重設:{reset_names} —— 其餘接續跳過:{remaining_names}")
        report.append(("進度偵測", "選擇性重設", f"重設:{reset_names}"))
        return remaining_done
    ok(f"接續部署——跳過:{names}")
    report.append(("進度偵測", "接續先前部署", f"跳過已完成:{names}"))
    return done

def stage_harden(host, user, report):
    hdr("步驟 4/13 · 系統強化")
    info("執行系統強化(earlyoom/journald/Wi-Fi/gpu_mem/藍牙/DNS解耦/SD卡批次化…)")
    # --no-nm-restart:避免透過 Wi-Fi 部署時 NM 重啟斷線(powersave 於尾端 reboot 生效)
    code, out = ssh_script(host, user, os.path.join(SCRIPTS, "harden_pi.sh"),
                           args="--no-nm-restart", timeout=300)
    tail = "\n".join(out.strip().splitlines()[-8:])
    if code == 0:
        ok("系統強化完成")
        print(f"{C.B}{tail}{C.E}")
        report.append(("系統強化", "完成", "9 步驟全數套用(見報告末尾驗證區)"))
    else:
        err(f"系統強化異常(exit {code})")
        report.append(("系統強化", f"異常 exit {code}", tail[:200]))
    return code == 0

AGH_TAR_URL = "https://static.adguard.com/adguardhome/release/AdGuardHome_linux_arm64.tar.gz"


def _agh_upgrade(host, user, report):
    """已安裝的 AGH 比對官方最新穩定版,較舊就詢問後升級(換執行檔,設定與資料不動)。

    過去已安裝就直接跳過,服役機器永遠停在部署當時的版本;只有 audit_fleet 會回報落後。
    升級失敗自動換回舊執行檔,不留半套。停服務期間 DNS 中斷約 10~20 秒。
    """
    _, out = ssh(host, user, "/opt/AdGuardHome/AdGuardHome --version 2>/dev/null")
    cur = (re.findall(r"v\d+\.\d+\.\d+", out) or [None])[0]
    latest = latest_agh_version()
    if not cur or not latest:
        info(f"AGH 版本比對略過(目前 {cur or '?'} · 最新 {latest or '查不到'})")
        return
    if _ver_tuple(cur) >= _ver_tuple(latest):
        ok(f"AGH 已是最新穩定版 {cur}")
        return
    warn(f"AGH {cur} 落後最新穩定版 {latest}")
    if ask("要升級嗎?(DNS 會中斷約 10~20 秒,設定與清單不動)",
           ["[Enter] 升級(推薦)", "[n]     先不升"]).lower() == "n":
        report.append(("AGH 升級", "略過(使用者選擇)", f"{cur} → 最新 {latest}"))
        return
    cmd = (f"set -e; cd /tmp && rm -rf agh-up && mkdir agh-up && cd agh-up "
           f"&& curl -fsSL -o agh.tar.gz {AGH_TAR_URL} && tar -xzf agh.tar.gz "
           f"&& ./AdGuardHome/AdGuardHome --version "
           f"&& sudo systemctl stop AdGuardHome "
           f"&& sudo cp -p /opt/AdGuardHome/AdGuardHome /opt/AdGuardHome/AdGuardHome.bak-{cur} "
           f"&& sudo install -m 755 ./AdGuardHome/AdGuardHome /opt/AdGuardHome/AdGuardHome "
           f"&& sudo systemctl start AdGuardHome && cd /tmp && rm -rf agh-up")
    code, out = ssh(host, user, cmd, timeout=240)
    _, now = ssh(host, user, "/opt/AdGuardHome/AdGuardHome --version; systemctl is-active AdGuardHome")
    if code == 0 and latest in now and "active" in now.split():
        ok(f"AGH 已升級 {cur} → {latest}(舊執行檔留在 AdGuardHome.bak-{cur})")
        report.append(("AGH 升級", "完成", f"{cur} → {latest}"))
        return
    err(f"AGH 升級失敗:{out.strip()[-200:]}")
    ssh(host, user, f"test -f /opt/AdGuardHome/AdGuardHome.bak-{cur} "
                    f"&& sudo install -m 755 /opt/AdGuardHome/AdGuardHome.bak-{cur} /opt/AdGuardHome/AdGuardHome; "
                    f"sudo systemctl start AdGuardHome")
    _, now = ssh(host, user, "systemctl is-active AdGuardHome")
    warn(f"已換回 {cur},服務狀態:{now.strip()}")
    report.append(("AGH 升級", "失敗已還原", f"{cur} 維持;{out.strip()[-120:]}"))


def stage_install_agh(host, user, report):
    hdr("步驟 5/13 · 安裝 AdGuard Home(最新穩定版)")
    # 檢查 53 埠
    _, port = ssh(host, user, "sudo ss -tulnp | grep ':53 ' || echo FREE")
    if "FREE" not in port and "AdGuardHome" not in port:
        warn("53 埠已被其他服務占用,可能需要先處理(常見 systemd-resolved)")
    # 已裝過就跳過安裝
    code, ver = ssh(host, user, "/opt/AdGuardHome/AdGuardHome --version 2>/dev/null || echo NONE")
    if "AdGuard Home" in ver:
        ok(f"AGH 已安裝:{ver.strip()}")
        _agh_upgrade(host, user, report)
    else:
        info("下載官方 arm64 最新穩定版並安裝為 systemd 服務…")
        cmd = ("cd /tmp && curl -fSL -o agh.tar.gz "
               f"{AGH_TAR_URL} "
               "&& tar -xzf agh.tar.gz && sudo mv AdGuardHome /opt/ "
               "&& cd /opt/AdGuardHome && sudo ./AdGuardHome -s install")
        code, out = ssh(host, user, cmd, timeout=180)
        if code != 0:
            err("AGH 安裝失敗"); report.append(("安裝 AGH", "失敗", out[:200])); return False
        _, ver = ssh(host, user, "/opt/AdGuardHome/AdGuardHome --version")
        ok(f"AGH 安裝完成:{ver.strip()}")
    # 設定精靈(全新才需要)
    _, hasusers = ssh(host, user, "sudo grep -q '^users:' /opt/AdGuardHome/AdGuardHome.yaml && echo HASUSERS || echo NEW")
    if "NEW" in hasusers:
        warn("這是全新安裝,需在瀏覽器完成設定精靈設定管理員帳密")
        info("  我已幫你開好設定頁;依精靈設定,DNS/Web 埠保持預設即可")
        info("  請自行設定管理員帳號與獨立密碼")
        subprocess.run(["open", f"http://{host}:3000"], capture_output=True)  # 自動開瀏覽器
        # 按 Enter 不代表精靈真的走完了——沒走完 yaml 就不存在,下一步校準必炸 traceback
        # (曾遇到:精靈沒完成就按了 Enter,校準連失敗兩輪)。這裡驗證到真的完成為止。
        while True:
            pause("設定精靈完成(瀏覽器看到主控台就是好了)")
            _, chk = ssh(host, user,
                "sudo grep -q '^users:' /opt/AdGuardHome/AdGuardHome.yaml 2>/dev/null && echo DONE || echo NOTYET")
            if "DONE" in chk:
                ok("設定精靈已完成,設定檔已生成")
                break
            warn("偵測不到完成後的設定檔——設定精靈其實還沒走完(還沒設定管理員帳密)")
            info(f"  請回瀏覽器 http://{host}:3000 把精靈走到「看到主控台登入畫面」,好了再按 Enter")
    report.append(("安裝 AGH", "完成", ver.strip()))
    return True

def stage_calibrate(host, user, has_ipv6, report):
    hdr("步驟 6/13 · 校準 AGH 設定")
    # 上傳並執行校準腳本
    # 校準前先確認設定檔存在:設定精靈沒走完就校準,只會換來一句 FileNotFoundError
    # 的 traceback,使用者看不懂也不知道該做什麼——在這裡就把話講明白
    _, ycheck = ssh(host, user, "sudo test -f /opt/AdGuardHome/AdGuardHome.yaml && echo YES || echo NO")
    if "YES" not in ycheck:
        err("AdGuardHome.yaml 不存在——設定精靈還沒完成,無法校準")
        info(f"  請先在瀏覽器完成設定精靈(http://{host}:3000,走到看到主控台),再重跑此階段")
        report.append(("校準設定", "失敗", "設定檔不存在(設定精靈未完成)"))
        return False
    subprocess.run(["scp", "-o", "BatchMode=yes", os.path.join(SCRIPTS, "apply_agh_config.py"),
                    f"{user}@{host}:/tmp/"], capture_output=True)
    flag = "--has-ipv6" if has_ipv6 else ""
    # start 放在 && 鏈之外:校準失敗時服務也必須拉回來,否則 AGH 停著不動、
    # 整台 DNS 直接死到下次重試(exit $RC 保留校準本身的成敗給上層判讀)
    code, out = ssh(host, user,
                    f"sudo systemctl stop AdGuardHome && sudo python3 /tmp/apply_agh_config.py {flag}; RC=$?; "
                    f"sudo systemctl start AdGuardHome; exit $RC", timeout=120)
    if code == 0:
        line = next((l for l in out.splitlines() if "過濾清單" in l), "")
        ok(f"校準完成 {line.strip()}")
        report.append(("校準設定", "完成", "14清單/上游備援鏈/樂觀快取/querylog記憶體化/ratelimit=50"))
    else:
        err("校準失敗"); report.append(("校準設定", "失敗", out[:200])); return False
    # 自癒健康檢查
    info("安裝自癒健康檢查(殭屍狀態自動重啟)…")
    code2, _ = ssh_script(host, user, os.path.join(SCRIPTS, "install_agh_healthcheck.sh"), timeout=60)
    if code2 == 0:
        ok("自癒健康檢查已安裝")
        report.append(("自癒健康檢查", "已安裝", "每 2 分鐘檢查,殭屍狀態自動重啟"))
    return True

def _wifi_profiles(host, user):
    """列出現有 Wi-Fi 連線設定檔 → [(連線名, SSID, 是否已設密碼)]。

    重要:NetworkManager **允許同名連線並存**(唯一鍵是 UUID 不是名稱),重複 `add`
    不會報錯,而是產生兩個同名設定檔——之後 `modify <名稱>` 只改得到其中一個,另一個
    沒密碼卻照樣 autoconnect,是很難查的隱形故障。所以新增前一定要先比對現況。
    """
    _, out = ssh(host, user,
        "nmcli -t -f NAME,TYPE connection show | grep wireless | cut -d: -f1 | "
        'while IFS= read -r c; do '
        '  s=$(nmcli -g 802-11-wireless.ssid connection show "$c" 2>/dev/null); '
        '  p=$(nmcli connection show "$c" 2>/dev/null | grep -c "psk:.*<hidden>"); '
        '  echo "$c|$s|$p"; done')
    profs = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] and not parts[0].startswith(("ssh", "Warning")):
            profs.append((parts[0], parts[1], parts[2] == "1"))
    return profs

def stage_wifi(host, user, report):
    hdr("步驟 7/13 · 正式 Wi-Fi 設定(可選,可加多組)")
    info("為部署地的網路建立連線設定檔。可加多組(主/備/不同樓層)。密碼由你自己輸入,腳本不經手。")
    info("提示:若這台要帶去別的地方(如父母家)才用,這步可先跳過——到現場後接上有線或")
    info("      直接用該處 Wi-Fi 開機,再補設定更準。現在就在部署地的話,就在這裡加。")
    # 先看現況:重跑/接續部署時可能已經加過,避免重複新增(NM 會允許同名並存)
    existing = _wifi_profiles(host, user)
    if existing:
        print()
        info("這台目前已有的 Wi-Fi 連線設定檔:")
        for name, ssid, has in existing:
            info(f"    • {name}(SSID {ssid or '?'}){'  密碼✓' if has else '  ⚠️ 未設密碼'}")
        info("  下面若輸入已存在的 SSID,會【沿用該設定檔並校正成標準設定】,不會重複新增")
    try:
        n = int(ask("要現在加幾組正式 Wi-Fi?",
                    ["[Enter] 0 組,跳過",
                     "[數字]  要加的組數(主/備/不同樓層可多組)"]) or "0")
    except (ValueError, EOFError):
        n = 0
    if n <= 0:
        info("跳過 Wi-Fi 設定")
        report.append(("正式 Wi-Fi", "跳過", f"未新增(機器上已有 {len(existing)} 組)" if existing else "之後手動加"))
        return
    ssids = []
    for i in range(n):
        s = ask(f"第 {i+1} 組 SSID?(大小寫敏感,務必精確)")
        if not s:
            continue
        if s in ssids:                      # 同一次執行輸入重複,直接擋掉
            warn(f"「{s}」剛才已經輸入過,略過重複")
            continue
        ssids.append(s)
    # 建立/校正連線設定檔(autoconnect + 無限重試 + Wi-Fi metric 600,與 skill 階段 7 一致,不含密碼)
    # ipv4.dns:設定檔要一併做 DNS 解耦——harden 只改「當時已存在」的連線,
    # 這裡新建的若漏掉,到了部署地會從 DHCP 拿到路由器當 DNS,而路由器又指回這台 Pi,
    # 形成 AGH 掛掉時自己也沒 DNS 的自我依賴迴圈(交付前總複查也會抓這項)
    STD = ("connection.autoconnect yes connection.autoconnect-retries 0 "
           "ipv4.route-metric 600 ipv6.route-metric 600 "
           "ipv4.dns '127.0.0.1 9.9.9.10' ipv4.ignore-auto-dns yes")
    by_ssid = {ssid: name for name, ssid, _ in existing if ssid}
    created, reused = [], 0                 # created: [(SSID, 連線名)]
    for s in ssids:
        con = by_ssid.get(s)
        if con:
            # 已有同 SSID 的設定檔(可能是上次部署建的、或燒錄時填的那組)→ 沿用並校正,不重建。
            # 刻意不動 wifi-sec.key-mgmt:若原本是開放網路,硬設 wpa-psk 又跳過密碼會弄壞它
            warn(f"SSID「{s}」已有設定檔「{con}」—— 沿用並校正成標準設定(不重複新增)")
            cmd = f"sudo nmcli connection modify '{con}' {STD}"
        else:
            con = f"wlan0-{s}"
            cmd = (f"sudo nmcli connection add type wifi ifname wlan0 con-name '{con}' ssid '{s}' "
                   f"wifi-sec.key-mgmt wpa-psk {STD}")
        code, out = ssh(host, user, cmd)
        if code == 0:
            if by_ssid.get(s):
                ok(f"連線設定檔已校正:{con}"); reused += 1
            else:
                ok(f"連線設定檔已建立:{con}")
            created.append((s, con))
            by_ssid[s] = con                # 記錄下來,後續同 SSID 一律走沿用路徑
        else:
            err(f"設定 {con} 失敗:{out.strip()[:120]}")
    # 就地輸入每組密碼(明碼可見:機器要拿去別處,當場看清楚確認打對,遠比打錯後到現場才發現連不上好)
    done = []
    if created:
        print()
        info("設定各組密碼:密碼會【顯示】以便你當場確認正確(拿到別處才發現打錯很麻煩)。")
        info("留空=跳過該組(沿用的設定檔若原本已有密碼,留空就是保留原密碼)。")
        info("若在旁人可見處操作、或要截圖,自行留意這幾行。")
    for s, con in created:
        while True:
            pw = ask(f"Wi-Fi「{s}」的密碼?(輸入時會顯示,方便當場核對)",
                     ["[Enter] 跳過這組(沿用的設定檔留空=保留原密碼)"])
            if not pw:
                warn(f"{s}:略過密碼(日後補:nmcli connection modify '{con}' wifi-sec.psk)"); break
            # 密碼經 stdin → 遠端 read,不出現在遠端 ps/history;nmcli 從變數取值
            remote = f'read -r PW && sudo nmcli connection modify "{con}" wifi-sec.psk "$PW"'
            subprocess.run(["ssh"] + SSH_OPTS + [f"{user}@{host}", remote],
                           input=pw + "\n", text=True, capture_output=True)
            _, chk = ssh(host, user, f"nmcli connection show '{con}' 2>/dev/null | grep -c 'psk:.*<hidden>'")
            if chk.strip() == "1":
                ok(f"{s}:密碼已設定 ✓(你輸入的是「{pw}」,對照無誤即可)"); done.append(s); break
            warn(f"{s}:設定沒成功,再輸一次或 Enter 跳過")
    report.append(("正式 Wi-Fi", f"{len(done)}/{len(ssids)} 組完成",
                   (", ".join(ssids) if ssids else "無") + (f"(其中 {reused} 組沿用既有設定檔)" if reused else "")))

def stage_verify(host, user, report):
    hdr("步驟 8/13 · 功能驗證")
    _, g = ssh(host, user, "dig +short +time=5 +tries=2 @127.0.0.1 google.com | head -1")
    normal_ok = bool(g.strip() and g.strip()[0].isdigit())
    if normal_ok: ok(f"正常網域解析:google.com → {g.strip()}")
    else: err("正常網域解析失敗")
    # 阻擋判斷:用 +short(NXDOMAIN 阻擋回空、0.0.0.0 阻擋回 "0.0.0.0",兩種都好判;
    # 舊版 grep|head-1 會把答案行切掉、永遠誤判未擋——2026-07-23 已修)。
    # 且加輪詢重試:全新機器清單首次載入需時間(不只 15 秒),太早測 doubleclick 還沒進黑名單。
    info("驗證廣告阻擋(清單首次載入需一點時間,最多等 60 秒)…")
    block_ok = False
    for _ in range(12):
        _, d = ssh(host, user, "dig +short +time=5 +tries=2 @127.0.0.1 doubleclick.net")
        if (not d.strip()) or ("0.0.0.0" in d):
            block_ok = True; break
        time.sleep(5)
    if block_ok: ok("廣告網域阻擋:doubleclick.net 已擋")
    else: warn("廣告網域仍未擋(等 60 秒仍未阻擋,查清單載入日誌 journalctl -u AdGuardHome)")
    _, mem = ssh(host, user, "free -m | awk '/^Mem:/{print $7}'")
    _, rss = ssh(host, user, "ps -o rss= -C AdGuardHome | head -1 | awk '{print int($1/1024)}'")
    ok(f"記憶體:AGH {rss.strip()}MB / 可用 {mem.strip()}MB")
    report.append(("功能驗證",
                   "通過" if (normal_ok and block_ok) else "部分通過",
                   f"解析✓ 阻擋{'✓' if block_ok else '?'} · AGH {rss.strip()}MB/可用 {mem.strip()}MB"))
    return normal_ok

def _fix_stage(key, host, user, has_ipv6, report):
    """補跑某個未完成的階段(全部腳本冪等,重跑安全)。"""
    if key == "harden":
        return stage_harden(host, user, report)
    if key == "agh":
        return stage_install_agh(host, user, report)
    if key == "calibrate":
        return stage_calibrate(host, user, has_ipv6, report)
    if key == "healthcheck":
        code, _ = ssh_script(host, user, os.path.join(SCRIPTS, "install_agh_healthcheck.sh"), timeout=60)
        (ok if code == 0 else err)("自癒健康檢查補裝" + ("完成" if code == 0 else "失敗"))
        return code == 0
    if key == "dns":
        info("重啟 AdGuardHome 並等待清單載入(最多 60 秒)…")
        ssh(host, user, "sudo systemctl restart AdGuardHome", timeout=60)
        for _ in range(12):
            time.sleep(5)
            _, d = ssh(host, user, "dig +short +time=5 +tries=2 @127.0.0.1 google.com | head -1")
            if d.strip() and d.strip()[0].isdigit():
                return True
        return False
    return False

def stage_precheck(host, user, has_ipv6, report, round_no=1):
    """交付前總複查:壓測前從頭再驗一次,確認所有設定都到位。

    壓測是「驗成品扛不扛得住」,在半套設定上跑 10 分鐘沒有意義(而且事後補完設定又
    等於沒測)。故此關把所有階段的成果重驗一遍;有缺項就地補跑(冪等)再複查,
    全部齊備才進壓測。全通過時只多印幾行綠字,不會多問任何一句。

    回傳 (全部通過?, checks 清單, 是否中止)。
    """
    hdr(f"步驟 9/13 · 交付前總複查(壓測前確認所有設定皆已完成)"
        + (f" · 第 {round_no} 輪" if round_no > 1 else ""))
    st = probe_state(host, user)
    if not st:
        warn("複查探測失敗(SSH 不穩?)——無法確認設定完整性")
        report.append(("交付前總複查", "無法探測", "SSH 探測失敗,未能確認"))
        return False, [], False
    checks = evaluate(st)
    print_checks(checks)
    bad = [(k, n) for k, n, o, _ in checks if o is False]
    if not bad:
        ok("全部設定齊備 —— 進入穩定度測試")
        report.append(("交付前總複查", "✅ 全部通過",
                       f"{sum(1 for c in checks if c[2])} 項檢查全數到位"))
        return True, checks, False

    print()
    warn(f"有 {len(bad)} 項未完成:" + "、".join(n for _, n in bad))
    info("  這些若不補齊就壓測,測到的不是最終成品(事後再補設定又等於沒測)")
    choice = ask("怎麼處理缺項?",
                 ["[Enter] 自動補跑缺的階段,補完再複查一次(推薦,腳本冪等)",
                  "[c]     不管它,直接進行壓測",
                  "[q]     中止部署(先產生報告,排除問題後再跑)"]).lower()
    if choice == "q":
        err("使用者選擇中止")
        report.append(("交付前總複查", "❌ 未通過(使用者中止)",
                       "未完成:" + "、".join(n for _, n in bad)))
        return False, checks, True
    if choice == "c":
        warn("略過補跑,直接壓測")
        report.append(("交付前總複查", "⚠️ 未通過(選擇略過)",
                       "未完成:" + "、".join(n for _, n in bad)))
        return False, checks, False
    if round_no >= 3:
        warn("已補跑兩輪仍有缺項——不再自動重試,請人工排查後單獨補跑對應腳本")
        report.append(("交付前總複查", "⚠️ 補跑後仍有缺項",
                       "未完成:" + "、".join(n for _, n in bad)))
        return False, checks, False
    for key, name in bad:
        info(f"補跑:{name}")
        _fix_stage(key, host, user, has_ipv6, report)
    return stage_precheck(host, user, has_ipv6, report, round_no + 1)

def stage_test(host, user, soak, report):
    hdr("步驟 10/13 · 穩定度測試")
    info(f"預設壓測 {soak} 分鐘(含前置/降溫全程約 {soak+8} 分鐘):CPU 滿載 + DNS 負載 + 溫度/供電/SD卡/記憶體檢查。")
    info("這是交付前驗機器扛不扛得住的重要一關;Pi 3B 約 5-8 分鐘達熱飽和,10 分鐘已完整涵蓋,")
    info("新電源/外殼組合或結果在及格邊緣想複驗可拉長;想快速交付可跳過(日後仍能單獨補跑)。")
    choice = ask("現在跑壓測嗎?",
                 [f"[Enter] 跑,{soak} 分鐘",
                  "[數字]  跑,自訂分鐘數",
                  "[s]     跳過(日後仍能單獨補跑)"])
    if choice.lower() == "s":
        info("已跳過穩定度測試")
        report.append(("穩定度測試", "跳過(當場選擇)", "日後可單獨補跑 preflight_check.sh"))
        return None, ""
    if choice:
        try:
            n = int(choice)
            if n <= 0:
                raise ValueError
            if n < 3:
                warn(f"{n} 分鐘可能測不到熱飽和(Pi 3B 約需 5-8 分鐘),仍照你的設定執行")
            soak = n
        except ValueError:
            warn(f"「{choice}」無法辨識為分鐘數,改用預設 {soak} 分鐘")
    warn(f"開始壓測,約 {soak+8} 分鐘,期間可以去忙別的…")
    code, out = ssh_script(host, user, os.path.join(SCRIPTS, "preflight_check.sh"),
                           args=str(soak), timeout=(soak+12)*60)
    # 解析 preflight 輸出的 PASS/WARN/FAIL
    passes = [l.strip() for l in out.splitlines() if "✅" in l]
    warns  = [l.strip() for l in out.splitlines() if "WARN" in l]
    fails  = [l.strip() for l in out.splitlines() if "FAIL" in l]
    verdict = "🟢 全部通過" if code == 0 else "🔴 有項目未通過"
    print(f"\n{verdict} — 通過 {len(passes)} 項" + (f",WARN {len(warns)} 項" if warns else "") + (f",FAIL {len(fails)} 項" if fails else ""))
    for w in warns: warn(w.replace("⚠️  WARN:", "").strip())
    for f in fails: err(f.replace("❌ FAIL:", "").strip())
    report.append(("穩定度測試", verdict,
                   f"時長 {soak} 分鐘 · 通過 {len(passes)} · WARN {len(warns)} · FAIL {len(fails)}"))
    return code == 0, out

def gen_report(host, user, report, report_path, preflight_raw, soak, tested, precheck=None):
    hdr("步驟 12/13 · 產生完整報告")
    _, sysinfo = ssh(host, user,
        "echo MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'); "
        "echo MAC=$(cat /sys/class/net/*/address 2>/dev/null | grep -iE 'b8:27:eb|dc:a6:32' | head -1); "
        "echo IP=$(hostname -I | awk '{print $1}'); "
        "echo AGHVER=$(/opt/AdGuardHome/AdGuardHome --version 2>/dev/null | grep -oE 'v[0-9.]+'); "
        "echo THROTTLE=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)")
    kv = dict(l.split("=", 1) for l in sysinfo.strip().splitlines() if "=" in l)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 樹莓派 AdGuard Home 部署報告",
        f"",
        f"- **部署時間**:{now}",
        f"- **目標機器**:`{host}`(SSH 使用者 `{user}`)",
        f"- **機型**:{kv.get('MODEL','?')}",
        f"- **IP / MAC**:{kv.get('IP','?')} / {kv.get('MAC','?')}",
        f"- **AGH 版本**:{kv.get('AGHVER','?')}",
        f"- **供電旗標**:{_throttle_verdict(kv.get('THROTTLE','?'))}",
        f"",
        f"## 部署階段結果",
        f"",
        f"| 階段 | 結果 | 說明 |",
        f"|---|---|---|",
    ]
    for stage, result, detail in report:
        lines.append(f"| {stage} | {result} | {detail} |")

    if precheck:
        lines += ["", "## 交付前總複查(壓測前的完整設定盤點)", "",
                  "| 項目 | 狀態 | 細節 |", "|---|---|---|"]
        for _, name, st_ok, detail in precheck:
            mark = "✅ 通過" if st_ok else ("ℹ️ 資訊" if st_ok is None else "❌ 未完成")
            lines.append(f"| {name} | {mark} | {detail} |")

    if tested:
        lines += ["", "## 穩定度測試完整輸出", "", "```", preflight_raw.strip(), "```"]
    else:
        lines += ["", "## 穩定度測試", "", "(本次以 --skip-test 跳過)"]

    lines += [
        "", "## 交付前檢查清單(人工確認)", "",
        "- [ ] 建立正式 Wi-Fi 連線設定檔並輸入密碼(見 skill 階段 7)",
        "- [ ] 路由器 Primary DNS 指向本機 IP",
        "- [ ] 路由器做 DHCP IP 綁定(最大單點風險)",
        "- [ ] 刪除「燒錄時填的安裝測試用 Wi-Fi」連線並關機",
        "      (下面步驟 13 收尾階段會互動詢問並自動處理;以下指令僅供備用/手動情境參考:",
        "      `sudo shutdown -h +1 && sudo nmcli connection delete <該連線名>`,先排程關機再刪避免斷線)",
        "",
        "**只關機、先不刪 Wi-Fi**(機器還要帶到部署地才刪、或還沒確認齊全只想先安全斷電):",
        "```bash",
        "ssh <user>@<host> \"sudo shutdown -h now\"",
        "```",
        "紅燈恆亮、綠燈不再閃(約 15 秒)即可拔電;安裝測試 Wi-Fi 仍留著,下次開機一樣能連。",
        "", f"*由 deploy_pi.py 產生 · skill: pi-adguard-setup*",
    ]
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    ok(f"完整報告已產生:{report_path}")
    return report_path

def write_machine_setup_md(host, user, report, soak, tested):
    """在機器自己身上寫一份 SETUP.md(部署流程 要求的部署紀錄)。

    為什麼要寫在「機器上」而不只是 Mac 端的報告:五年後這台可能被搬到別的家、
    Mac 端報告可能早就找不到,而機器自己一定跟著自己走。SSH 進去 `cat ~/SETUP.md`
    就知道這台是什麼、何時部署、配置基準為何——這是無人值守機器最基本的可追溯性。
    冪等:每次部署覆寫成最新狀態。
    """
    st = probe_state(host, user)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wifi = "、".join(p.split("|")[0] for p in _plist(st, "W_PROFILE")) or "(無)"
    body = f"""# 這台機器的部署紀錄

由 deploy_pi.py 自動產生 · 最後部署:{now}

| 項目 | 值 |
|---|---|
| 機型 | {st.get('M_MODEL', '?')} |
| hostname | {st.get('M_HOSTNAME', '?')} |
| 部署當下 IP | {st.get('M_IP', '?')} |
| SSH 帳號 | {user} |
| AdGuard Home | {st.get('A_VER', '?')} |
| 啟用過濾清單 | {st.get('C_FILTERS_ON', '?')} 個 |
| 規則總量 | {st.get('C_RULES_TOTAL', '?')} 條(五年閥門 {RULES_VALVE:,},超過就移除 OISD Big) |
| Wi-Fi 設定檔 | {wifi} |
| 本次壓測 | {f'{soak} 分鐘' if tested else '未執行'} |

## 這台是什麼

AdGuard Home DNS 廣告/追蹤/詐騙阻擋伺服器,家中所有裝置的 DNS 都指向它。
設計目標:五年無人值守穩定運行。

## 日常維運

    systemctl is-active AdGuardHome        # 服務狀態
    dig +short @127.0.0.1 google.com       # 解析是否正常
    dig +short @127.0.0.1 doubleclick.net  # 應回 0.0.0.0 或空(有擋)
    journalctl -t agh-healthcheck          # 殭屍自癒是否介入過
    free -h; vcgencmd measure_temp         # 記憶體與溫度

管理介面:http://{st.get('M_IP', '<本機IP>')}(帳密為部署時設定精靈設的)

## 關機(拔電前必做)

    sudo shutdown -h now
    # 紅燈恆亮、綠燈不再閃(約 15 秒)即可拔電

## 年度體檢(每年一次)

從 Mac 端跑:

    ssh {user}@<本機IP> bash -s -- 10 < scripts/preflight_check.sh

歷史趨勢累積在本機 ~/preflight-history.log,逐年比對可看出 SD 卡、散熱、供電的老化。

## 設定飄移稽核(建議每季)

從 Mac 端跑,純唯讀,會比對這台有沒有偏離標準配置:

    python3 scripts/audit_fleet.py {user}@<本機IP>

完整流程文件見 Mac 上的 README.md
"""
    # 經 stdin 寫入,避免 quoting 地獄與超長命令列
    code, _ = ssh_script_stdin(host, user, "cat > ~/SETUP.md", body)
    if code == 0:
        ok(f"已在機器上寫入部署紀錄:~/SETUP.md")
        report.append(("機器上部署紀錄", "已寫入", "~/SETUP.md(機型/版本/清單/維運指令/年檢方式)"))
    else:
        warn("寫入機器上的 SETUP.md 失敗(不影響部署結果)")
        report.append(("機器上部署紀錄", "寫入失敗", "可事後手動補"))
    return code == 0

def ssh_script_stdin(host, user, remote_cmd, content, timeout=30):
    """把內容透過 stdin 餵給遠端指令(用於寫檔,避免 shell quoting 問題)。"""
    full = ["ssh"] + SSH_OPTS + [f"{user}@{host}", remote_cmd]
    try:
        r = subprocess.run(full, input=content, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(寫入逾時)"

def stage_reboot(host, user, report):
    hdr("步驟 11/13 · 重開機測試(停電恢復 + 優化生效驗證)")
    info("送出 reboot,驗證機器能自己爬回來、服務恢復、config.txt 優化生效…")
    ssh(host, user, "sudo reboot", timeout=15)
    time.sleep(25)  # 給關機 + 開機起步
    back = False
    for _ in range(30):  # 最多再等 ~150 秒
        if _reachable(host, user):
            back = True; break
        time.sleep(5)
    if not back:
        err("重開機後約 3 分鐘仍未恢復——開機可能卡住,需人工檢查電源/SD 卡")
        report.append(("重開機測試", "❌ 未自動恢復", "3 分鐘未回來,人工檢查"))
        return False
    ok("機器已自動恢復連線")
    _, agh = ssh(host, user, "systemctl is-active AdGuardHome")
    _, gpu = ssh(host, user, "vcgencmd get_mem gpu")
    # AGH 的 dnsproxy(53埠)在 process 啟動後還要再等約 20 秒才真正監聽(webapi/80埠先起),
    # SSH 恢復不代表 DNS 已就緒——單次立即測會誤判(2026-07-23 實戰踩到:uptime 10 分鐘後
    # 手動複測完全正常,證實只是時機太早)。改成重試最多 40 秒。
    dns, dns_ok = "", False
    for _ in range(8):
        _, dns = ssh(host, user, "dig +short +time=5 +tries=2 @127.0.0.1 google.com | head -1")
        if dns.strip() and dns.strip()[0].isdigit():
            dns_ok = True; break
        time.sleep(5)
    agh_ok = "active" in agh
    gpu_ok = "16M" in gpu
    (ok if agh_ok else err)(f"AGH 服務:{'active ✓' if agh_ok else '未恢復 ✗'}")
    (ok if gpu_ok else warn)(f"config.txt 生效:gpu_mem={gpu.strip()}(應 16M)")
    (ok if dns_ok else err)(f"DNS 解析:{dns.strip() or '失敗 ✗'}")
    passed = agh_ok and dns_ok
    report.append(("重開機測試", "通過" if passed else "部分通過(見報告細節)",
                   f"自動恢復✓ · AGH{'✓' if agh_ok else '✗'} · gpu_mem {gpu.strip()} · DNS{'✓' if dns_ok else '✗'}"))
    return passed

def _other_connections(host, user, exclude_name):
    """回傳(排除 exclude_name 外)還有沒有其他可用連線——已設密碼的 Wi-Fi 設定檔 +
    「目前 active 的」有線連線數。

    有線刻意只算 active(真的插著線):RPi OS 預設就有 Wired connection 1 設定檔,
    沒插線也存在,拿設定檔數當保命線會讓「唯一連線」警告永遠不觸發。
    用來判斷「刪掉安裝測試 Wi-Fi 後,下次開機這台機器是否還連得上網」。
    """
    wifi = [p for p in _wifi_profiles(host, user) if p[0] != exclude_name and p[2]]
    _, eth = ssh(host, user, "nmcli -t -f TYPE connection show --active 2>/dev/null | grep -c ethernet")
    # ssh() 混有 stderr,取第一個「純數字」的行當計數(其餘視為雜訊)
    eth_n = next((int(l.strip()) for l in eth.splitlines() if l.strip().isdigit()), 0)
    return wifi, eth_n

def _guess_install_wifi(existing):
    """猜哪個是安裝測試用的 Wi-Fi:排除 stage_wifi 建立的 wlan0-<SSID> 命名慣例後,
    若剛好剩一個候選就是它;有多個或沒有就交給使用者自己指認,不用猜的硬選。
    """
    cands = [p for p in existing if not p[0].startswith("wlan0-")]
    return cands[0][0] if len(cands) == 1 else None

def stage_cleanup(host, user, report, report_path):
    """收尾:詢問要不要刪除安裝測試 Wi-Fi 並關機、只關機、或不動作。

    刪除前一定先確認「刪掉之後還有沒有其他可用連線」——沒有的話這台機器下次開機
    會完全連不上網、又是無頭機器查不到,只能實體重插,故此時預設值會自動改為
    「只關機」,要硬刪必須另外打字 yes 二次確認。
    """
    hdr("步驟 13/13 · 收尾")
    if not _reachable(host, user):
        # 機器此刻連不上(例如重開機測試沒爬回來又選擇繼續),假裝能關機只會誤導
        warn("機器目前連不上——無法執行收尾動作(刪 Wi-Fi / 關機),請人工處理")
        report.append(("收尾", "跳過(機器連不上)", "需人工關機"))
        try:
            with open(report_path, "a") as f:
                f.write("\n\n## 收尾\n\n跳過(機器連不上),需人工關機。\n")
        except OSError:
            pass
        return
    existing = _wifi_profiles(host, user)
    target = _guess_install_wifi(existing)
    others, eth_n = _other_connections(host, user, target or "")
    safe = bool(others or eth_n > 0)

    if existing:
        info("目前的 Wi-Fi 連線設定檔:")
        for name, ssid, has in existing:
            mark = "  ← 猜測是安裝測試用" if name == target else ""
            info(f"    • {name}(SSID {ssid or '?'}){'  密碼✓' if has else '  ⚠️ 未設密碼'}{mark}")
    if target and not safe:
        warn(f"「{target}」目前是唯一可用連線——刪除後下次開機將完全連不上網(需實體重插)")

    default_choice = "1" if (target and safe) else "2"
    choice = ask("要怎麼收尾?",
                 [f"[Enter] 同 [{default_choice}](預設)",
                  "[1] 刪除安裝測試 Wi-Fi 並關機" + ("(推薦)" if default_choice == "1" else "(目前不安全,見上方警告)"),
                  "[2] 直接關機(不刪 Wi-Fi)" + ("(推薦)" if default_choice == "2" else ""),
                  "[3] 不動作,結束"]) or default_choice

    detail = "未執行(使用者選擇不動作)"
    if choice == "3":
        info("不執行任何動作")
    else:
        del_name = None
        if choice == "1":
            if target:
                del_name = target
            elif existing:
                info("無法自動判斷哪個是安裝測試用 Wi-Fi,請指認:")
                for i, (name, ssid, has) in enumerate(existing, 1):
                    info(f"    [{i}] {name}(SSID {ssid or '?'})")
                sel = ask("刪哪一個?",
                          ["[Enter] 跳過刪除,只關機",
                           "[編號]  指認要刪的那組"])
                if sel.isdigit() and 1 <= int(sel) <= len(existing):
                    del_name = existing[int(sel) - 1][0]
                    _, eth2 = _other_connections(host, user, del_name)
                    safe = bool([p for p in existing if p[0] != del_name and p[2]] or eth2)
                else:
                    warn("未指認,改為直接關機")
            else:
                warn("偵測不到任何 Wi-Fi 連線設定檔,無可刪除項目——改為直接關機")
            if del_name and not safe:
                confirm = ask(f"⚠️  確定刪除唯一連線「{del_name}」?下次開機需實體介入",
                              ["[yes]  確定刪除",
                               "[其他] 取消,改為直接關機"])
                if confirm.lower() != "yes":
                    warn("未確認,改為直接關機")
                    del_name = None

        if del_name:
            info(f"排程關機(+1 分鐘)並刪除「{del_name}」…")
            # 先排程關機、再刪 Wi-Fi(見 收尾流程):刪除當前連線會立刻斷線,
            # 反過來寫可能來不及送出關機指令,機器會卡在懸空狀態,只能拔電
            ssh(host, user, f"sudo shutdown -h +1 && sudo nmcli connection delete '{del_name}'", timeout=15)
            ok("已排程:1 分鐘後關機,Wi-Fi 設定檔已刪除")
            info("紅燈恆亮、綠燈不再閃(約 1 分半後)即可拔電")
            detail = f"已刪除「{del_name}」並排程關機"
        else:
            info("直接關機…")
            ssh(host, user, "sudo shutdown -h now", timeout=15)
            ok("已送出關機指令")
            info("紅燈恆亮、綠燈不再閃(約 15 秒)即可拔電")
            detail = "已關機(未刪除 Wi-Fi 設定檔)"

    report.append(("收尾", detail, ""))
    try:
        with open(report_path, "a") as f:
            f.write(f"\n\n## 收尾\n\n{detail}\n\n"
                    "(上方「交付前檢查清單」中的『刪除安裝測試 Wi-Fi』與『關機』兩項"
                    "已由本步驟處理,狀態以此節為準)\n")
    except OSError:
        pass

# ── main ─────────────────────────────────────────────────────
class Report(list):
    pass

def preflight_selfcheck():
    """開始前確認依賴齊全(fail fast:別跑到一半才發現腳本缺失)。"""
    required = ["harden_pi.sh", "apply_agh_config.py", "install_agh_healthcheck.sh",
                "preflight_check.sh", "probe_state.sh"]
    missing = [s for s in required if not os.path.exists(os.path.join(SCRIPTS, s))]
    if missing:
        err(f"缺少依賴腳本:{', '.join(missing)}")
        err(f"請確認 pi-adguard-setup skill 完整安裝於 {SCRIPTS}")
        sys.exit(1)
    for tool in ("ssh", "scp", "ssh-keygen"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            err(f"本機缺少必要工具:{tool}"); sys.exit(1)

def main():
    ap = argparse.ArgumentParser(description="樹莓派 AGH 一鍵部署 orchestrator")
    ap.add_argument("--host", default=None, help="目標 IP/hostname;不給則掃描區網讓你選")
    ap.add_argument("--user", default=None, help="SSH 使用者名稱;不給則執行時詢問(每台可不同帳號)")
    ap.add_argument("--soak", type=int, default=10)
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--skip-test", action="store_true")
    ap.add_argument("--skip-reboot", action="store_true", help="跳過重開機測試(服役中機器避免斷線)")
    ap.add_argument("--no-resume", action="store_true",
                    help="不做斷點續跑:已完成的階段照樣重跑一遍(冪等,只是多花時間)")
    ap.add_argument("--has-ipv6", action="store_true")
    ap.add_argument("--report", default=None, help="報告完整路徑(覆蓋 --report-dir)")
    ap.add_argument("--report-dir",
                # 未設 PI_REPORT_DIR 時使用 repo 的 report/;
                # 其他平台請設 PI_REPORT_DIR,見 README.md 的報告設定
                default=os.environ.get("PI_REPORT_DIR", os.path.join(os.path.dirname(SCRIPTS), "report")),
                help="報告輸出目錄(預設讀 PI_REPORT_DIR 環境變數,見 README.md 的報告設定)")
    a = ap.parse_args()

    date = datetime.datetime.now().strftime("%Y%m%d-%H%M")

    preflight_selfcheck()  # fail fast:依賴不齊直接退出
    hdr("樹莓派 AdGuard Home 一鍵部署" + (f" · 目標 {a.host}" if a.host else " · 掃描區網選機"))
    info("需要人工的關卡會停下來引導;其餘全自動。密碼一律由你自己輸入,腳本不經手。")

    report = Report()
    rp = {"path": None, "final": False}  # path:讓異常處理也能存報告;final:完整報告已寫出
    pc = {"checks": None}  # 總複查結果 holder(中斷/異常時部分報告也帶上複查表)

    def _report_path(host):
        if a.report:
            return a.report
        rdir = os.path.expanduser(a.report_dir)
        os.makedirs(rdir, exist_ok=True)
        return os.path.join(rdir, f"pi-deploy-{(host or 'unknown').replace('.', '_')}-{date}.md")

    try:
        if not a.skip_flash:
            stage_flash(report)
        # 燒錄完(帳號已在 Imager 設好)才問帳號;--skip-flash 時卡也已燒好、帳號已定
        if not a.user:
            a.user = ask("這台樹莓派的 SSH 使用者名稱是?(燒錄時 Imager 進階選項填的那個)")
            while not a.user:
                a.user = ask("使用者名稱不能空白,請輸入")
        # 帳號一併回傳:連線階段允許當場更正帳號(打錯帳號與打錯密碼症狀相同),
        # 更正後必須讓後續每個階段都沿用新帳號
        actual_host, a.user = stage_connect(a.host, a.user, report)
        rp["path"] = _report_path(actual_host or a.host)

        if not actual_host:
            gen_report(a.host or "unknown", a.user, report, rp["path"], "", a.soak, False)
            sys.exit(1)
        a.host = actual_host  # 後續階段一律用實際連上的位址

        # 免密碼 sudo:後面全部是非互動 sudo,沒有就每一階段都失敗(新版 OS 預設沒有)
        if not stage_sudo(a.host, a.user, report):
            gen_report(a.host, a.user, report, rp["path"], "", a.soak, False)
            sys.exit(1)

        # 進度偵測:斷電/中斷後重跑時,已完成的重階段可直接跳過(全新機器不會多問)
        done = stage_probe(a.host, a.user, report, a.no_resume)

        # 系統強化:失敗非致命(冪等,可事後重跑),但明確告知
        if "harden" in done:
            info("系統強化:偵測到已完成,跳過(交付前總複查會再驗一次)")
            report.append(("系統強化", "跳過(已完成)", "進度偵測判定已套用,複查階段再驗證"))
        elif not stage_harden(a.host, a.user, report):
            warn("系統強化未完全成功——繼續部署,建議事後單獨重跑 harden_pi.sh 補完")
        # AGH 安裝:失敗即致命(沒 AGH 部署無意義)
        if "agh" in done:
            info("AdGuard Home:偵測到已安裝且設定精靈已完成,跳過安裝")
            report.append(("安裝 AGH", "跳過(已完成)", "進度偵測判定已安裝且服務正常"))
            _agh_upgrade(a.host, a.user, report)
        elif not stage_install_agh(a.host, a.user, report):
            gen_report(a.host, a.user, report, rp["path"], "", a.soak, False)
            sys.exit(1)
        if "calibrate" in done:
            info("校準設定:偵測到已是標準配置,跳過")
            report.append(("校準設定", "跳過(已完成)", "進度偵測判定已為標準配置"))
        else:
            stage_calibrate(a.host, a.user, a.has_ipv6, report)
        stage_wifi(a.host, a.user, report)
        stage_verify(a.host, a.user, report)

        # 交付前總複查:壓測前從頭確認所有設定齊備(不齊可就地補跑),確保壓測測的是成品
        _, precheck, abort = stage_precheck(a.host, a.user, a.has_ipv6, report)
        pc["checks"] = precheck
        if abort:
            gen_report(a.host, a.user, report, rp["path"], "", a.soak, False, precheck)
            hdr("已中止(未壓測)")
            info(f"報告:{rp['path']}")
            sys.exit(1)

        preflight_raw, tested = "", False
        if not a.skip_test:
            result, preflight_raw = stage_test(a.host, a.user, a.soak, report)
            tested = result is not None  # None = 當場選擇跳過

        # 重開機測試:驗證停電恢復 + config.txt 生效(服役中機器可 --skip-reboot 略過)
        if not a.skip_reboot:
            stage_reboot(a.host, a.user, report)
        else:
            warn("已跳過重開機測試——config.txt 優化(gpu_mem/藍牙/音訊)要等下次重開機才生效")
            report.append(("重開機測試", "跳過(--skip-reboot)", "config.txt 優化待下次重開機生效"))

        # 在機器自己身上留一份部署紀錄(部署流程):機器會被搬到別的家,
        # Mac 端的報告不一定跟著走,機器自己一定跟著自己走
        write_machine_setup_md(a.host, a.user, report, a.soak, tested)

        gen_report(a.host, a.user, report, rp["path"], preflight_raw, a.soak, tested, precheck)
        rp["final"] = True  # 完整報告已落地——之後的中斷/異常絕不能再重寫它(會洗掉壓測輸出)
        hdr("部署完成 🎉")
        info(f"報告:{rp['path']}")
        info("下一步:完成報告末尾的『交付前檢查清單』(正式Wi-Fi/路由器DNS/IP綁定)")

        stage_cleanup(a.host, a.user, report, rp["path"])

    except KeyboardInterrupt:
        print()
        if rp["final"]:
            warn("使用者中斷(Ctrl+C)——完整報告已在收尾前產生,保留不動")
            sys.exit(130)
        warn("使用者中斷(Ctrl+C)——產生部分報告後結束")
        report.append(("流程", "使用者中斷", "Ctrl+C"))
        if rp["path"]:
            gen_report(a.host or "unknown", a.user, report, rp["path"], "", a.soak, False, pc["checks"])
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        print()
        err(f"未預期的錯誤:{e}")
        if rp["final"]:
            warn("完整報告已在收尾前產生,保留不動")
            sys.exit(1)
        report.append(("流程", "異常中止", str(e)[:150]))
        if rp["path"]:
            gen_report(a.host or "unknown", a.user, report, rp["path"], "", a.soak, False, pc["checks"])
        sys.exit(1)

if __name__ == "__main__":
    main()
