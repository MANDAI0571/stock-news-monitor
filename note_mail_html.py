"""note下書きをメールで「ワンクリックでコピー」＋「note風プレビュー」にするための描画部品。

T-P(2026-08-11): 依頼「note下書きはメールでHTMLでワンクリックでコピーできるようにして、
それで実際のnoteに出るプレビューもメールで出るようにして」への対応。

実測にもとづく制約と、その回避:
- Gmailはメール本文中の <script> と onclick を必ず落とす。
  → 本文だけでは「ボタンを押してコピー」は成立しない。
    実際のコピーボタンは添付の note_copy_pack.html（ブラウザで開けばJSが動く）に置く。
    本文には、そのまま長押し全選択できるコピー用テキスト枠を置く。
- Gmailは約102KBで本文を打ち切る（Message clipped）。
  → 本文には予算（MAIL_HTML_BUDGET_BYTES）を設ける。全文は必ず添付側に入る。
- 途中で切れたテキストをnoteに貼ると記事が欠ける。
  → 本文のコピー枠は「全文が収まるときだけ」出す。収まらない本は添付へ誘導する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
import re


# メール本文HTMLの予算（バイト）。Gmailの102KB打ち切りに対する安全側の値。
MAIL_HTML_BUDGET_BYTES = 78_000
# メール本文にコピー用テキストを「全文」載せられる上限（文字）。超える本は添付へ。
MAIL_COPY_BLOCK_MAX_CHARS = 4_000
# メール本文のnote風プレビューに使う1本あたりの上限（文字）。
MAIL_PREVIEW_MAX_CHARS = 2_000
# T-P(2026-08-13): コピー用プレーンメール1通あたりの本文予算（バイト）。
#   Gmailは約102KBで本文を打ち切る。切られた本文を貼ると記事が欠けるので、
#   余裕をもって切り、超える本は 1/2、2/2 のように分けて送る。
COPY_MAIL_BUDGET_BYTES = 90_000

# T-Q(2026-08-15): iPhoneのSafariは添付HTMLからクリップボードAPIを使えないため、
#   同じ内容を GitHub Pages に公開し、そのURLをメール先頭に置く。
COPY_PAGE_URL = "https://mandai0571.github.io/stock-news-monitor/copy/latest.html"

NOTE_ARTICLES = (
    ("claude", "300万円 Claude"),
    ("pullback", "25MA/押し目・200MA/240MA"),
    ("highs", "52週新高値"),
)

_BODY_FONT = (
    "-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',"
    "'Noto Sans JP',sans-serif"
)
_MONO_FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# note本文の見た目に寄せたCSS。Gmailはヘッダの<style>を解釈する。
# 落とされた場合でも素のHTMLとして読めるよう、装飾だけをCSSに置いている。
NOTE_CSS = (
    "body{margin:0;padding:0;background:#eef1f4;}"
    ".wrap{max-width:840px;margin:0 auto;padding:16px 14px 40px;background:#fff;"
    "color:#111;font-family:" + _BODY_FONT + ";line-height:1.8;}"
    ".nt h1{font-size:23px;line-height:1.45;font-weight:700;margin:18px 0 12px;}"
    ".nt h2{font-size:18px;line-height:1.5;font-weight:700;margin:26px 0 10px;"
    "padding-left:10px;border-left:4px solid #1f745f;}"
    ".nt h3{font-size:16px;line-height:1.5;font-weight:700;margin:18px 0 8px;}"
    ".nt p{font-size:15px;line-height:1.85;margin:10px 0;}"
    ".nt ul{margin:10px 0;padding-left:22px;}"
    ".nt li{font-size:15px;line-height:1.85;}"
    ".nt a{color:#1f745f;}"
    # スマホで横スクロールが出ないよう、長いURLは途中で折り返す。
    ".nt p,.nt li,.nt a,.lnk{word-break:break-word;overflow-wrap:anywhere;}"
    ".nt blockquote{margin:14px 0;padding:8px 14px;border-left:4px solid #d4dbe1;color:#4a5560;}"
    ".nt hr{border:0;border-top:1px solid #e3e8ec;margin:22px 0;}"
    ".nt .scroll{overflow-x:auto;margin:12px 0;}"
    ".nt table{border-collapse:collapse;width:100%;}"
    ".nt th,.nt td{border:1px solid #dfe5ea;padding:6px 8px;font-size:13px;line-height:1.6;"
    "white-space:nowrap;text-align:left;}"
    ".nt th{background:#eef3f1;}"
    ".nt .img{margin:14px 0;padding:10px 12px;border:1px dashed #c8d2da;border-radius:6px;"
    "background:#f7f9fa;color:#5b6b78;font-size:13px;}"
    ".nt .cut{font-size:13px;color:#8a949c;}"
    # T-P(2026-08-21): 買い候補は「コード=青・銘柄名=黒の太字」。
    #   noteの本文はHTMLを受け付けないので、色が付くのはメールとコピー用ページだけ。
    ".nt .cd{color:#1d4ed8;font-weight:700;}"
    ".nt .nm{color:#111111;font-weight:700;}"
    # fix31(2026-08-23): 配当5%以上の銘柄名。
    #   コピー用ページ（Safari等）では点滅する。Gmailはアニメーションを無視するので、
    #   その場合でも黄色い下地とこげ茶の文字で目立つようにしてある。
    ".nt .hy{background:#fff3cd;color:#8a4b00;border-radius:4px;padding:0 4px;animation:hyblink 1.1s ease-in-out infinite;}"
    "@keyframes hyblink{0%,100%{opacity:1;}50%{opacity:0.35;}}"
    "@media (prefers-reduced-motion: reduce){.nt .hy{animation:none;}}"
    ".card{border:1px solid #e3e8ec;border-radius:8px;padding:12px 14px;margin:0 0 16px;}"
    ".meta{font-size:12px;color:#8a949c;margin:0 0 4px;}"
    ".ttl{font-size:15px;font-weight:700;margin:0 0 8px;}"
    ".lnk{font-size:12px;margin:0 0 8px;word-break:break-all;}"
    ".ng{color:#b23c17;}"
    ".hint{font-size:12px;color:#5b6b78;margin:12px 0 4px;}"
    ".copy{white-space:pre-wrap;word-break:break-all;font-family:" + _MONO_FONT + ";"
    "font-size:11px;line-height:1.55;background:#fbfcfd;border:1px solid #d4dbe1;"
    "border-radius:6px;padding:8px;margin:0;color:#111;}"
)


@dataclass(frozen=True)
class NotePart:
    """note下書き1本ぶん（分割記事なら1パート）。"""

    key: str
    label: str
    index: int
    total: int
    title: str
    markdown: str
    url: str

    @property
    def part_label(self) -> str:
        if self.total <= 1:
            return self.label
        return f"{self.label}（{self.index}/{self.total}）"

    @property
    def dom_id(self) -> str:
        return f"{self.key}_{self.index}"


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _part_stem(key: str, index: int) -> str:
    return key if index == 1 else f"{key}{index}"


def collect_note_parts(output_dir: Path) -> list[NotePart]:
    """outputs/ にある note_*.md（分割ぶんを含む）を順番どおりに拾う。

    1本目の保存に失敗してURLが無くても、2本目以降を落とさない。
    """
    parts: list[NotePart] = []
    for key, label in NOTE_ARTICLES:
        stems: list[tuple[int, str]] = []
        for index in range(1, 21):
            stem = _part_stem(key, index)
            if not (output_dir / f"note_{stem}.md").exists():
                if index == 1:
                    continue
                break
            stems.append((index, stem))
        total = len(stems)
        if total == 0:
            continue
        for index, stem in stems:
            markdown = _read_optional(output_dir / f"note_{stem}.md")
            if not markdown:
                continue
            title = _read_optional(output_dir / f"note_{stem}_title.txt")
            if not title:
                title = _first_heading(markdown) or label
            parts.append(
                NotePart(
                    key=key,
                    label=label,
                    index=index,
                    total=total,
                    title=title,
                    markdown=markdown,
                    url=_read_optional(output_dir / f"note_draft_url_{stem}.txt"),
                )
            )
    return parts


def _first_heading(markdown: str) -> str:
    for line in markdown.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def safari_url(note_url: str) -> str:
    return f"https://www.google.com/url?q={quote(note_url, safe='')}"


# ---------------------------------------------------------------------------
# note用のタグと見出し画像（T-P 2026-08-21）
#
# 記事ごとの固定タグに、その日の買い候補の銘柄名を最大3つ足す。
# noteのタグは途中に空白を置けないので、銘柄名からは空白と記号を落とす。
# ---------------------------------------------------------------------------

NOTE_TAGS = {
    "claude": ("日本株", "AI投資", "株式投資", "資産運用"),
    "pullback": ("日本株", "押し目買い", "株式投資", "テクニカル分析"),
    "highs": ("日本株", "52週新高値", "株式投資", "テクニカル分析"),
}
DEFAULT_TAGS = ("日本株", "株式投資")
TAG_NAME_LIMIT = 3

# fix26(2026-08-23): 52週新高値の記事は表と見出しでできているので、そこからも銘柄名を拾う。
# 見出しの形: ### 1. 東北電力（9506）　対象営業日に52週新高値を更新
HEADING_STOCK_RE = re.compile(r"^(\d+)\.\s*(\S.*?)（(\d{4}[0-9A-Z]?)）(.*)$")
# 表の行の形: | 9506 | 東北電力 | 1330.5 | …
TABLE_STOCK_ROW_RE = re.compile(r"^\|\s*(\d{4}[0-9A-Z]?)\s*\|\s*([^|]+?)\s*\|")
# 「1,330.5」「+1.4%」のような数字だけのマスは銘柄名ではない。
_NUMERIC_CELL_RE = re.compile(r"^[\d.,\-+%]+$")

# 買い候補TOP10の1行目: **1. 🟢 Sランク　7453　良品計画**
TOP10_LINE_RE = re.compile(
    r"^\*\*(\d+)\.\s(\S+)\s(\S*?)ランク\u3000(\S+)\u3000(.+?)\*\*$"
)
# 銘柄カードの見出し行: 7453 良品計画 ⚡出来高1.20倍
# fix31(2026-08-23): 見出しの末尾には ⚡出来高 と 💰高配当 が付きうる。
CARD_HEAD_RE = re.compile(r"^(\d{4}[0-9A-Z]?)\s(\S.*?)(\s[⚡💰].*)?$")
# タグに使えない文字（空白・全角空白・記号）
_TAG_DROP_RE = re.compile(r"[\s\u3000・（）()。、,，/／＆&「」【】\[\]]+")
_CHART_MARKER_RE = re.compile(r"<!--\s*chart_image:\s*(.+?)\s*-->")


def _stock_line_names(line: str) -> str:
    """1行から銘柄名を取り出す。買い候補の行でなければ空文字。"""
    match = TOP10_LINE_RE.match(line)
    if match:
        return match.group(5).strip()
    match = CARD_HEAD_RE.match(line)
    if match:
        return match.group(2).strip()
    return ""


def _fallback_line_name(line: str) -> str:
    """表の行や「### 1. 銘柄名（コード）」の見出しから銘柄名を取り出す。"""
    match = HEADING_STOCK_RE.match(line.lstrip("#").strip())
    if match:
        return match.group(2).strip()
    match = TABLE_STOCK_ROW_RE.match(line)
    if match:
        name = match.group(2).strip()
        if name and not _NUMERIC_CELL_RE.match(name):
            return name
    return ""


def _collect_names(markdown: str, picker, limit: int) -> list[str]:
    """1行ずつ picker にかけて銘柄名を上から集める（重複は除く）。"""
    names: list[str] = []
    for raw in markdown.split("\n"):
        name = picker(raw.strip())
        if not name:
            continue
        token = _TAG_DROP_RE.sub("", name).strip()
        if token and token not in names:
            names.append(token)
        if len(names) >= limit:
            break
    return names


def stock_names_in(markdown: str, limit: int = TAG_NAME_LIMIT) -> list[str]:
    """本文から買い候補の銘柄名を上から拾う（重複は除く）。

    fix26(2026-08-23): まずは今までどおりカード形式（TOP10・銘柄カード）で拾う。
    そこで1つも拾えなかったときだけ、表の行と「### N. 銘柄名（コード）」の
    見出しから拾う。52週新高値の記事はこの形しか持っていないため、
    fix24のままではタグに銘柄名が1つも入らなかった。
    すでに拾えている記事の結果は変わらない。
    """
    names = _collect_names(markdown, _stock_line_names, limit)
    if names:
        return names
    return _collect_names(markdown, _fallback_line_name, limit)


def build_tags(part: "NotePart") -> str:
    """その記事に貼るタグ1行（例: #日本株 #52週新高値 #株式投資 ...）。"""
    tokens: list[str] = []
    for token in list(NOTE_TAGS.get(part.key, DEFAULT_TAGS)) + stock_names_in(part.markdown):
        if token and token not in tokens:
            tokens.append(token)
    return " ".join("#" + token for token in tokens)


def part_image_rel(part: "NotePart") -> str:
    """本文の <!-- chart_image: ... --> から見出し画像の場所を読む。"""
    match = _CHART_MARKER_RE.search(part.markdown)
    return match.group(1).strip() if match else ""


def article_images(parts: list["NotePart"]) -> dict[str, str]:
    """記事キーごとの見出し画像。分割記事は1本目の画像を全パートで使う。"""
    found: dict[str, str] = {}
    for part in parts:
        rel = part_image_rel(part)
        if rel and part.key not in found:
            found[part.key] = rel
    return found


# ---------------------------------------------------------------------------
# Markdown → note風HTML
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def render_inline(text: str) -> str:
    out = escape(text)
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)

    def link(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        if not href.lower().startswith(("http://", "https://", "mailto:")):
            return label
        return f'<a href="{href}">{label}</a>'

    return _LINK_RE.sub(link, out)


def render_stock_line(text: str) -> str:
    """買い候補の行なら、コードを青・銘柄名を黒太字にして描く。

    T-P(2026-08-21): 高重さんの指示「買い候補の銘柄と銘柄コードは色をつけて」。
    対象は買い候補TOP10の行と、銘柄カードの見出し行。
    当てはまらない行は今までどおり普通に描く（誤爆させない）。
    """
    line = text.strip()
    match = TOP10_LINE_RE.match(line)
    if match:
        order, mark, rank, code, name = match.groups()
        return (
            f"<strong>{escape(order)}. {escape(mark)} {escape(rank)}ランク\u3000"
            f'<span class="cd">{escape(code)}</span>\u3000'
            f'<span class="nm">{escape(name)}</span></strong>'
        )
    match = CARD_HEAD_RE.match(line)
    if match:
        code, name, tail = match.group(1), match.group(2), match.group(3) or ""
        # fix31(2026-08-23): 配当5%以上の印が付いていたら、銘柄名を目立たせる。
        name_class = "nm hy" if "💰" in tail else "nm"
        return (
            f'<span class="cd">{escape(code)}</span> '
            f'<span class="{name_class}">{escape(name)}</span>{escape(tail)}'
        )
    return render_inline(text)


def render_heading_line(text: str) -> str:
    """見出しが「1. 銘柄名（コード）…」なら、コードを青・銘柄名を黒の太字にする。

    fix26(2026-08-23): 52週新高値の 2/4 はこの見出しだけでできていて、
    色が1か所も付いていなかった。
    当てはまらない見出しは今までどおり普通に描く（誤爆させない）。
    """
    match = HEADING_STOCK_RE.match(text.strip())
    if match:
        order, name, code, tail = match.groups()
        return (
            f"{escape(order)}. "
            f'<span class="nm">{escape(name)}</span>'
            f'（<span class="cd">{escape(code)}</span>）'
            f"{render_inline(tail)}"
        )
    return render_inline(text)


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and line.count("|") >= 2


def _is_table_sep(line: str) -> bool:
    return _is_table_row(line) and set(line.replace("|", "").strip()) <= set("-: ")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_note_html(markdown: str, max_chars: int | None = None) -> str:
    """noteの記事に近い見た目のHTML断片を返す（装飾はNOTE_CSS側）。"""
    truncated = False
    if max_chars is not None and len(markdown) > max_chars:
        markdown = markdown[:max_chars]
        truncated = True

    lines = markdown.split("\n")
    out: list[str] = ['<div class="nt">']
    in_list = False
    i = 0

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i].rstrip()
        if _is_table_row(line) and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            close_list()
            header = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and _is_table_row(lines[i].rstrip()):
                rows.append(_cells(lines[i].rstrip()))
                i += 1
            out.append(_render_table(header, rows))
            continue
        if not line.strip():
            close_list()
            i += 1
            continue
        if line.startswith("# "):
            close_list()
            out.append("<h1>" + render_inline(line[2:]) + "</h1>")
        elif line.startswith("## "):
            close_list()
            out.append("<h2>" + render_inline(line[3:]) + "</h2>")
        elif line.startswith("### "):
            close_list()
            out.append("<h3>" + render_heading_line(line[4:]) + "</h3>")
        elif line.startswith("![") and "](" in line and line.endswith(")"):
            close_list()
            alt = line[2:].split("](", 1)[0]
            out.append(
                '<div class="img">画像：'
                + escape(alt)
                + "（note側に貼り付け済み。メールでは表示しません）</div>"
            )
        elif line.startswith("<!--") and line.endswith("-->"):
            # T-P(2026-08-13): note本文中のHTMLコメント（chart_image など）は
            #   note側では表示されない。プレビューでも文字として出さない。
            close_list()
        elif line.startswith("> "):
            close_list()
            out.append("<blockquote>" + render_inline(line[2:]) + "</blockquote>")
        elif line.strip() in {"---", "***", "___"}:
            close_list()
            out.append("<hr>")
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + render_inline(line[2:]) + "</li>")
        else:
            close_list()
            out.append("<p>" + render_stock_line(line) + "</p>")
        i += 1
    close_list()
    if truncated:
        out.append(
            '<p class="cut">…（ここまで抜粋。全文は添付 note_copy_pack.html と note_*.md にあります）</p>'
        )
    out.append("</div>")
    return "\n".join(out)


# T-P(2026-08-21): 表でも「コード」列は青、「銘柄」列は黒太字にする。
_CODE_COLUMNS = {"コード", "銘柄コード", "code", "ticker"}
_NAME_COLUMNS = {"銘柄", "銘柄名", "name"}


def _column_class(header_text: str) -> str:
    key = header_text.strip().lower()
    if key in {item.lower() for item in _CODE_COLUMNS}:
        return "cd"
    if key in {item.lower() for item in _NAME_COLUMNS}:
        return "nm"
    return ""


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    width = max([len(header)] + [len(row) for row in rows]) if rows else len(header)
    classes = [
        _column_class(header[index]) if index < len(header) else "" for index in range(width)
    ]
    parts = ['<div class="scroll"><table><thead><tr>']
    for index in range(width):
        parts.append("<th>" + render_inline(header[index] if index < len(header) else "") + "</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for index in range(width):
            attr = f' class="{classes[index]}"' if classes[index] else ""
            parts.append(
                f"<td{attr}>" + render_inline(row[index] if index < len(row) else "") + "</td>"
            )
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 添付する「コピー用ページ」（ブラウザで開けばボタン1つでコピーできる）
# ---------------------------------------------------------------------------

_COPY_PACK_SCRIPT = """
document.addEventListener('click', function (event) {
  var button = event.target;
  while (button && button.tagName !== 'BUTTON') { button = button.parentElement; }
  if (!button) { return; }
  var flashLabel = button.getAttribute('data-label') || 'コピー';
  var flash = function (ok, ngText) {
    button.textContent = ok ? 'コピーしました' : ngText;
    setTimeout(function () { button.textContent = flashLabel; }, 2500);
  };
  if (button.className.indexOf('imgbtn') >= 0) {
    var src = button.getAttribute('data-image');
    var ng = '下の画像を長押ししてコピーしてください';
    if (!src) { return; }
    if (!window.ClipboardItem || !navigator.clipboard || !navigator.clipboard.write) {
      flash(false, ng);
      return;
    }
    var blobOf = function () {
      return fetch(src).then(function (res) { return res.blob(); });
    };
    try {
      var item = new ClipboardItem({ 'image/png': blobOf() });
      navigator.clipboard.write([item]).then(
        function () { flash(true); },
        function () { flash(false, ng); }
      );
    } catch (error) {
      blobOf().then(function (blob) {
        return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      }).then(function () { flash(true); }, function () { flash(false, ng); });
    }
    return;
  }
  if (button.className.indexOf('copybtn') < 0) { return; }
  var area = document.getElementById(button.getAttribute('data-target'));
  if (!area) { return; }
  var label = button.getAttribute('data-label') || 'コピー';
  var text = area.value;
  var done = function (ok) {
    button.textContent = ok ? 'コピーしました' : '枠の中を長押しして全選択してください';
    setTimeout(function () { button.textContent = label; }, 2500);
  };
  var legacy = function () {
    var ok = false;
    try {
      area.readOnly = false;
      area.focus();
      area.setSelectionRange(0, text.length);
      ok = document.execCommand('copy');
      area.readOnly = true;
    } catch (error) { ok = false; }
    done(ok);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, legacy);
  } else {
    legacy();
  }
});
"""

_COPY_PACK_CSS = (
    "h1.pk{font-size:22px;margin:8px 0 4px;}"
    "p.lead{font-size:13px;color:#5b6b78;margin:0 0 18px;}"
    "section{border-top:1px solid #e3e8ec;padding-top:18px;margin-top:26px;}"
    "section h2.pk{font-size:18px;margin:0 0 8px;}"
    "details{margin-top:14px;}"
    "summary{font-size:14px;color:#1f745f;cursor:pointer;padding:6px 0;}"
    ".row{margin-bottom:8px;}"
    "button.copybtn{-webkit-appearance:none;appearance:none;border:0;border-radius:8px;"
    "background:#1f745f;color:#fff;font-size:15px;font-weight:700;padding:12px 16px;"
    "margin:0 8px 8px 0;cursor:pointer;}"
    "button.copybtn:active{background:#17594a;}"
    "textarea{width:100%;box-sizing:border-box;height:150px;font-family:" + _MONO_FONT + ";"
    "font-size:12px;line-height:1.6;border:1px solid #d4dbe1;border-radius:6px;padding:8px;"
    "background:#fbfcfd;}"
    "textarea.short{height:56px;}"
    # T-P(2026-08-21): タグ・画像ボタンと、長押し用の見出し画像。
    "button.copybtn.tag{background:#6d4aa8;}"
    "button.copybtn.tag:active{background:#573a87;}"
    "button.imgbtn{-webkit-appearance:none;appearance:none;border:0;border-radius:8px;"
    "background:#2f6f8f;color:#fff;font-size:15px;font-weight:700;padding:12px 16px;"
    "margin:0 8px 8px 0;cursor:pointer;}"
    "button.imgbtn:active{background:#255a75;}"
    "a.dlbtn{display:inline-block;border-radius:8px;background:#2f6f8f;color:#fff;"
    "text-decoration:none;font-size:15px;font-weight:700;padding:12px 16px;"
    "margin:0 8px 8px 0;}"
    "textarea.tags{height:70px;}"
    "img.hdr{display:block;max-width:100%;height:auto;border:1px solid #d4dbe1;"
    "border-radius:6px;margin:10px 0 4px;}"
    "ul.toc{padding-left:20px;font-size:14px;}"
)


def build_copy_pack_html(
    parts: list[NotePart], now: datetime, images: dict[str, str] | None = None
) -> str:
    """note下書きを全文ぶん、ボタン1つでコピーできる単体HTMLにする。

    images: 記事キー -> このHTMLと同じ場所に置いた見出し画像のファイル名。
      渡さないときは画像ボタンを出さない（添付HTMLには画像を同梱できないため）。
    """
    blocks: list[str] = []
    toc: list[str] = []
    for part in parts:
        toc.append(
            f'<li><a href="#{part.dom_id}">{escape(part.part_label)}'
            f"（{len(part.markdown):,}文字）</a></li>"
        )
        if part.url:
            url_line = (
                '<p class="lnk">note下書き: '
                f'<a href="{escape(safari_url(part.url))}">{escape(part.url)}</a></p>'
            )
        else:
            url_line = (
                '<p class="lnk ng">この本はnote側の下書き保存が未完了です。'
                "下のボタンでコピーして、noteの新規記事に貼り付けてください。</p>"
            )
        # T-P(2026-08-21): タイトル・本文に加えて、タグと見出し画像も1タップで
        #   コピー（保存）できるようにする。iPhoneは画像のクリップボードを
        #   弾くことがあるので、保存ボタンと実物の画像も並べて逃げ道を2つ持たせる。
        image_name = (images or {}).get(part.key, "")
        buttons = [
            f'<button type="button" class="copybtn" data-target="title_{part.dom_id}" '
            'data-label="タイトルをコピー">タイトルをコピー</button>',
            f'<button type="button" class="copybtn" data-target="body_{part.dom_id}" '
            'data-label="本文をコピー">本文をコピー</button>',
            f'<button type="button" class="copybtn tag" data-target="tags_{part.dom_id}" '
            'data-label="タグをコピー">タグをコピー</button>',
        ]
        image_block = ""
        if image_name:
            buttons.append(
                f'<button type="button" class="imgbtn" data-image="{escape(image_name)}" '
                'data-label="画像をコピー">画像をコピー</button>'
            )
            buttons.append(
                f'<a class="dlbtn" href="{escape(image_name)}" '
                f'download="{escape(image_name)}">画像を保存</a>'
            )
            image_block = (
                f'<img class="hdr" src="{escape(image_name)}" alt="見出し画像">'
                '<p class="hint">ボタンが効かないときは、この画像を長押しして'
                "「写真に追加」または「コピー」を選んでください。</p>"
            )
        blocks.append(
            f'<section id="{part.dom_id}">'
            f'<h2 class="pk">{escape(part.part_label)}</h2>'
            f"{url_line}"
            '<div class="row">' + "".join(buttons) + "</div>"
            f'<textarea id="title_{part.dom_id}" class="short" readonly>{escape(part.title)}</textarea>'
            f'<textarea id="tags_{part.dom_id}" class="short tags" readonly>'
            f"{escape(build_tags(part))}</textarea>"
            f'<textarea id="body_{part.dom_id}" readonly>{escape(part.markdown)}</textarea>'
            f"{image_block}"
            "<details><summary>noteでの見え方（プレビュー）を開く</summary>"
            f"{render_note_html(part.markdown)}</details>"
            "</section>"
        )

    if not blocks:
        blocks.append('<section><h2 class="pk">note下書きが見つかりませんでした</h2></section>')
        toc.append("<li>なし</li>")

    return (
        '<!doctype html>\n<html lang="ja">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>note下書き コピー用</title>\n"
        "<style>" + NOTE_CSS + _COPY_PACK_CSS + "</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        '<h1 class="pk">note下書き コピー用</h1>\n'
        f'<p class="lead">作成: {now.strftime("%Y-%m-%d %H:%M JST")} ／ '
        "ボタンを押すとクリップボードに入ります。noteの新規記事に貼り付けてください。"
        "タイトル・本文・タグ・見出し画像を、それぞれ1タップでコピーできます。"
        "（ボタンが効かないときは枠の中を長押しして全選択してください）</p>\n"
        '<ul class="toc">\n' + "\n".join(toc) + "\n</ul>\n"
        + "\n".join(blocks)
        + "\n</div>\n<script>"
        + _COPY_PACK_SCRIPT
        + "</script>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# メール本文HTML
# ---------------------------------------------------------------------------


def _copy_block(part: NotePart) -> str:
    """メール本文に置くコピー用テキスト枠。途中で切れる本は出さない（貼ると記事が欠けるため）。"""
    if len(part.markdown) > MAIL_COPY_BLOCK_MAX_CHARS:
        return (
            '<p class="hint ng">本文が長い（'
            f"{len(part.markdown):,}文字）ので、コピー用テキストは添付 note_copy_pack.html "
            "の「本文をコピー」ボタンにまとめています。</p>"
        )
    return (
        '<p class="hint">コピー用テキスト（この枠を長押し→全選択→コピーで、そのままnoteに貼れます）</p>'
        '<pre class="copy">' + escape(part.markdown) + "</pre>"
    )


def build_note_mail_section(parts: list[NotePart]) -> str:
    """メール本文の「note下書き（コピー用＋プレビュー）」ブロック。予算内で詰める。"""
    header = (
        '<div class="nt"><h2>note下書き（コピー用とプレビュー）</h2></div>'
        '<p class="hint"><a href="' + COPY_PAGE_URL + '" '
        'style="display:inline-block;background:#1f745f;color:#ffffff;'
        'text-decoration:none;font-weight:700;font-size:17px;'
        'padding:14px 18px;border-radius:8px;">コピー用ページを開く（iPhone可）</a></p>'
        '<p class="hint">上のリンクを開くと、ボタン1つで本文をコピーできます。'
        "添付の note_copy_pack.html も同じ内容ですが、iPhoneではリンクのほうが確実です。</p>"
    )
    if not parts:
        return header + '<p class="hint ng">note下書きが見つかりませんでした。</p>'

    chunks: list[str] = [header]
    used = len(header.encode("utf-8"))
    for part in parts:
        pieces = [
            '<div class="card">',
            '<p class="meta">' + escape(part.part_label) + f"／{len(part.markdown):,}文字</p>",
            '<p class="ttl">' + escape(part.title) + "</p>",
        ]
        if part.url:
            pieces.append(
                '<p class="lnk"><a href="'
                + escape(safari_url(part.url))
                + '">noteの下書きを開く（Safari経由）</a></p>'
            )
        else:
            pieces.append(
                '<p class="lnk ng">note側の下書き保存が未完了です。'
                "添付 note_copy_pack.html からコピーして貼り付けてください。</p>"
            )
        # T-P(2026-08-21): タグもメールに出す（コピー用ページが開けないとき用）。
        pieces.append('<p class="hint">タグ（この枠を長押し→全選択→コピー）</p>')
        pieces.append('<pre class="copy">' + escape(build_tags(part)) + "</pre>")
        pieces.append(_copy_block(part))
        pieces.append('<p class="hint">noteでの見え方（プレビュー）</p>')
        pieces.append(render_note_html(part.markdown, max_chars=MAIL_PREVIEW_MAX_CHARS))
        pieces.append("</div>")
        block = "".join(pieces)
        size = len(block.encode("utf-8"))
        if used + size > MAIL_HTML_BUDGET_BYTES:
            chunks.append(
                '<p class="hint ng">これ以降（'
                + escape(part.part_label)
                + " 以降）は、メールが長くなりすぎるため省略しました。"
                "添付 note_copy_pack.html に全文が入っています。</p>"
            )
            break
        chunks.append(block)
        used += size
    return "".join(chunks)


def wrap_mail_html(title: str, sections: list[str]) -> str:
    return (
        '<!doctype html>\n<html lang="ja">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        "<style>" + NOTE_CSS + "</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n' + "\n".join(sections) + "\n</div>\n</body>\n</html>\n"
    )


def build_mail_html(subject: str, text_body: str, parts: list[NotePart]) -> str:
    """プレーン本文（Markdown）をHTML化し、note下書きのコピー枠とプレビューを先頭に足す。"""
    note_section = build_note_mail_section(parts)
    remaining = max(MAIL_HTML_BUDGET_BYTES - len(note_section.encode("utf-8")), 8_000)
    rest = render_note_html(_strip_preview_section(text_body), max_chars=remaining)
    return wrap_mail_html(subject, [note_section, rest])


def _strip_preview_section(text_body: str) -> str:
    """プレーン本文の「## 本文プレビュー」以下は、上のプレビューと重複するのでHTML側では省く。"""
    marker = "\n## 本文プレビュー"
    head, sep, tail = text_body.partition(marker)
    if not sep:
        return text_body
    keep_marker = "\n## 添付"
    _, sep2, attachments = tail.partition(keep_marker)
    if sep2:
        return head + keep_marker + attachments
    return head


# ---------------------------------------------------------------------------
# コピー用プレーンメール（iPhoneで確実に貼り付けられる経路）
# ---------------------------------------------------------------------------


def split_copy_text(text: str, budget: int = COPY_MAIL_BUDGET_BYTES) -> list[str]:
    """本文を、1通あたり budget バイト以内の塊に行単位で分ける。

    表の行や段落の途中では切らない（行の切れ目でだけ分ける）ので、
    貼り付けたときに文字が欠けない。
    """
    if budget <= 0 or len(text.encode("utf-8")) <= budget:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        line_bytes = len(line.encode("utf-8")) + 1
        if current and size + line_bytes > budget:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += line_bytes
    if current:
        chunks.append("\n".join(current))
    return chunks


SPLIT_NOTICE_RE = re.compile(
    r"^※\s*スマホでも開けるように、この記事は全\d+本に分割しています。これは\d+本目です。\s*$"
)
PART_SUFFIX_RE = re.compile(r"（\d+/\d+）\s*$")


def _copy_body(part: NotePart) -> str:
    """1本ぶんの本文から、noteに貼るとき邪魔になる行を落とす。

    T-P(2026-08-14): 落とすのは3種類。
      - <!-- chart_image: ... --> などのHTMLコメント行（note側では表示されない）
      - 「※ スマホでも開けるように…全N本に分割しています」の案内行
        （連結して1通にするので、もう嘘になる）
      - 2本目以降の先頭に付いている見出し行（1本目の見出しだけ残す）
    1本目の見出しからは（1/N）を外す。
    """
    out: list[str] = []
    for line in part.markdown.strip().split("\n"):
        stripped = line.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if SPLIT_NOTICE_RE.match(stripped):
            continue
        if not out and stripped.startswith("# "):
            if part.index > 1:
                continue
            out.append(PART_SUFFIX_RE.sub("", line).rstrip())
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def build_copy_mail_items(
    parts: list[NotePart], budget: int = COPY_MAIL_BUDGET_BYTES
) -> list[tuple[str, str, str]]:
    """記事1本につき (件名, 本文, アンカー) を1つ返す。分割ぶんは連結してから1通にする。

    T-P(2026-08-14): 以前は part ごとに1通作っていたので、押し目が6通、
    新高値が4通に散っていた。iPhoneでは1通ぶんずつしかコピーできないので、
    同じ key の part を index 順につないでから1通にする。
    Gmailは約102KBで本文を打ち切るため、そこに収まらない記事だけ
    【コピー用 1/2】のように分ける（普段は1通で収まる）。

    本文は記事テキストだけにする。前置きも免責も付けない。
    iPhoneのGmailで本文を長押し→すべてを選択→コピー、をそのまま
    noteの本文に貼れる状態にするため。
    """
    order: list[str] = []
    grouped: dict[str, list[NotePart]] = {}
    for part in parts:
        if part.key not in grouped:
            grouped[part.key] = []
            order.append(part.key)
        grouped[part.key].append(part)

    mails: list[tuple[str, str, str]] = []
    for key in order:
        group = sorted(grouped[key], key=lambda item: item.index)
        text = "\n\n".join(body for body in (_copy_body(item) for item in group) if body)
        text = text.strip()
        if not text:
            continue
        label = group[0].label
        title = PART_SUFFIX_RE.sub("", group[0].title).strip() or label
        chunks = split_copy_text(text, budget)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            head = "【コピー用】" if total == 1 else f"【コピー用 {index}/{total}】"
            mails.append((f"{head}{label}｜{title}", chunk, group[0].dom_id))
    return mails


def build_copy_mails(
    parts: list[NotePart], budget: int = COPY_MAIL_BUDGET_BYTES
) -> list[tuple[str, str]]:
    """(件名, 本文) だけが要る呼び出し向けの薄い包み。"""
    return [(subject, text) for subject, text, _ in build_copy_mail_items(parts, budget)]


# Gmailは約102KBで本文を打ち切る。HTML版はその内側に収める。
COPY_MAIL_HTML_BUDGET_BYTES = 99_000


def build_copy_mail_html(text: str, anchor: str) -> str:
    """【コピー用】メールのHTML版。先頭にワンクリックコピーのボタンを置く。

    T-P(2026-08-18): 件名に「コピー用」と書いてあるメールにこそボタンが無く、
    「どこにもワンクリックコピペがない」状態になっていた。ここで直す。
    プレーン本文は記事テキストだけのまま（HTMLを読めない環境の逃げ道）。
    """
    link = COPY_PAGE_URL + ("#" + anchor if anchor else "")
    head = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body style=\"margin:0;padding:16px;background:#ffffff;"
        f"font-family:{_BODY_FONT};color:#111111;\">"
        f'<p style="margin:0 0 14px 0"><a href="{escape(link)}" '
        'style="display:block;background:#1f745f;color:#ffffff;text-decoration:none;'
        'font-weight:700;font-size:20px;line-height:1.4;padding:18px 16px;'
        'border-radius:10px;text-align:center;">ここを押す → ワンクリックでコピー</a></p>'
        '<p style="margin:0 0 18px 0;font-size:15px;line-height:1.6;color:#333333;">'
        "上のボタンでコピー用ページが開きます。"
        "「本文をコピー」を1回押すだけで、そのままnoteに貼れます。</p>"
    )
    tail = "</body></html>"
    fallback = (
        '<p style="margin:0 0 6px 0;font-size:13px;color:#666666;">'
        "↓ 予備（ボタンが使えないとき用の同じ本文）</p>"
        '<pre style="white-space:pre-wrap;word-break:break-word;font-size:13px;'
        "line-height:1.6;background:#f6f7f8;border:1px solid #dddddd;"
        'border-radius:8px;padding:12px;margin:0;">' + escape(text) + "</pre>"
    )
    full = head + fallback + tail
    if len(full.encode("utf-8")) <= COPY_MAIL_HTML_BUDGET_BYTES:
        return full
    return (
        head
        + '<p style="margin:0;font-size:13px;color:#666666;">'
        + "本文が長いので、このHTML版では省いています。"
        + "上のボタン、または下のテキスト版をお使いください。</p>"
        + tail
    )
