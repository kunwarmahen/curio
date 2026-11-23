"""Orchestrates the analysis pipeline (steps 2-10) for one video."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from curio_learn.analysis.ads import remove_ads
from curio_learn.analysis.boundaries import refine_boundaries
from curio_learn.analysis.evaluate import evaluate_sections, section_text
from curio_learn.analysis.outline import generate_outline
from curio_learn.analysis.playlist import PlaylistConfig, build_playlist, compute_budget
from curio_learn.analysis.profiles import learner_brief, spec_for
from curio_learn.analysis.redundancy import similarity_matrix
from curio_learn.analysis.scoring import ScoringConfig, score_sections
from curio_learn.analysis.validation import validate
from curio_learn.config import Settings
from curio_learn.domain import (
    AnalysisStatus,
    LearningPreferences,
    LLMInfo,
    Transcript,
    VideoAnalysis,
    VideoMetadata,
)
from curio_learn.errors import AnalysisError
from curio_learn.llm.base import LLMProvider
from curio_learn.processing.normalize import boundary_units, build_blocks, clean_cues

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class AnalysisPipeline:
    def __init__(self, settings: Settings, provider: LLMProvider):
        self.settings = settings
        self.provider = provider

    def _batch_tokens(self) -> int:
        # Claude's long context lets one call see many sections at once.
        return 60000 if self.provider.name == "anthropic" else self.settings.evaluate_batch_tokens

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
    ) -> VideoAnalysis:
        s = self.settings
        started = time.monotonic()
        models = {"outline": s.model_for("outline"), "evaluate": s.model_for("evaluate")}

        progress("Normalizing transcript")
        blocks = build_blocks(transcript.cues)
        if not blocks:
            raise AnalysisError("The transcript is empty after cleaning.")
        log.info("%s: %d cues -> %d blocks", meta.video_id, len(transcript.cues), len(blocks))

        try:
            outline, repairs = generate_outline(
                self.provider,
                model=models["outline"],
                meta=meta,
                blocks=blocks,
                window_tokens=self._window_tokens(),
                retries=s.llm_max_retries,
                progress=progress,
            )
        except ValueError as exc:
            raise AnalysisError(f"Could not segment the transcript: {exc}") from exc
        sections = outline.sections
        log.info("Outline: %d sections", len(sections))

        units = boundary_units(transcript.cues)
        warnings: list[str] = []
        if s.refine_boundaries:
            sections, more_repairs, more_warnings = refine_boundaries(
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
        if s.detect_ads:
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
        playlist, others = build_playlist(scored, duration_s=meta.duration_s, budget_s=budget, config=pl_cfg)

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
            elapsed_s=round(time.monotonic() - started, 1),
        )
