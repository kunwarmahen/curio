# Development progress

## Milestone status

| Phase | Status |
|---|---|
| Architecture and design (`docs/ARCHITECTURE.md`) | ✅ done |
| **Phase 1: Functional MVP** | ✅ implemented, tested offline, verified end to end on 4 real videos (lecture, podcast, two auto-captioned talks), locally and in Podman |
| Web UI | ✅ React + Vite with watch map and IFrame player (replaced Streamlit) |
| Deployment | ✅ Podman image + `curio.sh` |
| Phase 2: Intelligent learning | ✅ learning profiles, custom objective, redundancy detection, skip recommendations, sentence-level boundaries, mid-roll ad removal and learning notes |
| LAN access | ✅ app and Podman container listen on all interfaces by default |
| Phase 3: Interactive learning | 🟡 Ask the Video, quizzes and flashcards with spaced repetition, a remembered default goal, and segment completion tracking; saved custom playlists are not built |
| Overnight queue | ✅ queue videos any time; analyzed 1–7 AM while the GPU is idle |
| Phase 4: Multi-video intelligence | 🟡 courses from YouTube playlists, one course playlist across lectures without repeats (every verdict shown), an "easy to miss" checklist, Ask the course, and a course quiz and flashcards; curricula across courses are not built |

## Phase 1 log (2026-11-23)

### Built
- `ingestion`: URL parsing (watch, youtu.be, shorts, embed, live, bare id);
  yt-dlp metadata without download; caption track selection (manual → auto →
  translated) with typed errors.
- `processing.normalize`: cue cleanup (annotations, speaker markers, HTML
  entities, rolling auto-caption duplicates, overlapping cue times) and merging
  into 20–45 s blocks that end at sentence boundaries.
- `llm`: provider protocol, structured generation with Pydantic validation
  and a retry that feeds the validation error back. Ollama (constrained JSON
  schema, fixed context with truncation guard) and Anthropic (structured outputs, adaptive
  thinking, streaming, prompt caching of the shared outline, refusal fallback).
- `analysis`: outline (single pass or windowed), contextual evaluation,
  TF-IDF redundancy, explainable scoring, profile-aware weights and kind priors,
  time-budgeted playlist with prerequisite pull-in and merging, validation and repair.
- `storage`: SQLAlchemy/SQLite (videos, transcripts, analyses, segments),
  cache keyed by video + preferences + model + pipeline version.
- `service`, FastAPI (`/api/v1`), CLI. (A Streamlit UI was built first and then replaced; see the UI log below.)
- 98 offline tests at the end of Phase 1 (105 with notes, 115 with Ask the Video, 121 with the overnight queue, 131 with quizzes, flashcards and the default goal, 134 with overnight notes and quizzes, 140 with courses, 144 with Ask the course, 146 with the course quiz) (ad detection and carving, sentence units and boundary refinement, URL parsing, normalization, segmentation repair, scoring and
  calibration, playlist and prerequisites, validation, LLM retry, request shapes and
  truncation guard, service caching, stale runs, API).

### Problems found and fixed
1. **Package name collision:** a top-level `curio` package breaks httpx, anyio and
   Starlette, because `sniffio` assumes any module named `curio` is the curio async
   library. The package was renamed to `curio_learn`; product and CLI names are unchanged.
2. **Schema bug that dropped titles:** the schema tightener stripped JSON Schema
   `title` annotations and, by mistake, the section property *named* `title`, so
   Ollama's constrained decoding could never produce section titles. Fixed, with a
   regression test.
3. **Stale in-progress analyses were reused:** an analysis left "running" by a
   killed process was handed back as if it were live. In-progress reuse now
   requires a progress update within the last 20 minutes, the synchronous CLI path
   never returns an unfinished analysis, and the API and UI mark interrupted runs
   as failed at startup.
4. **Grade inflation made the tiers meaningless:** qwen rated nearly every section
   7–10, so 13 of 15 sections were "essential", and the new-concepts bonus (+6)
   applied to every section. Half of each base score now comes from the section's
   rank within the video (`within_video_rank`, shown with its reason). Novelty counts
   only concepts a section *introduces*, and core sections that introduce nothing
   new lose points.
5. **Prerequisite chains crowded out the best sections:** recursive pull-in of
   `builds_on` (the model marks almost every section as building on the previous
   one) spent the budget on prerequisites and then dropped MIPROv2 (score 93),
   the section that needed them. Now only direct prerequisites rated recommended or
   better are pulled in, and only when they fit together with the section itself.
6. **Ollama reloaded the model and offloaded it to the CPU:** a different `num_ctx`
   per call forced reloads, and contexts up to 64k put 14% of the model on the CPU.
   Every call now uses one fixed context (32k by default; 100% GPU on a 24 GB card).
   Ollama's reported prompt token count is checked so a silently truncated
   transcript raises an error. (Measured: estimate 21,278 vs. actual 20,927 tokens.)
7. **Startup cleanup failed live runs in other processes:** the API and UI marked
   *every* running analysis as failed at startup, including a CLI run in progress.
   Cleanup and reuse now share a 20-minute staleness rule.

### Verified result: `e_m99aR7_3Q` (Stanford CS329Z Lecture 9, 80:34, manual captions)

| | First run | After fixes |
|---|---|---|
| Total analysis time (qwen3.8 27B, RTX 3090) | 1,240 s | **147 s** |
| Outline call / evaluation batches | 330 s / ~455 s each | 63 s / 40–44 s |
| Sections | 15 | 16 |
| Tiers (essential/recommended/optional/skip) | 13 / 1 / 1 / 0 | 7 / 4 / 2 / 3 |
| Playlist | 26:10, 3 segments | **24:05, 3 segments, saves 56:29** |
| Validation | ok | ok, 0 warnings, 0 repairs |

Playlist after the fixes: GEPA (40:18–46:51) · SFT → PPO/GRPO (51:06–1:06:55) ·
choosing between context and weight optimization (1:18:48–1:20:31). Skipped:
intro and poll, the learning-objectives slide, and the brief TextGrad aside.
Boundary spot check (transcript text either side of 4 boundaries): GEPA ("Then let's
talk about genetic Pareto prompt evolution"), RL ("Let's talk about reinforcement
learning") and the closing section ("Which strategy should you use?") start exactly at
the transition. **SFT starts one block (~30 s) early**, in the tail of a GEPA Q&A answer.

Observations:
- Picks vary between runs (temperature 0.2). Run 1 chose MIPROv2 instead of SFT.
  Both are defensible, but stability across runs is worth measuring.
- At the default 30% budget, a section can be "essential" yet not in the playlist
  (here MIPROv2 and on-policy distillation). The UI lists it under "More
  segments", and a target watch time includes it.

### UI: Streamlit replaced with React + Vite (2026-11-23)
The user prefers not to use Streamlit. `web/` is a React 19 + TypeScript app
(Vite) on the FastAPI API; `uv run curio api` serves the built `web/dist`
with client-side routes (`/`, `/v/<analysis id>`). Streamlit, its `curio ui` command
and its dependency are gone.
- Watch map: the whole video as one line, sections highlighted by value, the
  playlist underlined, and a live playhead. Stretches are keyboard-focusable
  buttons that play that section.
- The YouTube IFrame API replaces the static embed. It cues the first part without
  autoplay, stops at the end of a part, marks it watched, continues to the next
  (can be turned off), and offers 1×–2× speed.
- New API: `GET /analyses/{id}/completed`; recent videos include their watch map.
- Checked: production build and oxlint clean; desktop (1440 px) and mobile (390 px)
  screenshots; routing (deep links, unknown API → 404, path traversal → index.html);
  the YouTube player iframe is created. **Not verified:** real playback behavior
  (cue, stop at the part's end, auto-advance), because headless Chrome doesn't play
  YouTube video.

### Sentence-level boundaries (2026-11-23)
New step 6b (`analysis/boundaries.py`). Caption cues are grouped into sentence units
(1,110 units for this lecture, about 4.3 s each). One batched LLM call picks, for each
boundary, the first sentence of the new section from the sentences around it. Code moves
the start to that unit's cue time and rejects choices that are out of range or would
leave a section under 30 s. Evaluation now uses the text that plays between the refined
times. Also fixed: selection didn't count the 2 s lead-in and 1 s tail per segment, so a
playlist could finish a few seconds over its budget.

Result on `e_m99aR7_3Q` (run 3, 22 sections):
- All 21 boundaries checked: each now starts on the speaker's transition sentence
  ("Then let's talk about bootstrap reasoning", "Next paper I'm going to talk about is
  MIPROv2", "Let's talk about reinforcement learning", "Which strategy should you
  use?"). Wrap-up lines ("Cool.", "No questions.", "So any questions about that?")
  stay with the ending section.
- **SFT now starts at 51:27** ("Then we are going to talk about weight optimization"),
  instead of 51:08 inside a GEPA Q&A answer.
- 20 of 21 starts moved later, by 0.5–18.6 s. The refinement call took 20 s;
  validation passed with no warnings or repairs.
- Total time 201 s, up from 147 s: the 20 s refinement call, plus a larger outline
  this run (22 sections vs. 16; 85 s vs. 63 s for the outline call).
- Playlist: GEPA (40:18–47:09) · SFT introduction (51:25–53:33) · PPO → GRPO →
  DeepSeek-R1 (58:14–1:12:57) · choosing a strategy (1:18:53–1:20:31); 25:18 total.
- Remaining resolution limit: captions have no word timings, so when a new topic
  begins mid-cue the start is that cue's start, up to a few seconds early.

### Podman (2026-11-23)
`Containerfile` (Node 22 build stage for `web/dist`; Python 3.12-slim runtime with
uv 0.11.7, non-root user, health check) and `curio.sh` (build, start, stop,
restart, run, status, logs, analyze, shell). The image is 390 MB and holds no model:
Ollama stays on the host GPU.

Problems found and fixed while testing:
1. **Port 8000 was taken** by another app on this machine (the `blockoku`
   container). Curio now uses **8470** everywhere (`curio.sh`, `curio api`'s default,
   and the Vite dev proxy). `curio.sh` checks the port first and says how to choose
   another (`CURIO_PORT=…`) instead of passing on Podman's raw error.
2. **The container couldn't reach Ollama.** Under rootless Podman's `pasta`
   network, `host.containers.internal` resolved to the host's LAN address
   (192.168.1.44), which pasta treats as the container's own, so connections were
   refused. The script now maps `169.254.1.2` to the host (`--map-guest-addr`) and
   points `host.containers.internal` at it. `start` verifies the container can
   reach Ollama and warns if not.
3. **The health check was dropped:** Podman's default OCI image format ignores
   `HEALTHCHECK`, so the image is built with `--format docker`.

Verified: build; start, which waits for health and confirms Ollama is reachable from
the container (version 0.32.15); status (exit code 1 when stopped); logs; `analyze`
(usage message without a URL, CLI errors passed through); data in the `curio-data`
volume survives restart; the container runs as non-root; the port check reports a
conflict. A full analysis inside the container (`./curio.sh analyze`, the 80-minute
lecture) completed in 218 s against the host's Ollama.

### Calibration on more videos (2026-11-23)
Chosen to stress different things, each with a different learning profile:

| Video | Type | Captions | Profile |
|---|---|---|---|
| `RFccAuyPPOg` ElevenLabs founder interview (David Senra), 71 min | podcast | manual | general |
| `s_o8dwzRlu4` Kubernetes Crash Course (TechWorld with Nana), 72 min | tutorial with demos | auto, **no punctuation** | beginner |
| `rPsMwpFmdf0` PostgreSQL Performance Mastery (AWS Events), 59 min | conference talk | auto, sparse punctuation | technical |

**First pass: what worked.** Sponsor blocks, intros, host small talk and course
promotion were found and skipped with specific reasons. Interview boundaries started at
the host's questions. Tutorial concept and demo halves were labeled correctly.

**Problems found and fixed:**
1. **A mid-roll ad was hidden inside an "essential" section.** In the podcast, a 71 s
   AppLovin read (41:49–43:00) opened with a segue that sounds like content ("I found
   one of my all-time favorite quotes… Zero to One") and the outline kept it inside a
   content section. New step 6c (`analysis/ads.py`): code flags ad-like sentences and
   the LLM confirms each window and names the ad's first and last sentence. Rerun:
   cut out at **41:49–42:58**, starting at the segue. The Kubernetes sponsor read
   (25:49–26:27) was also found; in the lecture, the ad check correctly found no ad.
2. **Boundaries fell mid-sentence on auto-captions.** With no punctuation, 12 s
   "sentence" units started anywhere ("certified kubernetes ‖ administrator now we…").
   Measured punctuation: human captions ≈ 7 sentence marks per 100 words;
   Kubernetes 0.01; PostgreSQL 3.8 (26-word "sentences"). Below 5 per 100 words, each
   caption cue (≈ 2.5 s) is now a unit. Rerun: Kubernetes starts land on the
   speaker's cues ("so now let's see another very important…", "before moving on I
   want to give a shout…"). PostgreSQL still often starts a few words early,
   because its cues are long lines and the topic changes mid-cue; captions carry no
   word timings, so this is the resolution limit.
3. **Solid content was labeled "safe to skip"** purely by within-video rank (the
   podcast's founding story: rated 51, ranked 17 of 18 → 32 → skip). New
   `content_floor` rule: core/example/demo sections the AI rated ≥ 50, and not
   redundant, stay at least optional, with the reason shown.
4. **Calibration raised junk sections:** the sponsor and outro sections (rated 0)
   tied at the bottom, which lifted the intro (rated 1.7) by +6.8. Sponsor and
   housekeeping sections are now left out of the ranking.
5. **Repeated caption cues lost their time.** A cue that repeated the previous one
   word for word was dropped instead of extending the previous cue's end, so its
   seconds vanished from the transcript. Fixed in `clean_cues`, with a regression test.
6. **Tiny content fragments were absorbed into ads.** When an ad was cut out near a
   boundary, a 12 s piece of real content was labeled sponsor. Fragments now join
   neighbouring content and are absorbed into an ad only when ads surround them.
7. **Outline granularity varied 13–28 sections** for the same 80-minute lecture, and
   the 13-section run lost GEPA from the playlist. The outline prompt now gives an
   expected count from the duration (2.5–5 min per section; 16–32 for 80 min). One
   rerun in the container: 28 sections, and the playlist covered both halves (STaR,
   MIPROv2, GEPA, GRPO, strategy).
8. Section tiling is now validated **in time** (ordered, no overlaps, no gap
   containing speech) instead of by block numbers, since ads can be cut out mid-block.

**Results after the fixes** (pipeline v4; lecture v5 in the container):

| Video | Playlist | Tiers (ess/rec/opt/skip) | Skipped, with reasons |
|---|---|---|---|
| Podcast | 22:26 of 71:07 | 7 / 4 / 4 / 4 | 2 sponsor reads, 2 low-rated personal-story sections |
| Kubernetes | 23:42 of 72:03 | 9 / 3 / 2 / 3 | intro, sponsor read, paid-course outro |
| PostgreSQL | 18:30 of 59:20 | 4 / 2 / 2 / 3 | host small talk, brief index overview deferred to another session, AWS course promo |
| Lecture | 26:14 of 80:34 | — | intro, objectives slide, 3 audience Q&A blocks, results recap |

Observations, not yet acted on:
- The beginner Kubernetes playlist leaves out "What is Kubernetes and why it
  exists" (rated 82). It is a prerequisite of a prerequisite, and only direct
  prerequisites are pulled in (on purpose, since run 1, when chains crowded out the
  best sections). For an all-strong video and a 22-minute budget this is a judgment
  call; a beginner-specific rule could put foundations first.
- Per-call times were 2–3× slower during these runs than earlier. A repeat benchmark
  afterwards matched the earlier one exactly (21k-token prefill 19.0 s, 61 tokens/s),
  and another app on this machine (`aksharallm.portal`) started at the same time, so
  GPU contention is the likely cause. Not confirmed.
- Outline variance is reduced, not eliminated: one anchored run is not proof.

### Learning notes (2026-11-23)

Built `analysis/notes.py`, a `notes` table, four API routes, `curio notes`, and a
Notes tab (now the default tab on the video page). Design and grounding rules are in
ARCHITECTURE.md §9. 7 new tests (105 in total): sentence numbers map to cue
times, out-of-range numbers fall back and are counted, takeaways naming unknown
sections are dropped, the three scopes, reuse and `force`, stale notes are failed
at startup, the API flow and the Markdown export.

Verified with Ollama (`qwen3.8-64k`) on stored analyses:

| Video | Scope | Sections | Batches | Time |
|---|---|---|---|---|
| CS329Z lecture (manual captions) | playlist, 26 min | 4 | 1 | 68 s |
| Kubernetes crash course (unpunctuated auto-captions) | whole video, 71 min | 16 | 2 | 1 min 51 s |

- Every specific claim in the lecture notes was checked against the transcript at
  its cited time: FireAct (34% vs 31%), "four models to keep in memory" for PPO,
  GRPO beating PPO on math for DeepSeek-R1, Llama 2 fine-tuned on GPT-4 rollouts, Qwen3's AIME results (SFT 55%, RL 67%,
  on-policy distillation 74%), GEPA improving faster than GRPO per rollout. All were
  said, at the time cited. No sentence number was out of range in either run.
- The cue-level units of auto-captions work as well: the Kubernetes notes cite
  the right moments for Pods, Services, Ingress, ConfigMaps and the Minikube demo.
- Browser check (Playwright, desktop and 390 px): notes render, selecting a
  timestamp plays from that moment to the end of its segment and scrolls to the
  player. A clip started from a note doesn't mark the segment watched.

### LAN access (2026-11-23)

`curio api` now defaults to `--host 0.0.0.0`, `curio.sh` to `CURIO_BIND=0.0.0.0`,
and the Vite dev server to `host: true`. `curio.sh start/status/run` print the
local address and the machine's LAN addresses (skipping Podman's 10.88 bridge);
health and port checks use 127.0.0.1. Verified: the API answered on
`http://192.168.1.44:8470` from this machine. ufw is active here, so other devices
need `sudo ufw allow from 192.168.1.0/24 to any port 8470 proto tcp` (not run: it
needs sudo). There is no login, which README documents.

### Ask the Video (2026-11-23)

Built `analysis/ask.py`, `processing/retrieval.py` (BM25, embedding search, fusion,
time references), `questions` and `embeddings` tables, three API routes, `curio ask`
and the "Ask the video" tab. Design in ARCHITECTURE.md §10. 10 new tests (115 in
total).

Verified with Ollama on the CS329Z lecture, full-transcript mode:

| Question | Time | Result |
|---|---|---|
| Explain the method discussed at 23 minutes | 38.0 s | OPRO, its loop, "take a deep breath"; cites 22:13–24:28 |
| Where does the speaker explain GRPO? | 8.2 s | 59:26 intro through the update rule to DeepSeek-R1 at 1:07:36 |
| Compare GEPA and MIPROv2 | 8.8 s | TPE search (33:16) vs Pareto pool and mutation/merge (41:33–43:33) |
| A practical example of when to fine-tune | 5.6 s | distillation, context length, small models (1:20:01–1:20:10) |
| How do I deploy an agent on Kubernetes? | 5.5 s | "doesn't cover this", closest section cited, general advice in the separate field |

- The first question pays for reading the transcript (about 21k tokens); later ones
  reuse Ollama's cached prefix.
- Nine cited times were checked against the transcript; each lands on the sentence
  that says it (for example 22:13 "a 2024 paper called Large Language Models as
  Optimizers, or they call their method OPRO", 1:20:07 "You have a capable teacher,
  but you want a cheaper model, then you could use distillation"). The one number
  not in the earlier checks, chain-of-thought's 71.8%, is in the transcript.
- Search mode, forced with `CURIO_ASK_CONTEXT_TOKENS=5000` (about a quarter of the
  lecture): the GRPO, "take a deep breath" and "at 1:12:00" questions got the same
  answers from 46–47 of 206 passages, in 11–13 s. Embeddings for the 206 blocks were
  computed once and stored (2 MB).
- Browser check (desktop and 390 px, over the LAN address): asking from the form,
  the pending state, timestamps that play the cited moment, the "Not from the video"
  box and the search notice.
- The test questions are saved in the lecture's conversation in `data/curio.db`;
  "Clear the conversation" removes them.

### Overnight queue (2026-11-23)

Requested: add videos at any time and have Curio analyze them at night when nobody
is using the GPU, as a queue. Built `overnight.py` (hours window, GPU probe, queue
worker), a `queued` status, three API routes, `curio queue`, `./curio.sh queue`,
"Queue for later" on the home page with the queue listed above Recently analyzed,
and a queued state on the video page ("Analyze now", "Remove from the queue"). Design
in ARCHITECTURE.md §11. 6 new tests (121 in total).

Checked against the real machine, with a scratch database on port 8471:
- Two videos queued at 20:47 with the default `1-7` window: the queue reported
  "waiting_window", next start 01:00, and the page said so (desktop and 390 px).
- With the window open all day: the worker waited with "The GPU is in use (89%
  busy)", correctly: a training job (`aksharallm` autoencoder) had started on the
  GPU. I didn't start an analysis on top of it; starting and moving on to the next
  video are covered by the tests.
- Idle readings on this desktop are 0–5% with brief spikes to 30% (remote desktop,
  browser), so the check uses the median of five readings and a 20% threshold.
- Inside the container: `--device nvidia.com/gpu=all` (CDI) makes `nvidia-smi` work
  for the non-root user, and mounting `/etc/localtime` gives local time.

### Default goal, quizzes and flashcards (2026-11-23)

Requested: quizzes and flashcards; and goals that carry over to new videos and can
be applied to queued ones. Built:
- A saved default goal (`settings` table, `GET/PUT /preferences/default`), filled in
  on the home page and used by the CLI, and `POST /queue/preferences` with a "Use the
  goal above for the waiting videos" button. Queued videos show their goal.
- `analysis/study.py`, three tables (`study_sets`, `quiz_attempts`, `card_states`),
  seven routes, `curio quiz`, the "Quiz and flashcards" tab, the `/review` page and
  the "flashcards to review" link on the home page. Design in ARCHITECTURE.md §12.
  10 new tests.

Checked on the CS329Z lecture (playlist, 26 min) with the local model:
- 8 questions and 12 cards in 45-75 s. Every number and claim in the first set was
  checked against the transcript (FireAct 34% vs 31% at 57:20; Qwen3 55/67/74% at
  1:14:39-1:14:46; GRPO's z-score at 1:03:56; reverse KL at 1:12:44; GEPA "more sample
  efficient" at 1:19:16), and the cited times land on those sentences.
- Five prompt and schema iterations to stop the right answer standing out by length
  and to keep the key correct (table in ARCHITECTURE.md §12). Still open: in the last
  run the right answer was the longest option in 5 of 8 questions, though mostly by
  only a few characters.
- In the browser (on a copy of the database): answering, checking (2 of 8 when
  choosing A everywhere), the highlighted right answer and struck-through wrong pick,
  timestamps that play the cited moment, a flashcard session with Space and 1/2/3,
  and the review page at 390 px.
- The six CS329Z lectures queued in the container were switched, as asked, to the
  technical profile with the goal "I know about agents and have built few but want
  to know things I may have missed or I should be aware of" (watch time unchanged),
  by updating their rows: the running image predates the retarget endpoint.

### Overnight notes and quizzes (2026-11-24)
The six queued lectures all finished overnight (3:45–4:12 AM) with the technical
goal. Now each queued job also writes the playlist notes and a quiz with flashcards
after its analysis (`CURIO_QUEUE_PREPARE`, on by default), checking the GPU again
before each. Also fixed: the GPU probe didn't list the quiz model (`CURIO_LLM_MODEL_STUDY`)
as Curio's own. 3 new tests (134 in total).

### Courses (2026-11-24)
Phase 4's first piece (design in ARCHITECTURE §13). Paste a playlist link and Curio
lists the lectures (no download), queues the ones not analyzed with your goal, and
builds one course playlist under one time budget, leaving out repeats, with a
checklist of what's easy to miss. Also: group analyzed videos into a course, watched
parts per course, `curio course`, and a player overlay for videos whose owner turned
embedding off.

Tried on the six CS329Z lectures analyzed overnight plus Lecture 9 (analyzed with the
general goal, so flagged), on a copy of the database:
- 1 m 47 s, then 1 m 28 s after the fixes below. 19 parts, 1 h 38 min of 9 h 9 min.
- Found the CS329Z playlist (12 entries: 10 lectures, 2 private) on the Stanford
  Online channel; it's newest first, and the lecture-number check puts it in order.
- Fixed after the first run: a 46 s optional clip (Generative Agents ablation,
  without the section it builds on) filled leftover time; one wrong repeat (shared
  point only); the plan said "Lecture 7" for Lecture 9 (labels now come from titles);
  a left-out section was listed once per match.
- Browser (1280 and 390 px): course map, cross-lecture playback, checklist links
  play the cited moment, no horizontal scroll, no console errors.
- Found while testing: Lecture 2 (`Vr1NzH8bT44`) doesn't allow embedding, so it
  can't play on its video page either. The player now shows a link to that moment
  on YouTube (YouTube errors 101/150).
- 6 new tests (140 in total).

### Ask the course, and course playlists rebuilt overnight (2026-11-24)
- The queue rebuilds a course's playlist after its last waiting lecture (with the
  last length used), checking the GPU first.
- Ask the course: questions answered from all lectures at once, citing moments in
  each (ARCHITECTURE §13). The Ask conversation component is now shared by the video
  and course pages.
- Tried on 7 CS329Z lectures: "how to evaluate agents, and the pitfalls" drew on 5
  lectures (project rigor in Lecture 3, pass^k and AST matching in 4, memory
  benchmarks in 7, CooperBench in 8, metrics in 9); "in lecture 6, observation
  masking vs compaction" stayed in Lecture 6. 40 s each; checked claims were in the
  transcripts. The GPU was shared with an analysis running in the container at the
  time.
- 4 new tests (144 in total).

### Menu, repeat verdicts, and course quiz and flashcards (2026-11-24)
- **Menu.** A top bar on every page with the logo (home) and a Menu button that
  opens a panel: Analyze a video, Courses, Videos, Queue, Review flashcards, with
  counts. Courses, all videos and the queue moved to their own pages; the first page
  keeps the form, cards due, the queue in one line and the three latest videos. The
  course page's lower half became tabs, and its playlist scrolls on its own on wide
  screens (the page went from 5,800 to 3,600 px). Checked at 1280 and 390 px:
  Escape closes the menu and returns focus, going anywhere closes it, no horizontal
  scroll, no console errors.
- **Repeat verdicts.** Course playlists keep every pair judged; a Repeats tab shows
  left-out sections, other repeats and overlaps with the model's reasons. A 3-hour
  CS329Z build: 28 pairs, 3 repeats, 11 overlaps, 14 different, 1 left out. The
  earlier "no repeats" builds were verdict variation, not a broken check. Reasons now
  name lectures by label (not A/B), and lectures are named by label in course
  prompts (the guest lecture was "Lecture 10").
- **Course quiz and flashcards.** One set per lecture over its course-playlist
  sections, checked as one quiz; a card per section and per "easy to miss" point (the
  point is the answer); their own decks in review. CS329Z, 7 lectures: 60 questions,
  72 cards, 7 minutes. A checked answer (chunk overlap "preserves context or
  sentence boundary information", Lecture 3) matches the transcript.
- 2 new tests (146 in total).

### Known gaps / next steps
- The Anthropic provider is covered by unit tests with a mocked client only.
  No API key was available, so it has not been run against the live API yet.
- Calibrate the scoring thresholds and the redundancy threshold on more videos
  (lectures, podcasts, tutorials with auto-captions).
- Overnight queue, possible next step: pause between videos when someone starts using the GPU mid-run (today a running
  analysis finishes).
- Phase 3, still to build: saved custom playlists.
- Courses: reorder lectures by hand. Repeats are only looked for among each
  section's closest match in another lecture (about 4 pairs a lecture), and half of
  those turn out different; checking more pairs would find more.
- Quizzes: the right answer is still too often the longest option. Options: a
  code-side retry for questions with a clear gap, or asking for the options in a
  fixed length band from the start.
