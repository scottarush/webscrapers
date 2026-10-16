"""Write hand-truncated fixtures (<=4KB, <=5 items) for offline tests."""

from pathlib import Path

FX = Path(__file__).resolve().parent.parent / "fixtures"
FX.mkdir(exist_ok=True)

BOX = """<li>
 <div class="articlebox-compact">
  <div class="col-4 thumb-photo-wrapper"><div class="thumb-photo"><div class="cropper">
   <a href="/realtimenews/{id}-260407">{img}</a>
  </div></div></div>
  <div class="col">
   <h3 class="title"><a href="/realtimenews/{id}-260407">{title}</a></h3>
   <div class="meta-info">
    <time datetime="{date}"><span class="hour">12:00</span></time>
    <div class="category"><a href="/realtimenews/260407">政治</a></div>
   </div>
   <p class="intro">{summary}</p>
  </div>
 </div>
</li>"""


def page(boxes):
    return (
        "<html><head><meta charset='utf-8'></head><body><ul class='vertical-list'>"
        + "".join(boxes)
        + "</ul></body></html>"
    )


items = [
    {
        "id": "20261016000001",
        "title": "測試標題一",
        "date": "2026-10-16 12:00",
        "img": "<img src='https://images.chinatimes.com/a.jpg' class='photo'>",
        "summary": "第一段摘要。",
    },
    {
        "id": "20261016000002",
        "title": "測試標題二\x07",
        "date": "2026-10-16 11:30",
        "img": "<img data-src='https://images.chinatimes.com/b.jpg' class='photo'>",
        "summary": "第二段摘要。",
    },
    {
        "id": "20261016000002",
        "title": "重複標題",
        "date": "2026-10-16 11:30",
        "img": "",
        "summary": "重複連結應被去重。",
    },
    {
        "id": "20261016000003",
        "title": "無日期標題",
        "date": "",
        "img": "",
        "summary": "缺日期條目。",
    },
]
(FX / "realtime.html").write_text(page([BOX.format(**i) for i in items]), encoding="utf-8")

# duplicate URL with tracking param on first box, plus a lazy image
track = page(
    [
        BOX.format(**items[0]).replace(
            'href="/realtimenews/20261016000001-260407"',
            'href="/realtimenews/20261016000001-260407?ctrack=mo"',
            1,
        )
    ]
)
(FX / "tracking.html").write_text(track, encoding="utf-8")

# Big5-UA encoded listing (no charset meta) -> exercises explicit transcode
b5 = page([BOX.format(**i) for i in items[:2]]).replace("<meta charset='utf-8'>", "")
(FX / "realtime.big5.html").write_bytes(b5.encode("big5"))

# RSS with CDATA and a missing pubDate
(FX / "realtime.rss.xml").write_text(
    """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>測試</title><link>https://www.chinatimes.com/</link>
<item><title>CDATA 條目 &lt;特殊&gt;</title>
<link>https://www.chinatimes.com/realtimenews/20261016000010-260407</link>
<guid>https://www.chinatimes.com/realtimenews/20261016000010-260407</guid>
<pubDate>Fri, 16 Oct 2026 12:00:00 +0800</pubDate>
<description><![CDATA[含 <b>HTML</b> 的摘要]]></description></item>
<item><title>無日期條目</title>
<link>https://www.chinatimes.com/realtimenews/20261016000011-260407?ctrack=x</link>
<guid>https://www.chinatimes.com/realtimenews/20261016000011-260407</guid>
<description>無 pubDate</description></item>
</channel></rss>""",
    encoding="utf-8",
)

# JSON API fixture
(FX / "realtime.json").write_text(
    """{"data": {"list": [
 {"id": "a1", "title": "JSON 條目一", "url":
  "https://www.chinatimes.com/realtimenews/20261016000020-260407",
  "publish_time": "2026-10-16T12:00:00+08:00", "summary": "摘要一",
  "author": "記者甲", "images": ["https://images.chinatimes.com/j1.jpg"]},
 {"id": "a2", "title": "JSON 條目二", "url":
  "https://www.chinatimes.com/realtimenews/20261016000021-260407",
  "publish_time": "2026-10-16 11:00", "summary": "摘要二", "author": "",
  "images": "/imgs/j2.jpg"}
]}}""",
    encoding="utf-8",
)

for p in sorted(FX.iterdir()):
    print(p.name, p.stat().st_size, "bytes")
