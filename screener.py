"""
screener.py
GitHub Actions 每日執行: 以 TWSE OpenAPI 取得最新交易日資料,
篩選現股當沖候選標的, 產生 index.html 並寄送 Email。
量比以 history.csv 累積的每日成交量計算(前5個交易日均量), 累積未滿5日時留空。
"""

import os
import sys
import logging
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.header import Header

import requests
import pandas as pd

# ---------------- CONFIG ----------------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = SMTP_USER
MAIL_TO = os.environ.get("MAIL_TO", "")

TOP_N = 10
MIN_TURNOVER = 50_000_000
STAGE1_POOL = 40
HISTORY_FILE = "history.csv"
HISTORY_KEEP_DAYS = 10

API_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
API_DT = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def to_num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def find_key(keys, must_contain, exclude=()):
    for k in keys:
        kl = k.lower()
        if all(m.lower() in kl for m in must_contain) and not any(e.lower() in kl for e in exclude):
            return k
    return None


def fetch_json(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def load_history():
    if os.path.exists(HISTORY_FILE):
        return pd.read_csv(HISTORY_FILE, dtype={"code": str})
    return pd.DataFrame(columns=["date", "code", "volume"])


def save_history(hist):
    dates = sorted(hist["date"].unique())
    if len(dates) > HISTORY_KEEP_DAYS:
        hist = hist[hist["date"].isin(dates[-HISTORY_KEEP_DAYS:])]
    hist.to_csv(HISTORY_FILE, index=False)


def build_candidates():
    all_data = fetch_json(API_ALL)
    if not all_data:
        raise RuntimeError("STOCK_DAY_ALL 無資料")
    keys = all_data[0].keys()
    k_code = find_key(keys, ["code"])
    k_name = find_key(keys, ["name"])
    k_vol = find_key(keys, ["tradevolume"])
    k_amt = find_key(keys, ["tradevalue"])
    k_high = find_key(keys, ["highest"])
    k_low = find_key(keys, ["lowest"])
    k_close = find_key(keys, ["closing"])
    if None in (k_code, k_name, k_vol, k_amt, k_high, k_low, k_close):
        raise RuntimeError(f"STOCK_DAY_ALL 欄位對應失敗: {list(keys)}")

    df = pd.DataFrame(all_data)
    df["code"] = df[k_code].astype(str).str.strip()
    df["name"] = df[k_name]
    for src, dst in [(k_vol, "volume"), (k_amt, "amount"), (k_high, "high"), (k_low, "low"), (k_close, "close")]:
        df[dst] = df[src].map(to_num)
    df = df.dropna(subset=["volume", "amount", "high", "low", "close"])
    df = df[df["close"] > 0]

    # 當沖標的名單與當沖成交股數
    dt_data = fetch_json(API_DT)
    dt_vol_map = {}
    eligible = set()
    if dt_data:
        dkeys = dt_data[0].keys()
        dk_code = find_key(dkeys, ["code"]) or find_key(dkeys, ["stock"])
        dk_vol = find_key(dkeys, ["volume"], exclude=["buy", "sell"])
        if dk_code:
            for item in dt_data:
                c = str(item[dk_code]).strip()
                eligible.add(c)
                if dk_vol:
                    v = to_num(item[dk_vol])
                    if v:
                        dt_vol_map[c] = v

    if eligible:
        df = df[df["code"].isin(eligible)]

    df = df[df["amount"] >= MIN_TURNOVER]
    df = df.sort_values("amount", ascending=False).head(STAGE1_POOL).copy()

    df["振幅"] = ((df["high"] - df["low"]) / df["close"] * 100).round(2)
    df["成交金額億"] = (df["amount"] / 1e8).round(2)
    df["當沖占比"] = df.apply(
        lambda r: round(dt_vol_map[r["code"]] / r["volume"] * 100, 1)
        if r["code"] in dt_vol_map and r["volume"] else None,
        axis=1,
    )

    # 量比: 以 history.csv 前5個交易日均量計算
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y%m%d")
    hist = load_history()
    hist = hist[hist["date"].astype(str) != today]

    prev_dates = sorted(hist["date"].astype(str).unique())[-5:]
    vol_avg = {}
    if len(prev_dates) >= 5:
        h5 = hist[hist["date"].astype(str).isin(prev_dates)]
        vol_avg = h5.groupby("code")["volume"].mean().to_dict()

    df["量比"] = df["code"].map(
        lambda c: round(
            df.loc[df["code"] == c, "volume"].iloc[0] / vol_avg[c], 2
        ) if c in vol_avg and vol_avg[c] > 0 else None
    )

    # 寫入今日全市場成交量到 history
    today_rows = pd.DataFrame({
        "date": today,
        "code": df_all_codes(all_data, k_code),
        "volume": [to_num(item[k_vol]) for item in all_data],
    })
    today_rows = today_rows.dropna(subset=["volume"])
    save_history(pd.concat([hist, today_rows], ignore_index=True))

    # 綜合排名
    for c in ["成交金額億", "振幅", "當沖占比", "量比"]:
        df[c + "_rank"] = df[c].rank(ascending=False, na_option="bottom")
    df["score"] = df[["成交金額億_rank", "振幅_rank", "當沖占比_rank", "量比_rank"]].mean(axis=1)
    result = df.sort_values("score").head(TOP_N)

    return today, result


def df_all_codes(all_data, k_code):
    return [str(item[k_code]).strip() for item in all_data]


def format_message(df):
    lines = ["最新交易日 現股當沖候選標的", ""]
    for i, row in enumerate(df.itertuples(), 1):
        reason = f"收盤 {row.close}｜成交金額 {row.成交金額億} 億｜振幅 {row.振幅}%"
        if pd.notna(row.當沖占比):
            reason += f"｜當沖占比 {row.當沖占比}%"
        if pd.notna(row.量比):
            reason += f"｜量比 {row.量比}"
        lines.append(f"{i}. {row.code} {row.name}")
        lines.append(reason)
        lines.append("")
    lines.append("資料為最近交易日收盤統計, 僅供參考, 非投資建議。")
    return "\n".join(lines)


def build_html(df, generated_at):
    rows = ""
    for i, row in enumerate(df.itertuples(), 1):
        dz = "" if pd.isna(row.當沖占比) else f"{row.當沖占比}%"
        lb = "" if pd.isna(row.量比) else f"{row.量比}"
        rows += f"""<tr>
<td>{i}</td><td class="code">{row.code}</td><td>{row.name}</td>
<td class="num">{row.close}</td><td class="num">{row.成交金額億}</td>
<td class="num">{row.振幅}%</td><td class="num">{dz}</td><td class="num">{lb}</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>現股當沖候選標的</title>
<style>
body {{ font-family: "Microsoft JhengHei", sans-serif; margin: 0; padding: 16px; background: #f5f6fa; }}
h1 {{ font-size: 1.2rem; }}
.meta {{ color: #666; font-size: 0.8rem; margin-bottom: 12px; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; font-size: 0.85rem; }}
th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; white-space: nowrap; }}
th {{ background: #4F81BD; color: #fff; }}
tr:nth-child(even) {{ background: #f0f4fa; }}
.num {{ text-align: right; }}
.code {{ font-weight: bold; }}
.note {{ color: #999; font-size: 0.75rem; margin-top: 12px; }}
.wrap {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>現股當沖候選標的 (最新交易日)</h1>
<div class="meta">更新時間: {generated_at} (台北時間)</div>
<div class="wrap">
<table>
<tr><th>#</th><th>代號</th><th>名稱</th><th>收盤</th><th>成交金額(億)</th><th>振幅</th><th>當沖占比</th><th>量比</th></tr>
{rows}
</table>
</div>
<div class="note">資料為最近交易日收盤統計, 僅供參考, 非投資建議。量比需累積5個交易日資料後顯示。</div>
</body>
</html>"""


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = Header(subject, "utf-8")
    recipients = [addr.strip() for addr in MAIL_TO.split(",")]
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(MAIL_FROM, recipients, msg.as_string())


def main():
    try:
        today, df = build_candidates()
        tz = dt.timezone(dt.timedelta(hours=8))
        now = dt.datetime.now(tz).strftime("%Y/%m/%d %H:%M")

        if df.empty:
            html = "<html><body><p>無符合條件的當沖候選標的。</p></body></html>"
        else:
            html = build_html(df, now)

        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html)
        logging.info("index.html 已產生")

        if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
            if df.empty:
                send_email("當沖候選標的", "今日無符合條件的當沖候選標的。")
            else:
                send_email(f"{now[:10]} 現股當沖候選標的", format_message(df))
            logging.info("Email 已寄出")
    except Exception as e:
        logging.exception("執行失敗")
        try:
            if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
                send_email("當沖篩選腳本執行失敗", str(e))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
