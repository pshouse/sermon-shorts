import sys

# Account and audit commands need only the standard library, so dispatch them
# before importing the pipeline (OpenCV, numpy, ffmpeg...).
if len(sys.argv) > 1 and sys.argv[1] in ("users", "audit"):
    from .accounts_cli import users_main, audit_main
    raise SystemExit((users_main if sys.argv[1] == "users" else audit_main)(sys.argv[2:]))

from .cli import main

raise SystemExit(main())
