#!/usr/bin/env python3
"""Make the demo a single offline HTML with stdlib only; no task/network execution."""
from pathlib import Path
from urllib.parse import quote
root = Path(__file__).resolve().parent
html = (root / 'index.html').read_text()
html = html.replace('href="assets/favicon.svg"', 'href="data:image/svg+xml,' + quote((root / 'assets/favicon.svg').read_text()) + '"')
html = html.replace('<link rel="stylesheet" href="styles.css">', '<style>\n' + (root / 'styles.css').read_text() + '\n</style>')
html = html.replace('<script src="evidence.js" defer></script>', '')
html = html.replace('<script src="app.js" defer></script>', '')
scripts = (root / 'evidence.js').read_text() + '\n' + (root / 'app.js').read_text()
html = html.replace('</body>', '<script>\n' + scripts.replace('</script', '<\\/script') + '\n</script>\n</body>')
(root / 'offline.html').write_text(html)
print('Saved offline.html: self-contained HTML/CSS/JS/SVG; historical data remains visible without network.')
