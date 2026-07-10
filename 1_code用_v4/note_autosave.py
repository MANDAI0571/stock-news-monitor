from __future__ import annotations

import base64
import argparse
import json
import os
import re
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_TITLE_FILE = "note_title.txt"
NOTE_HTML_FILE = "note_daily.html"
NOTE_URL_FILE = "note_draft_url.txt"
NOTE_CLOUD_BODY_FILE = "note_body.md"
NOTE_CLOUD_PREVIEW_FILE = "note_preview.html"
NOTE_CLOUD_URL_FILE = "note_draft_url_cloud.txt"
NOTE_CLOUD_VERIFY_FILE = "note_autosave_verify_cloud.json"
# T-E: 4本Note分割。note_draft.build_note4 が書く manifest を読み、1ログインで
# 4下書きをまとめて保存する（公開は一切しない）。manifest が無ければ従来の単一Note動作。
NOTE4_MANIFEST_FILE = "note_drafts_manifest.json"
NOTE_NEW_URL = "https://note.com/notes/new"
# 現行noteの下書きURLは editor.note.com/notes/<id>/edit/ 形式。
# 旧 note.com/notes/<id> 形式も保険で許容（idが "new" 以外＝保存済み下書き）。
NOTE_DRAFT_URL_RE = re.compile(
    r"^https://(?:editor\.)?note\.com/notes/([A-Za-z0-9_-]+)(?:/edit)?/?$"
)


@dataclass(frozen=True)
class NoteDraftPayload:
    title: str
    body_html: str
    chart_path: str | None = None  # 本文冒頭に挿入する画像(PNG)の絶対パス。無ければNone。
    image_paths: tuple[str, ...] = ()
    verify_texts: tuple[str, ...] = ()
    min_image_count: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="note.com の下書きを自動保存する")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--note-url-file", default=NOTE_URL_FILE)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_false", dest="headless")
    # --single = manifest があっても従来の単一Note(note_daily)だけ保存する保険スイッチ
    parser.add_argument("--single", action="store_true", default=False)
    parser.add_argument(
        "--cloud-article",
        action="store_true",
        default=False,
        help="outputs/note_body.md と画像一式をnote.com下書きへ保存し、保存後に再確認する",
    )
    return parser.parse_args()


def load_note_payload(output_dir: Path) -> NoteDraftPayload:
    title_path = output_dir / NOTE_TITLE_FILE
    html_path = output_dir / NOTE_HTML_FILE
    if not title_path.exists():
        raise FileNotFoundError(f"{title_path} が見つかりません")
    if not html_path.exists():
        raise FileNotFoundError(f"{html_path} が見つかりません")
    title = title_path.read_text(encoding="utf-8").strip()
    html = html_path.read_text(encoding="utf-8")
    body_html = extract_body_fragment(html)
    return NoteDraftPayload(title=title, body_html=body_html)


def load_cloud_note_payload(output_dir: Path) -> NoteDraftPayload:
    body_path = output_dir / NOTE_CLOUD_BODY_FILE
    preview_path = output_dir / NOTE_CLOUD_PREVIEW_FILE
    if not body_path.exists():
        raise FileNotFoundError(f"{body_path} が見つかりません")
    if not preview_path.exists():
        raise FileNotFoundError(f"{preview_path} が見つかりません")
    markdown = body_path.read_text(encoding="utf-8")
    title = _title_from_markdown(markdown)
    image_paths = tuple(_image_paths_from_markdown(output_dir, markdown))
    verify_texts = (
        "本日の300万円運用判断",
        "ウォーレン判断",
        "市場状況",
        "WATCH",
        "免責文",
    )
    if "BUY 0件" in markdown:
        verify_texts = (*verify_texts, "CASH", "なぜBUY0件なのか")
    return NoteDraftPayload(
        title=title,
        body_html=_markdown_to_note_html(markdown),
        image_paths=image_paths,
        verify_texts=verify_texts,
        min_image_count=max(1, len(image_paths)),
    )


def _title_from_markdown(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    raise RuntimeError("note_body.md にタイトル行（# ...）がありません")


def _image_paths_from_markdown(output_dir: Path, markdown: str) -> list[str]:
    paths: list[str] = []
    seen: set[Path] = set()
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        raw = match.group(1).strip()
        if raw.startswith(("http://", "https://", "data:")):
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = output_dir / path
        path = path.resolve()
        if path.exists() and path not in seen:
            paths.append(str(path))
            seen.add(path)
    if not paths:
        raise RuntimeError("note_body.md から挿入対象画像を特定できません")
    return paths


def _markdown_to_note_html(markdown: str) -> str:
    """note本文へ貼り付けるHTML。画像は別途アップロードするため本文からは除く。"""
    parts: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("![") and "](" in line:
            continue
        if in_list and not line.startswith("- "):
            parts.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("# "):
            parts.append(f"<h1>{_inline_markdown(line[2:])}</h1>")
        elif line.startswith("## "):
            parts.append(f"<h2>{_inline_markdown(line[3:])}</h2>")
        elif line.startswith("### "):
            parts.append(f"<h3>{_inline_markdown(line[4:])}</h3>")
        elif line.startswith("- "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{_inline_markdown(line[2:])}</li>")
        else:
            parts.append(f"<p>{_inline_markdown(line)}</p>")
    if in_list:
        parts.append("</ul>")
    return "\n".join(parts)


def _inline_markdown(text: str) -> str:
    escaped = escape(text.strip())
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def extract_body_fragment(html: str) -> str:
    match = re.search(r"<body[^>]*>(.*)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return html.strip()


def load_credentials() -> tuple[str, str]:
    email = os.environ.get("NOTE_EMAIL", "").strip()
    password = os.environ.get("NOTE_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError("NOTE_EMAIL / NOTE_PASSWORD が不足しています")
    return email, password


def load_storage_state() -> dict | None:
    encoded = os.environ.get("NOTE_STORAGE_STATE", "").strip()
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded).decode("utf-8")
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("NOTE_STORAGE_STATE の復号に失敗しました") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("NOTE_STORAGE_STATE の内容が不正です")
    return payload


def _require_auth() -> None:
    storage_state = load_storage_state()
    credentials_available = bool(os.environ.get("NOTE_EMAIL", "").strip() and os.environ.get("NOTE_PASSWORD", "").strip())
    if storage_state is None and not credentials_available:
        raise RuntimeError("NOTE_STORAGE_STATE または NOTE_EMAIL / NOTE_PASSWORD が不足しています")


def _open_context(playwright, headless: bool):
    """1ログイン分のブラウザ/コンテキストを用意する。複数下書きで使い回して
    ログインを1回に抑える（storage_state があれば自動ログイン）。"""
    storage_state = load_storage_state()
    browser = playwright.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context_kwargs = {
        "viewport": {"width": 1440, "height": 1800},
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "extra_http_headers": {"Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
    }
    if storage_state is not None:
        context_kwargs["storage_state"] = storage_state
    context = browser.new_context(**context_kwargs)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://note.com")
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://editor.note.com")
    return browser, context


def _save_one(context, payload: NoteDraftPayload, error_key: str | None = None) -> tuple[str, str]:
    """与えられたコンテキスト上に新規ページを開いて1本だけ下書き保存する。
    公開は一切しない（下書き保存ボタンのみ）。失敗時は error_key 付きでスクショを残す。
    戻り値は (draft_url, image_status)。image_status は none/ok/failed のいずれか。
    画像挿入は本文保存をブロックしない（失敗してもwarningを出して本文保存は続行）。"""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = context.new_page()
    image_status = "none"
    try:
        page.goto(NOTE_NEW_URL, wait_until="domcontentloaded", timeout=60_000)
        if "login" in page.url:
            email, password = load_credentials()
            _login(page, email, password)
            page.goto(NOTE_NEW_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            _wait_for_editor_ready(page)
            _fill_title(page, payload.title)
            _fill_body(page, payload.body_html)
            # 本文を入れた後で、本文の冒頭(タイトル直下)に画像を挿入する。
            # set_all+paste が画像も消すため、必ず本文確定の「後」に行う。失敗は致命にしない。
            image_paths = list(payload.image_paths)
            if payload.chart_path:
                image_paths.append(payload.chart_path)
            if image_paths:
                image_ok = 0
                for idx, image_path in enumerate(reversed(image_paths), start=1):
                    key = f"{error_key or 'note'}_{idx}"
                    if _insert_image_top(page, image_path, key) == "ok":
                        image_ok += 1
                if image_ok == len(image_paths):
                    image_status = "ok"
                elif image_ok > 0:
                    image_status = "partial"
                else:
                    image_status = "failed"
                print(f"note_draft_image_uploaded_detail[{error_key}]={image_ok}/{len(image_paths)}")
            _try_save(page)
            try:
                page.wait_for_url(
                    re.compile(r"https://(?:editor\.)?note\.com/notes/(?!new)[A-Za-z0-9_-]+"),
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2000)
            draft_url = page.url
            if not is_saved_draft_url(draft_url):
                raise RuntimeError(f"note draft URL を取得できませんでした: {draft_url}")
        except Exception:
            _save_error_debug(page, error_key)
            raise
        return draft_url, image_status
    finally:
        page.close()


def _insert_image_top(page, image_path: str, key: str | None = None) -> str:
    """本文の先頭に画像(PNG)をアップロードして挿入する。best-effort。
    note.com の画像挿入はネイティブのファイル選択ダイアログを開くので、Playwright の
    expect_file_chooser で確実に捕まえるのが最有効。掴めない時は input[type=file] への
    直接set もフォールバックで試す。どれも失敗したら warning＋デバッグ用スクショを残して
    'failed' を返す（例外は投げない＝本文保存は必ず続行する）。成功で 'ok'。
    各段階でログを出すので、Mac実行時にどこで止まったかが分かる。"""
    path = Path(image_path)
    if not path.exists():
        print(f"note_image_warning[{key}]=画像ファイルが見つかりません: {path}")
        return "failed"
    try:
        print(f"note_image_start[{key}]={path.name}")
        # 1) 本文エディタにフォーカスし、先頭に空行を作ってそこへカーソルを置く
        #    （note.com の挿入「＋」は空段落の左に現れるため）
        editor = _find_body_editor(page)
        if editor is not None:
            try:
                editor.click()
            except Exception:
                pass
        _move_cursor_to_top(page)
        _ensure_empty_top_line(page)

        # 2) ＋メニュー→「画像をアップロード」の隠し input[type=file] へ直接セット（主軸）
        #    note.com は項目クリックでネイティブダイアログを開かず input にファイルを渡すため。
        if _insert_image_via_menu(page, str(path), key):
            print(f"note_image_ok[{key}]={path.name}")
            return "ok"

        print(f"note_image_warning[{key}]=画像アップロードUIを特定できませんでした（本文保存は続行）")
        _image_debug_screenshot(page, key)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - 画像で本文保存を止めない
        print(f"note_image_warning[{key}]=画像挿入で例外（本文保存は続行）: {exc}")
        _image_debug_screenshot(page, key)
        return "failed"


def _move_cursor_to_top(page) -> None:
    """本文エディタ内でドキュメント先頭へカーソルを移動。"""
    try:
        if os.name == "posix":
            page.keyboard.press("Meta+ArrowUp")
        else:
            page.keyboard.press("Control+Home")
        page.keyboard.press("Home")
    except Exception:
        pass


def _ensure_empty_top_line(page) -> None:
    """本文の先頭に空段落を1つ作り、そこへカーソルを戻す。
    note.com の挿入「＋」ボタンは空段落に対して表示されるため。"""
    try:
        page.keyboard.press("Enter")
        _move_cursor_to_top(page)
        page.wait_for_timeout(300)
    except Exception:
        pass


def _insert_image_via_menu(page, file_path: str, key: str | None = None) -> bool:
    """note.com の挿入メニュー(＋)→「画像をアップロード」で画像を挿入する。
    スクショ調査の結果、項目クリックはネイティブのファイルダイアログを開かず、
    ボタン内の隠し input[type=file] にファイルを渡す方式だった。よって input への
    直接 set_input_files を主軸にする。成功＝本文内に <img> が現れた時のみ True。
    どの input があるか（数・accept）もログに出すので、外れても次回特定できる。"""
    # 1) ＋メニューを開く
    plus_selectors = [
        'button[aria-label="メニューを開く"]',
        'button[aria-label*="追加"]',
        'button[aria-label*="挿入"]',
        'button[aria-label*="メニュー"]',
        '.o-largeMenu__btn',
        '[class*="largeMenu"] button',
        '[class*="LargeMenu"] button',
        'button[data-key="largeMenu"]',
        'button:has-text("+")',
    ]
    opened = _click_first(page, plus_selectors)
    print(f"note_image_plusmenu[{key}]={'opened' if opened else 'not_found'}")
    if opened:
        page.wait_for_timeout(600)

    # 2) 診断: いま存在する file input の数と accept をログ（次回の特定材料）
    _log_file_inputs(page, key)

    # 3) 主軸: 「画像をアップロード」ボタン内の input[type=file] に直接セット
    scoped_selectors = [
        'button:has-text("画像をアップロード") input[type="file"]',
        'label:has-text("画像をアップロード") input[type="file"]',
        '[role="menuitem"]:has-text("画像をアップロード") input[type="file"]',
        'li:has-text("画像をアップロード") input[type="file"]',
        'button:has-text("画像を追加") input[type="file"]',
        'label:has-text("画像") input[type="file"]',
    ]
    for sel in scoped_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            loc.first.set_input_files(file_path, timeout=5000)
            print(f"note_image_set[{key}]=scoped sel={sel}")
            if _wait_for_uploaded_image(page, timeout_ms=12000):
                return True
        except Exception as exc:
            print(f"note_image_set[{key}]=scoped_miss sel={sel}: {exc}")

    # 4) 予備: 項目クリックでネイティブダイアログを開く環境向けに expect_file_chooser
    upload_texts = ["画像をアップロード", "画像を追加", "画像"]
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            if not _click_text_first(page, upload_texts):
                raise RuntimeError("「画像をアップロード」項目が見つかりません")
        fc_info.value.set_files(file_path)
        print(f"note_image_filechooser[{key}]=set_files_done")
        if _wait_for_uploaded_image(page, timeout_ms=12000):
            return True
    except Exception as exc:
        print(f"note_image_filechooser[{key}]=miss: {exc}")

    # 5) 項目クリック後に出現する image用 input を再走査してセット（accept に image を含むもの優先）
    try:
        _click_text_first(page, upload_texts)
        page.wait_for_timeout(800)
        if _set_image_accepting_input(page, file_path, key):
            return True
    except Exception as exc:
        print(f"note_image_postclick[{key}]=miss: {exc}")

    return False


def _log_file_inputs(page, key: str | None = None) -> None:
    """ページ内の input[type=file] の数と accept 属性をログ出力（原因特定用）。"""
    try:
        inputs = page.locator('input[type="file"]')
        count = inputs.count()
    except Exception:
        print(f"note_image_inputs[{key}]=count=0")
        return
    accepts = []
    for idx in range(count):
        try:
            accepts.append(inputs.nth(idx).get_attribute("accept") or "")
        except Exception:
            accepts.append("?")
    print(f"note_image_inputs[{key}]=count={count} accepts={accepts}")


def _set_image_accepting_input(page, file_path: str, key: str | None = None) -> bool:
    """input[type=file] のうち accept に image を含むものを優先して set_input_files。
    本文内に <img> が出たもののみ採用（アイキャッチ等の誤爆は <img> 不出現で弾かれる）。"""
    try:
        inputs = page.locator('input[type="file"]')
        count = inputs.count()
    except Exception:
        return False
    ranked: list[tuple[int, int]] = []
    for idx in range(count):
        try:
            accept = (inputs.nth(idx).get_attribute("accept") or "").lower()
        except Exception:
            accept = ""
        ranked.append((0 if "image" in accept else 1, idx))
    ranked.sort()
    for _, idx in ranked:
        try:
            inputs.nth(idx).set_input_files(file_path, timeout=5000)
            print(f"note_image_set[{key}]=scan_input idx={idx}")
            if _wait_for_uploaded_image(page, timeout_ms=10000):
                return True
        except Exception:
            continue
    return False


def _click_text_first(page, texts: list[str]) -> bool:
    """可視テキストで最初に見つかった要素をクリック（Playwright get_by_text）。"""
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count() == 0:
                continue
            loc.first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _image_debug_screenshot(page, key: str | None = None) -> None:
    """画像挿入に失敗した時のエディタ状態をスクショ保存（原因特定用）。
    NOTE_IMAGE_DEBUG=0 で無効化。"""
    if os.environ.get("NOTE_IMAGE_DEBUG", "1") == "0":
        return
    try:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"note_image_debug_{key}.png" if key else "note_image_debug.png"
        screenshot_path = DEFAULT_OUTPUT_DIR / name
        page.screenshot(path=str(screenshot_path), full_page=False, timeout=8000)
        print(f"note_image_debug_screenshot={screenshot_path}")
    except Exception as exc:
        print(f"note_image_debug_failed={exc}")


def _try_set_file_input(page, file_path: str) -> bool:
    """ページ内の input[type=file] に set_input_files する。隠れていても試す。"""
    try:
        inputs = page.locator('input[type="file"]')
        count = inputs.count()
    except Exception:
        return False
    for idx in range(count):
        target = inputs.nth(idx)
        try:
            target.set_input_files(file_path, timeout=5000)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    return False


def _wait_for_uploaded_image(page, timeout_ms: int = 20_000) -> bool:
    """本文エディタ内に <img> が現れる＝アップロード完了をベストエフォートで待つ。"""
    try:
        page.wait_for_selector(
            '.ProseMirror img, [contenteditable="true"] img, figure img',
            state="visible",
            timeout=timeout_ms,
        )
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False


def save_note_draft(
    payload: NoteDraftPayload,
    headless: bool = True,
) -> str:
    from playwright.sync_api import sync_playwright

    _require_auth()
    with sync_playwright() as playwright:
        browser, context = _open_context(playwright, headless)
        try:
            url, _image_status = _save_one(context, payload)
            return url
        finally:
            context.close()
            browser.close()


def save_note_drafts(
    payloads: list[tuple[str, NoteDraftPayload]],
    headless: bool = True,
) -> list[tuple[str, str | None, str | None, str]]:
    """複数下書きを1ログインで連続保存する。戻り値は (key, url, error, image_status) のリスト。
    1本が失敗しても残りは続行する（ticket T-E: 1本失敗しても他を続行＋エラーscreenshot）。
    image_status は none/ok/failed（画像は本文保存をブロックしない）。"""
    from playwright.sync_api import sync_playwright

    _require_auth()
    results: list[tuple[str, str | None, str | None, str]] = []
    with sync_playwright() as playwright:
        browser, context = _open_context(playwright, headless)
        try:
            for key, payload in payloads:
                try:
                    url, image_status = _save_one(context, payload, error_key=key)
                    results.append((key, url, None, image_status))
                except Exception as exc:  # noqa: BLE001 - 1本失敗で全体を止めない
                    results.append((key, None, str(exc), "none"))
        finally:
            context.close()
            browser.close()
    return results


def save_cloud_note_draft(
    output_dir: Path,
    note_url_file: str = NOTE_CLOUD_URL_FILE,
    headless: bool = True,
) -> tuple[str, dict]:
    from playwright.sync_api import sync_playwright

    payload = load_cloud_note_payload(output_dir)
    _require_auth()
    with sync_playwright() as playwright:
        browser, context = _open_context(playwright, headless)
        try:
            draft_url, image_status = _save_one(context, payload, error_key="cloud")
            # 確認に失敗しても下書きURL・検証JSONを必ずArtifactに残す（先に書く）
            write_note_url(output_dir, draft_url, note_url_file)
            verify_path = output_dir / NOTE_CLOUD_VERIFY_FILE
            verification = _verify_saved_draft(context, draft_url, payload, image_status, key="cloud")
            verification["draft_list"] = _verify_draft_list(context, payload.title, key="cloud")
            # 最終判定は実際のnote画面に合わせた _cloud_verify_ok で行う
            verification["ok"] = _cloud_verify_ok(verification, draft_url)
            verify_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"note_cloud_draft_url={draft_url}")
            print(f"note_cloud_verify_file={verify_path}")
            print(f"note_cloud_verify_ok={verification.get('ok')}")
            print(f"note_cloud_draft_list_found={verification.get('draft_list', {}).get('title_found')}")
            if not verification.get("ok"):
                raise RuntimeError(f"note下書きの保存後確認に失敗しました: {verification}")
            return draft_url, verification
        finally:
            context.close()
            browser.close()


def _cloud_verify_ok(verification: dict, draft_url: str) -> bool:
    """保存確認の最終判定。実際のnote画面に合わせる。

    - 非公開下書き（draft_unpublished_assumed / draft / unpublished）は成功扱い。
    - タイトル一致・不足テキストなし・画像数OKが確認できれば、URL形式の違い
      （クエリ付与・editor.note.comへのリダイレクト等）だけでは落とさない。
    - 編集画面の再読込がフレーキーに失敗した場合でも、下書き一覧にタイトルが
      出ていれば保存はされているので成功扱いにする。
    """
    if not draft_url:
        return False
    public_state = str(verification.get("public_state") or "draft_unpublished_assumed")
    if public_state not in ("draft_unpublished_assumed", "draft", "unpublished"):
        return False
    title_found = bool(verification.get("title_found"))
    missing_ok = not verification.get("missing_texts")
    try:
        image_ok = int(verification.get("image_count") or 0) >= int(verification.get("min_image_count") or 0)
    except (TypeError, ValueError):
        image_ok = False
    draft_list_found = bool((verification.get("draft_list") or {}).get("title_found"))
    url_ok = bool(verification.get("url_pattern_ok")) or is_saved_draft_url(draft_url)
    # 本命: 本文・画像・タイトルが確認できていれば成功（URLパターン差異では落とさない）
    if title_found and missing_ok and image_ok:
        return True
    # 保険: 編集画面の再確認が失敗しても、下書き一覧にタイトルがあれば保存成功
    if draft_list_found and url_ok:
        return True
    return False


def _verify_saved_draft(
    context,
    draft_url: str,
    payload: NoteDraftPayload,
    image_status: str,
    key: str | None = None,
) -> dict:
    page = context.new_page()
    try:
        result = {
            "ok": False,
            "url": draft_url,
            "title_expected": payload.title,
            "title_found": False,
            "image_status": image_status,
            "image_count": 0,
            "min_image_count": payload.min_image_count,
            "missing_texts": list(payload.verify_texts),
            "public_state": "draft_unpublished_assumed",
            "url_pattern_ok": is_saved_draft_url(draft_url),
        }
        try:
            page.goto(draft_url, wait_until="domcontentloaded", timeout=60_000)
            _wait_for_editor_ready(page)
            page.wait_for_timeout(1500)
            page_text = _page_visible_text(page)
            title_text = _read_title_text(page)
            image_count = _count_editor_images(page)
            missing_texts = [text for text in payload.verify_texts if text and text not in page_text]
            result.update({
                "url": page.url,
                "title_found": payload.title in title_text or payload.title in page_text,
                "image_count": image_count,
                "missing_texts": missing_texts,
                "url_pattern_ok": is_saved_draft_url(page.url) or is_saved_draft_url(draft_url),
            })
            # URLパターンは記録のみ（クエリ付与やリダイレクトで形式が変わっても本文確認を優先）
            result["ok"] = (
                bool(result["title_found"])
                and image_count >= payload.min_image_count
                and not missing_texts
            )
        except Exception as exc:  # noqa: BLE001 - 再読込失敗でも記録を残し、最終判定は _cloud_verify_ok に委ねる
            result["error"] = str(exc)
        print(f"note_cloud_verify_title[{key}]={result['title_found']}")
        print(f"note_cloud_verify_images[{key}]={result['image_count']}/{payload.min_image_count}")
        print(f"note_cloud_verify_missing_texts[{key}]={result['missing_texts']}")
        if not result["ok"]:
            _save_error_debug(page, f"verify_{key}" if key else "verify")
        return result
    finally:
        page.close()


def _verify_draft_list(context, title: str, key: str | None = None) -> dict:
    page = context.new_page()
    try:
        page.goto("https://note.com/notes", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        text = _page_visible_text(page)
        result = {"checked": True, "url": page.url, "title_found": title in text}
        print(f"note_cloud_draft_list_title[{key}]={result['title_found']}")
        if not result["title_found"]:
            _save_error_debug(page, f"draft_list_{key}" if key else "draft_list")
        return result
    except Exception as exc:  # noqa: BLE001 - URL変更に備え、保存後検証とは分けて記録
        print(f"note_cloud_draft_list_error[{key}]={exc}")
        return {"checked": False, "url": page.url, "title_found": False, "error": str(exc)}
    finally:
        page.close()


def _page_visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return ""


def _read_title_text(page) -> str:
    selectors = [
        'textarea[placeholder*="タイトル"]',
        'textarea[aria-label*="タイトル"]',
        '[data-testid*="title"] textarea',
        'textarea',
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            value = locator.first.input_value(timeout=3000)
            if value:
                return value
        except Exception:
            continue
    return ""


def _count_editor_images(page) -> int:
    try:
        return int(page.locator('.ProseMirror img, [contenteditable="true"] img, figure img').count())
    except Exception:
        return 0


def is_saved_draft_url(url: str) -> bool:
    # クエリ・フラグメント付きでも下書きURLとして認める（?from=... 等が付くことがある）
    base = str(url or "").split("?", 1)[0].split("#", 1)[0]
    match = NOTE_DRAFT_URL_RE.fullmatch(base)
    if not match:
        return False
    note_id = match.group(1)
    return note_id.lower() != "new"


def _login(page, email: str, password: str) -> None:
    _fill_first(page, [
        'input[placeholder*="メールアドレス"]',
        'input[placeholder*="note ID"]',
        'input[type="email"]',
        'input[name="email"]',
    ], email)
    _fill_first(page, [
        'input[type="password"]',
        'input[name="password"]',
    ], password)
    _click_first(page, [
        'button:has-text("ログイン")',
        'button:has-text("続ける")',
        'button:has-text("次へ")',
    ])
    page.wait_for_timeout(2000)


def _wait_for_editor_ready(page, timeout_ms: int = 180_000) -> None:
    """note.com はSPA。goto直後はローディング表示だけで、タイトル/本文の編集欄は
    まだDOMに無い（失敗時スクショが3点ローディングだけだったのが証拠）。
    編集欄が実際に描画されるまで待ってから入力する＝『欄が見つかりません』を防ぐ。"""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    # 通信が落ち着くまで（ベストエフォート。常駐通信で落ち着かない事もあるので例外は無視）
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass

    # タイトルか本文の編集領域が「表示される」まで待つ
    editor_selectors = ", ".join([
        '[contenteditable="true"]',
        '[data-testid*="title"] textarea',
        '[data-testid*="title"] input',
        'textarea[placeholder*="タイトル"]',
        'input[placeholder*="タイトル"]',
        '.ProseMirror',
    ])
    try:
        page.wait_for_selector(editor_selectors, state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector(editor_selectors, state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("noteエディタの読み込みが完了しませんでした（描画待ちタイムアウト）") from exc

    # 描画直後はまだ入力を受け付けない事があるので少し待つ
    page.wait_for_timeout(1500)


def _fill_title(page, title: str) -> None:
    # noteの記事エディタ(editor.note.com)のタイトルは <textarea>。
    # 本文(ProseMirrorのcontenteditable)とは別要素。失敗時スクショ＝タイトルは
    # 大きな見出しのtextarea、本文は「あなたの日記も…」プレースホルダのcontenteditable。
    # 旧コードのcontenteditableフォールバックは本文(背の高いcontenteditable)しか掴めず
    # 『タイトル入力欄が見つかりません』になっていたので、textareaを直接狙う。
    selectors = [
        'textarea[placeholder*="タイトル"]',
        'textarea[placeholder*="記事"]',
        'textarea[aria-label*="タイトル"]',
        '[data-testid*="title"] textarea',
        'textarea[name*="title"]',
        'textarea',  # noteエディタのタイトルは先頭のtextarea（プレースホルダ非一致時の保険）
    ]

    if _fill_first(page, selectors, title):
        return

    raise RuntimeError("タイトル入力欄が見つかりません")


def _fill_body(page, body_html: str) -> None:
    editor = _find_body_editor(page)
    if editor is None:
        raise RuntimeError("本文入力欄が見つかりません")

    plain_text = _html_to_plain_text(body_html)
    if _fill_body_plain_text(page, editor, plain_text):
        return

    # page.evaluate は引数を1つしか取らない。htmlとtextはdictにまとめて渡す
    # （旧コードは2つ渡して TypeError: evaluate() takes ... but 4 were given になっていた）。
    page.evaluate(
        """async ({ html, text }) => {
            try {
                await navigator.clipboard.write([
                    new ClipboardItem({
                        "text/html": new Blob([html], { type: "text/html" }),
                        "text/plain": new Blob([text], { type: "text/plain" }),
                    }),
                ]);
            } catch (error) {
                await navigator.clipboard.writeText(text);
            }
        }""",
        {"html": body_html, "text": plain_text},
    )
    editor.click()
    _select_all(page)
    _paste(page)
    page.wait_for_timeout(1500)
    if _editor_text_length(editor) >= 80:
        return

    if _fill_body_plain_text(page, editor, plain_text):
        return

    raise RuntimeError("本文入力後の長さが不足しています")


def _fill_body_plain_text(page, editor, plain_text: str) -> bool:
    try:
        editor.click()
        _select_all(page)
        editor.fill(plain_text, timeout=10_000)
        page.wait_for_timeout(1000)
        if _editor_text_length(editor) >= 80:
            return True
    except Exception:
        pass

    try:
        editor.click()
        _select_all(page)
        page.keyboard.insert_text(plain_text[:20_000])
        page.wait_for_timeout(1000)
        return _editor_text_length(editor) >= 80
    except Exception:
        return False


def _try_save(page) -> None:
    if _click_first(page, [
        'button:has-text("下書き保存")',
        'button:has-text("保存")',
        'button:has-text("Draft")',
    ]):
        return
    page.keyboard.press("Control+S")


def _find_body_editor(page):
    # noteの本文は ProseMirror の contenteditable（スクショの「+」「あなたの日記も…」の領域）。
    # タイトルは textarea なのでここには掛からない＝本文だけを確実に掴む。
    selectors = [
        '.ProseMirror[contenteditable="true"]',
        '.ProseMirror',
        '[aria-label*="本文"][contenteditable="true"]',
        '[data-placeholder*="本文"][contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
    ]

    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.last

    locator = page.locator('[contenteditable="true"]')
    for idx in range(locator.count()):
        candidate = locator.nth(idx)
        try:
            box = candidate.bounding_box()
        except Exception:
            box = None
        if box and box.get("height", 0) >= 120:
            return candidate

    if locator.count() > 0:
        return locator.last

    return None


def _save_error_debug(page, key: str | None = None) -> None:
    try:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"note_autosave_error_{key}.png" if key else "note_autosave_error.png"
        screenshot_path = DEFAULT_OUTPUT_DIR / name
        # full_page=True は長い記事でフォント待ちにより30秒タイムアウトする事がある。
        # 表示領域だけ・短いタイムアウトで確実に残す（デバッグ画像が本来のエラーを隠さないように）。
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=10_000)
        except Exception:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)

        print(f"note_autosave_error_url={page.url}")
        print(f"note_autosave_error_title={page.title()}")
        print(f"note_autosave_error_screenshot={screenshot_path}")
    except Exception as exc:
        print(f"note_autosave_error_debug_failed={exc}")


def _select_all(page) -> None:
    if os.name == "posix":
        page.keyboard.press("Meta+A")
    else:
        page.keyboard.press("Control+A")


def _paste(page) -> None:
    # macOS(posix)の貼り付けは Cmd+V。Control+V では貼れず本文が空のままになる。
    if os.name == "posix":
        page.keyboard.press("Meta+V")
    else:
        page.keyboard.press("Control+V")


def _editor_text_length(editor) -> int:
    try:
        value = editor.evaluate("(el) => (el.innerText || el.textContent || '').trim().length")
    except Exception:
        return 0
    return int(value or 0)


def _html_to_plain_text(body_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body_html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h1|h2|h3|li|tr|table|ul|ol)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _fill_first(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        element = locator.first
        try:
            element.fill(value)
            return True
        except Exception:
            continue
    return False


def _click_first(page, selectors: list[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        try:
            locator.first.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def write_note_url(output_dir: Path, note_url: str, note_url_file: str) -> Path:
    path = output_dir / note_url_file
    path.write_text(note_url.strip() + "\n", encoding="utf-8")
    return path


def load_manifest_entries(output_dir: Path) -> list[dict]:
    """note_draft.build_note4 が書いた manifest を読む。無ければ空リスト＝従来の単一Note動作。"""
    manifest_path = output_dir / NOTE4_MANIFEST_FILE
    if not manifest_path.exists():
        return []
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{manifest_path} の読み込みに失敗しました") from exc
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("title_file") and e.get("html_file")]


def load_payload_from_entry(output_dir: Path, entry: dict) -> NoteDraftPayload:
    title_path = output_dir / entry["title_file"]
    html_path = output_dir / entry["html_file"]
    if not title_path.exists():
        raise FileNotFoundError(f"{title_path} が見つかりません")
    if not html_path.exists():
        raise FileNotFoundError(f"{html_path} が見つかりません")
    title = title_path.read_text(encoding="utf-8").strip()
    html = html_path.read_text(encoding="utf-8")
    chart_path = _resolve_chart_path(output_dir, entry)
    return NoteDraftPayload(title=title, body_html=extract_body_fragment(html), chart_path=chart_path)


def _resolve_chart_path(output_dir: Path, entry: dict) -> str | None:
    """manifest の chart_image（無ければ .md 内の <!-- chart_image: ... --> マーカー）から
    画像の絶対パスを決める。存在しなければ None（画像なしで本文だけ保存）。"""
    rel = entry.get("chart_image")
    if not rel:
        rel = _chart_marker_from_md(output_dir, entry)
    if not rel:
        return None
    candidate = Path(rel)
    if not candidate.is_absolute():
        # manifest の相対パスはリポジトリroot基準（outputs/charts_.../...png）。
        candidate = PROJECT_ROOT / rel
    if candidate.exists():
        return str(candidate)
    # outputs/ 起点でも探す（保険）
    alt = output_dir / Path(rel).name
    if alt.exists():
        return str(alt)
    print(f"note_image_warning[{entry.get('key')}]=画像ファイルが存在しません: {candidate}")
    return None


def _chart_marker_from_md(output_dir: Path, entry: dict) -> str | None:
    md_file = entry.get("md_file")
    if not md_file:
        return None
    md_path = output_dir / md_file
    if not md_path.exists():
        return None
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    match = re.search(r"<!--\s*chart_image:\s*(.+?)\s*-->", text)
    return match.group(1).strip() if match else None


def _run_multi(output_dir: Path, entries: list[dict], headless: bool) -> int:
    payloads: list[tuple[str, NoteDraftPayload]] = []
    for entry in entries:
        key = str(entry.get("key", "")) or "note"
        payloads.append((key, load_payload_from_entry(output_dir, entry)))
    results = save_note_drafts(payloads, headless=headless)
    url_by_key = {e.get("key"): e.get("url_file", f"note_draft_url_{e.get('key')}.txt") for e in entries}
    ok = 0
    img_ok = 0
    img_total = 0
    for key, url, error, image_status in results:
        url_file = url_by_key.get(key, f"note_draft_url_{key}.txt")
        if url:
            note_url_path = write_note_url(output_dir, url, url_file)
            print(f"note_draft_url[{key}]={url}")
            print(f"note_draft_url_file[{key}]={note_url_path}")
            ok += 1
        else:
            print(f"note_draft_error[{key}]={error}")
        if image_status != "none":
            img_total += 1
            if image_status == "ok":
                img_ok += 1
        print(f"note_draft_image[{key}]={image_status}")
    print(f"note_draft_saved={ok}/{len(results)}")
    print(f"note_draft_image_uploaded={img_ok}/{img_total}")
    return ok


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    if args.cloud_article:
        save_cloud_note_draft(output_dir, note_url_file=args.note_url_file, headless=args.headless)
    else:
        entries = [] if args.single else load_manifest_entries(output_dir)
        if entries:
            # T-E: 4本Note分割モード（manifest駆動・1ログインで連続保存・公開しない）
            _run_multi(output_dir, entries, headless=args.headless)
        else:
            # 従来の単一Note（note_daily）。manifest が無い／--single 指定時の後方互換。
            payload = load_note_payload(output_dir)
            draft_url = save_note_draft(payload, headless=args.headless)
            note_url_path = write_note_url(output_dir, draft_url, args.note_url_file)
            print(f"note_draft_url={draft_url}")
            print(f"note_draft_url_file={note_url_path}")
    if os.environ.get("NOTE_STORAGE_STATE", "").strip():
        print("note_auth=storage_state")
    else:
        print("note_auth=credentials")


if __name__ == "__main__":
    main()
