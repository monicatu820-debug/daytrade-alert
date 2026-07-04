"""
daytrade_plan.py
每日台股當沖作戰計畫 (純 Python, GitHub Actions 執行, 免 LLM API)。

資料來源 (皆為海外 CI 可通的免費來源):
- TWSE OpenAPI (openapi.twse.com.tw): 全市場收盤、當沖標的、大盤成交金額/指數、漲跌家數、處置/注意股
- Stooq (stooq.com) 為主 / Yahoo Finance 為備援: 美股四大指數、AI 半導體股、台積電 ADR、DXY、美10年殖利率
- Google News RSS: 國際重大新聞標題彙整

無可靠免費來源者, 依規格明確標註「資料不足」, 不臆測:
- 台指期夜盤即時報價
- 前一日三大法人現貨買賣超 (BFI82U/T86 為 twse.com.tw legacy, 海外 IP 被擋)
- 個股三大法人籌碼、經濟事件行事曆

輸出: 寄送 Email (Markdown 純文字) + 產生 index.html (GitHub Pages)。
"""

import os
import sys
import ssl
import json
import time
import logging
import smtplib
import datetime as dt
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.header import Header

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------- CONFIG ----------------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = SMTP_USER
MAIL_TO = os.environ.get("MAIL_TO", "")

# 離線測試: 設 FIXTURE_DIR 時, 從本地 json 讀資料而非連網
FIXTURE_DIR = os.environ.get("FIXTURE_DIR", "")

OPENAPI = "https://openapi.twse.com.tw/v1"
HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}

MIN_TURNOVER = 100_000_000   # 選股最低成交金額門檻(元) 1億
POOL_SIZE = 50               # 依成交金額先取前 N 檔進池
FINAL_N = 5                  # 最終最多推薦檔數
MIN_PRICE = 10               # 最低股價(元), 濾掉雞蛋水餃股
TR_STOP_MULT = 1.0           # 停損 = 進場參考價 ± TR_STOP_MULT × 當日真實區間
TR_TP1_MULT = 1.0            # 第一停利倍數
TR_TP2_MULT = 2.0            # 第二停利倍數

TZ = dt.timezone(dt.timedelta(hours=8))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 美股 / 總經 標的 (Stooq 代碼, Yahoo 代碼)
US_TICKERS = [
    ("道瓊工業", "^dji", "^DJI"),
    ("Nasdaq", "^ndq", "^IXIC"),
    ("S&P 500", "^spx", "^GSPC"),
    ("費城半導體", "^sox", "^SOX"),
]
US_STOCKS = [
    ("NVIDIA", "nvda.us", "NVDA"),
    ("AMD", "amd.us", "AMD"),
    ("Broadcom", "avgo.us", "AVGO"),
    ("台積電 ADR", "tsm.us", "TSM"),
    ("Micron", "mu.us", "MU"),
]
MACRO = [
    ("美元指數 DXY", "^dxy", "DX-Y.NYB"),
    ("美國10年期公債殖利率", "10usy.b", "^TNX"),
]

# 新聞主題 -> 查詢字
NEWS_TOPICS = [
    ("AI", "AI 人工智慧"),
    ("半導體", "半導體 晶片"),
    ("科技", "科技股 那斯達克"),
    ("地緣政治", "地緣政治 台海 中美"),
    ("FED / 利率", "Fed 聯準會 利率"),
    ("匯率", "台幣 美元 匯率"),
    ("國際油價", "油價 原油"),
]

# 優先族群: 個股代號 -> 族群 (熱門主力股 curated, 非完整, 名稱關鍵字為輔助)
SECTOR_BY_CODE = {
    # 半導體 / IC
    "2330": "半導體", "2303": "半導體", "2454": "半導體", "3034": "半導體",
    "2379": "半導體", "3443": "半導體", "3661": "半導體", "6415": "半導體",
    "4966": "半導體", "3529": "半導體", "5347": "半導體", "6770": "半導體",
    "3006": "半導體", "8081": "半導體", "3035": "半導體", "2408": "記憶體",
    # 記憶體
    "8299": "記憶體", "4967": "記憶體", "5289": "記憶體", "6485": "記憶體",
    "2344": "記憶體",
    # 面板 / 光電
    "2409": "面板", "3481": "面板", "6116": "面板", "8069": "面板",
    # PCB / 載板
    "2313": "PCB", "3037": "PCB", "2368": "PCB", "3044": "PCB",
    "6269": "PCB", "8046": "PCB", "4958": "PCB", "2383": "PCB", "3189": "PCB",
    # 散熱
    "3017": "散熱", "3324": "散熱", "6230": "散熱", "3653": "散熱", "8framework": "散熱",
    # 網通
    "2345": "網通", "3596": "網通", "4906": "網通", "6285": "網通",
    "5388": "網通", "3234": "網通", "2419": "網通",
    # 機器人 / 自動化
    "2049": "機器人", "2059": "機器人", "1590": "機器人",
    # AI 伺服器 / 代工
    "2317": "AI伺服器", "2382": "AI伺服器", "3231": "AI伺服器", "2356": "AI伺服器",
    "4938": "AI伺服器", "6669": "AI伺服器", "3706": "AI伺服器", "2376": "AI伺服器",
    "2357": "AI伺服器",
}
SECTOR_KEYWORDS = [
    ("半導體", ["半導體", "晶圓", "矽", "晶", "IC", "電子"]),
    ("記憶體", ["記憶體", "DRAM", "快閃", "儲存"]),
    ("面板", ["光電", "面板", "顯示"]),
    ("PCB", ["電路板", "銅箔", "載板", "PCB"]),
    ("散熱", ["散熱", "熱管", "均熱"]),
    ("網通", ["網通", "通訊", "電信", "光通"]),
    ("機器人", ["機器人", "自動化", "傳動", "滑軌"]),
]
PRIORITY_SECTORS = {"AI", "半導體", "記憶體", "面板", "PCB", "散熱", "網通", "機器人", "AI伺服器"}


# ---------------- utils ----------------
def to_num(x):
    try:
        s = str(x).replace(",", "").replace("+", "").replace("%", "").strip()
        if s in ("", "--", "-", "N/A", "null", "None"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def roc_to_iso(roc):
    """民國日期字串 -> yyyy/mm/dd. 接受 '1150703' 或 '115/07/03'."""
    s = str(roc).strip().replace("/", "")
    if len(s) < 7:
        return str(roc)
    y = int(s[:3]) + 1911
    return f"{y}/{s[3:5]}/{s[5:7]}"


def find_key(keys, must, exclude=()):
    for k in keys:
        kl = str(k).lower()
        if all(m.lower() in kl for m in must) and not any(e.lower() in kl for e in exclude):
            return k
    return None


def http_get(url, params=None, headers=None, timeout=30):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, params=params, headers=h, timeout=timeout, verify=True)
        r.raise_for_status()
        return r
    except requests.exceptions.SSLError:
        r = requests.get(url, params=params, headers=h, timeout=timeout, verify=False)
        r.raise_for_status()
        return r


def fetch_openapi(dataset):
    """回傳 list[dict]; 失敗回 None. 支援 FIXTURE_DIR 離線測試."""
    name = dataset.strip("/").split("/")[-1]
    if FIXTURE_DIR:
        path = os.path.join(FIXTURE_DIR, name + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None
    try:
        r = http_get(f"{OPENAPI}/{dataset}")
        return r.json()
    except Exception as e:
        logging.warning(f"OpenAPI {dataset} 失敗: {e}")
        return None


# ---------------- 美股 / 總經 ----------------
def _stooq_daily_change(sym):
    """Stooq 日線 CSV 取最新收盤與漲跌%. 回 (last, chg_pct) 或 None."""
    if FIXTURE_DIR:
        return None
    try:
        url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
        txt = http_get(url).text.strip()
        lines = [ln for ln in txt.splitlines() if ln and "," in ln]
        if len(lines) < 3 or not lines[0].lower().startswith("date"):
            return None
        rows = [ln.split(",") for ln in lines[1:]]
        closes = [to_num(r[4]) for r in rows if len(r) >= 5 and to_num(r[4])]
        if len(closes) < 2:
            return None
        last, prev = closes[-1], closes[-2]
        return last, round((last - prev) / prev * 100, 2)
    except Exception as e:
        logging.warning(f"Stooq {sym} 失敗: {e}")
        return None


def _yahoo_daily_change(sym):
    if FIXTURE_DIR:
        return None
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
        j = http_get(url, params={"range": "5d", "interval": "1d"}).json()
        res = j["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 2:
            return None
        last, prev = closes[-1], closes[-2]
        return round(last, 2), round((last - prev) / prev * 100, 2)
    except Exception as e:
        logging.warning(f"Yahoo {sym} 失敗: {e}")
        return None


def fetch_quote(stooq_sym, yahoo_sym):
    return _stooq_daily_change(stooq_sym) or _yahoo_daily_change(yahoo_sym)


def fetch_us_block():
    """回 dict: indices/stocks/macro -> list of (name, last, chg, ok)."""
    out = {"indices": [], "stocks": [], "macro": []}
    for label, group in [("indices", US_TICKERS), ("stocks", US_STOCKS), ("macro", MACRO)]:
        for name, s_sym, y_sym in group:
            q = fetch_quote(s_sym, y_sym)
            if q:
                out[label].append((name, q[0], q[1], True))
            else:
                out[label].append((name, None, None, False))
            time.sleep(0.2)
    return out


# ---------------- 新聞 ----------------
def fetch_news():
    """回 list of (topic, [titles]). 失敗的主題留空並標註."""
    if FIXTURE_DIR:
        return [(t, []) for t, _ in NEWS_TOPICS]
    out = []
    for topic, q in NEWS_TOPICS:
        titles = []
        try:
            url = "https://news.google.com/rss/search"
            params = {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
            xml = http_get(url, params=params, headers={"accept": "*/*"}).text
            root = ET.fromstring(xml)
            for item in root.iter("item"):
                t = item.findtext("title")
                if t:
                    titles.append(t.strip())
                if len(titles) >= 3:
                    break
        except Exception as e:
            logging.warning(f"News {topic} 失敗: {e}")
        out.append((topic, titles))
        time.sleep(0.2)
    return out


# ---------------- 台股資料 ----------------
def latest_row_by_date(data, date_key_musts=(("date",),)):
    """挑最新日期的資料列(針對 FMTQIK 這類多日陣列)."""
    if not data:
        return None
    keys = data[0].keys()
    dk = None
    for musts in date_key_musts:
        dk = find_key(keys, musts)
        if dk:
            break
    if not dk:
        return data[-1]
    return sorted(data, key=lambda r: str(r.get(dk, "")))[-1]


def fetch_market_breadth():
    """漲跌家數 (twtazu_od, 取 類型=股票). 回 dict 或 None."""
    data = fetch_openapi("opendata/twtazu_od")
    if not data:
        return None
    row = None
    for r in data:
        if str(r.get("類型", "")).strip() == "股票":
            row = r
            break
    row = row or data[-1]
    return {
        "date": roc_to_iso(row.get("出表日期", "")),
        "up": to_num(row.get("上漲")),
        "down": to_num(row.get("下跌")),
        "flat": to_num(row.get("持平")),
        "limit_up": to_num(row.get("漲停")),
        "limit_down": to_num(row.get("跌停")),
    }


def fetch_market_turnover():
    """大盤成交金額 + 加權指數 + 漲跌點 (FMTQIK 最新列)."""
    data = fetch_openapi("exchangeReport/FMTQIK")
    row = latest_row_by_date(data, (("date",),))
    if not row:
        return None
    return {
        "date": roc_to_iso(row.get("Date", "")),
        "value": to_num(row.get("TradeValue")),
        "taiex": to_num(row.get("TAIEX")),
        "change": to_num(str(row.get("Change", "")).replace("+", "")),
        "change_raw": str(row.get("Change", "")),
    }


def _in_period_today(period, today):
    """DispositionPeriod '115/06/22～115/07/03' 是否涵蓋今天(ROC today 'yyy/mm/dd')."""
    try:
        parts = period.replace("~", "～").split("～")
        if len(parts) != 2:
            return True  # 無法解析時保守視為仍在處置
        s = parts[0].strip().replace("/", "")
        e = parts[1].strip().replace("/", "")
        t = today.replace("/", "")
        return s <= t <= e
    except Exception:
        return True


def fetch_excluded_codes():
    """處置股(處置期間涵蓋今日) + 注意股 代號集合."""
    today_roc = dt.datetime.now(TZ)
    today_roc = f"{today_roc.year - 1911:03d}/{today_roc.month:02d}/{today_roc.day:02d}"
    disposed, attention = set(), set()

    punish = fetch_openapi("announcement/punish")
    if punish:
        for r in punish:
            code = str(r.get("Code", "")).strip()
            period = str(r.get("DispositionPeriod", "")).strip()
            if code and (not period or _in_period_today(period, today_roc)):
                disposed.add(code)

    notice = fetch_openapi("announcement/notice")
    if notice:
        for r in notice:
            code = str(r.get("Code", "")).strip()
            if code:
                attention.add(code)

    return disposed, attention


def classify_sector(code, name):
    if code in SECTOR_BY_CODE:
        return SECTOR_BY_CODE[code]
    for sector, kws in SECTOR_KEYWORDS:
        if any(k in str(name) for k in kws):
            return sector
    return ""


def build_candidates(news_hits):
    """回 (trade_date, list[dict]) 已排除處置/注意/低量/低價, 依綜合分數排序."""
    all_data = fetch_openapi("exchangeReport/STOCK_DAY_ALL")
    if not all_data:
        raise RuntimeError("STOCK_DAY_ALL 無資料")

    keys = all_data[0].keys()
    k_code = find_key(keys, ["code"])
    k_name = find_key(keys, ["name"])
    k_open = find_key(keys, ["opening"])
    k_high = find_key(keys, ["highest"])
    k_low = find_key(keys, ["lowest"])
    k_close = find_key(keys, ["closing"])
    k_chg = find_key(keys, ["change"])
    k_vol = find_key(keys, ["tradevolume"])
    k_amt = find_key(keys, ["tradevalue"])
    k_date = find_key(keys, ["date"])
    if None in (k_code, k_name, k_high, k_low, k_close, k_vol, k_amt):
        raise RuntimeError(f"STOCK_DAY_ALL 欄位對應失敗: {list(keys)}")

    trade_date = roc_to_iso(all_data[0].get(k_date, "")) if k_date else ""

    # 當沖可為標的 universe
    dt_data = fetch_openapi("exchangeReport/TWTB4U")
    eligible = set()
    if dt_data:
        dkc = find_key(dt_data[0].keys(), ["code"])
        if dkc:
            eligible = {str(r.get(dkc, "")).strip() for r in dt_data if str(r.get(dkc, "")).strip()}

    disposed, attention = fetch_excluded_codes()

    rows = []
    for r in all_data:
        code = str(r.get(k_code, "")).strip()
        if not code or len(code) != 4 or not code.isdigit():
            continue  # 只留一般上市股票代號(4碼數字), 濾掉權證/ETF數字碼由後續門檻再處理
        close = to_num(r.get(k_close))
        high = to_num(r.get(k_high))
        low = to_num(r.get(k_low))
        vol = to_num(r.get(k_vol))
        amt = to_num(r.get(k_amt))
        if None in (close, high, low, vol, amt) or close < MIN_PRICE or amt < MIN_TURNOVER:
            continue
        if eligible and code not in eligible:
            continue
        if code in disposed or code in attention:
            continue
        rows.append({
            "code": code, "name": str(r.get(k_name, "")).strip(),
            "open": to_num(r.get(k_open)), "high": high, "low": low, "close": close,
            "vol": vol, "amt": amt,
        })

    if not rows:
        return trade_date, []

    rows.sort(key=lambda x: x["amt"], reverse=True)
    rows = rows[:POOL_SIZE]

    # 指標
    for x in rows:
        rng = x["high"] - x["low"]
        x["amp"] = round(rng / x["close"] * 100, 2) if x["close"] else 0.0  # 振幅%
        x["tr"] = round(rng, 2)                                             # 當日真實區間(點)
        x["tr_pct"] = round(rng / x["close"] * 100, 2) if x["close"] else 0.0
        o = x["open"] or x["close"]
        x["intraday"] = round((x["close"] - o) / o * 100, 2) if o else 0.0  # 開->收 %
        x["close_pos"] = round((x["close"] - x["low"]) / rng, 2) if rng else 0.5  # 收盤在區間位置
        x["sector"] = classify_sector(x["code"], x["name"])
        # 消息面: 名稱/族群出現在今日新聞標題
        blob = " ".join(t for _, ts in news_hits for t in ts)
        x["news_hit"] = 1 if (x["name"] and x["name"] in blob) or (x["sector"] and x["sector"] in blob) else 0

    # 五維排名分數 (分數越高越好)
    def rank_score(key, reverse=True):
        vals = sorted({x[key] for x in rows}, reverse=reverse)
        idx = {v: i for i, v in enumerate(vals)}
        n = max(len(vals) - 1, 1)
        return {x["code"]: 1 - idx[x[key]] / n for x in rows}

    s_vol = rank_score("amt")          # 成交量
    s_amp = rank_score("tr_pct")       # 波動率
    s_liq = rank_score("vol")          # 流動性(股數)當籌碼/流動性 proxy
    for x in rows:
        # 技術面: 收盤位置 + 動能
        x["tech"] = round(0.6 * x["close_pos"] + 0.4 * (0.5 + max(-5, min(5, x["intraday"])) / 10), 3)
    s_tech = {x["code"]: x["tech"] for x in rows}

    for x in rows:
        c = x["code"]
        sector_bonus = 0.15 if x["sector"] in PRIORITY_SECTORS else 0.0
        x["score"] = round(
            0.30 * s_vol[c] +      # 成交量
            0.25 * s_amp[c] +      # 波動率
            0.15 * s_liq[c] +      # 籌碼/流動性 proxy
            0.10 * x["news_hit"] + # 消息面(heuristic)
            0.20 * s_tech[c] +     # 技術面
            sector_bonus, 4
        )

    rows.sort(key=lambda x: x["score"], reverse=True)
    return trade_date, rows


# ---------------- 情緒 / 作戰計畫 ----------------
def assess_sentiment(us, breadth):
    """規則版市場情緒. 回 (label, reasons[], score)."""
    reasons, score = [], 0

    def pct(block, name):
        for n, _l, c, ok in block:
            if n == name and ok and c is not None:
                return c
        return None

    sox = pct(us["indices"], "費城半導體")
    ndq = pct(us["indices"], "Nasdaq")
    tsm = pct(us["stocks"], "台積電 ADR")

    for label, v in [("費半", sox), ("Nasdaq", ndq), ("台積電ADR", tsm)]:
        if v is None:
            continue
        if v >= 1.0:
            score += 1; reasons.append(f"{label} +{v}%, 偏多")
        elif v <= -1.0:
            score -= 1; reasons.append(f"{label} {v}%, 偏空")
        else:
            reasons.append(f"{label} {v}%, 中性")

    if breadth and breadth.get("up") is not None and breadth.get("down") is not None:
        up, down = breadth["up"], breadth["down"]
        if down and up / down >= 1.5:
            score += 1; reasons.append(f"前一日上漲{int(up)}/下跌{int(down)}家, 廣度偏多")
        elif up and down / max(up, 1) >= 1.5:
            score -= 1; reasons.append(f"前一日上漲{int(up)}/下跌{int(down)}家, 廣度偏空")
        else:
            reasons.append(f"前一日上漲{int(up)}/下跌{int(down)}家, 廣度中性")

    reasons.append("台指期夜盤: 資料不足(無免費即時來源), 未納入情緒判斷")

    if score >= 2:
        label = "偏多"
    elif score <= -2:
        label = "偏空"
    else:
        label = "區間震盪"
    return label, reasons, score


def plan_for(x, sentiment_label):
    """單檔作戰計畫 dict."""
    close, tr = x["close"], x["tr"]
    # 方向
    if x["close_pos"] >= 0.6 and x["intraday"] >= 0 and sentiment_label != "偏空":
        direction = "偏多(等突破確認)"
        entry = "開盤站上前一日收盤且突破開盤 5 分鐘高點、成交量同步放大時進場"
        stop = f"跌破進場價 −{round(TR_STOP_MULT * tr, 2)} 點(約當日真實區間 1 倍)或失守開盤低點, 二擇一先到"
        tp1 = f"+{round(TR_TP1_MULT * tr, 2)} 點(約 1×當日區間)"
        tp2 = f"+{round(TR_TP2_MULT * tr, 2)} 點(約 2×當日區間)"
    elif x["close_pos"] <= 0.4 and x["intraday"] <= 0 and sentiment_label != "偏多":
        direction = "偏空(等跌破確認)"
        entry = "開盤跌破前一日收盤且跌破開盤 5 分鐘低點、無法站回時進場放空"
        stop = f"漲回進場價 +{round(TR_STOP_MULT * tr, 2)} 點或站回開盤高點, 二擇一先到"
        tp1 = f"−{round(TR_TP1_MULT * tr, 2)} 點(約 1×當日區間)"
        tp2 = f"−{round(TR_TP2_MULT * tr, 2)} 點(約 2×當日區間)"
    else:
        direction = "等待突破(方向未明)"
        entry = "開盤 30 分鐘觀察, 帶量突破開盤區間上緣做多 / 跌破下緣做空, 不預設方向"
        stop = f"進場後反向 −{round(TR_STOP_MULT * tr, 2)} 點(約當日真實區間 1 倍)"
        tp1 = f"±{round(TR_TP1_MULT * tr, 2)} 點(約 1×當日區間)"
        tp2 = f"±{round(TR_TP2_MULT * tr, 2)} 點(約 2×當日區間)"

    stop_pct = round(TR_STOP_MULT * tr / close * 100, 2) if close else 0
    risks = []
    if x["amp"] >= 7:
        risks.append(f"當日振幅 {x['amp']}% 偏大, 停損跳動快")
    if x["close_pos"] >= 0.9:
        risks.append("收在近全日高, 易高檔套牢, 追高留意假突破")
    if x["close_pos"] <= 0.1:
        risks.append("收在近全日低, 反彈搶反彈風險高")
    if not risks:
        risks.append("留意開盤跳空與量能是否延續")

    return {
        "direction": direction, "entry": entry, "stop": stop,
        "stop_pct": stop_pct, "tp1": tp1, "tp2": tp2, "risk": "；".join(risks),
    }


def top_sectors(rows):
    cnt = {}
    for x in rows[:FINAL_N * 2]:
        if x["sector"]:
            cnt[x["sector"]] = cnt.get(x["sector"], 0) + 1
    return [s for s, _ in sorted(cnt.items(), key=lambda kv: kv[1], reverse=True)][:3]


def market_risks(us, macro_map, sentiment_label):
    risks = []
    sox = next((c for n, _l, c, ok in us["indices"] if n == "費城半導體" and ok), None)
    if sox is not None and sox <= -1.5:
        risks.append(f"費半重挫 {sox}%, 台股半導體開盤易低開, 留意權值股拖累")
    dxy = macro_map.get("美元指數 DXY")
    ust = macro_map.get("美國10年期公債殖利率")
    if dxy and dxy[3] and dxy[2] is not None and dxy[2] >= 0.3:
        risks.append(f"美元指數走強({dxy[2]:+}%), 外資匯出壓力, 對台股資金面偏空")
    if ust and ust[3] and ust[2] is not None and ust[2] >= 1.0:
        risks.append(f"美10年期殖利率跳升({ust[2]:+}%), 壓抑高本益比科技股")
    risks.append("台指期夜盤與今日經濟事件行事曆資料不足, 開盤方向與盤中變數需臨場確認")
    if sentiment_label == "區間震盪":
        risks.append("方向未明, 當沖假突破機率高, 嚴設停損、寧可少做")
    # 補充一般性風險, 確保至少 3 項
    risks += [
        "當沖務必當日平倉, 避免留倉隔日跳空風險",
        "熱門股開盤常大幅跳動, 追高殺低易兩面挨耳光, 等回檔或突破確認再進",
        "單筆先設停損再進場, 未觸發前不加碼, 當日虧損達上限即收手",
    ]
    # 去重取前3
    seen, out = set(), []
    for r in risks:
        if r not in seen:
            out.append(r); seen.add(r)
        if len(out) == 3:
            break
    return out


# ---------------- 報告 ----------------
def fmt_pct(c):
    if c is None:
        return "資料不足"
    return f"{c:+.2f}%"


def build_markdown(ctx):
    now = dt.datetime.now(TZ).strftime("%Y/%m/%d %H:%M")
    us, macro_map = ctx["us"], ctx["macro_map"]
    L = []
    stale = ""
    try:
        td = dt.datetime.strptime(ctx.get("trade_date", ""), "%Y/%m/%d").date()
        rd = dt.datetime.strptime(ctx["today"], "%Y/%m/%d").date()
        if (rd - td).days > 5:
            stale = f"　⚠ 資料基準距今 {(rd - td).days} 天, 可能過期, 請確認資料來源"
    except Exception:
        pass
    L.append(f"# 台股當沖作戰計畫 {ctx['today']}")
    L.append(f"產生時間: {now} (台北)　資料基準: 前一交易日 {ctx.get('trade_date') or '資料不足'}{stale}")
    L.append("")
    L.append(f"## 今日市場情緒: {ctx['sentiment']}")
    for r in ctx["sentiment_reasons"]:
        L.append(f"- {r}")
    L.append("")

    # 推薦
    L.append("## 今日推薦標的")
    picks = ctx["picks"]
    if not picks:
        L.append("**今天建議空手, 不建議交易。** 無同時符合流動性、成交量、波動率且非處置/注意股之標的。")
    else:
        L.append(f"符合條件共推薦 {len(picks)} 檔 (依綜合評分):")
        L.append("")
        for i, x in enumerate(picks, 1):
            p = x["plan"]
            L.append(f"### {i}. {x['name']} ({x['code']})　族群: {x['sector'] or '其他'}")
            L.append(f"- 綜合評分: {x['score']}　收盤 {x['close']}　成交金額 {round(x['amt']/1e8,2)} 億　振幅 {x['amp']}%　當日區間 {x['tr']} 點")
            L.append(f"- 推薦理由: 成交量居前(流動性佳)、波動率足夠當沖、{'技術面收盤位置強勢' if x['close_pos']>=0.6 else ('技術面偏弱' if x['close_pos']<=0.4 else '技術面中性')}{'、當日出現於相關新聞' if x['news_hit'] else ''}{'、屬今日優先族群' if x['sector'] in PRIORITY_SECTORS else ''}")
            L.append(f"- 建議方向: {p['direction']}")
            L.append(f"- 進場方式: {p['entry']}")
            L.append(f"- 停損: {p['stop']}(約 −{p['stop_pct']}%)")
            L.append(f"- 第一停利: {p['tp1']}")
            L.append(f"- 第二停利: {p['tp2']}")
            L.append(f"- 放棄交易條件: 開盤 30 分鐘量縮無表態 / 帶量假突破隨即拉回 / 高檔爆大量滯漲 / 大盤與族群背離")
            L.append(f"- 個股風險: {p['risk']}")
            L.append("")
    L.append(f"**今日強勢族群**: {'、'.join(ctx['sectors']) if ctx['sectors'] else '資料不足'}")
    L.append("")

    # 風險
    L.append("## 今日風險提醒 (前 3 項)")
    for i, r in enumerate(ctx["risks"], 1):
        L.append(f"{i}. {r}")
    L.append("")

    # 附錄: 美股
    L.append("## 參考數據")
    L.append("### 美國四大指數")
    for n, v, c, ok in us["indices"]:
        L.append(f"- {n}: {'收 '+str(v)+'　' if ok and v is not None else ''}{fmt_pct(c)}")
    L.append("### 美股 AI / 半導體")
    for n, v, c, ok in us["stocks"]:
        L.append(f"- {n}: {'收 '+str(v)+'　' if ok and v is not None else ''}{fmt_pct(c)}")
    L.append("### 美元指數 / 公債殖利率 / 台指期夜盤")
    for n, v, c, ok in us["macro"]:
        L.append(f"- {n}: {'收 '+str(v)+'　' if ok and v is not None else ''}{fmt_pct(c)}")
    L.append("- 台指期夜盤: 資料不足 (無免費即時來源, 未取得)")
    L.append("")

    # 前一日台股
    t = ctx.get("turnover"); b = ctx.get("breadth")
    L.append("### 前一日台股")
    if t:
        L.append(f"- 加權指數: {t['taiex']}　漲跌: {t['change_raw']} 點　成交金額: {round((t['value'] or 0)/1e8,1)} 億")
    else:
        L.append("- 大盤成交金額 / 指數: 資料不足")
    if b:
        L.append(f"- 漲跌家數(股票): 上漲 {int(b['up'])} / 下跌 {int(b['down'])} / 持平 {int(b['flat'])}(漲停 {int(b['limit_up'])} / 跌停 {int(b['limit_down'])})")
    else:
        L.append("- 漲跌家數: 資料不足")
    L.append("- 外資 / 投信 / 自營商買賣超: 資料不足 (現貨三大法人為 twse.com.tw legacy 端點, 海外主機被擋, 未取得)")
    L.append("")

    # 新聞
    L.append("### 國際重大新聞 (標題彙整, 非深度分析)")
    any_news = False
    for topic, titles in ctx["news"]:
        if titles:
            any_news = True
            L.append(f"- {topic}: " + "；".join(titles))
        else:
            L.append(f"- {topic}: 資料不足")
    if not any_news:
        L.append("- (本次未取得新聞, 可能為來源暫時無回應)")
    L.append("")
    L.append("### 今日盤前重要新聞 / 重要事件 (財報·法說·經濟數據·政策)")
    L.append("- 資料不足: 無免費且穩定的經濟事件行事曆來源, 未自動整合。建議搭配券商盤前報告人工確認。")
    L.append("")
    L.append("---")
    L.append("資料為前一交易日之公開統計與國際行情, 僅供當沖研究參考, 非投資建議。資料不足處已明確標註, 未臆測。")
    return "\n".join(L)


def markdown_to_html(md):
    import html
    out = ['<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
           '<title>台股當沖作戰計畫</title><style>',
           'body{font-family:"Microsoft JhengHei",sans-serif;max-width:820px;margin:0 auto;padding:16px;background:#f5f6fa;color:#222;line-height:1.6}',
           'h1{font-size:1.4rem}h2{font-size:1.15rem;border-left:5px solid #4F81BD;padding-left:8px;margin-top:24px}',
           'h3{font-size:1rem;background:#eef2fb;padding:6px 8px;border-radius:4px}',
           'ul{margin:6px 0}li{margin:2px 0}code{background:#eee;padding:0 4px}hr{border:none;border-top:1px solid #ccc}',
           '.muted{color:#888;font-size:.8rem}</style></head><body>']
    in_ul = False
    for line in md.splitlines():
        if line.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("- ") or line.startswith(("1. ", "2. ", "3. ", "4. ", "5. ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            txt = html.escape(line[2:] if line.startswith("- ") else line[3:])
            txt = txt.replace("**", "")
            out.append(f"<li>{txt}</li>")
        elif line.strip() == "---":
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append("<hr>")
        elif line.strip() == "":
            if in_ul:
                out.append("</ul>"); in_ul = False
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            cls = ' class="muted"' if ("僅供" in line or "產生時間" in line) else ""
            out.append(f"<p{cls}>{html.escape(line).replace('**','')}</p>")
    if in_ul:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = Header(subject, "utf-8")
    recipients = [a.strip() for a in MAIL_TO.split(",") if a.strip()]
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(MAIL_FROM, recipients, msg.as_string())


# ---------------- main ----------------
def run():
    today = dt.datetime.now(TZ).strftime("%Y/%m/%d")

    news = fetch_news()
    us = fetch_us_block()
    macro_map = {n: (n, v, c, ok) for n, v, c, ok in us["macro"]}
    breadth = fetch_market_breadth()
    turnover = fetch_market_turnover()

    trade_date, rows = build_candidates(news)
    sentiment, s_reasons, _ = assess_sentiment(us, breadth)

    picks = rows[:FINAL_N]
    for x in picks:
        x["plan"] = plan_for(x, sentiment)

    ctx = {
        "today": today, "trade_date": trade_date,
        "sentiment": sentiment, "sentiment_reasons": s_reasons,
        "picks": picks, "sectors": top_sectors(rows) if rows else [],
        "risks": market_risks(us, macro_map, sentiment),
        "us": us, "macro_map": macro_map,
        "breadth": breadth, "turnover": turnover, "news": news,
    }

    md = build_markdown(ctx)
    html_out = markdown_to_html(md)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    with open("plan.md", "w", encoding="utf-8") as f:
        f.write(md)
    logging.info("index.html / plan.md 已產生")

    if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
        subj = f"{today} 台股當沖作戰計畫" + ("" if picks else " (建議空手)")
        send_email(subj, md)
        logging.info("Email 已寄出")
    else:
        logging.info("未設定 SMTP 環境變數, 略過寄信 (僅產出檔案)")
    return md


def main():
    try:
        print(run())
    except Exception as e:
        logging.exception("執行失敗")
        try:
            if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
                send_email("台股當沖作戰計畫 執行失敗", f"錯誤: {e}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
