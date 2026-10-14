"""Static HTML source checks only; no HTTP server, browser or device execution."""
from html.parser import HTMLParser
from pathlib import Path

class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.text=[]
    def handle_starttag(self,tag,attrs):
        if tag=='a':self.links.append(dict(attrs).get('href'))
    def handle_data(self,data):self.text.append(data)


def test_console_core_and_client_sources_are_separate():
    source=(Path(__file__).resolve().parents[1]/'app/ui/index.html').read_text()
    page=Links();page.feed(source);text=' '.join(page.text)
    prefix='https://github.com/7onytang/LastCore/releases/'
    assert 'CORE / v0.1.1' in text and 'ALPHA / 0.1' not in text
    assert not any('v0.1.1-alpha.1' in link for link in page.links if link)
    assert prefix+'tag/v0.1.1' in page.links
    assert prefix+'download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.zip' in page.links
    assert prefix+'download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.zip' in page.links
    assert prefix+'download/v0.1.1/LastCore-Android-0.1.0-draft-arm64-dev-1760373.apk' in page.links
    assert '未签名' in text and 'relay 未验' in text and 'GLIBC ≥ 2.35' in text
    assert 'Android Debug 开发签名' in text and '真机验收未完成' in text
    assert '未 Apple 公证' in text and '状态锁移植尚未合入' not in text
