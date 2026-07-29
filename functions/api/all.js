// Cloudflare Pages Function - /api/all
export async function onRequest(context) {
  const headers = {
    'Access-Control-Allow-Origin': '*',
    'Content-Type': 'application/json; charset=utf-8',
  };
  if (context.request.method === 'OPTIONS') return new Response(null, { status: 204, headers });

  const cmcKey = context.env.CMC_API_KEY || '';

  const results = await Promise.allSettled([
    fetchCNN(), fetchCrypto(cmcKey), fetchGold(), fetchUSBonds(),
    fetchChina('ashare', 'sh000001', 'A股', '🇨🇳'),
    fetchChina('hk', 'hkHSI', '港股', '🇭🇰'),
    fetchChina('cnbonds', 'sh000012', '中国国债', '🇨🇳📜'),
  ]);
  const markets = results.map(r => r.status === 'fulfilled' && r.value ? r.value : null).filter(Boolean);
  return new Response(JSON.stringify({ markets }), { headers });
}

// ─── Data fetchers ───

async function fetchCNN() {
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  // Try today first, fallback to yesterday
  for (const date of [new Date().toISOString().slice(0, 10), yesterday]) {
    try {
      const url = `https://production.dataviz.cnn.io/index/fearandgreed/graphdata/${date}`;
      const res = await fetch(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
          Accept: 'application/json',
          Origin: 'https://edition.cnn.com',
          Referer: 'https://edition.cnn.com/markets/fear-and-greed',
        }
      });
      if (!res.ok) continue;
      const raw = await res.json();
      const fg = raw.fear_and_greed || {};
      if (!fg.score) continue;
      return {
        source: 'CNN', market: '美股', icon: '🇺🇸',
        score: Math.round((fg.score || 0) * 10) / 10,
        label: label(fg.score),
        components: [
          { name: '标普500动量', score: raw.market_momentum_sp500?.score ?? 0 },
          { name: '股价强度', score: raw.stock_price_strength?.score ?? 0 },
          { name: '股价广度', score: raw.stock_price_breadth?.score ?? 0 },
          { name: '看跌/看涨期权', score: raw.put_call_options?.score ?? 0 },
          { name: 'VIX波动率', score: raw.market_volatility_vix?.score ?? 0 },
          { name: '垃圾债券需求', score: raw.junk_bond_demand?.score ?? 0 },
          { name: '避险需求', score: raw.safe_haven_demand?.score ?? 0 },
        ],
      };
    } catch (e) { continue; }
  }
  return null;
}

async function fetchCrypto(apiKey) {
  let cmcResult = null;
  if (apiKey) {
    try {
      const res = await fetch('https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest', {
        headers: { 'X-CMC_PRO_API_KEY': apiKey, Accept: 'application/json' }
      });
      if (res.ok) {
        const d = (await res.json()).data || {};
        const map = { 'Extreme Fear': '极度恐惧', Fear: '恐惧', Neutral: '中性', Greed: '贪婪', 'Extreme Greed': '极度贪婪' };
        cmcResult = { source: 'CoinMarketCap', market: '加密货币', icon: '₿', score: d.value || 50, label: map[d.value_classification] || d.value_classification };
      }
    } catch (e) {}
  }

  // Fetch BTC kline for factor computation (Binance)
  try {
    const kres = await fetch('https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=60');
    if (kres.ok) {
      const klines = await kres.json();
      const prices = klines.map(k => parseFloat(k[4])); // close prices
      const volumes = klines.map(k => parseFloat(k[5]));
      if (prices.length >= 20 && cmcResult) {
        const factors = computeCN(prices, volumes, prices[prices.length - 1]);
        cmcResult.components = factors.components;
        cmcResult.btc_price = factors.index_price;
      }
    }
  } catch (e) {}

  return cmcResult;
}

async function fetchGold() {
  const res = await fetch('https://onoff.markets/data/gold-fear-greed.json');
  if (!res.ok) return null;
  const d = await res.json();
  const comps = d.components || {};
  const names = { gld_price: 'GLD价格', momentum: 'RSI动量', gold_vs_spy: '黄金vs标普', dollar_index: '美元指数', real_rates: '实际利率', vix: 'VIX波动' };
  const components = Object.entries(names).map(([k, n]) => ({ name: n, score: comps[k]?.score || 0 }));
  return { source: 'onoff.markets', market: '黄金', icon: '🥇', score: Math.round(d.score * 10) / 10, label: label(d.score), components };
}

async function fetchUSBonds() {
  const res = await fetch('https://onoff.markets/data/bonds-fear-greed.json');
  if (!res.ok) return null;
  const d = await res.json();
  const comps = d.components || {};
  const names = { yield_curve: '收益率曲线', duration_risk: '久期风险', credit_quality: '信用质量', real_rates: '实际利率', bond_volatility: '债券波动', equity_vs_bonds: '股债对比' };
  const components = Object.entries(names).map(([k, n]) => ({ name: n, score: comps[k]?.score || 0 }));
  return { source: 'onoff.markets', market: '美国国债', icon: '🇺🇸📜', score: Math.round(d.score * 10) / 10, label: label(d.score), components };
}

async function fetchChina(market, code, name, icon) {
  const isSina = market === 'cnbonds';
  const url = isSina
    ? `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=${code}&scale=240&ma=no&datalen=60`
    : `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${code},day,,,60,qfq`;
  const res = await fetch(url, { headers: { Referer: isSina ? 'https://finance.sina.com.cn/' : 'https://gu.qq.com/' } });
  if (!res.ok) return null;

  let prices = [], volumes = [];
  if (isSina) {
    const klines = await res.json();
    prices = klines.map(k => parseFloat(k.close));
    volumes = klines.map(k => parseFloat(k.volume) || 0);
  } else {
    const data = await res.json();
    const klines = (data.data || {})[code]?.day || (data.data || {})[code]?.qfqday || [];
    prices = klines.map(k => parseFloat(k[2]));
    volumes = klines.map(k => parseFloat(k[5]) || 0);
  }
  if (prices.length < 20) return null;

  return { source: isSina ? 'Sina' : 'Tencent', market: name, icon, ...computeCN(prices, volumes, prices[prices.length - 1]) };
}

function label(s) { s = s || 50; return s <= 25 ? '极度恐惧' : s <= 45 ? '恐惧' : s <= 55 ? '中性' : s <= 75 ? '贪婪' : '极度贪婪'; }

function computeCN(prices, volumes, current) {
  const ma5 = prices.slice(-5).reduce((a, b) => a + b, 0) / 5;
  const ma20 = prices.slice(-20).reduce((a, b) => a + b, 0) / 20;
  const ma50 = prices.slice(-50).reduce((a, b) => a + b, 0) / 50;
  const momentum = Math.min(100, Math.max(0, 50 + (current / ma50 - 1) * 400));
  const returns = prices.slice(1).map((p, i) => p / prices[i] - 1);
  const rv = Math.sqrt(returns.slice(-10).reduce((a, r) => a + r * r, 0) / 10) || 0.01;
  const hv = Math.sqrt(returns.reduce((a, r) => a + r * r, 0) / returns.length) || 0.01;
  const vol = Math.min(100, Math.max(0, 50 - (rv / hv - 1) * 80));
  const vma5 = volumes.slice(-5).reduce((a, b) => a + b, 0) / 5;
  const vma20 = volumes.slice(-20).reduce((a, b) => a + b, 0) / 20 || vma5;
  const vm = Math.min(100, Math.max(0, 50 + (vma5 / vma20 - 1) * 150));
  const trend = Math.min(100, Math.max(0, 50 + (ma5 / ma20 - 1) * 500));
  const score = Math.round((momentum * 0.3 + vol * 0.3 + vm * 0.2 + trend * 0.2) * 10) / 10;
  return { score, label: label(score), index_price: Math.round(current * 100) / 100, components: { '动量': Math.round(momentum * 10) / 10, '波动率': Math.round(vol * 10) / 10, '成交量': Math.round(vm * 10) / 10, '趋势': Math.round(trend * 10) / 10 } };
}
