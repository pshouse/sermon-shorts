import sys

if len(sys.argv) > 1 and sys.argv[1] in ("--version", "version"):
    from .core.version import label
    print(f"sermon-shorts {label()}")
    raise SystemExit(0)

# Account and audit commands need only the standard library, so dispatch them
# before importing the pipeline (OpenCV, numpy, ffmpeg...).
if len(sys.argv) > 1 and sys.argv[1] in ("users", "audit"):
    from .accounts_cli import users_main, audit_main
    raise SystemExit((users_main if sys.argv[1] == "users" else audit_main)(sys.argv[2:]))
if len(sys.argv) > 1 and sys.argv[1] in ("approve", "reject", "note", "mark-posted", "retry-cover"):
    from .review_cli import main as review_main
    raise SystemExit(review_main(sys.argv[1], sys.argv[2:]))
if len(sys.argv) > 1 and sys.argv[1] == "serve":
    from .api import serve
    raise SystemExit(serve(sys.argv[2:]))
if len(sys.argv) > 1 and sys.argv[1] in ("services", "runs", "schedule"):
    from .records_cli import services_main, runs_main, schedule_main
    main = {"services": services_main, "runs": runs_main, "schedule": schedule_main}[sys.argv[1]]
    raise SystemExit(main(sys.argv[2:]))

from .cli import main

raise SystemExit(main())
