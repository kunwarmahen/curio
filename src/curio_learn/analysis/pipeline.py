"""Orchestrates the analysis pipeline (steps 2-10) for one video."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from curio_learn.analysis.ads import remove_ads
from curio_learn.analysis.boundaries import refine_boundaries
from curio_learn.analysis.evaluate import evaluate_sections, section_text
from curio_learn.analysis.outline import Outline, generate_outline
from curio_learn.analysis.playlist import PlaylistConfig, build_playlist, compute_budget, fit_labels, select_sections
from curio_learn.analysis.profiles import learner_brief, spec_for
from curio_learn.analysis.redundancy import similarity_matrix
from curio_learn.analysis.scoring import ScoringConfig, score_sections
from curio_learn.analysis.validation import validate
from curio_learn.config import Settings
from curio_learn.domain import (
    AnalysisStatus,
    LearningPreferences,
    LLMInfo,
    Section,
    SectionEvaluation,
    Transcript,
    VideoAnalysis,
    VideoMetadata,
)
from curio_learn.errors import AnalysisError
from curio_learn.llm.base import LLMProvider
from curio_learn.processing.normalize import boundary_units, build_blocks, clean_cues
from curio_learn.processing.retrieval import blocks_key

log = logging.getLogger(__name__)

Progress = Callable[[str], None]
SaveCheckpoint = Callable[[dict], None]

# The LLM steps, in order. A checkpoint holds the state after the last one finished.
STEPS = ("outline", "boundaries", "ads", "evaluate")
_STEP_NAMES = {
    "outline": "the outline",
    "boundaries": "the section boundaries",
    "ads": "ad removal",
    "evaluate": "the evaluations",
}


class _Resume:
    """Finished steps of an earlier, interrupted run of the same analysis.

    A checkpoint is `{"key", "step", "elapsed_s", "prefs", ...state}`, where `step` is
    the last finished step ("" while the outline is still being written; its finished
    windows are in `partial_outline`). It is used only when its key (models, pipeline
    version, transcript blocks, step switches) matches; the evaluations also need the
    same learning goal, since a queued video's goal can change while it waits.
    """

    def __init__(self, checkpoint: dict | None, key: str, prefs_key: str, save: SaveCheckpoint):
        data = dict(checkpoint) if checkpoint and checkpoint.get("key") == key else {}
        if data.get("step") == "evaluate" and data.get("prefs") != prefs_key:
            data["step"] = data.get("before_evaluate", "")  # keep the outline, redo the evaluations
        if data.get("step") not in STEPS:
            data["step"] = ""
        self.data: dict = data
        self.key, self.prefs_key, self._save = key, prefs_key, save
        self.elapsed_before = float(self.data.get("elapsed_s", 0.0))

    def done(self, step: str) -> bool:
        current = self.data.get("step", "")
        return current in STEPS and STEPS.index(step) <= STEPS.index(current)

    def save(self, step: str, elapsed_s: float, **state) -> None:
        before = self.data.get("step", "") if step == "evaluate" else ""
        self.data = {
            **self.data,
            **state,
            "key": self.key,
            "step": step,
            "prefs": self.prefs_key,
            "elapsed_s": round(self.elapsed_before + elapsed_s, 1),
        }
        if step == "evaluate":
            self.data["before_evaluate"] = before
            self.data.pop("partial_evaluations", None)
        if step == "outline":
            self.data.pop("partial_outline", None)
        self._write(step)

    def outline_windows(self) -> dict | None:
        """Outline windows finished before a pause (the outline step itself wasn't)."""
        return self.data.get("partial_outline")

    def save_outline_windows(self, elapsed_s: float, state: dict) -> None:
        self.data = {
            **self.data,
            "key": self.key,
            "step": self.data.get("step", ""),
            "partial_outline": state,
            "elapsed_s": round(self.elapsed_before + elapsed_s, 1),
        }
        self._write("outline window")

    def partial(self) -> dict[str, SectionEvaluation]:
        """Evaluation batches finished before a pause, for the same goal."""
        d = self.data
        if d.get("partial_prefs") != self.prefs_key:
            return {}
        return {k: SectionEvaluation.model_validate(v) for k, v in d.get("partial_evaluations", {}).items()}

    def save_partial(self, elapsed_s: float, evaluations: dict[str, SectionEvaluation]) -> None:
        """Save the evaluations so far (after each batch), keeping the finished step."""
        self.data = {
            **self.data,
            "partial_evaluations": {k: v.model_dump(mode="json") for k, v in evaluations.items()},
            "partial_prefs": self.prefs_key,
            "elapsed_s": round(self.elapsed_before + elapsed_s, 1),
        }
        self._write("evaluation batch")

    def _write(self, what: str) -> None:
        try:
            self._save(self.data)
        except Exception:  # a lost checkpoint only costs time on the next run
            log.exception("Could not save the %s checkpoint", what)


class AnalysisPipeline:
    def __init__(self, settings: Settings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def _batch_tokens(self) -> int:
        # Claude's long context lets one call see many sections at once.
        return 60000 if self.provider.name == "anthropic" else self.settings.evaluate_batch_tokens

    def _boundary_batch_tokens(self) -> int:
        # Each batch's prompt plus instructions (~1.5k) and the answer must fit the context.
        if self.provider.name == "anthropic":
            return 120000
        return max(4000, min(20000, self.settings.ollama_num_ctx - 8000))

    def _window_tokens(self) -> int:
        if self.provider.name == "anthropic":
            return 150000
        # Leave room in Ollama's fixed context for instructions (~2k) and the outline output (8k).
        return min(self.settings.outline_window_tokens, self.settings.ollama_num_ctx - 10500)

    def run(
        self,
        meta: VideoMetadata,
        transcript: Transcript,
        prefs: LearningPreferences,
        *,
        analysis_id: str | None = None,
        progress: Progress = lambda _: None,
        checkpoint: dict | None = None,
        save_checkpoint: SaveCheckpoint = lambda _: None,
        version: str = "",
    ) -> VideoAnalysis:
        """Analyze one video. After each LLM step the state so far goes to
        `save_checkpoint`; given that `checkpoint` back (same `version`, models and
        transcript), a run that was interrupted skips the steps it had finished."""
        s = self.settings
        started = time.monotonic()
        models = {"outline": s.model_for("outline"), "evaluate": s.model_for("evaluate")}

        progress("Normalizing transcript")
        blocks = build_blocks(transcript.cues)
        if not blocks:
            raise AnalysisError("The transcript is empty after cleaning.")
        log.info("%s: %d cues -> %d blocks", meta.video_id, len(transcript.cues), len(blocks))

        key = "|".join(
            [
                version,
                self.provider.name,
                models["outline"],
                models["evaluate"],
                blocks_key(blocks),
                f"r{int(s.refine_boundaries)}a{int(s.detect_ads)}",
            ]
        )
        resume = _Resume(checkpoint, key, prefs.cache_key(), save_checkpoint)

        def elapsed() -> float:
            return time.monotonic() - started

        def restore() -> tuple[list[Section], list[str], list[str]]:
            d = resume.data
            return [Section.model_validate(x) for x in d["sections"]], list(d["repairs"]), list(d["warnings"])

        if resume.done("outline"):
            d = resume.data
            outline = Outline(overview=d["overview"], key_concepts=d["key_concepts"], sections=[])
            sections, repairs, warnings = restore()
            log.info("%s: resuming after the %s step", meta.video_id, d["step"])
            progress(f"Resuming: kept the finished steps up to {_STEP_NAMES[d['step']]}")
        else:
            try:
                outline, repairs = generate_outline(
                    self.provider,
                    model=models["outline"],
                    meta=meta,
                    blocks=blocks,
                    window_tokens=self._window_tokens(),
                    retries=s.llm_max_retries,
                    progress=progress,
                    done=resume.outline_windows(),
                    on_window=lambda state: resume.save_outline_windows(elapsed(), state),
                )
            except ValueError as exc:
                raise AnalysisError(f"Could not segment the transcript: {exc}") from exc
            sections, warnings = outline.sections, []
            log.info("Outline: %d sections", len(sections))
            resume.save(
                "outline",
                elapsed(),
                overview=outline.overview,
                key_concepts=outline.key_concepts,
                sections=[x.model_dump(mode="json") for x in sections],
                repairs=repairs,
                warnings=warnings,
            )

        def saved(step: str) -> None:
            resume.save(
                step,
                elapsed(),
                sections=[x.model_dump(mode="json") for x in sections],
                repairs=repairs,
                warnings=warnings,
            )

        units = boundary_units(transcript.cues)
        if s.refine_boundaries and not resume.done("boundaries"):
            sections, more_repairs, more_warnings = refine_boundaries(
                self.provider,
                model=models["outline"],
                sections=sections,
                blocks=blocks,
                units=units,
                retries=s.llm_max_retries,
                progress=progress,
                batch_tokens=self._boundary_batch_tokens(),
            )
            repairs += more_repairs
            warnings += more_warnings
            saved("boundaries")
        if s.detect_ads and not resume.done("ads"):
            sections, more_repairs, more_warnings = remove_ads(
                self.provider,
                model=models["outline"],
                sections=sections,
                blocks=blocks,
                units=units,
                retries=s.llm_max_retries,
                progress=progress,
            )
            repairs += more_repairs
            warnings += more_warnings
            saved("ads")

        if resume.done("evaluate"):
            d = resume.data
            evaluations = {k: SectionEvaluation.model_validate(v) for k, v in d["evaluations"].items()}
            warnings += d["eval_warnings"]
        else:
            done = resume.partial()
            if done:
                progress(f"Resuming: kept the evaluations of {len(done)} section(s)")
            evaluations, eval_warnings = evaluate_sections(
                self.provider,
                model=models["evaluate"],
                title=meta.title,
                brief=learner_brief(prefs),
                sections=sections,
                blocks=units,
                batch_tokens=self._batch_tokens(),
                retries=s.llm_max_retries,
                progress=progress,
                done=done,
                on_batch=lambda evs: resume.save_partial(elapsed(), evs),
            )
            resume.save(
                "evaluate",
                elapsed(),
                evaluations={k: v.model_dump(mode="json") for k, v in evaluations.items()},
                eval_warnings=eval_warnings,
            )
            warnings += eval_warnings

        progress("Scoring segments and detecting redundancy")
        sim = similarity_matrix([section_text(sec, units) for sec in sections])
        scoring_cfg = ScoringConfig(
            weights=s.weights.as_dict(),
            threshold_essential=s.threshold_essential,
            threshold_recommended=s.threshold_recommended,
            threshold_optional=s.threshold_optional,
        )
        scored = score_sections(sections, evaluations, prefs, scoring_cfg, sim)

        progress("Building the learning playlist")
        pl_cfg = PlaylistConfig(
            budget_fraction=s.budget_fraction,
            budget_min_s=s.budget_min_minutes * 60,
            budget_max_s=s.budget_max_minutes * 60,
            merge_gap_s=s.merge_gap_s,
        )
        budget = compute_budget(
            meta.duration_s, pl_cfg, target_minutes=prefs.target_minutes, fraction=spec_for(prefs).budget_fraction
        )
        chosen = select_sections(scored, budget, pl_cfg)
        scored = fit_labels(scored, chosen, budget)
        playlist, others = build_playlist(
            scored, duration_s=meta.duration_s, budget_s=budget, config=pl_cfg, chosen=chosen
        )

        progress("Validating results")
        report = validate(
            duration_s=meta.duration_s,
            blocks=blocks,
            sections=sections,
            playlist=playlist,
            others=others,
            cue_starts={c.start_s for c in clean_cues(transcript.cues)},
            cue_ends={c.end_s for c in clean_cues(transcript.cues)},
        )
        report.repairs = repairs + report.repairs
        report.warnings = warnings + report.warnings
        report.ok = not report.warnings

        return VideoAnalysis(
            id=analysis_id or uuid.uuid4().hex[:12],
            video=meta,
            preferences=prefs,
            status=AnalysisStatus.DONE,
            progress="Done",
            overview=outline.overview,
            key_concepts=outline.key_concepts,
            sections=scored,
            segments=playlist.segments + others,
            playlist=playlist,
            validation=report,
            llm=LLMInfo(provider=self.provider.name, models=models),
            transcript_language=transcript.language,
            transcript_generated=transcript.is_generated,
            elapsed_s=round(resume.elapsed_before + elapsed(), 1),
        )
