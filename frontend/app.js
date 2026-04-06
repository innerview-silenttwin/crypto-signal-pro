// WebSocket 連線與狀態管理
const ws = new WebSocket(`ws://${window.location.host}/ws/signals`);
let currentSymbol = '2330.TW';
let currentTimeframe = '1d';
let currentMarket = 'stock';
let lastServerData = [];

// 台股 / 期貨自動更新的 timer
let twAutoRefreshTimer = null;
let twCountdownTimer = null;
let heartbeatTimer = null;
// 走勢圖系列
let chart = null;
let candleSeries = null;
let emaShortSeries = null;
let emaLongSeries = null;
let bbUpperSeries = null, bbMidSeries = null, bbLowerSeries = null;
let volumeSeries = null;
let rsiSeries = null;
let cachedCandles = [];
// 記憶圖表的縮放狀態
let isInitialChartLoad = true;
let chartVisibleRange = null;

// Debug 日誌系統 (清空以移除干擾)
function logDebug(msg) {
    console.log(msg);
}

// UI 元素
const ui = {
    btc: {
        price: document.getElementById('price-btc'),
        res_alert: document.getElementById('res-btc'),
        d1: {
            score: document.getElementById('score-btc-1d'),
            ring: document.getElementById('ring-btc-1d'),
            dir: document.getElementById('dir-btc-1d'),
            lvl: document.getElementById('lvl-btc-1d'),
            buy: document.getElementById('buy-btc-1d'),
            sell: document.getElementById('sell-btc-1d')
        },
        h4: {
            score: document.getElementById('score-btc-4h'),
            ring: document.getElementById('ring-btc-4h'),
            dir: document.getElementById('dir-btc-4h'),
            buy: document.getElementById('buy-btc-4h')
        }
    },
    eth: {
        price: document.getElementById('price-eth'),
        dir1d: document.getElementById('dir-eth-1d'),
        score1d: document.getElementById('score-eth-1d')
    },
    sol: {
        price: document.getElementById('price-sol'),
        dir1d: document.getElementById('dir-sol-1d'),
        score1d: document.getElementById('score-sol-1d')
    },
    btcm: {
        price: document.getElementById('price-btc-mini'),
        dir1d: document.getElementById('dir-btc-mini-1d'),
        score1d: document.getElementById('score-btc-mini-1d')
    },
    status: document.getElementById('last-update'),
    connStatus: document.getElementById('connection-status'),
    statusDot: document.getElementById('status-dot')
};

// 價格格式化：至少顯示小數點後兩位
function formatPrice(val) {
    const num = parseFloat(val);
    if (isNaN(num)) return '--';
    // 小數位數：若原始值有更多小數位就保留，否則至少 2 位
    const str = num.toString();
    const decimalPart = str.includes('.') ? str.split('.')[1] : '';
    const minDecimals = 2;
    const decimals = Math.max(minDecimals, decimalPart.length > 8 ? 2 : decimalPart.length);
    return num.toLocaleString(undefined, { minimumFractionDigits: minDecimals, maximumFractionDigits: Math.min(decimals, 8) });
}

// 根據當前主題取得圖表配色
function getChartThemeColors() {
    var style = getComputedStyle(document.documentElement);
    return {
        background: style.getPropertyValue('--chart-bg').trim() || '#131722',
        textColor: style.getPropertyValue('--chart-text').trim() || '#d1d4dc',
        gridColor: style.getPropertyValue('--chart-grid').trim() || 'rgba(42, 46, 57, 0.5)',
        upColor: style.getPropertyValue('--chart-up').trim() || '#26a69a',
        downColor: style.getPropertyValue('--chart-down').trim() || '#ef5350',
        borderColor: style.getPropertyValue('--chart-border').trim() || 'rgba(197, 203, 206, 0.8)',
    };
}

// 主題切換時更新圖表配色
window.onThemeChange = function(theme) {
    if (!chart) return;
    // Give a tiny delay so CSS variables update first
    setTimeout(function() {
        var c = getChartThemeColors();
        chart.applyOptions({
            layout: {
                background: { type: 'solid', color: c.background },
                textColor: c.textColor,
            },
            grid: {
                vertLines: { color: c.gridColor },
                horzLines: { color: c.gridColor },
            },
            rightPriceScale: { borderColor: c.borderColor },
            timeScale: { borderColor: c.borderColor },
        });
        if (candleSeries) {
            candleSeries.applyOptions({
                upColor: c.upColor,
                downColor: c.downColor,
            });
        }
        if (volumeSeries) {
            volumeSeries.applyOptions({ color: c.upColor });
        }
    }, 20);
};

// 初始化 TradingView 圖表
function initChart() {
    logDebug("Starting initChart...");
    const container = document.getElementById('tv-chart');
    if (!container) {
        logDebug("ERROR: #tv-chart not found!");
        return;
    }

    try {
        if (typeof LightweightCharts === 'undefined') {
            logDebug("ERROR: LightweightCharts library not loaded!");
            return;
        }

        var themeColors = getChartThemeColors();
        chart = LightweightCharts.createChart(container, {
            width: container.clientWidth || 600,
            height: 300,
            layout: {
                background: { type: 'solid', color: themeColors.background },
                textColor: themeColors.textColor,
            },
            localization: {
                locale: 'zh-TW',
                // 自動偵測使用者瀏覽器時區
                timeFormatter: (timestamp) => {
                    return new Date(timestamp * 1000).toLocaleString('zh-TW', {
                        year: 'numeric',
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false
                    });
                },
            },
            grid: {
                vertLines: { color: themeColors.gridColor },
                horzLines: { color: themeColors.gridColor },
            },
            rightPriceScale: {
                borderColor: themeColors.borderColor,
            },
            timeScale: {
                borderColor: themeColors.borderColor,
                timeVisible: true,
                secondsVisible: false,
            },
        });

        logDebug("Chart object created.");

        // 終極備援方案：偵測並嘗試所有可能的方法
        if (typeof chart.addCandlestickSeries === 'function') {
            candleSeries = chart.addCandlestickSeries({
                upColor: themeColors.upColor, downColor: themeColors.downColor, borderVisible: false,
            });
            logDebug("Series: Used addCandlestickSeries");
        } else if (typeof chart.addSeries === 'function') {
            // 嘗試 V4 字串定義方式，捕捉內部的 undefined 錯誤
            try {
                // 如果 LightweightCharts.SeriesType 消失了，試著直接用字串 'Candlestick'
                const type = (LightweightCharts.SeriesType && LightweightCharts.SeriesType.Candlestick) ?
                    LightweightCharts.SeriesType.Candlestick : 'Candlestick';
                candleSeries = chart.addSeries(type, {
                    upColor: themeColors.upColor, downColor: themeColors.downColor,
                });
                logDebug("Series: Used addSeries(" + type + ")");
            } catch (err) {
                logDebug("addSeries Logic Error: " + err.message);
            }
        }

        // --- 加入成交量序列 ---
        volumeSeries = chart.addHistogramSeries({
            color: themeColors.upColor,
            priceFormat: { type: 'volume' },
            priceScaleId: '',
        });
        chart.priceScale('').applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 },
        });

        // --- 加入均線序列 ---
        emaShortSeries = chart.addLineSeries({
            color: '#fbbf24',
            lineWidth: 1,
            title: 'EMA Short',
        });

        emaLongSeries = chart.addLineSeries({
            color: '#3b82f6',
            lineWidth: 1,
            title: 'EMA Long',
        });

        // --- 加入布林帶 ---
        const bbOpt = { lineWidth: 1, lastValueVisible: false, priceLineVisible: false };
        bbUpperSeries = chart.addLineSeries({ ...bbOpt, color: 'rgba(156, 39, 176, 0.6)', title: 'BB Upper' });
        bbMidSeries = chart.addLineSeries({ ...bbOpt, color: 'rgba(158, 158, 158, 0.4)', title: 'BB Mid' });
        bbLowerSeries = chart.addLineSeries({ ...bbOpt, color: 'rgba(156, 39, 176, 0.6)', title: 'BB Lower' });

        // --- 加入 RSI (獨立座標軸) ---
        rsiSeries = chart.addLineSeries({
            color: '#26c6da',
            lineWidth: 1,
            title: 'RSI',
            priceScaleId: 'rsi-scale',
        });
        chart.priceScale('rsi-scale').applyOptions({
            scaleMargins: { top: 0.1, bottom: 0.7 }, // 將 RSI 置於上方不重疊 (或改到底部)
            visible: false, // 預設隱藏
        });

        if (!candleSeries) {
            // 列出所有可用的 add 方法以便診斷
            const methods = Object.keys(chart).filter(k => k.startsWith('add')).join(', ');
            logDebug("ERROR: No series method found. Available: " + (methods || "None"));
        } else {
            logDebug("Success: Chart and Series ready.");
            // 綁定指標控制面板事件
            initIndicatorControls();
        }
    } catch (e) {
        logDebug("Init Error: " + e.message);
    }

    // Resize observer
    new ResizeObserver(entries => {
        if (entries.length === 0 || entries[0].target !== container) { return; }
        const newRect = entries[0].contentRect;
        chart.applyOptions({ height: newRect.height, width: newRect.width });
    }).observe(container);


    // 綁定圖表時間框架切換按鈕
    const btns = document.querySelectorAll('.chart-toggles button');
    btns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            btns.forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            let tf = e.target.textContent.toLowerCase();
            currentTimeframe = tf;
            loadChartData(tf, true);
        });
    });

    initExpandLogic();
}

// Stock helper: 中英文名稱對照表（可擴充）
const stockNames = {
    '2330.TW': '台積電',
    '0050.TW': '元大台灣50',
    '2303.TW': '聯電',
    '2317.TW': '鴻海',
    '2412.TW': '中華電',
    '3231.TW': '緯創',
    '2382.TW': '廣達',
    '2454.TW': '聯發科',
    '2357.TW': '華碩',
};
const stockNameSearchMapping = {
    '台積電': '2330.TW',
    '台積': '2330.TW',
    '緯創': '3231.TW',
    '偉創': '3231.TW',
    '廣達': '2382.TW',
    '聯發科': '2454.TW',
    '鴻海': '2317.TW',
    '聯電': '2303.TW',
    '中華電': '2412.TW',
    '華碩': '2357.TW',
    '元大台灣50': '0050.TW',
    '0050': '0050.TW',
};
const futuresNames = {
    'TX': '台指期',
    'MTX': '小台指',
    'TE': '電子期',
    'TF': '金融期',
};
const cryptoNames = {
    '比特幣': 'BTC/USDT',
    '乙太幣': 'ETH/USDT',
    '以太幣': 'ETH/USDT',
    '索拉納': 'SOL/USDT',
    'BTC': 'BTC/USDT',
    'ETH': 'ETH/USDT',
    'SOL': 'SOL/USDT'
};

const quickSymbolButtons = {
    crypto: [
        { symbol: 'BTC/USDT', label: 'BTC' },
        { symbol: 'ETH/USDT', label: 'ETH' },
        { symbol: 'SOL/USDT', label: 'SOL' },
    ],
    stock: [
        { symbol: '2330.TW', label: '2330' },
        { symbol: '0050.TW', label: '0050' },
        { symbol: '2303.TW', label: '2303' },
    ],
    futures: [
        { symbol: 'TX', label: '台指期' },
        { symbol: 'MTX', label: '小台指' },
        { symbol: 'TE', label: '電子期' },
        { symbol: 'TF', label: '金融期' },
    ],
};

// 台灣市場自選清單（盤後信號微縮卡片用）
const twWatchlist = {
    stock: [
        { symbol: '2330.TW', label: '2330', name: '台積電' },
        { symbol: '0050.TW', label: '0050', name: '元大台灣50' },
        { symbol: '2317.TW', label: '2317', name: '鴻海' },
    ],
    futures: [
        { symbol: 'TX', label: 'TX', name: '台指期' },
        { symbol: 'MTX', label: 'MTX', name: '小台指' },
    ],
};

function renderSymbolSwitcher(market) {
    const container = document.getElementById('h-symbol-switcher');
    if (!container) return;

    // 從搜尋紀錄中篩選當前市場的歷史
    const historyItems = searchHistory
        .filter(h => h.market === market)
        .slice(0, 5);  // 最多 5 個

    // 若歷史不足，補上預設
    const defaults = quickSymbolButtons[market] || [];
    const seen = new Set(historyItems.map(h => h.sym));
    const fallbacks = defaults.filter(d => !seen.has(d.symbol));

    // 組合：歷史優先 + 預設補齊，最多 5 個
    const items = [];
    for (const h of historyItems) {
        items.push({ symbol: h.sym, label: _getDisplayName(h.sym, h.market, h.name) });
    }
    for (const f of fallbacks) {
        if (items.length >= 5) break;
        items.push({ symbol: f.symbol, label: _getDisplayName(f.symbol, market, f.label) });
    }

    container.innerHTML = items.map(item => {
        const isActive = currentSymbol === item.symbol;
        return `<button class="${isActive ? 'active' : ''}" onclick="window.changeSymbol('${item.symbol}','${market}')">${item.label}</button>`;
    }).join('');
}

function _getDisplayName(symbol, market, fallback) {
    if (market === 'stock') {
        return stockNames[symbol] || fallback || symbol.replace('.TW', '').replace('.TWO', '');
    }
    if (market === 'futures') {
        const code = symbol.split('.')[0];
        return futuresNames[code] || fallback || code;
    }
    // crypto: 顯示幣種簡稱
    const cryptoDisplayNames = { 'BTC/USDT': 'BTC', 'ETH/USDT': 'ETH', 'SOL/USDT': 'SOL' };
    return cryptoDisplayNames[symbol] || fallback || symbol.split('/')[0];
}

async function fetchStockInfo(symbol) {
    try {
        const res = await fetch(`/api/stock-info?symbol=${encodeURIComponent(symbol)}`);
        if (!res.ok) return null;
        return await res.json();
    } catch (e) {
        return null;
    }
}

// --- 搜尋紀錄邏輯 ---
let searchHistory = JSON.parse(localStorage.getItem('searchHistory') || '[]');

function addToHistory(sym, market, nameInput = null) {
    if (!sym) return;

    // 移除相同 symbol 的舊紀錄
    searchHistory = searchHistory.filter(item => item.sym.toLowerCase() !== sym.toLowerCase());

    // 取得展示用名稱
    let name = nameInput;
    if (!name) {
        if (market === 'stock') {
            name = stockNames[sym] || sym.split('.')[0];
        } else if (market === 'futures') {
            name = futuresNames[sym.split('.')[0]] || sym;
        } else {
            // 虛擬幣找中文名稱
            const cnEntry = Object.entries(cryptoNames).find(([k, v]) => v === sym && !/^[A-Z/]+$/.test(k));
            name = cnEntry ? cnEntry[0] : sym.split('/')[0];
        }
    }

    // 加入最前面
    searchHistory.unshift({ sym, market, name });

    // 限制最多 10 筆
    if (searchHistory.length > 10) searchHistory.pop();

    localStorage.setItem('searchHistory', JSON.stringify(searchHistory));
    renderHistory();
}

function renderHistory() {
    const list = document.getElementById('history-list');
    if (!list) return;

    // 1. 渲染側邊欄清單
    if (searchHistory.length === 0) {
        list.innerHTML = '<div class="history-empty">暫無搜尋紀錄</div>';
        return;
    }

    list.innerHTML = searchHistory.map(item => {
        // 字數限制：最多三個字，超過則截斷
        const displayName = item.name.length > 3 ? item.name.substring(0, 3) : item.name;
        const isActive = currentSymbol === item.sym;

        return `
            <div class="history-grid-item ${isActive ? 'active' : ''}" 
                 onclick="window.changeSymbol('${item.sym}', '${item.market}')"
                 title="${item.name} (${item.sym})">
                <div class="history-dot dot-${item.market}"></div>
                <span class="history-name">${displayName}</span>
            </div>
        `;
    }).join('');
}

function getMarketLabel(m) {
    const labels = { 'crypto': '加密幣', 'stock': '台股', 'futures': '期貨' };
    return labels[m] || m;
}

window.changeSymbol = async function (sym, market = 'crypto') {
    const isSymbolChanged = (currentSymbol !== sym || currentMarket !== market);
    currentSymbol = sym;
    currentMarket = market;

    const baseSym = sym.split('/')[0].split('.')[0];
    let compName = market === 'stock' ? (stockNames[sym] || '') : '';
    const futName = market === 'futures' ? (futuresNames[baseSym] || '') : '';

    // 若是台股且沒有快取名稱，嘗試線上查詢
    if (market === 'stock' && !compName) {
        const info = await fetchStockInfo(sym);
        if (info && info.name) {
            compName = info.name;
            stockNames[sym] = compName;
        }
    }

    // 更新標題
    const headerTitle = document.getElementById('main-idx-title');
    const chartTitle = document.getElementById('chart-title');
    const chartSymbol = document.getElementById('chart-symbol');

    // 清空舊價格避免錯位顯示
    if (ui.btc.price) ui.btc.price.textContent = '載入中...';

    if (headerTitle) headerTitle.textContent =
        market === 'stock' ? `台股 ${baseSym}${compName ? ' ' + compName : ''}` :
            market === 'futures' ? `台期 ${baseSym}${futName ? ' ' + futName : ''}` : sym;
    if (chartTitle) chartTitle.textContent =
        market === 'stock' ? `${baseSym}${compName ? ' ' + compName : ''} 走勢圖` :
            market === 'futures' ? `${baseSym} ${futName} 走勢圖` :
                `${baseSym} 即時走勢圖`;
    if (chartSymbol) chartSymbol.textContent =
        market === 'stock' ? `${baseSym}${compName ? ' · ' + compName : ''}` :
            market === 'futures' ? `${baseSym}${futName ? ' · ' + futName : ''}` : sym;

    // 加入搜尋紀錄 (現在已經有名稱了)
    addToHistory(sym, market, compName || (market === 'futures' ? futName : null));

    // 顯示/隱藏各市場輸入欄
    const stockRow = document.getElementById('stock-input-row');
    const cryptoRow = document.getElementById('crypto-input-row');
    const futuresRow = document.getElementById('futures-input-row');
    const marketSelect = document.getElementById('market-select');
    if (stockRow) stockRow.style.display = market === 'stock' ? 'flex' : 'none';
    if (cryptoRow) cryptoRow.style.display = market === 'crypto' ? 'flex' : 'none';
    if (futuresRow) futuresRow.style.display = market === 'futures' ? 'flex' : 'none';
    if (marketSelect) marketSelect.value = market;

    // 顯示/隱藏即時信號卡片與微縮卡片
    const signalCard = document.getElementById('card-btc');
    const symbolSwitcher = document.getElementById('h-symbol-switcher');
    const miniCards = document.querySelector('.mini-cards-row:not(#tw-mini-cards)');
    const twMiniCards = document.getElementById('tw-mini-cards');
    if (signalCard) signalCard.style.display = 'block';
    if (symbolSwitcher) symbolSwitcher.style.display = 'flex';
    if (miniCards) miniCards.style.display = market === 'crypto' ? 'flex' : 'none';
    if (twMiniCards) twMiniCards.style.display = (market === 'stock' || market === 'futures') ? 'flex' : 'none';

    // 若切到股票或期貨，先顯示「計算中」，並渲染自選清單
    if (market === 'stock' || market === 'futures') {
        clearSignalCard(market, true); // loading 狀態
        renderTwMiniCards(market);
    }

    // 台股/期貨：隱藏 4H/1H 圖表切換按鈕，只保留 1D
    const chartToggles = document.querySelectorAll('.chart-toggles button');
    chartToggles.forEach(btn => {
        const tf = btn.textContent.trim();
        if (tf === '4H' || tf === '1H') {
            btn.style.display = (market === 'stock' || market === 'futures') ? 'none' : '';
        }
    });

    // 重新載入圖表資料，且如果是切換幣種，就強制縮放
    renderSymbolSwitcher(market);
    await loadChartData(currentTimeframe, isSymbolChanged);

    // 台股 / 期貨：盤後信號計算
    if (market === 'stock' || market === 'futures') {
        fetchTwSignals(sym, market);
    }

    // 台股三面分析
    if (market === 'stock') {
        fetchThreeLayerAnalysis(sym);
    } else {
        hideThreeLayerAnalysis();
    }

    // 只在 Crypto 模式下顯示即時訊號卡片
    if (market === 'crypto' && lastServerData.length > 0) {
        processData(lastServerData);
    }
};

function clearSignalCard(market = 'stock', loading = false) {
    const modeLabel = market === 'futures' ? '台股期貨' : '台股';
    if (ui.status) ui.status.textContent = loading
        ? `${modeLabel}盤後信號計算中...`
        : `${modeLabel}模式：無即時信號`;
    if (ui.btc && ui.btc.price) ui.btc.price.textContent = loading ? '...' : '--';

    const clearRing = (ringEl, scoreEl, dirEl, scoreTextEl) => {
        if (ringEl) ringEl.style.strokeDasharray = '0, 100';
        if (scoreEl) scoreEl.textContent = loading ? '...' : '--';
        if (dirEl) dirEl.textContent = loading ? '計算中' : 'N/A';
        if (scoreTextEl) scoreTextEl.textContent = loading ? '...' : 'N/A';
    };

    clearRing(ui.btc.d1.ring, ui.btc.d1.score, ui.btc.d1.dir, ui.btc.d1.lvl);
    clearRing(ui.btc.h4.ring, ui.btc.h4.score, ui.btc.h4.dir, null);

    // 台股/期貨：隱藏 4H 區塊，放大 1D
    const isTW = (market === 'stock' || market === 'futures');
    const tfSlots = document.querySelectorAll('.tf-slot');
    if (tfSlots.length >= 2) {
        tfSlots[1].style.display = isTW ? 'none' : '';   // 隱藏 4H
        tfSlots[0].style.flex = isTW ? '1' : '';          // 1D 佔滿
    }
    // 全屏 header 4H badge
    const h4Badge = document.getElementById('h-score-4h');
    if (h4Badge) h4Badge.closest('.h-badge').style.display = isTW ? 'none' : '';

    if (ui.btc.res_alert) {
        ui.btc.res_alert.className = 'resonance-alert';
        ui.btc.res_alert.innerHTML = loading
            ? `<em>${modeLabel}盤後信號計算中，請稍候...</em>`
            : `<em>${modeLabel}模式下暫不提供即時策略訊號</em>`;
    }
}

function renderTwMiniCards(market) {
    const container = document.getElementById('tw-mini-cards');
    if (!container) return;
    const list = twWatchlist[market] || [];
    container.innerHTML = list.map(item => `
        <div class="mini-card glass-panel" style="cursor:pointer"
             onclick="window.changeSymbol('${item.symbol}','${market}')">
            <div class="mc-head">
                <h3>${item.label} ${item.name}</h3>
                <span class="glow-text tw-mc-price" data-sym="${item.symbol}">...</span>
            </div>
            <div class="mc-body">1D: <span class="tw-mc-dir" data-sym="${item.symbol}">--</span>
                (<span class="tw-mc-score" data-sym="${item.symbol}">0</span>分)</div>
        </div>
    `).join('');
    container.style.display = list.length ? 'flex' : 'none';

    // 異步抓取各標的盤後信號
    list.forEach(async (item) => {
        try {
            const res = await fetch(`/api/tw-signals?symbol=${encodeURIComponent(item.symbol)}&market=${market}`);
            const data = await res.json();
            const s = data.signals && data.signals['1d'];
            if (!s) return;

            const priceEl = container.querySelector(`.tw-mc-price[data-sym="${item.symbol}"]`);
            const dirEl = container.querySelector(`.tw-mc-dir[data-sym="${item.symbol}"]`);
            const scoreEl = container.querySelector(`.tw-mc-score[data-sym="${item.symbol}"]`);

            if (priceEl) priceEl.textContent = formatPrice(s.price);
            if (scoreEl) scoreEl.textContent = s.confidence;
            if (dirEl) {
                const lbl = getSignalLabel(s.direction, s.confidence);
                dirEl.textContent = lbl.brief;
            }
        } catch (e) { /* 靜默 */ }
    });
}

// 更新頂部 ticker 的指定標的（通用，支援台股/期貨/crypto）
function updateTickerForSymbol(symbol, price, confidence, change) {
    const tickerMap = {
        'BTC/USDT': 'btc', 'ETH/USDT': 'eth', 'SOL/USDT': 'sol',
        '2330.TW': '2330', '0050.TW': '0050', '2317.TW': '2317',
        '2881.TW': '2881', '2603.TW': '2603'
    };
    const tickerId = tickerMap[symbol];
    if (!tickerId) return;

    const valEls = document.querySelectorAll(`[id^="ticker-${tickerId}"]`);
    const chgEls = document.querySelectorAll(`[id^="ticker-${tickerId}-chg"]`);
    const scoreEls = document.querySelectorAll(`[id^="ticker-${tickerId}-score"]`);

    valEls.forEach(el => {
        if (el && !el.id.includes('chg') && !el.id.includes('score')) {
            el.textContent = formatPrice(price);
        }
    });
    const chg = parseFloat(change) || 0;
    chgEls.forEach(el => {
        if (el) {
            const prefix = chg > 0 ? '▲' : chg < 0 ? '▼' : '';
            el.textContent = `${prefix}${Math.abs(chg).toFixed(2)}%`;
            el.className = 't-chg ' + (chg > 0 ? 'up' : chg < 0 ? 'down' : '');
        }
    });
    scoreEls.forEach(el => {
        if (el) el.textContent = `Score: ${confidence}`;
    });
}

// 頁面載入時一次拉取所有 ticker 資料（含台股），不依賴手動切換
async function initTickerData() {
    try {
        const res = await fetch('/api/ticker-summary');
        if (!res.ok) return;
        const data = await res.json();

        // Crypto ticker（WebSocket 可能還沒送達，先填）
        if (data.crypto) {
            for (const [sym, info] of Object.entries(data.crypto)) {
                updateTickerForSymbol(sym, info.price, info.confidence, info.change_24h || 0);
            }
        }
        // TW ticker
        if (data.tw) {
            for (const [sym, info] of Object.entries(data.tw)) {
                updateTickerForSymbol(sym, info.price, info.confidence, info.change_24h || 0);
            }
        }
        // 更新時間戳
        updateTimestamps(data.crypto_updated_at, data.tw_updated_at);
    } catch (e) {
        console.warn('[initTickerData] Failed:', e);
    }
}

// 更新右上角的分開時間戳
function updateTimestamps(cryptoTime, twTime) {
    const elCrypto = document.getElementById('ts-crypto');
    const elTw = document.getElementById('ts-tw');
    if (elCrypto && cryptoTime) elCrypto.textContent = `Crypto: ${cryptoTime}`;
    if (elTw && twTime) elTw.textContent = `TW: ${twTime}`;
}

async function fetchTwSignals(symbol, market) {
    try {
        const res = await fetch(`/api/tw-signals?symbol=${encodeURIComponent(symbol)}&market=${market}`);
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        const s = data.signals && data.signals['1d'];
        if (!s) {
            clearSignalCard(market, false);
            return;
        }

        // 更新頂部 ticker（台股/期貨）
        updateTickerForSymbol(symbol, s.price, s.confidence, s.change_24h || 0);

        // 更新 TW 時間戳
        const twNow = new Date().toTimeString().slice(0, 8);
        updateTimestamps(null, twNow);

        // 盤後不需自動更新
        if (data.market_open === false) {
            clearAutoRefresh();
        }

        const modeLabel = market === 'futures' ? '台股期貨' : '台股';

        // 價格
        if (ui.btc && ui.btc.price) ui.btc.price.textContent = formatPrice(s.price);
        if (ui.status) ui.status.textContent = `${modeLabel}盤後信號（日線）`;

        // 1D 環形圖
        if (ui.btc && ui.btc.d1) {
            updateRing(ui.btc.d1.ring, ui.btc.d1.score, s.confidence, s.direction);
            updateDirection(ui.btc.d1.dir, s.direction, s.level, s.confidence);
            if (ui.btc.d1.buy) ui.btc.d1.buy.textContent = s.buy_score;
            if (ui.btc.d1.sell) ui.btc.d1.sell.textContent = s.sell_score;
        }

        // 4H 區域改為盤後提示（日線資料不支援真正 4H）
        if (ui.btc.h4.ring) ui.btc.h4.ring.style.strokeDasharray = '0, 100';
        if (ui.btc.h4.score) ui.btc.h4.score.textContent = '--';
        if (ui.btc.h4.dir) ui.btc.h4.dir.textContent = '僅日線';
        if (ui.btc.h4.buy) ui.btc.h4.buy.textContent = '--';
        const sell4h = document.getElementById('sell-btc-4h');
        if (sell4h) sell4h.textContent = '--';

        // 建議語
        if (ui.btc.res_alert) {
            const c = s.confidence;
            const d = s.direction;
            if (d === 'BUY' && c >= 70) {
                ui.btc.res_alert.className = 'resonance-alert buy';
                ui.btc.res_alert.innerHTML = `<strong>盤後多方強勢</strong>：日線動能 ${c} 分，多指標共振看多，適合逢低布局。`;
            } else if (d === 'BUY' && c >= 50) {
                ui.btc.res_alert.className = 'resonance-alert buy';
                ui.btc.res_alert.innerHTML = `<strong>盤後偏多</strong>：日線動能 ${c} 分，方向偏多但動能中等，可觀察等待加碼點。`;
            } else if (d === 'SELL' && c >= 70) {
                ui.btc.res_alert.className = 'resonance-alert sell';
                ui.btc.res_alert.innerHTML = `<strong>盤後空方強勢</strong>：日線動能 ${c} 分，多指標看空，建議減碼或空倉觀望。`;
            } else if (d === 'SELL' && c >= 50) {
                ui.btc.res_alert.className = 'resonance-alert sell';
                ui.btc.res_alert.innerHTML = `<strong>盤後偏空</strong>：日線動能 ${c} 分，方向偏空但動能不強，暫緩進場。`;
            } else {
                ui.btc.res_alert.className = 'resonance-alert';
                ui.btc.res_alert.innerHTML = `<strong>盤後中性</strong>：日線動能 ${c} 分，多空不明朗，建議空倉等待明確信號。`;
            }
        }

        // 全屏 header
        const hPrice = document.getElementById('h-price-btc');
        const hS1d = document.getElementById('h-score-1d');
        const hS4h = document.getElementById('h-score-4h');
        if (hPrice) hPrice.textContent = formatPrice(s.price);
        if (hS1d) { hS1d.textContent = s.confidence; hS1d.style.color = s.direction === 'BUY' ? '#10b981' : s.direction === 'SELL' ? '#ef4444' : '#94a3b8'; }
        if (hS4h) { hS4h.textContent = '--'; hS4h.style.color = '#94a3b8'; }
        syncFullscreenHeader(symbol, s.direction, s.confidence, ui.btc.res_alert ? ui.btc.res_alert.innerHTML : '');
    } catch (e) {
        console.error('TW signal fetch error:', e);
        clearSignalCard(market, false);
    }
}

// ══════════════════════════════════════════
// 三面分析 (Three-Layer Analysis)
// ══════════════════════════════════════════

const REGIME_LABELS = {
    '強勢多頭': { cls: 'bullish', text: '強勢多頭' },
    '多頭':     { cls: 'bullish', text: '多頭' },
    '盤整':     { cls: 'neutral', text: '盤整' },
    '空頭':     { cls: 'bearish', text: '空頭' },
    '高檔轉折': { cls: 'bearish', text: '高檔轉折' },
    '底部轉強': { cls: 'bullish', text: '底部轉強' },
};

const VALUATION_LABELS = {
    '明顯低估': { cls: 'underval' },
    '偏低估':   { cls: 'underval' },
    '合理':     { cls: 'fair' },
    '偏高估':   { cls: 'overval' },
    '明顯高估': { cls: 'overval' },
};

async function fetchThreeLayerAnalysis(symbol) {
    const container = document.getElementById('three-layer-analysis');
    if (!container) return;

    // 顯示載入中
    container.style.display = 'block';
    document.getElementById('tla-technical').innerHTML = '<span style="color:var(--text-muted)">載入中...</span>';
    document.getElementById('tla-fundamental').innerHTML = '<span style="color:var(--text-muted)">載入中...</span>';
    document.getElementById('tla-sentiment').innerHTML = '<span style="color:var(--text-muted)">載入中...</span>';

    try {
        const res = await fetch(`/api/stock-analysis?symbol=${encodeURIComponent(symbol)}`);
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        renderThreeLayerAnalysis(data);
    } catch (e) {
        console.error('Three-layer analysis error:', e);
        document.getElementById('tla-technical').innerHTML = '無法載入';
        document.getElementById('tla-fundamental').innerHTML = '無法載入';
        document.getElementById('tla-sentiment').innerHTML = '無法載入';
    }
}

function _scoreBadgeCls(score) {
    if (score >= 70) return 'score-high';
    if (score >= 45) return 'score-mid';
    return 'score-low';
}

function renderThreeLayerAnalysis(data) {
    // ── 綜合推薦（下方三面區域內） ──
    const recEl = document.getElementById('tla-recommendation');
    // ── 綜合推薦（左欄 inline） ──
    const recInline = document.getElementById('tla-recommendation-inline');

    if (data.recommendation && data.recommendation.composite_score != null) {
        const rec = data.recommendation;
        const score = rec.composite_score;
        const actionCls = {
            strong_buy: 'rec-strong-buy', buy: 'rec-buy',
            neutral: 'rec-neutral', weak: 'rec-weak', avoid: 'rec-avoid',
        }[rec.action_class] || 'rec-neutral';

        const w = rec.weights || {};
        const LABEL = {chipflow:'籌碼',technical:'技術',fundamental:'基本面',regime:'盤勢',sentiment:'消息'};
        const weightStr = Object.entries(LABEL)
            .filter(([k]) => w[k])
            .map(([k, label]) => `${label}${w[k]}%`)
            .join('＋');
        const recHtml = `
            <div class="rec-score-ring ${actionCls}">
                <span class="rec-score-num">${Math.round(score)}</span>
            </div>
            <div class="rec-info">
                <div class="rec-action ${actionCls}">${rec.action}</div>
                <div class="rec-detail">${weightStr || '籌碼35%＋技術25%＋基本面20%＋盤勢13%＋消息7%'}</div>
            </div>`;

        // 渲染到左欄 inline 區塊
        if (recInline) {
            recInline.style.display = 'flex';
            recInline.innerHTML = recHtml;
        }
        // 隱藏下方的重複區塊
        if (recEl) recEl.style.display = 'none';
    } else {
        if (recEl) recEl.style.display = 'none';
        if (recInline) recInline.style.display = 'none';
    }

    // ── 技術面 ──
    const techEl = document.getElementById('tla-technical');
    const techScoreBadge = document.getElementById('tla-tech-score');
    if (data.technical) {
        const t = data.technical;
        const dirCls = t.direction === 'BUY' ? 'bullish' : t.direction === 'SELL' ? 'bearish' : 'neutral';
        const dirText = t.direction === 'BUY' ? '偏多' : t.direction === 'SELL' ? '偏空' : '中性';

        // 分數 badge
        techScoreBadge.textContent = t.buy_score + '分';
        techScoreBadge.className = 'tla-score-badge ' + _scoreBadgeCls(t.buy_score);

        let regimeHtml = '';
        if (data.regime) {
            const r = data.regime;
            const rStyle = REGIME_LABELS[r.state] || { cls: 'neutral', text: r.state };
            regimeHtml = `
                <div class="tla-row">
                    <span class="tla-row-label">盤勢 <span class="info-tooltip" data-tip="盤勢辨識層（RegimeLayer）綜合 6 項子信號判斷：(1)趨勢確認（Swing High/Low 頭頭高底底高）、(2)均線排列（5/10/20/60MA 多空排列）、(3)位階偵測（120日高低點相對位置）、(4)K線型態（長紅/長黑/吞噬/十字星）、(5)量價分析（底部爆量/高檔爆量不漲）、(6)ADX趨勢強度。產出 6 種狀態：強勢多頭/多頭/盤整/空頭/高檔轉折/底部轉強。使用最近 120 根日 K 計算。"><i>i</i></span></span>
                    <span class="tla-badge ${rStyle.cls}">${rStyle.text}</span>
                </div>`;
            if (r.position && r.position.percentile != null) {
                regimeHtml += `
                <div class="tla-row">
                    <span class="tla-row-label">位階 <span class="info-tooltip" data-tip="目前股價在近 120 根日 K 的相對位置百分比。計算：(現價 - 120日最低) / (120日最高 - 120日最低) × 100%。>85%=高檔區、60-85%=中高檔、40-60%=中間、15-40%=中低檔、<15%=低檔區。高檔區出現利空 K 線型態→可能觸發高檔轉折判定。"><i>i</i></span></span>
                    <span class="tla-row-value">${r.position.percentile}%</span>
                </div>`;
            }
            if (r.ma_alignment && r.ma_alignment.score != null) {
                regimeHtml += `
                <div class="tla-row">
                    <span class="tla-row-label">均線排列 <span class="info-tooltip" data-tip="六六大順評分（0-6 分），衡量均線多頭排列程度。6 項子條件各 1 分：(1)多頭排列（收盤>5MA>10MA>20MA>60MA）、(2)股價在 5MA 之上、(3)股價在 20MA 之上、(4)20MA 方向向上（vs 5日前）、(5)60MA 方向向上、(6)股價在 60MA 之上。6/6=完美多頭排列，0/6=完全空頭。使用最近 60 根日 K 計算均線。"><i>i</i></span></span>
                    <span class="tla-row-value">${r.ma_alignment.score}/6</span>
                </div>`;
            }
            if (r.advice) {
                regimeHtml += `
                <div class="tla-advice">${r.advice}</div>`;
            }
        }

        techEl.innerHTML = `
            <div class="tla-row">
                <span class="tla-row-label">方向 <span class="info-tooltip" data-tip="買入分 > 賣出分 → 偏多（BUY）；賣出分 > 買入分 → 偏空（SELL）。旁邊的分數 = 較高方的分數（即信心度 confidence），代表該方向的確信程度。"><i>i</i></span></span>
                <span class="tla-badge ${dirCls}">${dirText} ${t.confidence}分</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">做多分數 <span class="info-tooltip" data-tip="7 項指標中判定為買入信號的加權合計。例如 RSI<30（超賣，看多）貢獻其權重分；MACD 金叉貢獻其權重分。所有看多指標的分數加總即為做多分數，滿分 100。做多分數越高，代表越多指標同時看多。"><i>i</i></span></span>
                <span class="tla-row-value">${t.buy_score}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">信號強度 <span class="info-tooltip" data-tip="根據信心度（方向分數）分級：≥90=極強信號、70-89=強信號、50-69=中等信號、30-49=弱信號、<30=無信號。強信號代表多數指標方向一致，信號可靠度高。"><i>i</i></span></span>
                <span class="tla-row-value">${t.signal_level}</span>
            </div>
            ${regimeHtml}
            ${t.advice ? `<div class="tla-advice">${t.advice}</div>` : ''}`;
    } else {
        techEl.innerHTML = '<span style="color:var(--text-muted)">數據不足</span>';
        techScoreBadge.textContent = '';
    }

    // ── 基本面 ──
    const fundEl = document.getElementById('tla-fundamental');
    const fundScoreBadge = document.getElementById('tla-fund-score');
    if (data.fundamental) {
        const f = data.fundamental;
        const vStyle = VALUATION_LABELS[f.valuation] || { cls: 'fair' };
        const peText = f.pe != null ? f.pe.toFixed(2) : '--';
        const dyText = f.dy != null ? f.dy.toFixed(2) + '%' : '--';
        const pbText = f.pb != null ? f.pb.toFixed(2) : '--';

        const momText = f.mom != null ? (f.mom > 0 ? '+' : '') + f.mom.toFixed(2) + '%' : '--';
        const yoyText = f.yoy != null ? (f.yoy > 0 ? '+' : '') + f.yoy.toFixed(2) + '%' : '--';
        const momCls = f.mom != null ? (f.mom > 0 ? 'bullish' : f.mom < 0 ? 'bearish' : 'neutral') : 'neutral';
        const yoyCls = f.yoy != null ? (f.yoy > 0 ? 'bullish' : f.yoy < 0 ? 'bearish' : 'neutral') : 'neutral';

        // 分數 badge
        if (f.buy_score != null) {
            fundScoreBadge.textContent = f.buy_score + '分';
            fundScoreBadge.className = 'tla-score-badge ' + _scoreBadgeCls(f.buy_score);
        } else {
            fundScoreBadge.textContent = '';
        }

        fundEl.innerHTML = `
            <div class="tla-row">
                <span class="tla-row-label">估值評等</span>
                <span class="tla-badge ${vStyle.cls}">${f.valuation}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">本益比 (P/E)</span>
                <span class="tla-row-value">${peText}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">殖利率</span>
                <span class="tla-row-value">${dyText}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">股價淨值比</span>
                <span class="tla-row-value">${pbText}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">月營收 (MoM)</span>
                <span class="tla-badge ${momCls}">${momText}</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">年營收 (YoY)</span>
                <span class="tla-badge ${yoyCls}">${yoyText}</span>
            </div>
            ${f.advice ? `<div class="tla-advice">${f.advice}</div>` : ''}`;
    } else {
        fundEl.innerHTML = '<span style="color:var(--text-muted)">無基本面資料</span>';
        fundScoreBadge.textContent = '';
    }

    // ── 籌碼面 ──
    const chipEl = document.getElementById('tla-chipflow');
    const chipScoreBadge = document.getElementById('tla-chip-score');
    if (chipEl) {
        if (data.chipflow && data.chipflow.status === 'active') {
            const c = data.chipflow;

            if (c.buy_score != null) {
                chipScoreBadge.textContent = c.buy_score + '分';
                chipScoreBadge.className = 'tla-score-badge ' + _scoreBadgeCls(c.buy_score);
            } else {
                chipScoreBadge.textContent = '';
            }

            const chipLabelCls = c.buy_score >= 65 ? 'bullish' : c.buy_score <= 35 ? 'bearish' : 'neutral';

            // 外資連買/賣文字
            const fc = c.foreign_consec_buy || 0;
            const foreignCls = fc > 0 ? 'bullish' : fc < 0 ? 'bearish' : 'neutral';
            // 投信連買/賣文字
            const tc = c.trust_consec_buy || 0;
            const trustCls = tc > 0 ? 'bullish' : tc < 0 ? 'bearish' : 'neutral';
            // 融資增減
            const mc = c.margin_change_sum || 0;
            const marginCls = mc < 0 ? 'bullish' : mc > 0 ? 'bearish' : 'neutral';
            const marginText = mc < 0 ? `減少${Math.abs(mc).toLocaleString()}張` : mc > 0 ? `增加${mc.toLocaleString()}張` : '持平';

            // 每日明細（最近3個有效交易日，略過全零的非交易日）
            let dailyHtml = '';
            const validDays = (c.daily_data || []).filter(d =>
                (d.foreign_net || 0) !== 0 || (d.trust_net || 0) !== 0 || (d.dealer_net || 0) !== 0
            );
            if (validDays.length > 0) {
                dailyHtml = '<div class="tla-chip-daily">';
                for (const d of validDays.slice(0, 3)) {
                    const fNet = d.foreign_net || 0;
                    const tNet = d.trust_net || 0;
                    const fCls = fNet > 0 ? 'news-pos' : fNet < 0 ? 'news-neg' : 'news-neu';
                    const tCls = tNet > 0 ? 'news-pos' : tNet < 0 ? 'news-neg' : 'news-neu';
                    
                    const formatNet = (val) => {
                        const absVal = Math.abs(val);
                        if (absVal === 0) return '0張';
                        if (absVal < 1000) return `${val > 0 ? '+' : ''}${val}張`;
                        return `${val > 0 ? '+' : ''}${(val/1000).toFixed(1).replace('.0', '')}千張`;
                    };

                    dailyHtml += `<div class="tla-chip-day">
                        <span class="tla-chip-date">${d.date.slice(4,6)}/${d.date.slice(6)}</span>
                        <span class="${fCls}">外${formatNet(fNet)}</span>
                        <span class="${tCls}">投${formatNet(tNet)}</span>
                    </div>`;
                }
                dailyHtml += '</div>';
            }

            chipEl.innerHTML = `
                <div class="tla-row">
                    <span class="tla-row-label">籌碼判定</span>
                    <span class="tla-badge ${chipLabelCls}">${c.label}</span>
                </div>
                <div class="tla-row">
                    <span class="tla-row-label">外資</span>
                    <span class="tla-badge ${foreignCls}">${c.foreign_text}</span>
                </div>
                <div class="tla-row">
                    <span class="tla-row-label">投信</span>
                    <span class="tla-badge ${trustCls}">${c.trust_text}</span>
                </div>
                <div class="tla-row">
                    <span class="tla-row-label">融資變化</span>
                    <span class="tla-badge ${marginCls}">${marginText}</span>
                </div>
                <div class="tla-row">
                    <span class="tla-row-label">融券餘額</span>
                    <span class="tla-row-value">${(c.short_balance_latest || 0).toLocaleString()}張</span>
                </div>
                ${dailyHtml}
                ${c.advice ? `<div class="tla-advice">${c.advice}</div>` : ''}`;
        } else {
            chipEl.innerHTML = '<span style="color:var(--text-muted)">無籌碼資料</span>';
            if (chipScoreBadge) chipScoreBadge.textContent = '';
        }
    }

    // ── 消息面 ──
    const sentEl = document.getElementById('tla-sentiment');
    const sentScoreBadge = document.getElementById('tla-sent-score');
    sentScoreBadge.textContent = '';

    if (data.sentiment && data.sentiment.status === 'active') {
        const s = data.sentiment;

        // 分數 badge
        if (s.buy_score != null) {
            sentScoreBadge.textContent = s.buy_score + '分';
            sentScoreBadge.className = 'tla-score-badge ' + _scoreBadgeCls(s.buy_score);
        }

        const labelCls = s.score >= 5 ? 'bullish' : s.score <= -5 ? 'bearish' : 'neutral';

        let newsHtml = '';
        if (s.recent_news && s.recent_news.length > 0) {
            newsHtml = '<div class="tla-news-list">';
            for (const n of s.recent_news.slice(0, 3)) {
                const sCls = n.sentiment === '正面' ? 'news-pos' : n.sentiment === '負面' ? 'news-neg' : 'news-neu';
                newsHtml += `<div class="tla-news-item ${sCls}">
                    <a href="${n.link}" target="_blank" rel="noopener" class="tla-news-link">${n.title}</a>
                    <span class="tla-news-src">${n.source}</span>
                </div>`;
            }
            newsHtml += '</div>';
        } else {
            newsHtml = '<div style="font-size:11px; color:var(--text-muted); margin-top:4px;">無個股相關新聞</div>';
        }

        let marketHtml = '';
        if (s.market) {
            marketHtml = `
                <div class="tla-row">
                    <span class="tla-row-label">市場氣氛</span>
                    <span class="tla-row-value">${s.market.label}</span>
                </div>`;
        }

        sentEl.innerHTML = `
            <div class="tla-row">
                <span class="tla-row-label">情緒</span>
                <span class="tla-badge ${labelCls}">${s.label}（${s.score > 0 ? '+' : ''}${s.score}分）</span>
            </div>
            <div class="tla-row">
                <span class="tla-row-label">相關新聞</span>
                <span class="tla-row-value">${s.total_related}則（正${s.positive_count} / 負${s.negative_count}）</span>
            </div>
            ${marketHtml}
            ${newsHtml}
            ${s.advice ? `<div class="tla-advice">${s.advice}</div>` : ''}`;
    } else if (data.sentiment && data.sentiment.status === 'coming_soon') {
        sentEl.innerHTML = '<span class="tla-badge coming">Phase 3 即將推出</span><div style="margin-top:6px; font-size:11px; color:var(--text-muted)">RSS 監控 + 關鍵字情緒分析</div>';
    } else {
        sentEl.innerHTML = '<span style="color:var(--text-muted)">--</span>';
    }
}

function hideThreeLayerAnalysis() {
    const container = document.getElementById('three-layer-analysis');
    if (container) container.style.display = 'none';
    const recInline = document.getElementById('tla-recommendation-inline');
    if (recInline) recInline.style.display = 'none';
}

// ── 超級選股系統 ──

async function fetchScreenerPicks() {
    const section = document.getElementById('screener-section');
    const container = document.getElementById('screener-categories');
    const updatedEl = document.getElementById('screener-updated');
    if (!section || !container) return;

    try {
        const res = await fetch('/api/screener/picks');
        const data = await res.json();

        if (data.scanning && (!data.categories || data.categories.length === 0)) {
            section.style.display = 'block';
            container.innerHTML = '<div class="screener-loading">選股系統掃描中，請稍後重新整理...</div>';
            if (updatedEl) updatedEl.textContent = '';
            return;
        }

        if (!data.categories || data.categories.length === 0) {
            section.style.display = 'block';
            container.innerHTML = `
                <div class="screener-loading" style="display:flex; flex-direction:column; align-items:center; gap:20px;">
                    <div>${data.message || '尚未掃描，請點擊下方按鈕開始掃描'}</div>
                    <button class="btn-primary" onclick="document.getElementById('screener-refresh-btn').click()" style="padding: 12px 24px; font-size: 16px; border-radius: 8px; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                        <span style="font-size:20px;">↻</span> 立即掃描市場
                    </button>
                </div>`;
            if (updatedEl) updatedEl.textContent = '';
            return;
        }

        section.style.display = 'block';
        if (updatedEl && data.updated_at) {
            updatedEl.textContent = `更新: ${data.updated_at}`;
        }

        renderScreenerCards(data.categories);
    } catch (e) {
        console.warn('選股系統載入失敗:', e);
    }
}

function renderScreenerCards(categories) {
    const container = document.getElementById('screener-categories');
    if (!container) return;

    const DEFAULT_SHOW = 5;

    container.innerHTML = categories.map(cat => {
        const stocks = cat.stocks || [];
        const isRanking = cat.id === 'top_ranked';
        const hasMore = stocks.length > DEFAULT_SHOW;

        const stocksHtml = stocks.length > 0
            ? stocks.map((s, idx) => {
                const scoreCls = s.composite_score >= 70 ? 'score-high' : s.composite_score >= 45 ? 'score-mid' : 'score-low';
                const hiddenCls = (isRanking && idx >= DEFAULT_SHOW) ? ' screener-hidden' : '';
                const rankBadge = isRanking ? `<span class="screener-rank">${idx + 1}</span>` : '';
                return `<div class="screener-stock-row${hiddenCls}" data-symbol="${s.symbol}" data-market="stock">
                    ${rankBadge}
                    <span class="screener-stock-sym">${s.symbol.replace('.TW','')}</span>
                    <span class="screener-stock-name">${s.name}</span>
                    <span class="screener-stock-score ${scoreCls}">${Math.round(s.composite_score)}</span>
                    <span class="screener-stock-hl">${s.highlight}</span>
                </div>`;
            }).join('')
            : '<div class="screener-empty">暫無符合條件的標的</div>';

        const expandBtn = (isRanking && hasMore)
            ? `<button class="screener-expand-btn" data-cat="${cat.id}">顯示更多 (${stocks.length}檔) ▼</button>`
            : '';

        return `<div class="screener-cat-card glass-panel ${isRanking ? 'screener-ranking-card' : ''}">
            <div class="screener-cat-header">
                <span class="screener-cat-icon">${cat.icon}</span>
                <span class="screener-cat-name">${cat.name}</span>
                ${cat.description ? `<span class="info-tooltip" data-tip="${cat.description}" style="margin-left: 4px;"><i>i</i></span>` : ''}
                <span class="screener-cat-count">${stocks.length}檔</span>
            </div>
            <div class="screener-cat-stocks">${stocksHtml}</div>
            ${expandBtn}
        </div>`;
    }).join('');

    // 點擊個股 → 切換到該標的
    container.querySelectorAll('.screener-stock-row').forEach(row => {
        row.addEventListener('click', () => {
            const symbol = row.dataset.symbol;
            const market = row.dataset.market || 'stock';
            if (window.changeSymbol) {
                window.changeSymbol(symbol, market);
            }
        });
    });

    // 顯示更多 / 收起
    container.querySelectorAll('.screener-expand-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const card = btn.closest('.screener-cat-card');
            const hidden = card.querySelectorAll('.screener-hidden');
            const isExpanded = btn.dataset.expanded === '1';
            if (isExpanded) {
                card.querySelectorAll('.screener-stock-row').forEach((row, i) => {
                    if (i >= DEFAULT_SHOW) row.classList.add('screener-hidden');
                });
                btn.dataset.expanded = '0';
                btn.textContent = `顯示更多 (${card.querySelectorAll('.screener-stock-row').length}檔) ▼`;
            } else {
                hidden.forEach(el => el.classList.remove('screener-hidden'));
                btn.dataset.expanded = '1';
                btn.textContent = '收起 ▲';
            }
        });
    });
}

// 初始化選股系統
function initScreener() {
    // 載入選股資料
    fetchScreenerPicks();

    // 重新整理按鈕
    const refreshBtn = document.getElementById('screener-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            refreshBtn.disabled = true;
            refreshBtn.textContent = '⏳';
            try {
                await fetch('/api/screener/refresh', { method: 'POST' });
                // 等 3 秒後重新載入
                setTimeout(() => {
                    fetchScreenerPicks();
                    refreshBtn.disabled = false;
                    refreshBtn.textContent = '↻';
                }, 3000);
            } catch (e) {
                refreshBtn.disabled = false;
                refreshBtn.textContent = '↻';
            }
        });
    }

    // 每 30 分鐘自動刷新
    setInterval(fetchScreenerPicks, 30 * 60 * 1000);
}

function initExpandLogic() {
    const btn = document.getElementById('btn-chart-expand');
    const grid = document.querySelector('.dashboard-grid');
    const icon = document.getElementById('expand-icon');

    if (!btn || !grid) return;

    btn.onclick = () => {
        grid.classList.toggle('fullscreen-mode');
        const isFull = grid.classList.contains('fullscreen-mode');
        icon.textContent = isFull ? '❐' : '⛶';

        // 觸發多次 Resizing 以確保絕對恢復
        const doResize = () => {
            if (chart) {
                const container = document.getElementById('tv-chart');
                chart.resize(container.clientWidth, container.clientHeight);
                chart.timeScale().fitContent();
            }
        };

        // 立即執行一次，過渡中執行一次，結束再執行一次
        doResize();
        setTimeout(doResize, 150);
        setTimeout(doResize, 400);
    };

    // 監聽視窗大小變化
    window.addEventListener('resize', () => {
        if (chart) {
            const container = document.getElementById('tv-chart');
            chart.resize(container.clientWidth, container.clientHeight);
        }
    });
}

// 初始化指標面板監聽
function initIndicatorControls() {
    const ids = [
        'enable-vol', 'enable-ema-short', 'val-ema-short',
        'enable-ema-long', 'val-ema-long',
        'enable-bbands', 'val-bb-p', 'val-bb-s',
        'enable-rsi', 'val-rsi'
    ];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('change', () => refreshIndicators());
            if (el.tagName === 'INPUT' && el.type === 'number') {
                el.addEventListener('input', () => refreshIndicators());
            }
        }
    });
}

// 載入歷史 K 線資料
async function loadChartData(tf, forceFit = false) {
    if (!candleSeries) return;
    try {
        const res = await fetch(`/api/chart?symbol=${currentSymbol}&timeframe=${tf}&market=${currentMarket}`);
        if (!res.ok) throw new Error("API Status: " + res.status);
        const resp = await res.json();

        // 統一新格式解析：{candles, data_source, next_update_in}
        const isTwMarket = (currentMarket === 'stock' || currentMarket === 'futures');
        const data = resp.candles || (Array.isArray(resp) ? resp : []);
        const dataSource = resp.data_source || (currentMarket === 'crypto' ? 'ccxt' : null);
        const nextUpdateIn = resp.next_update_in ?? (currentMarket === 'crypto' ? null : 60);

        // 更新資料來源 badge
        updateDataSourceBadge(dataSource, nextUpdateIn);

        // 啟動自動更新機制（台股/期貨才需要）
        if (isTwMarket && nextUpdateIn !== null) {
            scheduleAutoRefresh(nextUpdateIn);
        } else {
            clearAutoRefresh();
        }

        // 若後端明確表示限流中且無資料，不更新圖表（保留舊畫面），僅等待倒數
        if (isTwMarket && dataSource === 'rate_limited') {
            // 圖表保持原來的資料（如果有），不清空也不報錯
            if (ui.status) ui.status.textContent = `⏳ 60 秒限流中，${nextUpdateIn}s 後自動更新...`;
            return;
        }

        if (data && data.length > 0) {
            data.sort((a, b) => a.time - b.time);
            const cleanData = [];
            let lastT = 0;
            for (const item of data) {
                if (item.time > lastT) {
                    cleanData.push({
                        time: item.time,
                        open: parseFloat(item.open),
                        high: parseFloat(item.high),
                        low: parseFloat(item.low),
                        close: parseFloat(item.close),
                        rawVolume: parseFloat(item.volume || 0)
                    });
                    lastT = item.time;
                }
            }

            candleSeries.setData(cleanData);
            cachedCandles = cleanData;

            // 同步左側儀表板價格（以圖表最新收盤價為準，避免 signal 和 chart 資料源不同步）
            if (cleanData.length > 0) {
                const lastCandle = cleanData[cleanData.length - 1];
                const chartPrice = lastCandle.close;
                if (ui.btc && ui.btc.price) {
                    const prefix = (currentMarket === 'crypto') ? '$' : '';
                    ui.btc.price.textContent = `${prefix}${formatPrice(chartPrice)}`;
                }
            }

            refreshIndicators();

            if (candleSeries) {
                const markers = generateHistoricalMarkers(cleanData);
                candleSeries.setMarkers(markers);
            }

            if (isInitialChartLoad || forceFit) {
                setTimeout(() => {
                    if (chart && chart.timeScale) {
                        const totalVisibleBars = 80;
                        const rightOffset = 20;
                        const lastIndex = cleanData.length - 1;
                        chart.timeScale().applyOptions({ rightOffset });
                        chart.timeScale().setVisibleLogicalRange({
                            from: lastIndex - (totalVisibleBars - rightOffset - 1),
                            to: lastIndex + rightOffset,
                        });
                    }
                    isInitialChartLoad = false;
                }, 50);
            }
        } else {
            // 找不到資料時，一律清空圖表，避免顯示舊資料造成誤會
            clearChart();
            if (isTwMarket) {
                // 台股/期貨找不到資料時，不跳 alert，改成友善的狀態訊息
                const reason = dataSource === null ? '找不到資料，請確認代碼是否正確' : '資料暫時無法取得';
                updateDataSourceBadge(null, null);
                if (ui.status) ui.status.textContent = `⚠️ ${currentSymbol.split('.')[0]} — ${reason}`;
                console.warn(`TW chart: no data for ${currentSymbol} (${tf}), source=${dataSource}`);
            } else {
                alert(`找不到商品: ${currentSymbol}，請重新輸入正確代碼。`);
                searchHistory = searchHistory.filter(item => item.sym !== currentSymbol);
                localStorage.setItem('searchHistory', JSON.stringify(searchHistory));
                renderHistory();
            }
        }
    } catch (e) {
        console.error("Chart load failed", e);
        clearChart();
        if (currentMarket === 'stock' || currentMarket === 'futures') {
            if (ui.status) ui.status.textContent = `⚠️ 載入失敗 (${currentSymbol})，請檢查代碼或連線。`;
        } else {
            alert(`載入失敗 (${currentSymbol})，請檢查代碼或連線。`);
        }
    }
}

// 清空圖表所有系列（搜尋不到資料時施作）
function clearChart() {
    cachedCandles = [];
    if (candleSeries) candleSeries.setData([]);
    if (volumeSeries) volumeSeries.setData([]);
    if (emaShortSeries) emaShortSeries.setData([]);
    if (emaLongSeries) emaLongSeries.setData([]);
    if (bbUpperSeries) bbUpperSeries.setData([]);
    if (bbMidSeries) bbMidSeries.setData([]);
    if (bbLowerSeries) bbLowerSeries.setData([]);
    if (rsiSeries) rsiSeries.setData([]);
    if (candleSeries) candleSeries.setMarkers([]);
}

// ---- 資料來源 Badge + 倒數 ----
function updateDataSourceBadge(source, nextUpdateIn) {
    const badge = document.getElementById('data-source-badge');
    if (!badge) return;

    if (!source) {
        badge.style.display = 'none';
        return;
    }
    badge.style.display = 'inline-flex';

    const config = {
        'yfinance': { icon: '⚡', label: 'Yahoo Finance 即時', bg: 'rgba(16,185,129,0.15)', border: '#10b981', color: '#10b981' },
        'yfinance_cache': { icon: '📦', label: 'Yahoo Finance 快取', bg: 'rgba(251,191,36,0.15)', border: '#fbbf24', color: '#fbbf24' },
        'twse_daily': { icon: '🏦', label: '證交所歷史日線', bg: 'rgba(59,130,246,0.15)', border: '#3b82f6', color: '#3b82f6' },
        'twse_daily_cache': { icon: '📦', label: '證交所快取', bg: 'rgba(148,163,184,0.15)', border: '#94a3b8', color: '#94a3b8' },
        'signals_cache': { icon: '📦', label: '信號快取', bg: 'rgba(148,163,184,0.15)', border: '#94a3b8', color: '#94a3b8' },
        'rate_limited': { icon: '⏳', label: '限流中，等待更新', bg: 'rgba(239,68,68,0.12)', border: '#ef4444', color: '#ef4444' },
        'archive_fallback': { icon: '🗄️', label: '本地持久存檔庫', bg: 'rgba(139,92,246,0.15)', border: '#8b5cf6', color: '#8b5cf6' },

        // 盤後狀態
        'yfinance_closed': { icon: '🌙', label: 'Yahoo 盤後資料', bg: 'rgba(148,163,184,0.15)', border: '#94a3b8', color: '#94a3b8' },
        'yfinance_cache_closed': { icon: '🌙', label: 'Yahoo 盤後快取', bg: 'rgba(148,163,184,0.15)', border: '#94a3b8', color: '#94a3b8' },
        'twse_daily_closed': { icon: '🌙', label: '證交所盤後日線', bg: 'rgba(148,163,184,0.15)', border: '#94a3b8', color: '#94a3b8' },
    };

    const c = config[source] || { icon: 'ℹ️', label: source, bg: 'rgba(100,100,100,0.1)', border: '#666', color: '#aaa' };

    badge.style.background = c.bg;
    badge.style.border = `1px solid ${c.border}`;
    badge.style.color = c.color;

    const countdownText = (nextUpdateIn !== null && nextUpdateIn >= 0)
        ? ` · Next: <span id="badge-countdown">${nextUpdateIn}</span>s`
        : '';
    badge.innerHTML = `<span>${c.icon} ${c.label}</span>${countdownText}`;
}

function startBadgeCountdown(seconds) {
    if (twCountdownTimer) clearInterval(twCountdownTimer);
    let remaining = seconds;
    twCountdownTimer = setInterval(() => {
        remaining = Math.max(0, remaining - 1);
        const el = document.getElementById('badge-countdown');
        if (el) el.textContent = remaining;
        if (remaining <= 0) clearInterval(twCountdownTimer);
    }, 1000);
}

function scheduleAutoRefresh(nextUpdateIn) {
    // 先啟動倒數顯示
    startBadgeCountdown(nextUpdateIn);
    // 清除舊 timer
    if (twAutoRefreshTimer) clearTimeout(twAutoRefreshTimer);
    const delayMs = Math.max(nextUpdateIn * 1000, 5000);
    twAutoRefreshTimer = setTimeout(async () => {
        console.log('[auto-refresh] Refreshing TW chart data...');
        await loadChartData(currentTimeframe, false);
        // 若是台股，也順便更新信號
        if (currentMarket === 'stock' || currentMarket === 'futures') {
            fetchTwSignals(currentSymbol, currentMarket);
        }
    }, delayMs);
}

function clearAutoRefresh() {
    if (twAutoRefreshTimer) { clearTimeout(twAutoRefreshTimer); twAutoRefreshTimer = null; }
    if (twCountdownTimer) { clearInterval(twCountdownTimer); twCountdownTimer = null; }
}

// 根據 UI 設定刷新所有技術指標
function refreshIndicators() {
    if (!cachedCandles || cachedCandles.length === 0) return;

    // 1. 成交量
    if (volumeSeries) {
        const isVolEnabled = document.getElementById('enable-vol').checked;
        if (isVolEnabled) {
            const volData = cachedCandles.map(d => ({
                time: d.time,
                value: d.rawVolume || 0, // 我們需要在 cleanData 存入原始 volume
                color: d.close >= d.open ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)'
            }));
            volumeSeries.setData(volData);
        } else {
            volumeSeries.setData([]);
        }
    }

    // 2. 短期 EMA
    if (emaShortSeries) {
        const isShortEnabled = document.getElementById('enable-ema-short').checked;
        const period = parseInt(document.getElementById('val-ema-short').value) || 25;
        if (isShortEnabled && cachedCandles.length > period) {
            emaShortSeries.setData(calculateEMA(cachedCandles, period));
        } else {
            emaShortSeries.setData([]);
        }
    }

    // 3. 長期 EMA
    if (emaLongSeries) {
        const isLongEnabled = document.getElementById('enable-ema-long').checked;
        const period = parseInt(document.getElementById('val-ema-long').value) || 99;
        if (isLongEnabled && cachedCandles.length > period) {
            emaLongSeries.setData(calculateEMA(cachedCandles, period));
        } else {
            emaLongSeries.setData([]);
        }
    }

    // 4. 布林帶
    if (bbUpperSeries) {
        const isBBEnabled = document.getElementById('enable-bbands').checked;
        const p = parseInt(document.getElementById('val-bb-p').value) || 20;
        const s = parseFloat(document.getElementById('val-bb-s').value) || 2;
        if (isBBEnabled && cachedCandles.length > p) {
            const bb = calculateBBands(cachedCandles, p, s);
            bbUpperSeries.setData(bb.map(d => ({ time: d.time, value: d.upper })));
            bbMidSeries.setData(bb.map(d => ({ time: d.time, value: d.mid })));
            bbLowerSeries.setData(bb.map(d => ({ time: d.time, value: d.lower })));
        } else {
            bbUpperSeries.setData([]); bbMidSeries.setData([]); bbLowerSeries.setData([]);
        }
    }

    // 5. RSI
    if (rsiSeries) {
        const isRSIEnabled = document.getElementById('enable-rsi').checked;
        const p = parseInt(document.getElementById('val-rsi').value) || 14;
        if (isRSIEnabled && cachedCandles.length > p) {
            chart.priceScale('rsi-scale').applyOptions({ visible: true });
            rsiSeries.setData(calculateRSI(cachedCandles, p));
        } else {
            chart.priceScale('rsi-scale').applyOptions({ visible: false });
            rsiSeries.setData([]);
        }
    }
}

// 指標計算函數庫
function calculateEMA(data, period) {
    const k = 2 / (period + 1);
    let emaData = [];
    if (data.length === 0) return [];
    let prevEma = data[0].close;
    for (let i = 0; i < data.length; i++) {
        prevEma = (data[i].close * k) + (prevEma * (1 - k));
        emaData.push({ time: data[i].time, value: prevEma });
    }
    return emaData;
}

function calculateBBands(data, period, stdDev) {
    let bb = [];
    for (let i = period - 1; i < data.length; i++) {
        const subset = data.slice(i - period + 1, i + 1);
        const avg = subset.reduce((acc, d) => acc + d.close, 0) / period;
        const squareDiffs = subset.map(d => Math.pow(d.close - avg, 2));
        const sd = Math.sqrt(squareDiffs.reduce((acc, d) => acc + d, 0) / period);
        bb.push({
            time: data[i].time,
            mid: avg,
            upper: avg + stdDev * sd,
            lower: avg - stdDev * sd
        });
    }
    return bb;
}

function calculateRSI(data, period) {
    let rsi = [];
    let gains = 0, losses = 0;
    for (let i = 1; i <= period; i++) {
        const diff = data[i].close - data[i - 1].close;
        if (diff >= 0) gains += diff; else losses -= diff;
    }
    let avgGain = gains / period; let avgLoss = losses / period;

    for (let i = period + 1; i < data.length; i++) {
        const diff = data[i].close - data[i - 1].close;
        let gain = diff >= 0 ? diff : 0;
        let loss = diff < 0 ? -diff : 0;
        avgGain = (avgGain * (period - 1) + gain) / period;
        avgLoss = (avgLoss * (period - 1) + loss) / period;
        const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
        rsi.push({ time: data[i].time, value: 100 - (100 / (1 + rs)) });
    }
    return rsi;
}

// 生成歷史戰略標記
function generateHistoricalMarkers(data) {
    if (data.length < 100) return [];

    const markers = [];
    const ema25 = calculateEMA(data, 25);
    const ema99 = calculateEMA(data, 99);
    const rsi = calculateRSI(data, 14);

    // 從第 100 根開始算，才有完整的均線數據
    for (let i = 100; i < data.length; i++) {
        const item = data[i];
        const prev = data[i - 1];
        const rsiVal = rsi.find(r => r.time === item.time)?.value || 50;
        const e25Val = ema25.find(e => e.time === item.time)?.value;
        const e99Val = ema99.find(e => e.time === item.time)?.value;

        if (!e25Val || !e99Val) continue;

        // --- [戰略判定] ---
        const isBullish = item.close > e25Val && e25Val > e99Val; // 多頭排列
        const isStrong = rsiVal > 62; // 強勢動能

        // 判定反轉/清倉
        if (prev.close >= e99Val && item.close < e99Val) {
            markers.push({
                time: item.time,
                position: 'aboveBar',
                color: '#ef5350',
                shape: 'arrowDown',
                text: '清倉 🛑',
            });
        }
        // 判定建倉 (只有在強勢多頭排列時才顯示文字)
        else if (prev.close <= e25Val && item.close > e25Val) {
            const highQuality = isBullish && isStrong;
            markers.push({
                time: item.time,
                position: 'belowBar',
                color: highQuality ? '#26a69a' : 'rgba(38, 166, 154, 0.4)',
                shape: 'arrowUp',
                text: highQuality ? '建倉 🚀' : '', // 弱勢時不給文字指令，避免誤導
                size: highQuality ? 1.2 : 0.8
            });
        }
        // 判定加碼 (只在強勢趨勢中給予文字)
        else if (item.close >= e25Val && item.low <= e25Val && rsiVal < 60 && rsiVal > 45 && isBullish) {
            if (markers.length === 0 || i - data.indexOf(data.find(d => d.time === markers[markers.length - 1].time)) > 8) {
                const isGreatSetup = rsiVal > 50;
                markers.push({
                    time: item.time,
                    position: 'belowBar',
                    color: '#26c6da',
                    shape: 'arrowUp',
                    text: isGreatSetup ? '加碼 💎' : '',
                    size: isGreatSetup ? 1 : 0.7
                });
            }
        }
    }
    return markers;
}

// === 核心：信號語意分級系統 ===
// 將原始的「分數 + 方向」轉換為使用者能立即理解的語意標籤
function getSignalLabel(direction, confidence) {
    const score = parseFloat(confidence) || 0;

    if (direction === 'BUY') {
        if (score >= 70) return { text: '🟢 強勢做多', css: 'buy', level: '極強信號', brief: 'STRONG BUY' };
        if (score >= 50) return { text: '🟢 做多傾向', css: 'buy', level: '中等信號', brief: 'BUY' };
        if (score >= 30) return { text: '🟡 弱多觀望', css: 'weak', level: '弱信號', brief: 'WEAK BUY' };
        return { text: '⚪ 多空不明', css: '', level: '無信號', brief: 'NEUTRAL' };
    }
    if (direction === 'SELL') {
        if (score >= 70) return { text: '🔴 強勢做空', css: 'sell', level: '極強警告', brief: 'STRONG SELL' };
        if (score >= 50) return { text: '🔴 做空傾向', css: 'sell', level: '中等警告', brief: 'SELL' };
        if (score >= 30) return { text: '🟡 弱空觀望', css: 'weak', level: '弱信號', brief: 'WEAK SELL' };
        return { text: '⚪ 多空不明', css: '', level: '無信號', brief: 'NEUTRAL' };
    }
    return { text: '⚪ 觀望', css: '', level: '無信號', brief: 'NEUTRAL' };
}

// 根據分數與方向更新圓環和顏色
function updateRing(ringElement, textElement, score, direction) {
    const val = Math.max(0, Math.min(100, score));
    textElement.textContent = val;
    ringElement.style.strokeDasharray = `${val}, 100`;

    // 弱信號使用灰色，讓使用者直覺知道「這個分數不具參考價值」
    let color = '#94a3b8'; // 無信號灰
    if (val >= 30) {
        if (direction === 'BUY') color = val >= 50 ? '#10b981' : '#6ee7b7'; // 強綠 / 淡綠
        if (direction === 'SELL') color = val >= 50 ? '#ef4444' : '#fca5a5'; // 強紅 / 淡紅
    }
    ringElement.style.stroke = color;
}

function updateDirection(el, dir, level, confidence) {
    const label = getSignalLabel(dir, confidence);
    el.textContent = label.text;
    el.className = 'signal-direction ' + (label.css || '');

    // 更新信號等級文字
    if (el.nextElementSibling && el.nextElementSibling.classList.contains('signal-level')) {
        el.nextElementSibling.textContent = label.level;
    }
}

// 處理伺服器資料
function processData(serverData) {
    if (!Array.isArray(serverData)) return;
    lastServerData = serverData;

    serverData.forEach(item => {
        const symbol = item.symbol;
        const sigs = item.signals;
        if (!sigs || !sigs['1d']) return;

        const d1 = sigs['1d'];
        const price = d1.price;
        const direction = d1.direction;
        const confidence = d1.confidence;

        // --- 更新頂部 Ticker 價格條 ---
        let tickerId = null;
        if (symbol === 'BTC/USDT') tickerId = 'btc';
        else if (symbol === 'ETH/USDT') tickerId = 'eth';
        else if (symbol === 'SOL/USDT') tickerId = 'sol';
        else if (symbol === '2330.TW') tickerId = '2330';
        else if (symbol === '0050.TW') tickerId = '0050';
        else if (symbol === '2317.TW') tickerId = '2317';
        else if (symbol === '2881.TW') tickerId = '2881';
        else if (symbol === '2603.TW') tickerId = '2603';

        if (tickerId) {
            const valEls = document.querySelectorAll(`[id^="ticker-${tickerId}"]`);
            const chgEls = document.querySelectorAll(`[id^="ticker-${tickerId}-chg"]`);
            const scoreEls = document.querySelectorAll(`[id^="ticker-${tickerId}-score"]`);

            valEls.forEach(el => {
                if (el && !el.id.includes('chg') && !el.id.includes('score')) {
                    el.textContent = formatPrice(price);
                }
            });

            const change24h = d1.change_24h || 0;
            chgEls.forEach(el => {
                if (el) {
                    const color = change24h > 0 ? '#10b981' : change24h < 0 ? '#ef4444' : '#94a3b8';
                    const prefix = change24h > 0 ? '▲' : change24h < 0 ? '▼' : '';
                    el.textContent = `${prefix}${Math.abs(change24h)}%`;
                    el.style.color = color;
                }
            });

            scoreEls.forEach(el => {
                if (el) el.textContent = `Score: ${confidence}`;
            });
        }

        // --- 若為目前選中的標的與市場，才更新儀表板主畫面 ---
        if (symbol === currentSymbol) {
            const currencyPrefix = (currentMarket === 'crypto') ? '$' : '';
            if (ui.btc && ui.btc.price) ui.btc.price.textContent = `${currencyPrefix}${formatPrice(price)}`;
            if (ui.status) ui.status.textContent = `最後更新: ${d1.timestamp}`;

            // Crypto 模式：恢復 4H 區塊顯示
            const tfSlots = document.querySelectorAll('.tf-slot');
            if (tfSlots.length >= 2) {
                tfSlots[1].style.display = '';
                tfSlots[0].style.flex = '';
            }
            const h4Badge = document.getElementById('h-score-4h');
            if (h4Badge) h4Badge.closest('.h-badge').style.display = '';

            // 1D Update
            const d1_sig = sigs['1d'];
            if (d1_sig) {
                updateRing(ui.btc.d1.ring, ui.btc.d1.score, d1_sig.confidence, d1_sig.direction);
                updateDirection(ui.btc.d1.dir, d1_sig.direction, d1_sig.level, d1_sig.confidence);
                ui.btc.d1.buy.textContent = d1_sig.buy_score || 0;
                ui.btc.d1.sell.textContent = d1_sig.sell_score || 0;
            }

            // --- 更新全屏模式 Header 精簡面板 ---
            const hPrice = document.getElementById('h-price-btc');
            const hS1d = document.getElementById('h-score-1d');
            if (hPrice) hPrice.textContent = `$${formatPrice(d1.price)}`;
            if (hS1d) {
                hS1d.textContent = d1.confidence;
                const color = d1.direction === 'BUY' ? '#10b981' : d1.direction === 'SELL' ? '#ef4444' : '#94a3b8';
                hS1d.style.color = color;
            }

            // --- 即時同步圖表價格 (讓右邊紅標籤與左邊同步) ---
            if (candleSeries && d1.price) {
                const nowUnix = Math.floor(Date.now() / 1000);
                const t = currentTimeframe === '1d' ? new Date().toISOString().split('T')[0] : nowUnix;
                try {
                    const price = parseFloat(d1.price);
                    candleSeries.update({
                        time: t,
                        open: price, high: price, low: price, close: price
                    });
                    // 同步更新成交量 (即時資料通常不帶成交量，這裡用 0 或保持)
                    if (volumeSeries) {
                        volumeSeries.update({ time: t, value: 0 }); // WebSocket 目前暫無即時成交量，先占位
                    }
                } catch (e) { /* 靜默忽略 */ }
            }
            // ------------------------------------------

            // 4H Update
            if (sigs['4h']) {
                const h4 = sigs['4h'];
                updateRing(ui.btc.h4.ring, ui.btc.h4.score, h4.confidence, h4.direction);
                updateDirection(ui.btc.h4.dir, h4.direction, null, h4.confidence);
                ui.btc.h4.buy.textContent = h4.buy_score;
                const sell4hEl = document.getElementById('sell-btc-4h');
                if (sell4hEl) sell4hEl.textContent = h4.sell_score;

                // --- 更新全屏模式 Header 精簡面板 ---
                const hS4h = document.getElementById('h-score-4h');
                if (hS4h) {
                    hS4h.textContent = h4.confidence;
                    const color = h4.direction === 'BUY' ? '#10b981' : h4.direction === 'SELL' ? '#ef4444' : '#94a3b8';
                    hS4h.style.color = color;
                }

                // --- 專業戰略指令系統 ---
                const currentRSI = rsiSeries && cachedCandles.length > 0 ? calculateRSI(cachedCandles, 14).pop()?.value : 50;
                const currentEMA25 = emaShortSeries && cachedCandles.length > 0 ? calculateEMA(cachedCandles, parseInt(document.getElementById('val-ema-short').value) || 25).pop()?.value : null;
                const currentEMA99 = emaLongSeries && cachedCandles.length > 0 ? calculateEMA(cachedCandles, parseInt(document.getElementById('val-ema-long').value) || 99).pop()?.value : null;
                const price = parseFloat(d1.price);

                const avgScore = (parseFloat(d1.confidence) + parseFloat(h4.confidence)) / 2;
                const isOverheated = d1.direction === 'BUY' && currentRSI > 78;
                const isPullback = d1.direction === 'BUY' && h4.direction === 'BUY' && currentRSI < 55 && currentRSI > 40 &&
                    currentEMA25 && (Math.abs(price - currentEMA25) / currentEMA25 < 0.008);
                const isReversal = (d1.direction !== 'BUY' && currentEMA99 && price < currentEMA99);

                if (isReversal && d1.direction === 'SELL') {
                    ui.btc.res_alert.className = 'resonance-alert sell';
                    ui.btc.res_alert.innerHTML = '🚨 <strong>趨勢反轉確認</strong>：價格跌破長期支撐線且動能轉空，建議【全數清倉】或反手做空。';
                } else if (d1.direction === 'BUY' && h4.direction === 'BUY') {
                    ui.btc.res_alert.className = 'resonance-alert buy';
                    if (isOverheated) {
                        ui.btc.res_alert.innerHTML = '🎯 <strong>過熱提醒</strong>：趨勢強勁但短期乖離率過大，【嚴禁追高】，請等待回調。';
                    } else if (isPullback) {
                        ui.btc.res_alert.innerHTML = '💎 <strong>優勢加碼點</strong>：多頭趨勢中的回踩確認，適合【分批進場】或【加碼部位】。';
                    } else if (avgScore > 75) {
                        ui.btc.res_alert.innerHTML = '🚀 <strong>金叉噴發點</strong>：強勢共振引爆，【初期進場】的黃金時機，勝率極高。';
                    } else if (avgScore < 50) {
                        ui.btc.res_alert.innerHTML = '⚠️ <strong>動能不足</strong>：長短期方向雖一致，但總體分數（' + avgScore.toFixed(1) + '）較低，目前【勝率不穩】，建議保持觀望。';
                    } else {
                        ui.btc.res_alert.innerHTML = '🎯 <strong>強烈信號 (共振)</strong>：日線與4小時方向一致且動能充沛，是絕佳的趨勢交易機會！';
                    }
                } else if (d1.direction === 'SELL' && h4.direction === 'SELL') {
                    ui.btc.res_alert.className = 'resonance-alert sell';
                    const avgScore = (parseFloat(d1.confidence) + parseFloat(h4.confidence)) / 2;
                    if (avgScore < 50) {
                        ui.btc.res_alert.innerHTML = '⚠️ <strong>弱勢空頭</strong>：空方動能不強（' + avgScore.toFixed(1) + '），目前【勝率較低】，建議空倉觀察，避免頻繁交易。';
                    } else {
                        ui.btc.res_alert.innerHTML = '🚨 <strong>強烈危險 (共振)</strong>：空頭趨勢共振，請注意風險，切勿在此盲目抄底！';
                    }
                } else {
                    ui.btc.res_alert.className = 'resonance-alert';
                    ui.btc.res_alert.innerHTML = '💡 <strong>戰略觀望</strong>：長短期方向不一致，市場正處於多空拉鋸，【空倉等待】是最佳策略。';
                }
            }

            // --- 同步全屏 Header 的訊號文字與建議 ---
            syncFullscreenHeader(symbol, d1.direction, d1.confidence, ui.btc.res_alert.innerHTML);

            // 圖表會在切換選單或啟動時透過 /api/chart 載入歷史資料
            // 若需要即時加上一根，實務上可在此呼叫 candleSeries.update()
        }

        // 小卡片更新
        // 小卡片使用語意標籤，讓使用者一眼判讀
        if (symbol === 'BTC/USDT' && sigs['1d']) {
            const lbl = getSignalLabel(sigs['1d'].direction, sigs['1d'].confidence);
            ui.btcm.price.textContent = `$${formatPrice(sigs['1d'].price)}`;
            ui.btcm.dir1d.textContent = lbl.brief;
            ui.btcm.score1d.textContent = sigs['1d'].confidence;
        }
        if (symbol === 'ETH/USDT' && sigs['1d']) {
            const lbl = getSignalLabel(sigs['1d'].direction, sigs['1d'].confidence);
            ui.eth.price.textContent = `$${formatPrice(sigs['1d'].price)}`;
            ui.eth.dir1d.textContent = lbl.brief;
            ui.eth.score1d.textContent = sigs['1d'].confidence;
        }
        if (symbol === 'SOL/USDT' && sigs['1d']) {
            const lbl = getSignalLabel(sigs['1d'].direction, sigs['1d'].confidence);
            ui.sol.price.textContent = `$${formatPrice(sigs['1d'].price)}`;
            ui.sol.dir1d.textContent = lbl.brief;
            ui.sol.score1d.textContent = sigs['1d'].confidence;
        }
    });
}

function syncFullscreenHeader(symbol, dir, confidence, adviceHtml) {
    const elSig = document.getElementById('h-signal-text');
    const elAdv = document.getElementById('h-advice-text');
    const elSym = document.getElementById('chart-symbol');

    if (elSym) elSym.textContent = symbol;

    if (elSig) {
        const lbl = getSignalLabel(dir, confidence);
        elSig.textContent = lbl.text;
        elSig.className = 'h-signal ' + (lbl.css || '');
    }
    if (elAdv) {
        elAdv.innerHTML = adviceHtml;
    }
}

// WebSocket 監聽
ws.onmessage = (event) => {
    try {
        const payload = JSON.parse(event.data);
        if (payload.type === "init" || payload.type === "update") {
            processData(payload.data);

            // 更新 crypto 時間戳
            const now = new Date();
            const tStr = now.toTimeString().slice(0, 8);
            updateTimestamps(tStr, null);

            const isSafeMode = payload.safe_mode || false;
            const safeEl = document.getElementById("safe-mode-alert");
            const safeEventText = document.getElementById("safe-event-name");
            if (safeEl) {
                safeEl.style.display = isSafeMode ? "block" : "none";
                if (isSafeMode && safeEventText) {
                    safeEventText.textContent = payload.safe_mode_event || "重大事件";
                }
            }

            const sentiment = payload.global_alert;
            const actionStrip = document.getElementById("action-strip");
            const newsArea = document.getElementById("news-ticker-area");
            const eventArea = document.getElementById("event-ticker-area");
            const divider = document.querySelector("#action-strip .vertical-divider");
            let hasNews = false, hasEvents = false;

            if (sentiment) {
                // 事件倒數
                if (sentiment.scheduled && sentiment.scheduled.length > 0) {
                    updateEventTimers(sentiment.scheduled);
                    hasEvents = true;
                }
                if (eventArea) eventArea.style.display = hasEvents ? "flex" : "none";

                // 即時情緒事件
                const current = sentiment.current;
                const alertEl = document.getElementById("global-alert");
                const alertText = document.getElementById("alert-text");
                const alertTag = document.getElementById("alert-tag");

                if (current && alertEl && alertText) {
                    const analysisLabel = current.analysis ? ` [${current.analysis}]` : "";
                    const fullText = current.text + analysisLabel;

                    // 建立可點擊的連結 + 跑馬燈容器
                    if (current.link) {
                        alertText.innerHTML = `<a href="${current.link}" target="_blank" rel="noopener" class="news-link"><span class="news-marquee-inner">${fullText}</span></a>`;
                    } else {
                        alertText.innerHTML = `<span class="news-marquee-inner">${fullText}</span>`;
                    }
                    // 文字夠長才啟動跑馬燈動畫
                    const inner = alertText.querySelector('.news-marquee-inner');
                    if (inner && inner.scrollWidth > alertText.clientWidth) {
                        alertText.classList.add('news-marquee');
                    } else {
                        alertText.classList.remove('news-marquee');
                    }

                    alertTag.textContent = current.tag || "BREAKING";
                    hasNews = true;

                    // 用 CSS class 搭配主題色
                    alertEl.style.background = "";
                    alertEl.classList.remove("sentiment-bearish", "sentiment-bullish", "sentiment-neutral");
                    if (alertTag) alertTag.classList.remove("tag-bearish", "tag-bullish", "tag-neutral");

                    if (current.score <= -10) {
                        alertEl.classList.add("sentiment-bearish");
                        if (alertTag) alertTag.classList.add("tag-bearish");
                    } else if (current.score >= 10) {
                        alertEl.classList.add("sentiment-bullish");
                        if (alertTag) alertTag.classList.add("tag-bullish");
                    } else {
                        alertEl.classList.add("sentiment-neutral");
                        if (alertTag) alertTag.classList.add("tag-neutral");
                    }
                }
                if (newsArea) newsArea.style.display = hasNews ? "flex" : "none";
            }

            // 整條 action-strip：有任何內容才顯示
            if (actionStrip) actionStrip.style.display = (hasNews || hasEvents) ? "flex" : "none";
            // 分隔線：兩區都有內容才顯示
            if (divider) divider.style.display = (hasNews && hasEvents) ? "block" : "none";
        }
    } catch (e) {
        console.error("Data parse error", e);
    }
};

ws.onopen = () => {
    if (ui.status) ui.status.textContent = '連線成功，等待數據推播...';
    if (ui.connStatus) {
        ui.connStatus.textContent = '✅ 系統正常 (Online)';
        ui.connStatus.style.color = '';
    }
    if (ui.statusDot) {
        ui.statusDot.classList.remove('offline');
    }
};

ws.onclose = () => {
    if (ui.status) ui.status.textContent = '連線中斷，請稍後。';
    if (ui.connStatus) {
        ui.connStatus.textContent = '❌ 連線中斷 (Offline)';
        ui.connStatus.style.color = '#ef4444';
    }
    if (ui.statusDot) {
        ui.statusDot.classList.add('offline');
    }
};

// 頁面載入完成
document.addEventListener('DOMContentLoaded', () => {
    initChart();
    startHeartbeat();
    initTickerData(); // 載入時一次拉取所有 ticker（含台股）
    initScreener();   // 載入超級選股系統

    // --- 跑馬燈點擊 → 切換到該標的 ---
    const tickerStrip = document.getElementById('ticker-content');
    if (tickerStrip) {
        tickerStrip.addEventListener('click', (e) => {
            const item = e.target.closest('.ticker-item');
            if (!item) return;
            const sym = item.dataset.symbol;
            const mkt = item.dataset.market;
            if (sym && mkt) {
                window.changeSymbol(sym, mkt);
            }
        });
    }

    // --- 市場選擇 (虛擬幣 / 台股) ---
    const marketSelect = document.getElementById('market-select');
    const cryptoInput = document.getElementById('crypto-symbol-input');
    const stockInput = document.getElementById('stock-symbol-input');
    const futuresInput = document.getElementById('futures-symbol-input');
    const globalLoadBtn = document.getElementById('global-load-btn');

    const switchToStock = (raw) => {
        if (marketSelect) marketSelect.value = 'stock';
        if (cryptoInput) cryptoInput.style.display = 'none';
        if (stockInput) { stockInput.style.display = 'block'; stockInput.value = raw; }
        if (futuresInput) futuresInput.style.display = 'none';
        const symbol = raw.includes('.') ? raw : `${raw}.TW`;
        window.changeSymbol(symbol, 'stock');
    };

    const handleSearch = () => {
        const m = marketSelect.value;
        // 取得當前可見輸入框的值
        const activeInput = m === 'crypto' ? cryptoInput :
                            m === 'stock' ? stockInput : futuresInput;
        let raw = activeInput ? activeInput.value.trim() : '';

        // 如果當前輸入框空的，嘗試從其他輸入框取值（用戶可能在錯的輸入框打字）
        if (!raw) {
            [cryptoInput, stockInput, futuresInput].forEach(inp => {
                if (!raw && inp && inp.value.trim()) raw = inp.value.trim();
            });
        }
        if (!raw) return;

        // 智慧偵測：純數字 4~6 碼 → 台股代碼，自動切換市場
        if (/^\d{4,6}$/.test(raw)) {
            switchToStock(raw);
            return;
        }

        // 智慧偵測：中文股名 → 台股，無論在哪個市場模式
        if (stockNameSearchMapping[raw]) {
            const symbol = stockNameSearchMapping[raw];
            if (marketSelect) marketSelect.value = 'stock';
            if (cryptoInput) cryptoInput.style.display = 'none';
            if (stockInput) { stockInput.style.display = 'block'; stockInput.value = raw; }
            if (futuresInput) futuresInput.style.display = 'none';
            window.changeSymbol(symbol, 'stock');
            return;
        }

        if (m === 'crypto') {
            raw = raw.toUpperCase();
            const symbol = cryptoNames[raw] || (raw.includes('/') ? raw : `${raw}/USDT`);
            window.changeSymbol(symbol, 'crypto');
        } else if (m === 'stock') {
            const symbol = raw.includes('.') ? raw : `${raw}.TW`;
            window.changeSymbol(symbol, 'stock');
        } else if (m === 'futures') {
            raw = raw.toUpperCase();
            window.changeSymbol(raw, 'futures');
        }
    };

    if (globalLoadBtn) globalLoadBtn.addEventListener('click', handleSearch);
    [cryptoInput, stockInput, futuresInput].forEach(inp => {
        if (inp) {
            inp.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); handleSearch(); }
            });
            // type="search" 在部分瀏覽器按 Enter 會觸發 search 事件
            inp.addEventListener('search', (e) => { e.preventDefault(); handleSearch(); });
        }
    });

    if (marketSelect) {
        marketSelect.addEventListener('change', (e) => {
            const m = e.target.value;
            // 控制三種輸入框的可見性
            if (cryptoInput) cryptoInput.style.display = (m === 'crypto' ? 'block' : 'none');
            if (stockInput) stockInput.style.display = (m === 'stock' ? 'block' : 'none');
            if (futuresInput) futuresInput.style.display = (m === 'futures' ? 'block' : 'none');

            // 切換市場時預帶入常用標的
            if (m === 'crypto') {
                window.changeSymbol('BTC/USDT', 'crypto');
                if (cryptoInput) cryptoInput.value = '';
            } else if (m === 'stock') {
                window.changeSymbol('2330.TW', 'stock');
                if (stockInput) stockInput.value = '2330';
            } else if (m === 'futures') {
                window.changeSymbol('TX', 'futures');
                if (futuresInput) futuresInput.value = 'TX';
            }
        });
    }

    // --- 教學 Modal 控制 ---
    const modal = document.getElementById('tutorial-modal');
    const openBtn = document.getElementById('open-tutorial');
    const closeBtn = document.getElementById('close-tutorial');

    if (openBtn && modal) {
        openBtn.addEventListener('click', () => {
            modal.classList.add('active');
        });
    }

    if (closeBtn && modal) {
        closeBtn.addEventListener('click', () => {
            modal.classList.remove('active');
        });
    }

    // 點擊背景也可關閉
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.classList.remove('active');
        });
    }

    // 初始化時設定正確顯示 (crypto / stock)
    window.changeSymbol(currentSymbol, currentMarket);
    renderHistory();
});
// 啟動連線品質監測 (Ping Heartbeat)
async function startHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);

    heartbeatTimer = setInterval(async () => {
        const start = Date.now();
        try {
            const res = await fetch('/api/ping', { cache: 'no-store' });
            const rtt = Date.now() - start;

            if (res.ok) {
                if (ui.statusDot) {
                    ui.statusDot.classList.remove('offline');
                    // RTT > 800ms 顯示黃燈，否則正常綠燈
                    if (rtt > 800) {
                        ui.statusDot.classList.add('slow');
                        if (ui.connStatus) {
                            ui.connStatus.textContent = `⚠️ 連線遲緩 (${rtt}ms)`;
                            ui.connStatus.style.color = '#f59e0b';
                        }
                    } else {
                        ui.statusDot.classList.remove('slow');
                        if (ui.connStatus) {
                            ui.connStatus.textContent = '✅ 系統正常 (Online)';
                            ui.connStatus.style.color = '';
                        }
                    }
                }
            }
        } catch (e) {
            // Fetch 失敗視為離線
            if (ui.statusDot) {
                ui.statusDot.classList.add('offline');
                ui.statusDot.classList.remove('slow');
            }
            if (ui.connStatus) {
                ui.connStatus.textContent = '❌ 連線中斷 (Offline)';
                ui.connStatus.style.color = '#ef4444';
            }
        }
    }, 30000); // 30 秒偵測一次
}

/**
 * Phase 3: 更新重大事件倒數計時器
 */
function updateEventTimers(scheduledEvents) {
    const listEl = document.getElementById('event-timer-list');
    if (!listEl || !scheduledEvents) return;

    listEl.innerHTML = ''; // 清空

    scheduledEvents.forEach(event => {
        const eventDate = new Date(event.date);
        const now = new Date();
        const diff = eventDate - now;

        let countdownText = "已發生";
        if (diff > 0) {
            const days = Math.floor(diff / (1000 * 60 * 60 * 24));
            const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
            const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

            if (days > 0) countdownText = `${days}天 ${hours}時`;
            else countdownText = `${hours}時 ${mins}分`;
        }

        const item = document.createElement(event.link ? 'a' : 'div');
        item.className = 'event-item';
        if (event.link) {
            item.href = event.link;
            item.target = '_blank';
            item.rel = 'noopener';
        }
        item.innerHTML = `
            <div class="event-info">
                <span class="event-name">${event.name}</span>
                <span class="event-warning">⚠️ ${event.warning}</span>
            </div>
            <div class="event-countdown-box">${countdownText}</div>
        `;
        listEl.appendChild(item);
    });
}
