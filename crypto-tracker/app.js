/* CriptoCartera — PWA de cartera de cripto + tendencia de mercado.
 *
 * Modelo SIN claves: el usuario registra sus monedas y cantidades, y la app
 * calcula el valor usando datos PÚBLICOS de mercado (no accede a ninguna cuenta).
 *   - Precios y % 24h ... API pública de Binance (/ticker/24hr)
 *   - Gráficas históricas ... API pública de Binance (/klines)
 *   - Índice Miedo/Codicia ... alternative.me (/fng)
 *
 * Nada de esto requiere API keys: son endpoints públicos de solo lectura.
 */
'use strict';

// ------------------------------------------------------------------ Estado
const LS = {
  holdings: 'cc_holdings',
  fiat: 'cc_fiat',
};

const state = {
  holdings: load(LS.holdings, []),          // [{ symbol:'BTC', amount:0.5 }]
  fiat: load(LS.fiat, 'USD'),               // 'USD' | 'EUR'
  prices: {},                               // symbol -> { price, changePct }  (en USD)
  eurUsd: 1,                                // 1 EUR = eurUsd USD (para convertir)
  chartRange: 7,
  chartSymbol: null,
  editIndex: -1,                            // índice en edición (modal), -1 = nuevo
};

// Fuentes (con respaldo) para datos públicos de Binance.
const BINANCE_BASES = [
  'https://api.binance.com',
  'https://data-api.binance.vision',
  'https://api1.binance.com',
];

const STABLES = new Set(['USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'BUSD']);

// ------------------------------------------------------------------ Utilidades
function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

function parseAmount(str) {
  if (typeof str === 'number') return str;
  const n = parseFloat(String(str).replace(/\s/g, '').replace(',', '.'));
  return Number.isFinite(n) ? n : NaN;
}

function fmtFiat(usdValue) {
  const value = state.fiat === 'EUR' ? usdValue / state.eurUsd : usdValue;
  const cur = state.fiat;
  return new Intl.NumberFormat('es-ES', {
    style: 'currency', currency: cur,
    maximumFractionDigits: value >= 1000 ? 0 : 2,
  }).format(value);
}
function fmtPrice(usdValue) {
  const value = state.fiat === 'EUR' ? usdValue / state.eurUsd : usdValue;
  const cur = state.fiat;
  const dec = value >= 1 ? 2 : value >= 0.01 ? 4 : 8;
  return new Intl.NumberFormat('es-ES', {
    style: 'currency', currency: cur, maximumFractionDigits: dec,
  }).format(value);
}
function fmtAmount(n) {
  return new Intl.NumberFormat('es-ES', { maximumFractionDigits: 8 }).format(n);
}
function fmtPct(p) {
  const s = (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  return s;
}

async function fetchJSONWithFallback(path) {
  let lastErr;
  for (const base of BINANCE_BASES) {
    try {
      const res = await fetch(base + path, { cache: 'no-store' });
      if (!res.ok) { lastErr = new Error('HTTP ' + res.status); continue; }
      return await res.json();
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error('Sin conexión');
}

// ------------------------------------------------------------------ Datos de mercado
function pairFor(symbol) {
  const s = symbol.toUpperCase();
  if (STABLES.has(s)) return null;          // las stablecoins valen ~1 USD
  return s + 'USDT';
}

// Devuelve la lista de pares (BTCUSDT...) que necesitamos cotizar.
function neededPairs() {
  const pairs = new Set();
  for (const h of state.holdings) {
    const p = pairFor(h.symbol);
    if (p) pairs.add(p);
  }
  return Array.from(pairs);
}

async function refreshPrices() {
  state.prices = {};

  // Tipo de cambio EUR/USD (para mostrar en euros). EURUSDT ≈ USD por 1 EUR.
  if (state.fiat === 'EUR') {
    try {
      const t = await fetchJSONWithFallback('/api/v3/ticker/price?symbol=EURUSDT');
      const v = parseFloat(t.price);
      if (Number.isFinite(v) && v > 0) state.eurUsd = v;
    } catch { /* mantenemos el último valor conocido */ }
  }

  // Stablecoins: precio 1, cambio 0.
  for (const h of state.holdings) {
    const s = h.symbol.toUpperCase();
    if (STABLES.has(s)) state.prices[s] = { price: 1, changePct: 0 };
  }

  const pairs = neededPairs();
  if (pairs.length === 0) return;

  // Una sola petición con todos los símbolos necesarios.
  const param = encodeURIComponent(JSON.stringify(pairs));
  let data;
  try {
    data = await fetchJSONWithFallback('/api/v3/ticker/24hr?symbols=' + param);
  } catch (e) {
    throw e;
  }
  const list = Array.isArray(data) ? data : [data];
  for (const t of list) {
    const sym = t.symbol.replace(/USDT$/, '');
    const price = parseFloat(t.lastPrice);
    const changePct = parseFloat(t.priceChangePercent);
    if (Number.isFinite(price)) state.prices[sym] = { price, changePct: Number.isFinite(changePct) ? changePct : 0 };
  }
}

async function fetchKlines(symbol, days) {
  let interval, limit;
  if (days <= 7) { interval = '4h'; limit = 42; }
  else if (days <= 30) { interval = '1d'; limit = 30; }
  else { interval = '1w'; limit = 53; }
  const pair = pairFor(symbol) || (symbol.toUpperCase() + 'USDT');
  const data = await fetchJSONWithFallback(
    `/api/v3/klines?symbol=${pair}&interval=${interval}&limit=${limit}`);
  return data.map(k => ({ t: k[0], close: parseFloat(k[4]) }));
}

async function fetchFearGreed() {
  // alternative.me expone CORS abierto para este endpoint público.
  const res = await fetch('https://api.alternative.me/fng/?limit=1', { cache: 'no-store' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const j = await res.json();
  return j.data && j.data[0];
}

// ------------------------------------------------------------------ Cálculos
function holdingValueUSD(h) {
  const p = state.prices[h.symbol.toUpperCase()];
  if (!p) return null;
  return h.amount * p.price;
}

function portfolioTotals() {
  let now = 0, prev = 0, hasUnknown = false;
  for (const h of state.holdings) {
    const v = holdingValueUSD(h);
    if (v == null) { hasUnknown = true; continue; }
    const p = state.prices[h.symbol.toUpperCase()];
    now += v;
    prev += v / (1 + p.changePct / 100);
  }
  const changeAbs = now - prev;
  const changePct = prev > 0 ? (changeAbs / prev) * 100 : 0;
  return { now, changeAbs, changePct, hasUnknown };
}

// ------------------------------------------------------------------ Render
function render() {
  renderTotals();
  renderHoldings();
  renderManageList();
  renderCoinSelect();
  $('#currencyToggle').textContent = state.fiat;
  $('#fiatSelect').value = state.fiat;
}

function renderTotals() {
  const t = portfolioTotals();
  const totalEl = $('#totalValue');
  const chgEl = $('#totalChange');
  if (state.holdings.length === 0) {
    totalEl.textContent = fmtFiat(0);
    chgEl.innerHTML = '&nbsp;';
    return;
  }
  totalEl.textContent = fmtFiat(t.now);
  const cls = t.changeAbs >= 0 ? 'pos' : 'neg';
  chgEl.className = 'total-change ' + cls;
  chgEl.textContent = `${fmtFiat(t.changeAbs)} (${fmtPct(t.changePct)}) · 24h`;
}

function coinBadge(symbol) {
  const s = symbol.toUpperCase().slice(0, 4);
  return `<div class="coin-badge">${s}</div>`;
}

function renderHoldings() {
  const list = $('#holdingsList');
  const empty = $('#holdingsEmpty');
  const addBtn = $('#addCoinBtn');
  if (state.holdings.length === 0) {
    empty.hidden = false;
    addBtn.hidden = true;
    list.innerHTML = '';
    return;
  }
  empty.hidden = true;
  addBtn.hidden = false;

  // Ordenar por valor descendente (las desconocidas al final).
  const rows = state.holdings.map((h, i) => ({ h, i, v: holdingValueUSD(h) }));
  rows.sort((a, b) => (b.v ?? -1) - (a.v ?? -1));

  list.innerHTML = rows.map(({ h, v }) => {
    const p = state.prices[h.symbol.toUpperCase()];
    const priceTxt = p ? fmtPrice(p.price) : '—';
    const chgTxt = p ? fmtPct(p.changePct) : 'sin datos';
    const chgCls = p ? (p.changePct >= 0 ? 'pos' : 'neg') : 'muted';
    const valTxt = v == null ? '—' : fmtFiat(v);
    return `
      <li class="holding">
        ${coinBadge(h.symbol)}
        <div>
          <div class="name">${h.symbol.toUpperCase()}</div>
          <div class="sub">${fmtAmount(h.amount)} · ${priceTxt}</div>
        </div>
        <div class="right">
          <div class="val">${valTxt}</div>
          <div class="chg ${chgCls}">${chgTxt}</div>
        </div>
      </li>`;
  }).join('');
}

function renderManageList() {
  const list = $('#manageList');
  $('#holdingsCount').textContent = state.holdings.length
    ? `${state.holdings.length} ${state.holdings.length === 1 ? 'moneda' : 'monedas'}` : '';
  if (state.holdings.length === 0) {
    list.innerHTML = '<li class="muted small" style="padding:8px 0">Aún no has añadido monedas.</li>';
    return;
  }
  list.innerHTML = state.holdings.map((h, i) => `
    <li class="manage-item">
      ${coinBadge(h.symbol)}
      <div>
        <div class="name">${h.symbol.toUpperCase()}</div>
        <div class="amt">${fmtAmount(h.amount)}</div>
      </div>
      <button class="mini-btn" data-edit="${i}" aria-label="Editar">✎</button>
      <button class="mini-btn danger" data-del="${i}" aria-label="Eliminar">🗑</button>
    </li>`).join('');
}

function renderCoinSelect() {
  const sel = $('#coinSelect');
  const symbols = state.holdings.map(h => h.symbol.toUpperCase())
    .filter(s => !STABLES.has(s));
  const unique = Array.from(new Set(symbols));
  const options = unique.length ? unique : ['BTC', 'ETH'];
  if (!state.chartSymbol || !options.includes(state.chartSymbol)) {
    state.chartSymbol = options[0];
  }
  sel.innerHTML = options.map(s => `<option value="${s}">${s}</option>`).join('');
  sel.value = state.chartSymbol;
}

// ------------------------------------------------------------------ Gráfica
let chart;
async function renderChart() {
  const msg = $('#chartMsg');
  const meta = $('#chartMeta');
  const symbol = state.chartSymbol || 'BTC';
  msg.textContent = 'Cargando…';
  meta.textContent = '';
  try {
    const data = await fetchKlines(symbol, state.chartRange);
    if (!data.length) throw new Error('sin datos');
    msg.textContent = '';

    const labels = data.map(d => labelForDate(new Date(d.t), state.chartRange));
    const closes = data.map(d => d.close);
    const up = closes[closes.length - 1] >= closes[0];
    const color = up ? '#16c784' : '#ea3943';

    const ctx = $('#priceChart').getContext('2d');
    const grad = ctx.createLinearGradient(0, 0, 0, 220);
    grad.addColorStop(0, (up ? 'rgba(22,199,132,0.30)' : 'rgba(234,57,67,0.30)'));
    grad.addColorStop(1, 'rgba(0,0,0,0)');

    const cfg = {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: closes,
          borderColor: color,
          backgroundColor: grad,
          borderWidth: 2,
          fill: true,
          tension: 0.25,
          pointRadius: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: {
          callbacks: { label: (c) => fmtPrice(c.parsed.y) },
        } },
        scales: {
          x: { grid: { display: false },
               ticks: { color: '#8b94ac', maxTicksLimit: 5, autoSkip: true } },
          y: { grid: { color: 'rgba(255,255,255,0.05)' },
               ticks: { color: '#8b94ac', maxTicksLimit: 5,
                        callback: (v) => fmtPrice(v) } },
        },
      },
    };

    if (chart) { chart.destroy(); }
    chart = new Chart(ctx, cfg);

    const first = closes[0], last = closes[closes.length - 1];
    const pct = ((last - first) / first) * 100;
    meta.innerHTML = `<span class="${up ? 'pos' : 'neg'}">${fmtPct(pct)}</span> en ${labelForRange(state.chartRange)} · actual ${fmtPrice(last)}`;
  } catch (e) {
    if (chart) { chart.destroy(); chart = null; }
    msg.textContent = 'No se pudo cargar la gráfica (¿sin conexión o moneda no listada en Binance?).';
  }
}
function labelForRange(d) { return d <= 7 ? '7 días' : d <= 30 ? '30 días' : '1 año'; }
function labelForDate(date, range) {
  if (range <= 7) return date.toLocaleDateString('es-ES', { day: '2-digit', month: 'short' });
  if (range <= 30) return date.toLocaleDateString('es-ES', { day: '2-digit', month: 'short' });
  return date.toLocaleDateString('es-ES', { month: 'short', year: '2-digit' });
}

// ------------------------------------------------------------------ Fear & Greed
async function renderFearGreed() {
  const valEl = $('#fngValue');
  const labelEl = $('#fngLabel');
  const updatedEl = $('#fngUpdated');
  const arc = $('#gaugeArc');
  try {
    const d = await fetchFearGreed();
    const value = parseInt(d.value, 10);
    valEl.textContent = value;
    labelEl.textContent = translateFng(d.value_classification);

    // Arco semicircular: longitud ≈ π·86 ≈ 270. Rellenamos según 0..100.
    const len = 270;
    arc.style.strokeDasharray = len;
    arc.style.strokeDashoffset = len * (1 - value / 100);
    arc.setAttribute('stroke', fngColor(value));
    labelEl.classList.remove('muted');

    const ts = d.timestamp ? new Date(parseInt(d.timestamp, 10) * 1000) : null;
    updatedEl.textContent = ts ? 'act. ' + ts.toLocaleDateString('es-ES') : '';
  } catch {
    valEl.textContent = '—';
    labelEl.textContent = 'no disponible';
  }
}
function fngColor(v) {
  if (v < 25) return '#ea3943';       // miedo extremo
  if (v < 45) return '#f6b73c';       // miedo
  if (v < 55) return '#f3d24e';       // neutral
  if (v < 75) return '#9bd13a';       // codicia
  return '#16c784';                   // codicia extrema
}
function translateFng(c) {
  const map = {
    'Extreme Fear': 'Miedo extremo',
    'Fear': 'Miedo',
    'Neutral': 'Neutral',
    'Greed': 'Codicia',
    'Extreme Greed': 'Codicia extrema',
  };
  return map[c] || c || '';
}

// ------------------------------------------------------------------ Acciones / red
async function refreshAll() {
  const btn = $('#refreshBtn');
  const status = $('#statusBar');
  btn.classList.add('spin');
  status.hidden = true;
  status.classList.remove('error');
  try {
    await refreshPrices();
    render();
    const t = portfolioTotals();
    if (t.hasUnknown) {
      status.hidden = false;
      status.textContent = 'Algunas monedas no se pudieron cotizar (no listadas en Binance frente a USDT).';
    }
  } catch (e) {
    status.hidden = false;
    status.classList.add('error');
    status.textContent = 'No hay conexión con el mercado. Revisa tu internet e inténtalo de nuevo.';
  } finally {
    btn.classList.remove('spin');
  }
  // Tendencia (independiente de los precios).
  renderFearGreed();
  renderChart();
}

// ------------------------------------------------------------------ Modal añadir/editar
function openModal(index) {
  state.editIndex = index ?? -1;
  const editing = state.editIndex >= 0;
  $('#modalTitle').textContent = editing ? 'Editar moneda' : 'Añadir moneda';
  $('#modalMsg').textContent = '';
  const sym = $('#coinSymbol');
  const amt = $('#coinAmount');
  if (editing) {
    const h = state.holdings[state.editIndex];
    sym.value = h.symbol.toUpperCase();
    amt.value = String(h.amount).replace('.', ',');
  } else {
    sym.value = '';
    amt.value = '';
  }
  $('#coinModal').hidden = false;
  setTimeout(() => sym.focus(), 50);
}
function closeModal() { $('#coinModal').hidden = true; }

function saveModal() {
  const symbol = $('#coinSymbol').value.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  const amount = parseAmount($('#coinAmount').value);
  const msg = $('#modalMsg');
  if (!symbol) { msg.textContent = 'Escribe el símbolo de la moneda (ej. BTC).'; return; }
  if (!Number.isFinite(amount) || amount <= 0) { msg.textContent = 'Escribe una cantidad válida mayor que 0.'; return; }

  if (state.editIndex >= 0) {
    state.holdings[state.editIndex] = { symbol, amount };
  } else {
    // Si ya existe la moneda, sumamos en vez de duplicar.
    const existing = state.holdings.findIndex(h => h.symbol.toUpperCase() === symbol);
    if (existing >= 0) state.holdings[existing].amount = amount;
    else state.holdings.push({ symbol, amount });
  }
  save(LS.holdings, state.holdings);
  closeModal();
  render();
  refreshAll();
}

function deleteHolding(index) {
  const h = state.holdings[index];
  if (!h) return;
  if (!confirm(`¿Eliminar ${h.symbol.toUpperCase()} de tu cartera?`)) return;
  state.holdings.splice(index, 1);
  save(LS.holdings, state.holdings);
  render();
  refreshAll();
}

function loadSample() {
  state.holdings = [
    { symbol: 'BTC', amount: 0.05 },
    { symbol: 'ETH', amount: 0.8 },
    { symbol: 'SOL', amount: 12 },
    { symbol: 'USDT', amount: 250 },
  ];
  save(LS.holdings, state.holdings);
  render();
  refreshAll();
}

// ------------------------------------------------------------------ Navegación
function showView(name) {
  $all('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  $all('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  if (name === 'tendencia') { renderFearGreed(); renderChart(); }
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ Eventos
function wireEvents() {
  $all('.tab').forEach(t => t.addEventListener('click', () => showView(t.dataset.view)));

  $('#refreshBtn').addEventListener('click', refreshAll);

  $('#currencyToggle').addEventListener('click', () => {
    state.fiat = state.fiat === 'USD' ? 'EUR' : 'USD';
    save(LS.fiat, state.fiat);
    render();
    refreshAll();
  });
  $('#fiatSelect').addEventListener('change', (e) => {
    state.fiat = e.target.value;
    save(LS.fiat, state.fiat);
    render();
    refreshAll();
  });

  // Añadir moneda (varios botones).
  ['#addCoinBtn', '#addCoinBtn2', '#addFromEmpty'].forEach(sel => {
    const el = $(sel); if (el) el.addEventListener('click', () => openModal(-1));
  });
  $('#sampleFromEmpty').addEventListener('click', loadSample);

  // Lista de gestión: editar / eliminar (delegación).
  $('#manageList').addEventListener('click', (e) => {
    const ed = e.target.closest('[data-edit]');
    const dl = e.target.closest('[data-del]');
    if (ed) openModal(parseInt(ed.dataset.edit, 10));
    if (dl) deleteHolding(parseInt(dl.dataset.del, 10));
  });

  // Modal.
  $('#modalSave').addEventListener('click', saveModal);
  $('#modalCancel').addEventListener('click', closeModal);
  $('#coinModal').addEventListener('click', (e) => { if (e.target.id === 'coinModal') closeModal(); });
  $('#coinAmount').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveModal(); });

  // Gráfica: rango y moneda.
  $('#rangeTabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-range]');
    if (!b) return;
    state.chartRange = parseInt(b.dataset.range, 10);
    $all('#rangeTabs button').forEach(x => x.classList.toggle('active', x === b));
    renderChart();
  });
  $('#coinSelect').addEventListener('change', (e) => {
    state.chartSymbol = e.target.value;
    renderChart();
  });

  // Refrescar al volver a la app (p.ej. desde segundo plano).
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshAll();
  });
}

// ------------------------------------------------------------------ Arranque
function init() {
  wireEvents();
  render();
  refreshAll();
  // Auto-refresco cada 60 s.
  setInterval(() => { if (document.visibilityState === 'visible') refreshAll(); }, 60000);

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('sw.js').catch(() => {});
    });
  }
}

document.addEventListener('DOMContentLoaded', init);
