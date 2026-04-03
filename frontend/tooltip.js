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
        var r = el.getBoundingClientRect();
        // Position above the element by default
        var top = r.top - box.offsetHeight - 8;
        var left = r.left + r.width / 2 - 120; // 120 = half of 240px width
        // If above would go off-screen, show below
        if (top < 4) top = r.bottom + 8;
        // Clamp horizontal
        if (left < 4) left = 4;
        if (left + 240 > window.innerWidth - 4) left = window.innerWidth - 244;
        box.style.top = top + 'px';
        box.style.left = left + 'px';
        box.classList.add('visible');
    });

    document.addEventListener('mouseout', function (e) {
        var el = e.target.closest('.info-tooltip[data-tip]');
        if (!el) return;
        box.classList.remove('visible');
    });
})();
