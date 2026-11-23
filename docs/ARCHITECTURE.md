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
├── README.md                 install / configure / run
├── docs/
│   ├── ARCHITECTURE.md       this file
│   └── PROGRESS.md           development log and milestone status
├── src/curio/
│   ├── config.py             pydantic-settings, env-driven
│   ├── logging_setup.py
│   ├── domain.py             Pydantic domain models (single source of truth)
│   ├── service.py            CurioService façade used by every frontend
│   ├── ingestion/
│   │   ├── url.py            YouTube URL → 11-char video id
│   │   ├── metadata.py       MetadataProvider protocol + yt-dlp impl (no download)
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
│   │   ├── profiles.py       Beginner / Technical / Executive / Deep Dive + custom
│   │   ├── prompts.py        all prompt text in one place
│   │   ├── outline.py        topic segmentation over the full transcript
│   │   ├── evaluate.py       contextual rubric evaluation of each section
│   │   ├── redundancy.py     lexical overlap between sections
│   │   ├── scoring.py        weighted rubric + rule-based adjustments
│   │   ├── playlist.py       time-budgeted selection, merging, ordering
│   │   ├── validation.py     timestamp / coverage / consistency checks
│   │   ├── boundaries.py     sentence-level section starts (step 6b)
│   │   ├── ads.py            cut sponsor reads out of content sections (step 6c)
│   │   └── pipeline.py       orchestrates the steps, reports progress
│   ├── storage/
│   │   ├── orm.py            SQLAlchemy tables
│   │   └── repository.py     persistence API (cache lookup, recent videos)
│   ├── api/app.py            FastAPI app
│
├── web/                      React + Vite + TypeScript frontend
│   └── src/                  pages (Home, VideoPage), WatchMap, Player, api.ts
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
```

All timestamps derive from caption cues. The LLM refers to **block numbers** (outline)
and **sentence numbers** (boundary refinement), never to raw seconds, so it cannot
invent a timestamp: code converts the numbers to the original cue times.

## 5. API contract (FastAPI, `/api/v1`)

| Method | Path | Body / result |
|---|---|---|
| `GET`  | `/health` | `{status, llm_provider, model}` |
| `POST` | `/analyses` | `{url, preferences}` → `202 {analysis_id, status}`; runs in background. Returns the cached analysis if one exists for the same video + preferences + model. |
| `GET`  | `/analyses/{id}` | `VideoAnalysis` (with `status`: `pending`/`running`/`done`/`failed`, `progress` message, `error`) |
| `GET`  | `/videos/recent?limit=10` | `[ {video, analysis_id, profile, analyzed_at, playlist_s, watch_map[]} ]` |
| `GET`  | `/videos/{video_id}/transcript` | `Transcript` (normalized blocks) |
| `GET`  | `/analyses/{id}/completed` | ids of segments marked watched |
| `PUT`  | `/analyses/{id}/segments/{segment_id}/completed` | `{completed: bool}` |

Errors use `{"detail": {"code": "...", "message": "..."}}` with codes
`invalid_url`, `video_unavailable`, `transcript_unavailable`, `rate_limited`,
`llm_error`.

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
| 6b | Sentence-level boundaries | Clean cues are grouped into sentence units (about 4 s on average for punctuated captions; a unit starts at a cue when the previous cue ended a sentence, after a pause or speaker change, or when a new sentence begins mid-cue). **LLM pass 1b:** for every boundary, the model sees the ending and starting sections plus the numbered sentences from one block before to one block after the boundary, never crossing into other sections, and picks the first sentence of the new section. Code moves the start to that unit's cue time and ends the previous section on the sentence before it, rejecting choices that are out of range or would leave a section under 30 s. On failure, block boundaries are kept with a warning. Evaluation and redundancy then use the text that plays between the refined times. **Captions with unreliable punctuation** (fewer than 5 sentence marks per 100 words; human captions measure about 7, YouTube auto-captions 0–3.8) use each caption cue (about 2.5 s) as a unit instead, because a 12 s chunk of unpunctuated text rarely starts where a topic does. Toggle: `CURIO_REFINE_BOUNDARIES`. |
| 6c | Sponsor reads | Mid-roll ads often open with a segue that sounds like content, and the outline can miss them. Code flags units that look like ad copy (web address, "sponsor", "promo code", "brought to you by", "link in the description") and groups them into windows reaching 75 s back and 25 s forward. **LLM pass 1c** confirms each window and gives the ad's first and last sentence, including the segue, plus the sponsor's name; passing mentions (a paper's URL, a tool being explained) are rejected. Each confirmed ad of 10 s or more becomes a `sponsor` section (always skipped), carved out of the sections it overlapped. A split section's second part gets "(continued)". Content fragments under 20 s join neighbouring content, never the ad. No LLM call when nothing looks like an ad. Toggle: `CURIO_DETECT_ADS`. |
| 7 | Scoring | **LLM pass 2 (evaluate):** rubric scores from 1 to 10 plus structured flags. `scoring.py` combines them with the configurable weights (30/20/20/15/15), then applies rule-based adjustments: kind priors (sponsor/meta → skip), a penalty for very short sections, a novelty bonus for introducing concepts, a lexical redundancy penalty, LLM-flagged redundancy, a standalone penalty. Every adjustment is recorded with its reason. Half of each base score comes from the section's rank among the video's content sections (`within_video_rank`), because local models rate almost everything 7–10; sponsor and housekeeping sections are excluded from that ranking. Content (core/example/demo) the AI rated 50 or above and not redundant is never pushed below optional by ranking alone (`content_floor`). |
| 8 | Redundancy | Bag-of-words cosine similarity between sections, combined with the LLM's `redundant_with` flags. The lower-scored section of a near-duplicate pair is demoted, not deleted. |
| 9 | Playlist | Tier by score (essential ≥ 70, recommended ≥ 55, optional ≥ 40, otherwise skip; thresholds configurable). Budget: user target, or 30% of duration clamped to 10–35 min. Essentials first, then the rest by score per minute; prerequisites (`builds_on`) are pulled in. Adjacent picks with a gap under 15 s are merged into one segment. Output is chronological. |
| 10 | Validation | Segments inside `[0, duration]`, `start < end`, no overlaps, every boundary is a real caption-cue time, non-empty explanations. Sections tile the transcript **in time**: ordered, no overlaps, and no gap that contains transcript speech (silence is fine). Playlist within budget. Problems are recorded as warnings, and fixable ones are repaired. |
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
  prompt, `think=False` for JSON calls, temperature 0.2.
- **Claude:** `messages.stream` with `output_config.format` (JSON schema) and
  `effort`, adaptive thinking, server-side refusal fallback (`fallbacks: "default"`).
- Per-task model selection: `CURIO_LLM_MODEL_OUTLINE`, `CURIO_LLM_MODEL_EVALUATE`,
  with `CURIO_LLM_MODEL` as the default.
- Provider selection: `CURIO_LLM_PROVIDER=auto|ollama|anthropic`. `auto` picks
  Anthropic when `ANTHROPIC_API_KEY` is set, otherwise Ollama.

## 8. Deployment (Podman)

```
 host                                             container "curio" (rootless Podman)
 ┌──────────────────────────────┐                 ┌───────────────────────────────────┐
 │ browser → 127.0.0.1:8470 ────┼── published ───►│ :8000  uvicorn → FastAPI           │
 │                              │                 │         ├─ /api/v1  CurioService   │
 │ Ollama :11434 (GPU) ◄────────┼── 169.254.1.2 ──┤         └─ /       web/dist (SPA)  │
 │                              │  (pasta map)    │ /app/data ── volume "curio-data"   │
 └──────────────────────────────┘                 └───────────────────────────────────┘
```

- `Containerfile`: a Node 22 stage builds `web/dist`; the runtime stage is Python
  3.12-slim with uv 0.11.7, `uv sync --frozen --no-dev`, a non-root user, and a
  health check on `/api/v1/health`.
- `curio.sh`: build, start, stop, restart, run, status, logs, analyze, shell.
  It publishes on `127.0.0.1` only, keeps the SQLite database in a named volume,
  checks that the port is free and that the container can reach Ollama.
- The LLM stays on the host: the image holds no model, and with
  `ANTHROPIC_API_KEY` in `.env` Ollama isn't needed at all.

## 9. Phase 1 MVP milestone

**Done when:** pasting the test lecture (`e_m99aR7_3Q`, 80 min) produces a
validated playlist of about 15–30 minutes in which every segment has an accurate
timestamp, a title, what you will learn, and why it is worth watching; clicking
a segment plays the embedded video from that point; results persist and appear
under "Recently analyzed"; `pytest` passes without network access.

Out of scope for Phase 1 (planned in later phases): Ask the Video, notes, quizzes and
flashcards, progress tracking, saved custom playlists, embeddings and vector search,
and multi-video curricula.
