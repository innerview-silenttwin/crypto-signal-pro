// ════════════════════════════════════════
// 跨頁標的同步（接收端）共用模組
//
// 寫入端：任一頁查詢/切換成功後寫 localStorage csp-last-symbol / csp-last-market
// （index.html 的 changeSymbol、sector_trading.html 的 doSearch）。
// 本模組供其他頁監聽「外部變化」並回呼，雙保險：
//   - storage event：別的 tab 寫入時即時 fire；背景分頁略過（省 API），切回時補位
//   - visibilitychange：同 tab 切回 / Safari hidden tab 節流時補位
// 以「載入時的值」為基準只對變化反應，避免把舊紀錄當成外部變化誤觸發。
// 寫回相同值不會再 fire storage event，無跨頁迴圈；
// 「已是當前標的就別重查」由 caller 自行把關（各頁的 current 定義不同）。
// ════════════════════════════════════════
window.cspSymbolSync = {
    /** @param {(sym: string, market: string) => void} onChange 偵測到外部標的變化時回呼 */
    watch(onChange) {
        let lastSeen = '';
        try { lastSeen = localStorage.getItem('csp-last-symbol') || ''; } catch (_) { }
        const emit = (sym) => {
            if (!sym || sym === lastSeen) return;
            lastSeen = sym;
            let market = 'stock';
            try { market = localStorage.getItem('csp-last-market') || 'stock'; } catch (_) { }
            onChange(sym, market);
        };
        window.addEventListener('storage', (e) => {
            if (e.key === 'csp-last-symbol' && !document.hidden) emit(e.newValue);
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) return;
            try { emit(localStorage.getItem('csp-last-symbol')); } catch (_) { }
        });
    },
};
