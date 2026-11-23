"""Command-line entry point: `curio analyze <url>` and `curio api`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from curio_learn.config import get_settings
from curio_learn.domain import LearningPreferences, LearningProfile, VideoAnalysis, fmt_ts
from curio_learn.errors import CurioError
from curio_learn.logging_setup import configure_logging


def print_analysis(a: VideoAnalysis) -> None:
    p = a.playlist
    print(f"\n{a.video.title}\n{a.video.channel} · {fmt_ts(a.video.duration_s)}\n")
    print(a.overview, "\n")
    if p:
        print(
            f"Playlist: {fmt_ts(p.total_s)} of {fmt_ts(p.original_s)} "
            f"(saves {fmt_ts(p.time_saved_s)}, AI coverage estimate {p.coverage_estimate:.0%})\n"
        )
        for seg in p.segments:
            print(f"  {seg.id} [{seg.timestamp}] {seg.importance.value:<11} {seg.score:5.1f}  {seg.title}")
            print(f"       {seg.what_you_learn}")
        print("\nSkip:")
        for s in p.skipped:
            print(f"  [{fmt_ts(s.start_s)}–{fmt_ts(s.end_s)}] {s.title} — {s.reason}")
    if a.validation.warnings:
        print("\nWarnings:", *a.validation.warnings, sep="\n  ")
    print(f"\n{a.llm.provider if a.llm else ''} {a.llm.models if a.llm else ''} · {a.elapsed_s}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="curio")
    sub = parser.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="Analyze a YouTube video")
    an.add_argument("url")
    an.add_argument("--profile", choices=[p.value for p in LearningProfile], default="general")
    an.add_argument("--objective", default="")
    an.add_argument("--minutes", type=float, default=None)
    an.add_argument("--force", action="store_true", help="Ignore cached analysis")
    an.add_argument("--json", type=Path, help="Write the full analysis JSON here")

    api = sub.add_parser("api", help="Run the API server (also serves the built web UI)")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8470)

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    if args.cmd == "analyze":
        from curio_learn.service import CurioService

        prefs = LearningPreferences(
            profile=LearningProfile(args.profile), custom_objective=args.objective, target_minutes=args.minutes
        )
        try:
            result = CurioService(settings).analyze(
                args.url, prefs, force=args.force, progress=lambda m: print(f"… {m}", flush=True)
            )
        except CurioError as exc:
            print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
        print_analysis(result)
        if args.json:
            args.json.write_text(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0
    if args.cmd == "api":
        import uvicorn

        uvicorn.run("curio_learn.api.app:app", host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
