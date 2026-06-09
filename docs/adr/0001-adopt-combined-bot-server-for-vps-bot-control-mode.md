# Adopt Combined Bot Server for VPS Bot Control Mode

YT Short Clipper is moving from a desktop Tkinter application to Bot Control Mode on a single-owner VPS. We will use a Combined Bot Server: one FastAPI process that serves Public Clip Links while running the Telegram bot, scheduler, and single-active-run Clipping Queue. SQLAlchemy with SQLite will store Source Videos, schedules, Workflow Defaults, Run Logs, generated clips, and publish attempts so the storage model can later migrate toward Supabase without changing the domain workflow.

This favors simple VPS deployment over separate API, bot, and worker processes for v1. If throughput or reliability requirements grow, the queue worker and scheduler can be split out later while keeping the same database-backed workflow model.
