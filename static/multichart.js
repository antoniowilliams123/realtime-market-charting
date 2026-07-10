/* Multichart frontend.
 *
 * makeQuadrant(containerEl, ticker, tf) creates a self-contained chart quadrant:
 *   - lightweight-charts v4.2.3 chart with the EXACT Appendix A config
 *   - an overlay <canvas> drawing the :30-anchored HH:MM ET x-axis labels,
 *     vertical dividers, and per-bucket H/L/C projection segments
 *   - a control strip [Ticker][Timeframe][Indicators][Maximize][Pop-out]
 *
 * The grid page owns ONE multiplexed EventSource and routes topic-tagged frames
 * to the matching quadrant. A pop-out (/quadrant) opens its own single stream.
 *
 * Frame contract (from the live backend):
 *   hot : {topic:{ticker,tf}, kind:"hot", bar:{ts_ms,open,high,low,close,...}, sequence_id, origin_ms}
 *   cold: {topic:{ticker,tf}, kind:"cold", snap:{bars,levels,segments,boundaries,
 *          last_ts_ms,default_zoom,timeframe,feed_state,snapshot_version,...},
 *          snapshot_version, sequence_id, origin_ms}
 */
(function () {
'use strict';


const LC = window.LightweightCharts;
const TICKERS = ['NQ', 'ES', 'YM', 'RTY', 'GC', 'CL'];   // 6 tickers — MBT removed 2026-06-15 (not traded); charts auto-linked
const TIMEFRAMES = [
  ['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['30m', '30m'],
  ['1h', '1h'], ['4h', '4h'], ['1d', 'Daily'],
];
// Readable timeframe caption for the per-quadrant footer.
const TF_FOOTER_LABEL = {
  '1m': '1-Minute', '5m': '5-Minute', '15m': '15-Minute', '30m': '30-Minute',
  '1h': '1-Hour', '4h': '4-Hour', '1d': 'Daily',
};
function tfFooterLabel(tf) { return TF_FOOTER_LABEL[tf] || tf; }
// Price shown next to a level label (e.g. "YC $4336.50") so prior D/W/M + 4H
// levels carry their price even when they're not on the right price axis.
function fmtLvl(p) { return (p == null) ? '' : ' $' + Number(p).toFixed(2); }
const STORAGE_KEY = 'mc_multichart_layout_v4';   // 2026-06-05: layout 1h/30m/5m/1m; 1H off all TFs, 4H on only 1m; 1h+30m 5-day; 5m+1m MAs off
// Ticker-link feature: when ON, picking a ticker in any one dropdown switches
// EVERY quadrant (and every popped-out window) to that ticker. State is shared
// across windows via a BroadcastChannel + a dedicated localStorage key so newly
// opened pop-outs adopt the current link state on boot.
const LINK_KEY = 'mc_link_tickers';
const LINK_CHANNEL = 'mc_ticker_link';
// The current shared ticker (last one picked in ANY window), persisted so a
// refreshed/newly-opened window REJOINS the group at this ticker instead of
// reverting to its frozen pop-out URL ticker (which caused refresh desyncs).
const CUR_TICKER_KEY = 'mc_cur_ticker';
// User-drawn horizontal lines are shared per-TICKER across every chart (so a line
// drawn on the 5m shows on the 1m and all other timeframes) and across windows,
// via a localStorage map { [ticker]: [{price,t1,t2}] } + a BroadcastChannel.
const LINES_KEY = 'mc_lines_v1';
const LINES_CHANNEL = 'mc_lines';
// Crosshair show/hide is a GLOBAL toggle shared across every quadrant AND every
// window (grid, pop-out, maximized) — turning it off on one chart hides the
// crosshair lines everywhere. Same localStorage + BroadcastChannel pattern as
// the ticker link. Default ON (unset → shown); only the literal '0' means off.
const CROSSHAIR_KEY = 'mc_crosshair';
const CROSSHAIR_CHANNEL = 'mc_crosshair';
function loadCrosshairOn() {
  try { return localStorage.getItem(CROSSHAIR_KEY) !== '0'; } catch (e) { return true; }
}
function loadLines(ticker) {
  try { return (JSON.parse(localStorage.getItem(LINES_KEY) || '{}')[ticker] || []).slice(); }
  catch (e) { return []; }
}
function saveLines(ticker, lines) {
  let all = {};
  try { all = JSON.parse(localStorage.getItem(LINES_KEY) || '{}'); } catch (e) {}
  all[ticker] = lines;
  try { localStorage.setItem(LINES_KEY, JSON.stringify(all)); } catch (e) {}
}
const DEFAULT_LAYOUT = [          // NQ — UL=1h, UR=30m, LL=5m, LR=1m (2026-06-08)
  { ticker: 'NQ', tf: '1h' },    // upper-left
  { ticker: 'NQ', tf: '30m' },   // upper-right
  { ticker: 'NQ', tf: '5m' },    // lower-left
  { ticker: 'NQ', tf: '1m' },    // lower-right
];

/* Moving-average preset sets — toggled via the buttons below.
 * At most ONE set is active at a time (clicking one turns the other off; click again = off).
 * Each line: key = bar field, color = line color. All MA lines render at width 2. */
const MA_SETS = {
  sb: { label: 'EMA4', title: 'EMA stack — 200 purple · 96 green · 50 blue · 20 red',
        lines: [ { key: 'ema200', color: '#9c27b0' }, { key: 'ema96', color: '#2e9c2e' },
                 { key: 'ema50', color: '#4b9cd3' }, { key: 'ema20', color: '#ff0000' } ] },
  ov: { label: 'MA', title: '200 SMA red + 20 EMA blue',
        lines: [ { key: 'sma200', color: '#ff0000' }, { key: 'ema20', color: '#0000ff' } ] },
};
const MA_WIDTH = 2;                       // all MA lines render at thickness 2 (locked)
/* Fresh-open default MA set: NONE — moving averages are OFF by default on EVERY
 * timeframe (standing default, 2026-06-10). The OV button still toggles
 * them on per-quadrant, and an explicit saved layout's maSet still wins. */
const MA_DEFAULT = null;

/* TEMPORARY (toggle back to false to restore): hide the SB / OV moving-average
 * buttons, make them un-pressable, and force MAs OFF on every quadrant regardless
 * of any saved layout. Set to false to bring the buttons (and saved MA state) back. */
const MA_BUTTONS_DISABLED = false;

/* EMA-stack button hidden by default — keep only the MA pair. When false: the SB button is hidden + un-pressable, and any saved
 * 'sb' state is coerced to off so it can't get stuck with no toggle. */
const SB_ENABLED = false;

/* TEMPORARY (set true to restore): hide the 1H + 4H bucket toggle buttons,
 * make them un-pressable, and force their vertical dividers / horizontal levels
 * / live-bucket shading OFF on every quadrant regardless of saved layout. Default
 * focus is Daily / Weekly / Monthly levels. Daily/Weekly/
 * Monthly are separate code paths and are NOT affected. */
const BUCKETS_1H_4H_ENABLED = false;
const BUCKETS_4H_ENABLED = true;   // 4H toggle button restored independently (1H stays hidden), default ON

/* Session shading — translucent high-low boxes drawn on the overlay, matching a print-chart
 * reference style. Each box spans a session's time window (x) and
 * only the high-low price range traded in it (y), NOT the full chart height. Hours
 * are ET, inclusive. Toggle via the "SES" button. */
const SESSION_WINDOWS = [
  // color = translucent box fill; line = solid same-hue color for the H/L lines; lbl = short tag.
  { key: 'asia',   loH: 19, hiH: 23, color: 'rgba(33,150,243,0.13)', line: 'rgba(33,150,243,0.85)', lbl: 'As' },   // Asia (blue)   7–11 PM
  { key: 'london', loH: 1,  hiH: 5,  color: 'rgba(233,30,99,0.11)',  line: 'rgba(233,30,99,0.85)',  lbl: 'Ln' },   // London (pink) 1–5 AM
  { key: 'ny',     loH: 7,  hiH: 11, color: 'rgba(255,193,7,0.16)',  line: 'rgba(255,193,7,0.95)',  lbl: 'Ny' },   // New York (yellow) 7–11 AM
];
// Only intraday TFs have within-day bars to bucket into session windows.
const SESSION_TFS = ['1m', '5m', '15m', '30m'];
// How many of the most-recent session boxes get H/L lines pushed to the right edge:
// the current (in-progress) session + the 2 prior rolling sessions = 3.
const SESSION_LINE_COUNT = 3;

// Cached ET parts formatter (DST-correct). h23 so midnight reads as hour 0, not 24.
// Lazily built on first use because `ET` is declared further down the module.
let _ET_PARTS = null;
function etDateHour(ms) {
  // → { date: 'YYYY-MM-DD', hour: 0..23 }  (ET, DST-aware)
  if (!_ET_PARTS) _ET_PARTS = new Intl.DateTimeFormat('en-US', {
    timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const p = {};
  for (const part of _ET_PARTS.formatToParts(new Date(ms))) p[part.type] = part.value;
  return { date: p.year + '-' + p.month + '-' + p.day, hour: parseInt(p.hour, 10), minute: parseInt(p.minute, 10) };
}

/* ET trading-MONTH for a bar timestamp (futures session rolls at 18:00 ET, so a
 * bar at/after 18:00 belongs to the next day's session — and thus the next
 * month at a month boundary). Cached: bar times are stable, so each ms is
 * resolved once. Used by the client-side month grid (1d/4h). */
const _tradingYMCache = new Map();
function tradingYM(ms) {
  let v = _tradingYMCache.get(ms);
  if (v) return v;
  const dh = etDateHour(ms);
  let Y = +dh.date.slice(0, 4), M = +dh.date.slice(5, 7), D = +dh.date.slice(8, 10);
  if (dh.hour >= 18) {                       // session rolls to the next day
    const d = new Date(Date.UTC(Y, M - 1, D)); d.setUTCDate(d.getUTCDate() + 1);
    Y = d.getUTCFullYear(); M = d.getUTCMonth() + 1;
  }
  v = { ym: Y * 100 + M, Y: Y, M: M };
  _tradingYMCache.set(ms, v);
  return v;
}
const MON_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function sessionWindowFor(mod) {
  // mod = minute-of-day (ET). Window ENDS at the top of hiH — e.g. London 1:00-5:00 (incl. the
  // 5:00 bar, NOT 5:01-5:59), matching the reference style. Was bugged: hour<=hiH ran to :59.
  for (const w of SESSION_WINDOWS) if (mod >= w.loH * 60 && mod <= w.hiH * 60) return w;
  return null;
}

/* Resolve the active MA set ('sb' | 'ov' | null). A saved layout's explicit maSet wins;
 * a legacy per-indicator {ema20,sma200} state maps to 'ov' (its old blue+red look);
 * otherwise the per-TF default. */
function maSetFrom(savedMaSet, legacyInd, defaultSet) {
  if (savedMaSet === 'sb' || savedMaSet === 'ov' || savedMaSet === null) return savedMaSet;
  if (legacyInd && typeof legacyInd === 'object') return (legacyInd.ema20 || legacyInd.sma200) ? 'ov' : null;
  return (defaultSet === undefined) ? MA_DEFAULT : defaultSet;
}

/* ------------------------------------------------------------------ *
 * Overlay styling — Appendix A.7 (1H/4H purple/black-green) generalized
 * to the higher-TF Day/Week/Month buckets via the §5.3 color rule:
 *   day = orange (#ff9800), week = blue (#4B9CD3), month = red (#D32F2F);
 *   close = solid heavier, H/L = dashed.
 * ------------------------------------------------------------------ */
const SEG_STYLE = {
  // 4H — purple — ALL 4H horizontal lines: thickness 1
  '4HH': { color: 'rgba(156,39,176,0.85)', lw: 1, dash: [2, 3] },
  '4HL': { color: 'rgba(156,39,176,0.85)', lw: 1, dash: [2, 3] },
  '4HC': { color: 'rgba(156,39,176,0.95)', lw: 1, dash: [] },
  // 1H — black H/L, green C — ALL 1H horizontal lines: thickness 1
  '1HH': { color: '#000000',              lw: 1, dash: [2, 3] },
  '1HL': { color: '#000000',              lw: 1, dash: [2, 3] },
  '1HC': { color: 'rgba(46,125,50,0.95)', lw: 1, dash: [] },
  // 1D — orange (matches PD static color) — thickness 3
  '1DH': { color: 'rgba(255,152,0,0.90)', lw: 3, dash: [5, 3] },
  '1DL': { color: 'rgba(255,152,0,0.90)', lw: 3, dash: [5, 3] },
  '1DC': { color: 'rgba(46,125,50,0.95)', lw: 3, dash: [] },
  // 1W — Carolina blue (matches PW static color) — thickness 3
  '1WH': { color: 'rgba(75,156,211,0.90)', lw: 3, dash: [5, 3] },
  '1WL': { color: 'rgba(75,156,211,0.90)', lw: 3, dash: [5, 3] },
  '1WC': { color: 'rgba(22,163,74,0.95)',  lw: 3, dash: [] },
  // 1M — red (matches PM static color) — thickness 3
  '1MH': { color: 'rgba(211,47,47,0.90)', lw: 3, dash: [6, 4] },
  '1ML': { color: 'rgba(211,47,47,0.90)', lw: 3, dash: [6, 4] },
  '1MC': { color: 'rgba(46,125,50,0.95)', lw: 3, dash: [] },
};

/* Annual "month grid" styling (used by 1d + 4h, drawn client-side over
 * ALL loaded candles incl. lazy deep history). Month divider gray dotted;
 * prior-month High/Low black dotted; prior-month Close green solid. */
const MONTH_GRID = {
  divider: { color: '#9aa1ad', lw: 1.5, dash: [2, 3] },
  hl:      { color: '#111e27', lw: 2,   dash: [2, 3] },
  close:   { color: '#2e7d32', lw: 2.2, dash: [] },
  label:   '#5a6472',
};
const MONTH_GRID_TFS = ['1d', '4h'];

/* Vertical divider styling per boundary key (Appendix A.8). The backend's
 * dividers() emits keys: 1H, 4H, 1D, 1W, 1M, cash_open, futures_open. */
const DIVIDER_STYLE = {
  '1H':           { color: '#000000',                lw: 1.0, dash: [],     z: 1 },
  '4H':           { color: 'rgba(156,39,176,0.85)',  lw: 1.6, dash: [6, 4], z: 4 },
  '1D':           { color: 'rgba(245,158,11,0.85)',  lw: 2.0, dash: [],     z: 4 },
  '1W':           { color: 'rgba(75,156,211,0.95)',  lw: 1.0, dash: [2, 3], z: 6 },
  '1M':           { color: 'rgba(211,47,47,0.95)',   lw: 1.0, dash: [2, 3], z: 6 },
  'futures_open': { color: 'rgba(245,158,11,0.85)',  lw: 2.2, dash: [],     z: 7 },
  'cash_open':    { color: 'rgba(22,163,74,0.90)',   lw: 2.4, dash: [],     z: 8 },
};

/* Label color by boundary type (Appendix A.2). */
const LABEL_COLOR = {
  '1H': '#000000',
  '4H': 'rgb(126,29,156)',
  '1D': '#f59e0b',
  '1W': '#4B9CD3',
  '1M': 'rgb(211,47,47)',
  'futures_open': '#f59e0b',
  'cash_open': '#16a34a',
  'am_0959': '#795548',
};
/* Boundary keys whose top labels read "<KEY> · HH:MM ET" (Appendix A.2). */
const TOP_LABEL_KEYS = ['4H', '1D', '1W', '1M'];

/* Timeframes that render the PROTOTYPE look (PROTO_buckets_example.png):
 * per-day prior H/L/C (YH/YL black-dotted, YC green-solid), full-width prior
 * week (Carolina blue) / month (red) gated to view, and two ET dividers —
 * 9:59 AM (orange) + 4:59 PM day-end (jet black). Handled by a dedicated branch in
 * drawOverlay; 1m/5m/4h/daily keep their own (1H/4H or 1D/1W/1M) rendering. */
const PROTO_TF = ['15m', '30m', '1h'];
const PROTO = {
  dayHL:  { color: '#000000',             lw: 3, dash: [2, 3] },  // YH / YL
  dayC:   { color: 'rgba(27,138,58,0.95)', lw: 3, dash: [] },     // YC
  week:   '#4B9CD3',   // Carolina blue
  month:  '#d32f2f',   // red
  amDiv:  '#795548',               // 9:59 AM ET — brown (10AM-hour eye-guide, NOT a day divider)
  pmDiv:  '#000000',               // 4:59 PM ET (day end)
};

/* Static reference price-line meta (Appendix A.6). */
function levelMeta() {
  return {
    // Label text matches the PROTO 15m/30m/1h chart convention so 1m/5m read
    // apples-to-apples with the higher TFs: Y* = prior day, W* = prior week,
    // M* = prior month, suffix H/L/C.
    PMH: { c: '#D32F2F', t: 'PMH', ls: LC.LineStyle.Dotted, lw: 3 },
    PML: { c: '#D32F2F', t: 'PML', ls: LC.LineStyle.Dotted, lw: 3 },
    PMC: { c: '#2e7d32', t: 'PMC', ls: LC.LineStyle.Solid,  lw: 3 },
    PWH: { c: '#4B9CD3', t: 'PWH', ls: LC.LineStyle.Dotted, lw: 3 },
    PWL: { c: '#4B9CD3', t: 'PWL', ls: LC.LineStyle.Dotted, lw: 3 },
    PWC: { c: '#16a34a', t: 'PWC', ls: LC.LineStyle.Solid,  lw: 3 },
    // CURRENT (in-progress) month + week H/L — same color family as prior, but SOLID
    // (prior is dotted) so the live range stands apart. Backend already deduped to highest TF.
    CMH: { c: '#D32F2F', t: 'CMH', ls: LC.LineStyle.Solid, lw: 3 },
    CML: { c: '#D32F2F', t: 'CML', ls: LC.LineStyle.Solid, lw: 3 },
    CWH: { c: '#4B9CD3', t: 'CWH', ls: LC.LineStyle.Solid, lw: 3 },
    CWL: { c: '#4B9CD3', t: 'CWL', ls: LC.LineStyle.Solid, lw: 3 },
    PDH: { c: '#000000', t: 'YH', ls: LC.LineStyle.Dotted, lw: 3 },   // YH jet black (matches DAY_LEVEL_COLOR/PROTO)
    PDL: { c: '#000000', t: 'YL', ls: LC.LineStyle.Dotted, lw: 3 },   // YL jet black
    PDC: { c: '#2e7d32', t: 'YC', ls: LC.LineStyle.Solid,  lw: 3 },
    // prior 2-5 day highs/lows — recent swing magnets, fading grey by age (older = fainter)
    '2DH': { c: '#5b6470', t: '2DH', ls: LC.LineStyle.Dashed, lw: 1 },
    '2DL': { c: '#5b6470', t: '2DL', ls: LC.LineStyle.Dashed, lw: 1 },
    '3DH': { c: '#828b97', t: '3DH', ls: LC.LineStyle.Dashed, lw: 1 },
    '3DL': { c: '#828b97', t: '3DL', ls: LC.LineStyle.Dashed, lw: 1 },
    '4DH': { c: '#a9b0ba', t: '4DH', ls: LC.LineStyle.Dashed, lw: 1 },
    '4DL': { c: '#a9b0ba', t: '4DL', ls: LC.LineStyle.Dashed, lw: 1 },
    '5DH': { c: '#c7ccd3', t: '5DH', ls: LC.LineStyle.Dashed, lw: 1 },
    '5DL': { c: '#c7ccd3', t: '5DL', ls: LC.LineStyle.Dashed, lw: 1 },
    // prior 2-3 WEEKS ago H/L (blue, fading with age) — beyond the immediate prior week (WH/WL)
    '2WH': { c: '#4B9CD3', t: '2W-H', ls: LC.LineStyle.Dashed, lw: 1 },
    '2WL': { c: '#4B9CD3', t: '2W-L', ls: LC.LineStyle.Dashed, lw: 1 },
    '3WH': { c: '#86b9dd', t: '3W-H', ls: LC.LineStyle.Dashed, lw: 1 },
    '3WL': { c: '#86b9dd', t: '3W-L', ls: LC.LineStyle.Dashed, lw: 1 },
    // prior 2-3 MONTHS ago H/L (red, fading with age) — beyond the immediate prior month (MH/ML)
    '2MH': { c: '#D32F2F', t: '2M-H', ls: LC.LineStyle.Dashed, lw: 1 },
    '2ML': { c: '#D32F2F', t: '2M-L', ls: LC.LineStyle.Dashed, lw: 1 },
    '3MH': { c: '#e57373', t: '3M-H', ls: LC.LineStyle.Dashed, lw: 1 },
    '3ML': { c: '#e57373', t: '3M-L', ls: LC.LineStyle.Dashed, lw: 1 },
  };
}
// Week/month + prior 2-5 day levels: full-width price lines, view-gated (skip when off-screen)
const WEEKLY_MONTHLY = new Set(['PWH', 'PWL', 'PWC', 'PMH', 'PML', 'PMC',
  'CWH', 'CWL', 'CMH', 'CML',
  '2DH', '2DL', '3DH', '3DL', '4DH', '4DL', '5DH', '5DL',
  '2WH', '2WL', '3WH', '3WL', '2MH', '2ML', '3MH', '3ML']);
// KEY reference levels the price axis should ADAPTIVELY include so they stay visible
// on every timeframe (2026-06-15) — current + prior week/month H/L/C and prior
// day H/L/C. NOT the far swing magnets (2-5D/2-3W/2-3M). These are also exempt from
// the addLevels off-screen skip so they're always drawn (the axis shows them when
// within the adaptive cap; a far one simply clips without squishing the candles).
const AXIS_LEVELS = new Set(['CMH', 'CML', 'CWH', 'CWL',
  'PMH', 'PML', 'PMC', 'PWH', 'PWL', 'PWC', 'PDH', 'PDL', 'PDC']);
// Prior-DAY levels are NOT drawn as full-width price lines — they're drawn on the
// overlay across the CURRENT day only (so yesterday's H/L/C sit on today's candles
// instead of stretching all the way to the left). Week/month stay full-width.
const DAY_LEVELS = new Set(['PDH', 'PDL', 'PDC']);
// Gap kept to the RIGHT of the newest candle so the level pills never cover it — expressed as
// a FRACTION of the visible window so the on-screen distance is the SAME on every timeframe
// (a fixed bar count looks tiny on 1m but huge on 15m). Tune this one number.
const GAP_FRAC = 0.05;
// YH/YL = JET BLACK on every timeframe (2026-06-09) — matches the PROTO
// (15m/30m/1h) dayHL black so prior-day H/L read identically across all TFs. PDC stays green.
const DAY_LEVEL_COLOR = { PDH: '#000000', PDL: '#000000', PDC: '#2e7d32' };

const ET = 'America/New_York';
function fmtET(ms) {
  return new Date(ms).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: ET });
}

/* Small DOM helper — avoids innerHTML (no untrusted markup, XSS-safe). */
function el(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) Object.keys(attrs).forEach((k) => {
    if (k === 'text') e.textContent = attrs[k]; else e.setAttribute(k, attrs[k]);
  });
  return e;
}

/* ================================================================== *
 * Quadrant factory
 * ================================================================== */
function makeQuadrant(containerEl, ticker, tf, opts) {
  opts = opts || {};
  // Bucket toggle defaults (2026-06-05): 1H dividers+levels OFF on ALL TFs; 4H ON only on the
  // 1-minute (off elsewhere). A saved layout's explicit per-quadrant state still wins.
  // BUCKETS_1H_4H_ENABLED=false → force both OFF regardless of saved layout.
  const def1h = !BUCKETS_1H_4H_ENABLED ? false
              : (opts.show1H != null) ? opts.show1H : false;
  const def4h = !BUCKETS_4H_ENABLED ? false
              : (opts.show4H != null) ? opts.show4H : true;

  // ---- DOM scaffold (built with safe DOM methods, no innerHTML) ----
  const root = el('div', 'quadrant');

  const controls  = el('div', 'q-controls');
  const tickerSel = el('select', 'q-ticker');           // kept (detached) so .value reads 'NQ'; not shown
  const tickerLbl = el('span', 'q-ticker-static', { text: 'NQ' });  // static label — NASDAQ only, no dropdown
  const tfSel     = el('select', 'q-tf');
  // Rolling look-back: show only the last N trading days (current day = rightmost
  // bucket). "All" removes the filter. Applies on top of whatever timeframe is active.
  const daysSel   = el('select', 'q-days', { title: 'Rolling look-back — show only the last N trading days (current day on the right). "All" removes the filter.' });
  // Moving-average SET toggles — EMA stack / MA pair. Mutually
  // exclusive: turning one on turns the other off; clicking the active one = off.
  const sbBtn = el('button', 'q-bkt q-ma', { title: MA_SETS.sb.title, text: MA_SETS.sb.label, type: 'button' });
  const ovBtn = el('button', 'q-bkt q-ma', { title: MA_SETS.ov.title, text: MA_SETS.ov.label, type: 'button' });
  // TEMPORARY: hide + disable the SB / OV buttons so they can't be pressed.
  if (MA_BUTTONS_DISABLED) {
    sbBtn.style.display = ovBtn.style.display = 'none';
    sbBtn.disabled = ovBtn.disabled = true;
  }
  // EMA-stack button hidden by default — hide + disable just that button; MA pair stays.
  if (!SB_ENABLED) {
    sbBtn.style.display = 'none';
    sbBtn.disabled = true;
  }
  // Bucket toggles — show/hide the 1H and 4H vertical dividers + horizontal levels.
  const bkt1hBtn = el('button', 'q-bkt' + (def1h ? ' active' : ''), { title: 'Toggle 1H dividers + levels', text: '1H' });
  const bkt4hBtn = el('button', 'q-bkt' + (def4h ? ' active' : ''), { title: 'Toggle 4H dividers + levels', text: '4H' });
  // TEMPORARY: 1H/4H buckets removed by request — hide + disable both buttons
  // (focusing on Daily/Weekly/Monthly only). Flip BUCKETS_1H_4H_ENABLED to restore.
  if (!BUCKETS_1H_4H_ENABLED) {                  // 1H button stays hidden/disabled
    bkt1hBtn.style.display = 'none'; bkt1hBtn.disabled = true;
  }
  if (!BUCKETS_4H_ENABLED) {                     // 4H button restored independently
    bkt4hBtn.style.display = 'none'; bkt4hBtn.disabled = true;
  }
  // Session shading toggle — Asia (blue) / London (pink) / New York (yellow) high-low boxes.
  const sessBtn = el('button', 'q-bkt active', { title: 'Toggle session shading — Asia 7–11 PM, London 1–5 AM, New York 7–11 AM ET (high-low boxes)', text: 'SES' });
  const followBtn = el('button', 'q-follow active', { title: 'Auto-scroll the window to the newest candle', text: '▶ Follow' });
  // Crosshair toggle — GLOBAL: hides/shows the crosshair lines on EVERY chart and
  // window (quadrants, pop-outs, maximized). Active class = crosshair shown.
  const crossBtn = el('button', 'q-bkt' + (loadCrosshairOn() ? ' active' : ''),
    { title: 'Toggle crosshair lines — applies to all charts and windows', text: '✛ Cross' });
  const fibBtn    = el('button', 'q-fib', { title: 'Range-expansion tool: click the 100% level, then the 0% level. Draws 100% expansions to 2500%. Click again to clear.', text: '📐 Fib' });
  const lineBtn   = el('button', 'q-line', { title: 'Horizontal line: turn ON, then click two points to draw a blue level (price = first click; the two clicks set the length) — keep clicking pairs to add more. Turn OFF to drag endpoints (resize). Right-click a line to delete it.', text: '╍ Line' });
  const spacer    = el('span', 'spacer');
  const badgeEl   = el('span', 'q-badge loading', { text: 'connecting' });
  const maxBtn    = el('button', 'q-max', { title: 'Maximize / restore', text: '⛛' });
  const popBtn    = el('button', 'q-pop', { title: 'Pop out to a new window', text: '⧉' });
  // maxBtn/popBtn placed right after the dropdowns so they're ALWAYS on the
  // first line (never clipped/wrapped off in a narrow quadrant). The dots/toggles
  // wrap below if the strip is too tight.
  controls.append(tickerSel, tfSel, daysSel, maxBtn, popBtn, sbBtn, ovBtn, bkt1hBtn, bkt4hBtn, sessBtn, followBtn, crossBtn, fibBtn, lineBtn, spacer, badgeEl);

  const wrap      = el('div', 'q-wrap');
  const chartEl   = el('div', 'q-chart');
  const overlay   = el('canvas', 'q-overlay');
  const overlayCtx = overlay.getContext('2d');
  const errEl     = el('div', 'q-error');

  // In-chart attribution watermark — TradingView credit only (Lightweight
  // Charts™ license requirement). The footer caption lives in the footer strip
  // below, so it's not duplicated here.
  const brandOverlay = el('div', 'q-brand-overlay');
  const tvLink = el('a', null, {
    href: 'https://www.tradingview.com/lightweight-charts/', target: '_blank',
    title: 'Charts powered by Lightweight Charts™ — TradingView',
    text: 'powered by TradingView' });
  brandOverlay.append(tvLink);
  const legend = el('div', 'q-legend');
  wrap.append(chartEl, overlay, legend, brandOverlay, errEl);

  // ---- footer (every view): candle timeframe caption ---------------------
  const footer = el('div', 'q-footer');
  const footTf = el('span', 'q-footer-tf', { text: tfFooterLabel(tf) });
  footer.append(footTf);

  root.append(controls, wrap, footer);
  containerEl.appendChild(root);

  TICKERS.forEach((t) => {
    tickerSel.appendChild(el('option', null, { value: t, text: t }));
  });
  TIMEFRAMES.forEach((pair) => {
    tfSel.appendChild(el('option', null, { value: pair[0], text: pair[1] }));
  });
  [['all', 'All'], ['1', '1d'], ['2', '2d'], ['3', '3d'], ['4', '4d'], ['5', '5d']].forEach((pair) => {
    daysSel.appendChild(el('option', null, { value: pair[0], text: pair[1] }));
  });

  // ---- per-quadrant state -----------------------------------------
  const st = {
    id: opts.id != null ? opts.id : 0,
    ticker: ticker,
    tf: tf,
    // Rolling look-back filter: null = All (no filter), else show only the last N
    // trading sessions (today = rightmost). Driven by the "Days" dropdown; survives
    // ticker/timeframe switches so it applies to whatever you're viewing.
    rollDays: opts.rollDays != null ? opts.rollDays : ((tf === '1h' || tf === '30m') ? 5 : null),  // 1h/30m: 5-day window default
    // MAs off by default on 5m/1m and on the month-grid TFs (1d/4h match the
    // clean annual month-grid look — no moving averages). Toggle on via the dots.
    maSet: MA_BUTTONS_DISABLED ? null   // TEMPORARY: MAs forced off while buttons are hidden
         : (() => {
             const m = maSetFrom(opts.maSet, opts.ind,
                                 (tf === '5m' || tf === '1m' || tf === '1d' || tf === '4h') ? null : MA_DEFAULT);
             return (m === 'sb' && !SB_ENABLED) ? null : m;   // SB removed → drop stale 'sb' to off
           })(),
    show1H: def1h,                      // default ON only on 1m (off elsewhere)
    show4H: def4h,                      // 4H toggle restored (BUCKETS_4H_ENABLED), default ON
    showSessions: opts.showSessions !== false,  // default ON: Asia/London/NY session high-low boxes
    follow: opts.follow !== false,      // default ON: window tracks the newest candle
    zoomRange: opts.zoomRange || null,
    // Caller-supplied initial zoom range survives the first natural visibleRangeChange
    // event that LWC fires during chart init (which would otherwise clobber zoomRange
    // before applySnapshot reads it). Consumed once in applySnapshot.
    pendingInitialZoom: opts.zoomRange || null,
    // Hard left-edge cutoff (unix seconds). When set, applySnapshot drops any
    // bars (real + whitespace) older than this time before setData, so the
    // chart genuinely contains zero bars before cropFrom — user can't pan
    // back to see them. Used by the dual-view top panel to enforce
    // "current ISO week Mon-onwards only".
    cropFrom: opts.cropFrom || null,
    isMaximized: !!opts.isMaximized,
    generation: 0,                 // bumped on every ticker/tf switch (spec §5.2)
    snapshotVersion: -1,           // reject older cold frames (spec §5.5)
    lastSeq: 0,                    // dedup by sequence_id (Task 14 step 3)
    // Range-expansion ("Fib") tool: click sets the 100% anchor, second click the
    // 0% anchor; the tool draws 100%-of-range expansion lines from 0% to 2500%.
    fib: null,                     // {a:price@100%, b:price@0%} when placed
    fibArming: 0,                  // 0 idle, 1 awaiting 100% click, 2 awaiting 0% click
    fibFirst: null,                // price captured by the first click
    // Horizontal-line tool: blue rgb(0,0,255) lw-2 segments. Two clicks place a
    // line (price = first click; the two clicks set the x-extent/length); grab an
    // endpoint to resize. Lines anchored by TIME (ms) so they track pan/zoom.
    lines: loadLines(ticker),      // [{price, t1, t2}] — shared per-ticker across all charts
    lineArming: false,             // true while awaiting clicks
    lineFirst: null,               // {price, t} after the first click
    lineDrag: null,                // {idx, which:'t1'|'t2'} during an endpoint drag
    lineHit: [],                   // [{idx,which,x,y}] endpoint hitboxes (rebuilt each draw)
    lineBodies: [],                // [{idx,y,x1,x2}] line-body hitboxes (for right-click delete)
    lineHover: null,               // idx of the line under the cursor (shows its endpoint handles)
  };

  let chart = null, candleSeries = null;
  let crosshairOn = loadCrosshairOn();   // GLOBAL crosshair show/hide (shared across windows)
  let maSeries = null;   // { sb: {key:series,...}, ov: {key:series,...} } — one line per (set,key); one set visible at a time

  // Apply the (global) crosshair show/hide to this chart + reflect the button.
  // Hides only the crosshair LINES + their axis labels; subscribeCrosshairMove
  // still fires, so the OHLC legend keeps updating on hover when lines are off.
  function applyCrosshair(show) {
    crosshairOn = !!show;
    crossBtn.classList.toggle('active', crosshairOn);
    if (chart) {
      chart.applyOptions({ crosshair: {
        vertLine: { visible: crosshairOn, labelVisible: crosshairOn },
        horzLine: { visible: crosshairOn, labelVisible: crosshairOn },
      } });
    }
  }
  let priceLines = [];
  let levelsData = {};   // raw snap.levels (PW*/PM* values) for the PROTO branch
  let levelTitles = {};  // optional per-level display titles (e.g. 2DL -> "MON-L"); from snap.level_titles
  let boundaries = {};
  let segments = [];
  let maintenanceGaps = [];   // [{start_ms, end_ms}] 17:00-18:00 ET futures-maint gaps
  let barTimesSec = [];   // ascending bar times (seconds) for snapping dividers to bars
  let barHigh = [], barLow = [], barClose = [];   // parallel to chart bars — autoscale + session-CIB close
  let sessionBoxes = [];  // [{_key, t0, t1, hi, lo, hiT, loT, color, line, lbl}] Asia/London/NY high-low shading boxes
  // Deep-history scroll-back (to 2010): bars fetched from /api/history when the
  // user scrolls near the left edge, OLDER than the live snapshot window. Merged
  // into the chart in applySnapshot so live cold updates don't wipe the view.
  // Dormant (empty) during normal live use → zero change to existing behavior.
  let histBars = [];
  let histFetching = false, histExhausted = false;
  let recentStartIdx = 0;   // index in the merged array where the live snapshot window starts
                            //   (overlays/session boxes only apply at/after this)
  let allBarsRef = [];      // full merged candle array (hist + live) — for the client-side month grid

  // Bucket one real bar into its session window's high-low box (or start a new one).
  // Called for each bar in applySnapshot and for every live bar update. Within a
  // single bar, high only rises and low only falls, so extending (never shrinking)
  // the box on same-bar 1s updates stays correct.
  function ingestSessionBar(ms, hi, lo, cl) {
    if (SESSION_TFS.indexOf(st.tf) === -1) return;     // only intraday TFs have session windows
    if (hi == null || lo == null || isNaN(hi) || isNaN(lo)) return;
    const dh = etDateHour(ms);
    const w = sessionWindowFor(dh.hour * 60 + dh.minute);
    if (!w) return;
    const key = dh.date + '|' + w.key;
    const last = sessionBoxes.length ? sessionBoxes[sessionBoxes.length - 1] : null;
    if (last && last._key === key) {                   // extend the in-progress box
      last.t1 = ms;
      // Track WHICH bar set each extreme so the H/L line can start at that candle.
      if (hi > last.hi) { last.hi = hi; last.hiT = ms; }
      if (lo < last.lo) { last.lo = lo; last.loT = ms; }
      if (cl != null && !isNaN(cl)) last.cl = cl;       // running close = last bar's close = session close
    } else {
      sessionBoxes.push({ _key: key, t0: ms, t1: ms, hi: hi, lo: lo, cl: (cl != null && !isNaN(cl)) ? cl : null,
                          hiT: ms, loT: ms, color: w.color, line: w.line, lbl: w.lbl });
    }
  }
  // Full rebuild from the current bar arrays (called after each cold snapshot).
  function rebuildSessionBoxes() {
    sessionBoxes = [];
    if (SESSION_TFS.indexOf(st.tf) === -1) return;
    // Session shading spans ALL loaded bars — including lazy scroll-back history — so the
    // Asia/London/NY boxes don't vanish when you scroll left. Box count grows only with how far
    // you've scrolled (each lazy chunk is bounded), and the draw loop culls off-screen boxes by x,
    // so it stays cheap. (The session H/L LINES still use slice(-N) = recent sessions only.)
    for (let i = 0; i < barTimesSec.length; i++) {
      if (isNaN(barHigh[i]) || isNaN(barLow[i])) continue;   // skip whitespace bars
      ingestSessionBar(barTimesSec[i] * 1000, barHigh[i], barLow[i], barClose[i]);
    }
  }

  // Current (in-progress) 1H/4H bucket high-low. Returns {t0,t1,hi,lo} for the bucket
  // the latest bar sits in, scanning only that bucket's bars so it recomputes cheaply
  // every frame → the lines expand live as the range develops. The bucket START is
  // taken from the server's own 1H/4H dividers (boundaries[key]) so it always aligns
  // with the existing levels; if live bars cross a divider before the next cold
  // snapshot refreshes boundaries, we advance by whole periods to find the new start.
  function currentBucketRange(key, periodMs) {
    if (INTRADAY_TF.indexOf(st.tf) === -1) return null;
    const list = boundaries[key];
    if (!list || !list.length || !lastBarMs || !barTimesSec.length) return null;
    let start = null;
    for (let i = 0; i < list.length; i++) {
      const ms = list[i];
      if (ms <= lastBarMs && (start == null || ms > start)) start = ms;
    }
    if (start == null) return null;
    if (lastBarMs - start >= periodMs) {                 // rolled past last known divider
      start += Math.floor((lastBarMs - start) / periodMs) * periodMs;
    }
    let hi = -Infinity, lo = Infinity, t0 = lastBarMs;
    for (let i = barTimesSec.length - 1; i >= 0; i--) {
      const ms = barTimesSec[i] * 1000;
      if (ms < start) break;
      const h = barHigh[i], l = barLow[i];
      if (isNaN(h) || isNaN(l)) continue;
      if (h > hi) hi = h;
      if (l < lo) lo = l;
      t0 = ms;
    }
    if (hi === -Infinity) return null;
    return { t0: t0, t1: lastBarMs, hi: hi, lo: lo };
  }
  let lastBarMs = 0, lastBarLogicalIdx = 0;
  const INTRADAY_TF = ['1m', '5m', '15m', '30m'];
  let candleYLo = 0, candleYHi = 0;
  let appliedFirstZoom = false;
  let lastSnap = null;   // most recent raw snapshot, so the Days filter can re-crop without a refetch
  let lastOHLC = null;        // last/forming bar OHLC for the crosshair legend

  function _fmtPx(v) {
    return (v == null || !isFinite(v)) ? '–'
      : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function setLegend(b) {
    legend.textContent = '';
    if (!b) return;
    const dir = b.close >= b.open ? '#26a69a' : '#ef5350';
    const head = document.createElement('span');
    head.textContent = (st.contract || st.ticker) + ' ' + st.tf + '  ';
    head.style.fontWeight = '700';
    legend.appendChild(head);
    const labels = [['O', b.open], ['H', b.high], ['L', b.low], ['C', b.close]];
    labels.forEach(([k, v], i) => {
      const s = document.createElement('span');
      s.textContent = k + ' ' + _fmtPx(v) + (i < labels.length - 1 ? '  ' : '');
      if (k === 'C') s.style.color = dir;
      legend.appendChild(s);
    });
  }

  // ---- chart creation (Appendix A config — ported verbatim) -------
  function createChart() {
    chart = LC.createChart(chartEl, {
      autoSize: true,
      layout: { background: { color: '#ffffff' }, textColor: '#000000', fontSize: 12,
                attributionLogo: false },
      localization: {
        locale: 'en-US',
        timeFormatter: (time) => {
          const d = new Date(time * 1000);
          return d.toLocaleString('en-US', {
            weekday: 'short', month: 'short', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false, timeZone: ET }) + ' ET';
        },
      },
      // No background grid lines — only explicitly coded levels should
      // appear as horizontal lines (no stray gray price-grid lines).
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      timeScale: {
        visible: true, timeVisible: true, secondsVisible: false,
        borderColor: '#000000', borderVisible: true,
        // Auto-follow newest candle by default: leave room to the right and
        // shift the visible range as new bars print (toggle via ▶ Follow).
        rightOffset: st.follow ? 8 : 4,   // placeholder; set proportionally once the snapshot's zoom is known
        shiftVisibleRangeOnNewBar: st.follow,
        // Intraday: suppress native labels (custom :30 labels in overlay, A.2).
        // Higher TFs (1h/4h/1d): show native date labels ("MMM D") since the
        // overlay :30 labels don't apply. Checks st.tf dynamically so it adapts
        // when the timeframe changes without recreating the chart.
        tickMarkFormatter: (time, tickMarkType) => {
          // Native HH:MM / MMM-D labels on every TF so the time axis is always
          // readable when zoomed in. The overlay's specialized day names / 4:59
          // PM markers sit above the axis and complement these.
          const d = new Date(time * 1000);
          // tickMarkType: 0 Year, 1 Month, 2 DayOfMonth, 3 Time, 4 TimeWithSeconds.
          if (tickMarkType >= 3) {   // time tick (1h/4h intraday-of-day) → HH:MM ET
            return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit',
                                                   hour12: false, timeZone: ET });
          }
          return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: ET });
        },
      },
      rightPriceScale: { borderColor: '#000000', borderVisible: true,
                         scaleMargins: { top: 0.06, bottom: 0.06 } },
      crosshair: { mode: LC.CrosshairMode.Normal },
      handleScroll: { mouseWheel: true, pressedMouseMove: true,
                      horzTouchDrag: true, vertTouchDrag: true },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true,
                     axisDoubleClickReset: true },
    });
    chart.timeScale().subscribeVisibleTimeRangeChange(onVisibleRangeChange);
    // Deep-history lazy-load: when the user scrolls within ~30 bars of the left
    // edge, fetch the next older chunk (back toward 2010) and prepend it.
    chart.timeScale().subscribeVisibleLogicalRangeChange((lr) => {
      if (lr && lr.from < 30) maybeFetchOlder();
    });
    buildSeries();
    applyCrosshair(crosshairOn);   // honor the shared crosshair on/off state at boot
    // Crosshair tooltip: show hovered bar's OHLC; fall back to the latest bar.
    chart.subscribeCrosshairMove((param) => {
      const d = param && param.seriesData ? param.seriesData.get(candleSeries) : null;
      if (d && isFinite(d.open)) setLegend(d);
      else if (lastOHLC) setLegend(lastOHLC);
    });
    // Range-expansion ("Fib") tool: while armed, the next two clicks set the
    // 100% then 0% anchors. coordinateToPrice turns the click's y into a price.
    chart.subscribeClick((param) => {
      if (!st.fibArming || !param || !param.point || !candleSeries) return;
      const price = candleSeries.coordinateToPrice(param.point.y);
      if (price == null) return;
      if (st.fibArming === 1) {
        st.fibFirst = price;          // 100% anchor
        st.fibArming = 2;
      } else {
        st.fib = { a: st.fibFirst, b: price };   // a=100%, b=0%
        st.fibArming = 0; st.fibFirst = null;
      }
      syncFibBtn();
      requestAnimationFrame(drawOverlay);
    });

    // ---- Horizontal-line tool: place points + drag endpoints -----------
    // All handled on a capture-phase mousedown so we can claim the gesture BEFORE
    // the chart starts a pan. When armed, each press places a point (price = first
    // press; the two presses set the x-extent); otherwise a press near an endpoint
    // starts a resize drag.
    wrap.addEventListener('mousedown', (e) => {
      if (!candleSeries) return;
      const r = wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      if (st.lineArming) {
        const price = candleSeries.coordinateToPrice(my);
        const t = msFromX(mx);
        if (price == null || t == null) return;
        e.stopPropagation(); e.preventDefault();
        if (!st.lineFirst) {
          st.lineFirst = { price: price, t: t };      // line price = first press
        } else {
          const t1 = Math.min(st.lineFirst.t, t), t2 = Math.max(st.lineFirst.t, t);
          st.lines.push({ price: st.lineFirst.price, t1: t1, t2: t2 });
          st.lineFirst = null;
          // STAY armed → keep drawing more lines until the button is toggled off.
          commitLines();        // share to every chart of this ticker
        }
        syncLineBtn();
        requestAnimationFrame(drawOverlay);
        return;
      }
      // cursor mode: grab an ENDPOINT (resize) or the BODY (move the whole line).
      const ep = st.lineHit.find((h) => Math.abs(h.x - mx) <= 8 && Math.abs(h.y - my) <= 8);
      if (ep) {
        e.stopPropagation(); e.preventDefault();
        st.lineDrag = { idx: ep.idx, which: ep.which };
        chart.applyOptions({ handleScroll: false, handleScale: false });
        return;
      }
      const bd = st.lineBodies.find((b) =>
        Math.abs(b.y - my) <= 6 &&
        mx >= Math.min(b.x1, b.x2) - 2 && mx <= Math.max(b.x1, b.x2) + 2);
      if (bd) {
        const ln = st.lines[bd.idx];
        e.stopPropagation(); e.preventDefault();
        st.lineDrag = { idx: bd.idx, which: 'move',
                        t0: msFromX(mx), p0: candleSeries.coordinateToPrice(my),
                        ot1: ln.t1, ot2: ln.t2, op: ln.price };
        chart.applyOptions({ handleScroll: false, handleScale: false });
        return;
      }
      // clicked empty space → not on a line: leave lines as-is, let the chart pan.
    }, true);
    window.addEventListener('mousemove', (e) => {
      if (!st.lineDrag) return;
      const r = wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const ln = st.lines[st.lineDrag.idx];
      if (!ln) return;
      const d = st.lineDrag;
      if (d.which === 'move') {                        // translate the whole line (x + price)
        const t = msFromX(mx), pr = candleSeries.coordinateToPrice(my);
        if (t == null || pr == null) return;
        const dt = t - d.t0, dp = pr - d.p0;
        ln.t1 = d.ot1 + dt; ln.t2 = d.ot2 + dt; ln.price = d.op + dp;
      } else {                                          // resize: move only this endpoint (price fixed)
        const t = msFromX(mx);
        if (t == null) return;
        ln[d.which] = t;
        if (ln.t1 > ln.t2) {                            // dragged past the other end → swap
          const tmp = ln.t1; ln.t1 = ln.t2; ln.t2 = tmp;
          d.which = d.which === 't1' ? 't2' : 't1';
        }
      }
      // Live-sync to every other chart DURING the drag (throttled to one frame),
      // so the line moves on all timeframes simultaneously.
      if (!d.syncPending) {
        d.syncPending = true;
        requestAnimationFrame(() => {
          if (st.lineDrag) st.lineDrag.syncPending = false;
          drawOverlay();
          commitLines();
        });
      }
    });
    window.addEventListener('mouseup', () => {
      if (!st.lineDrag) return;
      st.lineDrag = null;
      chart.applyOptions({
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
        handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true, axisDoubleClickReset: true },
      });
      commitLines();          // share the resized/moved line to every chart of this ticker
    });
    // Right-click a line (endpoint or anywhere along it) to delete it.
    wrap.addEventListener('contextmenu', (e) => {
      const r = wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      let idx = -1;
      const hit = st.lineHit.find((h) => Math.abs(h.x - mx) <= 8 && Math.abs(h.y - my) <= 8);
      if (hit) idx = hit.idx;
      else {
        const body = st.lineBodies.find((bd) =>
          Math.abs(bd.y - my) <= 6 &&
          mx >= Math.min(bd.x1, bd.x2) - 2 && mx <= Math.max(bd.x1, bd.x2) + 2);
        if (body) idx = body.idx;
      }
      if (idx < 0) return;                              // not on a line → normal menu
      e.preventDefault(); e.stopPropagation();
      st.lines.splice(idx, 1);
      st.lineHover = null;
      commitLines();          // removal propagates to every chart of this ticker
      requestAnimationFrame(drawOverlay);
    });
    // Hover: cursor feedback (↔ resize / move) + reveal the hovered line's endpoint
    // handles. Handles disappear when the cursor moves off the line.
    wrap.addEventListener('mousemove', (e) => {
      if (st.lineDrag) return;            // dragging: handles shown via lineDrag
      const r = wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const ep = st.lineHit.find((h) => Math.abs(h.x - mx) <= 8 && Math.abs(h.y - my) <= 8);
      let idx = ep ? ep.idx : null;
      if (idx == null) {
        const bd = st.lineBodies.find((b) =>
          Math.abs(b.y - my) <= 6 && mx >= Math.min(b.x1, b.x2) - 2 && mx <= Math.max(b.x1, b.x2) + 2);
        if (bd) idx = bd.idx;
      }
      chartEl.style.cursor = ep ? 'ew-resize' : (idx != null ? 'move' : '');
      if (st.lineArming) idx = null;      // no hover-handles while placing new lines
      if (idx !== st.lineHover) { st.lineHover = idx; requestAnimationFrame(drawOverlay); }
    });
    wrap.addEventListener('mouseleave', () => {
      if (st.lineHover != null) { st.lineHover = null; requestAnimationFrame(drawOverlay); }
    });
  }

  // Pixel-x → time(ms). Uses the chart's own mapping (snaps within data); projects
  // by logical index in the future-gap to the right of the last bar.
  function msFromX(x) {
    if (!chart) return null;
    const tscale = chart.timeScale();
    const t = tscale.coordinateToTime(x);
    if (t != null) return t * 1000;
    const lg = tscale.coordinateToLogical(x);
    if (lg == null || !lastBarMs || barTimesSec.length < 2) return null;
    const barMs = (barTimesSec[barTimesSec.length - 1] - barTimesSec[barTimesSec.length - 2]) * 1000;
    return lastBarMs + Math.round(lg - lastBarLogicalIdx) * barMs;
  }

  // Persist this ticker's lines to the shared store and notify every other chart
  // (siblings in this window + other windows) so the line appears on all of them.
  function commitLines() {
    saveLines(st.ticker, st.lines);
    if (opts.onLinesChange) opts.onLinesChange(st.ticker, api);
  }
  // Re-read this ticker's shared lines (called when another chart changes them).
  function refreshLines() {
    st.lines = loadLines(st.ticker);
    requestAnimationFrame(drawOverlay);
  }

  function buildSeries() {
    candleSeries = chart.addCandlestickSeries({
      // annual month-grid candle palette: green #089981 / red #f23645, gray wicks.
      upColor: '#089981', downColor: '#f23645',
      borderUpColor: '#089981', borderDownColor: '#f23645',
      wickUpColor: '#9aa1ad', wickDownColor: '#9aa1ad',
      priceLineVisible: true,    // last-price marker (dashed line + axis tag)
      // Autoscale to the visible candles, then ADAPTIVELY pull in the key reference
      // levels (AXIS_LEVELS) so they're visible on every timeframe (2026-06-15:
      // "levels there no matter what timeframe"). The stretch is CAPPED at 0.6x the
      // candle range past each edge, measured from the ORIGINAL candle bounds (no
      // chaining), so candles stay readable — a level far from price (e.g. a month
      // low on a 1m view) still clips instead of squishing the candles.
      autoscaleInfoProvider: () => {
        if (!chart || !barLow.length) return null;
        const lr = chart.timeScale().getVisibleLogicalRange();
        if (!lr) return null;
        const lo = Math.max(0, Math.floor(lr.from));
        const hi = Math.min(barLow.length - 1, Math.ceil(lr.to));
        if (hi < lo) return null;
        let mn = Infinity, mx = -Infinity;
        for (let i = lo; i <= hi; i++) {
          if (barLow[i] < mn) mn = barLow[i];
          if (barHigh[i] > mx) mx = barHigh[i];
        }
        if (!isFinite(mn) || !isFinite(mx)) return null;
        const origMn = mn, origMx = mx, cap = ((mx - mn) || 1) * 0.6;
        if (lastSnap && lastSnap.levels) {
          for (const k of AXIS_LEVELS) {
            const p = lastSnap.levels[k];
            if (p == null || !isFinite(p)) continue;
            if (p < mn && p >= origMn - cap) mn = p;   // nearby level below → include
            if (p > mx && p <= origMx + cap) mx = p;   // nearby level above → include
          }
        }
        const pad = (mx - mn) * 0.08 || 1;
        return { priceRange: { minValue: mn - pad, maxValue: mx + pad } };
      },
    });
    // Indicators are pure overlays: returning null from autoscaleInfoProvider keeps
    // them out of the price-scale autoscale so toggling them never rescales/squishes
    // the candles (most visible on 1m, which has the widest default zoom).
    const noAutoscale = { autoscaleInfoProvider: () => null };
    // One line series per (MA set, key). ema20 lives in BOTH sets (different colors),
    // so each set gets its own series. Visibility is driven by the active set.
    maSeries = {};
    Object.keys(MA_SETS).forEach((set) => {
      maSeries[set] = {};
      MA_SETS[set].lines.forEach((ln) => {
        maSeries[set][ln.key] = chart.addLineSeries({ color: ln.color, lineWidth: MA_WIDTH, lastValueVisible: false, priceLineVisible: false, ...noAutoscale });
      });
    });
    applyMaVisibility();
  }

  function allMaSeries() {
    const out = [];
    if (maSeries) Object.keys(maSeries).forEach((set) => Object.keys(maSeries[set]).forEach((k) => out.push(maSeries[set][k])));
    return out;
  }
  function applyMaVisibility() {
    if (!maSeries) return;
    Object.keys(maSeries).forEach((set) => {
      const on = (st.maSet === set);
      Object.keys(maSeries[set]).forEach((k) => maSeries[set][k].applyOptions({ visible: on }));
    });
  }

  /* Rebuild the candle + overlay line-series set (spec §5.2 — overlay set
   * differs by TF, so on a tf change we tear down and recreate the series). */
  function rebuildSeries() {
    allMaSeries().forEach((s) => { if (s) { try { chart.removeSeries(s); } catch (e) {} } });
    if (candleSeries) { try { chart.removeSeries(candleSeries); } catch (e) {} }
    clearPriceLines();
    buildSeries();
  }

  // ---- price lines (Appendix A.6) ---------------------------------
  function clearPriceLines() {
    priceLines.forEach((l) => { try { candleSeries.removePriceLine(l); } catch (e) {} });
    priceLines = [];
  }
  function addLevels(levels, ylo, yhi) {
    clearPriceLines();
    if (!levels) return;
    const meta = levelMeta();
    const yPad = (yhi - ylo) * 0.10;
    Object.keys(levels).forEach((name) => {
      if (DAY_LEVELS.has(name)) return;   // prior-day levels drawn per-day on overlay
      const price = levels[name];
      const m = meta[name] || { c: '#000000', t: name, ls: LC.LineStyle.Solid, lw: 2 };
      if (WEEKLY_MONTHLY.has(name) && !AXIS_LEVELS.has(name)) {
        if (price < ylo - yPad || price > yhi + yPad) return;   // far magnets only — key levels always drawn
      }
      const pl = candleSeries.createPriceLine({
        price: price, color: m.c, lineWidth: m.lw, lineStyle: m.ls,
        axisLabelVisible: true, title: (levelTitles[name] || m.t) });  // weekday title for 2-5D levels
      priceLines.push(pl);
    });
  }

  // ---- overlay drawing (Appendix A.2 :30-anchored labels + dividers + segs) ----
  function resizeOverlay() {
    overlay.width = wrap.clientWidth;
    overlay.height = wrap.clientHeight;
  }

  function drawOverlay() {
    resizeOverlay();
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    if (!chart || !candleSeries) return;
    const ts = chart.timeScale();
    const W = overlay.width, H = overlay.height;

    // time-ms → pixel x, projecting future timestamps via logical index.
    function timeToX(ms) {
      const x = ts.timeToCoordinate(ms / 1000);
      if (x != null) return x;
      if (!lastBarMs) return null;
      const minsAhead = (ms - lastBarMs) / 60000;
      return ts.logicalToCoordinate(lastBarLogicalIdx + minsAhead);
    }
    // Snap a boundary timestamp to the first bar at/after it. Day/week/month
    // boundary times (e.g. Sun 18:00 ET) are NOT daily-bar times, so
    // timeToCoordinate() returns null for them; snapping to the bar that OPENS
    // that period puts the divider exactly where the new bucket begins.
    function snapXToBar(ms) {
      if (!barTimesSec.length) return null;
      const tsec = ms / 1000;
      let lo = 0, hi = barTimesSec.length - 1, idx = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (barTimesSec[mid] >= tsec) { idx = mid; hi = mid - 1; } else { lo = mid + 1; }
      }
      if (idx === -1) return timeToX(ms);              // beyond last bar → project
      return ts.timeToCoordinate(barTimesSec[idx]);
    }
    // Snap a 4:59 PM day-END mark to a bar. On a weekday the next bar is the
    // 18:00 reopen ~1h later → snap forward to it (unchanged). But across a
    // weekend/holiday the next bar is Monday's open (~49h away), which would drag
    // the Friday divider off the Fri/Mon seam onto Monday's first candle (it then
    // reads as "no separator between Fri and Mon"). When the forward gap is large
    // (no same-evening reopen), snap to the LAST bar at/before the mark instead,
    // so the divider sits right at the Friday close. Generalises to holiday
    // long-weekends (the last trading day before the break gets the clean line).
    function snapDayEnd(ms) {
      if (!barTimesSec.length) return null;
      const tsec = ms / 1000;
      let lo = 0, hi = barTimesSec.length - 1, after = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (barTimesSec[mid] >= tsec) { after = mid; hi = mid - 1; } else { lo = mid + 1; }
      }
      if (after === -1) {                              // mark past last loaded bar (Fri closed,
        return ts.timeToCoordinate(barTimesSec[barTimesSec.length - 1]);  // Mon not open) → last bar
      }
      const gapHrs = (barTimesSec[after] - tsec) / 3600;
      if (gapHrs > 6 && after > 0) {                   // weekend/holiday — no same-evening reopen
        return ts.timeToCoordinate(barTimesSec[after - 1]);   // → Friday's last bar (the close)
      }
      return ts.timeToCoordinate(barTimesSec[after]);  // weekday — the 18:00 reopen (unchanged)
    }
    // Precise time→x for ANY timestamp (not just exact bar times): interpolate by
    // wall-clock time between the surrounding bars via the logical scale. This is
    // what makes a user-drawn line land at the SAME time/price on every timeframe
    // (a 1m timestamp like 10:07 isn't a 5m bar, so timeToCoordinate() returns null
    // there — interpolation places it correctly instead of mis-projecting).
    function lineX(ms) {
      const n = barTimesSec.length;
      if (!n) return null;
      const tsec = ms / 1000;
      let lo = 0, hi = n - 1, i = -1;                  // rightmost bar with time <= tsec
      while (lo <= hi) { const m = (lo + hi) >> 1; if (barTimesSec[m] <= tsec) { i = m; lo = m + 1; } else hi = m - 1; }
      // Pick two bracketing bars and INTERPOLATE IN PIXEL SPACE. (LWC's
      // logicalToCoordinate only accepts INTEGER indices — a fractional one
      // returns 0/garbage, which mis-placed lines on coarser timeframes.)
      let a, b;
      if (i < 0) { a = 0; b = 1; }                     // before first → extrapolate from 0,1
      else if (i >= n - 1) { a = n - 2; b = n - 1; }   // at/after last → extrapolate from last two
      else { a = i; b = i + 1; }                        // between bars i and i+1
      if (a < 0) a = 0;
      if (b >= n) b = n - 1;
      const xa = ts.logicalToCoordinate(a), xb = ts.logicalToCoordinate(b);
      if (xa == null || xb == null) return null;
      const gap = barTimesSec[b] - barTimesSec[a];
      const frac = gap > 0 ? (tsec - barTimesSec[a]) / gap : 0;
      return xa + frac * (xb - xa);
    }
    function vlines(msList, color, lw, dash, xfn) {
      if (!msList || !msList.length) return;
      const resolve = xfn || timeToX;
      overlayCtx.strokeStyle = color;
      overlayCtx.lineWidth = lw;
      overlayCtx.setLineDash(dash || []);
      const drawn = new Set();
      msList.forEach((ms) => {
        const x = resolve(ms);
        if (x == null) return;
        const xi = Math.round(x);
        if (drawn.has(xi)) return;                      // avoid double-draw after snapping
        drawn.add(xi);
        overlayCtx.beginPath();
        overlayCtx.moveTo(x + 0.5, 0);
        overlayCtx.lineTo(x + 0.5, H);
        overlayCtx.stroke();
      });
    }

    // ---- Annual MONTH GRID (1d / 4h) -----------------------------
    // Month dividers + PRIOR-month H/L/C, computed from ALL loaded candles so it
    // spans lazy-loaded deep history (not just the live snapshot window). Same
    // look on both TFs: gray dotted dividers, black-dotted prior H/L, green-solid
    // prior Close, month-name labels.
    if (MONTH_GRID_TFS.indexOf(st.tf) !== -1 && allBarsRef.length) {
      const groups = [];
      let cur = null;
      for (let i = 0; i < allBarsRef.length; i++) {
        const b = allBarsRef[i];
        if (b.open == null) continue;                  // skip whitespace
        const ym = tradingYM(b.time);
        if (!cur || cur.ym !== ym.ym) {
          cur = { ym: ym.ym, M: ym.M, t0: b.time, t1: b.time,
                  hi: b.high, lo: b.low, close: b.close };
          groups.push(cur);
        } else {
          cur.t1 = b.time;
          if (b.high > cur.hi) cur.hi = b.high;
          if (b.low  < cur.lo) cur.lo = b.low;
          cur.close = b.close;                         // chronologically last close
        }
      }
      // month dividers (snapped to each month's opening bar)
      vlines(groups.map((g) => g.t0), MONTH_GRID.divider.color,
             MONTH_GRID.divider.lw, MONTH_GRID.divider.dash, snapXToBar);
      // prior-month H/L/C projected across each month bucket
      const monthSeg = (t0, t1, price, sty) => {
        const y = candleSeries.priceToCoordinate(price);
        if (y == null) return;
        const x1 = snapXToBar(t0), x2 = (snapXToBar(t1) ?? timeToX(t1));
        if (x1 == null || x2 == null || x2 <= x1 || x1 > W || x2 < 0) return;
        overlayCtx.strokeStyle = sty.color; overlayCtx.lineWidth = sty.lw;
        overlayCtx.setLineDash(sty.dash);
        overlayCtx.beginPath(); overlayCtx.moveTo(x1, y + 0.5); overlayCtx.lineTo(x2, y + 0.5);
        overlayCtx.stroke();
      };
      for (let i = 1; i < groups.length; i++) {
        const g = groups[i], p = groups[i - 1];
        const xEnd = (i + 1 < groups.length) ? groups[i + 1].t0 : g.t1;   // span to next divider
        monthSeg(g.t0, xEnd, p.hi, MONTH_GRID.hl);
        monthSeg(g.t0, xEnd, p.lo, MONTH_GRID.hl);
        monthSeg(g.t0, xEnd, p.close, MONTH_GRID.close);
      }
      // month-name labels at the top
      overlayCtx.setLineDash([]);
      overlayCtx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
      overlayCtx.textBaseline = 'top'; overlayCtx.textAlign = 'left';
      overlayCtx.fillStyle = MONTH_GRID.label;
      groups.forEach((g) => {
        const x = snapXToBar(g.t0);
        if (x == null || x < -30 || x > W - 4) return;
        overlayCtx.fillText(MON_NAMES[g.M - 1], x + 5, 6);
      });
    }

    // Maintenance-break shading (17:00-18:00 ET) — drawn first so dividers,
    // levels, and candles all sit on top. Light translucent grey reads as a
    // non-trading window without overwhelming the chart.
    if (maintenanceGaps && maintenanceGaps.length) {
      overlayCtx.fillStyle = 'rgba(0, 0, 0, 0.08)';
      maintenanceGaps.forEach((g) => {
        const xs = ts.timeToCoordinate(g.start_ms / 1000);
        const xe = ts.timeToCoordinate(g.end_ms / 1000);
        if (xs == null || xe == null || xe <= xs) return;
        overlayCtx.fillRect(xs, 0, xe - xs, H);
      });
    }

    // Session shading — translucent high-low boxes (Asia/London/NY). Drawn under the
    // dividers/levels/labels below. Each box spans its time window (x) and only the
    // price range traded in that window (y), matching the reference style. Toggle: SES.
    if (st.showSessions && sessionBoxes.length) {
      sessionBoxes.forEach((b) => {
        const x0 = timeToX(b.t0), x1 = timeToX(b.t1);
        const y0 = candleSeries.priceToCoordinate(b.hi), y1 = candleSeries.priceToCoordinate(b.lo);
        if (x0 == null || x1 == null || y0 == null || y1 == null) return;
        if (x1 <= x0 || x1 < 0 || x0 > W) return;
        overlayCtx.fillStyle = b.color;
        overlayCtx.fillRect(x0, y0, x1 - x0, Math.max(1, y1 - y0));
      });
    }

    // Session HIGH/LOW lines — for the current session + the 2 prior rolling sessions,
    // a short-dotted line in the session's color runs from the candle that MADE the
    // high/low out to the right edge (the current candle). So if we're in the NY
    // session you see Asia + London levels pushing in, plus the live NY H/L. Toggle: SES.
    if (st.showSessions && sessionBoxes.length) {
      overlayCtx.font = 'bold 8px -apple-system, BlinkMacSystemFont, sans-serif';
      overlayCtx.textBaseline = 'top';
      overlayCtx.textAlign = 'left';
      const recent = sessionBoxes.slice(-SESSION_LINE_COUNT);
      recent.forEach((b) => {
        [['hi', 'hiT', 'H'], ['lo', 'loT', 'L']].forEach(([pk, tk, suf]) => {
          const y = candleSeries.priceToCoordinate(b[pk]);
          if (y == null || y < 0 || y > H) return;
          let x0 = timeToX(b[tk]);                 // start at the bar that made the extreme
          x0 = (x0 == null) ? 0 : Math.max(0, x0); // clamp if it scrolled off the left
          if (x0 >= W) return;
          overlayCtx.strokeStyle = b.line;
          overlayCtx.lineWidth = 1.5;
          overlayCtx.setLineDash([1, 3]);          // short dots (distinct from dashed prior-day levels)
          overlayCtx.beginPath();
          overlayCtx.moveTo(x0, y + 0.5); overlayCtx.lineTo(W, y + 0.5);
          overlayCtx.stroke();
          overlayCtx.setLineDash([]);
          // Compact right-edge tag, e.g. "AsH" / "LnL" / "NyH".
          if (y > 2 && y < H - 11) {
            const lbl = b.lbl + suf;
            const tw = overlayCtx.measureText(lbl).width;
            const lx = Math.max(2, W - tw - 5);
            overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
            overlayCtx.fillRect(lx - 2, y + 1, tw + 4, 11);
            overlayCtx.fillStyle = b.line;
            overlayCtx.fillText(lbl, lx, y + 2);
          }
        });
      });
    }

    // Current (in-progress) 1H + 4H bucket high/low — faint thin "live range" lines
    // that expand with each new bar. These complement the BOLD prior-bucket levels:
    // 4H light purple (faint version of the 4H purple), 1H light grey (faint 1H black).
    // Honors the same 1H/4H toggles. Recomputed every frame so it tracks live.
    function drawLiveBucket(key, periodMs, color) {
      const r = currentBucketRange(key, periodMs);
      if (!r) return;
      const x0 = timeToX(r.t0), x1 = timeToX(r.t1);
      if (x0 == null || x1 == null || x1 <= x0 || x1 < 0 || x0 > W) return;
      overlayCtx.strokeStyle = color;
      overlayCtx.lineWidth = 1;
      overlayCtx.setLineDash([]);
      [r.hi, r.lo].forEach((p) => {
        const y = candleSeries.priceToCoordinate(p);
        if (y == null) return;
        overlayCtx.beginPath();
        overlayCtx.moveTo(x0, y + 0.5);
        overlayCtx.lineTo(x1, y + 0.5);
        overlayCtx.stroke();
      });
    }
    if (st.show4H) drawLiveBucket('4H', 14400000, 'rgba(156,39,176,0.28)');  // light purple
    if (st.show1H) drawLiveBucket('1H', 3600000,  'rgba(0,0,0,0.22)');       // light grey

    // ===== PROTOTYPE branch (15m/30m) — PROTO_buckets_example.png ============
    // User-drawn horizontal lines (blue rgb(0,0,255), lw 2) with draggable
    // endpoints. Rebuilds st.lineHit (endpoint hitboxes) for the drag handler.
    function drawLines() {
      st.lineHit = []; st.lineBodies = [];
      if (!candleSeries || !st.lines.length) return;
      overlayCtx.save();
      st.lines.forEach((ln, idx) => {
        const y = candleSeries.priceToCoordinate(ln.price);
        if (y == null) return;
        const x1 = lineX(ln.t1), x2 = lineX(ln.t2);    // interpolated → same place on every TF
        if (x1 == null || x2 == null) return;
        st.lineBodies.push({ idx: idx, y: y, x1: x1, x2: x2 });
        overlayCtx.strokeStyle = 'rgb(0,0,255)';
        overlayCtx.lineWidth = 2;
        overlayCtx.setLineDash([]);
        overlayCtx.beginPath();
        overlayCtx.moveTo(x1, y + 0.5); overlayCtx.lineTo(x2, y + 0.5);
        overlayCtx.stroke();
        // Endpoint handles: only DRAWN while this line is hovered or being dragged
        // (they vanish when you move on); always registered as hitboxes so they
        // stay grabbable.
        const showHandles = (idx === st.lineHover) || (st.lineDrag && st.lineDrag.idx === idx);
        [[x1, 't1'], [x2, 't2']].forEach((pair) => {
          st.lineHit.push({ idx: idx, which: pair[1], x: pair[0], y: y });
          if (!showHandles) return;
          overlayCtx.beginPath();
          overlayCtx.arc(pair[0], y, 4, 0, 2 * Math.PI);
          overlayCtx.fillStyle = '#ffffff'; overlayCtx.fill();
          overlayCtx.lineWidth = 2; overlayCtx.strokeStyle = 'rgb(0,0,255)';
          overlayCtx.stroke();
        });
      });
      overlayCtx.restore();
    }

    // Drawn entirely here, then we return so none of the generic 1H/4H/static
    // rendering below runs for these timeframes.
    if (PROTO_TF.indexOf(st.tf) !== -1) {
      drawProto(timeToX, snapXToBar, vlines, W, H, snapDayEnd);
      drawFib();                 // range-expansion lines on top (all timeframes)
      drawLines();               // user horizontal lines on top
      return;
    }

    // Draw dividers in ascending z so session opens sit on top. Day/week/month
    // dividers snap to bars (their boundary times aren't bar times); 1H/4H and
    // session opens use the direct/projected mapping.
    // On the DAILY chart each candle IS one session, so the daily session-open
    // marks (futures_open 18:00 + cash_open 09:30) would land on essentially every
    // candle — a wall of orange/green lines. Suppress them so the Daily shows the
    // same clean week (blue) + month (red) dividers as the 4h.
    const SNAP_KEYS = { '1D': 1, '1W': 1, '1M': 1 };
    // Per-TF divider suppression: Daily drops both session opens (one per candle);
    // 1m/5m drop the 09:30 cash open (no RTH-open vertical line on those TFs).
    const skip = {};
    // Month-grid TFs (1d/4h): clean annual month-grid look — no cash/futures-open lines.
    if (MONTH_GRID_TFS.indexOf(st.tf) !== -1) { skip.cash_open = 1; skip.futures_open = 1; }
    if (st.tf === '1m' || st.tf === '5m') { skip.cash_open = 1; }
    if (!st.show1H) skip['1H'] = 1;   // 1H toggle: hide 1H dividers + levels
    if (!st.show4H) skip['4H'] = 1;   // 4H toggle: hide 4H dividers + levels
    Object.keys(boundaries)
      .filter((k) => DIVIDER_STYLE[k] && !skip[k])
      .sort((a, b) => DIVIDER_STYLE[a].z - DIVIDER_STYLE[b].z)
      .forEach((k) => {
        const s = DIVIDER_STYLE[k];
        // 5m chart: 1H + 4H dividers render short-dotted (same colors/thickness)
        // instead of their defaults, per the default preference for that timeframe.
        const dash = ((k === '4H' || k === '1H') && st.tf === '5m') ? [2, 2] : s.dash;
        let list = boundaries[k];
        // 5m chart: where a 1H divider would coincide with a 4H divider, let the
        // 4H dominate — drop the colliding 1H line (4H also draws last by z-order).
        if (st.tf === '5m' && k === '1H' && st.show4H && boundaries['4H']) {
          const fourH = new Set(boundaries['4H']);
          list = (list || []).filter((ms) => !fourH.has(ms));
        }
        vlines(list, s.color, s.lw, dash, SNAP_KEYS[k] ? snapXToBar : timeToX);
      });

    // (Intraday overlay HH:MM bottom labels removed — native time axis now shows
    // them, and the overlay version was covering the native X-axis at H-8.)

    // Top divider labels. Intraday 4H: "4H · HH:MM ET". Higher-TF day/week/month
    // dividers: "<KEY> · MMM D" (the boundary DATE), snapped to the opening bar.
    overlayCtx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
    overlayCtx.textBaseline = 'top';
    overlayCtx.textAlign = 'center';
    TOP_LABEL_KEYS.forEach((k) => {
      if (k === '4H' && !st.show4H) return;   // 4H toggle off → no 4H top labels
      const snapped = !!SNAP_KEYS[k];
      const placedX = new Set();
      (boundaries[k] || []).forEach((ms) => {
        const x = snapped ? snapXToBar(ms) : ts.timeToCoordinate(ms / 1000);
        if (x == null || x < 0 || x > W) return;
        const xi = Math.round(x);
        if (placedX.has(xi)) return; placedX.add(xi);
        const label = snapped
          ? k + ' · ' + new Date(ms).toLocaleDateString('en-US',
              { month: 'short', day: 'numeric', timeZone: ET })
          : k + ' · ' + fmtET(ms) + ' ET';
        const tw = overlayCtx.measureText(label).width + 10;
        const stroke = (DIVIDER_STYLE[k] || {}).color || 'rgba(156,39,176,0.85)';
        overlayCtx.fillStyle = 'white';
        overlayCtx.strokeStyle = stroke;
        overlayCtx.lineWidth = 1;
        overlayCtx.fillRect(x - tw / 2, 4, tw, 18);
        overlayCtx.strokeRect(x - tw / 2, 4, tw, 18);
        overlayCtx.fillStyle = LABEL_COLOR[k] || '#000000';
        overlayCtx.fillText(label, x, 7);
      });
    });

    // Per-bucket H/L/C projection segments (Appendix A.7 + generalized).
    // 1H/4H toggles hide their horizontal level lines (and labels) too.
    const segHidden = (t) =>
      (!st.show1H && t.indexOf('1H') === 0) || (!st.show4H && t.indexOf('4H') === 0);
    if (segments && segments.length) {
      segments.forEach((s) => {
        if (segHidden(s.type)) return;
        const y = candleSeries.priceToCoordinate(s.value);
        if (y == null) return;
        const x1 = timeToX(s.x_start_ms);
        const x2 = timeToX(s.x_end_ms);
        if (x1 == null || x2 == null || x2 <= x1) return;
        if (x1 > W || x2 < 0) return;
        const sty = SEG_STYLE[s.type] || { color: '#000000', lw: 1, dash: [] };
        overlayCtx.strokeStyle = sty.color;
        overlayCtx.lineWidth = sty.lw;
        overlayCtx.setLineDash(sty.dash);
        overlayCtx.beginPath();
        overlayCtx.moveTo(x1, y + 0.5);
        overlayCtx.lineTo(x2, y + 0.5);
        overlayCtx.stroke();
      });

      // Label the PREVIOUS bucket's H/L/C as shown in the CURRENT bucket: per
      // bucket type pick the most-recent NON-live segment (the current bucket's
      // projection of the prior bucket). `live` segments are the in-progress
      // bucket's OWN developing H/L/C — drawn but NOT labeled (note: we only
      // care about the previous 1H/4H highs/lows, not the current bucket's own).
      const currentByType = new Map();   // type → current bucket's prior-bucket projection
      segments.forEach((s) => {
        if (s.live || segHidden(s.type)) return;
        const cur = currentByType.get(s.type);
        if (!cur || s.x_end_ms > cur.x_end_ms) currentByType.set(s.type, s);
      });
      overlayCtx.setLineDash([]);
      overlayCtx.font = 'bold 9px -apple-system, BlinkMacSystemFont, sans-serif';
      overlayCtx.textBaseline = 'top';
      overlayCtx.textAlign = 'left';
      // Draw 1H* before 4H* so that when a 1H and 4H divider coincide (e.g.
      // 22:30) the 4H tag nudges to the right of the 1H tag instead of overlapping.
      const placed = [];   // {x,y,w} of already-drawn label rects
      Array.from(currentByType.values())
        .sort((a, b) => a.type.localeCompare(b.type))
        .forEach((s) => {
          const y = candleSeries.priceToCoordinate(s.value);
          const xDiv = timeToX(s.x_start_ms);   // divider that starts the current bucket
          if (y == null || xDiv == null) return;
          if (y < 2 || y > H - 12) return;
          const sty = SEG_STYLE[s.type] || { color: '#000000' };
          const lbl = s.type + fmtLvl(s.value);   // e.g. "4HC $4336.50"
          const tw = overlayCtx.measureText(lbl).width;
          const yText = y + 2;                   // directly under this line
          let xText = xDiv + 3;                  // right next to the divider
          // nudge right past any already-placed label sharing this row
          for (const p of placed) {
            if (Math.abs(p.y - yText) < 11 && xText < p.x + p.w + 3 && xText + tw > p.x - 3) {
              xText = p.x + p.w + 4;
            }
          }
          if (xText + tw > W - 2) xText = W - 2 - tw;
          if (xText < 2) xText = 2;
          overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
          overlayCtx.fillRect(xText - 2, yText - 1, tw + 4, 11);
          overlayCtx.fillStyle = sty.color;
          overlayCtx.fillText(lbl, xText, yText);
          placed.push({ x: xText, y: yText, w: tw });
        });
    }

    // Prior-DAY H/L/C across the CURRENT day only (not full-width). Spans from the
    // current session open (last 18:00 ET <= the latest bar) to the right edge, so
    // yesterday's levels sit on today's candles instead of stretching left.
    const fopens = boundaries.futures_open || [];
    let dayStartMs = null;
    for (let i = 0; i < fopens.length; i++) { if (fopens[i] <= lastBarMs) dayStartMs = fopens[i]; }
    if (dayStartMs != null) {
      const xStart = snapXToBar(dayStartMs);
      if (xStart != null) {
        overlayCtx.font = 'bold 9px -apple-system, BlinkMacSystemFont, sans-serif';
        overlayCtx.textBaseline = 'top';
        overlayCtx.textAlign = 'left';
        ['PDH', 'PDL', 'PDC'].forEach((name) => {
          const v = levelsData[name];
          if (v == null) return;
          const y = candleSeries.priceToCoordinate(v);
          if (y == null || y < 0 || y > H) return;
          const color = DAY_LEVEL_COLOR[name];
          const x1 = Math.max(0, xStart);
          overlayCtx.strokeStyle = color;
          // All prior-day levels lw 3 (consistent with week/month). PDC solid,
          // PDH/PDL short-dashed (matching 4HH/4HL convention).
          if (name === 'PDC') {
            overlayCtx.setLineDash([]);
            overlayCtx.lineWidth = 3;
          } else {
            overlayCtx.setLineDash([2, 3]);
            overlayCtx.lineWidth = 3;
          }
          overlayCtx.beginPath();
          overlayCtx.moveTo(x1, y + 0.5); overlayCtx.lineTo(W, y + 0.5);
          overlayCtx.stroke();
          overlayCtx.setLineDash([]);
          if (y > 2 && y < H - 11) {
            // Match the PROTO Y* labeling convention (PDH -> YH, PDL -> YL, PDC -> YC);
            // a combined title (e.g. "YH·CWH") wins when this prior-day level survived a collision.
            const lbl = (levelTitles[name] || ('Y' + name.charAt(2))) + fmtLvl(v);   // e.g. "YC $4336.50"
            const tw = overlayCtx.measureText(lbl).width;
            const lx = Math.max(2, x1 + 3);
            overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
            overlayCtx.fillRect(lx - 2, y + 1, tw + 4, 11);
            overlayCtx.fillStyle = color;
            overlayCtx.fillText(lbl, lx, y + 2);
          }
        });
      }
    }
    // 5m only: add the daily-bucket day-end (4:59 PM ET) divider + MON..FRI weekday names at top,
    // matching 15m/30m. Everything else on the 5m is unchanged (2026-06-05).
    if (st.tf === '5m') {
      vlines(boundaries.pm_0459, PROTO.pmDiv, 1.1, [], snapDayEnd);   // Fri divider at the close, not Mon open
      drawWeekdayNames(snapXToBar, W, H);
    }
    drawFib();                   // range-expansion lines on top (all timeframes)
    drawLines();                 // user horizontal lines on top
  }

  // ---- Range-expansion ("Fib") tool: horizontal lines at 100% increments of
  // the [0%,100%] range the user clicked, from 0% out to 2500%. price(p%) =
  // b + (p/100)*(a-b), with a=100% anchor, b=0% anchor. Drawn on the overlay so
  // it tracks pan/zoom. 0% + 100% anchors are solid+bold; expansions dashed.
  function drawFib() {
    if (!st.fib || !candleSeries) return;
    const a = st.fib.a, b = st.fib.b;
    if (a == null || b == null || a === b) return;
    const span = a - b;                          // price per 100%
    const W = overlay.width, H = overlay.height;
    overlayCtx.save();
    overlayCtx.font = 'bold 10px -apple-system, BlinkMacSystemFont, sans-serif';
    overlayCtx.textBaseline = 'middle';
    overlayCtx.textAlign = 'left';
    for (let pct = 0; pct <= 2500; pct += 100) {
      const price = b + (pct / 100) * span;
      const y = candleSeries.priceToCoordinate(price);
      if (y == null || y < 0 || y > H) continue; // off-screen → skip
      const anchor = (pct === 0 || pct === 100);
      overlayCtx.strokeStyle = anchor ? 'rgba(124,58,237,0.95)' : 'rgba(124,58,237,0.5)';
      overlayCtx.lineWidth = anchor ? 2 : 1;
      overlayCtx.setLineDash(anchor ? [] : [4, 4]);
      overlayCtx.beginPath();
      overlayCtx.moveTo(0, y + 0.5); overlayCtx.lineTo(W, y + 0.5);
      overlayCtx.stroke();
      const lbl = pct + '%  ' + price.toFixed(1);
      overlayCtx.setLineDash([]);
      const tw = overlayCtx.measureText(lbl).width;
      overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
      overlayCtx.fillRect(2, y - 6, tw + 6, 12);
      overlayCtx.fillStyle = anchor ? 'rgba(109,40,217,1)' : 'rgba(124,58,237,0.85)';
      overlayCtx.fillText(lbl, 5, y);
    }
    overlayCtx.restore();
  }

  // Day names MON..FRI centered between day-end (pm_0459) dividers. Shared by drawProto (15m/30m/1h)
  // and the 5m daily-bucket overlay (2026-06-05).
  function drawWeekdayNames(snapXToBar, W, H) {
    overlayCtx.setLineDash([]);
    const dayStarts = (boundaries.pm_0459 || [])
      .map(snapXToBar).filter((x) => x != null).sort((a, b) => a - b);
    overlayCtx.textAlign = 'center';
    overlayCtx.textBaseline = 'top';
    // On 1m/5m the 4H divider labels occupy the very top band, so drop the weekday below them
    // (y26) to avoid overlapping "4H · HH:MM ET". PROTO TFs (15m/30m/1h) keep it at the top (y8).
    const wdY = (st.tf === '1m' || st.tf === '5m') ? 26 : 8;
    (boundaries.am_0959 || []).forEach((ms) => {
      const xm = snapXToBar(ms);
      if (xm == null) return;
      let xs = 0, xe = W;
      for (const dx of dayStarts) { if (dx <= xm) xs = dx; else { xe = dx; break; } }
      const cx = (xs + xe) / 2;
      if (cx < 16 || cx > W - 16) return;
      const wd = new Date(ms).toLocaleDateString('en-US', { weekday: 'short', timeZone: ET }).toUpperCase();
      overlayCtx.font = 'bold 14px -apple-system, BlinkMacSystemFont, sans-serif';
      overlayCtx.fillStyle = '#000000';
      overlayCtx.fillText(wd, cx, wdY);   // weekday — y8 on PROTO, y26 on 1m/5m (clear of the 4H labels)
    });
  }

  // ---- PROTOTYPE overlay for 15m/30m (PROTO_buckets_example.png) ----------
  // Per-day prior H/L/C (YH/YL black-dotted, YC green-solid), full-width prior
  // week/month (gated to the visible candle range), and 9:59 AM + 4:59 PM ET
  // vertical dividers, plus MON..FRI day names. Mirrors the reference template. Returns nothing — draws straight onto overlayCtx.
  function drawProto(timeToX, snapXToBar, vlines, W, H, snapDayEnd) {
    const priceY = (v) => candleSeries.priceToCoordinate(v);

    // 1) 4:59 PM ET (16:59) daily-bucket vertical divider — drawn on all PROTO
    //    TFs (15m/30m/1h; drawProto only runs for those). Marks the day end
    //    before the 17:00-18:00 maintenance gap and the 18:00 new session.
    //    snapDayEnd places the Friday divider at the Friday close (not forward
    //    across the weekend to Monday's open), so the Fri/Mon separator is as
    //    clear as the weekday ones. The 9:59 AM line was removed previously.
    vlines(boundaries.pm_0459, PROTO.pmDiv, 1.1, [], snapDayEnd);

    // 2) Full-width week (blue) + month (red), PRIOR + CURRENT. Prior H/L DOTTED,
    //    current H/L SOLID (so PM vs CM / PW vs CW read at a glance); closes solid.
    //    Explicit labels (PMH/PWC/CMH…) — never bare MH/WC (2026-06-15). Gated
    //    to the (adaptively-expanded) candle range, skip if off-screen.
    const fullWidth = [
      ['PWH', PROTO.week, 'PWH', false], ['PWL', PROTO.week, 'PWL', false],
      ['PWC', PROTO.week, 'PWC', true],
      ['PMH', PROTO.month, 'PMH', false], ['PML', PROTO.month, 'PML', false],
      ['PMC', PROTO.month, 'PMC', true],
      ['CWH', PROTO.week, 'CWH', true], ['CWL', PROTO.week, 'CWL', true],
      ['CMH', PROTO.month, 'CMH', true], ['CML', PROTO.month, 'CML', true],
    ];
    overlayCtx.textBaseline = 'top';
    overlayCtx.textAlign = 'right';
    overlayCtx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
    fullWidth.forEach((row) => {
      const v = levelsData[row[0]];
      if (v == null) return;
      const y = priceY(v);
      if (y == null || y < 0 || y > H) return;       // only within candle range
      const color = row[1], lbl = (levelTitles[row[0]] || row[2]) + fmtLvl(v), solid = row[3];   // combined title (e.g. "YH·CWH") when coincident, else "WH $4905.00"
      overlayCtx.strokeStyle = color;
      overlayCtx.lineWidth = 3;
      overlayCtx.setLineDash(solid ? [] : [2, 3]);
      overlayCtx.beginPath();
      overlayCtx.moveTo(0, y + 0.5); overlayCtx.lineTo(W, y + 0.5);
      overlayCtx.stroke();
      const tw = overlayCtx.measureText(lbl).width;
      overlayCtx.setLineDash([]);
      overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
      overlayCtx.fillRect(W - tw - 8, y + 1, tw + 6, 13);
      overlayCtx.fillStyle = color;
      overlayCtx.fillText(lbl, W - 4, y + 2);
    });

    // 3) Per-day prior H/L/C (1D segments). Skip `live` (today's developing
    //    range — we only show the PRIOR day). YH/YL black dotted, YC green solid;
    //    label at each day's left edge, under the line.
    overlayCtx.textAlign = 'left';
    overlayCtx.font = 'bold 9px -apple-system, BlinkMacSystemFont, sans-serif';
    (segments || []).forEach((s) => {
      if (s.live || s.type.indexOf('1D') !== 0) return;
      const kind = s.type.charAt(2);                 // 'H' | 'L' | 'C'
      const y = priceY(s.value);
      const x1 = timeToX(s.x_start_ms), x2 = timeToX(s.x_end_ms);
      if (y == null || x1 == null || x2 == null || x2 <= x1) return;
      if (x2 < 0 || x1 > W) return;
      const sty = (kind === 'C') ? PROTO.dayC : PROTO.dayHL;
      overlayCtx.strokeStyle = sty.color;
      overlayCtx.lineWidth = sty.lw;
      overlayCtx.setLineDash(sty.dash);
      overlayCtx.beginPath();
      overlayCtx.moveTo(Math.max(0, x1), y + 0.5);
      overlayCtx.lineTo(Math.min(W, x2), y + 0.5);
      overlayCtx.stroke();
      if (y > 2 && y < H - 11 && x1 > -30 && x1 < W - 12) {
        const lbl = 'Y' + kind + fmtLvl(s.value);   // e.g. "YL $4336.50"
        const lx = Math.max(2, x1 + 3);
        const tw = overlayCtx.measureText(lbl).width;
        overlayCtx.setLineDash([]);
        overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
        overlayCtx.fillRect(lx - 2, y + 1, tw + 4, 11);
        overlayCtx.fillStyle = sty.color;
        overlayCtx.fillText(lbl, lx, y + 2);
      }
    });

    // 3b) Prior-4H H/L/C as horizontal lines (purple, like 1m/5m). NO 4H vertical
    //     divider on these TFs. Each 4H bucket shows the PRIOR 4H bucket's levels;
    //     label only the current (latest, non-live) bucket to avoid clutter.
    //     Gated by the 4H toggle.
    const cur4h = new Map();
    if (st.show4H) (segments || []).forEach((s) => {
      if (s.live || s.type.indexOf('4H') !== 0) return;
      const y = priceY(s.value);
      const x1 = timeToX(s.x_start_ms), x2 = timeToX(s.x_end_ms);
      if (y == null || x1 == null || x2 == null || x2 <= x1) return;
      if (x2 < 0 || x1 > W) return;
      const sty = SEG_STYLE[s.type] || { color: '#888', lw: 1, dash: [] };
      overlayCtx.strokeStyle = sty.color;
      overlayCtx.lineWidth = sty.lw;
      overlayCtx.setLineDash(sty.dash);
      overlayCtx.beginPath();
      overlayCtx.moveTo(Math.max(0, x1), y + 0.5);
      overlayCtx.lineTo(Math.min(W, x2), y + 0.5);
      overlayCtx.stroke();
      const cur = cur4h.get(s.type);
      if (!cur || s.x_end_ms > cur.x_end_ms) cur4h.set(s.type, s);
    });
    overlayCtx.setLineDash([]);
    overlayCtx.textAlign = 'left';
    overlayCtx.font = 'bold 9px -apple-system, BlinkMacSystemFont, sans-serif';
    cur4h.forEach((s) => {
      const y = priceY(s.value);
      const x1 = timeToX(s.x_start_ms);
      if (y == null || x1 == null || y < 2 || y > H - 11) return;
      const sty = SEG_STYLE[s.type] || { color: '#888' };
      const tw = overlayCtx.measureText(s.type).width;
      let lx = Math.max(2, Math.min(W - tw - 4, x1 + 3));
      overlayCtx.fillStyle = 'rgba(255,255,255,0.85)';
      overlayCtx.fillRect(lx - 2, y + 1, tw + 4, 11);
      overlayCtx.fillStyle = sty.color;
      overlayCtx.fillText(s.type, lx, y + 2);
    });

    // 4) Day names MON..FRI, centered between the surrounding day-end dividers.
    drawWeekdayNames(snapXToBar, W, H);

    // (Bottom "4:59 PM" labels removed along with the vertical line above.)
  }

  // ---- apply a full cold snapshot (atomic; spec §5.5) -------------
  function applySnapshot(snap) {
    lastSnap = snap;   // retain raw snapshot so the Days dropdown can re-crop without refetching
    // Surface the LIVE contract (e.g. '/NQ[U26]') in BOTH the ticker dropdown (its
    // selected value) and the OHLC legend below — so a stale/mis-rolled feed is
    // obvious at a glance. Backend sends it on init + every cold snapshot. The
    // dropdown option VALUE stays the base ticker so switching still works; only the
    // visible TEXT changes. The legend picks up st.contract via setLegend() later in
    // this function (it reads st.contract on every redraw).
    if (snap.contract) {
      st.contract = snap.contract;
      const opt = Array.from(tickerSel.options).find((o) => o.value === st.ticker);
      if (opt && opt.textContent !== snap.contract) opt.textContent = snap.contract;
    }
    let bars = snap.bars || [];
    if (!bars.length) return;
    // Effective hard left-edge cutoff (ms). Two independent sources, the later wins:
    //   • st.cropFrom  — dual-view top panel ("current ISO week Mon-onwards only")
    //   • st.rollDays  — the Days dropdown: keep only the last N trading sessions, so
    //     the current day is the rightmost bucket. Uses the backend's 1D session-open
    //     dividers (DST-correct) to find where the N-th-most-recent session begins.
    let cutoffMs = st.cropFrom ? st.cropFrom * 1000 : 0;
    if (st.rollDays) {
      // Session-open dividers mark each trading day's 18:00 ET start. BOTH lists —
      // '1D' (used by 15m/30m/1h) and 'futures_open' (used by 1m/5m) — include
      // phantom Fri-close / Sat 18:00 markers that start NO real session. Filter
      // ALL of them by keeping only opens that have a bar within 6h after them, so
      // N counts real trading sessions on every TF (today = the Nth/rightmost).
      // (Without this, rollDays:5 on 1h reached only ~3 real days: 2 of the 5
      //  counted-back markers were weekend phantoms with no bars.)
      const b = snap.boundaries || {};
      let opens = b['1D'] || b['futures_open'] || [];
      if (opens.length) {
        const SIXH = 6 * 3600 * 1000, times = bars.map((x) => x.time);
        opens = opens.filter((o) => {
          let lo = 0, hi = times.length;
          while (lo < hi) { const m = (lo + hi) >> 1; if (times[m] < o) lo = m + 1; else hi = m; }
          return lo < times.length && times[lo] <= o + SIXH;
        });
      }
      if (opens.length >= st.rollDays) cutoffMs = Math.max(cutoffMs, opens[opens.length - st.rollDays]);
      else if (opens.length)          cutoffMs = Math.max(cutoffMs, opens[0]);
    }
    if (cutoffMs) {
      bars = bars.filter((b) => b.time >= cutoffMs);
      if (!bars.length) return;
    }
    // Merge scroll-back deep history (older than this snapshot) so cold updates
    // don't wipe a scrolled-back view. Dormant when histBars is empty.
    const snapFirstMs = bars[0].time;
    const histOlder = histBars.filter((h) => h.time < snapFirstMs);
    recentStartIdx = histOlder.length;
    const allBars = histOlder.length ? histOlder.concat(bars) : bars;
    allBarsRef = allBars;   // for the client-side month grid (1d/4h), which spans all loaded candles

    lastBarLogicalIdx = allBars.length - 1;
    lastBarMs = allBars[allBars.length - 1].time;
    barTimesSec = allBars.map((b) => b.time / 1000);   // ascending; for snapping dividers
    // barHigh/barLow drive the visible-candle autoscale (indexed by logical range,
    // so they MUST span the merged set); whitespace bars get NaN.
    barHigh = allBars.map((b) => (b.open == null ? NaN : b.high));
    barLow  = allBars.map((b) => (b.open == null ? NaN : b.low));
    barClose = allBars.map((b) => (b.open == null ? NaN : b.close));
    rebuildSessionBoxes();   // recent window only (starts at recentStartIdx)

    // Preserve the scrolled-back view across a cold update / a history prepend:
    // capture the visible TIME range (stable regardless of how many bars prepend)
    // and restore it after setData. Only when history is present (else unchanged).
    let _prevRange = null;
    if (histOlder.length && chart) { try { _prevRange = chart.timeScale().getVisibleRange(); } catch (e) {} }

    // Whitespace bars (just {time}) come through for the 17:00-17:59 maintenance
    // window; pass them as pure WhitespaceData so LWC reserves the slot without
    // drawing a candle, and skip them for line series and autoscale.
    candleSeries.setData(allBars.map((b) =>
      b.open == null
        ? { time: b.time / 1000 }
        : { time: b.time / 1000, open: b.open, high: b.high, low: b.low, close: b.close }
    ));
    if (maSeries) {
      Object.keys(maSeries).forEach((set) => {
        MA_SETS[set].lines.forEach((ln) => {
          maSeries[set][ln.key].setData(allBars.filter((b) => b[ln.key] != null).map((b) => ({ time: b.time / 1000, value: b[ln.key] })));
        });
      });
    }
    if (_prevRange) { try { chart.timeScale().setVisibleRange(_prevRange); } catch (e) {} }

    // Levels/last-bar anchor on the RECENT window (deep history carries no overlays).
    const realBars = bars.filter((b) => b.open != null);
    candleYLo = Math.min.apply(null, realBars.map((b) => b.low));
    candleYHi = Math.max.apply(null, realBars.map((b) => b.high));
    const lb = realBars[realBars.length - 1];
    lastOHLC = { time: lb.time / 1000, open: lb.open, high: lb.high, low: lb.low, close: lb.close };
    setLegend(lastOHLC);
    levelsData = snap.levels || {};
    levelTitles = snap.level_titles || {};   // weekday titles for the 2-5D levels
    // PROTO timeframes draw week/month full-width on the canvas overlay (so they
    // can be labelled WH/WL/WC, MH/ML/MC under the line) instead of axis price
    // lines; clear any price lines and skip addLevels for them.
    if (PROTO_TF.indexOf(st.tf) !== -1) {
      // PROTO TFs (15m/30m/1h) draw W/M on the canvas overlay, but the 2-5 day weekday levels
      // get the SAME clean axis pills as 1m/5m (createPriceLine) instead of faint overlay text.
      const md = {};
      ['2DH', '2DL', '3DH', '3DL', '4DH', '4DL', '5DH', '5DL',
       '2WH', '2WL', '3WH', '3WL', '2MH', '2ML', '3MH', '3ML'].forEach((k) => {
        if (snap.levels && snap.levels[k] != null) md[k] = snap.levels[k];
      });
      addLevels(md, candleYLo, candleYHi);   // clears price lines, then draws the weekday + multi-week/month pills
    } else addLevels(snap.levels, candleYLo, candleYHi);

    boundaries = snap.boundaries || {};
    segments = snap.segments || [];
    maintenanceGaps = snap.maintenance_gaps || [];
    // When a cutoff is in effect, also crop boundaries/segments/maintenance_gaps
    // so overlay rendering doesn't try to draw lines for the dropped bars.
    if (cutoffMs) {
      const filteredB = {};
      Object.keys(boundaries).forEach((k) => {
        filteredB[k] = (boundaries[k] || []).filter((ms) => ms >= cutoffMs);
      });
      boundaries = filteredB;
      segments = segments
        .filter((s) => s.x_end_ms >= cutoffMs)
        .map((s) => s.x_start_ms < cutoffMs ? { ...s, x_start_ms: cutoffMs } : s);
      maintenanceGaps = maintenanceGaps.filter((g) => g.end_ms >= cutoffMs);
    }
    if (snap.current) updateCurrentBar(snap.current);

    // One-time default zoom on first paint (do not fight the user afterward).
    if (!appliedFirstZoom) {
      appliedFirstZoom = true;
      // Prefer the caller's explicit initial zoom (e.g. dual view's Mon→now) so
      // LWC's natural visibleRangeChange events during chart init can't override it.
      // BUT a rolling-days filter always fits the whole cropped window — ignore any
      // restored/auto zoom (which setData's own visibleRangeChange can repopulate
      // with the prior ~1-day range before we read it).
      const restored = st.rollDays ? null : (st.pendingInitialZoom || st.zoomRange);
      st.pendingInitialZoom = null;
      const zoomBars = snap.default_zoom || 240;
      const lastIdx = bars.length - 1;
      const barSec = bars.length > 1 ? (bars[bars.length - 1].time - bars[bars.length - 2].time) : 60;
      requestAnimationFrame(() => {
        try {
          // When PAUSED (not following), honour the user's saved zoom verbatim. When FOLLOWING
          // (the default), ALWAYS re-fit to the latest `visBars` data bars + a proportional right
          // gap via the LOGICAL range (which can extend past the last bar). Re-fitting every
          // snapshot — instead of restoring the auto-saved range — is what makes the gap STICK:
          // the old code saved my gapped range, then a later recompute "restored" it and re-pinned
          // the candle to the edge. Deterministic, identical fraction on every TF, can't compound.
          if (!st.follow && restored && restored.from && restored.to) {
            chart.timeScale().setVisibleRange({ from: restored.from, to: restored.to });
          } else {
            const visBars = st.rollDays ? bars.length : Math.min(bars.length, zoomBars);
            const gB = Math.max(3, Math.round(GAP_FRAC * visBars)); st.gapBars = gB;
            chart.timeScale().applyOptions({ rightOffset: gB });
            chart.timeScale().setVisibleLogicalRange({ from: lastIdx - visBars + 1, to: lastIdx + gB });
          }
        } catch (e) {}
        requestAnimationFrame(drawOverlay);
      });
    }
    setBadge(snap.feed_state === 'down' ? 'down'
           : snap.feed_state === 'degraded' ? 'stale' : 'live');
    requestAnimationFrame(drawOverlay);
  }

  function updateCurrentBar(c) {
    const isNewBar = c.ts_ms > lastBarMs;
    candleSeries.update({ time: c.ts_ms / 1000, open: c.open, high: c.high, low: c.low, close: c.close });
    lastOHLC = { time: c.ts_ms / 1000, open: c.open, high: c.high, low: c.low, close: c.close };
    setLegend(lastOHLC);
    // keep barHigh/barLow current so the visible-candle autoscale includes the
    // forming candle's extremes.
    if (isNewBar) { barHigh.push(c.high); barLow.push(c.low); barClose.push(c.close); }
    else if (barHigh.length) { barHigh[barHigh.length - 1] = c.high; barLow[barLow.length - 1] = c.low; }
    ingestSessionBar(c.ts_ms, c.high, c.low, c.close);   // grow the live session box with the forming candle
    if (isNewBar) {
      lastBarLogicalIdx += 1; lastBarMs = c.ts_ms;
      // A fresh candle started — if following, keep the window tracking it.
      if (st.follow && chart) { try { chart.timeScale().scrollToRealTime(); } catch (e) {} }
    }
  }

  function setBadge(kind) {
    badgeEl.className = 'q-badge ' + kind;
    badgeEl.textContent = kind === 'live' ? 'live'
      : kind === 'stale' ? 'stale'
      : kind === 'down' ? 'down' : 'connecting';
  }

  function showError(msg) { errEl.textContent = msg; errEl.classList.add('show'); }
  function clearError() { errEl.classList.remove('show'); }

  // ---- frame routing (called by the shared stream) ----------------
  function handleFrame(frame) {
    if (!frame || !frame.topic) return;
    if (frame.topic.ticker !== st.ticker || frame.topic.tf !== st.tf) return;
    // dedup by sequence_id (Task 14 step 3).
    if (frame.sequence_id != null && frame.sequence_id <= st.lastSeq) return;
    if (frame.sequence_id != null) st.lastSeq = frame.sequence_id;

    if (frame.kind === 'hot') {
      if (frame.bar && candleSeries) updateCurrentBar(frame.bar);
      requestAnimationFrame(drawOverlay);
    } else if (frame.kind === 'cold') {
      const snap = frame.snap || {};
      const sv = snap.snapshot_version != null ? snap.snapshot_version : frame.snapshot_version;
      if (sv != null && sv < st.snapshotVersion) return;   // stale snapshot — reject (§5.5)
      if (sv != null) st.snapshotVersion = sv;
      clearError();
      applySnapshot(snap);
    }
  }

  // ---- /api/init fetch with generation guard (spec §5.2) ----------
  async function fetchInit() {
    const gen = st.generation;
    setBadge('loading');
    try {
      const r = await fetch('/api/init/' + st.ticker + '/' + st.tf);
      if (gen !== st.generation) return;          // stale — a newer switch happened
      if (r.status === 400) {
        const b = await r.json();
        showError((b.error || 'invalid topic') + '\nvalid: ' +
          (b.valid_tickers || []).join(',') + ' / ' + (b.valid_timeframes || []).join(','));
        return;
      }
      const data = await r.json();
      if (gen !== st.generation) return;
      if (data.error) { setBadge('stale'); return; }
      clearError();
      // /api/init returns the snapshot fields at the top level.
      if (data.snapshot_version != null) st.snapshotVersion = data.snapshot_version;
      applySnapshot(data);
    } catch (e) {
      if (gen === st.generation) setBadge('stale');
    }
  }

  // ---- control wiring (Task 15) -----------------------------------
  function syncControls() {
    tickerSel.value = st.ticker;
    tfSel.value = st.tf;
    footTf.textContent = tfFooterLabel(st.tf);   // footer center tracks the active TF
    daysSel.value = st.rollDays == null ? 'all' : String(st.rollDays);
    sbBtn.classList.toggle('active', st.maSet === 'sb');
    ovBtn.classList.toggle('active', st.maSet === 'ov');
    bkt1hBtn.classList.toggle('active', st.show1H);
    bkt4hBtn.classList.toggle('active', st.show4H);
    sessBtn.classList.toggle('active', st.showSessions);
    followBtn.classList.toggle('active', st.follow);
    followBtn.textContent = st.follow ? '▶ Follow' : '⏸ Free';
  }

  function switchTopic(newTicker, newTf) {
    const tfChanged = newTf !== st.tf;
    const tickerChanged = newTicker !== st.ticker;
    st.ticker = newTicker;
    st.tf = newTf;
    if (tickerChanged) st.lines = loadLines(newTicker);   // lines are per-ticker
    // Month-grid TFs (daily/4h) show full history (use scroll-back / zoom), not an
    // N-day crop which would fight the month grid, and default to NO moving
    // averages (clean annual month-grid look — toggle them on via the dots).
    if (MONTH_GRID_TFS.indexOf(newTf) !== -1) { st.rollDays = null; st.maSet = null; }
    st.generation += 1;            // invalidate in-flight init + stale frames
    st.snapshotVersion = -1;
    st.lastSeq = 0;
    appliedFirstZoom = false;
    st.zoomRange = null;
    // Drop any scrolled-back deep history — it belongs to the old instrument/TF.
    histBars = []; histFetching = false; histExhausted = false; recentStartIdx = 0; allBarsRef = [];
    if (tfChanged) rebuildSeries();   // overlay set differs by TF (spec §5.2)
    boundaries = {}; segments = []; maintenanceGaps = [];
    st.fib = null; st.fibArming = 0; st.fibFirst = null; syncFibBtn();   // anchors are price-specific to the old instrument
    syncControls();
    if (opts.onTopicChange) opts.onTopicChange();   // page re-POSTs subscribe set
    fetchInit();
    persist();
  }

  tickerSel.addEventListener('change', (e) => {
    switchTopic(e.target.value, st.tf);
    // Notify the page so ticker-link mode can fan this pick out to siblings.
    // (Programmatic setTicker() below does NOT fire 'change', so no echo loop.)
    if (opts.onTickerChange) opts.onTickerChange(e.target.value, api);
  });
  tfSel.addEventListener('change',     (e) => switchTopic(st.ticker, e.target.value));
  daysSel.addEventListener('change', (e) => {
    const v = e.target.value;
    st.rollDays = (v === 'all') ? null : parseInt(v, 10);
    appliedFirstZoom = false;     // re-zoom to fit the new rolling window
    st.zoomRange = null;
    if (lastSnap) applySnapshot(lastSnap);   // re-crop the data we already have — no refetch
    persist();
  });
  function toggleMaSet(set) {
    if (MA_BUTTONS_DISABLED) return;              // TEMPORARY: MA buttons disabled — no-op
    if (set === 'sb' && !SB_ENABLED) return;      // SB removed by request — no-op
    st.maSet = (st.maSet === set) ? null : set;   // mutually exclusive; clicking the active one turns it off
    applyMaVisibility();
    syncControls();
    persist();
  }
  sbBtn.addEventListener('click', () => toggleMaSet('sb'));
  ovBtn.addEventListener('click', () => toggleMaSet('ov'));
  bkt1hBtn.addEventListener('click', () => {
    if (!BUCKETS_1H_4H_ENABLED) return;           // 1H/4H buckets removed — no-op
    st.show1H = !st.show1H;
    syncControls();
    requestAnimationFrame(drawOverlay);
    persist();
  });
  bkt4hBtn.addEventListener('click', () => {
    if (!BUCKETS_4H_ENABLED) return;              // 4H button enabled independently
    st.show4H = !st.show4H;
    syncControls();
    requestAnimationFrame(drawOverlay);
    persist();
  });
  sessBtn.addEventListener('click', () => {
    st.showSessions = !st.showSessions;
    syncControls();
    requestAnimationFrame(drawOverlay);
    persist();
  });
  followBtn.addEventListener('click', () => {
    st.follow = !st.follow;
    if (chart) {
      chart.timeScale().applyOptions({
        shiftVisibleRangeOnNewBar: st.follow,
        rightOffset: st.follow ? (st.gapBars || 8) : 4,   // proportional gap (set from the snapshot zoom)
      });
      if (st.follow) { try { chart.timeScale().scrollToRealTime(); } catch (e) {} }
    }
    syncControls();
    persist();
  });
  // Crosshair toggle — delegate to the global coordinator so it flips on every
  // quadrant + every window. The coordinator calls back into applyCrosshair().
  crossBtn.addEventListener('click', () => {
    if (opts.onCrosshairToggle) opts.onCrosshairToggle();
    else applyCrosshair(!crosshairOn);   // standalone fallback (no coordinator)
  });
  // Range-expansion tool. Click: if a drawing exists, clear it; else arm/disarm.
  function syncFibBtn() {
    fibBtn.classList.toggle('active', !!(st.fib || st.fibArming));
    fibBtn.textContent = st.fibArming === 1 ? '📐 click 100%'
                       : st.fibArming === 2 ? '📐 click 0%'
                       : '📐 Fib';
  }
  fibBtn.addEventListener('click', () => {
    if (st.fib) { st.fib = null; st.fibArming = 0; st.fibFirst = null; }
    else { st.fibArming = st.fibArming ? 0 : 1; st.fibFirst = null; }
    syncFibBtn();
    requestAnimationFrame(drawOverlay);
  });
  function syncLineBtn() {
    lineBtn.classList.toggle('active', !!st.lineArming);   // blue ONLY while armed
    lineBtn.textContent = st.lineArming
      ? (st.lineFirst ? '╍ click end' : '╍ click start')
      : '╍ Line';
  }
  // Toggle drawing mode: ON = each two clicks draws another line (stays on);
  // OFF = cursor mode (drag endpoints to resize). Right-click a line to delete.
  lineBtn.addEventListener('click', () => {
    st.lineArming = !st.lineArming;
    st.lineFirst = null;
    syncLineBtn();
  });
  maxBtn.addEventListener('click', () => { if (opts.onMaximize) opts.onMaximize(api); });
  popBtn.addEventListener('click', () => {
    window.open('/quadrant?ticker=' + encodeURIComponent(st.ticker) +
                '&tf=' + encodeURIComponent(st.tf),
                '_blank', 'width=1200,height=800');
  });

  // ---- zoom persistence -------------------------------------------
  function onVisibleRangeChange(range) {
    drawOverlay();
    if (range && range.from && range.to) {
      st.zoomRange = { from: range.from, to: range.to };
      persist();
    }
  }

  // Fetch the next older chunk of deep history (scroll-back to 2010) and prepend
  // it. Reads /api/history (archive-backed, off the live path). Re-renders via
  // applySnapshot, which merges histBars + the live snapshot and preserves the
  // visible time range so the view doesn't jump.
  function maybeFetchOlder() {
    if (histFetching || histExhausted) return;
    const oldestMs = histBars.length ? histBars[0].time
                   : (lastSnap && lastSnap.bars && lastSnap.bars.length ? lastSnap.bars[0].time : null);
    if (oldestMs == null) return;
    histFetching = true;
    // Scrolling into history means you're examining the past, not following live —
    // drop follow so the live edge can't yank the view forward.
    if (st.follow) { st.follow = false; syncControls(); persist(); }
    // Also drop the N-day "Days" crop (30m/1h default to 5d): otherwise the
    // cropped-out middle (between the snapshot's true oldest and the N-day window)
    // leaves a gap between the lazy history and the visible window.
    if (st.rollDays != null) { st.rollDays = null; syncControls(); persist(); }
    const gen = st.generation;
    const chunk = (st.tf === '1d') ? 200 : 800;   // daily is the heaviest to resample
    fetch('/api/history/' + st.ticker + '/' + st.tf + '?before_ms=' + oldestMs + '&n=' + chunk)
      .then((r) => r.json())
      .then((d) => {
        if (gen !== st.generation) return;        // ticker/tf changed mid-flight — discard
        const older = (d.bars || []).filter((b) => b.time < oldestMs);
        if (!older.length || d.exhausted) { histExhausted = true; return; }
        histBars = older.concat(histBars);
        if (lastSnap) applySnapshot(lastSnap);    // re-render merged + preserve view
      })
      .catch(() => {})
      .finally(() => { histFetching = false; });
  }

  let persistTimer = null;
  function persist() {
    if (!opts.onPersist) return;
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => opts.onPersist(), 250);   // debounce ~250ms (§5.6)
  }

  // ---- responsive resize (ResizeObserver → rAF-debounced) ---------
  let rafPending = false;
  const ro = new ResizeObserver(() => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      if (chart) chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
      drawOverlay();
    });
  });
  ro.observe(wrap);

  // ---- public api --------------------------------------------------
  const api = {
    el: root,
    get ticker() { return st.ticker; },
    get tf() { return st.tf; },
    state: st,
    topic: function () { return { ticker: st.ticker, tf: st.tf }; },
    // Programmatically switch this quadrant's ticker (used by ticker-link mode).
    // Does NOT fire the dropdown 'change' event, so it never re-propagates.
    setTicker: function (ticker) { if (ticker && ticker !== st.ticker) switchTopic(ticker, st.tf); },
    refreshLines: refreshLines,    // re-read shared lines (line-sync coordinator)
    setCrosshair: applyCrosshair,  // show/hide crosshair (crosshair-sync coordinator)
    handleFrame: handleFrame,
    resize: function () {
      if (chart) {
        chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
        // Defensively re-assert the time axis on every resize so it never
        // gets stuck hidden after a layout flip (e.g. maximize toggle).
        chart.timeScale().applyOptions({ visible: true, timeVisible: true, borderVisible: true });
      }
      drawOverlay();
    },
    serialize: function () {
      return { ticker: st.ticker, tf: st.tf, maSet: st.maSet,
               show1H: st.show1H, show4H: st.show4H, showSessions: st.showSessions,
               follow: st.follow, zoomRange: st.zoomRange, isMaximized: st.isMaximized,
               rollDays: st.rollDays };   // lines persist in the shared per-ticker store, not here
    },
    destroy: function () {
      ro.disconnect();
      if (chart) { try { chart.remove(); } catch (e) {} }
      if (root.parentNode) root.parentNode.removeChild(root);
    },
  };

  // ---- boot --------------------------------------------------------
  createChart();
  syncControls();
  syncLineBtn();
  applyMaVisibility();
  fetchInit();
  return api;
}

/* ================================================================== *
 * Shared multiplexed stream manager (one EventSource per page/window)
 * ================================================================== */
function makeStreamManager(quadrantsOrFn) {
  // Accepts either a fixed quadrant array OR a callback returning the live set.
  // Callback form lets transient sub-views (e.g. dual-view) add/remove their
  // own quadrants without touching the persistent grid list.
  const getQuadrants = typeof quadrantsOrFn === 'function'
    ? quadrantsOrFn
    : () => quadrantsOrFn;
  const clientId = 'mc_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
  let evtSource = null;

  function topics() {
    // de-dup identical (ticker,tf) topics across quadrants.
    const seen = {}; const out = [];
    getQuadrants().forEach((q) => {
      const t = q.topic(); const key = t.ticker + '|' + t.tf;
      if (!seen[key]) { seen[key] = true; out.push(t); }
    });
    return out;
  }

  async function subscribe() {
    try {
      await fetch('/api/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: clientId, topics: topics() }),
      });
    } catch (e) { /* server will re-sync on reconnect */ }
  }

  function open() {
    if (evtSource) { try { evtSource.close(); } catch (e) {} }
    evtSource = new EventSource('/api/stream?client_id=' + encodeURIComponent(clientId));
    evtSource.onmessage = (e) => {
      let frame;
      try { frame = JSON.parse(e.data); } catch (err) { return; }
      getQuadrants().forEach((q) => q.handleFrame(frame));
    };
    evtSource.onerror = () => {
      // Native EventSource auto-reconnects. On reopen, re-declare the topic set
      // so the server re-pushes a fresh cold snapshot for every active view.
      subscribe();
    };
  }

  async function start() {
    await subscribe();
    open();
  }

  // Re-POST the full topic set (called when any quadrant switches topic).
  let resubTimer = null;
  function resubscribe() {
    if (resubTimer) clearTimeout(resubTimer);
    resubTimer = setTimeout(() => subscribe(), 60);
  }

  return { start: start, resubscribe: resubscribe, clientId: clientId };
}

/* ================================================================== *
 * Layout persistence (spec §5.6 — localStorage, debounced)
 * ================================================================== */
function loadLayout() {
  // The 4-quadrant TF arrangement ALWAYS resets to the default on every refresh
  // (UL=1h, UR=30m, LL=5m, LR=1m — 2026-06-08). Only the (auto-linked) ticker
  // carries over from the saved layout; per-quadrant TF/zoom/indicator tweaks are
  // intentionally NOT persisted across a refresh.
  let ticker = 'NQ';
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    const q0 = parsed && Array.isArray(parsed.quadrants) && parsed.quadrants[0];
    if (q0 && q0.ticker) ticker = q0.ticker;
  } catch (e) {}
  return { quadrants: DEFAULT_LAYOUT.map((d) => ({ ticker: ticker, tf: d.tf })) };
}
function saveLayout(quadrants, maximizedId) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      quadrants: quadrants.map((q) => q.serialize()),
      maximizedQuadrantId: maximizedId,
      indDefaultsV2: true,   // marks that the EMA20+SMA200 default migration ran
    }));
  } catch (e) {}
}

/* ================================================================== *
 * Ticker-link coordinator (shared by grid + pop-out windows)
 * ------------------------------------------------------------------ *
 * When ON, a ticker pick in ANY dropdown switches every quadrant in this
 * window AND broadcasts to other windows so they switch too. State lives in
 * localStorage (LINK_KEY) + a BroadcastChannel so it survives reloads and
 * reaches separate pop-out windows.
 * ================================================================== */
function makeTickerLink(getQuadrants, button) {
  let on = false;
  try { on = localStorage.getItem(LINK_KEY) === '1'; } catch (e) {}
  let chan = null;
  try { if ('BroadcastChannel' in window) chan = new BroadcastChannel(LINK_CHANNEL); } catch (e) {}

  function reflect() { if (button) button.classList.toggle('active', on); }

  // The shared "current ticker" survives reloads so a refreshed window can rejoin.
  function curTicker() {
    try { return localStorage.getItem(CUR_TICKER_KEY) || null; } catch (e) { return null; }
  }
  function rememberTicker(ticker) {
    if (!ticker) return;
    try { localStorage.setItem(CUR_TICKER_KEY, ticker); } catch (e) {}
  }

  // Push `ticker` onto every quadrant in THIS window except the source.
  // q.setTicker() is programmatic (no 'change' event), so it never re-broadcasts.
  function applyToAll(ticker, source) {
    if (!ticker) return;
    getQuadrants().forEach((q) => { if (q !== source) q.setTicker(ticker); });
  }

  function setOn(next, broadcast) {
    on = !!next;
    try { localStorage.setItem(LINK_KEY, on ? '1' : '0'); } catch (e) {}
    reflect();
    if (broadcast && chan) { try { chan.postMessage({ type: 'link', on: on }); } catch (e) {} }
  }

  // ALWAYS linked (2026-06-07): a user picking a ticker in any chart fans it
  // out to every quadrant + every window. No toggle.
  function onLocalTickerChange(ticker, source) {
    rememberTicker(ticker);                                 // so refreshes rejoin here
    applyToAll(ticker, source);
    if (chan) { try { chan.postMessage({ type: 'ticker', ticker: ticker }); } catch (e) {} }
  }

  if (chan) {
    chan.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === 'ticker') { rememberTicker(m.ticker); applyToAll(m.ticker, null); }  // always follow
    };
  }

  // Rejoin the group on boot: if any window has already picked a ticker, adopt it
  // here (without re-broadcasting) so a refreshed pop-out snaps back to the shared
  // ticker instead of reverting to its frozen URL ticker. No-op on the very first
  // load (nothing remembered yet) → windows keep their URL/layout default.
  const cur = curTicker();
  if (cur) applyToAll(cur, null);

  reflect();
  return {
    isOn: function () { return on; },
    toggle: function () { setOn(!on, true); },
    onLocalTickerChange: onLocalTickerChange,
    currentTicker: curTicker,
  };
}

/* ================================================================== *
 * Line sync — user-drawn horizontal lines are shared per-ticker across
 * every chart (all timeframes) and across windows. When one chart draws /
 * moves / deletes a line, every other chart of that ticker refreshes.
 * ================================================================== */
function makeLineSync(getQuadrants) {
  let chan = null;
  try { if ('BroadcastChannel' in window) chan = new BroadcastChannel(LINES_CHANNEL); } catch (e) {}
  function applyToAll(ticker, source) {
    getQuadrants().forEach((q) => {
      if (q !== source && q.topic && q.topic().ticker === ticker && q.refreshLines) q.refreshLines();
    });
  }
  // A chart in THIS window changed the lines (already saved to the shared store).
  function onLocalLinesChange(ticker, source) {
    applyToAll(ticker, source);                                   // siblings in this window
    if (chan) { try { chan.postMessage({ ticker: ticker }); } catch (e) {} }   // other windows
  }
  if (chan) {
    chan.onmessage = (e) => { const m = e.data || {}; if (m.ticker) applyToAll(m.ticker, null); };
  }
  return { onLocalLinesChange: onLocalLinesChange };
}

/* ================================================================== *
 * Crosshair sync — the crosshair show/hide is GLOBAL: toggling it on any
 * chart hides/shows the crosshair lines on EVERY quadrant and EVERY window
 * (grid, pop-out, maximized). State lives in localStorage (CROSSHAIR_KEY) +
 * a BroadcastChannel so it survives reloads and reaches separate windows.
 * Mirrors makeTickerLink / makeLineSync.
 * ================================================================== */
function makeCrosshairSync(getQuadrants) {
  let on = loadCrosshairOn();
  let chan = null;
  try { if ('BroadcastChannel' in window) chan = new BroadcastChannel(CROSSHAIR_CHANNEL); } catch (e) {}
  function applyToAll() {
    getQuadrants().forEach((q) => { if (q.setCrosshair) q.setCrosshair(on); });
  }
  function setOn(next, broadcast) {
    on = !!next;
    try { localStorage.setItem(CROSSHAIR_KEY, on ? '1' : '0'); } catch (e) {}
    applyToAll();
    if (broadcast && chan) { try { chan.postMessage({ on: on }); } catch (e) {} }
  }
  if (chan) {
    chan.onmessage = (e) => { const m = e.data || {}; if (typeof m.on === 'boolean') setOn(m.on, false); };
  }
  applyToAll();   // push the saved state onto every chart at boot
  return {
    toggle: function () { setOn(!on, true); },
    sync: applyToAll,                       // re-push to newly created quadrants
    isOn: function () { return on; },
  };
}

/* ================================================================== *
 * Grid page bootstrap (4 quadrants over one shared stream)
 * ================================================================== */
function bootGrid(gridEl) {
  const saved = loadLayout();
  const layout = (saved && saved.quadrants) || DEFAULT_LAYOUT;
  // One-time migration: older saved layouts had all indicators on. Re-apply the
  // new EMA20+SMA200 default once (preserving tickers/timeframes/zoom), then the
  // flag in saveLayout prevents re-running it. Future per-quadrant toggles persist.
  if (saved && !saved.indDefaultsV2) {
    layout.forEach((cfg) => { delete cfg.ind; delete cfg.indicatorsOn; });
  }
  let maximizedId = (saved && saved.maximizedQuadrantId) != null ? saved.maximizedQuadrantId : null;
  const quadrants = [];
  let stream = null;
  let link = null;   // ticker-link coordinator (assigned once quadrants exist)
  let lineSync = null;   // horizontal-line sync coordinator (assigned once quadrants exist)
  let crosshair = null;  // crosshair show/hide sync coordinator (assigned once quadrants exist)

  function persistAll() { saveLayout(quadrants, maximizedId); }

  function applyMaximized() {
    gridEl.classList.toggle('has-max', maximizedId != null);
    quadrants.forEach((q) => {
      const on = q.state.id === maximizedId;
      q.el.classList.toggle('maximized', on);
      // Toggle the surrounding slot too so the CSS grid hides/expands it.
      const slot = q.el.closest ? q.el.closest('.quadrant-slot') : null;
      if (slot) slot.classList.toggle('maximized', on);
      q.state.isMaximized = on;
    });
    // Resize after the CSS layout settles (two rAFs) — and again ~150 ms later
    // as a defensive belt-and-suspenders. Going from a CSS-Grid relative slot
    // to a position:fixed maximized slot can leave LWC with stale dims for one
    // tick, which manifested as the X-axis not rendering in the maximized view.
    const doResize = () => quadrants.forEach((q) => q.resize());
    requestAnimationFrame(() => requestAnimationFrame(doResize));
    setTimeout(doResize, 150);
  }

  function onMaximize(q) {
    maximizedId = (maximizedId === q.state.id) ? null : q.state.id;
    applyMaximized();
    persistAll();
  }

  const slots = gridEl.querySelectorAll('.quadrant-slot');
  layout.forEach((cfg, i) => {
    const mount = slots[i] || gridEl;       // mount into the i-th slot if present
    const q = makeQuadrant(mount, cfg.ticker || 'NQ', cfg.tf || '1m', {
      id: i,
      maSet: cfg.maSet,
      ind: cfg.ind,                     // legacy migration: old {ema20,sma200} state maps to OV
      show1H: cfg.show1H,
      show4H: cfg.show4H,
      showSessions: cfg.showSessions,
      zoomRange: cfg.zoomRange,
      isMaximized: cfg.isMaximized,
      rollDays: cfg.rollDays,
      lines: cfg.lines,
      onPersist: persistAll,
      onMaximize: onMaximize,
      onTopicChange: () => { if (stream) stream.resubscribe(); },
      onTickerChange: (t, src) => { if (link) link.onLocalTickerChange(t, src); },
      onLinesChange: (t, src) => { if (lineSync) lineSync.onLocalLinesChange(t, src); },
      onCrosshairToggle: () => { if (crosshair) crosshair.toggle(); },
    });
    quadrants.push(q);
  });

  // Dual-view sub-quadrants live outside the persistent grid array so they're
  // never serialised to localStorage and never participate in the 2x2 layout.
  let dualQuadrants = [];
  stream = makeStreamManager(() => quadrants.concat(dualQuadrants));
  stream.start();
  if (maximizedId != null) applyMaximized();

  // ---------- Ticker-link toggle (next to the Dual button) ----------
  const linkBtn = document.getElementById('link-btn');
  link = makeTickerLink(() => quadrants.concat(dualQuadrants), linkBtn);
  lineSync = makeLineSync(() => quadrants.concat(dualQuadrants));
  crosshair = makeCrosshairSync(() => quadrants.concat(dualQuadrants));
  if (linkBtn) linkBtn.addEventListener('click', () => link.toggle());

  // ---------- Dual View (page-level side-by-side: 1H left + 15m right) ----------
  const dualBtn = document.getElementById('dual-btn');
  const dualContainer = document.getElementById('dual');
  function enterDualView() {
    if (dualQuadrants.length) return;
    const ticker = (quadrants[0] && quadrants[0].state.ticker) || 'NQ';
    const leftSlot  = dualContainer.querySelector('.dual-slot[data-side="left"]');
    const rightSlot = dualContainer.querySelector('.dual-slot[data-side="right"]');
    // Left = 1H, last 5 trading sessions (5 weekdays). Right = 15m, last 3
    // trading sessions. rollDays crops to the last N sessions AND fits the zoom
    // to exactly those bars (see the rollDays branch in the zoom logic) — so each
    // pane opens "perfectly fitting" its window, and tracks the newest bar live.
    const qLeft = makeQuadrant(leftSlot, ticker, '1h', {
      id: 'dual-left', follow: true, rollDays: 5,
      onTopicChange: () => { if (stream) stream.resubscribe(); },
      onTickerChange: (t, src) => { if (link) link.onLocalTickerChange(t, src); },
      onLinesChange: (t, src) => { if (lineSync) lineSync.onLocalLinesChange(t, src); },
      onCrosshairToggle: () => { if (crosshair) crosshair.toggle(); },
    });
    const qRight = makeQuadrant(rightSlot, ticker, '15m', {
      id: 'dual-right', follow: true, rollDays: 3,
      onTopicChange: () => { if (stream) stream.resubscribe(); },
      onTickerChange: (t, src) => { if (link) link.onLocalTickerChange(t, src); },
      onLinesChange: (t, src) => { if (lineSync) lineSync.onLocalLinesChange(t, src); },
      onCrosshairToggle: () => { if (crosshair) crosshair.toggle(); },
    });
    dualQuadrants = [qLeft, qRight];
    stream.resubscribe();
    if (crosshair) crosshair.sync();   // new dual charts adopt the shared crosshair state
    document.body.classList.add('dual-on');
    dualBtn.classList.add('active');
  }
  function exitDualView() {
    document.body.classList.remove('dual-on');
    dualBtn.classList.remove('active');
    dualQuadrants.forEach((q) => { try { q.destroy(); } catch (e) {} });
    dualQuadrants = [];
    stream.resubscribe();
  }
  if (dualBtn) {
    dualBtn.addEventListener('click', () => {
      if (dualQuadrants.length) exitDualView(); else enterDualView();
    });
  }

  // Lock in the indicator-default migration immediately (so it doesn't re-run
  // even if the page is closed without any interaction).
  if (saved && !saved.indDefaultsV2) persistAll();

  window.addEventListener('beforeunload', persistAll);
}

/* ================================================================== *
 * Pop-out / single-quadrant bootstrap
 * ================================================================== */
function bootSingle(containerEl, ticker, tf) {
  document.body.classList.add('single');
  let stream = null;
  let link = null;
  let lineSync = null;
  let crosshair = null;
  const q = makeQuadrant(containerEl, ticker, tf, {
    id: 0,
    onMaximize: function () {},          // no-op in a single window
    onTopicChange: function () { if (stream) stream.resubscribe(); },
    // Pop-out windows have no toggle button, but they still follow/broadcast
    // ticker-link state (shared via localStorage + BroadcastChannel) so a
    // separated window switches in lock-step with the grid when link is ON.
    onTickerChange: function (t, src) { if (link) link.onLocalTickerChange(t, src); },
    onLinesChange: function (t, src) { if (lineSync) lineSync.onLocalLinesChange(t, src); },
    // Pop-outs DO have a crosshair button (it's in every quadrant strip) — wire it
    // to the shared coordinator so it toggles every window in lock-step.
    onCrosshairToggle: function () { if (crosshair) crosshair.toggle(); },
  });
  link = makeTickerLink(function () { return [q]; }, null);
  lineSync = makeLineSync(function () { return [q]; });
  crosshair = makeCrosshairSync(function () { return [q]; });
  stream = makeStreamManager([q]);
  stream.start();
  return q;
}

// Expose for /quadrant pop-out page and external callers.
window.makeQuadrant = bootSingle;
window.makeQuadrantFactory = makeQuadrant;
window.bootGrid = bootGrid;

// Auto-boot the grid when a #grid element is present (the index page).
document.addEventListener('DOMContentLoaded', () => {
  const gridEl = document.getElementById('grid');
  if (gridEl) bootGrid(gridEl);
});

})();
