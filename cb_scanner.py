#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可轉債策略儀表板 v2 - 台灣股市
Convertible Bond Strategy Dashboard for Taiwan Stock Market

策略邏輯:
  現股買進訊號:
    1. 融券餘額_t < 融券餘額_{t-1} 且連續兩日
    2. 收盤價 > 開盤價 (紅K) 且連續兩日以上
    3. 股本小於 30 億
    4. 交易量 > 10 日均量

  現股賣出訊號:
    1. 融券餘額_t < 融券餘額_{t-1} 且至少減少 200 張
    2. 股價 > 10 日均線
    3. 股本小於 30 億

新增功能 (v2):
  ① 可轉債成交量分析：CB 日成交金額 vs 公司市值，評估流動性
  ② 公司歷史 CB 記錄：同公司過去可轉債成功/失敗案例
  ③ 自動交易留檔：記錄買賣訊號觸發時的進出場價格與損益
  ④ CBAS 時間節點：上市第 6 交易日（可拆分）及第 90 天（可轉換）警示

使用方式:
    pip install -r requirements.txt
    python cb_scanner.py
    => 輸出 cb_dashboard.html (可嵌入 WordPress)
"""

import requests
import pandas as pd
from bs4 import BeautifulSoup
import yfinance as yf
from datetime import datetime, timedelta
import json
import time
import logging
import os
import re
import uuid

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ================================================================
# 設定參數
# ================================================================
CAPITAL_THRESHOLD_亿 = 30       # 股本門檻 (億元)
VOLUME_MA_DAYS = 10              # 交易量均線天數
PRICE_MA_DAYS = 10               # 股價均線天數
MIN_SHORT_DECREASE_LOTS = 200    # 融券最小減少張數
SHORT_DECREASE_CONSECUTIVE = 2   # 融券連續下降天數
RED_CANDLE_CONSECUTIVE = 2       # 連續紅K天數

# 可轉債成交量門檻
CB_MIN_DAILY_LOTS = 50           # 最低日成交量 (張)
CB_MIN_TURNOVER_NTD = 5_000_000  # 最低日成交金額 (500萬 NTD)
CB_TURNOVER_VS_MCAP_PCT = 0.05   # 成交金額 / 市值 最低比例 (%)

# CBAS 時間節點
CBAS_SPLIT_DAY = 6               # 上市第幾個交易日可以拆分
CONVERSION_DAYS = 90             # 上市幾天後可轉換 (自然日)
CBAS_ALERT_WINDOW = 2            # 前後幾個交易日觸發警示

OUTPUT_FILE = "cb_dashboard.html"
HISTORY_FILE = "cb_history.json"   # 歷史 CB 記錄
TRADE_LOG_FILE = "trade_log.json"  # 交易留檔

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}


# ================================================================
# 1. 抓取可轉債清單
# ================================================================

def fetch_cb_list_thefew() -> pd.DataFrame | None:
    """從 thefew.tw/cb 抓取可轉債清單 (含上市日期、CB 成交量)"""
    logger.info("嘗試從 thefew.tw/cb 抓取資料...")
    try:
        resp = requests.get(
            'https://thefew.tw/cb',
            headers={**HEADERS, 'Referer': 'https://thefew.tw/'},
            timeout=15
        )
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 嘗試從 <script> 找嵌入 JSON (Nuxt/Next)
        for script in soup.find_all('script'):
            text = script.string or ''
            if '__NUXT_DATA__' in text or 'window.__INITIAL_STATE__' in text:
                match = re.search(r'(\[|\{).*(\]|\})', text, re.DOTALL)
                if match:
                    try:
                        raw = json.loads(match.group(0))
                        logger.info("從嵌入 JSON 解析到資料")
                    except Exception:
                        pass

        # 嘗試解析 HTML table
        tables = pd.read_html(resp.text)
        for df in tables:
            if len(df) > 5:
                logger.info(f"從 thefew.tw 表格解析到 {len(df)} 筆資料")
                return normalize_thefew_df(df)

    except Exception as e:
        logger.warning(f"thefew.tw 抓取失敗: {e}")
    return None


def normalize_thefew_df(df: pd.DataFrame) -> pd.DataFrame:
    """標準化 thefew.tw 的欄位名稱"""
    col_map = {}
    for col in df.columns:
        c = str(col).strip()
        if '代碼' in c or 'code' in c.lower():
            col_map[col] = 'cb_code'
        elif '名稱' in c or 'name' in c.lower():
            col_map[col] = 'cb_name'
        elif '轉換價' in c or '換價' in c:
            col_map[col] = 'conversion_price'
        elif '現價' in c or '市價' in c:
            col_map[col] = 'cb_price'
        elif '溢價' in c:
            col_map[col] = 'premium_rate'
        elif '發行' in c and '價' in c:
            col_map[col] = 'issue_price'
        elif '到期' in c:
            col_map[col] = 'maturity_date'
        elif '上市' in c or 'listing' in c.lower():
            col_map[col] = 'listing_date'
        elif '成交量' in c or 'volume' in c.lower():
            col_map[col] = 'cb_volume'
        elif '標的' in c or '股票' in c:
            col_map[col] = 'stock_code'
    df = df.rename(columns=col_map)
    return df


def fetch_cb_list_twse() -> pd.DataFrame | None:
    """從 TWSE Open API 抓取可轉換公司債資料"""
    logger.info("嘗試從 TWSE Open API 抓取可轉債...")
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if data:
            df = pd.DataFrame(data)
            logger.info(f"TWSE 取得 {len(df)} 筆資料，欄位: {list(df.columns)}")
            return df
    except Exception as e:
        logger.warning(f"TWSE API 失敗: {e}")
    return None


def fetch_cb_list_tpex() -> pd.DataFrame | None:
    """從 TPEx 抓取上櫃可轉債"""
    logger.info("嘗試從 TPEx 抓取可轉債...")
    try:
        url = "https://www.tpex.org.tw/web/bond/bond_cb_info/bond_cb_query.php?l=zh-tw"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        tables = pd.read_html(resp.text, encoding='utf-8')
        if tables:
            df = tables[0]
            logger.info(f"TPEx 取得 {len(df)} 筆可轉債資料")
            return normalize_tpex_df(df)
    except Exception as e:
        logger.warning(f"TPEx 抓取失敗: {e}")
    return None


def normalize_tpex_df(df: pd.DataFrame) -> pd.DataFrame:
    """標準化 TPEx 欄位"""
    col_map = {}
    for col in df.columns:
        c = str(col).strip()
        if '債券代號' in c or '代號' in c:
            col_map[col] = 'cb_code'
        elif '債券名稱' in c or '名稱' in c:
            col_map[col] = 'cb_name'
        elif '轉換價格' in c:
            col_map[col] = 'conversion_price'
        elif '標的股票代號' in c or '股票代號' in c:
            col_map[col] = 'stock_code'
        elif '標的股票名稱' in c:
            col_map[col] = 'stock_name'
        elif '到期日' in c:
            col_map[col] = 'maturity_date'
        elif '上市' in c or '掛牌' in c:
            col_map[col] = 'listing_date'
    return df.rename(columns=col_map)


def get_cb_list() -> pd.DataFrame:
    """主要入口：依序嘗試各資料源，回傳標準化的可轉債 DataFrame"""
    for fetch_fn in [fetch_cb_list_thefew, fetch_cb_list_twse, fetch_cb_list_tpex]:
        df = fetch_fn()
        if df is not None and len(df) > 0:
            for col in ['cb_code', 'cb_name', 'stock_code']:
                if col not in df.columns:
                    df[col] = ''
            # 確保 listing_date 欄位存在
            if 'listing_date' not in df.columns:
                df['listing_date'] = None
            return df

    logger.warning("所有資料源皆失敗，使用示範資料")
    return get_demo_cb_data()


def get_demo_cb_data() -> pd.DataFrame:
    """示範用資料（含上市日期、歷史 CB 記錄）"""
    today = datetime.now()

    def td(n):
        """計算 n 個交易日前的日期（含週末補償）"""
        d = today
        count = 0
        while count < abs(n):
            d -= timedelta(days=1)
            if d.weekday() < 5:
                count += 1
        return d.strftime('%Y-%m-%d')

    def cd(n):
        """n 個自然日前的日期"""
        return (today - timedelta(days=n)).strftime('%Y-%m-%d')

    return pd.DataFrame([
        # 第 5 個交易日 → 明天是 day 6 (CBAS 可拆分前夕)
        {'cb_code': '91611', 'cb_name': '儒鴻一', 'stock_code': '1476',
         'stock_name': '儒鴻', 'conversion_price': 285.0, 'cb_price': 102.5,
         'premium_rate': 2.5, 'issue_price': 100, 'maturity_date': '2028-06-30',
         'listing_date': td(5), 'cb_volume': 320},

        # 正好第 6 個交易日 → 今天可拆分！
        {'cb_code': '91651', 'cb_name': '雷虎一', 'stock_code': '8033',
         'stock_name': '雷虎', 'conversion_price': 45.0, 'cb_price': 103.2,
         'premium_rate': 3.2, 'issue_price': 100, 'maturity_date': '2028-08-15',
         'listing_date': td(6), 'cb_volume': 88},

        # 第 88 自然日 → 接近 3 個月可轉換
        {'cb_code': '98451', 'cb_name': '台半一', 'stock_code': '5425',
         'stock_name': '台半', 'conversion_price': 78.5, 'cb_price': 101.8,
         'premium_rate': 1.8, 'issue_price': 100, 'maturity_date': '2028-03-20',
         'listing_date': cd(88), 'cb_volume': 45},

        # 正常期間
        {'cb_code': '34501', 'cb_name': '聯電一', 'stock_code': '2303',
         'stock_name': '聯電', 'conversion_price': 55.0, 'cb_price': 99.5,
         'premium_rate': -0.5, 'issue_price': 100, 'maturity_date': '2026-12-31',
         'listing_date': cd(200), 'cb_volume': 1200},

        # 流動性不足範例
        {'cb_code': '23871', 'cb_name': '中鋼構一', 'stock_code': '2390',
         'stock_name': '中鋼構', 'conversion_price': 62.0, 'cb_price': 104.0,
         'premium_rate': 4.0, 'issue_price': 100, 'maturity_date': '2027-09-10',
         'listing_date': cd(150), 'cb_volume': 18},
    ])


# ================================================================
# 2. 抓取股票資料
# ================================================================

def get_stock_ohlcv(stock_code: str, days: int = 35) -> pd.DataFrame:
    """用 yfinance 抓取台股 OHLCV"""
    for suffix in ['.TW', '.TWO']:
        try:
            ticker = yf.Ticker(f"{stock_code}{suffix}")
            hist = ticker.history(period=f"{days}d", auto_adjust=True)
            if len(hist) >= 5:
                if hist.index.tz is not None:
                    hist.index = hist.index.tz_localize(None)
                return hist
        except Exception as e:
            logger.debug(f"  {stock_code}{suffix}: {e}")
    return pd.DataFrame()


def get_stock_info(stock_code: str) -> dict:
    """取得股票基本資訊（股本、市值等）"""
    for suffix in ['.TW', '.TWO']:
        try:
            ticker = yf.Ticker(f"{stock_code}{suffix}")
            info = ticker.info
            shares = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding') or 0
            price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
            market_cap = info.get('marketCap') or (shares * price)
            capital_亿 = round(shares * 10 / 1e8, 2) if shares > 0 else None
            return {
                'capital_亿': capital_亿,
                'market_cap': market_cap,       # 市值 (NTD)
                'shares': shares,
                'suffix': suffix
            }
        except Exception:
            pass
    return {'capital_亿': None, 'market_cap': 0, 'shares': 0, 'suffix': ''}


def get_margin_short_data(stock_code: str) -> pd.DataFrame:
    """
    從 TWSE 取得融券餘額資料
    API: https://www.twse.com.tw/exchangeReport/MI_MARGN

    欄位索引 (0-based):
      0: 股票代號, 1: 名稱, 5: 融資餘額, 13: 融券餘額, 15: 融券增減
    """
    results = []
    current_date = datetime.now()
    attempted = 0

    while len(results) < 5 and attempted < 20:
        if current_date.weekday() >= 5:
            current_date -= timedelta(days=1)
            attempted += 1
            continue

        date_str = current_date.strftime('%Y%m%d')
        url = (
            f"https://www.twse.com.tw/exchangeReport/MI_MARGN"
            f"?response=json&date={date_str}&selectType=ALL"
        )
        try:
            resp = requests.get(
                url,
                headers={**HEADERS, 'Referer': 'https://www.twse.com.tw/'},
                timeout=10
            )
            data = resp.json()
            if data.get('stat') == 'OK' and data.get('data'):
                for row in data['data']:
                    if str(row[0]).strip() == str(stock_code).strip():
                        def ci(val):
                            try:
                                return int(str(val).replace(',', '').replace('-', '0'))
                            except Exception:
                                return 0
                        results.append({
                            'date': pd.to_datetime(date_str),
                            'stock_code': stock_code,
                            'margin_balance': ci(row[5]),
                            'short_balance': ci(row[13]),
                            'short_change': ci(row[15]),
                        })
                        break
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"  融券 {date_str}: {e}")

        current_date -= timedelta(days=1)
        attempted += 1

    if results:
        df = pd.DataFrame(results)
        return df.sort_values('date').reset_index(drop=True)
    return pd.DataFrame()


def fetch_cb_daily_volume(cb_code: str) -> int | None:
    """
    從 TWSE 抓取可轉債當日成交量 (張)
    使用 BWIBBU 債券交易資料
    endpoint: https://www.twse.com.tw/exchangeReport/BWIBBU?response=json&date=YYYYMMDD
    """
    today = datetime.now()
    for offset in range(5):
        d = today - timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        date_str = d.strftime('%Y%m%d')
        url = (
            f"https://www.twse.com.tw/exchangeReport/BWIBBU"
            f"?response=json&date={date_str}"
        )
        try:
            resp = requests.get(
                url,
                headers={**HEADERS, 'Referer': 'https://www.twse.com.tw/'},
                timeout=10
            )
            data = resp.json()
            if data.get('stat') == 'OK' and data.get('data'):
                for row in data['data']:
                    # row[0] = 債券代號, row[2] = 成交量
                    if str(row[0]).strip() == str(cb_code).strip():
                        try:
                            vol = int(str(row[2]).replace(',', ''))
                            return vol
                        except Exception:
                            return None
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"  CB成交量 {date_str}: {e}")
    return None


# ================================================================
# ① 新功能：可轉債成交量分析
# ================================================================

def analyze_cb_volume(
    cb_volume_lots: int | None,
    cb_price: float,
    market_cap_ntd: float
) -> dict:
    """
    評估可轉債流動性。

    計算邏輯：
      1 張 CB = 面值 10 萬 NTD
      CB 日成交金額 = cb_volume_lots × (cb_price / 100) × 100,000
      流動性比率 = CB 日成交金額 / 公司市值 × 100  (%)

    評級：
      🟢 充足 (Adequate):  量 ≥ 50 張 AND 成交額 ≥ 500 萬 NTD
      🟡 偏低 (Low):       量 ≥ 20 張 但未達充足
      🔴 不足 (Thin):      量 < 20 張
    """
    if cb_volume_lots is None:
        return {
            'cb_volume_lots': None,
            'cb_turnover_ntd': None,
            'liquidity_ratio_pct': None,
            'liquidity_grade': 'unknown',
            'liquidity_label': '─ 無資料',
            'is_adequate': False,
        }

    face_value = 100_000  # 每張面值 10 萬
    cb_turnover_ntd = cb_volume_lots * (cb_price / 100) * face_value

    liq_ratio = (cb_turnover_ntd / market_cap_ntd * 100) if market_cap_ntd > 0 else 0

    if cb_volume_lots >= CB_MIN_DAILY_LOTS and cb_turnover_ntd >= CB_MIN_TURNOVER_NTD:
        grade = 'adequate'
        label = f'🟢 充足 ({cb_volume_lots:,} 張)'
        is_adequate = True
    elif cb_volume_lots >= 20:
        grade = 'low'
        label = f'🟡 偏低 ({cb_volume_lots:,} 張)'
        is_adequate = False
    else:
        grade = 'thin'
        label = f'🔴 不足 ({cb_volume_lots:,} 張)'
        is_adequate = False

    return {
        'cb_volume_lots': cb_volume_lots,
        'cb_turnover_ntd': cb_turnover_ntd,
        'liquidity_ratio_pct': round(liq_ratio, 4),
        'liquidity_grade': grade,
        'liquidity_label': label,
        'is_adequate': is_adequate,
    }


# ================================================================
# ② 新功能：公司歷史 CB 記錄
# ================================================================

class CBHistoryDB:
    """管理公司歷史可轉債記錄（讀寫 cb_history.json）"""

    def __init__(self, filepath: str = HISTORY_FILE):
        self.filepath = filepath
        self.records = self._load()

    def _load(self) -> list[dict]:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save(self):
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.records, f, ensure_ascii=False, indent=2)

    def get_company_history(self, stock_code: str) -> list[dict]:
        """取得特定公司的所有歷史 CB 記錄"""
        return [r for r in self.records if str(r.get('stock_code', '')) == str(stock_code)]

    def add_record(self, record: dict):
        """新增或更新一筆歷史記錄"""
        # 以 cb_code 作為唯一鍵
        existing = next((r for r in self.records if r.get('cb_code') == record.get('cb_code')), None)
        if existing:
            existing.update(record)
        else:
            self.records.append(record)
        self.save()

    def get_company_stats(self, stock_code: str) -> dict:
        """計算公司歷史 CB 的統計摘要"""
        hist = self.get_company_history(stock_code)
        closed = [r for r in hist if r.get('status') == 'closed']
        if not closed:
            return {'total': len(hist), 'closed': 0, 'profit_count': 0,
                    'loss_count': 0, 'win_rate': None, 'avg_pnl_pct': None}

        profit_count = sum(1 for r in closed if (r.get('pnl_pct') or 0) > 0)
        loss_count = len(closed) - profit_count
        avg_pnl = sum(r.get('pnl_pct', 0) for r in closed) / len(closed)
        return {
            'total': len(hist),
            'closed': len(closed),
            'profit_count': profit_count,
            'loss_count': loss_count,
            'win_rate': round(profit_count / len(closed) * 100, 1),
            'avg_pnl_pct': round(avg_pnl, 2),
        }


def init_sample_history(db: CBHistoryDB):
    """初始化示範歷史資料（首次執行時）"""
    if db.records:
        return  # 已有資料，不覆蓋

    samples = [
        # 儒鴻 (1476) 歷史
        {'cb_code': '91601', 'cb_name': '儒鴻一 (已結)', 'stock_code': '1476',
         'company_name': '儒鴻', 'issue_date': '2021-03-10',
         'maturity_date': '2024-03-10', 'issue_price': 100,
         'conversion_price': 220.0, 'exit_price': 115.2,
         'pnl_pct': 15.2, 'status': 'closed', 'result': 'profit',
         'exit_reason': '到期前提前贖回', 'notes': ''},
        {'cb_code': '91591', 'cb_name': '儒鴻零 (已結)', 'stock_code': '1476',
         'company_name': '儒鴻', 'issue_date': '2019-06-01',
         'maturity_date': '2022-06-01', 'issue_price': 100,
         'conversion_price': 180.0, 'exit_price': 122.5,
         'pnl_pct': 22.5, 'status': 'closed', 'result': 'profit',
         'exit_reason': '股價高漲後轉換', 'notes': ''},

        # 雷虎 (8033) 歷史
        {'cb_code': '91641', 'cb_name': '雷虎零 (已結)', 'stock_code': '8033',
         'company_name': '雷虎', 'issue_date': '2022-11-01',
         'maturity_date': '2025-11-01', 'issue_price': 100,
         'conversion_price': 42.0, 'exit_price': 98.5,
         'pnl_pct': -1.5, 'status': 'closed', 'result': 'loss',
         'exit_reason': '到期到底贖回', 'notes': '股價未達轉換價'},

        # 台半 (5425) 歷史
        {'cb_code': '98441', 'cb_name': '台半零 (已結)', 'stock_code': '5425',
         'company_name': '台半', 'issue_date': '2020-08-15',
         'maturity_date': '2023-08-15', 'issue_price': 100,
         'conversion_price': 65.0, 'exit_price': 109.8,
         'pnl_pct': 9.8, 'status': 'closed', 'result': 'profit',
         'exit_reason': '轉換成股票', 'notes': ''},

        # 聯電 (2303) 歷史
        {'cb_code': '34491', 'cb_name': '聯電零 (已結)', 'stock_code': '2303',
         'company_name': '聯電', 'issue_date': '2021-01-20',
         'maturity_date': '2024-01-20', 'issue_price': 100,
         'conversion_price': 50.0, 'exit_price': 103.2,
         'pnl_pct': 3.2, 'status': 'closed', 'result': 'profit',
         'exit_reason': '期間獲利出場', 'notes': ''},
    ]
    for s in samples:
        db.add_record(s)
    logger.info(f"已初始化 {len(samples)} 筆示範歷史記錄到 {db.filepath}")


# ================================================================
# ③ 新功能：自動交易留檔
# ================================================================

class TradeLogger:
    """
    自動記錄買賣訊號觸發的進出場，並計算損益。
    資料儲存在 trade_log.json，每次執行時自動更新。
    """

    def __init__(self, filepath: str = TRADE_LOG_FILE):
        self.filepath = filepath
        self.trades = self._load()

    def _load(self) -> list[dict]:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save(self):
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)

    def _find_open(self, cb_code: str) -> dict | None:
        return next(
            (t for t in self.trades if t['cb_code'] == cb_code and t['status'] == 'open'),
            None
        )

    def record_entry(
        self,
        cb_code: str,
        cb_name: str,
        stock_code: str,
        cb_price: float,
        stock_price: float,
        signal_type: str,
        conditions: dict,
    ):
        """記錄新的進場（如果該 CB 已有未平倉記錄則跳過）"""
        if self._find_open(cb_code):
            return  # 已有開倉記錄，不重複

        trade = {
            'id': str(uuid.uuid4())[:8],
            'cb_code': cb_code,
            'cb_name': cb_name,
            'stock_code': stock_code,
            'signal_type': signal_type,           # 'buy' or 'sell'
            'entry_cb_price': cb_price,
            'entry_stock_price': stock_price,
            'entry_date': datetime.now().strftime('%Y-%m-%d'),
            'entry_time': datetime.now().strftime('%H:%M'),
            'entry_conditions': conditions,
            'exit_cb_price': None,
            'exit_stock_price': None,
            'exit_date': None,
            'cb_pnl_pct': None,
            'stock_pnl_pct': None,
            'status': 'open',
            'result': None,
            'notes': '',
        }
        self.trades.append(trade)
        self.save()
        logger.info(f"  📝 新交易記錄: {cb_code} ({cb_name}) [{signal_type.upper()}] @ CB {cb_price}")

    def update_prices(self, cb_code: str, current_cb_price: float, current_stock_price: float):
        """更新未平倉部位的現價（用於計算浮動損益）"""
        trade = self._find_open(cb_code)
        if trade:
            trade['current_cb_price'] = current_cb_price
            trade['current_stock_price'] = current_stock_price
            # 浮動損益
            trade['unrealized_cb_pnl_pct'] = round(
                (current_cb_price - trade['entry_cb_price']) / trade['entry_cb_price'] * 100, 2
            )
            self.save()

    def record_exit(
        self,
        cb_code: str,
        exit_cb_price: float,
        exit_stock_price: float,
        notes: str = ''
    ):
        """手動平倉並記錄損益"""
        trade = self._find_open(cb_code)
        if not trade:
            logger.warning(f"找不到 {cb_code} 的未平倉記錄")
            return

        trade['exit_cb_price'] = exit_cb_price
        trade['exit_stock_price'] = exit_stock_price
        trade['exit_date'] = datetime.now().strftime('%Y-%m-%d')
        trade['cb_pnl_pct'] = round(
            (exit_cb_price - trade['entry_cb_price']) / trade['entry_cb_price'] * 100, 2
        )
        trade['stock_pnl_pct'] = round(
            (exit_stock_price - trade['entry_stock_price']) / trade['entry_stock_price'] * 100, 2
        )
        trade['status'] = 'closed'
        trade['result'] = 'profit' if trade['cb_pnl_pct'] > 0 else 'loss'
        trade['notes'] = notes
        self.save()
        logger.info(
            f"  ✅ 平倉: {cb_code} PnL = {trade['cb_pnl_pct']:+.2f}%  ({trade['result'].upper()})"
        )

    def get_summary(self) -> dict:
        closed = [t for t in self.trades if t['status'] == 'closed']
        open_trades = [t for t in self.trades if t['status'] == 'open']
        profit = [t for t in closed if t.get('result') == 'profit']
        loss = [t for t in closed if t.get('result') == 'loss']
        avg_pnl = sum(t.get('cb_pnl_pct', 0) for t in closed) / len(closed) if closed else 0
        return {
            'total': len(self.trades),
            'open': len(open_trades),
            'closed': len(closed),
            'profit': len(profit),
            'loss': len(loss),
            'win_rate': round(len(profit) / len(closed) * 100, 1) if closed else None,
            'avg_pnl_pct': round(avg_pnl, 2),
        }


def auto_record_signals(results: list[dict], cb_df: pd.DataFrame, logger_obj: TradeLogger):
    """
    掃描訊號後，自動將新觸發的買進/賣出訊號寫入交易日誌，
    並更新已有開倉部位的現價。
    """
    # 建立 stock_code → CB 資訊的對照表
    cb_by_stock = {}
    for _, row in cb_df.iterrows():
        sc = str(row.get('stock_code', '')).strip()
        if sc:
            cb_by_stock.setdefault(sc, []).append(row)

    for r in results:
        sc = r['stock_code']
        cbs = cb_by_stock.get(sc, [])

        for cb in cbs:
            cb_code = str(cb.get('cb_code', '')).strip()
            if not cb_code:
                continue

            cb_price = float(cb.get('cb_price', 0) or 0)
            stock_price = r.get('latest_price', 0)

            # 更新現價（不管有無訊號）
            if cb_price > 0 and stock_price > 0:
                logger_obj.update_prices(cb_code, cb_price, stock_price)

            # 買進訊號 → 記錄進場
            if r['buy_signal'] and cb_price > 0:
                logger_obj.record_entry(
                    cb_code=cb_code,
                    cb_name=str(cb.get('cb_name', cb_code)),
                    stock_code=sc,
                    cb_price=cb_price,
                    stock_price=stock_price,
                    signal_type='buy',
                    conditions=r['buy_checks'],
                )

            # 賣出訊號 → 記錄進場（放空或賣出現有部位）
            if r['sell_signal'] and cb_price > 0:
                logger_obj.record_entry(
                    cb_code=cb_code,
                    cb_name=str(cb.get('cb_name', cb_code)),
                    stock_code=sc,
                    cb_price=cb_price,
                    stock_price=stock_price,
                    signal_type='sell',
                    conditions=r['sell_checks'],
                )


# ================================================================
# ④ 新功能：CBAS 時間節點
# ================================================================

def count_trading_days(start_date: datetime, end_date: datetime) -> int:
    """計算兩個日期間的交易日數（排除週末，未扣除國定假日）"""
    count = 0
    d = start_date
    while d < end_date:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def check_cbas_milestones(listing_date_str: str | None) -> dict | None:
    """
    檢查 CBAS 重要時間節點：

    ① 上市第 6 個交易日：可申請 CBAS 拆分（債券 + 換股權利分離）
       → 前後 CBAS_ALERT_WINDOW 天觸發 ⚡ 警示

    ② 上市第 90 個自然日：開始可以申請轉換成現股
       → 前後 5 天觸發 🔄 警示

    Args:
        listing_date_str: 上市日期字串 'YYYY-MM-DD'

    Returns:
        dict with keys: listing_date, calendar_days, trading_days,
                        can_split, can_convert, alerts, milestone_badges
    """
    if not listing_date_str:
        return None

    # 清理格式
    date_str = str(listing_date_str).strip()
    for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d']:
        try:
            listing_date = datetime.strptime(date_str[:10], fmt)
            break
        except ValueError:
            continue
    else:
        return None

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    calendar_days = (today - listing_date).days
    trading_days = count_trading_days(listing_date, today)

    alerts = []
    badges = []

    # ── ① CBAS 拆分（第 6 個交易日）──
    days_to_split = CBAS_SPLIT_DAY - trading_days
    can_split = trading_days >= CBAS_SPLIT_DAY

    if trading_days == CBAS_SPLIT_DAY:
        alerts.append({
            'type': 'cbas_split_today',
            'icon': '⚡',
            'title': 'CBAS 今日可拆分！',
            'desc': f'上市第 {trading_days} 個交易日（第 6 日），今天起可申請 CBAS 拆分',
            'severity': 'critical',
        })
        badges.append('<span class="badge-milestone split-today">⚡ 今日可拆!</span>')

    elif 0 < days_to_split <= CBAS_ALERT_WINDOW:
        alerts.append({
            'type': 'cbas_split_soon',
            'icon': '⚡',
            'title': f'CBAS 拆分即將到來（{days_to_split} 交易日後）',
            'desc': f'目前第 {trading_days} 交易日，再 {days_to_split} 個交易日即可申請拆分',
            'severity': 'high',
        })
        badges.append(f'<span class="badge-milestone split-soon">⚡ 拆分 -{days_to_split}日</span>')

    elif trading_days > CBAS_SPLIT_DAY and trading_days <= CBAS_SPLIT_DAY + CBAS_ALERT_WINDOW:
        alerts.append({
            'type': 'cbas_split_passed',
            'icon': '✅',
            'title': f'CBAS 拆分已開放（第 {trading_days} 交易日）',
            'desc': '已超過第 6 個交易日，拆分視窗已開放',
            'severity': 'info',
        })

    # ── ② 可轉換期（第 90 個自然日）──
    days_to_conv = CONVERSION_DAYS - calendar_days
    can_convert = calendar_days >= CONVERSION_DAYS

    if calendar_days == CONVERSION_DAYS:
        alerts.append({
            'type': 'conversion_today',
            'icon': '🔄',
            'title': '今日起可申請轉換現股！',
            'desc': f'上市滿 {CONVERSION_DAYS} 天，今天起可申請轉換成標的股票',
            'severity': 'critical',
        })
        badges.append('<span class="badge-milestone conv-today">🔄 今日可轉換!</span>')

    elif 0 < days_to_conv <= 5:
        alerts.append({
            'type': 'conversion_soon',
            'icon': '🔄',
            'title': f'即將進入可轉換期（{days_to_conv} 天後）',
            'desc': f'上市第 {calendar_days} 天，{days_to_conv} 天後即可申請轉換',
            'severity': 'high',
        })
        badges.append(f'<span class="badge-milestone conv-soon">🔄 轉換 -{days_to_conv}日</span>')

    elif calendar_days > CONVERSION_DAYS and calendar_days <= CONVERSION_DAYS + 5:
        alerts.append({
            'type': 'conversion_open',
            'icon': '🔄',
            'title': '可轉換期已開放',
            'desc': f'上市第 {calendar_days} 天，現在可申請轉換',
            'severity': 'info',
        })

    return {
        'listing_date': listing_date_str,
        'calendar_days': calendar_days,
        'trading_days': trading_days,
        'days_to_split': days_to_split,
        'days_to_convert': days_to_conv,
        'can_split': can_split,
        'can_convert': can_convert,
        'alerts': alerts,
        'milestone_badges': badges,
    }


# ================================================================
# 3. 訊號判斷邏輯
# ================================================================

def check_short_decreasing_consecutive(margin_df: pd.DataFrame, days: int = 2) -> bool:
    """融券餘額連續 days 日下降"""
    if len(margin_df) < days + 1:
        return False
    balances = margin_df.tail(days + 1)['short_balance'].values
    for i in range(1, len(balances)):
        if balances[i] >= balances[i - 1]:
            return False
    return True


def check_short_drop_by_amount(margin_df: pd.DataFrame, min_lots: int = 200) -> bool:
    """融券餘額單日減少量 >= min_lots 張"""
    if len(margin_df) < 2:
        return False
    drop = margin_df.iloc[-2]['short_balance'] - margin_df.iloc[-1]['short_balance']
    return drop >= min_lots


def check_consecutive_red_candles(ohlcv: pd.DataFrame, days: int = 2) -> bool:
    """連續 days 日收盤 > 開盤 (紅K)"""
    if len(ohlcv) < days:
        return False
    recent = ohlcv.tail(days)
    return all(recent['Close'] > recent['Open'])


def check_volume_above_ma(ohlcv: pd.DataFrame, ma_days: int = 10) -> bool:
    """最新成交量 > 前 ma_days 日均量"""
    if len(ohlcv) < ma_days + 1:
        return False
    latest_vol = ohlcv.iloc[-1]['Volume']
    avg_vol = ohlcv.iloc[-(ma_days + 1):-1]['Volume'].mean()
    return latest_vol > avg_vol


def check_price_above_ma(ohlcv: pd.DataFrame, ma_days: int = 10) -> bool:
    """最新收盤價 > 前 ma_days 日均價"""
    if len(ohlcv) < ma_days + 1:
        return False
    latest_price = ohlcv.iloc[-1]['Close']
    avg_price = ohlcv.iloc[-(ma_days + 1):-1]['Close'].mean()
    return latest_price > avg_price


def evaluate_signals(
    stock_code: str,
    ohlcv: pd.DataFrame,
    margin_df: pd.DataFrame,
    stock_info: dict,
) -> dict:
    """評估買進與賣出訊號，回傳詳細結果"""
    capital_亿 = stock_info.get('capital_亿')

    # ── 買進訊號 ──
    b1 = check_short_decreasing_consecutive(margin_df, SHORT_DECREASE_CONSECUTIVE)
    b2 = check_consecutive_red_candles(ohlcv, RED_CANDLE_CONSECUTIVE)
    b3 = (capital_亿 < CAPITAL_THRESHOLD_亿) if capital_亿 is not None else None
    b4 = check_volume_above_ma(ohlcv, VOLUME_MA_DAYS)

    buy_checks = {'融券連降': b1, '連續紅K': b2, '股本<30億': b3, '量>10日均': b4}
    buy_signal = (
        all(v for v in buy_checks.values() if v is not None) and
        sum(1 for v in buy_checks.values() if v is not None) >= 3
    )

    # ── 賣出訊號 ──
    s1 = check_short_drop_by_amount(margin_df, MIN_SHORT_DECREASE_LOTS)
    s2 = check_price_above_ma(ohlcv, PRICE_MA_DAYS)
    s3 = (capital_亿 < CAPITAL_THRESHOLD_亿) if capital_亿 is not None else None

    sell_checks = {'融券減≥200張': s1, '股價>10日均': s2, '股本<30億': s3}
    sell_signal = (
        all(v for v in sell_checks.values() if v is not None) and
        sum(1 for v in sell_checks.values() if v is not None) >= 2
    )

    # ── 輔助數據 ──
    latest_price = float(ohlcv.iloc[-1]['Close']) if len(ohlcv) > 0 else 0
    latest_vol = int(ohlcv.iloc[-1]['Volume']) if len(ohlcv) > 0 else 0
    ma10_price = (
        float(ohlcv.iloc[-(PRICE_MA_DAYS + 1):-1]['Close'].mean())
        if len(ohlcv) > PRICE_MA_DAYS else 0
    )
    ma10_vol = (
        float(ohlcv.iloc[-(VOLUME_MA_DAYS + 1):-1]['Volume'].mean())
        if len(ohlcv) > VOLUME_MA_DAYS else 0
    )

    short_balance = int(margin_df.iloc[-1]['short_balance']) if len(margin_df) > 0 else None
    short_prev = int(margin_df.iloc[-2]['short_balance']) if len(margin_df) > 1 else None
    short_change = (short_balance - short_prev) if (short_balance is not None and short_prev is not None) else None

    return {
        'stock_code': stock_code,
        'buy_signal': buy_signal,
        'sell_signal': sell_signal,
        'buy_checks': buy_checks,
        'sell_checks': sell_checks,
        'latest_price': latest_price,
        'latest_vol': latest_vol,
        'ma10_price': round(ma10_price, 2),
        'ma10_vol': int(ma10_vol),
        'capital_亿': capital_亿,
        'market_cap': stock_info.get('market_cap', 0),
        'short_balance': short_balance,
        'short_prev': short_prev,
        'short_change': short_change,
        'has_margin_data': len(margin_df) > 0,
    }


# ================================================================
# 4. 主掃描流程
# ================================================================

def scan_stocks(cb_df: pd.DataFrame, history_db: CBHistoryDB, trade_log: TradeLogger) -> list[dict]:
    """掃描所有可轉債的標的股，套用策略邏輯，附加新四項功能"""
    results = []

    if 'stock_code' not in cb_df.columns:
        logger.error("cb_df 缺少 stock_code 欄位")
        return results

    stock_codes = [
        c for c in cb_df['stock_code'].dropna().astype(str).unique()
        if c.strip() and c.strip().isdigit()
    ]
    logger.info(f"\n開始掃描 {len(stock_codes)} 支標的股...\n{'─' * 45}")

    for i, stock_code in enumerate(stock_codes, 1):
        logger.info(f"[{i:02d}/{len(stock_codes)}] 處理 {stock_code}...")

        try:
            # 基本股票資料
            ohlcv = get_stock_ohlcv(stock_code)
            if ohlcv.empty:
                logger.warning(f"  {stock_code}: 無 OHLCV，略過")
                continue

            margin_df = get_margin_short_data(stock_code)
            sinfo = get_stock_info(stock_code)

            # 評估買/賣訊號
            signal = evaluate_signals(stock_code, ohlcv, margin_df, sinfo)

            # 附上 CB 基本資料
            stock_cbs = cb_df[cb_df['stock_code'].astype(str).str.strip() == stock_code]
            signal['cbs'] = stock_cbs.to_dict('records')

            if 'stock_name' in stock_cbs.columns and len(stock_cbs) > 0:
                signal['stock_name'] = str(stock_cbs.iloc[0]['stock_name'])
            else:
                signal['stock_name'] = stock_code

            # ── ① CB 成交量分析（每支 CB 各自計算）──
            cb_volume_analyses = {}
            for _, cb_row in stock_cbs.iterrows():
                cb_code = str(cb_row.get('cb_code', '')).strip()
                cb_price = float(cb_row.get('cb_price', 100) or 100)

                # 優先使用 DataFrame 中已有的成交量，否則嘗試即時抓取
                vol = cb_row.get('cb_volume')
                if vol is None or (isinstance(vol, float) and pd.isna(vol)):
                    vol = fetch_cb_daily_volume(cb_code)
                    time.sleep(0.2)

                vol_int = int(vol) if vol is not None else None
                cb_volume_analyses[cb_code] = analyze_cb_volume(
                    vol_int, cb_price, sinfo.get('market_cap', 0)
                )

            signal['cb_volume_analyses'] = cb_volume_analyses

            # ── ② 公司歷史 CB 記錄 ──
            signal['company_cb_history'] = history_db.get_company_history(stock_code)
            signal['company_cb_stats'] = history_db.get_company_stats(stock_code)

            # ── ④ CBAS 時間節點（每支 CB）──
            cb_milestones = {}
            has_urgent_milestone = False
            for _, cb_row in stock_cbs.iterrows():
                cb_code = str(cb_row.get('cb_code', '')).strip()
                listing_date = cb_row.get('listing_date')
                ms = check_cbas_milestones(listing_date)
                cb_milestones[cb_code] = ms
                if ms and any(a['severity'] in ('critical', 'high') for a in ms.get('alerts', [])):
                    has_urgent_milestone = True

            signal['cb_milestones'] = cb_milestones
            signal['has_urgent_milestone'] = has_urgent_milestone

            results.append(signal)

            status = '🟢 買進' if signal['buy_signal'] else ('🔴 賣出' if signal['sell_signal'] else '─')
            milestone_flag = ' ⚡CBAS!' if has_urgent_milestone else ''
            logger.info(
                f"  {status}{milestone_flag} | 股價 {signal['latest_price']} "
                f"| 融券 {signal['short_balance']}"
            )

        except Exception as e:
            logger.error(f"  {stock_code} 處理失敗: {e}", exc_info=True)

        time.sleep(0.8)

    # ── ③ 自動交易留檔 ──
    auto_record_signals(results, cb_df, trade_log)

    return results


# ================================================================
# 5. HTML 儀表板產生
# ================================================================

def _cls(v) -> str:
    return 'ok' if v else 'fail' if v is False else 'na'


def render_signal_row(r: dict, mode: str) -> str:
    checks = r['buy_checks'] if mode == 'buy' else r['sell_checks']
    badges = ' '.join(
        '<span class="check-item ' + _cls(v) + '">' + k + '</span>'
        for k, v in checks.items()
    )
    cbs_str = ', '.join(
        str(c.get('cb_code', '')) + ' (' + str(c.get('cb_name', '')) + ')'
        for c in r.get('cbs', [])[:3]
    )
    price_vs_ma = ''
    if r['ma10_price'] > 0:
        diff_pct = (r['latest_price'] - r['ma10_price']) / r['ma10_price'] * 100
        color = 'green' if diff_pct >= 0 else 'red'
        price_vs_ma = f' <span style="color:{color}">({diff_pct:+.1f}%)</span>'

    if r['short_change'] is not None:
        sign = '+' if r['short_change'] >= 0 else ''
        short_change_str = f'({sign}{r["short_change"]})'
    else:
        short_change_str = ''

    capital_str = f'{r["capital_亿"]:.1f} 億' if r['capital_亿'] else '─'
    row_class = 'buy-row' if mode == 'buy' else 'sell-row'

    # Milestone badges
    ms_badges = ''
    for ms in r.get('cb_milestones', {}).values():
        if ms:
            ms_badges += ' '.join(ms.get('milestone_badges', []))

    return f"""
    <tr class="signal-row {row_class}">
      <td><strong>{r['stock_code']}</strong></td>
      <td>{r['stock_name']} {ms_badges}</td>
      <td class="price">{r['latest_price']:.2f}{price_vs_ma}</td>
      <td>{r['short_balance']:,} <span class="small">{short_change_str}</span></td>
      <td>{capital_str}</td>
      <td class="badges">{badges}</td>
      <td class="cbs-cell">{cbs_str or '─'}</td>
    </tr>"""


def render_cbas_alerts(results: list[dict]) -> str:
    """產生 CBAS 時間節點警示區塊的 HTML"""
    rows = []
    for r in results:
        for cb_code, ms in r.get('cb_milestones', {}).items():
            if not ms or not ms.get('alerts'):
                continue
            for alert in ms['alerts']:
                severity_cls = {
                    'critical': 'alert-critical',
                    'high': 'alert-high',
                    'info': 'alert-info',
                }.get(alert['severity'], 'alert-info')

                # 找 CB 名稱
                cb_name = ''
                for cb in r.get('cbs', []):
                    if str(cb.get('cb_code', '')) == cb_code:
                        cb_name = cb.get('cb_name', '')
                        break

                rows.append(f"""
                <div class="cbas-alert {severity_cls}">
                  <div class="alert-icon">{alert['icon']}</div>
                  <div class="alert-body">
                    <div class="alert-title">
                      {cb_code} {cb_name} — {alert['title']}
                    </div>
                    <div class="alert-desc">
                      {alert['desc']} ｜
                      上市日：{ms['listing_date']} ｜
                      已過 {ms['calendar_days']} 天 / {ms['trading_days']} 交易日
                    </div>
                  </div>
                </div>""")

    if not rows:
        return '<div class="empty-msg">目前無 CBAS 重要時間節點警示</div>'
    return '\n'.join(rows)


def render_all_cb_rows(cb_df: pd.DataFrame, results: list[dict]) -> str:
    """渲染所有可轉債列表（含成交量分析、歷史記錄、CBAS 里程碑）"""
    signal_map = {r['stock_code']: r for r in results}
    rows = []

    for _, row in cb_df.iterrows():
        stock_code = str(row.get('stock_code', '')).strip()
        cb_code = str(row.get('cb_code', '')).strip()
        sig = signal_map.get(stock_code, {})

        buy = sig.get('buy_signal', False)
        sell = sig.get('sell_signal', False)
        signal_cell = (
            '<span class="tag-buy">買進</span>' if buy else
            '<span class="tag-sell">賣出</span>' if sell else '─'
        )

        price = sig.get('latest_price', 0)
        price_str = f"{price:.2f}" if price else '─'

        # ① 成交量分析
        vol_info = sig.get('cb_volume_analyses', {}).get(cb_code)
        if vol_info:
            vol_label = vol_info['liquidity_label']
            turnover_str = ''
            if vol_info['cb_turnover_ntd']:
                turnover_str = f"<br/><span class='small'>{vol_info['cb_turnover_ntd']/1e6:.1f} 百萬</span>"
            vol_cell = vol_label + turnover_str
        else:
            vol_cell = '─'

        # ② 歷史 CB 統計
        stats = sig.get('company_cb_stats', {})
        if stats and stats.get('closed', 0) > 0:
            wr = stats.get('win_rate', 0)
            avg_pnl = stats.get('avg_pnl_pct', 0)
            pnl_color = 'green' if avg_pnl >= 0 else 'red'
            hist_cell = (
                f"勝率 {wr}%<br/>"
                f"<span class='small' style='color:{pnl_color}'>均損益 {avg_pnl:+.1f}%</span>"
            )
        else:
            hist_cell = f"無記錄 ({stats.get('total', 0)} 檔)"

        # ④ CBAS 里程碑標籤
        ms = sig.get('cb_milestones', {}).get(cb_code)
        ms_badges = ' '.join(ms.get('milestone_badges', [])) if ms else ''
        days_info = ''
        if ms:
            days_info = f"<br/><span class='small'>上市 {ms['calendar_days']}天 / {ms['trading_days']}交易日</span>"

        rows.append(f"""
        <tr>
          <td>{row.get('cb_code', '─')}</td>
          <td>{row.get('cb_name', '─')} {ms_badges}</td>
          <td><strong>{stock_code}</strong></td>
          <td>{row.get('stock_name', '─')}</td>
          <td class="price">{price_str}</td>
          <td>{row.get('cb_price', '─')}</td>
          <td>{row.get('conversion_price', '─')}</td>
          <td>{row.get('premium_rate', '─')}</td>
          <td>{row.get('maturity_date', '─')}{days_info}</td>
          <td>{vol_cell}</td>
          <td>{hist_cell}</td>
          <td>{signal_cell}</td>
        </tr>""")

    return '\n'.join(rows)


def render_trade_log(trade_log: TradeLogger) -> str:
    """渲染交易記錄表格"""
    if not trade_log.trades:
        return '<tr><td colspan="10" class="empty-msg">尚無交易記錄</td></tr>'

    # 最新在前
    sorted_trades = sorted(
        trade_log.trades,
        key=lambda t: t.get('entry_date', ''),
        reverse=True
    )

    rows = []
    for t in sorted_trades[:50]:  # 最多顯示 50 筆
        status_cls = 'trade-open' if t['status'] == 'open' else (
            'trade-profit' if t.get('result') == 'profit' else 'trade-loss'
        )
        status_label = (
            '📂 持倉中' if t['status'] == 'open' else
            ('📈 獲利' if t.get('result') == 'profit' else '📉 虧損')
        )

        # 損益顯示
        if t['status'] == 'open':
            unrealized = t.get('unrealized_cb_pnl_pct')
            pnl_str = f"浮動 {unrealized:+.2f}%" if unrealized is not None else '持倉中'
            pnl_color = 'green' if (unrealized or 0) >= 0 else 'red'
        else:
            pnl = t.get('cb_pnl_pct', 0)
            pnl_str = f"{pnl:+.2f}%"
            pnl_color = 'green' if pnl >= 0 else 'red'

        sig_label = '🟢 買進' if t['signal_type'] == 'buy' else '🔴 賣出'

        rows.append(f"""
        <tr class="{status_cls}">
          <td>{t.get('entry_date', '─')}</td>
          <td><strong>{t['cb_code']}</strong></td>
          <td>{t.get('cb_name', '─')}</td>
          <td>{t.get('stock_code', '─')}</td>
          <td>{sig_label}</td>
          <td class="price">{t.get('entry_cb_price', '─')}</td>
          <td class="price">{t.get('exit_cb_price', '─') or '─'}</td>
          <td style="color:{pnl_color}; font-weight:600">{pnl_str}</td>
          <td>{t.get('exit_date', '─') or '─'}</td>
          <td>{status_label}</td>
        </tr>""")

    return '\n'.join(rows)


def render_company_history_section(results: list[dict]) -> str:
    """渲染各公司歷史 CB 記錄詳細展開區塊"""
    rows = []
    for r in results:
        hist = r.get('company_cb_history', [])
        if not hist:
            continue
        stats = r.get('company_cb_stats', {})
        wr = stats.get('win_rate')
        wr_str = f"{wr}%" if wr is not None else 'N/A'
        avg_pnl = stats.get('avg_pnl_pct')
        avg_pnl_str = f"{avg_pnl:+.2f}%" if avg_pnl is not None else 'N/A'
        pnl_color = 'green' if (avg_pnl or 0) >= 0 else 'red'

        rows.append(f"""
        <div class="history-card">
          <div class="history-header">
            <strong>{r['stock_code']} {r['stock_name']}</strong>
            <span class="history-stats">
              歷史 {stats.get('total', 0)} 檔 ｜ 已結案 {stats.get('closed', 0)} ｜
              勝率 {wr_str} ｜
              平均損益 <span style="color:{pnl_color}">{avg_pnl_str}</span>
            </span>
          </div>
          <table class="history-table">
            <thead>
              <tr>
                <th>代碼</th><th>名稱</th><th>發行日</th>
                <th>到期日</th><th>轉換價</th><th>出場價</th>
                <th>損益</th><th>結果</th><th>備註</th>
              </tr>
            </thead>
            <tbody>""")

        for h in hist:
            pnl = h.get('pnl_pct')
            pnl_s = f"{pnl:+.2f}%" if pnl is not None else '─'
            pnl_c = 'green' if (pnl or 0) > 0 else ('red' if (pnl or 0) < 0 else '#666')
            res = h.get('result', '')
            res_icon = '📈' if res == 'profit' else ('📉' if res == 'loss' else '⏳')
            rows.append(f"""
              <tr>
                <td>{h.get('cb_code', '─')}</td>
                <td>{h.get('cb_name', '─')}</td>
                <td>{h.get('issue_date', '─')}</td>
                <td>{h.get('maturity_date', '─')}</td>
                <td>{h.get('conversion_price', '─')}</td>
                <td>{h.get('exit_price', '─') or '─'}</td>
                <td style="color:{pnl_c}; font-weight:600">{pnl_s}</td>
                <td>{res_icon} {res}</td>
                <td>{h.get('exit_reason', h.get('notes', ''))}</td>
              </tr>""")

        rows.append("""
            </tbody>
          </table>
        </div>""")

    if not rows:
        return '<div class="empty-msg">尚無歷史 CB 記錄（首次執行示範資料將自動建立）</div>'
    return '\n'.join(rows)


def generate_html(
    cb_df: pd.DataFrame,
    results: list[dict],
    trade_log: TradeLogger,
) -> str:
    """產生完整 HTML 儀表板"""
    buy_results = [r for r in results if r['buy_signal']]
    sell_results = [r for r in results if r['sell_signal']]
    urgent_results = [r for r in results if r.get('has_urgent_milestone')]
    update_time = datetime.now().strftime('%Y-%m-%d %H:%M')
    trade_summary = trade_log.get_summary()

    buy_rows_html = (
        '\n'.join(render_signal_row(r, 'buy') for r in buy_results) if buy_results
        else '<tr><td colspan="7" class="empty-msg">目前無符合買進訊號的標的</td></tr>'
    )
    sell_rows_html = (
        '\n'.join(render_signal_row(r, 'sell') for r in sell_results) if sell_results
        else '<tr><td colspan="7" class="empty-msg">目前無符合賣出訊號的標的</td></tr>'
    )
    cbas_alerts_html = render_cbas_alerts(results)
    all_cb_rows_html = render_all_cb_rows(cb_df, results)
    trade_rows_html = render_trade_log(trade_log)
    history_html = render_company_history_section(results)

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>可轉債策略儀表板 v2</title>
  <style>
    :root {{
      --green: #16a34a; --green-light: #dcfce7;
      --red: #dc2626; --red-light: #fee2e2;
      --blue: #2563eb; --blue-light: #dbeafe;
      --amber: #d97706; --amber-light: #fef3c7;
      --purple: #7c3aed; --purple-light: #ede9fe;
      --gray: #6b7280; --border: #e5e7eb;
      --bg: #f1f5f9; --card-bg: #ffffff;
      --radius: 10px; --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: var(--font); background: var(--bg); color: #111827; font-size: 14px; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 20px 16px; }}

    /* Stat Cards */
    .stats {{ display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; }}
    .stat-card {{ background: var(--card-bg); border: 1px solid var(--border);
                  border-radius: var(--radius); padding: 12px 18px;
                  flex: 1; min-width: 140px; text-align: center; }}
    .stat-card .num {{ font-size: 28px; font-weight: 700; }}
    .stat-card .label {{ color: var(--gray); font-size: 11px; margin-top: 2px; }}
    .stat-card.green .num {{ color: var(--green); }}
    .stat-card.red .num {{ color: var(--red); }}
    .stat-card.blue .num {{ color: var(--blue); }}
    .stat-card.amber .num {{ color: var(--amber); }}
    .stat-card.purple .num {{ color: var(--purple); }}

    /* Section */
    .section {{ background: var(--card-bg); border: 1px solid var(--border);
                border-radius: var(--radius); margin-bottom: 20px; overflow: hidden; }}
    .section-header {{ padding: 12px 16px; border-bottom: 1px solid var(--border);
                       display: flex; align-items: center; gap: 8px; }}
    .section-header h2 {{ font-size: 14px; font-weight: 600; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; }}
    .dot-green {{ background: var(--green); }}
    .dot-red {{ background: var(--red); }}
    .dot-blue {{ background: var(--blue); }}
    .dot-amber {{ background: var(--amber); }}
    .dot-purple {{ background: var(--purple); }}

    /* Tables */
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 9px 11px; text-align: left; border-bottom: 1px solid var(--border); }}
    th {{ background: #f8fafc; font-size: 11px; font-weight: 600;
          color: var(--gray); text-transform: uppercase; white-space: nowrap; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #f9fafb; }}

    .buy-row {{ background: #f0fdf4; }}
    .buy-row:hover td {{ background: #dcfce7; }}
    .sell-row {{ background: #fff1f2; }}
    .sell-row:hover td {{ background: #ffe4e6; }}
    .trade-open {{ background: #fefce8; }}
    .trade-profit {{ background: #f0fdf4; }}
    .trade-loss {{ background: #fff1f2; }}

    /* Badges */
    .check-item {{ display: inline-block; padding: 2px 6px; border-radius: 4px;
                   font-size: 11px; font-weight: 500; margin: 1px; }}
    .check-item.ok {{ background: var(--green-light); color: var(--green); }}
    .check-item.fail {{ background: var(--red-light); color: var(--red); }}
    .check-item.na {{ background: #f3f4f6; color: var(--gray); }}

    .tag-buy {{ background: var(--green); color: #fff;
                padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
    .tag-sell {{ background: var(--red); color: #fff;
                 padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}

    /* Milestone badges */
    .badge-milestone {{ display: inline-block; padding: 2px 7px; border-radius: 10px;
                        font-size: 11px; font-weight: 700; margin-left: 4px; }}
    .split-today {{ background: #fbbf24; color: #78350f; animation: pulse 1.5s infinite; }}
    .split-soon  {{ background: #fde68a; color: #92400e; }}
    .conv-today  {{ background: #818cf8; color: #fff; animation: pulse 1.5s infinite; }}
    .conv-soon   {{ background: #c7d2fe; color: #3730a3; }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.6; }} }}

    /* CBAS Alert Cards */
    .cbas-alerts {{ padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }}
    .cbas-alert {{ display: flex; align-items: flex-start; gap: 10px;
                   padding: 10px 14px; border-radius: 8px; border: 1px solid; }}
    .alert-critical {{ background: #fff7ed; border-color: #fb923c; }}
    .alert-high     {{ background: #fffbeb; border-color: #fbbf24; }}
    .alert-info     {{ background: #f0f9ff; border-color: #7dd3fc; }}
    .alert-icon {{ font-size: 20px; flex-shrink: 0; }}
    .alert-title {{ font-weight: 600; font-size: 13px; }}
    .alert-desc {{ color: var(--gray); font-size: 12px; margin-top: 3px; }}

    /* History Cards */
    .history-card {{ border: 1px solid var(--border); border-radius: 8px;
                     margin: 12px 16px; overflow: hidden; }}
    .history-header {{ background: #f8fafc; padding: 10px 14px;
                       display: flex; align-items: center; justify-content: space-between;
                       flex-wrap: wrap; gap: 4px; }}
    .history-stats {{ color: var(--gray); font-size: 12px; }}
    .history-table {{ width: 100%; border-collapse: collapse; }}
    .history-table th, .history-table td {{ padding: 7px 10px; font-size: 12px;
                                            border-bottom: 1px solid var(--border); }}
    .history-table th {{ background: #f8fafc; color: var(--gray); font-weight: 600; }}

    .price {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
    .small {{ color: var(--gray); font-size: 11px; }}
    .cbs-cell {{ font-size: 12px; color: #374151; }}
    .empty-msg {{ color: var(--gray); font-style: italic; text-align: center;
                  padding: 20px; }}

    /* Strategy legend */
    .legend {{ background: #fffbeb; border: 1px solid #fde68a;
               border-radius: var(--radius); padding: 12px 16px;
               margin-bottom: 20px; font-size: 13px; }}
    .legend h3 {{ font-size: 13px; font-weight: 600; margin-bottom: 8px; color: #92400e; }}
    .legend-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    @media (max-width: 600px) {{ .legend-grid {{ grid-template-columns: 1fr; }} }}
    .legend ol {{ padding-left: 16px; color: #451a03; }}
    .legend ol li {{ margin: 3px 0; }}
  </style>
</head>
<body>
<div class="container">

  <!-- 策略說明 -->
  <div class="legend">
    <h3>⚙ 可轉債交易邏輯</h3>
    <div class="legend-grid">
      <div>
        <strong>🟢 現股買進</strong>
        <ol>
          <li>融券餘額_t &lt; 融券餘額_(t-1) 連續兩日</li>
          <li>收盤 &gt; 開盤（紅K）連續兩日以上</li>
          <li>股本小於 30 億</li>
          <li>交易量 &gt; 10 日均量</li>
        </ol>
      </div>
      <div>
        <strong>🔴 現股賣出</strong>
        <ol>
          <li>融券餘額至少減少 200 張</li>
          <li>股價 &gt; 10 日均線</li>
          <li>股本小於 30 億</li>
        </ol>
      </div>
    </div>
  </div>

  <!-- Stat Cards -->
  <div class="stats">
    <div class="stat-card blue">
      <div class="num">{len(cb_df)}</div>
      <div class="label">可轉債總數</div>
    </div>
    <div class="stat-card green">
      <div class="num">{len(buy_results)}</div>
      <div class="label">買進訊號</div>
    </div>
    <div class="stat-card red">
      <div class="num">{len(sell_results)}</div>
      <div class="label">賣出訊號</div>
    </div>
    <div class="stat-card amber">
      <div class="num">{len(urgent_results)}</div>
      <div class="label">⚡ CBAS 時間節點</div>
    </div>
    <div class="stat-card purple">
      <div class="num">{trade_summary['open']}</div>
      <div class="label">持倉中</div>
    </div>
    <div class="stat-card green">
      <div class="num">{trade_summary['win_rate'] or 'N/A'}{'%' if trade_summary['win_rate'] else ''}</div>
      <div class="label">歷史勝率</div>
    </div>
  </div>

  <!-- ④ CBAS 時間節點 -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-amber"></div>
      <h2>⚡ CBAS 重要時間節點</h2>
      <span style="color:#6b7280;font-size:12px;margin-left:8px;">
        上市第 6 交易日（可拆分）｜ 上市第 90 天（可轉換）
      </span>
    </div>
    <div class="cbas-alerts">{cbas_alerts_html}</div>
  </div>

  <!-- 買進訊號 -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-green"></div>
      <h2>🟢 買進訊號 ({len(buy_results)} 筆)</h2>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>股票代號</th><th>股票名稱</th><th>最新股價</th>
            <th>融券餘額(張)</th><th>股本</th><th>訊號條件</th><th>相關可轉債</th>
          </tr>
        </thead>
        <tbody>{buy_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- 賣出訊號 -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-red"></div>
      <h2>🔴 賣出訊號 ({len(sell_results)} 筆)</h2>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>股票代號</th><th>股票名稱</th><th>最新股價</th>
            <th>融券餘額(張)</th><th>股本</th><th>訊號條件</th><th>相關可轉債</th>
          </tr>
        </thead>
        <tbody>{sell_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- ③ 交易記錄 -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-purple"></div>
      <h2>📒 交易記錄
        <span style="font-size:12px;font-weight:400;color:#6b7280;margin-left:8px;">
          持倉 {trade_summary['open']} ｜ 已結 {trade_summary['closed']} ｜
          勝率 {trade_summary['win_rate'] or 'N/A'}{'%' if trade_summary['win_rate'] else ''} ｜
          均損益 {trade_summary['avg_pnl_pct']:+.2f}%
        </span>
      </h2>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>進場日</th><th>CB 代碼</th><th>CB 名稱</th><th>股票</th>
            <th>訊號</th><th>進場CB價</th><th>出場CB價</th>
            <th>損益(%)</th><th>出場日</th><th>狀態</th>
          </tr>
        </thead>
        <tbody>{trade_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- 所有可轉債 + ①②④ -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-blue"></div>
      <h2>📋 可轉債完整清單（含流動性 & 時間節點）</h2>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>CB 代碼</th><th>CB 名稱</th><th>股票代號</th><th>股票名稱</th>
            <th>現股股價</th><th>CB 現價</th><th>轉換價</th><th>溢價率</th>
            <th>到期日 / 上市天數</th>
            <th>① CB 成交量</th>
            <th>② 歷史勝率</th>
            <th>訊號</th>
          </tr>
        </thead>
        <tbody>{all_cb_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- ② 公司歷史 CB 記錄詳細 -->
  <div class="section">
    <div class="section-header">
      <div class="dot dot-blue"></div>
      <h2>📚 公司歷史 CB 案例（含過去損益）</h2>
    </div>
    <div style="padding: 4px 0;">{history_html}</div>
  </div>


</div>
</body>
</html>"""


# ================================================================
# JSON 匯出（供 WordPress PHP 短代碼使用）
# ================================================================

JSON_OUTPUT_FILE = "cb_data.json"


def export_json(cb_df: pd.DataFrame, results: list[dict], trade_log: TradeLogger) -> None:
    """將掃描結果匯出為 cb_data.json，供 WordPress PHP 短代碼抓取並渲染。"""

    def _safe(v):
        """將 pandas/Python 非 JSON 型別轉為基本型別。"""
        if v is None:
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        if isinstance(v, (pd.Timestamp, datetime)):
            return str(v)
        if isinstance(v, (pd.Series, pd.DataFrame)):
            return None
        return v

    def _clean_cb_row(row: dict) -> dict:
        return {k: _safe(v) for k, v in row.items()}

    def _clean_result(r: dict) -> dict:
        return {
            'stock_code':       r.get('stock_code'),
            'stock_name':       r.get('stock_name'),
            'latest_price':     _safe(r.get('latest_price')),
            'ma10_price':       _safe(r.get('ma10_price')),
            'latest_vol':       _safe(r.get('latest_vol')),
            'ma10_vol':         _safe(r.get('ma10_vol')),
            'capital_亿':        _safe(r.get('capital_亿')),
            'market_cap':       _safe(r.get('market_cap')),
            'short_balance':    _safe(r.get('short_balance')),
            'short_prev':       _safe(r.get('short_prev')),
            'short_change':     _safe(r.get('short_change')),
            'buy_signal':       bool(r.get('buy_signal', False)),
            'sell_signal':      bool(r.get('sell_signal', False)),
            'buy_checks':       {k: _safe(v) for k, v in r.get('buy_checks', {}).items()},
            'sell_checks':      {k: _safe(v) for k, v in r.get('sell_checks', {}).items()},
            'cbs':              [_clean_cb_row(c) for c in r.get('cbs', [])],
            'cb_volume_analyses': {
                cb_code: {
                    'cb_volume_lots':      a.get('cb_volume_lots'),
                    'cb_turnover_ntd':     a.get('cb_turnover_ntd'),
                    'liquidity_grade':     a.get('liquidity_grade'),
                    'liquidity_label':     a.get('liquidity_label'),
                    'is_adequate':         a.get('is_adequate'),
                }
                for cb_code, a in r.get('cb_volume_analyses', {}).items()
            },
            'company_cb_stats': r.get('company_cb_stats', {}),
            'has_urgent_milestone': bool(r.get('has_urgent_milestone', False)),
        }

    trade_summary = trade_log.get_summary()
    all_cleaned = [_clean_result(r) for r in results]

    data = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'stats': {
            'total_cbs':      len(cb_df),
            'buy_signals':    sum(1 for r in results if r['buy_signal']),
            'sell_signals':   sum(1 for r in results if r['sell_signal']),
            'cbas_alerts':    sum(1 for r in results if r.get('has_urgent_milestone')),
            'open_positions': trade_summary['open'],
            'win_rate':       trade_summary['win_rate'],
            'avg_pnl_pct':    trade_summary['avg_pnl_pct'],
        },
        'buy_signals':  [r for r in all_cleaned if r['buy_signal']],
        'sell_signals': [r for r in all_cleaned if r['sell_signal']],
        'cbas_alerts':  [r for r in all_cleaned if r['has_urgent_milestone']],
        'all_cbs':      all_cleaned,
        'trades':       trade_log.trades,
    }

    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, JSON_OUTPUT_FILE)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"✅ JSON 資料已匯出: {json_path}")


# ================================================================
# 主程式
# ================================================================

def main():
    logger.info("=" * 50)
    logger.info("  可轉債策略儀表板 v2 啟動")
    logger.info("=" * 50)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 初始化歷史記錄庫
    history_db = CBHistoryDB(os.path.join(script_dir, HISTORY_FILE))
    init_sample_history(history_db)

    # 初始化交易日誌
    trade_log = TradeLogger(os.path.join(script_dir, TRADE_LOG_FILE))

    # 抓取可轉債清單
    cb_df = get_cb_list()
    logger.info(f"\n取得 {len(cb_df)} 筆可轉債資料")

    # 掃描股票（含 4 個新功能）
    results = scan_stocks(cb_df, history_db, trade_log)

    # 產生 HTML
    html = generate_html(cb_df, results, trade_log)

    output_path = os.path.join(script_dir, OUTPUT_FILE)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    # 匯出 JSON（供 WordPress PHP 短代碼使用）
    export_json(cb_df, results, trade_log)

    # 統計
    buy_count = sum(1 for r in results if r['buy_signal'])
    sell_count = sum(1 for r in results if r['sell_signal'])
    cbas_count = sum(1 for r in results if r.get('has_urgent_milestone'))
    ts = trade_log.get_summary()

    logger.info(f"\n{'=' * 50}")
    logger.info(f"✅ 儀表板已產生: {output_path}")
    logger.info(f"   🟢 買進訊號 : {buy_count} 筆")
    logger.info(f"   🔴 賣出訊號 : {sell_count} 筆")
    logger.info(f"   ⚡ CBAS節點 : {cbas_count} 筆")
    logger.info(f"   📒 持倉中   : {ts['open']} 筆 | 歷史勝率 {ts['win_rate'] or 'N/A'}")
    logger.info(f"{'=' * 50}")


if __name__ == '__main__':
    main()
