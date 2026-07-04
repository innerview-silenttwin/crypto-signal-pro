/**
 * Global JS tooltip for .info-tooltip[data-tip] elements.
 * Uses position:fixed on a <body>-level element to escape overflow containers.
 */
(function () {
    var box = document.createElement('div');
    box.className = 'tip-box';
    document.body.appendChild(box);

    document.addEventListener('mouseover', function (e) {
        var el = e.target.closest('.info-tooltip[data-tip]');
        if (!el) return;
        box.textContent = el.getAttribute('data-tip');
        // body 有 zoom 時：gBCR/innerWidth 是視覺座標，但 style.left 會再被 zoom 乘一次
        // → 全部用視覺座標計算、最後除以 z 寫回（z=1 時行為與原本完全相同）
        var z = parseFloat(getComputedStyle(document.body).zoom) || 1;
        var r = el.getBoundingClientRect();
        var boxH = box.offsetHeight * z;   // offsetHeight 是邏輯 px → 轉視覺 px
        var TIP_W = 240 * z;               // .tip-box 寬 240px（邏輯）→ 視覺
        // Position above the element by default
        var top = r.top - boxH - 8;
        var left = r.left + r.width / 2 - TIP_W / 2;
        // If above would go off-screen, show below
        if (top < 4) top = r.bottom + 8;
        // Clamp horizontal
        if (left < 4) left = 4;
        if (left + TIP_W > window.innerWidth - 4) left = window.innerWidth - TIP_W - 4;
        box.style.top = (top / z) + 'px';
        box.style.left = (left / z) + 'px';
        box.classList.add('visible');
    });

    document.addEventListener('mouseout', function (e) {
        var el = e.target.closest('.info-tooltip[data-tip]');
        if (!el) return;
        box.classList.remove('visible');
    });
})();
