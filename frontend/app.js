// WebSocket 連線與狀態管理
const ws = new WebSocket(`ws://${window.location.host}/ws/signals`);
let currentSymbol = 'BTC/USDT';
let currentTimeframe = '1d';
let currentMarket = 'crypto';
let lastServerData = [];
// 走勢圖系列
let chart = null;
let candleSeries = null;
let emaShortSeries = null;
let emaLongSeries = null;
let bbUpperSeries = null, bbMidSeries = null, bbLowerSeries = null;
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

        chart = LightweightCharts.createChart(container, {
            width: container.clientWidth || 600,
            height: 300,
            layout: {
                background: { type: 'solid', color: '#131722' },
                textColor: '#d1d4dc',
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
                vertLines: { color: 'rgba(42, 46, 57, 0.5)' },
                horzLines: { color: 'rgba(42, 46, 57, 0.5)' },
            },
            rightPriceScale: {
                borderColor: 'rgba(197, 203, 206, 0.8)',
            },
            timeScale: {
                borderColor: 'rgba(197, 203, 206, 0.8)',
                timeVisible: true,
                secondsVisible: false,
            },
        });

        logDebug("Chart object created.");

        // 終極備援方案：偵測並嘗試所有可能的方法
        if (typeof chart.addCandlestickSeries === 'function') {
            candleSeries = chart.addCandlestickSeries({
                upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
            });
            logDebug("Series: Used addCandlestickSeries");
        } else if (typeof chart.addSeries === 'function') {
            // 嘗試 V4 字串定義方式，捕捉內部的 undefined 錯誤
            try {
                // 如果 LightweightCharts.SeriesType 消失了，試著直接用字串 'Candlestick'
                const type = (LightweightCharts.SeriesType && LightweightCharts.SeriesType.Candlestick) ?
                    LightweightCharts.SeriesType.Candlestick : 'Candlestick';
                candleSeries = chart.addSeries(type, {
                    upColor: '#26a69a', downColor: '#ef5350',
                });
                logDebug("Series: Used addSeries(" + type + ")");
            } catch (err) {
                logDebug("addSeries Logic Error: " + err.message);
            }
        }

        // --- 加入成交量序列 ---
        volumeSeries = chart.addHistogramSeries({
            color: '#26a69a',
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
    const list = quickSymbolButtons[market] || [];
    container.innerHTML = list.map(item =>
        `<button onclick="window.changeSymbol('${item.symbol}','${market}')">${item.label}</button>`
    ).join('');
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

function addToHistory(sym, market) {
    if (!sym) return;
    
    // 移除相同 symbol 的舊紀錄
    searchHistory = searchHistory.filter(item => item.sym.toLowerCase() !== sym.toLowerCase());
    
    // 取得展示用名稱
    let name = '';
    if (market === 'stock') {
        name = stockNames[sym] || sym.split('.')[0];
    } else if (market === 'futures') {
        name = futuresNames[sym.split('.')[0]] || sym;
    } else {
        // 虛擬幣找中文名稱
        const cnEntry = Object.entries(cryptoNames).find(([k, v]) => v === sym && !/^[A-Z/]+$/.test(k));
        name = cnEntry ? cnEntry[0] : sym.split('/')[0];
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

// 切換幣種 / 市場的對外函數
window.changeSymbol = async function (sym, market = 'crypto') {
    const isSymbolChanged = (currentSymbol !== sym || currentMarket !== market);
    currentSymbol = sym;
    currentMarket = market;

    // 加入搜尋紀錄
    addToHistory(sym, market);

    const baseSym = sym.split('/')[0].split('.')[0];
    const compName = market === 'stock' ? (stockNames[sym] || '') : '';
    const futName = market === 'futures' ? (futuresNames[baseSym] || '') : '';

    // 更新標題
    const headerTitle = document.getElementById('main-idx-title');
    const chartTitle = document.getElementById('chart-title');
    const chartSymbol = document.getElementById('chart-symbol');

    if (headerTitle) headerTitle.textContent =
        market === 'stock' ? `台股 ${baseSym}` :
            market === 'futures' ? `台期 ${baseSym}` : sym;
    if (chartTitle) chartTitle.textContent =
        market === 'stock' ? `${baseSym}${compName ? ' ' + compName : ''} 走勢圖` :
            market === 'futures' ? `${baseSym} ${futName} 走勢圖` :
                `${baseSym} 即時走勢圖`;
    if (chartSymbol) chartSymbol.textContent =
        market === 'stock' ? `${baseSym}${compName ? ' · ' + compName : ''}` :
            market === 'futures' ? `${baseSym}${futName ? ' · ' + futName : ''}` : sym;

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

    // 重新載入圖表資料，且如果是切換幣種，就強制縮放
    renderSymbolSwitcher(market);
    await loadChartData(currentTimeframe, isSymbolChanged);

    // 若是切換到股票，嘗試取得公司名稱並更新標題
    if (market === 'stock') {
        if (!compName) {
            const info = await fetchStockInfo(sym);
            if (info && info.name) {
                stockNames[sym] = info.name;
                if (chartTitle) chartTitle.textContent = `${baseSym} ${info.name} 走勢圖`;
                if (chartSymbol) chartSymbol.textContent = `${baseSym} · ${info.name}`;
            }
        }
    }

    // 台股 / 期貨：盤後信號計算
    if (market === 'stock' || market === 'futures') {
        fetchTwSignals(sym, market);
    }

    // 只在 Crypto 模式下顯示即時訊號卡片
    if (market === 'crypto' && lastServerData.length > 0) {
        processData(lastServerData);
    }
};

function clearSignalCard(market = 'stock', loading = false) {
    const modeLabel = market === 'futures' ? '台股期貨' : '台股';
    ui.status.textContent = loading
        ? `${modeLabel}盤後信號計算中...`
        : `${modeLabel}模式：無即時信號`;
    ui.btc.price.textContent = loading ? '...' : '--';

    const clearRing = (ringEl, scoreEl, dirEl, scoreTextEl) => {
        if (ringEl) ringEl.style.strokeDasharray = '0, 100';
        if (scoreEl) scoreEl.textContent = loading ? '...' : '--';
        if (dirEl) dirEl.textContent = loading ? '計算中' : 'N/A';
        if (scoreTextEl) scoreTextEl.textContent = loading ? '...' : 'N/A';
    };

    clearRing(ui.btc.d1.ring, ui.btc.d1.score, ui.btc.d1.dir, ui.btc.d1.lvl);
    clearRing(ui.btc.h4.ring, ui.btc.h4.score, ui.btc.h4.dir, null);

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

            if (priceEl) priceEl.textContent = s.price.toLocaleString();
            if (scoreEl) scoreEl.textContent = s.confidence;
            if (dirEl) {
                const lbl = getSignalLabel(s.direction, s.confidence);
                dirEl.textContent = lbl.brief;
            }
        } catch (e) { /* 靜默 */ }
    });
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

        const modeLabel = market === 'futures' ? '台股期貨' : '台股';

        // 價格
        ui.btc.price.textContent = `${s.price.toLocaleString()}`;
        ui.status.textContent = `${modeLabel}盤後信號（日線）`;

        // 1D 環形圖
        updateRing(ui.btc.d1.ring, ui.btc.d1.score, s.confidence, s.direction);
        updateDirection(ui.btc.d1.dir, s.direction, s.level, s.confidence);
        ui.btc.d1.buy.textContent = s.buy_score;
        ui.btc.d1.sell.textContent = s.sell_score;

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
        if (hPrice) hPrice.textContent = `${s.price.toLocaleString()}`;
        if (hS1d) { hS1d.textContent = s.confidence; hS1d.style.color = s.direction === 'BUY' ? '#10b981' : s.direction === 'SELL' ? '#ef4444' : '#94a3b8'; }
        if (hS4h) { hS4h.textContent = '--'; hS4h.style.color = '#94a3b8'; }
        syncFullscreenHeader(symbol, s.direction, s.confidence, ui.btc.res_alert ? ui.btc.res_alert.innerHTML : '');
    } catch (e) {
        console.error('TW signal fetch error:', e);
        clearSignalCard(market, false);
    }
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

// 載入歷史 K 線資料 (移除 fitContent 以記憶視野)
async function loadChartData(tf, forceFit = false) {
    if (!candleSeries) return;
    try {
        const res = await fetch(`/api/chart?symbol=${currentSymbol}&timeframe=${tf}&market=${currentMarket}`);
        if (!res.ok) throw new Error("API Status: " + res.status);
        const data = await res.json();

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
                        rawVolume: parseFloat(item.volume || 0) // 重要：保留原始成交量
                    });
                    lastT = item.time;
                }
            }


            candleSeries.setData(cleanData);
            cachedCandles = cleanData; // 存入快取

            // 套用目前的指標設定
            refreshIndicators();

            // --- 生成並設定歷史訊號標記 ---
            if (candleSeries) {
                const markers = generateHistoricalMarkers(cleanData);
                candleSeries.setMarkers(markers);
            }

            // 首次載入或切換 symbol/market 時自動調整可見範圍
            if (isInitialChartLoad || forceFit) {
                setTimeout(() => {
                    if (chart && chart.timeScale) {
                        // 預設顯示 60 根 K 線，並在右邊留 1/4 空白 (即 20 根的寬度)
                        // 總顯示格數 = 60 / 0.75 = 80 格
                        const totalVisibleBars = 80;
                        const rightOffset = 20;
                        const lastIndex = cleanData.length - 1;

                        chart.timeScale().applyOptions({
                            rightOffset: rightOffset,
                        });

                        chart.timeScale().setVisibleLogicalRange({
                            from: lastIndex - (totalVisibleBars - rightOffset - 1),
                            to: lastIndex + rightOffset,
                        });
                    }
                    isInitialChartLoad = false;
                }, 50);
            }
        }
    } catch (e) {
        console.error("Chart load failed", e);
    }
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
    // 只在 Crypto 模式下才處理即時信號
    if (currentMarket !== 'crypto') return;

    // data 應該是 array of {symbol: 'BTC/USDT', signals: { '1d': {...}, '4h': {...} }}
    if (!Array.isArray(serverData)) return;
    lastServerData = serverData;

    serverData.forEach(item => {
        const symbol = item.symbol;
        const sigs = item.signals;

        // 更新主畫面的大卡片
        if (symbol === currentSymbol && sigs['1d']) {
            ui.btc.price.textContent = `$${sigs['1d'].price.toLocaleString()}`;
            ui.status.textContent = `最後更新: ${sigs['1d'].timestamp}`;

            // 1D Update
            const d1 = sigs['1d'];
            updateRing(ui.btc.d1.ring, ui.btc.d1.score, d1.confidence, d1.direction);
            updateDirection(ui.btc.d1.dir, d1.direction, d1.level, d1.confidence);
            ui.btc.d1.buy.textContent = d1.buy_score;
            ui.btc.d1.sell.textContent = d1.sell_score;

            // --- 更新全屏模式 Header 精簡面板 ---
            const hPrice = document.getElementById('h-price-btc');
            const hS1d = document.getElementById('h-score-1d');
            if (hPrice) hPrice.textContent = `$${parseFloat(d1.price).toLocaleString()}`;
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
            ui.btcm.price.textContent = `$${sigs['1d'].price.toLocaleString()}`;
            ui.btcm.dir1d.textContent = lbl.brief;
            ui.btcm.score1d.textContent = sigs['1d'].confidence;
        }
        if (symbol === 'ETH/USDT' && sigs['1d']) {
            const lbl = getSignalLabel(sigs['1d'].direction, sigs['1d'].confidence);
            ui.eth.price.textContent = `$${sigs['1d'].price.toLocaleString()}`;
            ui.eth.dir1d.textContent = lbl.brief;
            ui.eth.score1d.textContent = sigs['1d'].confidence;
        }
        if (symbol === 'SOL/USDT' && sigs['1d']) {
            const lbl = getSignalLabel(sigs['1d'].direction, sigs['1d'].confidence);
            ui.sol.price.textContent = `$${sigs['1d'].price.toLocaleString()}`;
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
        if (payload.type === 'init' || payload.type === 'update') {
            processData(payload.data);
        }
    } catch (e) {
        console.error("Data parse error", e);
    }
};

ws.onopen = () => {
    ui.status.textContent = '連線成功，等待數據推播...';
    if (ui.connStatus) ui.connStatus.textContent = '✅ 系統正常 (Online)';
};

ws.onclose = () => {
    ui.status.textContent = '連線中斷，請稍後。';
    if (ui.connStatus) {
        ui.connStatus.textContent = '❌ 連線中斷 (Offline)';
        ui.connStatus.style.color = '#ef4444';
    }
    if (ui.statusDot) ui.statusDot.style.animation = 'none';
};

// 頁面載入完成
document.addEventListener('DOMContentLoaded', () => {
    initChart();

    // --- 市場選擇 (虛擬幣 / 台股) ---
    const marketSelect = document.getElementById('market-select');
    const stockInput = document.getElementById('stock-symbol-input');
    const stockLoadBtn = document.getElementById('stock-load-btn');

    const loadStock = () => {
        if (!stockInput) return;
        let raw = stockInput.value.trim();
        if (!raw) return;

        // 如果在名稱對照表有，就用對照表的代碼
        let symbol;
        if (stockNameSearchMapping[raw]) {
            symbol = stockNameSearchMapping[raw];
        } else {
            // 否則視為純代碼，補上 .TW
            symbol = raw.includes('.') ? raw : `${raw}.TW`;
        }
        window.changeSymbol(symbol, 'stock');
    };

    const futuresInput = document.getElementById('futures-symbol-input');
    const futuresLoadBtn = document.getElementById('futures-load-btn');

    if (marketSelect) {
        marketSelect.addEventListener('change', (e) => {
            const m = e.target.value;
            if (m === 'crypto') {
                window.changeSymbol('BTC/USDT', 'crypto');
            } else if (m === 'futures') {
                if (futuresInput) futuresInput.value = 'TX';
                window.changeSymbol('TX', 'futures');
            } else {
                // default to 台股 2330
                const defaultStock = '2330.TW';
                if (stockInput) stockInput.value = '2330';
                window.changeSymbol(defaultStock, 'stock');
            }
        });
    }

    const loadFutures = () => {
        if (!futuresInput) return;
        const raw = futuresInput.value.trim().toUpperCase();
        if (!raw) return;
        window.changeSymbol(raw, 'futures');
    };

    if (futuresLoadBtn) futuresLoadBtn.addEventListener('click', loadFutures);
    if (futuresInput) {
        futuresInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loadFutures();
        });
    }

    if (stockLoadBtn) {
        stockLoadBtn.addEventListener('click', loadStock);
    }

    if (stockInput) {
        stockInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loadStock();
        });
    }

    // --- 虛擬幣手動載入邏輯 ---
    const cryptoInput = document.getElementById('crypto-symbol-input');
    const cryptoLoadBtn = document.getElementById('crypto-load-btn');

    const loadCrypto = () => {
        if (!cryptoInput) return;
        let raw = cryptoInput.value.trim().toUpperCase();
        if (!raw) return;

        // 檢查是否有中文對照，若無則補上 /USDT
        let symbol = cryptoNames[raw] || (raw.includes('/') ? raw : `${raw}/USDT`);
        window.changeSymbol(symbol, 'crypto');
    };

    if (cryptoLoadBtn) cryptoLoadBtn.addEventListener('click', loadCrypto);
    if (cryptoInput) {
        cryptoInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loadCrypto();
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
