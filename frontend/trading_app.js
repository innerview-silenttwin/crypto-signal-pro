/**
 * 幣圈交易中心 - BTC 四策略交易前端邏輯
 * 接 /api/btc-trading/* 端點
 * 
 * 損益與策略顯示對齊台股交易中心格式
 */

let isTradingActive = false;
let statusTimer = null;
let latestStatus = null;  // 保存最新狀態供損益計算用
let latestHistory = []; // 保存最新歷史紀錄供損益計算用
let strategyBarChart = null;

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
    renderStrategyBarChart();
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
        latestStatus = d;

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

        // 總報酬率 — 對齊台股格式：金額 + 百分比
        if (ui.pl) {
            const pct = d.total_return_pct || 0;
            const plAmount = d.equity - d.initial_balance;
            const prefix = pct >= 0 ? '+' : '';
            const amtStr = prefix + '$' + Math.round(Math.abs(plAmount)).toLocaleString();
            ui.pl.innerHTML = `${plAmount < 0 ? '-' : ''}${amtStr} <span style="font-size:14px; opacity:0.8">(${prefix}${pct.toFixed(2)}%)</span>`;
            if (ui.plCard) {
                ui.plCard.style.borderTop = `2px solid ${pct >= 0 ? 'var(--buy-color)' : 'var(--sell-color)'}`;
            }
            ui.pl.style.color = pct >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';
        }

        // 持倉
        renderHoldings(d.holdings, d.btc_price);

        // 策略列表
        renderStrategies(d.strategies);

        // 恐懼貪婪
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

        // 持倉時間顯示
        let holdTimeStr = '--';
        if (h.time) {
            const ms = Date.now() - new Date(h.time).getTime();
            const holdHours = ms / (1000 * 60 * 60);
            if (holdHours >= 24) {
                holdTimeStr = `${(holdHours / 24).toFixed(1)} 天`;
            } else {
                holdTimeStr = `${holdHours.toFixed(1)} 小時`;
            }
        }
        
        const stratId = h.strat_id || sym;

        return `
            <div style="margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid rgba(255,255,255,0.05)">
                <div style="font-weight:700; color:var(--text-main); margin-bottom:8px">策略 ${stratId} 持倉</div>
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
                    <span class="hold-label">持倉時間</span>
                    <span class="hold-value" style="font-size:12px">${holdTimeStr}</span>
                </div>
                <div class="hold-detail">
                    <span class="hold-label">進場時間</span>
                    <span class="hold-value" style="font-size:12px">${h.time}</span>
                </div>
            </div>
        `;
    }).join('');
}

// ── 策略列表渲染（對齊台股：顯示買入/賣出門檻 + 即時分數）──

function renderStrategies(strategies) {
    if (!ui.strategiesList || !strategies) return;
    ui.strategiesList.innerHTML = strategies.map(s => {
        const buyColor = (s.current_buy_score >= s.buy_threshold && s.current_buy_score > 0) ? 'color:var(--buy-color)' : 'color:var(--text-main)';
        const sellColor = (s.current_sell_score >= s.sell_threshold && s.current_sell_score > 0) ? 'color:var(--sell-color)' : 'color:var(--text-main)';
        
        return `
            <div class="strategy-card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <span class="strategy-id">${s.id}</span>
                        <span class="strategy-name">${s.name}</span>
                    </div>
                    <div style="text-align:right; font-size:12px; font-weight:700;">
                        <span style="font-size:10px; color:var(--text-muted); font-weight:600; margin-right:4px">現分</span>
                        買 <span style="${buyColor}">${s.current_buy_score != null ? s.current_buy_score.toFixed(1) : '--'}</span> / 
                        賣 <span style="${sellColor}">${s.current_sell_score != null ? s.current_sell_score.toFixed(1) : '--'}</span>
                    </div>
                </div>
                <div class="strategy-meta">
                    門檻：買 ${s.buy_threshold}分 | 賣 ${s.sell_threshold}分 | Flow: ${s.use_flow ? '是' : '否'}
                </div>
                <div class="strategy-backtest">
                    回測: ${s.backtest}
                </div>
            </div>
        `;
    }).join('');
}

// ── 交易歷史（對齊台股：損益已實現/未實現 + 策略+分數）──

async function refreshHistory() {
    try {
        const res = await fetch('/api/btc-trading/history');
        const history = await res.json();
        latestHistory = history;

        if (ui.tradeCount) ui.tradeCount.textContent = history.length + ' 筆交易';
        if (!ui.historyBody) return;

        if (history.length === 0) {
            ui.historyBody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:40px; color:var(--text-muted); font-weight:600">尚無交易紀錄，開啟自動交易後會自動偵測進場時機</td></tr>';
            return;
        }

        // 為了計算買入的損益，我們需要追蹤每筆買入的後續狀態
        // 反序歷史（最新在前），找每筆 BUY 對應的 SELL
        const btcPrice = latestStatus ? latestStatus.btc_price : null;
        const currentHolding = latestStatus ? latestStatus.holdings : {};

        ui.historyBody.innerHTML = history.map(t => {
            const isBuy = t.type === 'BUY';
            const typeColor = isBuy ? 'var(--buy-color)' : 'var(--sell-color)';
            const typeText = isBuy ? '買入' : '賣出';
            const amount = isBuy ? t.cost : t.income;

            // ── 損益顯示（對齊台股格式）──
            let plText;
            if (isBuy) {
                // 買入行：判斷這筆買入是否就是「當前持倉」
                const isCurrentHolding = currentHolding && Object.values(currentHolding).some(h => h.time === t.time);
                if (isCurrentHolding && btcPrice) {
                    // 這筆有持倉 — 計算未實現損益
                    const unrealized = (btcPrice - t.price) * t.qty;
                    const unrealizedPct = t.price > 0 ? ((btcPrice - t.price) / t.price * 100) : 0;
                    const color = unrealized >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';
                    plText = `<span style="color:${color}; font-weight:700">${unrealized >= 0 ? '+' : ''}$${Math.round(unrealized).toLocaleString()}</span>` +
                             `<small style="color:${color}; margin-left:2px">(${unrealizedPct >= 0 ? '+' : ''}${unrealizedPct.toFixed(1)}%)</small>` +
                             ` <small style="color:var(--text-muted)">未實現</small>`;
                } else {
                    plText = `<span style="color:var(--text-muted)">—</span>`;
                }
            } else {
                // 賣出行：顯示已實現損益 + 報酬率 + 持倉時間
                const profit = t.profit || 0;
                const changePct = t.change_pct;
                const holdHours = t.hold_hours;
                const color = profit >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';

                let pctStr = '';
                if (changePct != null) {
                    pctStr = ` <small style="color:${color}">(${changePct >= 0 ? '+' : ''}${changePct.toFixed(1)}%)</small>`;
                }
                let holdStr = '';
                if (holdHours != null) {
                    if (holdHours >= 24) {
                        holdStr = ` <small style="color:var(--text-muted)">${(holdHours/24).toFixed(1)}天</small>`;
                    } else {
                        holdStr = ` <small style="color:var(--text-muted)">${holdHours.toFixed(0)}h</small>`;
                    }
                }
                plText = `<span style="color:${color}; font-weight:700">${profit >= 0 ? '+' : ''}$${Math.round(profit).toLocaleString()}</span>` +
                         pctStr +
                         ` <small style="color:var(--text-muted)">已實現</small>` +
                         holdStr;
            }

            // ── 策略欄（對齊台股：顯示策略+分數）──
            let stratText = '';
            const signal = t.signal || '';
            const strategies = t.triggered_strategies || '';
            const buyScore = t.buy_score;
            const sellScore = t.sell_score;
            const sigLevel = t.signal_level;

            if (strategies) {
                // 新格式：有 triggered_strategies 欄位
                const stratBadges = strategies.split(',').map(s =>
                    `<span class="strategy-id">${s.trim()}</span>`
                ).join('');
                stratText = stratBadges;
                
                // 如果是停損或停利，必須顯示出來
                if (signal && (signal.includes('停損') || signal.includes('停利'))) {
                    stratText += `<span style="color:var(--sell-color); font-weight:700; font-size:11px; margin-left:4px">${signal.includes('停損') ? '⛔ 停損' : '🎯 停利'}</span>`;
                }
                
                if (buyScore != null && sellScore != null) {
                    stratText += `<span style="font-size:10px; color:var(--text-muted); margin-left:4px">買${buyScore}/賣${sellScore}</span>`;
                }
                if (sigLevel) {
                    stratText += `<span style="font-size:10px; color:var(--text-muted); margin-left:4px">${sigLevel}</span>`;
                }
            } else if (signal) {
                // 舊格式：從 signal 字串提取
                const sigMatch = signal.match(/\[(S[\d,]+)\](.*)/);
                if (sigMatch) {
                    const ids = sigMatch[1].split(',');
                    const restText = sigMatch[2] ? sigMatch[2].trim() : '';
                    stratText = ids.map(s => `<span class="strategy-id">${s.trim()}</span>`).join('');
                    if (restText) {
                        const scoreMatch = restText.match(/(\d+)分/);
                        if (scoreMatch) {
                            stratText += `<span style="font-size:10px; color:var(--text-muted); margin-left:4px">${scoreMatch[1]}分</span>`;
                        } else {
                            stratText += `<span style="font-size:10px; color:var(--text-muted); margin-left:4px">${restText}</span>`;
                        }
                    }
                } else if (signal.includes('停損') || signal.includes('停利')) {
                    stratText = `<span style="color:var(--sell-color); font-weight:700; font-size:11px">${signal.includes('停損') ? '⛔ 停損' : '🎯 停利'}</span>`;
                } else {
                    stratText = `<span style="font-size:11px; color:var(--text-muted)">${signal}</span>`;
                }
            } else {
                stratText = '--';
            }

            return `
                <tr>
                    <td style="color:var(--text-muted); font-size:12px">${t.time}</td>
                    <td style="color:${typeColor}; font-weight:700">${typeText}</td>
                    <td>$${Number(t.price).toLocaleString()}</td>
                    <td>${Number(t.qty).toFixed(6)}</td>
                    <td>$${Number(amount).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
                    <td>${plText}</td>
                    <td style="min-width:80px; white-space:nowrap">${stratText}</td>
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
            ui.heroBlock.style.borderColor = 'rgba(52, 211, 153, 0.2)';
        }
    } else {
        if (ui.btnToggle) {
            ui.btnToggle.textContent = '開啟自動交易';
            ui.btnToggle.className = 'btn-start';
        }
        if (ui.statusLabel) {
            ui.statusLabel.textContent = '未啟動';
            ui.statusLabel.className = 'status-badge inactive';
        }
        if (ui.heroBlock) {
            ui.heroBlock.style.background = 'rgba(167, 139, 250, 0.04)';
            ui.heroBlock.style.borderColor = 'var(--border-color)';
        }
    }
}

function renderStrategyBarChart() {
    const ctx = document.getElementById('strategy-bar-chart');
    if (!ctx) return;

    const strategies = ['S1', 'S2', 'S3', 'S4'];
    const realizedProfit = [0, 0, 0, 0];
    const unrealizedProfit = [0, 0, 0, 0];
    const realizedLoss = [0, 0, 0, 0];
    const unrealizedLoss = [0, 0, 0, 0];

    // 1. Calculate Realized PL per strategy from history
    latestHistory.forEach(t => {
        if (t.type === 'SELL' && t.profit) {
            let sId = null;
            if (t.triggered_strategies) {
                sId = t.triggered_strategies.split(',')[0].trim();
            } else if (t.signal) {
                const match = t.signal.match(/\[(S\d+)\]/);
                if (match) sId = match[1];
            }
            if (!sId) return;

            const idx = strategies.indexOf(sId);
            if (idx !== -1) {
                if (t.profit >= 0) realizedProfit[idx] += t.profit;
                else realizedLoss[idx] += Math.abs(t.profit);
            }
        }
    });

    // 2. Calculate Unrealized PL from holdings
    const holdings = latestStatus ? latestStatus.holdings : {};
    const btcPrice = latestStatus ? latestStatus.btc_price : 0;
    
    Object.keys(holdings).forEach(key => {
        const h = holdings[key];
        const sId = h.strat_id || (key.includes('_') ? key.split('_')[1] : null);
        if (!sId) return;

        const idx = strategies.indexOf(sId);
        if (idx !== -1 && btcPrice > 0) {
            const cost = h.qty * h.avg_price;
            const mkt = h.qty * btcPrice;
            const pnl = mkt - cost;
            if (pnl >= 0) unrealizedProfit[idx] += pnl;
            else unrealizedLoss[idx] += Math.abs(pnl);
        }
    });

    if (strategyBarChart) {
        strategyBarChart.data.datasets[0].data = realizedLoss;
        strategyBarChart.data.datasets[1].data = unrealizedLoss;
        strategyBarChart.data.datasets[2].data = realizedProfit;
        strategyBarChart.data.datasets[3].data = unrealizedProfit;
        strategyBarChart.update('none');
        return;
    }

    strategyBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: strategies,
            datasets: [
                {
                    label: '已實現虧損',
                    data: realizedLoss,
                    backgroundColor: 'rgba(239,68,68,0.85)',
                    borderColor: '#dc2626',
                    borderWidth: 1,
                    borderRadius: { topLeft: 0, topRight: 0, bottomLeft: 4, bottomRight: 4 },
                    stack: 'loss',
                },
                {
                    label: '未實現虧損',
                    data: unrealizedLoss,
                    backgroundColor: 'rgba(239,68,68,0.35)',
                    borderColor: '#dc2626',
                    borderWidth: 1,
                    borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
                    stack: 'loss',
                },
                {
                    label: '已實現獲利',
                    data: realizedProfit,
                    backgroundColor: 'rgba(52,211,153,0.85)',
                    borderColor: '#10b981',
                    borderWidth: 1,
                    borderRadius: { topLeft: 0, topRight: 0, bottomLeft: 4, bottomRight: 4 },
                    stack: 'profit',
                },
                {
                    label: '未實現獲利',
                    data: unrealizedProfit,
                    backgroundColor: 'rgba(52,211,153,0.35)',
                    borderColor: '#10b981',
                    borderWidth: 1,
                    borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
                    stack: 'profit',
                },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    position: 'bottom',
                    labels: { font: { size: 10 }, color: '#94a3b8', boxWidth: 12, padding: 8 }
                },
                tooltip: {
                    callbacks: {
                        label: item => ' ' + item.dataset.label + ': $' + Math.round(item.raw).toLocaleString()
                    }
                }
            },
            scales: {
                x: {
                    ticks: { font: { size: 11, weight: 'bold' }, color: '#94a3b8' },
                    grid: { display: false },
                },
                y: {
                    ticks: {
                        font: { size: 10 }, color: '#94a3b8',
                        callback: v => Math.abs(v) >= 1000 ? (v/1000).toFixed(0)+'K' : v
                    },
                    grid: { color: 'rgba(167,139,250,0.06)' }
                }
            }
        }
    });
}

document.addEventListener('DOMContentLoaded', init);
