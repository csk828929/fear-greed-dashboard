// Cloudflare Pages Function - /api/history
export async function onRequest(context) {
  const url = new URL(context.request.url);
  const market = url.searchParams.get('market') || 'us';
  const days = parseInt(url.searchParams.get('days') || '30');
  const headers = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };

  if (context.request.method === 'OPTIONS') return new Response(null, { status: 204, headers });

  const codeMap = { us: 'us', ashare: 'ashare', hk: 'hk', gold: 'gold', crypto: 'crypto', usbonds: 'usbonds', cnbonds: 'cnbonds' };
  const marketCode = codeMap[market] || market;

  try {
    const history = await fetchHistory(marketCode, days);
    if (history && history.length > 0) {
      return new Response(JSON.stringify(history), { headers });
    }
    return new Response(JSON.stringify({ error: 'no data' }), { status: 404, headers });
  } catch (e) {
    return new Response(JSON.stringify({ error: e.message }), { status: 500, headers });
  }
}

async function fetchHistory(market, days) {
  let klineCode, isSina = false;
  if (market === 'ashare') klineCode = 'sh000001';
  else if (market === 'hk') klineCode = 'hkHSI';
  else if (market === 'cnbonds') { klineCode = 'sh000012'; isSina = true; }
  else if (market === 'gold') klineCode = 'usGLD';
  else if (market === 'usbonds') klineCode = 'usTLT';
  else if (market === 'us') klineCode = 'us'; // use SPY or CNN
  else if (market === 'crypto') klineCode = 'crypto';
  else return [];

  let url, headersOpt = {};
  if (isSina) {
    url = `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=${klineCode}&scale=240&ma=no&datalen=${days}`;
    headersOpt = { Referer: 'https://finance.sina.com.cn/' };
  } else {
    url = `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=${klineCode},day,,,${days},qfq`;
    headersOpt = { Referer: 'https://gu.qq.com/' };
  }

  const res = await fetch(url, { headers: headersOpt });
  if (!res.ok) return [];

  let prices = [];
  if (isSina) {
    const klines = await res.json();
    prices = klines.map(k => ({ date: k.day, score: 0, close: parseFloat(k.close) }));
  } else {
    const data = await res.json();
    const klines = (data.data || {})[klineCode]?.day || (data.data || {})[klineCode]?.qfqday || [];
    prices = klines.map(k => ({ date: k[0], score: 0, close: parseFloat(k[2]) }));
  }

  // Compute fear-greed scores from kline data
  for (let i = 20; i < prices.length; i++) {
    const slice = prices.slice(0, i + 1);
    const ps = slice.map(p => p.close);
    const vs = slice.map(() => 1);
    const { score } = computeCN(ps, vs, ps[ps.length - 1]);
    // Invert for cnbonds
    prices[i].score = market === 'cnbonds' ? (100 - score) : score;
  }
  // Trim to requested days
  return prices.slice(-days).filter(p => p.score > 0).map(p => ({ date: p.date, score: Math.round(p.score * 10) / 10 }));
}

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
  return { score, index_price: Math.round(current * 100) / 100, components: { '动量': Math.round(momentum * 10) / 10, '波动率': Math.round(vol * 10) / 10, '成交量': Math.round(vm * 10) / 10, '趋势': Math.round(trend * 10) / 10 } };
}
