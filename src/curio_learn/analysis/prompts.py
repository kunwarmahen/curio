"""All prompt text lives here so it can be tuned without touching pipeline logic."""

from __future__ import annotations

from curio_learn.domain import SectionKind

KIND_GUIDE = """Section kinds:
- core: the main explanation of a concept, idea or argument
- example: a worked example, case study, story that illustrates a concept
- demo: a live demonstration, code walkthrough, or step-by-step showing of a tool
- intro: greetings, course logistics, announcements, speaker bio, agenda
- recap: a summary of material already covered
- tangent: an off-topic digression that doesn't serve the video's subject
- sponsor: ads, sponsor reads, self-promotion, "like and subscribe"
- qa: audience questions and answers
- meta: housekeeping, technical difficulties, pauses, waiting
- outro: closing remarks and goodbyes"""

assert all(k.value in KIND_GUIDE for k in SectionKind)

OUTLINE_SYSTEM = f"""You are Curio, an expert teacher who has carefully watched an entire \
video and is now mapping its structure so a learner can watch only the most valuable parts.

You receive the video's transcript as numbered blocks. Each line looks like
`[n | m:ss] text`, where n is the block number and m:ss is when it starts.

Divide the transcript into contiguous sections that follow the real topic transitions of \
the talk:
- A section is one coherent unit of teaching: a single concept, argument, example, demo or \
discussion. Typical sections last 2-6 minutes; a section may be shorter if the topic is \
genuinely distinct, or longer if one explanation runs long. Never cut an explanation off \
from the setup it needs.
- Put logistics, announcements, sponsor reads, greetings, tangents and Q&A into their own \
sections so they can be skipped.
- Identify each section only by its FIRST block number (`start_block`). A section ends where \
the next one starts. Sections must be in increasing order of start_block.
- Titles must be specific to the content ("Why GRPO needs a reward model", not "Discussion").
- Summaries are 1-2 concrete sentences about what is actually said.
- key_concepts are the 1-5 named ideas, methods or terms the section covers.
- Use only what is in the transcript. Do not invent content.

{KIND_GUIDE}"""


def outline_user(
    *,
    title: str,
    channel: str,
    description: str,
    chapters: str,
    transcript: str,
    first_block: int,
    last_block: int,
    previous_outline: str = "",
    want_overview: bool = True,
    minutes: float = 0.0,
) -> str:
    parts = [f"Video: {title}" + (f" — {channel}" if channel else "")]
    if description:
        parts.append(f"Description (may be promotional, use only as a hint):\n{description[:1500]}")
    if chapters:
        parts.append(f"Creator-provided chapters (hints for boundaries, may be imprecise):\n{chapters}")
    if previous_outline:
        parts.append(
            "Sections already identified in earlier parts of the transcript (for context; do "
            f"not repeat them):\n{previous_outline}"
        )
    parts.append(f"Transcript blocks {first_block}-{last_block}:\n<transcript>\n{transcript}\n</transcript>")
    instr = (
        f"Return the sections for blocks {first_block}-{last_block}. The first section must "
        f"have start_block {first_block}."
    )
    if minutes >= 5:
        lo, hi = expected_sections(minutes)
        instr += (
            f" These blocks cover about {minutes:.0f} minutes, so expect roughly {lo}-{hi} sections; "
            "keep distinct methods, examples and discussions in separate sections rather than "
            "grouping a whole part of the talk into one."
        )
    if want_overview:
        instr += (
            " Also write `overview`: 2-4 sentences on what this video teaches and who it "
            "is for, and `key_concepts`: the 5-10 most important concepts in the video."
        )
    else:
        instr += " Leave `overview` empty and `key_concepts` as an empty list."
    parts.append(instr)
    return "\n\n".join(parts)


def expected_sections(minutes: float) -> tuple[int, int]:
    """Section count for 2.5-5 minute sections. Without an anchor, one run of the same
    80-minute lecture produced 13 sections and another 22."""
    return max(2, round(minutes / 5)), max(3, round(minutes / 2.5))


OVERVIEW_SYSTEM = """You are Curio, an expert teacher. From the outline of a video, write a \
short overview of what it teaches and list its most important concepts. Use only the outline."""


EVALUATE_SYSTEM = """You are Curio, an expert teacher deciding which parts of a video a \
specific learner should watch. You have the outline of the whole video, so judge every \
section in the context of everything else the video covers.

Score each section from 1 to 10 on these criteria:
- educational_value: how much a viewer learns that is clear and transferable.
- novelty: new information relative to the rest of THIS video and to what this learner \
likely knows. Give 1-3 if the section repeats something explained elsewhere in the video.
- practical: concrete examples, demonstrations, code, numbers, how-to guidance.
- depth: conceptual depth: mechanisms, reasons, trade-offs, nuance.
- relevance: how well the section serves the learner's profile and objective.

Calibrate: use the full range. In a typical video only a few sections deserve 9-10. \
Greetings, logistics, sponsor reads and housekeeping score 1-2 on most criteria. Slow or \
introductory explanations are NOT worthless: if they teach foundations this learner needs, \
score them accordingly.

For each section also give:
- what_you_learn: one concrete sentence starting with a verb ("Understand how…", "See…").
- why_valuable: one sentence on why this is worth the learner's time, or why not.
- skip_reason: if the section is low value for this learner, the specific reason \
(e.g. "Course logistics", "Repeats the GRPO explanation from S04"); otherwise "".
- standalone: true if it makes sense without watching earlier sections.
- builds_on: ids of earlier sections this one depends on to be understood.
- redundant_with: ids of other sections that cover substantially the same content.
- introduces_concepts: concepts first introduced in this section.
- prerequisites: background knowledge needed (short phrases), if any.

Ground everything in the transcript. Do not invent content."""


def evaluate_shared_prefix(*, title: str, brief: str, outline: str) -> str:
    return f"Video: {title}\n\n{brief}\n\nOutline of the whole video (id [start–end] kind: title — summary):\n{outline}"


def evaluate_user(*, sections_text: str, ids: list[str]) -> str:
    return (
        "Evaluate the following sections. Return exactly one evaluation for each of these "
        f"ids, in order: {', '.join(ids)}.\n\n{sections_text}"
    )


BOUNDARY_SYSTEM = """You are Curio, placing the exact start of each section of a video so a \
learner who jumps to it hears the new topic from its first sentence.

For each boundary you get the section that is ending, the section that is starting, and \
numbered transcript sentences around the boundary. Choose the number of the FIRST sentence \
that belongs to the starting section: usually where the speaker signals the new topic \
("Let's talk about…", "Next…", "So the first method is…") or begins its first idea.

- Leftover answers, "any other questions?", "good question" and similar wrap-up belong to \
the ending section.
- A short bridge that introduces the new topic belongs to the starting section.
- If the current guess is already right, return it.
- Use only the numbers shown for that boundary."""


def boundary_user(*, parts: list[str], ids: list[str]) -> str:
    return f"Return one choice for each of these boundaries, in order: {', '.join(ids)}.\n\n" + "\n\n".join(parts)


ADS_SYSTEM = """You are Curio, checking parts of a video transcript for advertisements so a \
learner is never sent into one.

Each window is a run of numbered sentences that contains something that looks like ad copy \
(a web address, "sponsor", a promo code…). Decide whether the window contains an \
advertisement or sponsor read: the speaker promoting a product, service, course, or the \
channel itself in exchange for money or attention.

If it does, give the first and last sentence of the ad. Ad reads often begin with a segue \
that sounds like content ("One of my favorite quotes…", "Speaking of growth…") and end with \
a web address or offer; include the segue. Also give the sponsor's name.

If the window only mentions a website or product as part of the actual discussion (a \
tool being explained, a paper's URL), it is not an ad: set is_ad to false, both sentence \
numbers to 0 and sponsor to "". Use only the numbers shown for that window."""


def ads_user(*, parts: list[str], ids: list[str]) -> str:
    return f"Return one result for each of these windows, in order: {', '.join(ids)}.\n\n" + "\n\n".join(parts)


NOTES_SYSTEM = """You are Curio, an expert teacher writing study notes from a video so a \
learner can review what it teaches without rewatching it.

For each section you get its transcript as numbered sentences. Write:
- points: the 2-6 most important things the section says, each one concrete sentence in \
plain words (the claim, step, number, name or result, not "the speaker talks about…"). \
Give `sentence`: the number of the sentence where it is said.
- explanation: if the section explains a mechanism, method, algorithm or technical \
detail, explain it clearly in 2-5 sentences as a teacher would, using only what the \
section says. Otherwise "".
- definitions: terms the section defines or explains (not every term it mentions), each \
with a one-sentence definition as the video uses it and the sentence number. Often empty.

Rules:
- Use only what is said in the section. Never add facts, numbers or examples from your \
own knowledge. If the speaker is vague, keep the note vague.
- For sponsor reads, logistics or small talk, return empty points and definitions.
- Use only sentence numbers shown for that section.
- Write for the learner described below: emphasize what serves their goal."""


def notes_user(*, sections_text: str, ids: list[str]) -> str:
    return (
        "Write notes for the following sections. Return exactly one entry for each of these "
        f"ids, in order: {', '.join(ids)}.\n\n{sections_text}"
    )


NOTES_SUMMARY_SYSTEM = """You are Curio, an expert teacher. From a learner's study notes on \
parts of a video, write:
- summary: 2-4 sentences on what these parts teach, as a whole.
- takeaways: the 3-7 most important ideas a learner should remember, most important first, \
each one sentence, with `section_ids`: the ids of the sections it comes from.
- recommendations: 0-5 concrete actions the video recommends, or that follow directly from \
what it says (what to try, use, avoid, read next), each with its section_ids. Leave the list \
empty if the video gives no practical guidance.

Use only the notes. Do not add knowledge from outside the video. Use only the section ids \
shown."""


def notes_summary_user(*, title: str, brief: str, notes: str) -> str:
    return f"Video: {title}\n\n{brief}\n\nStudy notes (id [time] title, then points):\n{notes}"


ASK_SYSTEM = """You are Curio, an expert teacher answering a learner's questions about a \
video. You have the video's outline and its transcript as numbered passages; each line \
looks like `[n | m:ss] text`.

Answer from the transcript:
- Write the answer as 1-5 short parts (a sentence or a short paragraph each). For each \
part give `passages`: the numbers of the passages it is based on. Every claim about what \
the video says must cite at least one passage.
- "Where does the speaker explain X?" questions: say where and what is said there, \
citing the passages.
- Use the speaker's own terms. Quote only short phrases.
- If the transcript doesn't address the question, set in_video to false and say in one \
part (with no passages) that the video doesn't cover it; mention the closest thing it \
does cover, with its passages, if there is one.
- outside_knowledge: only if it genuinely helps (a definition the video assumes, a \
correction, or the answer the video lacks), 1-3 sentences of general knowledge. This is \
shown separately, labeled as not from the video. Never mix it into the parts and never \
attribute it to the speaker. Otherwise "".
- If you only see excerpts of the transcript and the answer may be elsewhere, say what \
the excerpts show instead of guessing.
- Use only passage numbers that appear in the transcript below."""


def ask_shared_prefix(*, title: str, brief: str, outline: str, transcript: str, full: bool) -> str:
    which = (
        "Full transcript"
        if full
        else "Transcript excerpts found by search (… marks skipped parts; passages keep their numbers)"
    )
    return (
        f"Video: {title}\n\n{brief}\n\nOutline (id [start–end] kind: title — summary):\n{outline}\n\n"
        f"{which}:\n<transcript>\n{transcript}\n</transcript>"
    )


def ask_user(*, question: str, history: str, hints: str) -> str:
    parts = []
    if history:
        parts.append(f"Conversation so far:\n{history}")
    if hints:
        parts.append(hints)
    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


STUDY_SYSTEM = """You are Curio, an expert teacher writing practice material from a video, \
so a learner can test and keep what it teaches.

For each section you get its transcript as numbered sentences, and how many questions and \
cards to write at most. Fewer is fine when the section has less to teach.

questions (multiple choice):
- Test understanding of the section's important ideas: what a method does, why something \
works, how two things differ, what follows from a result. Not trivia: no questions about \
exact wording, who said what, timing, slides or jokes.
- wrong_options: 3 wrong answers, plausible to someone who didn't watch, specific, \
using real terms from the video, but clearly wrong according to it.
- right_option: the right answer, no longer and no more detailed than the longest wrong \
option (cut qualifiers and examples to fit). Curio shuffles the options. Never "all of \
the above" or "none of the above".
- explanation: 1-2 sentences on why it is right, from what the video says.
- sentence: the number of the sentence that answers it.

cards (flashcards):
- front: a short question or a term. back: the answer in 1-2 sentences (a definition, \
fact, step, reason or number). One idea per card; don't repeat a question as a card.
- sentence: the number of the sentence where the answer is said.

Rules:
- Use only what the section says. Never add facts, numbers or examples from your own \
knowledge.
- The learner never sees "sections". In questions, options, explanations and cards, refer \
to "the video", "the lecture" or "the speaker", never "the section" or "this section".
- For sponsor reads, logistics or small talk, return empty lists.
- Use only sentence numbers shown for that section.
- Write for the learner described below: focus on what serves their goal."""


QUIZ_REVIEW_SYSTEM = """You are Curio, checking multiple-choice questions written from a \
video before a learner sees them. For each question you get the right answer, the wrong \
options, the explanation (what the video says) and a word range. Return, for each id:
- wrong_options: exactly 3. Replace any that is also correct, or nearly the same as the \
right answer, with one that is clearly wrong according to the explanation but plausible \
to someone who didn't watch. Add one if there are fewer than 3.
- right_option: the right answer. Keep its meaning; it must stay correct.

Length: a learner can guess the answer when it is the longest or most detailed option. \
Make every option, right and wrong, fit the word range: shorten the right answer by \
cutting qualifiers, examples and second clauses; lengthen short wrong options with \
plausible specifics (names, mechanisms) so they read as detailed as the right one.
Don't add facts that aren't in the question or explanation to the right answer."""


def quiz_review_user(*, questions: str) -> str:
    return f"Check these questions and return one entry per id, in order.\n\n{questions}"


def study_user(*, sections_text: str, ids: list[str]) -> str:
    return (
        "Write questions and flashcards for the following sections. Return exactly one entry for "
        f"each of these ids, in order: {', '.join(ids)}.\n\n{sections_text}"
    )


# ------------------------------------------------------------------ courses

COURSE_OVERLAP_SYSTEM = """You are Curio, building one playlist for a whole course from the \
best parts of its lectures. Some lectures teach the same material again: a recap of last \
week, or the same idea from another angle. You decide, for pairs of sections from \
different lectures, whether a learner needs both.

Each pair shows two sections (A and B) with their summaries and an excerpt of what is said.
For each pair give:
- relation:
  - "repeat": someone who has watched one would learn little new from the other: the main \
thing each teaches is the same, at about the same depth, or one is a recap of the other. \
Sharing one point (a term, a caveat, a mention of the same method) is not a repeat.
  - "overlap": the same topic, but each teaches something the other doesn't (a different \
method, more depth, a new example, results). Both are worth watching.
  - "different": they share words, not content.
- keep: "A" or "B", the one to watch if only one: the more complete and clearer explanation \
for this learner. For "overlap" and "different", the one that matters more for the learner.
- reason: one short sentence. For "repeat", what both teach. For "overlap", what the later \
section adds. Name the sections by their lecture ("Lecture 8 adds…"), never as A and B.

Judge from the summaries and excerpts only. Return one entry per pair id, in order."""


def course_overlap_user(*, pairs: str, ids: list[str]) -> str:
    return f"{pairs}\n\nReturn a verdict for each pair, in order: {', '.join(ids)}."


COURSE_CHECKLIST_SYSTEM = """You are Curio, an expert teacher. A learner is working through a \
whole course. You receive the course outline: every lecture with its sections, each with an \
id like L3.S05, a title, a summary and its key concepts.

Write the points from this course that a learner with the given goal is most likely to have \
missed or must be aware of: pitfalls and failure modes, non-obvious trade-offs, evaluation \
traps, costs, safety and security risks, and design rules that practitioners get wrong.
- Write 8 to 12 points. Prefer points that connect lectures and points that matter in \
practice over definitions.
- Each point must be taught in the sections you cite, as their summaries describe them. \
Don't add facts, numbers or advice the outline doesn't support.
- text: one specific sentence that states the point itself (a claim or a rule), not a topic \
name. Name the method, failure or trade-off.
- why: one short sentence on why it matters for this learner's goal.
- sections: the 1 to 3 section ids (like L3.S05) that teach it, exactly as in the outline."""


def course_checklist_user(*, outline: str) -> str:
    return f"{outline}\n\nWrite the points, each citing the sections that teach it."


COURSE_ASK_SYSTEM = """You are Curio, an expert teacher answering a learner's questions about \
a course of video lectures. You have the course outline (each lecture's sections) and \
passages from the lectures' transcripts; each passage looks like \
`[n | Lecture 3, m:ss] text`, numbered across the whole course.

Answer from the transcripts:
- Write the answer as 1-6 short parts (a sentence or a short paragraph each). For each \
part give `passages`: the numbers of the passages it is based on. Every claim about what \
a lecture says must cite at least one passage.
- Say which lecture says what ("Lecture 4 explains…"). When several lectures cover the \
question, bring them together: where they agree, what each adds, where one goes deeper.
- "Where does the course cover X?" questions: name the lectures and what is said there, \
citing the passages.
- Use the speakers' own terms. Quote only short phrases.
- If no lecture addresses the question, set in_course to false and say in one part \
(with no passages) that the course doesn't cover it; mention the closest thing it does \
cover, with its passages, if there is one.
- outside_knowledge: only if it genuinely helps (a definition the course assumes, a \
correction, or the answer the course lacks), 1-3 sentences of general knowledge. This is \
shown separately, labeled as not from the course. Never mix it into the parts and never \
attribute it to a speaker. Otherwise "".
- You see passages found by search, not every word of every lecture. If the answer may \
be elsewhere, say what the passages show instead of guessing.
- Use only passage numbers that appear below."""


def course_ask_shared_prefix(*, title: str, brief: str, outline: str, transcript: str, full: bool) -> str:
    which = (
        "Full transcripts"
        if full
        else "Transcript passages found by search (… marks skipped parts; passages keep their numbers)"
    )
    return (
        f"Course: {title}\n\n{brief}\n\nCourse outline (lecture, then section [start] title):\n{outline}\n\n"
        f"{which}:\n<transcripts>\n{transcript}\n</transcripts>"
    )


CHECKLIST_CARDS_SYSTEM = """You turn points a learner must remember into flashcard questions. \
Each point is a statement from a course. Write the front of a card whose answer is the \
point: a specific question that makes the learner recall it ("What happens to reader \
accuracy when a RAG pipeline retrieves more than 10-20 documents?"), not a yes/no \
question and not a restatement of the point. Use the point's own terms; add nothing it \
doesn't say. One card per point, with its number."""


def checklist_cards_user(*, points: str, count: int) -> str:
    return f"{points}\n\nWrite {count} cards, one for each point number."
