Browser-runtime smoke test (jsdom). Not part of `unittest` because it needs Node.

    npm install jsdom@24
    python -m tests.fixtures /tmp/ui/data            # generates TEST price files (never shipped with the product)
    # create /tmp/ui/web.json (db_path, log_path, data_dir=/tmp/ui/data, port 18090, base_url http://127.0.0.1:18090),
    # register users free@/pro@/elite@example.com (Pro/Elite via the DB) and an admin boss@example.com with password Correct-Horse-Battery-9
    python tests/ui_smoke/seed.py                    # events/headlines are relative to NOW: run right before each smoke run
    python -m apex.cli --config /tmp/ui/web.json web-serve &
    node tests/ui_smoke/smoke.js

It drives login, signup (terms), Opportunities, the timing card and timeline, calendar filters, journal, the command palette, the
watchlist dialog, account sessions/export, and the admin go-live checklist and feeds panel, and fails on any runtime error.
