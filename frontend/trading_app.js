/**
 * Trading Station - 虛擬交易中心前端邏輯
 */

let isTradingActive = false;
let tradeStatusTimer = null;

const ui = {
    equity: document.getElementById('trade-equity'),
    pl: document.getElementById('trade-pl'),
    plCard: document.getElementById('pl-card'),
    historyBody: document.getElementById('trade-history-body'),
    holdings: document.getElementById('holdings-display'),
    btnToggleHero: document.getElementById('btn-toggle-trading-hero'),
    statusLabel: document.getElementById('sidebar-status'),
    heroBlock: document.getElementById('status-hero'),
    refreshTime: document.getElementById('last-refresh-time'),
    symbolList: document.getElementById('active-symbols-list'),
    btnAddSymbol: document.getElementById('btn-add-symbol'),
    inputSymbol: document.getElementById('new-symbol-input'),
    // 篩選與分頁組件
    filterSymbol: document.getElementById('filter-symbol'),
    filterStart: document.getElementById('filter-start'),
    filterEnd: document.getElementById('filter-end'),
    btnApplyFilters: document.getElementById('btn-apply-filters'),
    btnResetFilters: document.getElementById('btn-reset-filters'),
    btnPrevPage: document.getElementById('btn-prev-page'),
    btnNextPage: document.getElementById('btn-next-page'),
    currentPageLabel: document.getElementById('current-page-label'),
    totalCountLabel: document.getElementById('total-count'),
    pageStartIdxLabel: document.getElementById('page-start-idx'),
    pageEndIdxLabel: document.getElementById('page-end-idx')
};

// 篩選與分頁狀態
let historyPage = 1;
const historyPageSize = 15;
let currentFilters = {
    symbol: '',
    start: '',
    end: ''
};

async function init() {
    if (ui.btnToggleHero) {
        ui.btnToggleHero.addEventListener('click', toggleTradingStatus);
    }
    
    if (ui.btnAddSymbol) {
        ui.btnAddSymbol.addEventListener('click', addWatchlistSymbol);
    }
    
    if (ui.inputSymbol) {
        ui.inputSymbol.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') addWatchlistSymbol();
        });
    }

    // 篩選與分頁事件
    if (ui.btnApplyFilters) ui.btnApplyFilters.addEventListener('click', applyHistoryFilters);
    if (ui.btnResetFilters) ui.btnResetFilters.addEventListener('click', resetHistoryFilters);
    if (ui.btnPrevPage) ui.btnPrevPage.addEventListener('click', () => changeHistoryPage(-1));
    if (ui.btnNextPage) ui.btnNextPage.addEventListener('click', () => changeHistoryPage(1));
    
    // 初始載入
    await refreshAll();
    
    // 每 10 秒刷新
    tradeStatusTimer = setInterval(refreshAll, 10000);
}

async function refreshAll() {
    await refreshTradingStatus();
    await refreshTradingHistory();
    await refreshWatchlist();
    if (ui.refreshTime) {
        ui.refreshTime.textContent = `最後更新: ${new Date().toLocaleTimeString()}`;
    }
}

async function toggleTradingStatus() {
    try {
        const nextStatus = !isTradingActive;
        const res = await fetch(`/api/trading/toggle?active=${nextStatus}`, { method: 'POST' });
        const data = await res.json();
        
        isTradingActive = data.is_active;
        updateUI();
        
    } catch (e) {
        console.error("Toggle trading failed", e);
    }
}

async function refreshTradingStatus() {
    try {
        const res = await fetch('/api/trading/status');
        const data = await res.json();
        
        isTradingActive = data.is_active;
        
        if (ui.equity) ui.equity.textContent = data.equity.toLocaleString();
        if (ui.pl) {
            const prefix = data.unrealized_pl >= 0 ? "+" : "";
            const percent = ((data.unrealized_pl / 1000000) * 100).toFixed(2);
            ui.pl.textContent = `${prefix}${data.unrealized_pl.toLocaleString()} (${prefix}${percent}%)`;
            
            if (ui.plCard) {
                ui.plCard.style.color = data.unrealized_pl >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';
            }
        }
        
        renderHoldings(data.holdings);
        updateUI();
    } catch (e) {
        console.warn("Status refresh failed");
    }
}

function renderHoldings(holdings) {
    if (!ui.holdings) return;
    const symbols = Object.keys(holdings);
    if (symbols.length === 0) {
        ui.holdings.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-muted); font-size:13px; font-weight:600">目前帳戶無持倉</div>';
        return;
    }
    
    ui.holdings.innerHTML = symbols.map(sym => {
        const h = holdings[sym];
        return `
            <div class="hold-item" style="margin-bottom:15px; border-bottom:1.5px solid rgba(167,139,250,0.08); padding-bottom:10px">
                <div class="hold-info">
                    <span class="hold-symbol">${sym.split('.')[0]}</span>
                    <span class="hold-qty" style="font-size:12px; color:var(--text-muted)">${h.qty} 股 / 成本 ${h.avg_price}</span>
                </div>
                <div class="hold-price" style="text-align:right">
                    <div style="font-size:10px; color:var(--text-muted)">${h.time}</div>
                    <div style="color:var(--primary); font-size:12px; font-weight:700">持有中</div>
                </div>
            </div>
        `;
    }).join('');
}

async function refreshTradingHistory() {
    try {
        const params = new URLSearchParams({
            page: historyPage,
            pageSize: historyPageSize,
            symbol: currentFilters.symbol,
            startDate: currentFilters.start,
            endDate: currentFilters.end
        });
        
        const res = await fetch(`/api/trading/history?${params.toString()}`);
        const result = await res.json();
        const history = result.data;
        
        if (!ui.historyBody) return;
        
        if (history.length === 0) {
            ui.historyBody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:40px; color:var(--text-muted); font-weight:600">尚無符合篩選條件的交易紀錄</td></tr>';
            updatePaginationUI(0, 0);
            return;
        }
        
        ui.historyBody.innerHTML = history.map(t => {
            const isBuy = t.type === 'BUY';
            const plText = t.profit !== undefined ?
                `<span style="color:${t.profit >= 0 ? 'var(--buy-color)' : 'var(--sell-color)'}; font-weight:700">${t.profit >= 0 ? '+' : ''}${t.profit.toLocaleString()}</span>` : '--';

            return `
                <tr>
                    <td style="color:var(--text-muted)">${t.time}</td>
                    <td style="font-weight:800">${t.symbol.split('.')[0]}</td>
                    <td style="color:${isBuy ? 'var(--buy-color)' : 'var(--sell-color)'}; font-weight:700">${isBuy ? '買進建倉' : '賣出平倉'}</td>
                    <td>${t.price}</td>
                    <td>${t.qty}</td>
                    <td>${plText}</td>
                </tr>
            `;
        }).join('');

        updatePaginationUI(result.total, history.length);
    } catch (e) {
        console.warn("History refresh failed", e);
    }
}

// 篩選套用
function applyHistoryFilters() {
    currentFilters.symbol = ui.filterSymbol.value.trim();
    currentFilters.start = ui.filterStart.value;
    currentFilters.end = ui.filterEnd.value;
    historyPage = 1; // 重置到第一頁
    refreshTradingHistory();
}

// 篩選重置
function resetHistoryFilters() {
    ui.filterSymbol.value = '';
    ui.filterStart.value = '';
    ui.filterEnd.value = '';
    currentFilters = { symbol: '', start: '', end: '' };
    historyPage = 1;
    refreshTradingHistory();
}

// 分頁切換
function changeHistoryPage(delta) {
    historyPage += delta;
    if (historyPage < 1) historyPage = 1;
    refreshTradingHistory();
}

// 更新分頁 UI
function updatePaginationUI(total, countOnPage) {
    if (ui.totalCountLabel) ui.totalCountLabel.textContent = total;
    if (ui.currentPageLabel) ui.currentPageLabel.textContent = historyPage;
    
    const startIdx = total === 0 ? 0 : (historyPage - 1) * historyPageSize + 1;
    const endIdx = startIdx + countOnPage - 1;
    
    if (ui.pageStartIdxLabel) ui.pageStartIdxLabel.textContent = startIdx;
    if (ui.pageEndIdxLabel) ui.pageEndIdxLabel.textContent = endIdx;
    
    if (ui.btnPrevPage) ui.btnPrevPage.disabled = (historyPage === 1);
    if (ui.btnNextPage) ui.btnNextPage.disabled = (endIdx >= total);
}

function updateUI() {
    if (isTradingActive) {
        if (ui.btnToggleHero) {
            ui.btnToggleHero.textContent = '停止自動交易';
            ui.btnToggleHero.className = 'btn-stop btn-hero';
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
        if (ui.btnToggleHero) {
            ui.btnToggleHero.textContent = '開啟自動交易';
            ui.btnToggleHero.className = 'btn-start btn-hero';
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

// ============================================================
// 監控標的管理
// ============================================================

async function refreshWatchlist() {
    try {
        const res = await fetch('/api/trading/symbols');
        const symbols = await res.json();
        renderWatchlist(symbols);
    } catch (e) {
        console.warn("Watchlist refresh failed");
    }
}

function renderWatchlist(symbols) {
    if (!ui.symbolList) return;
    
    ui.symbolList.innerHTML = symbols.map(sym => `
        <div class="symbol-chip" style="display:flex; align-items:center; gap:6px; background:rgba(167,139,250,0.1); border:1.5px solid rgba(167,139,250,0.25); padding:5px 12px; border-radius:15px; font-size:12px; color:var(--text-main); font-weight:700">
            <span>${sym}</span>
            <span onclick="removeWatchlistSymbol('${sym}')" style="cursor:pointer; color:var(--sell-color); font-weight:bold; margin-left:4px">×</span>
        </div>
    `).join('');
}

async function addWatchlistSymbol() {
    const symbol = ui.inputSymbol.value.trim().toUpperCase();
    if (!symbol) return;
    
    try {
        const res = await fetch(`/api/trading/symbols/add?symbol=${encodeURIComponent(symbol)}`, { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            ui.inputSymbol.value = '';
            renderWatchlist(data.symbols);
        } else {
            alert('標的已在名單中');
        }
    } catch (e) {
        alert('新增失敗');
    }
}

async function removeWatchlistSymbol(symbol) {
    if (!confirm(`確定要取消監控 ${symbol} 嗎？`)) return;
    
    try {
        const res = await fetch(`/api/trading/symbols/remove?symbol=${encodeURIComponent(symbol)}`, { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            renderWatchlist(data.symbols);
        }
    } catch (e) {
        alert('移除失敗');
    }
}

// 暴露給 window 供 onclick 使用
window.removeWatchlistSymbol = removeWatchlistSymbol;

document.addEventListener('DOMContentLoaded', init);
