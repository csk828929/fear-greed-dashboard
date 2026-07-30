// Cloudflare Pages Function - /api/history
// Proxies through Render for reliable data access
export async function onRequest(context) {
  const url = new URL(context.request.url);
  const market = url.searchParams.get('market') || 'us';
  const days = url.searchParams.get('days') || '30';
  const headers = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };

  if (context.request.method === 'OPTIONS') return new Response(null, { status: 204, headers });

  try {
    const proxyUrl = `https://fear-greed-dashboard.onrender.com/api/${market}/history?days=${days}`;
    const res = await fetch(proxyUrl);
    if (!res.ok) throw new Error('upstream error');
    const data = await res.json();
    return new Response(JSON.stringify(data), { headers });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'no data' }), { status: 404, headers });
  }
}
