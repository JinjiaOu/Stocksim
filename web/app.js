// StockSim front end — plain ES module, no build step.
import { BY_SYMBOL, COMPANIES, SECTORS } from './companies.js';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state = {
  accounts: [], accountId: null, account: null,
  symbol: null, range: null, side: 'buy', type: 'market',
  quote: null, portfolio: null, watch: [], status: null, tab: 'positions', busy: false,
  list: 'watch', sector: '全部', companyQuotes: {},
};

// ---------- formatting ----------
const fmtUsd = (v) => v == null ? '—' : v.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const fmtPx = (v) => v == null ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: v < 10 ? 4 : 2 });
const fmtPct = (v) => v == null || !isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;
const fmtSigned = (v) => v == null ? '—' : `${v >= 0 ? '+' : '−'}${fmtUsd(Math.abs(v))}`;
const cls = (v) => v > 0 ? 'up' : v < 0 ? 'down' : '';
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const etFmt = new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const etFmtYear = new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const fmtTime = (iso) => iso ? `${(isReplay() ? etFmtYear : etFmt).format(new Date(iso))}` : '—';
const SIDE = { buy: '买入', sell: '卖出' };
const TYPE = { market: '市价', limit: '限价' };
const STATUS = { open: '待成交', filled: '已成交', cancelled: '已撤销', expired: '已过期', rejected: '已拒绝' };
const PHASE = { open: '开盘', close: '收盘' };

const isReplay = () => state.account?.mode === 'replay';
const withAcct = (path) => `${path}${path.includes('?') ? '&' : '?'}account_id=${state.accountId}`;
const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const d = body?.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((e) => e.msg).join('；') : `请求失败（${res.status}）`);
  }
  return body;
}

let toastTimer;
function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 5000 : 3000);
}

// ---------- chart ----------
const chart = LightweightCharts.createChart($('#chart'), {
  autoSize: true,
  layout: { background: { color: 'transparent' }, textColor: '#8b95a3' },
  grid: { vertLines: { color: '#1f262f' }, horzLines: { color: '#1f262f' } },
  rightPriceScale: { borderColor: '#2a323d' },
  timeScale: { borderColor: '#2a323d', timeVisible: true, secondsVisible: false },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
});
const candles = chart.addCandlestickSeries({
  upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350',
});
candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
const volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
let costLine = null;

const etParts = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
// Lightweight Charts renders timestamps as UTC; shift them so the axis reads New York wall time.
function etWall(sec) {
  const p = Object.fromEntries(etParts.formatToParts(new Date(sec * 1000)).map((x) => [x.type, x.value]));
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) / 1000;
}
const etDate = (iso) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date(iso));

function chartMsg(text) {
  let el = $('#chart .chart-msg');
  if (!text) { el?.remove(); return; }
  if (!el) { el = document.createElement('div'); el.className = 'chart-msg'; $('#chart').append(el); }
  el.textContent = text;
}

let chartToken = 0;
async function loadChart() {
  const token = ++chartToken;
  if (!state.symbol) return;
  const sym = state.symbol;
  try {
    const [bars, fills] = await Promise.all([
      api(withAcct(`/api/market/bars/${encodeURIComponent(sym)}?range=${state.range}`)),
      api(withAcct('/api/fills?limit=500')),
    ]);
    if (token !== chartToken) return;
    const intraday = typeof bars[0]?.time === 'number';
    const data = bars.map((b) => ({ ...b, time: intraday ? etWall(b.time) : b.time }));
    candles.setData(data.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volume.setData(data.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? 'rgba(38,166,154,.35)' : 'rgba(239,83,80,.35)' })));
    candles.setMarkers(fillMarkers(fills.filter((f) => f.symbol === sym), data, intraday));
    chartMsg(data.length ? '' : '暂无 K 线数据');
    chart.timeScale().fitContent();
  } catch (e) {
    if (token !== chartToken) return;
    candles.setData([]); volume.setData([]);
    chartMsg(e.message);
  }
}

function fillMarkers(fills, data, intraday) {
  if (!data.length) return [];
  const times = data.map((b) => b.time);
  const first = times[0];
  const markers = [];
  for (const f of fills) {
    let t = intraday ? Math.floor(etWall(Date.parse(f.executed_at) / 1000) / 60) * 60 : etDate(f.executed_at);
    if (t < first) continue;
    // snap to the last bar at or before the fill time
    let snapped = times[0];
    for (const x of times) { if (x <= t) snapped = x; else break; }
    markers.push({
      time: snapped, position: f.side === 'buy' ? 'belowBar' : 'aboveBar',
      color: f.side === 'buy' ? '#26a69a' : '#ef5350', shape: f.side === 'buy' ? 'arrowUp' : 'arrowDown',
      text: `${f.side === 'buy' ? 'B' : 'S'} ${f.qty}@${fmtPx(f.price)}`,
    });
  }
  return markers.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

function updateCostLine() {
  if (costLine) { candles.removePriceLine(costLine); costLine = null; }
  const pos = state.portfolio?.positions.find((p) => p.symbol === state.symbol);
  if (pos) {
    costLine = candles.createPriceLine({ price: pos.avg_cost, color: '#f0b429', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '成本' });
  }
}

function renderRangeTabs() {
  const ranges = isReplay() ? [['1y', '1年'], ['5y', '5年']] : [['1d', '当日'], ['1y', '1年'], ['5y', '5年']];
  if (!ranges.some(([r]) => r === state.range)) state.range = isReplay() ? '1y' : '1d';
  $('#range-tabs').innerHTML = ranges.map(([r, label]) =>
    `<button data-range="${r}" class="${r === state.range ? 'active' : ''}">${label}</button>`).join('');
}

// ---------- data loaders ----------
async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  const saved = Number(store.get('accountId', 0));
  if (!state.accounts.some((a) => a.id === state.accountId)) {
    state.accountId = state.accounts.some((a) => a.id === saved) ? saved : state.accounts[0].id;
  }
  state.account = state.accounts.find((a) => a.id === state.accountId);
  $('#account-select').innerHTML = state.accounts.map((a) => {
    const label = a.mode === 'live' ? '实时账户' : `回放 · ${a.start_date} 开局${a.status === 'finished' ? '（已结束）' : ''}`;
    return `<option value="${a.id}" ${a.id === state.accountId ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');
  $('#replay-controls').hidden = !isReplay();
  $('#reset-btn').hidden = isReplay();
  renderRangeTabs();
}

async function loadStatus() {
  const s = await api(withAcct('/api/market/status'));
  state.status = s;
  const pill = $('#market-pill');
  if (s.mode === 'replay') {
    pill.className = 'pill replay';
    pill.textContent = s.status === 'active' ? `回放 ${s.sim_date} ${PHASE[s.sim_phase]}` : `回放已结束 · ${s.sim_date}`;
    $$('#replay-controls button').forEach((b) => { b.disabled = s.status !== 'active' || state.busy; });
  } else {
    pill.className = `pill ${s.open ? 'open' : 'closed'}`;
    pill.textContent = s.open ? '交易中' : s.reason;
    pill.title = `下单归属交易日 ${s.session_date}`;
  }
}

async function loadPortfolio() {
  const p = await api(withAcct('/api/portfolio'));
  state.portfolio = p;
  const unrealized = p.positions.reduce((s, x) => s + (x.unrealized ?? 0), 0);
  const stats = [
    ['big', '总资产', `${fmtUsd(p.equity)} <span class="${cls(p.total_return)}" style="font-size:14px">${fmtPct(p.total_return)}</span>`],
    ['', '现金', fmtUsd(p.cash)],
    ['', '可用资金', fmtUsd(p.available_cash)],
    ['', '持仓市值', fmtUsd(p.market_value)],
    ['', '挂单冻结', fmtUsd(p.reserved)],
    ['', '未实现盈亏', `<span class="${cls(unrealized)}">${fmtSigned(unrealized)}</span>`],
    ['', '已实现盈亏', `<span class="${cls(p.realized)}">${fmtSigned(p.realized)}</span>`],
  ];
  if (p.benchmark_return != null) {
    stats.push(['', '同期 SPY', `<span class="${cls(p.benchmark_return)}">${fmtPct(p.benchmark_return)}</span>`]);
    const alpha = p.total_return - p.benchmark_return;
    stats.push(['', '跑赢大盘', `<span class="${cls(alpha)}">${fmtPct(alpha)}</span>`]);
  }
  $('#summary').innerHTML = stats.map(([k, label, v]) =>
    `<div class="stat ${k}"><div class="label">${label}</div><div class="value">${v}</div></div>`).join('')
    + (p.unpriced_symbols.length ? `<div class="stat big small muted">无法估值：${esc(p.unpriced_symbols.join(', '))}</div>` : '');
  updateCostLine();
}

async function loadWatchlist() {
  state.watch = await api(withAcct('/api/watchlist'));
  const el = $('#watchlist');
  if (!state.watch.length) {
    el.innerHTML = '<li class="empty">在"公司列表"里或搜索股票后点 ☆ 加入自选</li>';
  } else {
    el.innerHTML = state.watch.map(({ symbol, quote: q }) => quoteRow(symbol, q)).join('');
  }
  $('#watch-toggle').textContent = isWatched(state.symbol) ? '★' : '☆';
  if (state.list === 'companies') renderCompanies();
}

const isWatched = (sym) => state.watch.some((w) => w.symbol === sym);

function quoteRow(symbol, q, withStar = false) {
  const chg = q?.prev_close ? q.price / q.prev_close - 1 : null;
  const name = BY_SYMBOL[symbol]?.cn;
  const star = withStar ? `<button class="star" data-star="${esc(symbol)}" title="加入/移出自选">${isWatched(symbol) ? '★' : '☆'}</button>` : '';
  return `<li data-symbol="${esc(symbol)}" class="${symbol === state.symbol ? 'active' : ''}"
      title="${esc(BY_SYMBOL[symbol]?.desc ?? '')}">${star}
    <span class="who"><span class="sym">${esc(symbol)}</span>${name && name !== symbol ? `<span class="name">${esc(name)}</span>` : ''}</span>
    <span class="px">${q ? fmtPx(q.price) : ''}</span>
    <span class="chg ${cls(chg)}">${q ? fmtPct(chg) : ''}</span></li>`;
}

// ---------- company list ----------
function renderCompanies() {
  $('#sector-chips').innerHTML = ['全部', ...SECTORS].map((s) =>
    `<button data-sector="${esc(s)}" class="${s === state.sector ? 'active' : ''}">${esc(s)}</button>`).join('');
  const f = $('#company-filter').value.trim().toLowerCase();
  const rows = COMPANIES.filter((c) => (state.sector === '全部' || c.sector === state.sector)
    && (!f || c.symbol.toLowerCase().includes(f) || c.cn.toLowerCase().includes(f) || c.en.toLowerCase().includes(f)));
  let html = '';
  let last = null;
  for (const c of rows) {
    if (c.sector !== last && state.sector === '全部') html += `<li class="sector-head">${esc(c.sector)}</li>`;
    last = c.sector;
    html += quoteRow(c.symbol, state.companyQuotes[c.symbol], true);
  }
  $('#company-list').innerHTML = html || '<li class="empty">没有匹配的公司（可用上方搜索框搜索全部美股）</li>';
}

let companyQuotesAt = 0;
async function loadCompanyQuotes(force = false) {
  if (!force && Date.now() - companyQuotesAt < 25000) return;
  companyQuotesAt = Date.now();
  try {
    const symbols = COMPANIES.map((c) => c.symbol).join(',');
    state.companyQuotes = await api(withAcct(`/api/market/quotes?symbols=${encodeURIComponent(symbols)}`));
  } catch { state.companyQuotes = {}; }
  if (state.list === 'companies') renderCompanies();
}

function showList(list) {
  state.list = list;
  store.set('list', list);
  $$('#list-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.list === list));
  $('#watchlist').hidden = list !== 'watch';
  $('#companies').hidden = list !== 'companies';
  if (list === 'companies') { renderCompanies(); loadCompanyQuotes(); }
}

async function toggleWatch(sym) {
  try {
    await api(withAcct(`/api/watchlist/${encodeURIComponent(sym)}`), { method: isWatched(sym) ? 'DELETE' : 'POST' });
    await loadWatchlist();
  } catch (e) { toast(e.message, 'error'); }
}

async function loadQuote() {
  if (!state.symbol) return;
  const sym = state.symbol;
  $('#q-symbol').textContent = sym;
  const info = BY_SYMBOL[sym];
  $('#q-desc').textContent = info ? `${info.cn}（${info.en}）· ${info.sector} · ${info.desc}` : '';
  try {
    const q = await api(withAcct(`/api/market/quote/${encodeURIComponent(sym)}`));
    if (sym !== state.symbol) return;
    state.quote = q;
    $('#q-price').textContent = fmtPx(q.price);
    const chg = q.prev_close ? q.price - q.prev_close : null;
    $('#q-change').className = `q-change ${cls(chg)}`;
    $('#q-change').textContent = chg == null ? '' : `${chg >= 0 ? '+' : ''}${fmtPx(chg)} (${fmtPct(chg / q.prev_close)})`;
    const meta = [];
    if (isReplay()) {
      meta.push(`${q.as_of.slice(0, 10)} ${state.account.sim_phase === 'open' ? '开盘价' : '收盘价'}`);
    } else {
      const age = q.age_seconds;
      const ageText = age < 120 ? '最新' : age < 3600 ? `${Math.round(age / 60)} 分钟前` : `${Math.round(age / 3600)} 小时前`;
      meta.push(`报价时间 ${fmtTime(q.as_of)} ET（${ageText}）`);
    }
    if (!q.tradable && q.reason) meta.push(`<span class="stale">${esc(q.reason)}</span>`);
    $('#q-meta').innerHTML = meta.join(' · ');
  } catch (e) {
    if (sym !== state.symbol) return;
    state.quote = null;
    $('#q-price').textContent = '—';
    $('#q-change').textContent = '';
    $('#q-meta').innerHTML = `<span class="stale">${esc(e.message)}</span>`;
  }
  renderTicket();
}

// ---------- bottom tables ----------
function table(headers, rows, empty) {
  const head = `<tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr>`;
  const body = rows.length ? rows.join('') : `<tr><td class="empty-row" colspan="${headers.length}">${empty}</td></tr>`;
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}
const symLink = (s) => `<span class="link" data-symbol="${esc(s)}">${esc(s)}</span>`;

async function loadTab() {
  const tab = state.tab;
  let html = '';
  if (tab === 'positions') {
    const p = state.portfolio ?? await api(withAcct('/api/portfolio'));
    html = table(['代码', '数量', '均价', '现价', '市值', '未实现盈亏', '已实现', '占比', ''],
      p.positions.map((x) => {
        const pct = x.unrealized != null ? x.unrealized / x.cost : null;
        return `<tr><td>${symLink(x.symbol)}</td><td>${x.qty}${x.reserved_qty ? `<span class="muted small">（冻结 ${x.reserved_qty}）</span>` : ''}</td>
          <td>${fmtPx(x.avg_cost)}</td><td>${fmtPx(x.price)}</td><td>${fmtUsd(x.market_value)}</td>
          <td class="${cls(x.unrealized)}">${fmtSigned(x.unrealized)} (${fmtPct(pct)})</td>
          <td class="${cls(x.realized)}">${fmtSigned(x.realized)}</td>
          <td>${x.market_value != null && p.equity ? fmtPct(x.market_value / p.equity).replace('+', '') : '—'}</td>
          <td><button class="small" data-close="${esc(x.symbol)}">平仓</button></td></tr>`;
      }), '暂无持仓');
  } else if (tab === 'open') {
    const orders = await api(withAcct('/api/orders?status=open'));
    html = table(['代码', '方向', '类型', '数量', '限价', '冻结', '交易日', '下单时间', ''],
      orders.map((o) => `<tr><td>${symLink(o.symbol)}</td><td class="${o.side === 'buy' ? 'up' : 'down'}">${SIDE[o.side]}</td>
        <td>${TYPE[o.type]}</td><td>${o.qty}</td><td>${fmtPx(o.limit_price)}</td>
        <td>${o.side === 'buy' ? fmtUsd(o.reserved) : `${o.qty} 股`}</td><td>${o.trade_date}</td><td>${fmtTime(o.placed_at)}</td>
        <td><button class="small" data-cancel="${o.id}">撤单</button></td></tr>`), '暂无挂单');
  } else if (tab === 'orders') {
    const orders = await api(withAcct('/api/orders?limit=200'));
    html = table(['时间', '代码', '方向', '类型', '数量', '限价', '成交价', '状态', '说明'],
      orders.map((o) => `<tr><td>${fmtTime(o.placed_at)}</td><td>${symLink(o.symbol)}</td>
        <td class="${o.side === 'buy' ? 'up' : 'down'}">${SIDE[o.side]}</td><td>${TYPE[o.type]}</td><td>${o.qty}</td>
        <td>${o.limit_price ? fmtPx(o.limit_price) : '—'}</td><td>${o.fill_price ? fmtPx(o.fill_price) : '—'}</td>
        <td><span class="tag ${o.status}">${STATUS[o.status]}</span></td><td class="muted">${esc(o.reason ?? '')}</td></tr>`), '暂无订单');
  } else {
    const fills = await api(withAcct('/api/fills?limit=200'));
    html = table(['成交时间', '代码', '方向', '数量', '成交价', '金额', '费用', ''],
      fills.map((f) => `<tr><td>${fmtTime(f.executed_at)}</td><td>${symLink(f.symbol)}</td>
        <td class="${f.side === 'buy' ? 'up' : 'down'}">${SIDE[f.side]}</td><td>${f.qty}</td><td>${fmtPx(f.price)}</td>
        <td>${fmtUsd(f.amount)}</td><td>${fmtUsd(f.fees)}</td><td class="muted small">${f.estimated ? '模拟估算' : ''}</td></tr>`), '暂无成交');
  }
  if (tab === state.tab) $('#tab-body').innerHTML = html;
}

// ---------- order ticket ----------
const slip = () => (state.account?.slippage_bps ?? 0) / 10000;

function ticketNumbers() {
  const qty = Number($('#t-qty').value);
  const limit = Number($('#t-limit').value);
  const ref = state.type === 'limit' ? limit : state.quote?.price;
  const px = ref ? (state.type === 'market' ? ref * (state.side === 'buy' ? 1 + slip() : 1 - slip()) : ref) : null;
  const pos = state.portfolio?.positions.find((p) => p.symbol === state.symbol);
  const sellable = pos ? pos.qty - pos.reserved_qty : 0;
  return { qty, limit, px, amount: px && qty ? px * qty : null, sellable, cash: state.portfolio?.available_cash ?? 0 };
}

function renderTicket() {
  $('#t-symbol').value = state.symbol ?? '';
  $$('#side-seg button').forEach((b) => b.classList.toggle('active', b.dataset.side === state.side));
  $$('#type-seg button').forEach((b) => b.classList.toggle('active', b.dataset.type === state.type));
  $('#t-limit-wrap').hidden = state.type !== 'limit';
  const btn = $('#t-submit');
  btn.className = `primary ${state.side}`;
  btn.textContent = `${SIDE[state.side]} ${state.symbol ?? ''}`;

  const n = ticketNumbers();
  const lines = [];
  let problem = null;
  if (n.amount != null) lines.push(`预计${state.side === 'buy' ? '花费' : '收入'} <b>${fmtUsd(n.amount)}</b>${state.type === 'market' ? `（含 ${state.account.slippage_bps}bp 滑点）` : ''}`);
  if (state.side === 'buy') lines.push(`可用资金 <b>${fmtUsd(n.cash)}</b>`);
  else lines.push(`可卖 <b>${n.sellable}</b> 股`);

  if (!state.symbol) problem = '先选择一只股票';
  else if (state.status?.mode === 'replay' && state.status.status !== 'active') problem = '回放已结束';
  else if (!Number.isInteger(n.qty) || n.qty <= 0) problem = '数量必须是正整数';
  else if (state.type === 'limit' && !(n.limit > 0)) problem = '请输入限价';
  else if (state.type === 'market' && !state.quote) problem = '暂无行情';
  else if (state.type === 'market' && !state.quote.tradable) problem = `现在不能市价成交：${state.quote.reason}`;
  else if (state.side === 'buy' && n.amount > n.cash) problem = '可用资金不足';
  else if (state.side === 'sell' && n.qty > n.sellable) problem = '可卖股数不足';
  if (problem) lines.push(`<span class="warn">${esc(problem)}</span>`);
  else if (state.type === 'limit' && state.quote && !state.quote.tradable) lines.push('<span class="warn">当前不可成交，挂单将在可交易时检查</span>');

  $('#t-est').innerHTML = lines.join('<br>');
  btn.disabled = !!problem || state.busy;
  $('#t-note').textContent = isReplay()
    ? '当日有效（DAY）。收盘时下的单属于下一交易日。成交为模拟估算。'
    : '当日有效（DAY）。成交为模拟估算，不代表真实可成交价格。';
}

// Resolve on the form's submit (which carries the clicked button) rather than the dialog's
// async `close` event, which some embedded browsers deliver late or not at all. Esc → false.
function openDialog(dlg) {
  dlg.showModal();
  return new Promise((resolve) => {
    const form = dlg.querySelector('form');
    const done = (ok) => {
      form.removeEventListener('submit', onSubmit);
      dlg.removeEventListener('cancel', onCancel);
      if (dlg.open) dlg.close();
      resolve(ok);
    };
    const onSubmit = (e) => { e.preventDefault(); done(e.submitter?.value === 'ok'); };
    const onCancel = (e) => { e.preventDefault(); done(false); };
    form.addEventListener('submit', onSubmit);
    dlg.addEventListener('cancel', onCancel);
  });
}

function confirmDialog(html) {
  $('#confirm-body').innerHTML = html;
  return openDialog($('#confirm-dialog'));
}

async function submitOrder() {
  const n = ticketNumbers();
  const body = { account_id: state.accountId, symbol: state.symbol, side: state.side, type: state.type, qty: n.qty };
  if (state.type === 'limit') body.limit_price = Number(n.limit.toFixed(4));
  const ok = await confirmDialog(`<div class="confirm-grid">
    <span>股票</span><span><b>${esc(state.symbol)}</b></span>
    <span>方向</span><span class="${state.side === 'buy' ? 'up' : 'down'}">${SIDE[state.side]}</span>
    <span>类型</span><span>${TYPE[state.type]}${state.type === 'limit' ? ` @ ${fmtPx(n.limit)}` : ''}（当日有效）</span>
    <span>数量</span><span>${n.qty} 股</span>
    <span>预计金额</span><span>${fmtUsd(n.amount)}</span>
    <span>账户</span><span>${isReplay() ? `回放（${state.account.sim_date} ${PHASE[state.account.sim_phase]}）` : '实时账户'}</span>
  </div>`);
  if (!ok) return;
  state.busy = true;
  renderTicket();
  try {
    const o = await api('/api/orders', { method: 'POST', body: JSON.stringify(body) });
    if (o.status === 'filled') toast(`已成交：${SIDE[o.side]} ${o.qty} 股 ${o.symbol} @ ${fmtPx(o.fill_price)}`, 'ok');
    else if (o.status === 'open') toast(`挂单已提交：${SIDE[o.side]} ${o.qty} 股 ${o.symbol} @ ${fmtPx(o.limit_price)}`, 'ok');
    else toast(`订单被拒绝：${o.reason}`, 'error');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    state.busy = false;
  }
  await refreshAll();
  loadChart();
}

// ---------- actions ----------
function selectSymbol(sym) {
  sym = sym.trim().toUpperCase();
  if (!sym) return;
  state.symbol = sym;
  state.quote = null;
  store.set('symbol', sym);
  $('#q-price').textContent = '…';
  $('#q-change').textContent = '';
  $('#q-meta').textContent = '';
  $$('#watchlist li, #company-list li').forEach((li) => li.classList.toggle('active', li.dataset.symbol === sym));
  $('#watch-toggle').textContent = isWatched(sym) ? '★' : '☆';
  renderTicket();
  updateCostLine();
  loadQuote();
  loadChart();
}

async function switchAccount(id) {
  state.accountId = id;
  store.set('accountId', String(id));
  state.portfolio = null;
  state.companyQuotes = {};
  await loadAccounts();
  await refreshAll();
  loadChart();
  if (state.list === 'companies') loadCompanyQuotes(true);
}

async function refreshAll() {
  try {
    if (isReplay()) await loadAccounts(); // sim_date / phase may have moved
    await Promise.all([loadStatus(), loadPortfolio(), loadWatchlist()]);
    await Promise.all([loadQuote(), loadTab()]);
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function advance(step) {
  state.busy = true;
  $$('#replay-controls button').forEach((b) => { b.disabled = true; });
  try {
    const ev = await api('/api/replay/advance', { method: 'POST', body: JSON.stringify({ account_id: state.accountId, step }) });
    const bits = [];
    if (ev.filled) bits.push(`成交 ${ev.filled} 笔`);
    if (ev.expired) bits.push(`过期 ${ev.expired} 笔`);
    if (ev.finished) bits.push('已到达最新数据，回放结束');
    if (bits.length) toast(bits.join('，'), ev.finished ? '' : 'ok');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    state.busy = false;
  }
  await refreshAll();
  loadChart();
}

// ---------- search ----------
let searchTimer, searchSeq = 0;
function hideResults() { $('#search-results').hidden = true; }

$('#search-input').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (!q) { hideResults(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const results = await api(`/api/market/search?q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq) return;
      const ul = $('#search-results');
      ul.innerHTML = results.length
        ? results.map((r) => `<li data-symbol="${esc(r.symbol)}"><span class="sym">${esc(r.symbol)}</span><span class="name">${esc(r.name)} · ${esc(r.exchange)}</span></li>`).join('')
        : '<li class="muted">无结果（可直接回车打开代码）</li>';
      ul.hidden = false;
    } catch { /* ignore search errors */ }
  }, 300);
});
$('#search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { selectSymbol(e.target.value); e.target.value = ''; hideResults(); }
  if (e.key === 'Escape') hideResults();
});
$('#search-results').addEventListener('mousedown', (e) => {
  const li = e.target.closest('li[data-symbol]');
  if (!li) return;
  selectSymbol(li.dataset.symbol);
  $('#search-input').value = '';
  hideResults();
});
$('#search-input').addEventListener('blur', () => setTimeout(hideResults, 150));

// ---------- wiring ----------
$('#watchlist').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-symbol]');
  if (li) selectSymbol(li.dataset.symbol);
});
$('#watch-toggle').addEventListener('click', () => { if (state.symbol) toggleWatch(state.symbol); });
$('#list-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-list]');
  if (b) showList(b.dataset.list);
});
$('#sector-chips').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-sector]');
  if (!b) return;
  state.sector = b.dataset.sector;
  renderCompanies();
});
$('#company-filter').addEventListener('input', renderCompanies);
$('#company-list').addEventListener('click', (e) => {
  const star = e.target.closest('[data-star]');
  if (star) { toggleWatch(star.dataset.star); return; }
  const li = e.target.closest('li[data-symbol]');
  if (li) selectSymbol(li.dataset.symbol);
});
$('#range-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-range]');
  if (!b) return;
  state.range = b.dataset.range;
  store.set('range', state.range);
  renderRangeTabs();
  loadChart();
});
$('#bottom-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (!b) return;
  state.tab = b.dataset.tab;
  $$('#bottom-tabs button').forEach((x) => x.classList.toggle('active', x === b));
  loadTab().catch((err) => toast(err.message, 'error'));
});
$('#tab-body').addEventListener('click', async (e) => {
  const link = e.target.closest('[data-symbol]');
  if (link) { selectSymbol(link.dataset.symbol); return; }
  const cancel = e.target.closest('[data-cancel]');
  if (cancel) {
    cancel.disabled = true;
    try {
      await api(withAcct(`/api/orders/${cancel.dataset.cancel}/cancel`), { method: 'POST' });
      toast('已撤单', 'ok');
    } catch (err) { toast(err.message, 'error'); }
    await refreshAll();
    return;
  }
  const close = e.target.closest('[data-close]');
  if (close) {
    const pos = state.portfolio.positions.find((p) => p.symbol === close.dataset.close);
    selectSymbol(pos.symbol);
    state.side = 'sell';
    state.type = 'market';
    $('#t-qty').value = pos.qty - pos.reserved_qty;
    renderTicket();
  }
});
$('#side-seg').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-side]');
  if (b) { state.side = b.dataset.side; renderTicket(); }
});
$('#type-seg').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-type]');
  if (!b) return;
  state.type = b.dataset.type;
  if (state.type === 'limit' && !$('#t-limit').value && state.quote) $('#t-limit').value = state.quote.price.toFixed(2);
  renderTicket();
});
$('#qty-shortcuts').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-frac]');
  if (!b) return;
  const frac = Number(b.dataset.frac);
  const n = ticketNumbers();
  let max = 0;
  if (state.side === 'sell') max = n.sellable;
  else if (n.px) max = Math.floor(n.cash / (n.px * (1 + 1e-9)) - 1e-9);
  $('#t-qty').value = Math.max(0, Math.floor(max * frac));
  renderTicket();
});
$('#t-qty').addEventListener('input', renderTicket);
$('#t-limit').addEventListener('input', renderTicket);
$('#t-submit').addEventListener('click', submitOrder);
$('#account-select').addEventListener('change', (e) => switchAccount(Number(e.target.value)));
$('#replay-controls').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-step]');
  if (b) advance(b.dataset.step);
});
$('#new-replay-btn').addEventListener('click', async () => {
  if (!(await openDialog($('#replay-dialog')))) return;
  const body = { initial: Number($('#r-initial').value) || 100000 };
  if ($('#r-start').value) body.start_date = $('#r-start').value;
  toast('正在准备历史数据…');
  try {
    const acct = await api('/api/replay/new', { method: 'POST', body: JSON.stringify(body) });
    toast(`回放开始：${acct.start_date}`, 'ok');
    await switchAccount(acct.id);
  } catch (e) { toast(e.message, 'error'); }
});
$('#reset-btn').addEventListener('click', async () => {
  if (!confirm('重置实时账户？所有订单、成交和持仓将被清除，资金恢复初始值。')) return;
  try {
    await api(withAcct('/api/account/reset'), { method: 'POST' });
    toast('账户已重置', 'ok');
  } catch (e) { toast(e.message, 'error'); }
  await refreshAll();
  loadChart();
});

// ---------- polling (live account only; replay only changes when you advance) ----------
let tick = 0;
setInterval(async () => {
  if (document.hidden || isReplay() || state.busy) return;
  tick += 1;
  await refreshAll();
  if (state.range === '1d' && tick % 6 === 0) loadChart();
  if (state.list === 'companies') loadCompanyQuotes();
}, 5000);

// ---------- boot ----------
(async function boot() {
  state.range = store.get('range', '1d');
  try {
    await loadAccounts();
  } catch (e) {
    toast(`无法连接本地服务：${e.message}`, 'error');
    return;
  }
  state.symbol = store.get('symbol', 'SPY');
  await refreshAll();
  selectSymbol(state.symbol);
  showList(store.get('list', 'watch') === 'companies' ? 'companies' : 'watch');
})();
