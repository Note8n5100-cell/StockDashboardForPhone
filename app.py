"""
10 檔個股即時走勢小工具（Streamlit，支援手機）

執行方式：
    pip install streamlit yfinance pandas
    streamlit run stock_dashboard.py --server.address 0.0.0.0

手機（與電腦同一個 Wi-Fi）開啟： http://電腦的區網IP:8501

版面採用 CSS Grid + 內嵌 SVG 走勢圖：
  手機 → 每列 1 檔；平板/電腦 → 每列 2 檔（或更多），卡片與走勢圖永遠左右並排。
台股慣例：紅色 = 上漲、綠色 = 下跌；藍線 = 昨收價。資料來源 Yahoo Finance，可能有延遲。
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="個股即時走勢",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",  # 手機上預設收起設定欄
)

# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------
DEFAULT_STOCKS = """3481,群創
2409,友達
2610,華航
2317,鴻海
2328,廣宇
2330,台積電
2454,聯發科
2603,長榮
2881,富邦金
2002,中鋼"""

UP, DOWN, FLAT = "#ff4b3e", "#00c853", "#cccccc"
LIMIT_PCT = 9.5  # 接近漲跌停（±10%）時，整張卡片反白強調
W, H = 300, 120  # SVG 內部座標系（會自動縮放）
SESSION_MIN = 270  # 09:00 ~ 13:30

CSS = """
<style>
  .block-container {padding: 1rem 0.8rem 1rem 0.8rem; max-width: 1400px;}
  header[data-testid="stHeader"] {height: 2.5rem;}
  .grid {display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:10px;}
  .card {display:flex; height:130px; border:1px solid #333; background:#000;}
  .info {flex:0 0 42%; display:flex; flex-direction:column; min-width:0;}
  .head {display:flex; justify-content:space-between; align-items:center;
         padding:6px 10px; background:#2b2b2b; color:#fff;}
  .name {font-size:20px; font-weight:600; white-space:nowrap;}
  .code {font-size:13px; color:#aaa;}
  .body {flex:1; display:flex; flex-direction:column; justify-content:center; align-items:center;}
  .price {font-size:32px; font-weight:600; line-height:1.1;}
  .chg {display:flex; gap:16px; font-size:16px; margin-top:4px;}
  .chart {flex:1; position:relative; padding:5px 6px 17px 6px; min-width:0;}
  .chart svg {display:block; width:100%; height:100%;}
  .xl {position:absolute; left:6px; right:6px; bottom:1px; height:14px;}
  .xl span {position:absolute; transform:translateX(-50%); font-size:11px; color:#ddd;}
  .up-limit {background:#5a0e08;}
  .up-limit .head {background:#a3221a;}
  .down-limit {background:#06401c;}
  .down-limit .head {background:#13803a;}
  .nodata {color:#888; font-size:15px;}
  @media (max-width: 480px) {
    .card {height:118px;}
    .name {font-size:17px;} .code {font-size:12px;}
    .head {padding:5px 8px;}
    .price {font-size:27px;} .chg {font-size:14px; gap:12px;}
  }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# 側邊欄
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("設定")
    raw = st.text_area("股票清單（每行：代號,名稱，最多 10 檔）", DEFAULT_STOCKS, height=260)
    market = st.radio("預設市場", ["上市 (.TW)", "上櫃 (.TWO)"], horizontal=True)
    refresh = st.slider("自動更新間隔（秒）", 10, 300, 30, step=10)
    st.caption("個別上櫃股票可直接寫 `6488.TWO,環球晶`。")

suffix = ".TW" if market.startswith("上市") else ".TWO"


def parse_stocks(text: str):
    stocks = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.replace("，", ",").split(",")]
        if not parts or not parts[0]:
            continue
        code = parts[0]
        name = parts[1] if len(parts) > 1 else code
        symbol = code if "." in code else code + suffix
        stocks.append((code.split(".")[0], name, symbol))
    return stocks[:10]


STOCKS = parse_stocks(raw)


# ----------------------------------------------------------------------------
# 資料
# ----------------------------------------------------------------------------
@st.cache_data(ttl=15, show_spinner=False)
def fetch_intraday(symbol: str) -> pd.DataFrame:
    """當日 1 分鐘收盤價序列（台北時間）。"""
    df = yf.Ticker(symbol).history(period="1d", interval="1m", auto_adjust=False)
    if df.empty:
        return df
    df.index = df.index.tz_convert("Asia/Taipei").tz_localize(None)
    return df[["Close"]].dropna()


@st.cache_data(ttl=600, show_spinner=False)
def fetch_prev_close(symbol: str, trade_day: dt.date):
    """trade_day 之前最近一個交易日的收盤價（昨收）。"""
    daily = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=False)
    if daily.empty:
        return None
    daily.index = daily.index.tz_localize(None).normalize()
    prev = daily[daily.index.date < trade_day]["Close"].dropna()
    return float(prev.iloc[-1]) if not prev.empty else None


def load(stock):
    code, name, symbol = stock
    try:
        df = fetch_intraday(symbol)
        if df.empty:
            return code, name, None, None
        prev = fetch_prev_close(symbol, df.index[-1].date())
        return code, name, df, prev
    except Exception:
        return code, name, None, None


# ----------------------------------------------------------------------------
# 繪圖（內嵌 SVG，可隨螢幕寬度縮放）
# ----------------------------------------------------------------------------
def svg_sparkline(df: pd.DataFrame, prev: float, color: str) -> str:
    t0 = df.index[-1].normalize() + pd.Timedelta(hours=9)
    secs = (df.index - t0).total_seconds()
    xs = [min(max(float(t) / (SESSION_MIN * 60), 0.0), 1.0) * W for t in secs]
    vals = [float(v) for v in df["Close"]]

    lo, hi = min(min(vals), prev), max(max(vals), prev)
    pad = (hi - lo) * 0.15 or hi * 0.005
    lo, hi = lo - pad, hi + pad

    def y(v):
        return H - (v - lo) / (hi - lo) * H

    d = f"M{xs[0]:.1f},{y(vals[0]):.1f}"
    for x, v in zip(xs[1:], vals[1:]):  # 階梯線，與看盤軟體一致
        d += f"H{x:.1f}V{y(v):.1f}"

    grid = "".join(
        f'<line x1="{(h - 9) * 60 / SESSION_MIN * W:.1f}" x2="{(h - 9) * 60 / SESSION_MIN * W:.1f}" '
        f'y1="0" y2="{H}" stroke="#666" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        for h in range(10, 14)
    )
    return (
        f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="none" stroke="#bbb" '
        f'stroke-width="1" vector-effect="non-scaling-stroke"/>'
        f"{grid}"
        f'<line x1="0" x2="{W}" y1="{y(prev):.1f}" y2="{y(prev):.1f}" stroke="#3b82c4" '
        f'stroke-width="1.5" vector-effect="non-scaling-stroke"/>'
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3" '
        f'vector-effect="non-scaling-stroke"/></svg>'
    )


def x_labels() -> str:
    return "".join(
        f'<span style="left:{(h - 9) * 60 / SESSION_MIN * 100:.2f}%">{h:02d}</span>'
        for h in range(9, 14)
    )


def card_html(code, name, df, prev) -> str:
    if df is None or prev is None:
        return (
            f'<div class="card"><div class="info"><div class="head"><span class="name">{name}</span>'
            f'<span class="code">{code}</span></div><div class="body nodata">暫無資料</div></div>'
            f'<div class="chart"></div></div>'
        )
    price = float(df["Close"].iloc[-1])
    chg = price - prev
    pct = chg / prev * 100
    color = UP if chg > 0 else DOWN if chg < 0 else FLAT
    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "–"
    cls = "up-limit" if pct >= LIMIT_PCT else "down-limit" if pct <= -LIMIT_PCT else ""
    text_color = "#fff" if cls else color
    return (
        f'<div class="card {cls}"><div class="info">'
        f'<div class="head"><span class="name">{name}</span><span class="code">{code}</span></div>'
        f'<div class="body"><div class="price" style="color:{text_color}">{price:,.2f}</div>'
        f'<div class="chg" style="color:{text_color}"><span>{arrow} {abs(chg):.2f}</span>'
        f"<span>{abs(pct):.2f}%</span></div></div></div>"
        f'<div class="chart">{svg_sparkline(df, prev, color)}<div class="xl">{x_labels()}</div></div></div>'
    )


# ----------------------------------------------------------------------------
# 主畫面（fragment 會自動定時重繪）
# ----------------------------------------------------------------------------
st.markdown("### 📈 個股即時走勢")


@st.fragment(run_every=refresh)
def dashboard():
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    st.caption(f"更新：{now:%m/%d %H:%M:%S}｜每 {refresh} 秒自動更新｜紅漲綠跌，藍線＝昨收")
    with ThreadPoolExecutor(max_workers=10) as ex:  # 10 檔同時抓，縮短等待
        results = list(ex.map(load, STOCKS))
    cards = "".join(card_html(*r) for r in results)
    st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)


dashboard()
