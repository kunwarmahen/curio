"""Learning profiles: how a learner's objective shapes prompts, weights and budgets."""

from __future__ import annotations

from dataclasses import dataclass, field

from curio_learn.domain import LearningPreferences, LearningProfile, SectionKind


@dataclass(frozen=True)
class ProfileSpec:
    label: str
    brief: str
    # Multipliers applied to the configured rubric weights.
    weight_multipliers: dict[str, float] = field(default_factory=dict)
    # Score adjustments (points out of 100) by section kind, added to the base kind priors.
    kind_bias: dict[SectionKind, float] = field(default_factory=dict)
    budget_fraction: float | None = None
    # Up to this many points for a content section that later teaching sections build
    # on (directly or through others), in proportion to how much of the video does.
    foundation_bonus: float = 0.0


PROFILES: dict[LearningProfile, ProfileSpec] = {
    LearningProfile.GENERAL: ProfileSpec(
        label="General",
        brief="A curious learner who wants the most important ideas of this video in the "
        "least time. Balance foundations, insight and practical examples.",
    ),
    LearningProfile.BEGINNER: ProfileSpec(
        label="Beginner",
        brief="A beginner new to this subject. Prioritize clear foundational explanations, "
        "definitions and intuitive examples. Introductory context and recaps can be "
        "valuable; very advanced detail is less useful.",
        weight_multipliers={"educational_value": 1.2, "depth": 0.6, "novelty": 0.8},
        kind_bias={SectionKind.INTRO: 6, SectionKind.RECAP: 6, SectionKind.EXAMPLE: 3},
        # The model rates "what is X and how its parts fit" below hands-on sections
        # (Kubernetes: 77-82 vs. 85-99), and asking it to didn't help; the sections
        # that everything else builds on are the beginner's foundations.
        foundation_bonus=12.0,
    ),
    LearningProfile.TECHNICAL: ProfileSpec(
        label="Technical",
        brief="A practitioner who builds things. Prioritize implementation details, "
        "architecture, code, algorithms, demonstrations and concrete numbers. Skip "
        "motivational material and high-level overviews they already know.",
        weight_multipliers={"practical": 1.5, "depth": 1.3, "educational_value": 0.9},
        kind_bias={SectionKind.DEMO: 6, SectionKind.EXAMPLE: 3, SectionKind.INTRO: -4},
    ),
    LearningProfile.EXECUTIVE: ProfileSpec(
        label="Executive",
        brief="A decision maker with little time. Prioritize strategy, business implications, "
        "trade-offs, key decisions, results and conclusions. Low-level implementation "
        "detail is rarely needed.",
        weight_multipliers={"relevance": 1.5, "practical": 0.8, "depth": 0.7},
        kind_bias={SectionKind.DEMO: -6, SectionKind.RECAP: 4},
        budget_fraction=0.20,
    ),
    LearningProfile.DEEP_DIVE: ProfileSpec(
        label="Deep Dive",
        brief="An expert who wants depth. Prioritize advanced concepts, subtle arguments, "
        "novel insights, contrarian viewpoints and detailed technical discussion. A "
        "speaker's own framework or mental model for the field (a new way to think about "
        "it) is a novel insight even without technical detail. Basic explanations are "
        "less valuable.",
        weight_multipliers={"depth": 1.6, "novelty": 1.3, "educational_value": 0.9},
        kind_bias={SectionKind.QA: 3, SectionKind.INTRO: -4, SectionKind.RECAP: -4},
        budget_fraction=0.45,
    ),
}


def spec_for(prefs: LearningPreferences) -> ProfileSpec:
    return PROFILES[prefs.profile]


def learner_brief(prefs: LearningPreferences) -> str:
    spec = spec_for(prefs)
    brief = f"Learner profile: {spec.label}. {spec.brief}"
    if prefs.custom_objective.strip():
        brief += (
            "\nThe learner's own objective (this takes priority when judging relevance): "
            f'"{prefs.custom_objective.strip()}"'
            "\nJudge novelty and relevance separately. If the objective says what the learner "
            "already knows, a plain explanation of that scores low on novelty however well it "
            "is made. Relevance is only whether the section gives what the objective asks for "
            "(for example pitfalls, failure cases, lessons, frameworks, or what they may have "
            "missed); a section that gives that scores high on relevance even when its topic "
            "is familiar, and so does its novelty if it goes beyond the basics."
        )
    return brief


def effective_weights(base: dict[str, float], prefs: LearningPreferences) -> dict[str, float]:
    spec = spec_for(prefs)
    weights = {k: v * spec.weight_multipliers.get(k, 1.0) for k, v in base.items()}
    if prefs.custom_objective.strip():
        weights["relevance"] *= 2.0  # an explicit objective makes relevance matter more
    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}
