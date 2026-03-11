with open("frontend/style.css", "r") as f:
    orig = f.read()
import re
new_css = re.sub(r'/\* 終極 Grid 溢出修復.*width: 100%;\n}', '', orig, flags=re.DOTALL)

new_code = """
/* 終極 Grid 溢出修復 */
.chart-container, .signal-card {
    min-width: 0;
}
"""

with open("frontend/style.css", "w") as f:
    f.write(new_css + new_code)
