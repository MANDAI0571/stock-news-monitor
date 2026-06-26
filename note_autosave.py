from __future__ import annotations

import base64
import argparse
import json
import os
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_TITLE_FILE = "note_title.txt"
NOTE_HTML_FILE = "note_daily.html"
NOTE_URL_FILE = "note_draft_url.txt"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="note.com の下書きを自動保存する")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--note-url-file", default=NOTE_URL_FILE)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_false", dest="headless")
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


def save_note_draft(
    payload: NoteDraftPayload,
    headless: bool = True,
) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    storage_state = load_storage_state()
    credentials_available = bool(os.environ.get("NOTE_EMAIL", "").strip() and os.environ.get("NOTE_PASSWORD", "").strip())
    if storage_state is None and not credentials_available:
        raise RuntimeError("NOTE_STORAGE_STATE または NOTE_EMAIL / NOTE_PASSWORD が不足しています")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context_kwargs = {"viewport": {"width": 1440, "height": 1800}}
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://note.com")
        page = context.new_page()
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
                _save_error_debug(page)
                raise
        finally:
            context.close()
            browser.close()

    return draft_url


def is_saved_draft_url(url: str) -> bool:
    match = NOTE_DRAFT_URL_RE.fullmatch(url)
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


def _wait_for_editor_ready(page, timeout_ms: int = 60_000) -> None:
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
    if _editor_text_length(editor) < 80:
        raise RuntimeError("本文入力後の長さが不足しています")


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


def _save_error_debug(page) -> None:
    try:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = DEFAULT_OUTPUT_DIR / "note_autosave_error.png"
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


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
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
