"""
screener.py
GitHub Actions 每日執行: 篩選台股現股當沖候選標的, 產生 index.html 並寄送 Email。
"""

import os
import sys
import time
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

TOP_N = 10                 # 推播檔數
MIN_TURNOVER = 50_000_000  # 最低成交金額門檻(元)
STAGE1_POOL = 40           # 先依成交金額保留檔數，再計算5日指標
REQUEST_SLEEP = 1.5        # 逐檔請求間隔(秒)

HEADERS = {"User-Agent": "Mozilla/5.0"}
LOG_FILE = "day_trade_alert.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def find_col(columns, keywords):
    for kw in keywords:
        for c in columns:
            if kw in c:
                return c
    return None


def to_number(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "").str.replace("+", "").str.strip(),
        errors="coerce",
    )


def last_trading_date(max_back=10):
    d = dt.date.today() - dt.timedelta(days=1)
    for _ in range(max_back):
        date_str = d.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date={date_str}&response=json"
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            j = r.json()
            if j.get("data"):
                return date_str, j
        except Exception as e:
            logging.warning(f"檢查 {date_str} 失敗: {e}")
        d -= dt.timedelta(days=1)
    raise RuntimeError("找不到最近交易日資料")


def fetch_day_trading_list(date_str):
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/TWTB4U?date={date_str}&response=json"
    r = requests.get(url, headers=HEADERS, timeout=10)
    j = r.json()
    if not j.get("data"):
        return pd.DataFrame()
    return pd.DataFrame(j["data"], columns=j["fields"])


def fetch_stock_history(stock_no, date_str):
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={date_str}&stockNo={stock_no}&response=json"
    r = requests.get(url, headers=HEADERS, timeout=10)
    j = r.json()
    if not j.get("data"):
        return None
    return pd.DataFrame(j["data"], columns=j["fields"])


def build_candidates():
    date_str, all_json = last_trading_date()
    logging.info(f"使用交易日: {date_str}")

    df_all = pd.DataFrame(all_json["data"], columns=all_json["fields"])
    col_code = find_col(df_all.columns, ["證券代號", "股票代號", "代號"])
    col_name = find_col(df_all.columns, ["證券名稱", "名稱"])
    col_vol = find_col(df_all.columns, ["成交股數"])
    col_amt = find_col(df_all.columns, ["成交金額"])
    col_high = find_col(df_all.columns, ["最高價"])
    col_low = find_col(df_all.columns, ["最低價"])
    col_close = find_col(df_all.columns, ["收盤價"])

    required = [col_code, col_name, col_vol, col_amt, col_high, col_low, col_close]
    if any(c is None for c in required):
        raise RuntimeError(f"STOCK_DAY_ALL 欄位對應失敗: {list(df_all.columns)}")

    df_all[col_amt] = to_number(df_all[col_amt])
    df_all[col_vol] = to_number(df_all[col_vol])
    df_all[col_high] = to_number(df_all[col_high])
    df_all[col_low] = to_number(df_all[col_low])
    df_all[col_close] = to_number(df_all[col_close])

    df_dt = fetch_day_trading_list(date_str)
    col_dt_code = find_col(df_dt.columns, ["證券代號", "股票代號", "代號"])
    col_dt_vol = find_col(df_dt.columns, ["當日沖銷交易成交股數", "當沖成交股數", "成交股數"])
    if col_dt_code is None:
        raise RuntimeError(f"TWTB4U 欄位對應失敗: {list(df_dt.columns)}")

    eligible_codes = set(df_dt[col_dt_code].astype(str).str.strip())

    df = df_all[df_all[col_code].astype(str).str.strip().isin(eligible_codes)].copy()
    df = df[df[col_amt] >= MIN_TURNOVER]
    df = df.sort_values(col_amt, ascending=False).head(STAGE1_POOL)

    if col_dt_vol:
        df_dt_small = df_dt[[col_dt_code, col_dt_vol]].copy()
        df_dt_small[col_dt_vol] = to_number(df_dt_small[col_dt_vol])
        df = df.merge(df_dt_small, left_on=col_code, right_on=col_dt_code, how="left")
        df["當沖占比"] = (df[col_dt_vol] / df[col_vol] * 100).round(1)
    else:
        df["當沖占比"] = None

    df["振幅"] = ((df[col_high] - df[col_low]) / df[col_close] * 100).round(2)
    df["成交金額億"] = (df[col_amt] / 1e8).round(2)

    records = []
    for _, row in df.iterrows():
        code = str(row[col_code]).strip()
        time.sleep(REQUEST_SLEEP)
        try:
            hist = fetch_stock_history(code, date_str)
        except Exception as e:
            logging.warning(f"{code} 歷史資料失敗: {e}")
            hist = None

        vol_ratio = None
        if hist is not None and len(hist) >= 6:
            h_vol_col = find_col(hist.columns, ["成交股數"])
            if h_vol_col:
                hv = to_number(hist[h_vol_col]).dropna()
                if len(hv) >= 6:
                    avg5_vol = hv.iloc[-6:-1].mean()
                    today_vol = hv.iloc[-1]
                    if avg5_vol and avg5_vol > 0:
                        vol_ratio = round(today_vol / avg5_vol, 2)

        records.append({
            "代號": code,
            "名稱": row[col_name],
            "收盤價": row[col_close],
            "成交金額億": row["成交金額億"],
            "振幅": row["振幅"],
            "當沖占比": row["當沖占比"],
            "量比": vol_ratio,
        })

    result = pd.DataFrame(records)
    for c in ["成交金額億", "振幅", "當沖占比", "量比"]:
        result[c + "_rank"] = result[c].rank(ascending=False, na_option="bottom")

    result["score"] = result[
        ["成交金額億_rank", "振幅_rank", "當沖占比_rank", "量比_rank"]
    ].mean(axis=1)
    result = result.sort_values("score").head(TOP_N)

    return date_str, result


def format_message(date_str, df):
    d = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
    lines = [f"{d} 現股當沖候選標的", ""]
    for i, row in enumerate(df.itertuples(), 1):
        reason = f"收盤 {row.收盤價}｜成交金額 {row.成交金額億} 億｜振幅 {row.振幅}%"
        if pd.notna(row.當沖占比):
            reason += f"｜當沖占比 {row.當沖占比}%"
        if row.量比:
            reason += f"｜量比 {row.量比}"
        lines.append(f"{i}. {row.代號} {row.名稱}")
        lines.append(reason)
        lines.append("")
    lines.append("資料為前一交易日收盤統計，僅供參考，非投資建議。")
    return "\n".join(lines)


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


def build_html(date_str, df, generated_at):
    d = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
    rows = ""
    for i, row in enumerate(df.itertuples(), 1):
        dz = "" if pd.isna(row.當沖占比) else f"{row.當沖占比}%"
        lb = "" if not row.量比 else f"{row.量比}"
        rows += f"""<tr>
<td>{i}</td><td class="code">{row.代號}</td><td>{row.名稱}</td>
<td class="num">{row.收盤價}</td><td class="num">{row.成交金額億}</td>
<td class="num">{row.振幅}%</td><td class="num">{dz}</td><td class="num">{lb}</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{d} 現股當沖候選標的</title>
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
<h1>{d} 現股當沖候選標的</h1>
<div class="meta">更新時間: {generated_at} (台北時間)</div>
<div class="wrap">
<table>
<tr><th>#</th><th>代號</th><th>名稱</th><th>收盤</th><th>成交金額(億)</th><th>振幅</th><th>當沖占比</th><th>量比</th></tr>
{rows}
</table>
</div>
<div class="note">資料為前一交易日收盤統計, 僅供參考, 非投資建議。</div>
</body>
</html>"""


def main():
    try:
        date_str, df = build_candidates()
        tz = dt.timezone(dt.timedelta(hours=8))
        now = dt.datetime.now(tz).strftime("%Y/%m/%d %H:%M")
        d = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

        if df.empty:
            html = f"<html><body><p>{d} 無符合條件的當沖候選標的。</p></body></html>"
        else:
            html = build_html(date_str, df, now)

        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html)
        logging.info("index.html 已產生")

        if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
            if df.empty:
                send_email(f"{d} 當沖候選標的", "今日無符合條件的當沖候選標的。")
            else:
                send_email(f"{d} 現股當沖候選標的", format_message(date_str, df))
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
