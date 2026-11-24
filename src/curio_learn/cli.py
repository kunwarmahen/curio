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

    co = sub.add_parser("course", help="Add a course from a YouTube playlist, list courses, or build a course playlist")
    co.add_argument("target", nargs="?", help="A playlist link to add, or a course id to show; nothing lists courses")
    co.add_argument("--profile", choices=[p.value for p in LearningProfile], help="Default: your saved goal")
    co.add_argument("--objective", default=None)
    co.add_argument("--build", action="store_true", help="Build the course playlist from the analyzed lectures")
    co.add_argument("--minutes", type=int, default=None, help="Course playlist length (default: ~15 min a lecture)")
    co.add_argument("--force", action="store_true", help="Build a new course playlist even if a saved one exists")

    sub.add_parser(
        "check-claude",
        help="Make one tiny request to Claude to check the API key, model and structured output (costs < $0.01)",
    )

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
    if args.cmd == "check-claude":
        return check_claude(settings)
    if args.cmd == "course":
        from curio_learn.service import CurioService

        return run_course(args, CurioService(settings))
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


_LECTURE_STATE = {
    "ready": "analyzed",
    "other_goal": "analyzed for another goal",
    "queued": "queued",
    "analyzing": "analyzing",
    "failed": "failed",
    "missing": "not queued",
}


def run_course(args, service) -> int:
    from curio_learn.domain import fmt_ts

    if not args.target:
        courses = service.courses()
        if not courses:
            print("No courses yet. Add one with: curio course <youtube playlist link>")
        for c in courses:
            ready = sum(lec.analysis_id is not None for lec in c.lectures)
            print(f"{c.id}  {c.title}  ({ready} of {len(c.lectures)} lectures analyzed)")
        return 0
    try:
        if "list=" in args.target:
            prefs = cli_preferences(
                argparse.Namespace(profile=args.profile, objective=args.objective, minutes=None), service
            )
            course_id = service.import_course(args.target, prefs)
            print("… Checking each lecture's captions and queuing the ones not analyzed yet", flush=True)
            service.queue_course(course_id)
        else:
            course_id = args.target
        course = service.course(course_id)
        if course is None:
            print(f"Error: no course with id {course_id}", file=sys.stderr)
            return 1
        print(f"{course.title} ({course.id})")
        for lec in course.lectures:
            state = _LECTURE_STATE[lec.state.value]
            extra = f": {lec.error}" if lec.error else ""
            print(f"  {lec.label:>10}  {lec.title}  [{state}{extra}]")
        if course.unavailable:
            print(f"  ({course.unavailable} private or deleted video(s) in the playlist were left out)")
        if not args.build:
            return 0
        plan = service.course_plan(
            course_id, args.minutes, force=args.force, progress=lambda m: print(f"… {m}", flush=True)
        )
    except CurioError as exc:
        print(f"Error ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    print(f"\nCourse playlist: {fmt_ts(plan.total_s)} of {fmt_ts(plan.original_s)} ({len(plan.parts)} parts)")
    label = {lec.position: lec.label for lec in course.lectures}
    lecture = None
    for part in plan.parts:
        if part.lecture != lecture:
            lecture = part.lecture
            print(f"\n{label.get(lecture, lecture)}")
        print(f"  {fmt_ts(part.start_s)}–{fmt_ts(part.end_s)}  {part.title}")
    if plan.repeats:
        print("\nLeft out, taught in another lecture:")
        for r in plan.repeats:
            print(
                f"  {label.get(r.section.lecture)} {fmt_ts(r.section.start_s)} {r.section.title}  →  "
                f"{label.get(r.covered_by.lecture)} {fmt_ts(r.covered_by.start_s)} {r.covered_by.title}"
            )
    if plan.checklist:
        print("\nEasy to miss:")
        for item in plan.checklist:
            where = ", ".join(f"{label.get(r.lecture)} {fmt_ts(r.start_s)}" for r in item.refs)
            print(f"  - {item.text} ({where})")
    for w in plan.warnings:
        print(f"Warning: {w}", file=sys.stderr)
    return 0


def check_claude(settings) -> int:
    """One small structured request through the same provider the pipeline uses."""
    from pydantic import BaseModel

    from curio_learn.llm.anthropic_provider import AnthropicProvider
    from curio_learn.llm.base import generate_structured

    class Check(BaseModel):
        capital: str

    # CURIO_LLM_MODEL may name a local model; only use it when Claude is the provider.
    model = settings.llm_model if settings.resolved_provider == "anthropic" and settings.llm_model else None
    model = model or settings.anthropic_default_model
    print(f"Asking {model} one short question…", flush=True)
    try:
        out = generate_structured(
            AnthropicProvider(effort="low"),
            Check,
            model=model,
            system="Answer in the requested JSON.",
            user="What is the capital of France?",
            max_tokens=200,
            retries=0,
            task="check-claude",
        )
    except CurioError as exc:
        print(f"Claude is not working: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # e.g. no credentials at all
        if "authentication method" in str(exc):
            print("No Claude credentials: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN).", file=sys.stderr)
        else:
            print(f"Claude is not working: {exc}", file=sys.stderr)
        return 1
    print(f"Claude works: structured answer {out.model_dump()}.")
    print("Set CURIO_LLM_PROVIDER=anthropic (or leave it on auto with ANTHROPIC_API_KEY set) to use it.")
    return 0
