# Curio — Architecture

> Curio finds the most valuable learning segments inside long-form video and
> turns them into a curated playlist. Guiding metric: **knowledge acquired per
> minute watched.**

## 1. System overview

```
                ┌───────────────────────────── Frontends ─────────────────────────────┐
                │  React + Vite web app (web/)                                         │
                └──────────────┬──────────────────────────────┬───────────────────────┘
                               │ HTTP/JSON                     │ in-process (CLI)
                               ▼                               ▼
                        ┌──────────────────────────────────────────────┐
                        │ CurioService (curio.service)                 │◄── FastAPI (curio.api)
                        │  analyze(url, preferences) · get · recent     │
                        └──────┬──────────────┬──────────────┬─────────┘
                               │              │              │
            ┌──────────────────▼───┐  ┌───────▼────────┐  ┌──▼──────────────────┐
            │ Ingestion            │  │ Analysis        │  │ Storage             │
            │  url → video id      │  │ pipeline (11    │  │ SQLAlchemy/SQLite   │
            │  MetadataProvider    │  │ steps, below)   │  │ videos, transcripts │
            │  TranscriptProvider  │  │                 │  │ analyses, segments  │
            └──────────────────────┘  └───────┬────────┘  └─────────────────────┘
                                              │
                                     ┌────────▼────────┐
                                     │ LLM layer        │
                                     │  LLMProvider     │──► Ollama (local)
                                     │  structured JSON │──► Anthropic Claude
                                     │  retry/validate  │──► (future providers)
                                     └─────────────────┘
```

Key rule: **the intelligence layer (`ingestion`, `processing`, `analysis`,
`llm`) never imports a frontend.** The web app talks only to the FastAPI
contract, and FastAPI and the CLI both call `CurioService`, so the frontend can
change without touching analysis code.

## 2. Repository layout

```
curio/
├── pyproject.toml            uv-managed; Python 3.12+
├── .env.example              every setting, no secrets
├── Containerfile             image: web app build + API runtime
├── curio.sh                  Podman build/start/stop/run/status/logs/analyze
├── README.md                 user guide: install, run, use the app, settings
├── docs/
│   ├── ARCHITECTURE.md       this file
│   └── PROGRESS.md           development log and milestone status
├── src/curio/
│   ├── config.py             pydantic-settings, env-driven
│   ├── logging_setup.py
│   ├── domain.py             Pydantic domain models (single source of truth)
│   ├── service.py            CurioService façade used by every frontend
│   ├── overnight.py          overnight queue: hours window, GPU idle check, worker
│   ├── gpu.py                GPU page: cards, programs on the GPU, Ollama's models
│   ├── ingestion/
│   │   ├── url.py            YouTube URL → 11-char video id
│   │   ├── metadata.py       MetadataProvider protocol + yt-dlp impl (no download)
│   │   ├── playlist.py       YouTube playlists (courses) via yt-dlp, flat; lecture order
│   │   └── transcripts.py    TranscriptProvider protocol + youtube-transcript-api impl
│   ├── processing/
│   │   └── normalize.py      cue cleanup, de-overlap, merge into timestamped blocks
│   ├── llm/
│   │   ├── base.py           LLMProvider protocol, structured-output + retry loop
│   │   ├── schema.py         Pydantic → strict JSON Schema
│   │   ├── anthropic_provider.py
│   │   ├── ollama_provider.py
│   │   └── factory.py        picks provider/model per task from config
│   ├── analysis/
│   │   ├── profiles.py       Beginner / Technical / Executive / Deep Dive + custom: briefs, weights, kind biases, foundation bonus
│   │   ├── prompts.py        all prompt text in one place
│   │   ├── outline.py        topic segmentation over the full transcript
│   │   ├── evaluate.py       contextual rubric evaluation of each section
│   │   ├── redundancy.py     lexical overlap between sections
│   │   ├── scoring.py        weighted rubric + rule-based adjustments
│   │   ├── playlist.py       time-budgeted selection, merging, ordering
│   │   ├── validation.py     timestamp / coverage / consistency checks
│   │   ├── boundaries.py     sentence-level section starts (step 6b)
│   │   ├── ads.py            cut sponsor reads out of content sections (step 6c)
│   │   ├── notes.py          learning notes, takeaways, definitions, Markdown export
│   │   ├── ask.py            Ask the Video: context selection, grounded answers
│   │   ├── study.py          quizzes and flashcards, spaced-repetition schedule
│   │   ├── course.py         course playlist across lectures: repeats, checklist
│   │   ├── course_ask.py     Ask the course: search across lectures, cited answers
│   │   ├── curriculum.py     curricula: needs across sources, ordering, suggesting sources
│   │   └── pipeline.py       orchestrates the steps, reports progress, saves step checkpoints
│   ├── storage/
│   │   ├── orm.py            SQLAlchemy tables
│   │   └── repository.py     persistence API (cache lookup, recent videos)
│   ├── api/app.py            FastAPI app
│
├── web/                      React + Vite + TypeScript frontend
│   └── src/                  pages (Home, VideoPage, Course, Lists: Courses/Videos/Queue,
│                             Review), TopBar (logo + hidden menu), WatchMap, Player, Ask,
│                             Quiz, Flashcards, api.ts, a small history router
└── tests/                    pytest; no network, LLM mocked
```

## 3. External dependencies and their limitations

| Dependency | Use | Limitations / risk | Mitigation |
|---|---|---|---|
| **youtube-transcript-api** | Read public caption tracks (manual and auto-generated) with timestamps | Unofficial; reads YouTube's web endpoints. Can break when YouTube changes, and YouTube may block IPs that make many requests (cloud IPs especially). Gets nothing for videos with captions disabled, age-restricted, members-only or private. | Behind the `TranscriptProvider` protocol; clear typed errors (`TranscriptUnavailable`, `VideoUnavailable`, `RateLimited`); transcripts cached in SQLite so each video is fetched once. Proxy support can be added in the provider. |
| **yt-dlp** | Metadata only (`skip_download=True`): title, channel, duration, description, thumbnail, chapters | Unofficial; same breakage risk; slower (~2 s). | Behind `MetadataProvider`; a YouTube Data API v3 provider can be added later. |
| **YouTube IFrame embed** | Playback from a timestamp | Embedding can be disabled per video; segment end markers need the IFrame JS API (the web app uses the IFrame Player API to stop at a part's end and auto-advance). | Always show "Open on YouTube at mm:ss" links as fallback. |
| **Ollama** | Local LLM (default without a Claude key): `qwen3.8-64k` | Slow on long inputs (minutes per video); smaller models are weaker at strict JSON. 64k context fits ~2.5 h of speech. | `format=<JSON schema>` constrained decoding + Pydantic validation + retry with the error fed back; long transcripts processed in overlapping windows. |
| **Anthropic Claude** | Cloud LLM: `claude-opus-5-5` | Needs `ANTHROPIC_API_KEY`; costs per video (about 20–40k input tokens for 90 min). Opus 5.5 may decline a request (`stop_reason: refusal`). | Structured outputs (`output_config.format`), streaming for long calls, server-side refusal fallback enabled. |
| **SQLite** | Storage | Single writer. Fine for a local MVP. | SQLAlchemy keeps PostgreSQL a config change (`CURIO_DATABASE_URL`). |

Copyright: Curio never downloads, stores, or re-hosts video or audio. It
stores caption text for analysis and links back to YouTube for playback.

## 4. Data models (`curio/domain.py`)

```
VideoMetadata   video_id, title, channel, duration_s, description, thumbnail_url,
                url, chapters[Chapter(title, start_s, end_s)]
TranscriptCue   start_s, duration_s, text                       (raw, as fetched)
Transcript      video_id, language, is_generated, source, cues[]
Block           id "B0001", start_s, end_s, text                (normalized ~20–45 s unit)
LearningPreferences  profile (beginner|technical|executive|deep_dive|general),
                     custom_objective?, target_minutes?
Section         id "S01", start_block, end_block, start_s, end_s, title, summary,
                kind (core|example|demo|intro|recap|tangent|sponsor|qa|meta|outro),
                key_concepts[]
SectionEvaluation  section_id, rubric{educational_value, novelty, practical,
                depth, relevance} (1–10 each), standalone, introduces_concepts[],
                builds_on[section ids], redundant_with[section ids],
                prerequisites[], what_you_learn, why_valuable, skip_reason?
ScoreBreakdown  weighted_rubric, adjustments[(rule, delta, reason)], final (0–100)
LearningSegment id, section_ids[], title, start_s, end_s, duration_s, summary,
                what_you_learn, why_valuable, prerequisites[], importance
                (essential|recommended|optional|skip), score, breakdown, kind
Playlist        segments[] (chronological), total_s, original_s, time_saved_s,
                coverage_estimate (AI estimate, 0–1), skipped[] (with reasons)
VideoAnalysis   id, video, preferences, overview, key_concepts[], sections[],
                evaluations[], segments[], playlist, validation{warnings[]},
                llm{provider, model}, created_at, status
LearningNotes   id, analysis_id, scope (playlist|video|selected), segment_ids[],
                status, progress, summary, takeaways[Takeaway], sections[SectionNotes],
                recommendations[Takeaway], glossary[] (computed), watch_s, warnings[]
SectionNotes    section_id, title, start_s, end_s, points[NotePoint(text, start_s)],
                explanation, definitions[Definition(term, definition, start_s)]
Takeaway        text, section_ids[], start_s (start of its earliest section)
Answer          id, analysis_id, question, parts[AnswerPart(text, citations[start_s])],
                in_video, outside_knowledge (not from the video), context
                (full|excerpts), search (keyword|keyword+semantic), warnings[]
StudySet        id, analysis_id, scope, segment_ids[], status, progress,
                questions[QuizQuestion(id, question, options[], answer, explanation,
                section_id, start_s)], cards[Flashcard(id, front, back, section_id,
                start_s)], warnings[]
QuizAttempt     id, study_id, answers{question id: option}, correct[], score, total
CardState       study_id, card_id, box (0 = new, 1-6), due_at, reviews, lapses
Course          id, title, channel, playlist_id, preferences, lectures[CourseLecture
                (position, label, video_id, title, duration_s, state, analysis_id,
                pending_id, error)], plan (newest CoursePlan), plan_stale,
                custom_parts[] (the learner's own playlist), curriculum?
CoursePlan      id, course_id, status, budget_s, total_s, original_s, analysis_ids[],
                parts[CoursePart], repeats[CourseRepeat], pairs[CoursePair],
                checklist[ChecklistItem], needs[CourseNeed], order (lectures|sections),
                warnings[]
CourseNeed      section (CourseRef, in the playlist), prerequisite, taught_by
                (CourseRef), status (in_playlist|added|untaught), made_room[CourseRef],
                why (untaught: worth|share|repeat|skip)
CoursePart      key "<video_id>@<start>", lecture, video_id, analysis_id,
                section_ids[], title, start_s, end_s, what_you_learn, importance,
                score, also_in[CourseRef]
CourseSectionChoice  a section for the playlist editor: curio_pick, picked, pinned,
                watched
CurriculumInfo  goal, order (lectures|sections), auto_order, notes[] (why things moved)
CurriculumCandidate  kind (course|video), id, title, detail, suggested, reason
```

All timestamps derive from caption cues. The LLM refers to **block numbers** (outline)
and **sentence numbers** (boundary refinement, ads, notes), never to raw seconds, so it cannot
invent a timestamp: code converts the numbers to the original cue times.

## 5. API contract (FastAPI, `/api/v1`)

| Method | Path | Body / result |
|---|---|---|
| `GET`  | `/health` | `{status, llm_provider, model}` |
| `POST` | `/analyses` | `{url, preferences, force?, queue?}` → `202 {analysis_id, status}`; runs in background, or with `queue` waits in the overnight queue (`status: queued`). Returns the cached, queued or running analysis if one exists for the same video + preferences + model; without `queue`, a matching queued one is started now. |
| `GET`  | `/analyses/{id}` | `VideoAnalysis` (with `status`: `queued`/`pending`/`running`/`done`/`failed`, `progress` message, `error`) |
| `GET`  | `/queue` | `QueueStatus`: `window_start`/`window_end` (local `HH:MM`), `state` (`empty`, `busy`, `waiting_window`, `waiting_gpu`, `starting`), `message`, `next_start`, `items[]` (running first, then queued oldest first), `run_now[]` (waiting ids the learner started now), `in_hours` |
| `POST` | `/queue/run-now` | `{analysis_ids?}` → `QueueStatus`: run these waiting videos (all when no ids) now, skipping the queue hours but not the GPU check |
| `DELETE` | `/queue/run-now` | back to the queue hours for everything waiting → `QueueStatus` |
| `POST` | `/queue/{id}/start` | start a queued analysis now → `202` (`400` if it isn't queued any more) |
| `DELETE` | `/queue/{id}` | remove a queued analysis |
| `GET`  | `/gpu` | `GpuStatus`: `cards[]` (utilization, memory, temperature, power, fan), `processes[]` (pid, program, command with secrets hidden, `app` from the cgroup, kind, memory, utilization, running time, `curio`), `processes_visible`, `ollama[]` (loaded models, VRAM, `expires_at`, `curio_model`), `ollama_clients[]` (programs connected to Ollama: pid, program, command, app, `connections`, `curio`, `exact`, `connected`, `last_seen`; `null` when connections can't be read), and `curio_busy`, `curio_doing[]`, `curio_last_model_use`, `queue_message`. Read at most every 2 s |
| `GET`  | `/videos/recent?limit=10` | `[ {video, analysis_id, profile, analyzed_at, playlist_s, watch_map[]} ]` |
| `GET`  | `/videos/{video_id}/transcript` | `Transcript` (normalized blocks) |
| `GET`  | `/analyses/{id}/completed` | ids of segments marked watched |
| `PUT`  | `/analyses/{id}/segments/{segment_id}/completed` | `{completed: bool}` |
| `POST` | `/analyses/{id}/notes` | `{scope, segment_ids?, force?}` → `202 {notes_id, status}`; runs in background on the same single worker as analyses. Reuses saved notes with the same scope and model unless `force`. |
| `GET`  | `/analyses/{id}/custom-playlist` / `PUT` | the learner's own playlist `{segment_ids}` (time order) or `null` for Curio's; `PUT {segment_ids: null}` (or exactly Curio's picks) goes back |
| `GET`  | `/analyses/{id}/notes` | `[LearningNotes]`, newest first |
| `GET`  | `/notes/{id}` | `LearningNotes` (poll while `pending`/`running`) |
| `POST` | `/analyses/{id}/questions` | `{question}` → `Answer` (synchronous, seconds) |
| `GET`  | `/analyses/{id}/questions` | the conversation, oldest first |
| `DELETE` | `/analyses/{id}/questions` | clear the conversation → `{deleted}` |
| `GET`  | `/notes/{id}/markdown` | the notes as a Markdown download, with timestamp links into the video |
| `POST` | `/analyses/{id}/study` | `{scope, segment_ids?, force?}` → `202 {study_id, status}`; a quiz and flashcards, in the background like notes |
| `GET`  | `/analyses/{id}/study` | `[StudySet]`, newest first |
| `GET`  | `/study/{id}` | `StudySet` (poll while `pending`/`running`) |
| `POST` | `/study/{id}/attempts` | `{answers: {question id: option index}}` → `QuizAttempt` (graded and saved) |
| `GET`  | `/study/{id}/attempts` | `[QuizAttempt]`, newest first |
| `GET`  | `/flashcards?study_id=&analysis_id=&course_id=&limit=` | `FlashcardDeck {cards[DueCard], due, total, next_due_at}`: due cards from every deck, one video's, one course's (in lecture order), or one set |
| `POST` | `/flashcards/review` | `{study_id, card_id, grade: again\|good\|easy}` → `CardState` |
| `GET`  | `/preferences/default` / `PUT` | the learner's default goal `{profile, custom_objective, target_minutes}` (`PUT` body `{preferences}`) |
| `POST` | `/courses` | `{url, preferences}` → `202 {course_id}`: a course from a YouTube playlist (the same playlist and goal reuse the course). Its lectures are checked and queued in a background thread beside the job worker |
| `GET`  | `/curricula/candidates` | `CurriculumCandidate[]`: every course (not curricula) and analyzed video outside them; no model call |
| `POST` | `/curricula/suggest` | `{goal}` → the candidates with `suggested` and `reason` (one model call) |
| `POST` | `/curricula` | `{goal, title?, sources: [{kind: course\|video, id}], order: lectures\|sections}` → `201 {course_id}`: a curriculum, ordered by what builds on what |
| `PUT`  | `/courses/{id}/curriculum` | `{order?, auto_order?}` → `Course`; `404` for a plain course |
| `POST` | `/courses/combine` | `{title, course_ids}` → `201 {course_id}`: a learning path, the courses' lectures in that order without duplicates, the first course's goal |
| `POST` | `/courses/group` | `{title, analysis_ids}` → `201 {course_id}`: a course of analyzed videos, in upload order |
| `GET`  | `/courses` / `/courses/{id}` | `Course`: lectures with their state (`ready`, `other_goal`, `queued`, `analyzing`, `failed`, `missing`), the newest `plan`, `plan_stale`, and `custom_parts` (the learner's own course playlist, or `null`; only on `/courses/{id}`) |
| `DELETE` | `/courses/{id}` | delete a course (its analyses stay) |
| `POST` | `/courses/{id}/refresh` | read the playlist again and queue new lectures |
| `POST` | `/courses/{id}/plan` | `{minutes?, force?}` → `202 {plan_id, status}`: build the course playlist from the lectures analyzed so far, on the job worker |
| `POST` | `/courses/{id}/questions` | `{question}` → `CourseAnswer` (synchronous; citations carry `video_id`, `analysis_id`, `lecture`) |
| `GET`  | `/courses/{id}/questions` / `DELETE` | the course conversation, oldest first / clear it |
| `POST` | `/courses/{id}/practice` | `{force?}` → `202 CoursePractice`: a quiz and flashcards for every lecture of the course playlist, on the job worker; sets with the same sections are reused unless `force` |
| `GET`  | `/courses/{id}/practice` | `CoursePractice {sets[{lecture, study}], status, progress, stale, questions, cards}` |
| `GET`  | `/courses/{id}/watched` / `PUT` | watched course parts (`PUT` body `{part_key, watched}`) |
| `PUT`  | `/courses/{id}/lectures/order` | `{video_ids}` (every lecture once) → `Course` in the new order; the saved plan is renumbered, not rebuilt |
| `GET`  | `/courses/{id}/playlist-sections` | `CourseSectionChoice[]`: every section of the lectures the course uses, with `curio_pick`, `picked` (in the playlist shown now), `pinned` and `watched`; needs a built plan |
| `PUT`  | `/courses/{id}/pins` | `{sections: ["<analysis id>:<section id>", …]}` → the saved pins in course order (`[]` clears them); they count from the next build |
| `PUT`  | `/courses/{id}/custom-playlist` | `{sections: ["<analysis id>:<section id>", …]}` → `Course` with `custom_parts`; `null` (or exactly Curio's picks) goes back to Curio's |
| `POST` | `/queue/preferences` | `{preferences, analysis_ids?, save_default?}` → `{updated[], already_analyzed[]}`: give queued videos a goal (all queued when no ids); a video already analyzed with that goal leaves the queue |

Errors use `{"detail": {"code": "...", "message": "..."}}` with codes
`invalid_url`, `video_unavailable`, `transcript_unavailable`, `rate_limited`,
`llm_error`, `analysis_error` (for notes: analysis not finished, unknown or empty selection).

## 6. Transcript acquisition strategy

1. Parse the URL (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`, bare id).
2. Metadata through yt-dlp, no download. Reject live streams and private videos early.
3. Transcript preference order: manually created track in the preferred
   languages (`CURIO_TRANSCRIPT_LANGUAGES`, default `en`) → auto-generated
   track in those languages → any manual track translated to English through
   YouTube's own translation → fail with `TranscriptUnavailable` listing what
   exists.
4. Store the raw cues once per video. Normalization is deterministic and re-runnable.
5. Future providers (same protocol): YouTube Data API for owners, user-uploaded
   `.srt/.vtt`, local Whisper on user-supplied audio.

**Normalization:** strip `[Music]`-style annotations and `>>` speaker markers
(the marker becomes a speaker-change hint), unescape HTML, collapse whitespace,
remove the rolling duplicate text that auto-captions repeat across cues, and
clip overlapping cue times. Then merge cues into **blocks** of 20–45 s that end
at sentence punctuation or at a pause ≥ 1.2 s when possible. Blocks are the
atomic unit for segment boundaries: fine enough for accurate timestamps, coarse
enough to keep the transcript prompt compact (`[B0042 12:31] text…`).

## 7. AI processing pipeline

| # | Step | Implementation |
|---|---|---|
| 1 | Ingestion | metadata + transcript (cached) |
| 2 | Normalization | `processing.normalize` → blocks |
| 3 | Topic segmentation | **LLM pass 1 (outline):** the whole transcript in one prompt when it fits the model's budget; otherwise overlapping windows, each given the outline produced so far. Output: an overview, the key concepts, and contiguous sections expressed as block ranges, each with a title, summary, kind and concepts. YouTube chapters, when present, are given as hints. Code repairs gaps and overlaps so the sections tile the video exactly. |
| 4 | Contextual analysis | Pass 2 receives the **full outline** of every section plus the text of the sections being evaluated, so each judgment is made with the whole video in view. |
| 5 | Objective analysis | The profile plus the custom objective become an explicit "learner brief" inside the prompts, and adjust scoring weights. |
| 6 | Segment identification | Sections are the candidates; their boundaries are the outline's topic transitions (refined in 6b). Each playlist segment starts 2 s before its first sentence and ends 1 s after its last, so speech isn't clipped. |
| 6b | Sentence-level boundaries | Clean cues are grouped into sentence units (about 4 s on average for punctuated captions; a unit starts at a cue when the previous cue ended a sentence, after a pause or speaker change, or when a new sentence begins mid-cue). **LLM pass 1b:** for every boundary, the model sees the ending and starting sections plus the numbered sentences from one block before to one block after the boundary, never crossing into other sections, and picks the first sentence of the new section. Code moves the start to that unit's cue time and ends the previous section on the sentence before it, rejecting choices that are out of range or would leave a section under 30 s. Boundaries go to the model in batches that fit the context (about 20k prompt tokens with Ollama's 32k; an 82-minute podcast with 53 sections needed 36k in one call), with one progress message before the first batch so a pause never splits the step. On failure, block boundaries are kept with a warning (per batch). Evaluation and redundancy then use the text that plays between the refined times. **Captions with unreliable punctuation** (fewer than 5 sentence marks per 100 words; human captions measure about 7, YouTube auto-captions 0–3.8) use each caption cue (about 2.5 s) as a unit instead, because a 12 s chunk of unpunctuated text rarely starts where a topic does. Toggle: `CURIO_REFINE_BOUNDARIES`. |
| 6c | Sponsor reads | Mid-roll ads often open with a segue that sounds like content, and the outline can miss them. Code flags units that look like ad copy (web address, "sponsor", "promo code", "brought to you by", "link in the description") and groups them into windows reaching 75 s back and 25 s forward. **LLM pass 1c** confirms each window and gives the ad's first and last sentence, including the segue, plus the sponsor's name; passing mentions (a paper's URL, a tool being explained) are rejected. Each confirmed ad of 10 s or more becomes a `sponsor` section (always skipped), carved out of the sections it overlapped. A split section's second part gets "(continued)". Content fragments under 20 s join neighbouring content, never the ad. No LLM call when nothing looks like an ad. Toggle: `CURIO_DETECT_ADS`. |
| 7 | Scoring | **LLM pass 2 (evaluate):** rubric scores from 1 to 10 plus structured flags. `scoring.py` combines them with the configurable weights (30/20/20/15/15), then applies rule-based adjustments: kind priors (sponsor/meta → skip), a penalty for very short sections, a novelty bonus for introducing concepts, a lexical redundancy penalty (TF-IDF cosine 0.55 or more against a better section; at 0.45 it only ever caught different sections sharing vocabulary), LLM-flagged redundancy, a standalone penalty. Every adjustment is recorded with its reason. Half of each base score comes from the section's rank among the video's teaching sections (core, example, demo, Q&A; `within_video_rank`), placed on the range of ratings the model gave them, not on 0–100: it orders bunched ratings without dragging solid sections of an all-strong video down a tier (a 0–100 percentile turned a Kubernetes basics section rated 82 into 65). Intros, recaps, outros, sponsor and housekeeping sections aren't ranked; they keep their kind priors. For the beginner goal, a content section that later teaching sections build on (following `builds_on` transitively) gets up to +12 (`foundation`), in proportion to the share of the video's teaching that depends on it, because the model rates "what is X and how its parts fit" below hands-on sections and a prompt sentence didn't change that. The learner brief tells the model that, with the learner's own goal, novelty and relevance are judged separately: a plain explanation of what they say they already know is low novelty, and relevance is only whether a section gives what they ask for, even on a familiar topic (marking known material low on both had pulled Lecture 1's debugging framework down from 88 to 55–59; it is back at 70); and, for deep dives, that a speaker's own framework or mental model is a novel insight (Karpathy's "LLM OS" went from 35 to 71–78 over two runs). Content (core/example/demo) the AI rated 50 or above and not redundant is never pushed below optional by ranking alone (`content_floor`). |
| 8 | Redundancy | Bag-of-words cosine similarity between sections, combined with the LLM's `redundant_with` flags. The lower-scored section of a near-duplicate pair is demoted, not deleted. |
| 9 | Playlist | Tier by score (essential ≥ 70, recommended ≥ 55, optional ≥ 40, otherwise skip; thresholds configurable). Budget: user target, or 30% of duration clamped to 10–35 min. Selection is a small knapsack (5 s steps): the set of non-skip sections with the most total value that fits, where a section's value is (score/100)³ × √minutes (strong sections beat several middling ones; a 10-minute section teaches more than a 5-minute one, but not twice as much), raised 10% for each better section that builds on it (at most two), counting pinned and reserved top sections too (counting only the sections still in the knapsack, Kubernetes for beginners dropped "What is Kubernetes" and its architecture, built on by its two 100-scored sections, for Volumes and Deployments). Before the knapsack, sections scored 95 or more go in first, best (then shortest) first, while each fits and they fill at most half the playlist together (`top_score`, `top_share`); in a course, one that repeats a better reserved section isn't reserved. Without this the √ curve let several shorter sections crowd out a long top one (CS329Z's DSPy optimizers, 100, 9.3 minutes of a 90-minute course playlist); the half keeps MIT's 10-minute proof out of a 14-minute playlist. Then direct prerequisites (`builds_on`) of chosen sections are added while they fit. Before, sections went in by tier and score, and one long top section could crowd out several nearly as good (MIT 6.006: a 10-minute proof scored 100 left out efficiency and complexity classes, 87 and 93, from 14 minutes). Course playlists use the same knapsack with their repeat exclusions: when the best set holds both halves of a repeat, the half worth less is set aside and the set is chosen again until no repeat is left (a course has a few repeat pairs, so a few rounds); a prerequisite that repeats a chosen section isn't added. For long course budgets the step grows so the table stays under ~1,500 steps. Then `fit_labels` keeps "essential" for sections in the playlist only: a section that scored as essential but didn't fit is labeled recommended, with a zero-point `beyond_budget` note naming the budget (its score is unchanged; across a course it still counts as essential). `curio relabel` applies this to analyses saved before the rule, changing labels only. Before this, 40% of essential sections were outside their playlist, because essentials added up to 1.1–2.6× the budget. Adjacent picks with a gap under 15 s are merged into one segment. Output is chronological. Threshold calibration: see PROGRESS, 2026-11-24. |
| 10 | Validation | Segments inside `[0, duration]`, `start < end`, no overlaps, every boundary is a real caption-cue time (either edge of a cue: next to a cut-out sponsor read, content ends where the ad's first cue starts), non-empty explanations. Sections tile the transcript **in time**: ordered, no overlaps, and no gap that contains transcript speech (silence is fine). Playlist within budget. Problems are recorded as warnings, and fixable ones are repaired. |
| 11 | Interface | Web app: watch map of the whole video, playlist beside the YouTube player (cue, stop at the part's end, auto-advance, speed), overview, more to watch, skip list with reasons, scoring table. |

**Why two LLM passes instead of chunk summaries:** pass 1 sees the whole
transcript, so section boundaries follow real topic transitions. Pass 2 judges
each section against the outline of the whole video, which is what makes
novelty ("introduces a concept not covered elsewhere") and redundancy
judgments possible.

**Why not trust the LLM's number:** the rubric is only one input. Final scores
are deterministic functions of rubric, kind, duration, lexical redundancy and
concept novelty, and each adjustment is shown to the user.

### LLM layer

```python
class LLMProvider(Protocol):
    name: str
    def complete_json(self, *, system: str, user: str, schema: dict,
                      task: str, max_tokens: int) -> str: ...

def generate_structured(provider, model_cls, *, system, user, task, retries=2) -> model_cls
    # builds a strict JSON schema from the Pydantic model, calls the provider,
    # validates; on failure re-prompts with the validation error appended.
```

- **Ollama:** `format=<schema>` (constrained decoding), `num_ctx` sized to the
  prompt, `think=False` for JSON calls, temperature 0.2 (a call can override it: the course repeat check uses 0).
- **Claude:** `messages.stream` with `output_config.format` (JSON schema) and
  `effort`, adaptive thinking, server-side refusal fallback (`fallbacks: "default"`).
- Per-task model selection: `CURIO_LLM_MODEL_OUTLINE`, `CURIO_LLM_MODEL_EVALUATE`, `CURIO_LLM_MODEL_NOTES`,
  with `CURIO_LLM_MODEL` as the default.
- Provider selection: `CURIO_LLM_PROVIDER=auto|ollama|anthropic`. `auto` picks
  Anthropic when `ANTHROPIC_API_KEY` is set, otherwise Ollama.

## 8. Deployment (Podman)

```
 host                                             container "curio" (rootless Podman)
 ┌──────────────────────────────┐                 ┌───────────────────────────────────┐
 │ browser → <host>:8470 ───────┼── published ───►│ :8000  uvicorn → FastAPI           │
 │                              │                 │         ├─ /api/v1  CurioService   │
 │ Ollama :11434 (GPU) ◄────────┼── 169.254.1.2 ──┤         └─ /       web/dist (SPA)  │
 │                              │  (pasta map)    │ /app/data ── volume "curio-data"   │
 └──────────────────────────────┘                 └───────────────────────────────────┘
```

- `Containerfile`: a Node 22 stage builds `web/dist`; the runtime stage is Python
  3.12-slim with uv 0.11.7, `uv sync --frozen --no-dev`, a non-root user, and a
  health check on `/api/v1/health`.
- `curio.sh`: build, start, stop, restart, run, status, logs, analyze, queue, shell.
  It publishes on all interfaces (`CURIO_BIND=0.0.0.0`, so the LAN can use it; there
  is no login), prints the local and LAN addresses, keeps the SQLite database in a named volume,
  checks that the port is free and that the container can reach Ollama.
- The LLM stays on the host: the image holds no model, and with
  `ANTHROPIC_API_KEY` in `.env` Ollama isn't needed at all.
- Settings in `.env` are passed with `--env-file`; the database location
  (`sqlite:////app/data/curio.db`) and the Ollama address are always set for the
  container, whatever `.env` says for local runs. The container database (volume) is
  separate from `data/curio.db` used by local `uv run` commands.
- Networking: rootless Podman's default network (`pasta`) resolves
  `host.containers.internal` to the host's LAN address, which pasta treats as the
  container's own, so connections are refused. The script maps `169.254.1.2` to the
  host (`--map-guest-addr`) and points `host.containers.internal` at it. Ollama must
  listen on all interfaces (`*:11434` in `ss -ltn`).
- The image is built in Docker format, because the OCI format drops the health check.
- The container gets the host's `/etc/localtime` (queue hours are local time) and,
  when an NVIDIA CDI spec exists (`CURIO_GPU_CHECK`, default on then), `--device
  nvidia.com/gpu=all`, used only to read utilization with `nvidia-smi`. With it,
  `--pid=host` (`CURIO_GPU_PROCESSES`, default 1) lets the GPU page see the host's
  processes. Inside its own PID namespace `nvidia-smi` lists no processes. The
  trade-off: the container's root maps to the host user, so with the host's process
  namespace it could also signal that user's processes.
- Container variables: `CURIO_PORT` (8470), `CURIO_BIND` (0.0.0.0), `CURIO_VOLUME`
  (curio-data), `CURIO_OLLAMA_URL` (`http://host.containers.internal:11434`),
  `CURIO_GPU_CHECK`, `CURIO_GPU_PROCESSES`, `CURIO_IMAGE` / `CURIO_CONTAINER`.

## 9. Learning notes (`analysis/notes.py`)

Notes are generated on request, after an analysis, for one of three scopes: the
playlist's sections, every section except sponsor reads and housekeeping, or
segments the learner picks.

1. **Section notes**, batched by transcript size like evaluation
   (`CURIO_EVALUATE_BATCH_TOKENS` with Ollama, 60k tokens with Claude). The prompt
   holds the learner brief and each section's sentence units, numbered. The model
   returns, per section, 2–6 points and any definitions, each with a sentence
   number, plus an explanation when the section explains a method. Code maps each
   number to that sentence's cue time; an out-of-range number falls back to the
   section's start and is reported in `warnings`. Skipped sections are retried once.
2. **Summary**: one call over the notes (not the transcript) returns the summary,
   3–7 takeaways and 0–5 recommendations, each with section ids. Ids outside the
   notes are dropped and reported. If this call fails, the section notes are still
   saved.

Grounding is in the prompt (only what the section says, no outside facts) and in
code (every time is a real cue time, every section id is checked). Notes are stored
in their own table (`notes`) and keyed by analysis, scope, picked segments and
model (`NOTES_VERSION`), so the same request returns the saved notes. Markdown
export renders every timestamp as a link to the video at that second.

## 10. Ask the Video (`analysis/ask.py`, `processing/retrieval.py`)

**Context.** The prompt holds the learner brief, the outline, and transcript blocks
as `[n | m:ss] text`. If all blocks fit the budget (`CURIO_ASK_CONTEXT_TOKENS`, or
automatically: Ollama's `num_ctx` minus room for instructions, outline,
conversation and a 2,000-token answer; 150k with Claude), the model sees the whole
transcript. This part of the prompt is identical for every question about a video,
so Claude's prompt cache and Ollama's KV cache reuse it: measured 38 s for the first
question on the 80-minute lecture, 5–9 s for the next ones.

**Search** (only when the transcript doesn't fit). Blocks are ranked by BM25 and by
cosine similarity of `nomic-embed-text` embeddings (with its `search_query:` /
`search_document:` prefixes), fused by reciprocal rank. Block vectors are computed
once per video and stored in `embeddings`, keyed by a hash of the blocks; if Ollama
can't embed, search falls back to BM25 alone. Times named in the question ("at 23
minutes", "1:05:00", "minute 7") pull in 1.5 min before to 2.5 min after that point
first; then each hit is added with its neighbouring blocks until the budget is
used. The search query includes the previous question, for follow-ups. Excerpts are
shown in video order with `…` for gaps, and the answer says it was searched.

**Answer.** `AnswerOut` = parts (text + passage numbers), `in_video`,
`outside_knowledge`. Code drops citations of passages that weren't shown (reported
in `warnings`) and maps each remaining one to the start of the sentence in that
block sharing the most words with the part, so a timestamp points at the claim, not
just its 20–45 s block. The last four turns are included as conversation history.
Answers are stored in `questions`.

## 11. Overnight queue (`overnight.py`)

A queued analysis is an ordinary `analyses` row with status `queued`, created by
`submit(..., queue=True)` after metadata and transcript are fetched (so a bad link
fails at once, not at 3 AM). Matching uses the same cache key as analyses, so
queuing a video twice reuses the row, and analyzing a queued video now takes it
out of the queue (`claim_queued`, a conditional `UPDATE ... WHERE status='queued'`,
so the worker and a "start now" request can't both start it). Queued rows are
never marked stale.

The API server owns a `QueueWorker` thread that ticks every `CURIO_QUEUE_CHECK_S`
(60 s). A tick starts the oldest queued analysis on the API's single job executor
when all of these hold:

1. **Curio is idle**: no analysis or notes job in flight (`Jobs` counts them).
2. **Hours**: local time is inside `CURIO_QUEUE_HOURS` (default `1-7`; may cross
   midnight; empty = any time).
3. **GPU idle** (Ollama only; with Claude this check is skipped):
   - Ollama `/api/ps` lists no model except Curio's own (its LLM and embedding
     models), and Curio's own models count only within 15 minutes of Curio's last
     LLM use (their `keep_alive` is 10 minutes). Otherwise a loaded model means
     someone else is using Ollama.
   - `nvidia-smi` utilization, the median of five one-second readings, is below
     `CURIO_QUEUE_GPU_BUSY_PERCENT` (20). Measured on the dev machine: an idle
     GNOME desktop reads 0–5% with spikes to about 30%; a training job reads 88–90%.
     Without `nvidia-smi` this check is skipped.

**Run now.** The learner can start waiting videos outside the hours (all of them,
or a course's waiting lectures). Their ids are kept in the `settings` table
(`queue_run_now`), so a restart keeps them. A tick outside the hours considers only
those ids; inside the hours they go first. Conditions 1 and 3 still apply, so they
run one at a time and wait for an idle GPU. An id stays on the list while it runs, so
a video that pauses for the GPU and goes back to `queued` is still the learner's pick.
The list is cleared on the first tick that finds none of it waiting and Curio idle.
The per-video `POST /queue/{id}/start` is different: it runs at once on the job
executor, with no GPU check.

The conditions are checked before each start, so a run started at 6:55 finishes
after 7:00, and a GPU job started at night pauses the queue before the next video.
During a queued analysis, `CurioService.run(should_pause=...)` asks the GPU probe
before each pipeline step, at most once a minute (`_every(60, worker.gpu_busy)`; the
probe takes 5 s, and the queue checked just before starting). If something else is
using the GPU the run raises `Paused` and goes back to `queued` with the reason as its
progress, and picks up where it stopped when the GPU is free.

**Checkpoints.** After each LLM step (outline, sentence-level boundaries, ad removal,
evaluations) the pipeline saves the state so far to `analysis_checkpoints` (one row per
analysis: the step, the sections, repairs and warnings, the outline's overview and
concepts, the evaluations, and the seconds spent). A run that finds a checkpoint skips
the steps it covers. A checkpoint is
used only when its key matches: the pipeline version, provider and models, a hash of the
transcript blocks, and the boundary and ad switches. The evaluations also need the same
learning goal, since a queued video's goal can change while it waits; with a new goal
the outline and boundaries are kept and only the evaluations are redone. The checkpoint
is deleted when the analysis finishes or fails, or when it's taken out of the queue.
Within steps, every model call that a pause could follow is saved too. A pause is only
checked at a progress message, and those come between model calls. A long transcript's
outline saves its state after each window (`partial_outline`: the sections so far, where
the next window starts, the window size; `step` is still "" then), so a resumed run reads
only the windows left, then writes the overview. Each finished evaluation batch is saved
(`partial_evaluations`, with the goal it was written for), and a resumed run asks only
the batches not done. Batches and windows come out the same on both runs, because the
transcript, sections and sizes are the same. The boundary and ad steps report progress
only before their calls, so a pause never interrupts them. A pause loses no finished
model call.
The worker remembers the last reason it waited, which `GET /queue` reports.

**Preparing study material.** With `CURIO_QUEUE_PREPARE` (default on), the queued job
doesn't end with the analysis: `CurioService.prepare` then writes playlist notes and a
quiz with flashcards, reusing any that already exist, so a video queued at night is
ready to watch, read and practice in the morning. Before each of the two it asks the
GPU probe again (`QueueWorker.gpu_busy`, which counts Curio's own loaded model as its
own); if someone else started using the GPU, the rest is skipped and can be written
from the video's page. The window isn't checked again, as for a running analysis.
"Analyze now" and starting a queued video by hand don't prepare anything. In
Podman, `curio.sh` mounts the host's `/etc/localtime` and, when an NVIDIA CDI spec
exists, adds `--device nvidia.com/gpu=all` so `nvidia-smi` works in the container.

**The GPU page** (`gpu.py`, `GET /gpu`). It reads one `nvidia-smi --query-gpu` and
one `nvidia-smi pmon -c 1 -s um` sample. pmon covers graphics processes too and gives
per-process SM %; its columns differ between drivers, so they're read from its header.
For each pid it reads `/proc`:
- `cmdline`: Chromium-style programs pack theirs into one string, which is split.
  Flags and `KEY=value` arguments named token, key, secret, pass, auth or cred have
  their values hidden, and long arguments are cut.
- `cgroup`: gives the app. A `.service` becomes "ollama service"; a snap or
  `app-gnome-*` scope becomes the app ("VS Code"), or "Started from VS Code" for
  another program in it; `libpod-<id>.scope` becomes "Container <id>", or Curio's
  own when the id matches the container's hostname.
- `stat` plus `uptime`: running time.

Ollama's `/api/ps` gives the loaded models.

**Ollama's clients.** Ollama can't say who loaded a model, so the page lists the
programs with an established TCP connection to Ollama's port (from
`CURIO_OLLAMA_HOST`).
1. Processes are grouped by network namespace (`/proc/<pid>/ns/net`). For each
   namespace, one process's `/proc/<pid>/net/tcp{,6}` gives the inodes of client
   sockets (state 01, remote port = Ollama's).
2. Each process's `/proc/<pid>/fd` links (`socket:[inode]`) match those inodes to a
   pid.
3. Network proxies (pasta, slirp4netns, rootlessport) are left out. A container's
   connection shows up there too, and the container is found in its own namespace.

A process in another user namespace hides its `ns` and `fd` links, even when it runs
as the same host uid. That covers the containers of other rootless Podman setups,
quadlet units and keep-id containers. Its `cgroup`, `cmdline` and `net/tcp` stay
readable:
- These processes are grouped by cgroup. A group whose client sockets weren't already
  seen in a readable namespace is credited as a whole, `exact: false`, showing its
  main program past an init wrapper (podman-init, catatonit, tini, conmon).
- A table that several groups share is skipped. That's a namespace they share: the
  host's, for host-network containers and conmon, where the owner can't be told.

Containers are named in one of two ways:
- a systemd (quadlet) unit's container is named after its unit
  (`…/sarathi-dvara.service/libpod-payload-<id>`);
- when Curio runs on the host, Podman's store
  (`overlay-containers/{volatile-,}containers.json`) names the rest.

In Curio's container the store isn't readable (it runs as user `curio`), so a plain
`podman run` container shows as "Container <id>". The pid of Curio's own process
(on the host) or container (hostname = id) is marked `curio`. A client connects only
while it sends a request, so `GpuWatch` keeps each one seen in the last 15 minutes
(`connected: false` and `last_seen` once it's gone). A scan takes about 0.2 s. `processes_visible` is false when memory
is in use but pmon lists nobody, which means a container without `--pid=host`. A
`GpuWatch` on the app caches the report for 2 s, so several open pages cost one read.

## 12. Quizzes and flashcards (`analysis/study.py`)

**Writing.** The same scopes, batching and sentence numbering as notes. Each section
header carries a budget by length (under 3 min: 1 question, 2 cards; under 8 min:
2 and 3; longer: 3 and 4), and code enforces it. Every question and card names the
sentence that answers it, mapped to that sentence's cue time ("Explained at 55:12").
A question is `{question, wrong_options[], right_option, explanation, sentence}`:
there is no answer index for the model to get wrong (an earlier version with
`correct: 4` produced a mislabeled key). Code keeps 2-4 distinct wrong options and
shuffles them with the right one, seeded by the question text.

**Checking.** One more call over all questions replaces "wrong" options that are also
correct or near-copies of the answer, tops them up to three, and asks for every option
to fit a word range around the longest wrong one, because a right answer that is the
longest option is a giveaway. Measured on the CS329Z playlist (8 questions):

| Version | Right answer longest | Problems |
|---|---|---|
| first prompt | 7 of 8, by 20-50% | explanations said "the section" |
| "keep options similar in length" | 7 of 8 | |
| right answer written last, `correct: 4` | 5 of 8 | one key pointed at a wrong option |
| separate `right_option` field | 8 of 8 | one "wrong" option was correct |
| + checking call, character limit | 7 of 8 | |
| + checking call, word range | 5 of 8, mostly by 3-12% | one clear giveaway (150 vs 114 chars) |
| + evening-out pass (2026-11-24), on 3-4 lectures of 10-12 questions | 0-2 of 10-12 | first try 9 of 11 by 5-15%; fixed by the 5% threshold, a character minimum and a second round |

Chance is a quarter. The checking call costs 10-15 s; a playlist set takes 75-100 s.

**Evening out.** After the checking call, `giveaway(q)` flags a question whose right
answer is 5% and 6 characters longer than every wrong option. One call (and a second
for those still flagged) rewrites only the wrong options, to at least the right
answer's length and at most a third longer, keeping them wrong per the explanation.
A rewrite is applied only if it clears the flag and no wrong option runs past 1.5x the
right answer (an earlier version produced dramatic, much longer wrong options, which
made the plain right answer stand out instead). The right answer is never touched.

**Names the video never says.** A right answer is checked for names (words with a
digit or an inner capital, and acronyms: GPT-4, ChatGPT, A2A, MCP) against the
transcript of the sections in scope, ignoring spacing and hyphens. Any name not said
drops the question, with a warning. Found because a quiz answered "GPT-4" where the
Tools lecture only ever says ChatGPT. A hyphen continues a name only before a capital
or digit (GPT-4o), so "API-only" is checked as "API".

**Numbers and plain-word claims aren't checked** (measured 2026-11-25 on 404 saved
items: notes, quizzes, cards, answers). Only 2 had a number the video never says (both
true Kubernetes facts, likely on screen). A model check of 120 items against their
section found none whose main point is unsupported and 20 that add a detail the
section doesn't say, nearly all true background (a Service's DNS name, etcd). Some of
those are said in another section, so a per-section verifier would strip correct
content, at about a minute per notes or quiz; the prompts already forbid outside facts.

**Spaced repetition.** Leitner boxes: a card with no state is new (due now).
"Forgot" puts it in box 1, due in 10 minutes (a lapse if it had been learned);
"Remembered" moves it up one box and "Easy" two, due after 1, 3, 7, 16, 35 or 90 days.
The review deck is each analysis's newest finished set (writing a new set replaces
the cards in review; the old set can still be drilled from the tab by `study_id`),
plus, as separate decks, each analysis's newest set for each course (§13):
overdue cards first, most overdue first, then new cards in video order.

## 13. Courses (`ingestion/playlist.py`, `analysis/course.py`, `analysis/course_ask.py`)

**Lectures.** A course is a list of videos in order (`courses.lectures_json`) and a
goal. From a playlist, yt-dlp lists the entries flat (no download, one request);
private and deleted entries are counted and left out. When the lecture numbers in
the titles mostly go down along the playlist, it's reversed (CS329Z is newest
first). Importing then calls `submit(..., queue=True)` for each lecture, which
fetches its captions and reuses any analysis with the course goal, so only new
lectures are queued, in course order. Why a lecture couldn't be queued (no captions)
is stored per video. A lecture's state is worked out on every read from its
analyses: done with the course goal (`ready`); done only with another goal
(`other_goal`, used meanwhile, its scores from that goal); queued or running with the
course goal; failed; or missing. Labels come from the titles ("Lecture 6") when
they number the lectures uniquely, else the position.

**Course playlist.** Inputs: the analysis ids in course order and a length; a plan
with the same inputs and model is reused, and `plan_stale` says lectures changed.

1. Items: every section that isn't skip, intro, outro, housekeeping or sponsor, and
   not an optional section under 2 minutes (those only filled leftover seconds, out
   of context). Scores come from each lecture's analysis; half of each is the
   section's rank within its lecture, so they compare across lectures.
2. Candidates: each item's two closest items in other lectures, by cosine of
   `nomic-embed-text` vectors of title, summary, concepts and what it teaches
   (TF-IDF without an embedder). The closest pairs, up to 6 per lecture (at most 72).
3. Judging, 12 pairs a call: summaries plus about 160 words of transcript from each
   side (the opening and the middle). Verdict `repeat` (watch one), `overlap` (each
   adds something) or `different`, which one to keep, and a reason. Asked at
   temperature 0 (Ollama; Claude's adaptive thinking takes no temperature): at 0.2,
   three runs over CS329Z's 60 pairs disagreed on 10, and each `repeat` came up in
   only one of the three, so rebuilds dropped different sections; at 0 the three runs
   matched exactly (0 repeats, 38 overlaps, 22 different).
4. Selection: the playlist knapsack (step 9) over all items at once, with section ids
   prefixed by lecture (prerequisites too). For a repeat, the half to skip starts
   15 points lower and is no longer essential, and choosing either half rules out the
   other (`select_sections(excludes=...)`: the half worth less is set aside and the set
   chosen again). Parts are merged within a lecture as in a
   video playlist; `also_in` lists related sections in other lectures. A left-out
   repeat is listed once, with its closest match.
5. Checklist: the course outline (ids like `L3.S05`, title, summary, concepts;
   summaries of weaker sections are dropped if it doesn't fit the context) and the
   learner brief. 8–12 points, each citing sections; points without a real section
   are dropped, and each says whether its sections are in the course playlist.

On CS329Z (7 lectures, 9 h 9 min, qwen3.8 27B): 28 pairs judged in 3 calls and the
checklist in one, 1.5–2 minutes in all. A 90-minute budget gave 19 parts (1 h 38
min, inside the 10% tolerance). Repeats found: RAG failure modes (Lectures 3 and 4),
context engineering (2 and 6), skills (6 and 7). The first prompt also called
"MemGPT Q&A" a repeat of "context tips" because both mention KV-cache invalidation;
saying that one shared point isn't a repeat fixed it. Checklist claims checked
against the transcripts (reader accuracy saturating after 10–20 documents; memory
agents scoring worst on MemoryArena) were in the lectures.

**Rebuilt overnight.** After a queued lecture's notes and quiz, the job calls
`rebuild_courses_after`: every course with that video and no lecture still queued or
running is rebuilt with its last plan's length (the default when none was built),
after another GPU check. A skipped rebuild is recorded as a failed plan with the reason.

**Ask the course** (`analysis/course_ask.py`). The blocks of every analyzed lecture are
numbered across the course (`[n | Lecture 3, m:ss]`), and the outline lists each
lecture's section titles. With Ollama's 32k context a course never fits, so the
course-wide ranker (BM25 and `nomic-embed-text` over all blocks at once, fused by
reciprocal rank; each lecture's vectors are the ones Ask the Video stores) orders the
passages, and Ask's `select_context` adds them with neighbours until the budget is
full (about 25k tokens: some 250 of 1,400 passages for CS329Z). Lectures the question
names come first, and a time it names in one lecture is read first. Citations become
sentence starts in that lecture (`precise_start`). On CS329Z: about 40 s an answer;
the claims checked ("a demo that works once … will not score highly", observation
masking and compaction "very similar" in solve rate) were in the transcripts.

**Repeat verdicts.** Every pair the model judged is kept in the plan (`pairs`: both
sections, verdict, which to keep, reason, similarity, and whether each half is in
the playlist), and the Repeats tab shows them. On CS329Z, a 3-hour build compared
28 pairs: 3 repeats (1 left out, 2 where the other half was also out or the pair
crossed the budget), 11 overlaps, 14 different. Builds that showed no left-out
repeats were run-to-run variation in the verdicts plus a long budget, not a broken
check. Reasons name lectures by their labels ("Lecture 8 adds…").

**More pairs (2026-11-24).** Two matches per section and 6 pairs a lecture: a
3-hour CS329Z build compared 42 pairs (was 28) in 117 s and judged 18 overlaps, 24
different, 0 repeats. The pairs judged repeats before (context engineering in Lectures
2 and 6, skills in 6 and 7) were judged overlaps this time, each with a specific
"adds" (KV-cache invalidation; skills vs tools). The line between repeat and overlap
was a judgment call that varied run to run (until the check moved to temperature 0,
see step 3); overlapping sections both stay eligible, which is the safer error. A
hand check of CS329Z's verdicts at temperature 0 found 6 of 7 right; the misses lean
to repeat (a preview of a later lecture; the same worked example in two lectures) and
cost about 2 minutes of a 3-hour playlist, so the prompt wasn't made stricter.

**Lecture order.** `reorder_course` stores a new order (every lecture once). A saved plan
isn't rebuilt: every `lecture` number in it (parts, references, pairs, checklist) and in
saved Ask the course answers is renumbered by `video_id` when read, and its parts are
played in the new lecture order, so labels and watched parts stay right. A plan goes
stale only when the set of lectures changes, not their order. Building it again redoes
what depends on order (which half of a repeat is the earlier one, the checklist).

**Your own course playlist.** Like a video's custom playlist: the learner picks sections
from every section of the lectures the course uses (`course_playlist_sections`, marking
Curio's picks), stored in `course_custom_playlists` as `<analysis id>:<section id>`.
Exactly Curio's picks, or none, means Curio's. On read, `custom_course_parts` turns them
into parts the same way the plan does (merged within a lecture, `also_in` from the
plan's judged pairs), so a part the learner kept has the same `<video id>@<start>` key and
its watched state. Sections of an analysis the course no longer uses drop out. The
course quiz and flashcards cover the learner's playlist when there is one (it goes stale
when they edit it). The course map and playlist switch between "Yours" and "Curio's
picks"; a skip section the learner added is drawn as optional.

**Pins.** Sections the learner pins (`course_pins`, `<analysis id>:<section id>`) are
always in Curio's course playlist: `build_course_plan(pinned=…)` keeps them as items
even when marked skip (relabeled optional for the selector), and
`select_sections(required=…)` takes them first, whatever they cost, before the top
sections and the knapsack, which share the time left. A section a pin repeats is left
out; two pins that repeat each other both stay. Pins longer than the playlist all go in
and nothing else does, with a warning. Pins are part of the plan key, so changing them
makes the next build a real one.

**Unwatched first.** When a plan is built, the sections the learner has watched
*anywhere* (`_watched_sections`: at least 80% of the section's time lies in stretches
marked watched in a video playlist, `completed_spans`, or in any course; a course part
key is looked up in that course's playlists, an older key gives the section playing
just after its start; matching by time lets a video analyzed for another goal count)
start `WATCHED_PENALTY` (15) points lower, like a repeat's weaker half, so new material wins near ties and a
strong watched section can still stay. After the build, a new part made only of
watched sections is marked watched, so marks survive a part's start moving. Watched
marks aren't part of the plan key: a rebuild without `force` still reuses the plan.

**Learning paths.** `combine_courses` makes a course from several courses' lectures in
order; everything above then works across courses.

**Curricula** (`analysis/curriculum.py`). A curriculum is a course with a row in
`curricula` (`goal`, `order_mode` `lectures` | `sections`, `groups` mapping each video
to its source course or `""` for a single video, `auto_order`, `notes`), so the page,
plan, pins, watched marks, Ask and practice all work unchanged. Its scoring goal is
the one most of its analyses were made with; nothing is re-analyzed. Lectures with no
analysis at all (`curriculum_missing`) are queued with that goal in the background, as
an imported course's are (`queue_course` skips every other lecture of a curriculum,
so one analyzed for another goal isn't queued again).
- **Suggesting sources** (`suggest_sources`, one call, temperature 0): every course
  (not curricula) and every analyzed video outside them, with lecture titles or the
  overview and key concepts, under short ids (`c1`, …); the model says include or not
  and why. If the call fails, candidates sharing a content word with the goal are
  suggested. "Build AI agents" on the calibration library: CS329Z, Karpathy's LLM talk
  and 3Blue1Brown's neural networks in, algorithms, the brain lecture and the sleep
  podcast out, each with a reason (24 s cold, 6 s warm).
- **Needs** (no model calls but embeddings): a section's `prerequisites` phrase not taught earlier
  in its own video, nor by an earlier lecture of its own course, is an outside need;
  sources of *other* courses teach it when one of their concepts (`introduces_concepts`,
  `key_concepts`) covers it: the concept holds every content word of the need ("Neural
  network as a function" covers "basic neural network concepts"), or the need holds a
  concept of two or more words, sharing a word that isn't broad ("model", "agent",
  "system"). Filler ("basic", "understanding of") is dropped; words are roughly singular.
  A one-word need is taught by another source's words only through that very concept
  ("Python", or "Large Language Models" read as "LLM"): an analysis of Karpathy's talk
  listed "Python Interpreter", and CS329Z's "Basic Python" prerequisites matched it.
  Whether a video or its course already covers a need stays lenient (Karpathy's own
  "LLM Inference" covers its "Basic understanding of LLMs"; strict there, the talk
  seemed to need CS329Z lecture 1 and dropped behind it). A run of 2–4 words whose initials the library writes in capitals somewhere
  (`library_acronyms`: "LLM", "RAG", "SFT") reads as that acronym, so "Basic knowledge
  of LLMs" matches "Large Language Models". A need also matches a concept close in
  meaning: cosine ≥ 0.8 of `nomic-embed-text` vectors (`SemanticMatcher`). On CS329Z's
  212 prerequisites × 1,158 concepts, 0.8 and up were right ("Understanding of agent
  architectures" ~ "Agent Architecture", missed by words because both words are
  broad), 0.75–0.8 mixed ("Policy Gradient methods" ~ "Feedback gradients"), and the
  "LLM" probe never got past 0.72, hence the acronyms. Vectors are saved per phrase
  (`phrase_vectors`, float32, keyed by model and hash): the first CS329Z curriculum
  embedded ~1,400 phrases in 7.5 s, later orderings take ~2 s (pure-Python cosine).
  Without an embedder, or when it fails, words alone; page reads never embed.
- **Whole lectures** (`source_edges`, `order_sources`): A goes before the first
  lecture of a course (or a lone video) that needs it when A teaches that course's
  needs with weight ≥ 1.5, summed over its lectures (each distinct need of a lecture
  counts 1, split among the sources that teach it; one lecture's five "Basic Python"s
  count once), and at least twice what the course teaches of A's. Per lecture,
  3Blue1Brown (one need each of lectures 2 and 5) never moved. A course's lectures always keep its
  order; edges closing a cycle are dropped, weakest first. The order is the given one
  with each source's predecessors pulled in just before it, so an unneeded video keeps
  its place (picking the lowest-ranked ready source let the sleep podcast jump to
  second whenever CS329Z waited on a foundation). Matching each lecture's needs with
  everything put CS329Z's lecture 7 before lecture 2; hence the fixed course order.
  `notes` say once per moved source what it teaches the first lecture that needs it.
  Lectures without an analysis stay after their old neighbour. `submit_course_plan`
  re-orders before every build unless the learner reordered by hand (`auto_order`).
- **Section by section** (`order_sections`, plan `order: "sections"`): chosen
  sections in lecture order, each source's in time order and each course's lectures in
  order, with a chosen teaching section pulled ahead of a chosen section of another
  course that needs it. `course_parts(sequence=…)` then merges only sections that
  follow each other in that order and in their lecture; `course()` doesn't re-sort
  such a plan by lecture. The learner's own playlist follows the same order when the
  plan on screen was built section by section (`custom_course_parts(by_section=…)`).
- **Needs as prerequisites:** in both modes, a teaching section another course needs
  joins the needing section's `builds_on`, so the selector adds it when it fits (and it
  plays first). A plan only orders what it holds, so after the selection
  `cover_needs` (`analysis/playlist.py`) makes sure each chosen section's outside need is
  taught: for a need no chosen section teaches, the shortest teacher goes in, in the
  time left when it fits, else in place of the picks worth least per minute
  (`_pick_value` / cost), as long as they're worth less than the teacher and the section it serves
  together, and within `need_share` (10%) of the playlist across all swaps. Pins,
  teachers already serving a need and the sections they serve are never swapped out;
  needs go best section first. Whole lectures play in source order, so there only a
  teacher from an *earlier* source counts; section by section, any teacher does (it
  moves ahead). `cover_needs` returns an outcome per need of a section still in the
  playlist (`in_playlist`, `added` with what it `made_room` for, or `untaught` with the
  shortest teacher), saved as `plan.needs` (one per section and prerequisite, in course
  order; older plans have none). The curriculum panel's **What builds on what** lists
  what was added, what made room and what wasn't taught; a playing part shows **Builds
  on** (its needs and their teachers) and **In the playlist because** / **Lays the
  ground for** (the sections that need it). These replace the plan warnings an untaught
  need first got. An untaught need says why (`why`): the swap would cost more than it
  adds (`worth`), past the share (`share`), the teacher repeats a part in the playlist
  (`repeat`) or is marked skip (`skip`); the page words the first two as "didn't fit
  this length".
- **The share and the swap rule, swept** (2 libraries: live and test; 4 source orders:
  Curio's, Karpathy last, both videos last, the course reversed; 1, 2, 3 and 5 hours;
  both modes; share 0, 5, 10, 15, 20% and no cap; 384 builds, stub model). Needs of
  chosen sections taught: 27% without swaps, 65% at 5%, 68% at 10% and the same at
  every larger share: the cap never bound above 10%, and the most any playlist wanted
  was 6.6% (test library, 3 hours, section by section). All 33 needs left untaught
  (of 104) were stopped by the worth rule, 26 of them in 2-hour playlists, where the
  picks are nearly all top sections and swapping in a 3.5–4.5 minute foundation would
  cost more than it and the section it serves are worth; at 3 and 5 hours they go in.
  Swaps push out a little more time than they add (sections are whole), leaving the
  playlist 1.1 minutes shorter on average (at most 3), not worth a refill step. Kept:
  10% and the rule as they are. Before this, a teacher went in
  only as a `builds_on` prerequisite that fit the time left, and a source placed late
  by hand stayed late. First tries, on the curriculum test library with Karpathy put
  last by hand (3 hours, section by section): dropping the picks worth least overall
  freed too little time (they are the short ones) and left Lecture 2's "LLM inference"
  untaught; comparing the loss with the needing section alone, not the teacher too,
  did the same. Result: Karpathy's "What is an LLM", "Demo: Tool Use" and "The LLM as
  an Operating System" play after Lecture 1's first part, with 3Blue1Brown's "Series
  scope" just before them, for Lecture 1's Reflexion lead-in, Lecture 6's ReAct and
  3.7 minutes of Karpathy's security part. In Curio's own order, whole lectures, it
  adds Karpathy's "What is an LLM" (which Lecture 2's test-time scaling needs) and its
  LLM OS section.
- **A teacher brings its own teachers along:** `order_sections` keeps an edge from
  every chosen teacher to the chosen section that needs it, even when the teacher
  already comes first. Before, an edge was kept only when the teacher came later, so
  a section pulled ahead of a third source left its own teacher behind (Karpathy's
  "What is an LLM", moved up for CS329Z, played before 3Blue1Brown's neural
  networks).
- Calibration library, offline (a stub model, so no repeats or checklist): CS329Z +
  Karpathy + 3Blue1Brown given last + algorithms + sleep → Lecture 1, Karpathy,
  Lectures 2–4, 3Blue1Brown, Lectures 5–9, guest lecture, then sleep and algorithms;
  3 hours, 48 parts. Section by section, Karpathy's last two sections (which build on
  retrieval) come after Lecture 3.
- API: `GET /curricula/candidates`, `POST /curricula/suggest {goal}`,
  `POST /curricula {goal, title?, sources[{kind, id}], order}` → `{course_id}`,
  `PUT /courses/{id}/curriculum {order?, auto_order?}` → `Course` (with `curriculum`).

**Course quiz and flashcards.** A study set per lecture with scope `course`: its
`segment_ids` are the section ids the course playlist uses from that lecture, and its
scope key is `course|<course id>|<hash of the sections>` (so the same sections reuse
it, and the course is known from the key). One question and one card per section
(`generate_study(budget=(1, 1))`), then one call writes a card question for each
"easy to miss" point; the point itself is the back, attached to the set of the
lecture it cites first, at that section. In review, decks are keyed by analysis and
course, so a course's cards don't replace a lecture's own. On CS329Z (7 lectures, 60
sections): 60 questions and 72 cards in 7 minutes.

**Page layout.** The course map, player and course playlist stay on top; Easy to
miss, Quiz and flashcards, Ask the course, Repeats and Lectures are tabs below, as
on a video page. Everything else in the app is one menu away (top bar on every
page).

**Playback.** One YouTube player; a clip names its video, so the course plays parts
of several lectures in a row. Watched parts are stored per course by
`<video id>@<start second>`, which survives rebuilds that keep the part. YouTube
errors 101 and 150 (embedding turned off by the owner) show a link to that moment on
YouTube instead.

## 14. Phase 1 MVP milestone

**Done when:** pasting the test lecture (`e_m99aR7_3Q`, 80 min) produces a
validated playlist of about 15–30 minutes in which every segment has an accurate
timestamp, a title, what you will learn, and why it is worth watching; clicking
a segment plays the embedded video from that point; results persist and appear
under "Recently analyzed"; `pytest` passes without network access.

Out of scope for Phase 1 (planned in later phases): Ask the Video, notes, quizzes and
flashcards, progress tracking, saved custom playlists, embeddings and vector search,
and multi-video curricula.
