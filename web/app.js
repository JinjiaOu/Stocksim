// StockSim front end — plain ES module, no build step.
// Two display modes share all data and logic: "simple" (plain-language, fewer fields, buy by dollar
// amount) and "full". Static text swaps via data-simple attributes; dynamic text uses T(simple, full).
import { BY_SYMBOL, COMPANIES, SECTORS } from './companies.js';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  accounts: [], accountId: null, account: null,
  symbol: null, range: null, side: 'buy', type: 'market',
  quote: null, portfolio: null, watch: [], status: null, tab: 'positions', busy: false,
  list: 'watch', sector: '全部', companyQuotes: {},
  simple: store.get('mode', 'simple') !== 'full',
};

// ---------- formatting ----------
const T = (simple, full) => (state.simple ? simple : full);
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
const cnName = (sym) => BY_SYMBOL[sym]?.cn;

const isReplay = () => state.account?.mode === 'replay';
const isMargin = () => state.account?.account_type === 'margin';
const withAcct = (path) => `${path}${path.includes('?') ? '&' : '?'}account_id=${state.accountId}`;

// Server messages rewritten in plain words for simple mode.
const PLAIN = [
  [/^市场未开盘.*/, '现在休市。美股开市时间：美东时间周一到周五 9:30–16:00'],
  [/^无最新行情.*/, '今天可能是美股放假，或者暂时拿不到最新价格'],
  [/^行情过期$/, '价格不是最新的，暂时不能按这个价格买卖'],
  [/^价格异常跳变.*/, '价格突然变化太大，可能是数据出错，先暂停一下'],
  [/^该股票当日无行情.*/, '这只股票这一天没有交易（可能还没上市，或者暂停交易）'],
  [/^(\S+) 在 (\S+) 还没有交易数据.*/, '$1 在 $2 还没上市，那时候买不到'],
  [/^暂无该股票行情$/, '暂时拿不到这只股票的价格'],
  [/^可用资金不足$/, '钱不够'],
  [/^可用持仓不足（现金账户不能卖空，可卖 (\d+) 股）$/, '你手上只有 $1 股能卖。普通账户不能卖手上没有的股票'],
  [/^保证金不足（需要 (.+)，可用 (.+)）$/, '押金不够：这笔要押上你自己的钱 $1，你只剩 $2 可押。买少一点吧'],
  [/^无法给持仓估值.*/, '有的股票暂时拿不到价格，算不出你还能借多少，先不能开新的仓位'],
  [/^当日有效订单已过期$/, '当天没成交，单子自动作废了'],
  [/^强制平仓：低于维持保证金$/, '亏得太多，系统自动帮你平掉了'],
  [/^保证金不足，系统撤单$/, '亏得太多，系统自动撤掉了这个单子'],
  [/^该存档已结束，不能下单$/, '这局已经结束了，不能再买卖'],
  [/^回放已结束$/, '这局回放已经结束了'],
];
function plain(msg) {
  if (!state.simple || !msg) return msg;
  for (const [re, out] of PLAIN) if (re.test(msg)) return msg.replace(re, out);
  return msg;
}

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
  el.textContent = plain(msg);
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 6000 : 3500);
}

// ---------- display mode ----------
function applyMode() {
  document.body.classList.toggle('simple', state.simple);
  for (const el of $$('[data-simple]')) {
    if (el.dataset.full === undefined) el.dataset.full = el.textContent;
    el.textContent = state.simple ? el.dataset.simple : el.dataset.full;
  }
  for (const el of $$('[data-simple-placeholder]')) {
    if (el.dataset.fullPlaceholder === undefined) el.dataset.fullPlaceholder = el.placeholder;
    el.placeholder = state.simple ? el.dataset.simplePlaceholder : el.dataset.fullPlaceholder;
  }
  const btn = $('#mode-btn');
  btn.textContent = state.simple ? '简单模式 ✓' : '简单模式';
  btn.classList.toggle('on', state.simple);
  if (state.simple) {
    state.type = 'market';
    if (state.tab === 'open' || state.tab === 'orders') selectTab('positions');
  }
  volume.applyOptions({ visible: !state.simple });
}

async function toggleMode() {
  state.simple = !state.simple;
  store.set('mode', state.simple ? 'simple' : 'full');
  applyMode();
  $('#account-select').dataset.html = '';
  await refreshAll();
  updateCostLine();
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
    chartMsg(data.length ? '' : T('这只股票暂时没有价格走势图', '暂无 K 线数据'));
    chart.timeScale().fitContent();
  } catch (e) {
    if (token !== chartToken) return;
    candles.setData([]); volume.setData([]);
    chartMsg(plain(e.message));
  }
}

function fillMarkers(fills, data, intraday) {
  if (!data.length) return [];
  const times = data.map((b) => b.time);
  const first = times[0];
  const markers = [];
  for (const f of fills) {
    const t = intraday ? Math.floor(etWall(Date.parse(f.executed_at) / 1000) / 60) * 60 : etDate(f.executed_at);
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
    costLine = candles.createPriceLine({ price: pos.avg_cost, color: '#f0b429', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: T('我的成本', '成本') });
  }
}

function renderRangeTabs() {
  const ranges = isReplay() ? [['1y', '1年'], ['5y', '5年']] : [['1d', T('今天', '当日')], ['1y', '1年'], ['5y', '5年']];
  if (!ranges.some(([r]) => r === state.range)) state.range = isReplay() ? '1y' : '1d';
  $('#range-tabs').innerHTML = ranges.map(([r, label]) =>
    `<button data-range="${r}" class="${r === state.range ? 'active' : ''}">${label}</button>`).join('');
}

// ---------- accounts & status ----------
async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  const saved = Number(store.get('accountId', 0));
  if (!state.accounts.some((a) => a.id === state.accountId)) {
    state.accountId = state.accounts.some((a) => a.id === saved) ? saved : state.accounts[0].id;
  }
  state.account = state.accounts.find((a) => a.id === state.accountId);
  const options = state.accounts.map((a) =>
    `<option value="${a.id}" ${a.id === state.accountId ? 'selected' : ''}>${esc(accountLabel(a))}</option>`).join('');
  // Re-render only on change so polling doesn't close an open dropdown.
  if ($('#account-select').dataset.html !== options) {
    $('#account-select').innerHTML = options;
    $('#account-select').dataset.html = options;
  }
  const defaultLive = Math.min(...state.accounts.filter((a) => a.mode === 'live').map((a) => a.id));
  $('#replay-controls').hidden = !isReplay();
  $('#reset-btn').hidden = isReplay();
  $('#archive-btn').hidden = state.accountId === defaultLive;
  renderRangeTabs();
}

function accountLabel(a) {
  const type = a.account_type === 'margin'
    ? T(`杠杆账户 ${a.leverage}倍`, `保证金 ${a.leverage}x`)
    : T('普通账户', '现金');
  const status = { finished: '（已结束）', bust: T('（钱亏光了）', '（已爆仓）') }[a.status] ?? '';
  return a.mode === 'live' ? `实时 · ${type}${status}` : `回放 · ${a.start_date} · ${type}${status}`;
}

async function loadStatus() {
  const s = await api(withAcct('/api/market/status'));
  state.status = s;
  const pill = $('#market-pill');
  if (state.account?.status === 'bust') {
    pill.className = 'pill closed';
    pill.textContent = T('钱亏光了，这局结束', s.mode === 'replay' ? `已爆仓 · ${s.sim_date}` : '已爆仓');
    $$('#replay-controls button').forEach((b) => { b.disabled = true; });
  } else if (s.mode === 'replay') {
    pill.className = 'pill replay';
    pill.textContent = s.status === 'active'
      ? T(`现在是 ${s.sim_date} ${s.sim_phase === 'open' ? '早上开盘' : '下午收盘'}`, `回放 ${s.sim_date} ${PHASE[s.sim_phase]}`)
      : T(`这局已结束（${s.sim_date}）`, `回放已结束 · ${s.sim_date}`);
    $$('#replay-controls button').forEach((b) => { b.disabled = s.status !== 'active' || state.busy; });
  } else {
    pill.className = `pill ${s.open ? 'open' : 'closed'}`;
    pill.textContent = s.open ? T('开市中，可以买卖', '交易中') : T('休市中', s.reason);
    pill.title = state.simple && !s.open ? plain(s.reason) : `下单归属交易日 ${s.session_date}`;
  }
}

function renderMarginBanner(p) {
  const el = $('#margin-banner');
  const m = p.margin;
  let text = '', kind = '';
  if (state.account?.status === 'bust') {
    kind = 'danger';
    text = T(`你的钱亏光了（爆仓），只剩 ${fmtUsd(p.equity)}。${isReplay() ? '点"开新的一局"再来。' : '可以点"重新开始"。'}`,
      `账户已爆仓：净资产 ${fmtUsd(p.equity)}。${isReplay() ? '请新建一局回放。' : '可点"重置账户"重新开始。'}`);
  } else if (m?.margin_call) {
    kind = 'danger';
    text = T('危险！你亏得太多，已经跌破安全线。下次能交易的时候，系统会自动帮你把仓位平掉。',
      `保证金不足！净资产 ${fmtUsd(p.equity)} 低于维持保证金 ${fmtUsd(m.maintenance_requirement)}，下次可交易时将被强制平仓。`);
  } else if (m?.usage > 0.8) {
    kind = 'warn';
    text = T(`注意：风险已经到 ${(m.usage * 100).toFixed(0)}%。再亏 ${fmtUsd(m.maintenance_excess)}，系统就会自动帮你平仓。`,
      `接近强制平仓线：维持保证金已占净资产的 ${(m.usage * 100).toFixed(0)}%，还差 ${fmtUsd(m.maintenance_excess)} 就会被强平。`);
  }
  el.className = `banner ${kind}`;
  el.textContent = text;
  el.hidden = !text;
}

// ---------- summary ----------
const stat = (k, label, value, hint = '') =>
  `<div class="stat ${k}"><div class="label">${label}</div><div class="value">${value}</div>${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;

function usageMeter(usage, simple) {
  if (usage == null) return '<span class="down">—</span>';
  const pct = Math.min(usage, 1);
  const level = usage >= 0.8 ? 'high' : usage >= 0.5 ? 'mid' : '';
  const word = simple ? `（${usage >= 0.8 ? '很危险' : usage >= 0.5 ? '要小心' : '安全'}）` : '';
  return `<span class="${usage >= 0.8 ? 'down' : ''}">${(usage * 100).toFixed(0)}%${word}</span>
    ${simple ? '' : '<span class="muted small">（100% 即强制平仓）</span>'}
    <div class="meter ${level}"><div style="width:${(pct * 100).toFixed(1)}%"></div></div>`;
}

function simpleSummary(p) {
  const m = p.margin;
  const gain = p.equity - p.initial;
  const out = [
    stat('big', '我的钱', fmtUsd(p.equity),
      m ? '扣掉借来的钱和股票之后，真正属于你的' : '现金 + 手上股票按现在的价格值多少'),
    stat('', gain >= 0 ? '赚了' : '亏了', `<span class="${cls(gain)}">${fmtUsd(Math.abs(gain))}<br><small>${fmtPct(p.total_return)}</small></span>`,
      `和一开始的 ${fmtUsd(p.initial)} 比`),
  ];
  if (m) {
    out.push(stat('', '还能开多少仓位', fmtUsd(m.buying_power), '算上能借的钱，还能再买（或卖空）多少钱的股票'));
    if (m.debit > 0) out.push(stat('', '借来的钱', `<span class="down">${fmtUsd(m.debit)}</span>`, '买股票时借的，每年收 8% 利息'));
    if (m.short_value > 0) out.push(stat('', '借来卖掉的股票', fmtUsd(m.short_value), '现在把它们买回来还掉要花这么多，每年收 3% 费用'));
    out.push(stat('big', '风险', usageMeter(m.usage, true), '到 100% 系统会自动帮你平仓（强制卖出或买回），亏损就定死了'));
  } else {
    out.push(stat('', '能用的现金', fmtUsd(p.available_cash), '现在还能拿去买股票的钱'));
    out.push(stat('', '股票现在值', fmtUsd(p.market_value), '手上所有股票按现在的价格算'));
  }
  if (p.benchmark_return != null) {
    const alpha = p.total_return - p.benchmark_return;
    out.push(stat('', '如果全买大盘', `<span class="${cls(p.benchmark_return)}">${fmtPct(p.benchmark_return)}</span>`, '同一时间把钱全买 SPY 一直拿着的收益'));
    out.push(stat('', alpha >= 0 ? '比大盘多赚' : '比大盘少赚', `<span class="${cls(alpha)}">${fmtPct(Math.abs(alpha)).replace('+', '')}</span>`, '你的收益减去大盘的收益'));
  }
  return out.join('');
}

function fullSummary(p) {
  const unrealized = p.positions.reduce((s, x) => s + (x.unrealized ?? 0), 0);
  const m = p.margin;
  const ret = `<span class="${cls(p.total_return)}" style="font-size:14px">${fmtPct(p.total_return)}</span>`;
  const pnl = [
    stat('', '未实现盈亏', `<span class="${cls(unrealized)}">${fmtSigned(unrealized)}</span>`),
    stat('', '已实现盈亏', `<span class="${cls(p.realized)}">${fmtSigned(p.realized)}</span>`),
  ];
  const out = m ? [
    stat('big', '净资产', `${fmtUsd(p.equity)} ${ret}`),
    stat('', p.cash < 0 ? '借款' : '现金', p.cash < 0 ? `<span class="down">${fmtUsd(-p.cash)}</span>` : fmtUsd(p.cash)),
    stat('', '购买力', fmtUsd(m.buying_power)),
    stat('', '多头市值', fmtUsd(m.long_value)),
    stat('', '空头市值', fmtUsd(m.short_value)),
    stat('', '杠杆', `${m.leverage == null ? '—' : m.leverage.toFixed(2)}x <span class="muted small">/ 上限 ${m.leverage_max}x</span>`),
    stat('', '维持保证金', fmtUsd(m.maintenance_requirement)),
    stat('big', '保证金使用率', usageMeter(m.usage, false)),
    ...pnl,
  ] : [
    stat('big', '总资产', `${fmtUsd(p.equity)} ${ret}`),
    stat('', '现金', fmtUsd(p.cash)),
    stat('', '可用资金', fmtUsd(p.available_cash)),
    stat('', '持仓市值', fmtUsd(p.market_value)),
    stat('', '挂单冻结', fmtUsd(p.reserved)),
    ...pnl,
  ];
  if (p.benchmark_return != null) {
    const alpha = p.total_return - p.benchmark_return;
    out.push(stat('', '同期 SPY', `<span class="${cls(p.benchmark_return)}">${fmtPct(p.benchmark_return)}</span>`));
    out.push(stat('', '跑赢大盘', `<span class="${cls(alpha)}">${fmtPct(alpha)}</span>`));
  }
  return out.join('');
}

async function loadPortfolio() {
  const p = await api(withAcct('/api/portfolio'));
  state.portfolio = p;
  renderMarginBanner(p);
  $('#summary').innerHTML = (state.simple ? simpleSummary(p) : fullSummary(p))
    + (p.unpriced_symbols.length
      ? `<div class="stat big small muted">${T('暂时拿不到价格：', '无法估值：')}${esc(p.unpriced_symbols.join(', '))}</div>` : '');
  updateCostLine();
}

// ---------- watchlist & company list ----------
async function loadWatchlist() {
  state.watch = await api(withAcct('/api/watchlist'));
  const el = $('#watchlist');
  if (!state.watch.length) {
    el.innerHTML = `<li class="empty">${T('还没有关注的股票。去"公司列表"里点 ☆ 就能加进来', '在"公司列表"里或搜索股票后点 ☆ 加入自选')}</li>`;
  } else {
    el.innerHTML = state.watch.map(({ symbol, quote: q }) => quoteRow(symbol, q)).join('');
  }
  $('#watch-toggle').textContent = isWatched(state.symbol) ? '★' : '☆';
  if (state.list === 'companies') renderCompanies();
}

const isWatched = (sym) => state.watch.some((w) => w.symbol === sym);

function quoteRow(symbol, q, withStar = false) {
  const chg = q?.prev_close ? q.price / q.prev_close - 1 : null;
  const name = cnName(symbol);
  const star = withStar ? `<button class="star" data-star="${esc(symbol)}" title="加入/移出自选">${isWatched(symbol) ? '★' : '☆'}</button>` : '';
  return `<li data-symbol="${esc(symbol)}" class="${symbol === state.symbol ? 'active' : ''}"
      title="${esc(BY_SYMBOL[symbol]?.desc ?? '')}">${star}
    <span class="who"><span class="sym">${esc(symbol)}</span>${name && name !== symbol ? `<span class="name">${esc(name)}</span>` : ''}</span>
    <span class="px">${q ? fmtPx(q.price) : ''}</span>
    <span class="chg ${cls(chg)}">${q ? fmtPct(chg) : ''}</span></li>`;
}

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

// ---------- quote ----------
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
    $('#q-change').textContent = chg == null ? ''
      : `${T('比前一天 ', '')}${chg >= 0 ? '+' : ''}${fmtPx(chg)} (${fmtPct(chg / q.prev_close)})`;
    const meta = [];
    if (isReplay()) {
      const phase = state.account.sim_phase === 'open';
      meta.push(T(`这是 ${q.as_of.slice(0, 10)} ${phase ? '早上开盘' : '下午收盘'}时的价格`,
        `${q.as_of.slice(0, 10)} ${phase ? '开盘价' : '收盘价'}`));
    } else {
      const age = q.age_seconds;
      const ageText = age < 120 ? T('刚刚', '最新') : age < 3600 ? `${Math.round(age / 60)} 分钟前` : `${Math.round(age / 3600)} 小时前`;
      meta.push(T(`价格更新于美东时间 ${fmtTime(q.as_of)}（${ageText}）`, `报价时间 ${fmtTime(q.as_of)} ET（${ageText}）`));
    }
    if (!q.tradable && q.reason) meta.push(`<span class="stale">${esc(plain(q.reason))}</span>`);
    $('#q-meta').innerHTML = meta.join(' · ');
  } catch (e) {
    if (sym !== state.symbol) return;
    state.quote = null;
    $('#q-price').textContent = '—';
    $('#q-change').textContent = '';
    $('#q-meta').innerHTML = `<span class="stale">${esc(plain(e.message))}</span>`;
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
const symCell = (s) => `${symLink(s)}${state.simple && cnName(s) ? `<span class="sub">${esc(cnName(s))}</span>` : ''}`;

function positionsTable(p) {
  const margin = !!p.margin;
  if (state.simple) {
    return table(['股票', ...(margin ? ['方向'] : []), '股数', '平均成本', '现价', '现在值', '现在平掉能', ...(margin ? ['到这个价会被强平'] : []), ''],
      p.positions.map((x) => {
        const pct = x.unrealized != null ? x.unrealized / x.cost : null;
        const verb = x.unrealized == null ? '' : x.unrealized >= 0 ? '赚 ' : '亏 ';
        return `<tr><td>${symCell(x.symbol)}</td>
          ${margin ? `<td><span class="tag ${x.side}">${x.side === 'long' ? '买涨' : '卖空（赌跌）'}</span></td>` : ''}
          <td>${Math.abs(x.qty)}</td><td>${fmtPx(x.avg_cost)}</td><td>${fmtPx(x.price)}</td>
          <td>${fmtUsd(x.market_value == null ? null : Math.abs(x.market_value))}</td>
          <td class="${cls(x.unrealized)}">${verb}${fmtUsd(x.unrealized == null ? null : Math.abs(x.unrealized))}<br><small>${fmtPct(pct)}</small></td>
          ${margin ? `<td class="muted">${x.liquidation_price ? fmtPx(x.liquidation_price) : '—'}</td>` : ''}
          <td><button class="small" data-close="${esc(x.symbol)}">${x.side === 'long' ? '全部卖掉' : '全部买回'}</button></td></tr>`;
      }), '你还没有股票。在右边"买卖"里买一点试试吧');
  }
  return table(['代码', '方向', '数量', '均价', '现价', '市值', '未实现盈亏', '已实现', '占比', ...(margin ? ['强平价'] : []), ''],
    p.positions.map((x) => {
      const pct = x.unrealized != null ? x.unrealized / x.cost : null;
      return `<tr><td>${symLink(x.symbol)}</td>
        <td><span class="tag ${x.side}">${x.side === 'long' ? '多' : '空'}</span></td>
        <td>${Math.abs(x.qty)}${x.reserved_qty ? `<span class="muted small">（冻结 ${x.reserved_qty}）</span>` : ''}</td>
        <td>${fmtPx(x.avg_cost)}</td><td>${fmtPx(x.price)}</td><td>${fmtUsd(x.market_value)}</td>
        <td class="${cls(x.unrealized)}">${fmtSigned(x.unrealized)} (${fmtPct(pct)})</td>
        <td class="${cls(x.realized)}">${fmtSigned(x.realized)}</td>
        <td>${x.market_value != null && p.equity > 0 ? fmtPct(Math.abs(x.market_value) / p.equity).replace('+', '') : '—'}</td>
        ${margin ? `<td class="muted">${x.liquidation_price ? fmtPx(x.liquidation_price) : '—'}</td>` : ''}
        <td><button class="small" data-close="${esc(x.symbol)}">平仓</button></td></tr>`;
    }), '暂无持仓');
}

async function loadTab() {
  const tab = state.tab;
  let html = '';
  if (tab === 'positions') {
    html = positionsTable(state.portfolio ?? await api(withAcct('/api/portfolio')));
  } else if (tab === 'open') {
    const orders = await api(withAcct('/api/orders?status=open'));
    html = table(['代码', '方向', '类型', '数量', '限价', '冻结', '交易日', '下单时间', ''],
      orders.map((o) => `<tr><td>${symLink(o.symbol)}</td><td class="${o.side === 'buy' ? 'up' : 'down'}">${SIDE[o.side]}</td>
        <td>${TYPE[o.type]}</td><td>${o.qty}</td><td>${fmtPx(o.limit_price)}</td>
        <td>${isMargin() || o.side === 'buy' ? fmtUsd(o.reserved) : `${o.qty} 股`}</td><td>${o.trade_date}</td><td>${fmtTime(o.placed_at)}</td>
        <td><button class="small" data-cancel="${o.id}">撤单</button></td></tr>`), '暂无挂单');
  } else if (tab === 'orders') {
    const orders = await api(withAcct('/api/orders?limit=200'));
    html = table(['时间', '代码', '方向', '类型', '数量', '限价', '成交价', '状态', '说明'],
      orders.map((o) => `<tr><td>${fmtTime(o.placed_at)}</td><td>${symLink(o.symbol)}</td>
        <td class="${o.side === 'buy' ? 'up' : 'down'}">${SIDE[o.side]}</td><td>${TYPE[o.type]}</td><td>${o.qty}</td>
        <td>${o.limit_price ? fmtPx(o.limit_price) : '—'}</td><td>${o.fill_price ? fmtPx(o.fill_price) : '—'}</td>
        <td><span class="tag ${o.status}">${STATUS[o.status]}</span></td><td class="muted">${esc(o.reason ?? '')}</td></tr>`), '暂无订单');
  } else if (state.simple) {
    const fills = await api(withAcct('/api/fills?limit=200'));
    html = table(['时间（美东）', '股票', '做了什么', '股数', '价格', '金额'],
      fills.map((f) => `<tr><td>${fmtTime(f.executed_at)}</td><td>${symCell(f.symbol)}</td>
        <td class="${f.side === 'buy' ? 'up' : 'down'}">${SIDE[f.side]}</td><td>${f.qty}</td><td>${fmtPx(f.price)}</td>
        <td>${fmtUsd(f.amount)}</td></tr>`), '还没有买卖过');
  } else {
    const fills = await api(withAcct('/api/fills?limit=200'));
    html = table(['成交时间', '代码', '方向', '数量', '成交价', '金额', '费用', ''],
      fills.map((f) => `<tr><td>${fmtTime(f.executed_at)}</td><td>${symLink(f.symbol)}</td>
        <td class="${f.side === 'buy' ? 'up' : 'down'}">${SIDE[f.side]}</td><td>${f.qty}</td><td>${fmtPx(f.price)}</td>
        <td>${fmtUsd(f.amount)}</td><td>${fmtUsd(f.fees)}</td><td class="muted small">${f.estimated ? '模拟估算' : ''}</td></tr>`), '暂无成交');
  }
  if (tab === state.tab) $('#tab-body').innerHTML = html;
}

function selectTab(tab) {
  state.tab = tab;
  $$('#bottom-tabs button').forEach((x) => x.classList.toggle('active', x.dataset.tab === tab));
  loadTab().catch((err) => toast(err.message, 'error'));
}

// ---------- order ticket ----------
const slip = () => (state.account?.slippage_bps ?? 0) / 10000;
const currentPos = () => state.portfolio?.positions.find((p) => p.symbol === state.symbol);
// Simple mode buys by dollar amount, except when buying back a short (then shares are clearer).
const amountMode = () => state.simple && state.side === 'buy' && (currentPos()?.qty ?? 0) >= 0;

function ticketNumbers() {
  const limit = Number($('#t-limit').value);
  const ref = state.type === 'limit' ? limit : state.quote?.price;
  const px = ref ? (state.type === 'market' ? ref * (state.side === 'buy' ? 1 + slip() : 1 - slip()) : ref) : null;
  const amountInput = Number($('#t-amount').value);
  const qty = amountMode() ? (px ? Math.floor(amountInput / px + 1e-9) : 0) : Number($('#t-qty').value);
  const pos = currentPos();
  const posQty = pos?.qty ?? 0;
  const sellable = pos ? pos.qty - pos.reserved_qty : 0;
  const out = { qty, limit, px, amountInput, amount: px && qty ? px * qty : null, sellable, posQty, cash: state.portfolio?.available_cash ?? 0 };
  const m = state.portfolio?.margin;
  if (m && px) {
    // Mirrors the server: extra initial margin for the new exposure; reducing orders need none.
    const init = state.account.initial_margin_bps / 10000;
    const newQty = posQty + (state.side === 'buy' ? qty : -qty);
    const reducing = posQty !== 0 && Math.abs(newQty) < Math.abs(posQty) && (newQty === 0 || (newQty > 0) === (posQty > 0));
    const curMark = state.quote?.price ?? px;
    out.need = reducing ? 0 : Math.max(0, Math.abs(newQty) * px * init - Math.abs(posQty) * curMark * init);
    out.excess = m.excess;
    const closable = state.side === 'buy' ? Math.max(0, -posQty) : Math.max(0, posQty);
    out.maxQty = closable + Math.max(0, Math.floor(m.excess / (px * init) + 1e-9));
    // Cash borrowed by a long purchase beyond what the account holds.
    out.borrow = state.side === 'buy' && newQty > 0 ? Math.max(0, (out.amount ?? 0) - Math.max(0, state.portfolio.cash)) : 0;
  }
  return out;
}

function actionLabel(side, qty, posQty) {
  if (!isMargin()) return '';
  if (side === 'buy') return posQty < 0 ? (qty > -posQty ? '平空并做多' : '平空') : '做多';
  return posQty > 0 ? (qty > posQty ? '平多并开空' : '平多') : '开空';
}

// Plain-language action: [button verb, longer explanation].
function plainAction(side, qty, posQty) {
  if (side === 'buy') {
    if (posQty < 0) return qty > -posQty ? ['买回并买涨', '先把借来卖掉的股票全部买回还掉，多出来的部分变成买涨'] : ['买回', '把之前借来卖掉的股票买回来还掉'];
    return ['买入', '买进来，涨了你就赚'];
  }
  if (posQty > 0) return qty > posQty ? ['卖光并卖空', '先卖掉手上所有的，多卖的部分是借来卖的（赌它跌）'] : ['卖出', '卖掉手上的股票'];
  return ['卖空', '借股票来卖掉，以后再买回来还。跌了你赚，涨了你亏'];
}

function renderTicket() {
  if (state.simple) state.type = 'market';
  $('#t-symbol').value = state.symbol ? `${state.symbol}${state.simple && cnName(state.symbol) ? ` ${cnName(state.symbol)}` : ''}` : '';
  $$('#side-seg button').forEach((b) => b.classList.toggle('active', b.dataset.side === state.side));
  $$('#type-seg button').forEach((b) => b.classList.toggle('active', b.dataset.type === state.type));
  $('#t-limit-wrap').hidden = state.type !== 'limit';
  $('#t-amount-wrap').hidden = !amountMode();
  $('#t-qty-wrap').hidden = amountMode();
  const n = ticketNumbers();
  $('#t-qty-label').textContent = T(
    state.side === 'sell' ? (n.posQty > 0 ? '卖几股' : '卖空几股') : '买回几股', '数量（股）');

  const btn = $('#t-submit');
  btn.className = `primary ${state.side}`;
  if (state.simple) {
    btn.textContent = `${plainAction(state.side, n.qty, n.posQty)[0]} ${state.symbol ?? ''}`;
  } else {
    const action = actionLabel(state.side, n.qty, n.posQty);
    btn.textContent = `${SIDE[state.side]}${action ? `（${action}）` : ''} ${state.symbol ?? ''}`;
  }

  const lines = [];
  if (state.simple) {
    if (amountMode() && n.px) lines.push(`能买 <b>${n.qty}</b> 股，大约花 <b>${fmtUsd(n.amount ?? 0)}</b>`);
    else if (n.amount != null) lines.push(`大约${state.side === 'buy' ? '要花' : '能收到'} <b>${fmtUsd(n.amount)}</b>`);
    if (isMargin()) {
      if (n.need) lines.push(`要押上你自己的钱 <b>${fmtUsd(n.need)}</b>（你还能押 <b>${fmtUsd(Math.max(0, n.excess))}</b>）`);
      if (n.borrow > 0) lines.push(`其中要借 <b>${fmtUsd(n.borrow)}</b>，每年收 8% 利息`);
      if (state.side === 'sell' && n.qty > Math.max(0, n.posQty)) lines.push('会借股票来卖，每年收 3% 费用');
      if (n.posQty) lines.push(`你现在${n.posQty > 0 ? '有' : '卖空了'} <b>${Math.abs(n.posQty)}</b> 股`);
    } else if (state.side === 'buy') lines.push(`你能用的现金 <b>${fmtUsd(n.cash)}</b>`);
    else lines.push(`你手上有 <b>${n.sellable}</b> 股可以卖`);
  } else {
    if (n.amount != null) lines.push(`预计${state.side === 'buy' ? '花费' : '收入'} <b>${fmtUsd(n.amount)}</b>${state.type === 'market' ? `（含 ${state.account.slippage_bps}bp 滑点）` : ''}`);
    if (isMargin()) {
      if (n.need != null) lines.push(`所需保证金 <b>${fmtUsd(n.need)}</b> / 可用 <b>${fmtUsd(Math.max(0, n.excess))}</b>`);
      if (n.posQty) lines.push(`当前持仓 <b>${n.posQty > 0 ? `多 ${n.posQty}` : `空 ${-n.posQty}`}</b> 股`);
    } else if (state.side === 'buy') lines.push(`可用资金 <b>${fmtUsd(n.cash)}</b>`);
    else lines.push(`可卖 <b>${n.sellable}</b> 股`);
  }

  let problem = null;
  if (!state.symbol) problem = T('先在左边选一只股票', '先选择一只股票');
  else if (state.account?.status === 'bust') problem = T('钱已经亏光了，这局结束', '账户已爆仓');
  else if (state.status?.mode === 'replay' && state.status.status !== 'active') problem = T('这局回放已经结束了', '回放已结束');
  else if (state.type === 'market' && !state.quote) problem = T('暂时拿不到价格', '暂无行情');
  else if (state.type === 'market' && !state.quote.tradable) problem = T(plain(state.quote.reason), `现在不能市价成交：${state.quote.reason}`);
  else if (!Number.isInteger(n.qty) || n.qty <= 0) {
    problem = amountMode() ? (n.amountInput > 0 ? '钱太少，连 1 股都买不到' : '填一下要花多少钱') : T('填一下股数', '数量必须是正整数');
  } else if (state.type === 'limit' && !(n.limit > 0)) problem = '请输入限价';
  else if (isMargin()) { if (n.need > n.excess) problem = T('押金不够，少买一点吧', '保证金不足'); }
  else if (state.side === 'buy' && n.amount > n.cash) problem = T('钱不够', '可用资金不足');
  else if (state.side === 'sell' && n.qty > n.sellable) {
    problem = n.sellable ? T('你没有这么多股', '可卖股数不足') : T('你手上没有这只股票。普通账户只能卖手上有的股票', '现金账户不能卖空（需要保证金账户）');
  }
  if (problem) lines.push(`<span class="warn">${esc(problem)}</span>`);
  else if (state.type === 'limit' && state.quote && !state.quote.tradable) lines.push('<span class="warn">当前不可成交，挂单将在可交易时检查</span>');

  $('#t-est').innerHTML = lines.join('<br>');
  btn.disabled = !!problem || state.busy;
  $('#t-note').textContent = state.simple
    ? `按现在的价格马上成交，实际价格可能差一点点（约 0.05%）。${isReplay() ? '回放里的价格就是当前这个时刻（开盘或收盘）的价格。' : ''}`
      + (isMargin() ? '借钱每年收 8% 利息，借股票卖空每年收 3% 费用，每天扣一点。' : '')
    : (isReplay() ? '当日有效（DAY）。收盘时下的单属于下一交易日。成交为模拟估算。' : '当日有效（DAY）。成交为模拟估算，不代表真实可成交价格。')
      + (isMargin() ? ' 保证金账户：借款年息 8%，做空年费 3%，按日计收。' : '');
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

function confirmHtml(n) {
  if (state.simple) {
    const [verb, explain] = plainAction(state.side, n.qty, n.posQty);
    const amount = n.amount ?? 0;
    const ten = fmtUsd(amount * 0.1);
    const opensLong = state.side === 'buy' && n.posQty >= 0;
    const opensShort = state.side === 'sell' && n.posQty <= 0;
    const whatIf = opensLong ? `如果 ${state.symbol} 涨 10%，你大约赚 ${ten}；跌 10%，你大约亏 ${ten}。`
      : opensShort ? `如果 ${state.symbol} 跌 10%，你大约赚 ${ten}；涨 10%，你大约亏 ${ten}。涨得越多亏得越多，没有上限。`
        : '平掉之后，这部分的赚或亏就定下来了，之后涨跌都跟你无关。';
    return `<div class="confirm-grid">
      <span>股票</span><span><b>${esc(state.symbol)}</b> ${esc(cnName(state.symbol) ?? '')}</span>
      <span>做什么</span><span class="${state.side === 'buy' ? 'up' : 'down'}"><b>${verb}</b></span>
      <span>股数</span><span>${n.qty} 股</span>
      <span>大约金额</span><span>${fmtUsd(amount)}</span>
      ${isMargin() && n.need ? `<span>押上自己的钱</span><span>${fmtUsd(n.need)}</span>` : ''}
      ${n.borrow > 0 ? `<span>借钱</span><span class="down">${fmtUsd(n.borrow)}</span>` : ''}
    </div>
    <p class="confirm-note">${explain}。</p>
    <p class="confirm-note">${whatIf}</p>`;
  }
  return `<div class="confirm-grid">
    <span>股票</span><span><b>${esc(state.symbol)}</b></span>
    <span>方向</span><span class="${state.side === 'buy' ? 'up' : 'down'}">${SIDE[state.side]}${isMargin() ? `（${actionLabel(state.side, n.qty, n.posQty)}）` : ''}</span>
    <span>类型</span><span>${TYPE[state.type]}${state.type === 'limit' ? ` @ ${fmtPx(n.limit)}` : ''}（当日有效）</span>
    <span>数量</span><span>${n.qty} 股</span>
    <span>预计金额</span><span>${fmtUsd(n.amount)}</span>
    ${isMargin() ? `<span>所需保证金</span><span>${fmtUsd(n.need)}</span>` : ''}
    <span>账户</span><span>${esc(accountLabel(state.account))}${isReplay() ? `（${state.account.sim_date} ${PHASE[state.account.sim_phase]}）` : ''}</span>
  </div>${isMargin() && state.side === 'sell' && actionLabel(state.side, n.qty, n.posQty).includes('空')
    ? '<p class="muted small">做空：股价上涨你亏钱，而且理论上亏损没有上限。</p>' : ''}`;
}

async function submitOrder() {
  const n = ticketNumbers();
  const body = { account_id: state.accountId, symbol: state.symbol, side: state.side, type: state.type, qty: n.qty };
  if (state.type === 'limit') body.limit_price = Number(n.limit.toFixed(4));
  if (!(await confirmDialog(confirmHtml(n)))) return;
  const verb = plainAction(state.side, n.qty, n.posQty)[0];
  state.busy = true;
  renderTicket();
  try {
    const o = await api('/api/orders', { method: 'POST', body: JSON.stringify(body) });
    if (o.status === 'filled') {
      toast(T(`成功！${verb} ${o.qty} 股 ${o.symbol}，价格 ${fmtPx(o.fill_price)}`,
        `已成交：${SIDE[o.side]} ${o.qty} 股 ${o.symbol} @ ${fmtPx(o.fill_price)}`), 'ok');
    } else if (o.status === 'open') {
      toast(`挂单已提交：${SIDE[o.side]} ${o.qty} 股 ${o.symbol} @ ${fmtPx(o.limit_price)}`, 'ok');
    } else {
      toast(`${T('没成功：', '订单被拒绝：')}${plain(o.reason)}`, 'error');
    }
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
    await loadAccounts(); // replay clock, bust / reset status may have changed
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
    if (ev.filled) bits.push(T(`有 ${ev.filled} 个单子成交了`, `成交 ${ev.filled} 笔`));
    if (ev.expired) bits.push(T(`${ev.expired} 个单子没成交，作废了`, `过期 ${ev.expired} 笔`));
    if (ev.financing >= 0.01) bits.push(T(`扣了利息和借股票费用 ${fmtUsd(ev.financing)}`, `利息/融券费 ${fmtUsd(ev.financing)}`));
    if (ev.liquidated) bits.push(T(`⚠ 亏得太多，系统自动帮你平掉了 ${ev.liquidated} 只股票`, `⚠ 保证金不足，强制平仓 ${ev.liquidated} 个仓位`));
    if (ev.bust) bits.push(T('钱亏光了，这局结束', '账户爆仓，本局结束'));
    else if (ev.finished) bits.push(T('已经走到今天了，这局结束', '已到达最新数据，回放结束'));
    if (bits.length) toast(bits.join('，'), ev.liquidated || ev.bust ? 'error' : ev.finished ? '' : 'ok');
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
        : `<li class="muted">${T('没找到。试试英文名，或者去"公司列表"里挑', '无结果（可直接回车打开代码）')}</li>`;
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
$('#mode-btn').addEventListener('click', toggleMode);
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
  if (b) selectTab(b.dataset.tab);
});
$('#tab-body').addEventListener('click', async (e) => {
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
    state.side = pos.qty > 0 ? 'sell' : 'buy';
    state.type = 'market';
    $('#t-qty').value = pos.qty > 0 ? pos.qty - pos.reserved_qty : -pos.qty;
    renderTicket();
    return;
  }
  const link = e.target.closest('[data-symbol]');
  if (link) selectSymbol(link.dataset.symbol);
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
  if (n.maxQty != null) max = n.maxQty;
  else if (state.side === 'sell') max = n.sellable;
  else if (n.px) max = Math.floor(n.cash / (n.px * (1 + 1e-9)) - 1e-9);
  const qty = Math.max(0, Math.floor(max * frac));
  if (amountMode()) $('#t-amount').value = n.px ? (Math.ceil(qty * n.px * 100) / 100).toFixed(2) : '';
  else $('#t-qty').value = qty;
  renderTicket();
});
$('#t-qty').addEventListener('input', renderTicket);
$('#t-amount').addEventListener('input', renderTicket);
$('#t-limit').addEventListener('input', renderTicket);
$('#t-submit').addEventListener('click', submitOrder);
$('#account-select').addEventListener('change', (e) => switchAccount(Number(e.target.value)));
$('#replay-controls').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-step]');
  if (b) advance(b.dataset.step);
});

function renderAccountDialog() {
  const replayMode = $('#n-mode').value === 'replay';
  const margin = $('#n-type').value === 'margin';
  $('#n-start-wrap').hidden = !replayMode;
  $('#n-replay-note').hidden = !replayMode;
  $('#n-leverage-wrap').hidden = !margin;
  $('#n-margin-note').hidden = !margin;
  if (margin) {
    const lev = Number($('#n-leverage').value);
    const init = 100 / lev;
    $('#n-margin-note').textContent = T(
      `买 $100 的股票，你自己出 $${init.toFixed(0)}${lev > 1 ? `，借 $${(100 - init).toFixed(0)}` : ''}。涨跌都按 $100 算，所以赚亏都是普通账户的 ${lev} 倍。亏到你的钱只剩仓位的 ${(init / 2).toFixed(1)}%（卖空是 ${(init * 0.6).toFixed(1)}%）时，系统会自动帮你平仓。借钱每年 8% 利息，卖空每年 3% 费用。`,
      `开仓需自有资金 ${init.toFixed(1)}%；净资产跌破仓位价值的 ${(init / 2).toFixed(1)}%（多头）/ ${(init * 0.6).toFixed(1)}%（空头）会被强制平仓。借款年息 8%，做空年费 3%。`);
  }
}
['#n-mode', '#n-type', '#n-leverage'].forEach((s) => $(s).addEventListener('change', renderAccountDialog));

$('#new-account-btn').addEventListener('click', async () => {
  renderAccountDialog();
  if (!(await openDialog($('#account-dialog')))) return;
  const body = {
    mode: $('#n-mode').value, account_type: $('#n-type').value,
    leverage: Number($('#n-leverage').value), initial: Number($('#n-initial').value) || 100000,
  };
  if (body.mode === 'replay' && $('#n-start').value) body.start_date = $('#n-start').value;
  if (body.mode === 'replay') toast(T('正在准备历史数据，稍等…', '正在准备历史数据…'));
  try {
    const acct = await api('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
    toast(`${T('开好了：', '已创建：')}${accountLabel(acct)}`, 'ok');
    await switchAccount(acct.id);
  } catch (e) { toast(e.message, 'error'); }
});
$('#archive-btn').addEventListener('click', async () => {
  if (!confirm(T(`收起「${accountLabel(state.account)}」？收起后列表里就看不到了，没成交的单子会被取消。`,
    `归档「${accountLabel(state.account)}」？归档后不再显示在列表中，挂单会被撤销。`))) return;
  try {
    await api(`/api/accounts/${state.accountId}/archive`, { method: 'POST' });
    toast(T('已收起', '已归档'), 'ok');
    state.accountId = null;
    store.set('accountId', '');
    await loadAccounts();
    await switchAccount(state.accountId);
  } catch (e) { toast(e.message, 'error'); }
});
$('#reset-btn').addEventListener('click', async () => {
  if (!confirm(T(`把「${accountLabel(state.account)}」重新开始？所有股票和买卖记录都会清空，钱恢复成一开始的数。`,
    `重置「${accountLabel(state.account)}」？所有订单、成交和持仓将被清除，资金恢复初始值。`))) return;
  try {
    await api(withAcct('/api/account/reset'), { method: 'POST' });
    toast(T('已经重新开始了', '账户已重置'), 'ok');
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
  applyMode();
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

// Packaged exe only: offer a quit button (the server exits when the last tab closes anyway).
fetch('/api/ping', { method: 'POST' }).then((r) => r.json()).then((d) => {
  if (!d.exe) return;
  const btn = $('#quit-btn');
  btn.hidden = false;
  btn.addEventListener('click', async () => {
    if (!confirm('确定退出程序吗？存档已自动保存。')) return;
    await fetch('/api/quit', { method: 'POST' }).catch(() => {});
    document.body.innerHTML = '<p style="padding:2rem">StockSim 已退出，可以关闭此页面。</p>';
  });
}).catch(() => {});
