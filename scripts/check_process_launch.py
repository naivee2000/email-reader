"""Guarded fictional child-launch stress test after native WebEngine startup.

Run with PYTHONPATH=src .venv/bin/python scripts/check_process_launch.py.
No real mail, account, Keychain, or AI backend is opened.
"""

import argparse

from email_reader.process_diagnostics import run_probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lanes", type=int, default=4)
    parser.add_argument("--children-per-lane", type=int, default=12)
    args = parser.parse_args()
    return run_probe(lanes=args.lanes, children_per_lane=args.children_per_lane)


if __name__ == "__main__":
    raise SystemExit(main())
