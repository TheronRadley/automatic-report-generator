"""Allow ``python -m reportgen`` (used by job subprocesses)."""

from reportgen.cli.app import main

if __name__ == "__main__":
    main()
