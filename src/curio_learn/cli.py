"""Command-line entry point: `curio analyze`, `curio notes`, `curio ask` and `curio api`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from curio_learn.config import get_settings
from curio_learn.domain import LearningPreferences, LearningProfile, NotesScope, VideoAnalysis, fmt_ts
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


def cli_preferences(args, service) -> LearningPreferences:
    """Flags given on the command line, the rest from the saved default goal."""
    saved = service.default_preferences()
    return LearningPreferences(
        profile=LearningProfile(args.profile) if args.profile else saved.profile,
        custom_objective=saved.custom_objective if args.objective is None else args.objective,
        target_minutes=args.minutes if args.minutes is not None else saved.target_minutes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="curio")
    sub = parser.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="Analyze a YouTube video")
    an.add_argument("url")
    an.add_argument("--profile", choices=[p.value for p in LearningProfile], help="Default: your saved goal")
    an.add_argument("--objective", default=None)
    an.add_argument("--minutes", type=float, default=None)
    an.add_argument("--force", action="store_true", help="Ignore cached analysis")
    an.add_argument("--json", type=Path, help="Write the full analysis JSON here")

    nt = sub.add_parser("notes", help="Write learning notes for a finished analysis")
    nt.add_argument("analysis_id")
    nt.add_argument("--scope", choices=[s.value for s in NotesScope], default="playlist")
    nt.add_argument("--segments", default="", help="Comma-separated segment ids (with --scope selected)")
    nt.add_argument("--force", action="store_true", help="Write new notes even if saved ones exist")
    nt.add_argument("--out", type=Path, help="Write the Markdown here instead of printing it")

    qz = sub.add_parser("quiz", help="Write a quiz and flashcards for a finished analysis, and print them")
    qz.add_argument("analysis_id")
    qz.add_argument("--scope", choices=[s.value for s in NotesScope], default="playlist")
    qz.add_argument("--segments", default="", help="Comma-separated segment ids (with --scope selected)")
    qz.add_argument("--force", action="store_true", help="Write a new set even if a saved one exists")

    ak = sub.add_parser("ask", help="Ask a question about an analyzed video")
    ak.add_argument("analysis_id")
    ak.add_argument("question")

    qu = sub.add_parser("queue", help="Queue a video for the overnight queue, or list the queue")
    qu.add_argument("url", nargs="?", help="Video to queue; without it, list the queue")
    qu.add_argument("--profile", choices=[p.value for p in LearningProfile], help="Default: your saved goal")
    qu.add_argument("--objective", default=None)
    qu.add_argument("--minutes", type=float, default=None)
    qu.add_argument("--remove", metavar="ANALYSIS_ID", help="Take a video out of the queue")

    api = sub.add_parser("api", help="Run the API server (also serves the built web UI)")
    # All interfaces, so phones and laptops on the LAN can use it. --host 127.0.0.1 keeps it local.
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8470)

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    if args.cmd == "analyze":
        from curio_learn.service import CurioService

        service = CurioService(settings)
        prefs = cli_preferences(args, service)
        try:
            result = service.analyze(args.url, prefs, force=args.force, progress=lambda m: print(f"… {m}", flush=True))
        except CurioError as exc:
            print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
        print_analysis(result)
        if args.json:
            args.json.write_text(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0
    if args.cmd == "notes":
        from curio_learn.service import CurioService

        service = CurioService(settings)
        segments = [x.strip() for x in args.segments.split(",") if x.strip()]
        try:
            notes = service.notes(
                args.analysis_id,
                NotesScope(args.scope),
                segments,
                force=args.force,
                progress=lambda m: print(f"… {m}", flush=True),
            )
        except KeyError:
            print(f"Error: no analysis with id {args.analysis_id}", file=sys.stderr)
            return 1
        except CurioError as exc:
            print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
        text = service.notes_markdown(notes.id) or ""
        if args.out:
            args.out.write_text(text)
            print(f"Wrote {args.out}")
        else:
            print(text)
        for w in notes.warnings:
            print(f"Warning: {w}", file=sys.stderr)
        return 0
    if args.cmd == "quiz":
        from curio_learn.service import CurioService

        segments = [x.strip() for x in args.segments.split(",") if x.strip()]
        try:
            study = CurioService(settings).study(
                args.analysis_id,
                NotesScope(args.scope),
                segments,
                force=args.force,
                progress=lambda m: print(f"… {m}", flush=True),
            )
        except KeyError:
            print(f"Error: no analysis with id {args.analysis_id}", file=sys.stderr)
            return 1
        except CurioError as exc:
            print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
        print(f"\nQuiz ({len(study.questions)} questions)\n")
        for q in study.questions:
            print(f"{q.id}. {q.question}  [{fmt_ts(q.start_s)}]")
            for i, o in enumerate(q.options):
                print(f"   {'*' if i == q.answer else ' '} {chr(65 + i)}. {o}")
            if q.explanation:
                print(f"   {q.explanation}")
        print(f"\nFlashcards ({len(study.cards)})\n")
        for c in study.cards:
            print(f"{c.id}. {c.front}\n    {c.back}  [{fmt_ts(c.start_s)}]")
        for w in study.warnings:
            print(f"Warning: {w}", file=sys.stderr)
        print(f"\n{study.elapsed_s}s")
        return 0
    if args.cmd == "ask":
        from curio_learn.service import CurioService

        try:
            a = CurioService(settings).ask(args.analysis_id, args.question)
        except KeyError:
            print(f"Error: no analysis with id {args.analysis_id}", file=sys.stderr)
            return 1
        except CurioError as exc:
            print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
            return 1
        print()
        for p in a.parts:
            cites = ", ".join(fmt_ts(c.start_s) for c in p.citations)
            print(f"{p.text}" + (f"  [{cites}]" if cites else ""))
        if not a.in_video:
            print("\n(The video doesn't cover this.)")
        if a.outside_knowledge:
            print(f"\nNot from the video: {a.outside_knowledge}")
        for w in a.warnings:
            print(f"\nNote: {w}")
        print(f"\n{a.llm.provider if a.llm else ''} · {a.context} transcript · {a.elapsed_s}s")
        return 0
    if args.cmd == "queue":
        from curio_learn.domain import AnalysisStatus
        from curio_learn.service import CurioService

        service = CurioService(settings)
        if args.remove:
            if not service.remove_queued(args.remove):
                print(f"Error: no queued video with id {args.remove}", file=sys.stderr)
                return 1
            print(f"Removed {args.remove} from the queue.")
            return 0
        if args.url:
            prefs = cli_preferences(args, service)
            try:
                analysis_id, _ = service.submit(args.url, prefs, queue=True)
            except CurioError as exc:
                print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
                return 1
            current = service.get(analysis_id)
            if current and current.status == AnalysisStatus.QUEUED:
                hours = settings.queue_hours or "any time"
                print(f"Queued {analysis_id}: {current.video.title}")
                print(f"The API server (curio api) analyzes it during the queue hours ({hours}) when the GPU is idle.")
            elif current:
                print(f"Already {current.status.value}: {analysis_id} ({current.video.title})")
            return 0
        items = service.repo.queue_items()
        if not items:
            print("The queue is empty.")
        for it in items:
            v = it["video"]
            print(f"{it['analysis_id']}  {it['status'].value:<8} {fmt_ts(v.duration_s):>8}  {v.title}")
        return 0
    if args.cmd == "api":
        import uvicorn

        uvicorn.run("curio_learn.api.app:app", host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
