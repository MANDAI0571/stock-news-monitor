"""Claude自動修復ブランチの差分が安全境界内か検査する。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
IMMUTABLE_PREFIXES = (
    "data/",
    "docs/archive/",
)
IMMUTABLE_EXCEPTIONS: tuple[str, ...] = ()
HIGH_REVIEW_PATHS = {
    "dual_300man_config.py",
    "dual_300man_fill.py",
    "claude_300man_declare.py",
    "claude_300man_fill.py",
}
MAX_CHANGED_FILES = 12


def changed_files(base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{base_ref}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"cannot diff against {base_ref}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def validate(paths: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    review: list[str] = []
    if not paths:
        errors.append("修正差分がありません")
    if len(paths) > MAX_CHANGED_FILES:
        errors.append(f"変更ファイル数が上限超過: {len(paths)} > {MAX_CHANGED_FILES}")
    for raw in paths:
        path = PurePosixPath(raw)
        normalized = path.as_posix()
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"不正なパス: {raw}")
            continue
        if any(normalized.startswith(prefix) for prefix in IMMUTABLE_PREFIXES) and normalized not in IMMUTABLE_EXCEPTIONS:
            errors.append(f"自動修復で変更禁止: {normalized}")
        if normalized in HIGH_REVIEW_PATHS:
            review.append(f"売買経路のため人の重点レビュー必須: {normalized}")
    return errors, review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", default="origin/main")
    args = parser.parse_args()
    try:
        paths = changed_files(args.base_ref)
    except Exception as error:
        print(f"claude_repair_guard=failed reason={error}")
        return 1
    errors, review = validate(paths)
    print(f"claude_repair_guard changed={len(paths)}")
    for path in paths:
        print(f"changed={path}")
    for item in review:
        print(f"review_required={item}")
    for item in errors:
        print(f"error={item}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
