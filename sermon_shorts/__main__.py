import sys

# Account and audit commands need only the standard library, so dispatch them
# before importing the pipeline (OpenCV, numpy, ffmpeg...).
if len(sys.argv) > 1 and sys.argv[1] in ("users", "audit"):
    from .accounts_cli import users_main, audit_main
    raise SystemExit((users_main if sys.argv[1] == "users" else audit_main)(sys.argv[2:]))
if len(sys.argv) > 1 and sys.argv[1] in ("approve", "reject", "note", "mark-posted", "retry-cover"):
    from .review_cli import main as review_main
    raise SystemExit(review_main(sys.argv[1], sys.argv[2:]))
if len(sys.argv) > 1 and sys.argv[1] in ("services", "runs"):
    from .records_cli import services_main, runs_main
    raise SystemExit((services_main if sys.argv[1] == "services" else runs_main)(sys.argv[2:]))

from .cli import main

raise SystemExit(main())
