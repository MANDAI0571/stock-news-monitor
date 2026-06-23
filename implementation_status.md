# Implementation Status

## Current state

- `note_draft.py` generates `outputs/note_daily.md`, `outputs/note_title.txt`, and `outputs/note_daily.html`.
- `note_autosave.py` saves the note draft through Playwright and writes `outputs/note_draft_url.txt`.
- `daily_note_mail.py` sends Gmail with the note draft URL in the body and note files as attachments.
- `note_autosave.yml` runs the daily workflow in GitHub Actions.

## Secrets required

- `NOTE_EMAIL`
- `NOTE_PASSWORD`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `MAIL_TO`

## Outputs

- `outputs/note_title.txt`
- `outputs/note_daily.md`
- `outputs/note_daily.html`
- `outputs/note_draft_url.txt`

## Notes

- `outputs/` remains outside git control.
- `research.py` is not part of this commit set.
