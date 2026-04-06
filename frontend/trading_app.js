/**
 * BTC 虛擬交易中心 - 前端邏輯
 * 接 /api/btc-trading/* 端點
 */

let isTradingActive = false;
let statusTimer = null;

const ui = {
    btcPrice: document.getElementById('btc-price'),
    initialBalance: document.getElementById('initial-balance'),
    equity: document.getElementById('trade-equity'),
    pl: document.getElementById('trade-pl'),
    plCard: document.getElementById('pl-card'),
    historyBody: document.getElementById('trade-history-body'),
    tradeCount: document.getElementById('trade-count'),
    holdings: document.getElementById('holdings-display'),
    btnToggle: document.getElementById('btn-toggle-trading-hero'),
    btnRunOnce: document.getElementById('btn-run-once'),
    statusLabel: document.getElementById('sidebar-status'),
    heroBlock: document.getElementById('status-hero'),
    refreshTime: document.getElementById('last-refresh-time'),
    strategiesList: document.getElementById('strategies-list'),
    fngValue: document.getElementById('fng-value'),
    fngLabel: document.getElementById('fng-label'),
    fngMarker: document.getElementById('fng-marker'),
    frPct: document.getElementById('fr-pct'),
};

async function init() {
    if (ui.btnToggle) ui.btnToggle.addEventListener('click', toggleTrading);
    if (ui.btnRunOnce) ui.btnRunOnce.addEventListener('click', runOnce);

    await refreshAll();
    statusTimer = setInterval(refreshAll, 15000);
}

async function refreshAll() {
    await Promise.all([refreshStatus(), refreshHistory()]);
    if (ui.refreshTime) {
        ui.refreshTime.textContent = '最後更新: ' + new Date().toLocaleTimeString();
    }
}

// ── 開關交易 ──

async function toggleTrading() {
    try {
        const next = !isTradingActive;
        const res = await fetch('/api/btc-trading/toggle?active=' + next, { method: 'POST' });
        const data = await res.json();
        isTradingActive = data.is_active;
        updateToggleUI();
    } catch (e) {
        console.error('Toggle failed', e);
    }
}

async function runOnce() {
    try {
        ui.btnRunOnce.textContent = '檢查中...';
        ui.btnRunOnce.disabled = true;
        await fetch('/api/btc-trading/run-once', { method: 'POST' });
        setTimeout(async () => {
            await refreshAll();
            ui.btnRunOnce.textContent = '手動檢查一次';
            ui.btnRunOnce.disabled = false;
        }, 3000);
    } catch (e) {
        ui.btnRunOnce.textContent = '手動檢查一次';
        ui.btnRunOnce.disabled = false;
    }
}

// ── 狀態刷新 ──

async function refreshStatus() {
    try {
        const res = await fetch('/api/btc-trading/status');
        const d = await res.json();

        isTradingActive = d.is_active;
        updateToggleUI();

        // BTC 價格
        if (ui.btcPrice && d.btc_price) {
            ui.btcPrice.textContent = '$' + Number(d.btc_price).toLocaleString(undefined, {maximumFractionDigits: 0});
        }

        // 初始本金
        if (ui.initialBalance && d.initial_balance) {
            ui.initialBalance.textContent = '$' + Number(d.initial_balance).toLocaleString();
        }

        // 淨值
        if (ui.equity) {
            ui.equity.textContent = '$' + Number(d.equity).toLocaleString(undefined, {maximumFractionDigits: 0});
        }

        // 總報酬率
        if (ui.pl) {
            const pct = d.total_return_pct || 0;
            const prefix = pct >= 0 ? '+' : '';
            ui.pl.textContent = prefix + pct.toFixed(2) + '%';
            if (ui.plCard) {
                ui.plCard.style.color = pct >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';
            }
        }

        // 持倉
        renderHoldings(d.holdings, d.btc_price);

        // 策略列表
        renderStrategies(d.strategies);

        // 恐懼貪婪 — 從最新信號取得（需要額外邏輯，先用 API）
        fetchFlowInfo();

    } catch (e) {
        console.warn('Status refresh failed', e);
    }
}

// ── 恐懼貪婪指數 ──

async function fetchFlowInfo() {
    try {
        const res = await fetch('/api/btc-trading/flow-info');
        const d = await res.json();
        if (ui.fngValue) ui.fngValue.textContent = d.fear_greed;
        if (ui.fngLabel) {
            ui.fngLabel.textContent = d.fng_class;
            ui.fngLabel.style.color = getFngColor(d.fear_greed);
        }
        if (ui.fngMarker) ui.fngMarker.style.left = d.fear_greed + '%';
        if (ui.frPct) ui.frPct.textContent = d.funding_rate_pct.toFixed(1) + '%';
    } catch (e) {
        // flow-info endpoint 可能不存在，靜默失敗
    }
}

function getFngColor(val) {
    if (val <= 25) return '#ef4444';
    if (val <= 45) return '#f59e0b';
    if (val <= 55) return 'var(--text-muted)';
    if (val <= 75) return '#22c55e';
    return '#16a34a';
}

// ── 持倉渲染 ──

function renderHoldings(holdings, btcPrice) {
    if (!ui.holdings) return;
    const syms = Object.keys(holdings || {});
    if (syms.length === 0) {
        ui.holdings.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-muted); font-size:13px; font-weight:600">目前無持倉</div>';
        return;
    }

    ui.holdings.innerHTML = syms.map(sym => {
        const h = holdings[sym];
        const qty = h.qty;
        const avg = h.avg_price;
        const mktVal = btcPrice ? (qty * btcPrice) : (qty * avg);
        const cost = qty * avg;
        const pnl = mktVal - cost;
        const pnlPct = cost > 0 ? (pnl / cost * 100) : 0;
        const pnlColor = pnl >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';

        return `
            <div>
                <div class="hold-detail">
                    <span class="hold-label">數量</span>
                    <span class="hold-value">${qty.toFixed(6)} BTC</span>
                </div>
                <div class="hold-detail">
                    <span class="hold-label">均價</span>
                    <span class="hold-value">$${avg.toLocaleString()}</span>
                </div>
                <div class="hold-detail">
                    <span class="hold-label">市值</span>
                    <span class="hold-value">$${mktVal.toLocaleString(undefined,{maximumFractionDigits:0})}</span>
                </div>
                <div class="hold-detail">
                    <span class="hold-label">未實現損益</span>
                    <span class="hold-value" style="color:${pnlColor}">${pnl >= 0 ? '+' : ''}$${pnl.toLocaleString(undefined,{maximumFractionDigits:0})} (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)</span>
                </div>
                <div class="hold-detail">
                    <span class="hold-label">進場時間</span>
                    <span class="hold-value" style="font-size:12px">${h.time}</span>
                </div>
            </div>
        `;
    }).join('');
}

// ── 策略列表渲染 ──

function renderStrategies(strategies) {
    if (!ui.strategiesList || !strategies) return;
    ui.strategiesList.innerHTML = strategies.map(s => {
        return `
            <div class="strategy-card">
                <div>
                    <span class="strategy-id">${s.id}</span>
                    <span class="strategy-name">${s.name}</span>
                </div>
                <div class="strategy-meta">
                    門檻 ${s.threshold}分 | Flow: ${s.use_flow ? '是' : '否'}
                </div>
                <div class="strategy-backtest">
                    回測: ${s.backtest}
                </div>
            </div>
        `;
    }).join('');
}

// ── 交易歷史 ──

async function refreshHistory() {
    try {
        const res = await fetch('/api/btc-trading/history');
        const history = await res.json();

        if (ui.tradeCount) ui.tradeCount.textContent = history.length + ' 筆交易';
        if (!ui.historyBody) return;

        if (history.length === 0) {
            ui.historyBody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:40px; color:var(--text-muted); font-weight:600">尚無交易紀錄，開啟自動交易後會自動偵測進場時機</td></tr>';
            return;
        }

        ui.historyBody.innerHTML = history.map(t => {
            const isBuy = t.type === 'BUY';
            const typeColor = isBuy ? 'var(--buy-color)' : 'var(--sell-color)';
            const typeText = isBuy ? '買入' : '賣出';
            const amount = isBuy ? t.cost : t.income;
            const plText = t.profit !== undefined
                ? `<span style="color:${t.profit >= 0 ? 'var(--buy-color)' : 'var(--sell-color)'}; font-weight:700">${t.profit >= 0 ? '+' : ''}$${Number(t.profit).toLocaleString(undefined,{maximumFractionDigits:0})}</span>`
                : '--';

            // 從 signal 欄位提取策略 ID
            const sigMatch = t.signal ? t.signal.match(/\[(S\d)\]/) : null;
            const stratTag = sigMatch ? sigMatch[1] : '--';

            return `
                <tr>
                    <td style="color:var(--text-muted); font-size:12px">${t.time}</td>
                    <td style="color:${typeColor}; font-weight:700">${typeText}</td>
                    <td>$${Number(t.price).toLocaleString()}</td>
                    <td>${Number(t.qty).toFixed(6)}</td>
                    <td>$${Number(amount).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                    <td>${plText}</td>
                    <td><span class="strategy-id">${stratTag}</span></td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.warn('History refresh failed', e);
    }
}

// ── UI 狀態切換 ──

function updateToggleUI() {
    if (isTradingActive) {
        if (ui.btnToggle) {
            ui.btnToggle.textContent = '停止自動交易';
            ui.btnToggle.className = 'btn-stop btn-hero';
        }
        if (ui.statusLabel) {
            ui.statusLabel.textContent = '策略監控中';
            ui.statusLabel.className = 'status-badge active';
        }
        if (ui.heroBlock) {
            ui.heroBlock.style.background = 'rgba(52, 211, 153, 0.06)';
            ui.heroBlock.style.borderColor = 'rgba(52, 211, 153, 0.3)';
        }
    } else {
        if (ui.btnToggle) {
            ui.btnToggle.textContent = '開啟自動交易';
            ui.btnToggle.className = 'btn-start btn-hero';
        }
        if (ui.statusLabel) {
            ui.statusLabel.textContent = '未啟動';
            ui.statusLabel.className = 'status-badge inactive';
        }
        if (ui.heroBlock) {
            ui.heroBlock.style.background = 'rgba(167, 139, 250, 0.03)';
            ui.heroBlock.style.borderColor = 'var(--border-color)';
        }
    }
}

document.addEventListener('DOMContentLoaded', init);
