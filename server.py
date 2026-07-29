"""
全球市场恐贪指数 - 数据聚合后端
FastAPI server that aggregates Fear & Greed indices from multiple sources.
Uses pure HTTP APIs (no akshare) for maximum compatibility.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI(title="恐贪指数 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache: dict = {}
CACHE_TTL = 300


def cache_get(key: str) -> Optional[dict]:
    if key in cache:
        entry = cache[key]
        if time.time() - entry["ts"] < CACHE_TTL:
            return entry["data"]
    return None


def cache_set(key: str, data: dict):
    cache[key] = {"ts": time.time(), "data": data}


def _classify(score: float) -> str:
    s = float(score)
    if s <= 25:
        return "极度恐惧"
    elif s <= 45:
        return "恐惧"
    elif s <= 55:
        return "中性"
    elif s <= 75:
        return "贪婪"
    else:
        return "极度贪婪"


def _fallback(market: str, icon: str, error: str = "") -> dict:
    return {
        "source": "fallback",
        "market": market,
        "icon": icon,
        "score": 50,
        "label": "数据获取中",
        "error": error,
        "updated": datetime.now().isoformat(),
    }


# ─── US Stock (CNN Official API via curl_cffi TLS fingerprint) ───

def _build_us_result(raw: dict) -> dict:
    fg = raw.get("fear_and_greed", {})
    score = round(fg.get("score", 50), 1)
    rating = fg.get("rating", "neutral")
    rating_map = {
        "extreme fear": "极度恐惧", "fear": "恐惧",
        "neutral": "中性", "greed": "贪婪", "extreme greed": "极度贪婪",
    }
    indicator_names = {
        "market_momentum_sp500": "标普500动量", "stock_price_strength": "股价强度",
        "stock_price_breadth": "股价广度", "put_call_options": "看跌/看涨期权",
        "market_volatility_vix": "VIX波动率", "junk_bond_demand": "垃圾债券需求",
        "safe_haven_demand": "避险需求",
    }
    components = []
    for key, name in indicator_names.items():
        ind = raw.get(key, {})
        components.append({"name": name, "score": ind.get("score", 0), "rating": ind.get("rating", "")})
    return {
        "source": "CNN (production.dataviz.cnn.io)",
        "market": "美股", "icon": "🇺🇸",
        "score": score, "label": rating_map.get(rating, rating),
        "components": components,
        "history": {
            "1w": round(fg.get("previous_1_week", 0), 1),
            "1m": round(fg.get("previous_1_month", 0), 1),
            "1y": round(fg.get("previous_1_year", 0), 1),
        },
        "timestamp": fg.get("timestamp"),
        "updated": datetime.now().isoformat(),
    }


def fetch_us_fear_greed() -> dict:
    cached = cache_get("us_fng")
    if cached:
        return cached

    # First try local cache file (updated externally via WebFetch)
    cache_path = os.path.join(os.path.dirname(__file__), "cnn_cache.json")
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                raw = json.load(f)
            cached_at = raw.get("_cached_at", "")
            if cached_at:
                cache_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
                age_hours = (datetime.now().astimezone() - cache_time).total_seconds() / 3600
                if age_hours < 2 and raw.get("fear_and_greed", {}).get("score"):
                    return _build_us_result(raw)
    except Exception:
        pass

    # Try live CNN API — plain requests first (works on US servers), then curl_cffi
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today}"
        headers = {
            "Accept": "application/json",
            "Origin": "https://edition.cnn.com",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        cnn_data = None

        # 1) Try plain requests (works on US/EU servers)
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                cnn_data = r.json()
        except Exception:
            pass

        # 2) Try curl_cffi TLS fingerprint (needed in China)
        if not cnn_data:
            try:
                from curl_cffi import requests as curl_requests
                for target in ["chrome131", "chrome124", "firefox133"]:
                    try:
                        r = curl_requests.get(url, headers=headers, impersonate=target, timeout=20)
                        if r.status_code == 200:
                            cnn_data = r.json()
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if not cnn_data:
            raise Exception("CNN API unreachable")

        raw = cnn_data
        result = _build_us_result(raw)
        cache_set("us_fng", result)
        return result
    except Exception:
        pass

    # Fallback to feargreedchart.com
    try:
        r = requests.get("https://feargreedchart.com/api/?action=all", timeout=15)
        data = r.json()
        score_data = data.get("score", {})
        result = {
            "source": "feargreedchart.com (CNN fallback)",
            "market": "美股",
            "icon": "🇺🇸",
            "score": score_data.get("score", 50),
            "label": _classify(score_data.get("score", 50)),
            "components": score_data.get("components", []),
            "updated": datetime.now().isoformat(),
        }
        cache_set("us_fng", result)
        return result
    except Exception as e:
        return _fallback("美股", "🇺🇸", str(e))


# ─── Crypto (CoinMarketCap primary, alternative.me fallback) ───

CMC_API_KEY = os.environ.get("CMC_API_KEY", "")

def fetch_crypto_fear_greed() -> dict:
    cached = cache_get("crypto_fng")
    if cached:
        return cached

    # Try CoinMarketCap first (user's key)
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
        r = requests.get(
            "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            fng = data.get("data", {})
            score = int(fng.get("value", 50))
            classification = fng.get("value_classification", "neutral")
            rating_map = {
                "Extreme Fear": "极度恐惧", "Fear": "恐惧",
                "Neutral": "中性", "Greed": "贪婪",
                "Extreme Greed": "极度贪婪",
            }
            result = {
                "source": "CoinMarketCap",
                "market": "加密货币",
                "icon": "₿",
                "score": score,
                "label": rating_map.get(classification, classification),
                "timestamp": fng.get("timestamp"),
                "updated": datetime.now().isoformat(),
            }
            # Add BTC price factors via CoinGecko
            try:
                kr = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=60", timeout=10)
                if kr.status_code == 200:
                    cg = kr.json()
                    prices = [p[1] for p in (cg.get("prices") or [])]
                    volumes = [v[1] for v in (cg.get("total_volumes") or [])]
                    if len(prices) >= 20:
                        factors = _compute_sentiment(prices, volumes)
                        result["components"] = factors.get("components", {})
            except Exception:
                pass
            cache_set("crypto_fng", result)
            return result
    except Exception:
        pass

    # Fallback to alternative.me
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()
        fng_data = data["data"][0]
        score = int(fng_data["value"])
        result = {
            "source": "alternative.me (CMC fallback)",
            "market": "加密货币",
            "icon": "₿",
            "score": score,
            "label": _classify(score),
            "timestamp": fng_data.get("timestamp"),
            "updated": datetime.now().isoformat(),
        }
        cache_set("crypto_fng", result)
        return result
    except Exception as e:
        return _fallback("加密货币", "₿", str(e))


# ─── Gold ───

def fetch_gold_fear_greed() -> dict:
    cached = cache_get("gold_fng")
    if cached:
        return cached
    try:
        r = requests.get("https://onoff.markets/data/gold-fear-greed.json", timeout=15)
        data = r.json()
        raw_components = data.get("components", {})
        gold_names = {
            "gld_price": "GLD价格", "momentum": "RSI动量", "gold_vs_spy": "黄金vs标普",
            "dollar_index": "美元指数", "real_rates": "实际利率", "vix": "VIX波动",
        }
        components = []
        for key, name in gold_names.items():
            c = raw_components.get(key, {})
            if c:
                components.append({"name": name, "score": c.get("score", 0)})

        result = {
            "source": "onoff.markets",
            "market": "黄金",
            "icon": "🥇",
            "score": round(data.get("score", 50), 1),
            "label": _classify(data.get("score", 50)),
            "components": components,
            "updated": data.get("timestamp", datetime.now().isoformat()),
        }
        cache_set("gold_fng", result)
        return result
    except Exception as e:
        return _fallback("黄金", "🥇", str(e))


# ─── US Bonds (onoff.markets) ───

def fetch_usbonds_fear_greed() -> dict:
    cached = cache_get("usbonds_fng")
    if cached:
        return cached
    try:
        r = requests.get("https://onoff.markets/data/bonds-fear-greed.json", timeout=15)
        data = r.json()
        raw_components = data.get("components", {})
        bond_names = {
            "yield_curve": "收益率曲线", "duration_risk": "久期风险", "credit_quality": "信用质量",
            "real_rates": "实际利率", "bond_volatility": "债券波动", "equity_vs_bonds": "股债对比",
        }
        components = []
        for key, name in bond_names.items():
            c = raw_components.get(key, {})
            if c:
                components.append({"name": name, "score": c.get("score", 0)})

        result = {
            "source": "onoff.markets",
            "market": "美国国债",
            "icon": "🇺🇸📜",
            "score": round(data.get("score", 50), 1),
            "label": _classify(data.get("score", 50)),
            "components": components,
            "updated": data.get("timestamp", datetime.now().isoformat()),
        }
        cache_set("usbonds_fng", result)
        return result
    except Exception as e:
        return _fallback("美国国债", "🇺🇸📜", str(e))


# ─── Chinese Government Bonds (Sina Bond Index) ───

def _fetch_sina_kline(symbol: str, days: int = 60) -> list:
    """Fetch daily K-line from Sina Finance for indices like sh000012 (bond index)."""
    try:
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={days}"
        headers = {"Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = "gbk"
        data = r.json()
        result = []
        for item in data:
            result.append({
                "date": item.get("day", ""),
                "close": float(item.get("close", 0)),
                "volume": float(item.get("volume", 0)),
            })
        return result
    except Exception:
        return []


def fetch_cnbonds_fear_greed() -> dict:
    cached = cache_get("cnbonds_fng")
    if cached:
        return cached

    try:
        history = _fetch_sina_kline("sh000012", 60)
        if history and len(history) >= 20:
            prices = [h["close"] for h in history]
            volumes = [h["volume"] for h in history]
            sentiment = _compute_sentiment(prices, volumes)
            result = {
                "source": "Sina Finance (国债指数)",
                "market": "中国国债",
                "icon": "🇨🇳📜",
                "score": sentiment["score"],
                "label": sentiment["label"],
                "index_name": "国债指数",
                "index_price": sentiment["index_price"],
                "components": sentiment["components"],
                "updated": datetime.now().isoformat(),
            }
            cache_set("cnbonds_fng", result)
            return result
    except Exception:
        pass

    return _fallback("中国国债", "🇨🇳📜")


# ─── Index History via Tencent Finance ───

def _fetch_tencent_kline(code: str, days: int = 60) -> list:
    """Fetch daily K-line data from Tencent Finance API.
    code: e.g. 'sh000001' for SSE, 'hkHSI' for Hang Seng
    Returns list of {date, close, volume} dicts sorted oldest-first.
    Each kline: [date, open, close, high, low, volume]
    """
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
        headers = {"Referer": "https://gu.qq.com/"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            return []

        stock_data = data.get("data", {}).get(code, {})
        klines = stock_data.get("day", stock_data.get("qfqday", []))
        if not klines:
            return []

        result = []
        for k in klines:
            result.append({
                "date": k[0],
                "close": float(k[2]),
                "volume": float(k[5]) if len(k) > 5 else 0,
            })
        return result
    except Exception:
        return []


def _compute_sentiment(prices: list, volumes: list = None) -> dict:
    """Compute a simple fear-greed score from price data."""
    if len(prices) < 20:
        return {"score": 50, "label": "数据不足", "components": {}}

    current = prices[-1]
    ma5 = sum(prices[-5:]) / 5 if len(prices) >= 5 else current
    ma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else current
    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else current

    # Momentum (30%): position vs 50-day MA
    momentum_score = min(100, max(0, 50 + (current / ma50 - 1) * 400))

    # Volatility (30%): recent vs historical volatility
    returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    recent_vol = (sum(r * r for r in returns[-10:]) / min(len(returns), 10)) ** 0.5 if len(returns) >= 10 else 0.01
    hist_vol = (sum(r * r for r in returns) / len(returns)) ** 0.5 if returns else 0.01
    vol_ratio = recent_vol / hist_vol if hist_vol > 0 else 1
    vol_score = min(100, max(0, 50 - (vol_ratio - 1) * 80))

    # Volume momentum (20%)
    if volumes and len(volumes) >= 10:
        vol_ma5 = sum(volumes[-5:]) / 5
        vol_ma20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else vol_ma5
        vol_momentum = vol_ma5 / vol_ma20 if vol_ma20 > 0 else 1
        vol_score_val = min(100, max(0, 50 + (vol_momentum - 1) * 150))
    else:
        vol_score_val = 50

    # Trend (20%): short-term vs medium-term
    trend_score = min(100, max(0, 50 + (ma5 / ma20 - 1) * 400))

    composite = momentum_score * 0.30 + vol_score * 0.30 + vol_score_val * 0.20 + trend_score * 0.20

    return {
        "score": round(composite, 1),
        "label": _classify(composite),
        "components": {
            "动量": round(momentum_score, 1),
            "波动率": round(vol_score, 1),
            "成交量": round(vol_score_val, 1),
            "趋势": round(trend_score, 1),
        },
        "index_price": round(current, 2),
    }


def fetch_ashare_fear_greed() -> dict:
    cached = cache_get("ashare_fng")
    if cached:
        return cached

    try:
        history = _fetch_tencent_kline("sh000001", 60)
        if history and len(history) >= 20:
            prices = [h["close"] for h in history]
            volumes = [h["volume"] for h in history]
            sentiment = _compute_sentiment(prices, volumes)
            result = {
                "source": "Tencent Finance",
                "market": "A股",
                "icon": "🇨🇳",
                "score": sentiment["score"],
                "label": sentiment["label"],
                "index_name": "上证指数",
                "index_price": sentiment["index_price"],
                "components": sentiment["components"],
                "updated": datetime.now().isoformat(),
            }
            cache_set("ashare_fng", result)
            return result
    except Exception as e:
        pass

    return _fallback("A股", "🇨🇳")


# ─── HK Stock via Tencent Finance ───

def fetch_hk_fear_greed() -> dict:
    cached = cache_get("hk_fng")
    if cached:
        return cached

    try:
        history = _fetch_tencent_kline("hkHSI", 60)
        if history and len(history) >= 20:
            prices = [h["close"] for h in history]
            volumes = [h["volume"] for h in history]
            sentiment = _compute_sentiment(prices, volumes)
            result = {
                "source": "Tencent Finance",
                "market": "港股",
                "icon": "🇭🇰",
                "score": sentiment["score"],
                "label": sentiment["label"],
                "index_name": "恒生指数",
                "index_price": sentiment["index_price"],
                "components": sentiment["components"],
                "updated": datetime.now().isoformat(),
            }
            cache_set("hk_fng", result)
            return result
    except Exception:
        pass

    return _fallback("港股", "🇭🇰")


# ─── API Endpoints ───


@app.get("/api/all")
async def get_all():
    us, crypto, gold, usbonds, ashare, hk, cnbonds = await asyncio.gather(
        asyncio.to_thread(fetch_us_fear_greed),
        asyncio.to_thread(fetch_crypto_fear_greed),
        asyncio.to_thread(fetch_gold_fear_greed),
        asyncio.to_thread(fetch_usbonds_fear_greed),
        asyncio.to_thread(fetch_ashare_fear_greed),
        asyncio.to_thread(fetch_hk_fear_greed),
        asyncio.to_thread(fetch_cnbonds_fear_greed),
    )
    return {
        "timestamp": datetime.now().isoformat(),
        "markets": [us, ashare, hk, gold, crypto, usbonds, cnbonds],
    }


@app.get("/api/{market}/history")
async def get_market_history(market: str, days: int = 30):
    """Return historical score data [{date, score}] sorted oldest-first for charting."""
    try:
        if market == "gold":
            r = requests.get("https://onoff.markets/data/gold-fear-greed.json", timeout=15)
            data = r.json()
            history = data.get("history", [])
            # onoff.markets history is newest-first, reverse to oldest-first
            history.reverse()
            return [{"date": h["date"], "score": h["score"]} for h in history[-days:]]

        elif market == "crypto":
            r = requests.get(f"https://api.alternative.me/fng/?limit={days}&date_format=cn", timeout=10)
            data = r.json()
            items = data.get("data", [])
            # alternative.me returns newest-first, reverse to oldest-first
            items.reverse()
            return [{"date": d["timestamp"], "score": int(d["value"])} for d in items]

        elif market == "us":
            # feargreedchart.com history is oldest-first, no reversal needed
            r = requests.get("https://feargreedchart.com/api/?action=history", timeout=15)
            data = r.json()
            return [{"date": d["date"], "score": d["score"]} for d in data[-days:]]

        elif market == "usbonds":
            r = requests.get("https://onoff.markets/data/bonds-fear-greed.json", timeout=15)
            data = r.json()
            history = data.get("history", [])
            # onoff.markets history is newest-first, reverse to oldest-first
            history.reverse()
            return [{"date": h["date"], "score": h["score"]} for h in history[-days:]]

        elif market in ("ashare", "hk", "cnbonds"):
            symbols = {"ashare": "sh000001", "hk": "hkHSI", "cnbonds": "sh000012"}
            code = symbols.get(market, "")
            if market == "cnbonds":
                klines = _fetch_sina_kline(code, days + 60)
            else:
                klines = _fetch_tencent_kline(code, days + 60)

            if not klines or len(klines) < 20:
                return []

            # Tencent/Sina K-line is already oldest-first
            results = []
            window = 30
            for i in range(window, len(klines)):
                window_prices = [k["close"] for k in klines[i - window:i + 1]]
                window_volumes = [k["volume"] for k in klines[i - window:i + 1]]
                sentiment = _compute_sentiment(window_prices, window_volumes)
                results.append({
                    "date": klines[i]["date"],
                    "score": sentiment["score"],
                })
            return results[-days:]
        else:
            return []
    except Exception:
        return []


@app.get("/api/{market}")
async def get_market(market: str):
    fetchers = {
        "us": fetch_us_fear_greed,
        "crypto": fetch_crypto_fear_greed,
        "gold": fetch_gold_fear_greed,
        "usbonds": fetch_usbonds_fear_greed,
        "cnbonds": fetch_cnbonds_fear_greed,
        "ashare": fetch_ashare_fear_greed,
        "hk": fetch_hk_fear_greed,
    }
    if market not in fetchers:
        return JSONResponse({"error": f"Unknown market: {market}"}, status_code=404)
    return await asyncio.to_thread(fetchers[market])


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/")
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
