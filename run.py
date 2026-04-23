"""Single entry point.

  python run.py cli <ASIN> [options]   # one-off CLI check with intelligence
  python run.py web                    # start dashboard on http://127.0.0.1:5000
  python run.py monitor                # run one scheduled pass manually
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _usage():
    print(__doc__)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        _usage()
    mode = sys.argv.pop(1)
    if mode == "cli":
        from kdp_checker.cli import main as cli_main
        cli_main()
    elif mode == "web":
        from web.app import app
        app.run(host="127.0.0.1", port=5000, debug=False)
    elif mode == "monitor":
        from kdp_checker.scheduler import _run_checks_once
        _run_checks_once()
    else:
        _usage()


if __name__ == "__main__":
    main()
