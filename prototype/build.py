#!/usr/bin/env python3
"""把原型源码与本地依赖打包成单文件 index.html（无外部资源依赖）。

解决预览面板不加载同目录 assets/ 时只剩顶部导航的问题：
CSS / 图标字体 / ECharts / 样例数据 / 业务脚本全部内联。
"""
from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SRC = ROOT / "src"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def inline_fonts(css: str) -> str:
    font = ASSETS / "fonts" / "tabler-icons.woff2"
    b64 = base64.b64encode(font.read_bytes()).decode("ascii")
    uri = f"data:font/woff2;base64,{b64}"
    # 把 woff / ttf / woff2 三种引用统一替换为内联 woff2
    out = css
    for token in [
        'url("./fonts/tabler-icons.woff2?v3.31.0") format("woff2")',
        'url("./fonts/tabler-icons.woff?") format("woff")',
        'url("./fonts/tabler-icons.ttf?v3.31.0") format("truetype")',
        'url("./fonts/tabler-icons.woff2") format("woff2")',
        'url("./fonts/tabler-icons.woff") format("woff")',
        'url("./fonts/tabler-icons.ttf") format("truetype")',
    ]:
        out = out.replace(token, f'url("{uri}") format("woff2")')
    return out


def main() -> None:
    html = read(SRC / "template.html")
    replacements = {
        "/*__TABLER_CSS__*/": read(ASSETS / "tabler.min.css"),
        "/*__ICONS_CSS__*/": inline_fonts(read(ASSETS / "tabler-icons.min.css")),
        "/*__APP_CSS__*/": read(SRC / "app.css"),
        "/*__ECHARTS_JS__*/": read(ASSETS / "echarts.min.js"),
        "/*__DATA_JS__*/": read(ASSETS / "data.js"),
        "/*__APP_JS__*/": read(SRC / "app.js"),
    }
    for token, content in replacements.items():
        if token not in html:
            raise SystemExit(f"模板缺少占位符: {token}")
        html = html.replace(token, content)

    # 安全检查：避免外部资源字符串提前闭合标签
    if "</script" in replacements["/*__APP_JS__*/"].lower():
        raise SystemExit("app.js 含 </script>，无法安全内联")
    if "</style" in replacements["/*__APP_CSS__*/"].lower():
        raise SystemExit("app.css 含 </style>，无法安全内联")

    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out} ({out.stat().st_size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
