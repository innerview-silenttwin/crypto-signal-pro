/**
 * 台股信號績效總覽 - 前端邏輯
 * 全股票大表格 + sparkline 趨勢圖 + 信號觸發 tooltip
 */

let perfData = null;
let sortCol = 'period_return';
let sortAsc = false;
let sparkCharts = {};       // sparkline chart instances by canvas id
let overlayChart = null;

const SECTOR_NAMES = {
    semiconductor: '半導體', electronics: '電子',
    finance: '金融', traditional: '傳產', default: '其他',
};

const COLUMNS = [
    { key: 'symbol',             label: '代碼',       sort: 'symbol',          type: 'text' },
    { key: 'name',               label: '名稱',       sort: 'name',            type: 'text' },
    { key: 'sector',             label: '產業',       sort: 'sector',          type: 'sector' },
    { key: 'period_return',      label: '漲跌幅%',    sort: 'period_return',   type: 'pct' },
    { key: 'max_drawdown',       label: '最大回撤%',  sort: 'max_drawdown',    type: 'dd' },
    { key: 'end_price',          label: '現價',       sort: 'end_price',       type: 'price' },
    { key: 'spark_price',        label: '股價走勢',   sort: null,              type: 'spark', dim: 'close' },
    { key: 'spark_tech',         label: '技術面',     sort: 'last_tech',       type: 'spark', dim: 'tech' },
    { key: 'spark_regime',       label: '盤勢層',     sort: 'last_regime',     type: 'spark', dim: 'regime' },
    { key: 'spark_chip',         label: '籌碼面',     sort: 'last_chip',       type: 'spark', dim: 'chip' },
    { key: 'last_regime_state',  label: '盤勢狀態',   sort: 'last_regime',     type: 'regime_tag' },
    { key: 'buy_count',          label: '技術買入',   sort: 'buy_count',       type: 'signal', sig: 'buy_triggers', cls: 'buy' },
    { key: 'strong_buy_count',   label: '技術強買',   sort: 'strong_buy_count', type: 'signal', sig: 'strong_buy_triggers', cls: 'buy' },
    { key: 'sell_count',         label: '技術賣出',   sort: 'sell_count',      type: 'signal', sig: 'sell_triggers', cls: 'sell' },
    { key: 'foreign_buy_count',  label: '外資連買',   sort: 'foreign_buy_count', type: 'signal', sig: 'foreign_buy', cls: 'chip' },
    { key: 'trust_buy_count',    label: '投信連買',   sort: 'trust_buy_count', type: 'signal', sig: 'trust_buy', cls: 'chip' },
    { key: 'regime_bull_count',  label: '多頭次數',   sort: 'regime_bull_count', type: 'signal', sig: 'regime_bull', cls: 'regime' },
    { key: 'regime_bottom_count', label: '底部轉強',  sort: 'regime_bottom_count', type: 'signal', sig: 'regime_bottom', cls: 'regime' },
];

// ── Init ──

async function init() {
    document.getElementById('btn-refresh').addEventListener('click', triggerRefresh);
    document.getElementById('filter-sector').addEventListener('change', renderTable);
    document.getElementById('filter-search').addEventListener('input', renderTable);
    setupFloatingTip();
    await loadData();
}

async function loadData() {
    const loadEl = document.getElementById('loading-panel');
    const mainEl = document.getElementById('main-panel');
    const msgEl = document.getElementById('loading-msg');

    loadEl.style.display = 'block';
    mainEl.style.display = 'none';

    try {
        const res = await fetch('/api/signal-performance');
        const data = await res.json();

        if (data.status === 'computing' || data.computing) {
            msgEl.textContent = data.message || '計算中，請稍候...';
            setTimeout(loadData, 8000);
            return;
        }
        if (data.status === 'no_cache') {
            msgEl.textContent = '首次計算中，約需 3-5 分鐘...';
            await fetch('/api/signal-performance/refresh', { method: 'POST' });
            setTimeout(loadData, 8000);
            return;
        }

        perfData = data;
        loadEl.style.display = 'none';
        mainEl.style.display = 'block';
        renderOverview();
        renderTable();
    } catch (e) {
        msgEl.textContent = '載入失敗: ' + e.message;
    }
}

async function triggerRefresh() {
    const btn = document.getElementById('btn-refresh');
    btn.textContent = '計算中...';
    btn.disabled = true;
    try {
        await fetch('/api/signal-performance/refresh', { method: 'POST' });
        setTimeout(async () => {
            await loadData();
            btn.textContent = '重新計算';
            btn.disabled = false;
        }, 5000);
    } catch (e) {
        btn.textContent = '重新計算';
        btn.disabled = false;
    }
}

// ── Overview ──

function renderOverview() {
    const m = perfData.meta;
    const stocks = perfData.stocks || [];
    const up = stocks.filter(s => s.period_return > 0).length;
    const down = stocks.filter(s => s.period_return < 0).length;
    const avgRet = stocks.length ? (stocks.reduce((a, s) => a + s.period_return, 0) / stocks.length).toFixed(2) : 0;

    document.getElementById('overview-row').innerHTML = `
        <div class="ov-chip">統計區間<span class="ov-val">${m.start_date} ~ ${m.end_date}</span></div>
        <div class="ov-chip">追蹤股票<span class="ov-val">${m.total_stocks} 檔</span></div>
        <div class="ov-chip">上漲<span class="ov-val" style="color:#10b981">${up}</span> / 下跌<span class="ov-val" style="color:#ef4444">${down}</span></div>
        <div class="ov-chip">平均漲跌<span class="ov-val">${avgRet}%</span></div>
        <div class="ov-chip">計算時間<span class="ov-val">${m.elapsed_seconds}秒</span></div>
    `;
    document.getElementById('last-update').textContent = '上次更新: ' + (m.generated_at || '--');
}

// ── Render Table ──

function renderTable() {
    if (!perfData || !perfData.stocks) return;
    destroyAllSparklines();

    const sectorFilter = document.getElementById('filter-sector').value;
    const searchFilter = document.getElementById('filter-search').value.toLowerCase().trim();

    let stocks = perfData.stocks.filter(s => {
        if (sectorFilter && s.sector !== sectorFilter) return false;
        if (searchFilter) {
            const code = s.symbol.replace('.TW', '').toLowerCase();
            const nm = (s.name || '').toLowerCase();
            if (!code.includes(searchFilter) && !nm.includes(searchFilter)) return false;
        }
        return true;
    });

    // Sort
    stocks = [...stocks].sort((a, b) => {
        const va = getSortValue(a, sortCol);
        const vb = getSortValue(b, sortCol);
        if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
        return sortAsc ? va - vb : vb - va;
    });

    document.getElementById('result-count').textContent = `${stocks.length} 檔`;

    // Header
    const thead = document.getElementById('main-thead');
    thead.innerHTML = '<tr>' + COLUMNS.map(c => {
        const cls = c.sort === sortCol ? (sortAsc ? 'sort-asc' : 'sort-desc') : '';
        return `<th class="${cls}" data-sort="${c.sort || ''}">${c.label}</th>`;
    }).join('') + '</tr>';

    thead.querySelectorAll('th').forEach(th => {
        const s = th.dataset.sort;
        if (s) th.addEventListener('click', () => {
            if (sortCol === s) sortAsc = !sortAsc;
            else { sortCol = s; sortAsc = false; }
            renderTable();
        });
    });

    // Body
    const tbody = document.getElementById('main-tbody');
    tbody.innerHTML = stocks.map((s, idx) => {
        return '<tr>' + COLUMNS.map(c => renderCell(s, c, idx)).join('') + '</tr>';
    }).join('');

    // Stock code links → navigate to main page
    tbody.querySelectorAll('.stock-link').forEach(a => {
        a.addEventListener('click', (e) => {
            e.preventDefault();
            const sym = a.dataset.symbol;
            localStorage.setItem('csp-pending-symbol', sym);
            localStorage.setItem('csp-pending-market', 'stock');
            window.location.href = 'index.html';
        });
    });

    // Draw sparklines after DOM is ready
    requestAnimationFrame(() => {
        stocks.forEach((s, idx) => {
            drawSparklines(s, idx);
        });
    });
}

function getSortValue(stock, col) {
    if (col === 'symbol') return stock.symbol;
    if (col === 'name') return stock.name || '';
    if (col === 'sector') return stock.sector || '';
    if (col === 'last_tech') return lastScore(stock, 'tech');
    if (col === 'last_regime') return lastScore(stock, 'regime');
    if (col === 'last_chip') return lastScore(stock, 'chip');

    // screener_summary fields
    const ss = stock.screener_summary || {};
    if (col in ss) return ss[col] || 0;

    return stock[col] ?? 0;
}

function lastScore(stock, dim) {
    const ds = stock.daily_scores;
    if (!ds || !ds.length) return 0;
    return ds[ds.length - 1][dim] ?? 0;
}

function renderCell(stock, col, idx) {
    const ss = stock.screener_summary || {};
    const sigs = stock.signals || {};

    switch (col.type) {
        case 'text':
            if (col.key === 'symbol') {
                const code = stock.symbol.replace('.TW', '');
                return `<td style="font-weight:700"><a href="index.html" class="stock-link" data-symbol="${stock.symbol}" title="在分析儀表板查看 ${code}">${code}</a></td>`;
            }
            return `<td>${stock[col.key] || ''}</td>`;

        case 'sector':
            return `<td><span class="sector-tag ${stock.sector}">${SECTOR_NAMES[stock.sector] || stock.sector}</span></td>`;

        case 'pct': {
            const v = stock[col.key] ?? 0;
            const cls = v >= 0 ? 'pos' : 'neg';
            return `<td class="num ${cls}">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</td>`;
        }

        case 'dd':
            return `<td class="num neg">${(stock[col.key] ?? 0).toFixed(2)}%</td>`;

        case 'price':
            return `<td class="num">${(stock[col.key] ?? 0).toFixed(1)}</td>`;

        case 'spark':
            return `<td class="spark-cell"><canvas id="spark-${col.dim}-${idx}" width="90" height="32"
                        data-dim="${col.dim}" data-idx="${idx}"></canvas></td>`;

        case 'regime_tag': {
            const ds = stock.daily_scores;
            const state = ds && ds.length ? ds[ds.length - 1].regime_state : '未知';
            const cls = ['強勢多頭', '多頭'].includes(state) ? 'bull' :
                        ['空頭', '高檔轉折'].includes(state) ? 'bear' : 'neutral';
            return `<td><span class="regime-tag ${cls}">${state}</span></td>`;
        }

        case 'signal': {
            const count = ss[col.key] || 0;
            const items = sigs[col.sig] || [];
            const zeroClass = count === 0 ? ' zero' : '';
            let tipData = '';
            if (count > 0 && items.length > 0) {
                // 儲存為 JSON 帶入 data 屬性，不內嵌 HTML
                tipData = JSON.stringify({ label: col.label, count, items: items.slice(-15).reverse() });
            }
            const dataTip = tipData ? ` data-tip='${tipData.replace(/'/g, '&#39;')}'` : '';
            return `<td class="num"><span class="sig-badge ${col.cls}${zeroClass}"${dataTip}>${count}</span></td>`;
        }

        default:
            return '<td>--</td>';
    }
}

// ── Sparklines ──

function drawSparklines(stock, idx) {
    const ds = stock.daily_scores;
    if (!ds || ds.length < 2) return;

    ['close', 'tech', 'regime', 'chip'].forEach(dim => {
        const canvasId = `spark-${dim}-${idx}`;
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const values = ds.map(d => d[dim] ?? 0);
        const color = sparkColor(dim, values);

        const ctx = canvas.getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: ds.map(d => d.date),
                datasets: [{
                    data: values,
                    borderColor: color,
                    borderWidth: 1.5,
                    pointRadius: 0,
                    fill: true,
                    backgroundColor: color.replace(')', ',0.08)').replace('rgb', 'rgba'),
                    tension: 0.3,
                }],
            },
            options: {
                responsive: false,
                animation: false,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                scales: { x: { display: false }, y: { display: false } },
                elements: { line: { borderWidth: 1.5 } },
            },
        });
        sparkCharts[canvasId] = chart;

        // Hover → show overlay
        canvas.addEventListener('mouseenter', (e) => showSparkOverlay(e, stock, dim, ds));
        canvas.addEventListener('mouseleave', hideSparkOverlay);
    });
}

function sparkColor(dim, values) {
    if (dim === 'close') {
        return values[values.length - 1] >= values[0] ? 'rgb(16,185,129)' : 'rgb(239,68,68)';
    }
    const last = values[values.length - 1];
    if (dim === 'tech') return last >= 60 ? 'rgb(16,185,129)' : last >= 40 ? 'rgb(245,158,11)' : 'rgb(239,68,68)';
    if (dim === 'regime') return last >= 70 ? 'rgb(16,185,129)' : last >= 50 ? 'rgb(245,158,11)' : 'rgb(239,68,68)';
    if (dim === 'chip') return last >= 65 ? 'rgb(59,130,246)' : 'rgb(148,163,184)';
    return 'rgb(167,139,250)';
}

function destroyAllSparklines() {
    Object.values(sparkCharts).forEach(c => c.destroy());
    sparkCharts = {};
}

// ── Spark Overlay (hover 大圖) ──

function showSparkOverlay(e, stock, dim, ds) {
    const overlay = document.getElementById('spark-overlay');
    const titleEl = document.getElementById('spark-overlay-title');
    const canvas = document.getElementById('spark-overlay-canvas');

    const dimNames = { close: '股價', tech: '技術面分數', regime: '盤勢分數', chip: '籌碼面分數' };
    titleEl.textContent = `${stock.symbol.replace('.TW', '')} ${stock.name} - ${dimNames[dim] || dim}`;

    const values = ds.map(d => d[dim] ?? 0);
    const labels = ds.map(d => d.date);
    const color = sparkColor(dim, values);

    if (overlayChart) overlayChart.destroy();
    overlayChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels,
            datasets: [{
                data: values,
                borderColor: color,
                borderWidth: 2,
                pointRadius: 2,
                pointHoverRadius: 4,
                fill: true,
                backgroundColor: color.replace(')', ',0.1)').replace('rgb', 'rgba'),
                tension: 0.3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: (ctx) => {
                            const v = ctx.raw;
                            return dim === 'close' ? `$${v}` : `${v}分`;
                        }
                    }
                },
            },
            scales: {
                x: {
                    ticks: { color: getTextColor(), maxTicksLimit: 8, font: { size: 10 } },
                    grid: { color: 'rgba(167,139,250,0.08)' },
                },
                y: {
                    ticks: { color: getTextColor(), font: { size: 10 } },
                    grid: { color: 'rgba(167,139,250,0.08)' },
                },
            },
        },
    });

    // Position overlay near mouse — 防止超出視窗任意邂
    const OVERLAY_W = 372;
    const OVERLAY_H = 212;
    const MARGIN = 12;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const rect = e.target.getBoundingClientRect();
    // 預設顯示在元素右側
    let left = rect.right + MARGIN;
    let top  = rect.top - 60;

    // 如果右側超出視窗，改為顯示在左側
    if (left + OVERLAY_W > vw - MARGIN) left = rect.left - OVERLAY_W - MARGIN;
    // 如果左側也不夠，就麨在左邂界
    if (left < MARGIN) left = MARGIN;

    // 預設垂直對齊元素中心
    top = Math.max(MARGIN, Math.min(top, vh - OVERLAY_H - MARGIN));

    overlay.style.left = left + 'px';
    overlay.style.top  = top + 'px';
    overlay.style.display = 'block';
}

function hideSparkOverlay() {
    document.getElementById('spark-overlay').style.display = 'none';
}

function getTextColor() {
    return getComputedStyle(document.documentElement).getPropertyValue('--text-primary').trim() || '#e2e8f0';
}

// ── Floating Tip (sig-badge hover) ──

function setupFloatingTip() {
    const tip = document.getElementById('sig-floating-tip');
    if (!tip) return;

    // 委托到 tbody，處理所有 sig-badge
    document.addEventListener('mouseover', (e) => {
        const badge = e.target.closest('.sig-badge[data-tip]');
        if (!badge) return;

        let payload;
        try { payload = JSON.parse(badge.dataset.tip); } catch { return; }

        const { label, count, items } = payload;
        const rows = items.map(it => {
            const detail = it.score != null ? `${it.score}分` :
                           it.days  != null ? `連買${it.days}天` :
                           it.state || '';
            return `<div class="tt-item">${it.date} <span>${detail}</span></div>`;
        }).join('');
        const more = (count > 15) ? `<div class="tt-item" style="opacity:0.5">...共 ${count} 次</div>` : '';
        tip.innerHTML = `<div class="tt-title">${label} (${count}次)</div>${rows}${more}`;
        tip.style.display = 'block';
        positionTip(tip, badge);
    });

    document.addEventListener('mousemove', (e) => {
        if (tip.style.display === 'none') return;
        const badge = e.target.closest('.sig-badge[data-tip]');
        if (badge) positionTip(tip, badge);
    });

    document.addEventListener('mouseout', (e) => {
        const badge = e.target.closest('.sig-badge[data-tip]');
        if (!badge) return;
        // 鵐標移入的目標不是同層 badge，則隱藏
        if (!e.relatedTarget || !e.relatedTarget.closest('.sig-badge[data-tip]')) {
            tip.style.display = 'none';
        }
    });
}

function positionTip(tip, badge) {
    const TIP_W = 224;
    const MARGIN = 10;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const rect = badge.getBoundingClientRect();

    // 預設顯示在 badge 右側
    let left = rect.right + MARGIN;
    let top  = rect.top;

    // 右側超出 → 改左側
    if (left + TIP_W > vw - MARGIN) left = rect.left - TIP_W - MARGIN;
    // 左側也超出 → 對齊左邂
    if (left < MARGIN) left = MARGIN;

    // 如果高度未知，先顯示再計算
    const tipH = tip.offsetHeight || 240;
    // 預設對齊 badge 上展
    if (top + tipH > vh - MARGIN) top = vh - tipH - MARGIN;
    if (top < MARGIN) top = MARGIN;

    tip.style.left = left + 'px';
    tip.style.top  = top  + 'px';
}

// ── Start ──

document.addEventListener('DOMContentLoaded', init);
