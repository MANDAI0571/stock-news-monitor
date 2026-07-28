from __future__ import annotations

import argparse
import self_test

CRITICAL_TESTS = {
    "_test_discipline_normal_and_stop",
    "_test_track_record",
    "_test_journal_and_pattern_learning",
    "_test_decision_engine",
    "_test_trade_verification",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=("critical", "editorial"), required=True)
    args = parser.parse_args()

    all_tests = sorted(
        name
        for name in dir(self_test)
        if name.startswith("_test_") and callable(getattr(self_test, name))
    )
    if args.group == "critical":
        names = [name for name in all_tests if name in CRITICAL_TESTS]
    else:
        names = [name for name in all_tests if name not in CRITICAL_TESTS]

    if not names:
        raise SystemExit(f"no tests selected for group={args.group}")

    failures = 0
    for name in names:
        print(f"self-test: running {name}", flush=True)
        try:
            getattr(self_test, name)()
        except Exception as exc:
            failures += 1
            print(f"self-test: FAIL {name}: {exc}", flush=True)
            if args.group == "critical":
                raise

    print(
        f"self-test: group={args.group} total={len(names)} failures={failures}",
        flush=True,
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
