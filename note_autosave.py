from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
NOTE_TITLE_FILE = "note_title.txt"
NOTE_HTML_FILE = "note_daily.html"
NOTE_URL_FILE = "note_draft_url.txt"
NOTE_NEW_URL = "https://note.com/notes/new"


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


def save_note_draft(
    email: str,
    password: str,
    payload: NoteDraftPayload,
    headless: bool = True,
) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 1800})
        page = context.new_page()
        try:
            page.goto(NOTE_NEW_URL, wait_until="domcontentloaded", timeout=60_000)
            if "login" in page.url:
                _login(page, email, password)
                page.goto(NOTE_NEW_URL, wait_until="domcontentloaded", timeout=60_000)
            _fill_title(page, payload.title)
            _fill_body(page, payload.body_html)
            _try_save(page)
            try:
                page.wait_for_url(re.compile(r"https://note\.com/notes/(?!new).*"), timeout=30_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2000)
            url = page.url
        finally:
            context.close()
            browser.close()

    if "note.com" not in url:
        raise RuntimeError(f"note draft URL を取得できませんでした: {url}")
    return url


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


def _fill_title(page, title: str) -> None:
    selectors = [
        'input[placeholder*="タイトル"]',
        'textarea[placeholder*="タイトル"]',
        'input[name*="title"]',
        'textarea[name*="title"]',
        'input[type="text"]',
    ]
    if _fill_first(page, selectors, title):
        return
    raise RuntimeError("タイトル入力欄が見つかりません")


def _fill_body(page, body_html: str) -> None:
    selectors = [
        'div[contenteditable="true"]',
        '[role="textbox"]',
        "textarea",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        element = locator.first
        tag_name = element.evaluate("(el) => el.tagName.toLowerCase()")
        if tag_name == "textarea":
            element.fill(body_html)
            return
        element.click()
        element.evaluate(
            """(el, html) => {
                el.focus();
                el.innerHTML = html;
                el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertFromPaste" }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            body_html,
        )
        return
    raise RuntimeError("本文入力欄が見つかりません")


def _try_save(page) -> None:
    if _click_first(page, [
        'button:has-text("下書き保存")',
        'button:has-text("保存")',
        'button:has-text("Draft")',
    ]):
        return
    page.keyboard.press("Control+S")


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
    email, password = load_credentials()
    note_url = save_note_draft(email, password, payload, headless=args.headless)
    note_url_path = write_note_url(output_dir, note_url, args.note_url_file)
    print(f"note_draft_url={note_url}")
    print(f"note_draft_url_file={note_url_path}")


if __name__ == "__main__":
    main()
