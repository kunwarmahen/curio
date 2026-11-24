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
| Phase 3: Interactive learning | ✅ Ask the Video, quizzes and flashcards with spaced repetition, a remembered default goal, segment completion tracking, and saved custom playlists |
| Overnight queue | ✅ queue videos any time; analyzed 1–7 AM while the GPU is idle; a paused analysis resumes after its last finished step |
| Phase 4: Multi-video intelligence | 🟡 learning paths across courses, courses from YouTube playlists, one course playlist across lectures without repeats (every verdict shown) that you can edit and reorder, an "easy to miss" checklist, Ask the course, and a course quiz and flashcards; curricula across courses are not built |

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
- 98 offline tests at the end of Phase 1 (105 with notes, 115 with Ask the Video, 121 with the overnight queue, 131 with quizzes, flashcards and the default goal, 134 with overnight notes and quizzes, 140 with courses, 144 with Ask the course, 146 with the course quiz, 156 with the follow-ups) (ad detection and carving, sentence units and boundary refinement, URL parsing, normalization, segmentation repair, scoring and
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

### Quiz giveaways, more pairs, pausing, custom playlists, Claude check, learning paths (2026-11-24)
- **Quiz giveaways.** A pass rewrites only the wrong options of questions whose right
  answer is noticeably the longest (5% and 6 characters), up to two rounds, rejecting
  rewrites that overshoot (1.5x). Five lecture quizzes: the right answer is the
  longest in 0-2 of 10-12 questions (chance is a quarter), from 5-9 before; about
  15-20 s more per quiz. Quiz version 3.
- **Made-up names.** Reading rewritten questions found a right answer saying ToolLLM
  used "GPT-4"; the lecture only says ChatGPT. Right answers that name something the
  transcript never says are now dropped, with a warning.
- **More pairs.** Two matches per section, up to 6 pairs a lecture (course version
  2): 42 pairs instead of 28 for 7 lectures, 117 s; this run judged no repeats (the
  earlier ones came back as overlaps with specific reasons).
- **Pausing.** A queued analysis checks the GPU about once a minute and, if
  something else uses it, goes back into the queue ("Paused: …", shown in the queue)
  and starts over later. Notes, quiz and course rebuild already checked between steps.
- **Custom playlists.** Edit a video's playlist (add parts Curio left out, drop its
  picks), saved on the server, with a switch back to Curio's picks.
- **Claude.** Checked the provider against the current API reference and the installed
  SDK (2.12.0): adaptive thinking, explicit effort (Opus 5.5 defaults to medium),
  `output_config.format`, refusal fallback (`fallbacks="default"` + beta), all
  present. Added: `ANTHROPIC_AUTH_TOKEN` also selects Claude; a log line when the
  fallback model answered; `curio check-claude`. Still not run live: no credentials
  on this machine.
- **Learning paths.** Combine courses into one course (in order, no duplicate
  videos); everything a course has works across them.
- The container's 10-lecture course quiz: 55 questions and 67 cards (12 from "easy to
  miss") in 6.5 minutes. The cards arrive about 9 s after the quiz shows as done.
- 10 new tests (156 in total).

### Paused analyses resume, lecture order, your own course playlist (2026-11-24)
- **Resume after a pause.** Each LLM step of an analysis (outline, boundaries, ad
  removal, evaluations) saves a checkpoint; a paused run picks up after the last
  finished step (ARCHITECTURE §11). The checkpoint is checked against the models,
  pipeline version, transcript and switches; a new goal keeps the outline and redoes
  only the evaluations. Real run on a copy (Lecture 1, 70 min, general goal): paused
  at 172 s during the evaluations; 105 s of finished steps were kept and the resumed
  run took 80 s (184.6 s of work in all, 26 sections, no warnings). The 67 s of the
  interrupted evaluation batch was redone: a pause inside a step still redoes that step.
- **Lecture order.** Up and down arrows in the Lectures tab. The saved course playlist
  follows at once, with no rebuild: its lecture numbers (and those in saved Ask the
  course answers) are renumbered by video when read. Only a change in which lectures
  are used makes the plan stale.
- **Your own course playlist.** Edit the course playlist like a video's: tick sections
  from any lecture (276 for CS329Z, Curio's 55 marked), switch between "Yours" and
  "Curio's picks". Parts are merged and keyed as Curio's, so watched parts carry over
  (34 of 35 kept their key after dropping one section and adding one). The course
  quiz covers your playlist when you have one.
- Browser (1280 and 390 px, copy of the database): editor, save, switching, moving a
  lecture with the keyboard (focus stays on the moved lecture's arrow), the playlist
  regrouped in the new order; no horizontal scroll, no console errors. Found: a skip
  section added by hand was drawn hatched like a left-out repeat; now drawn as optional.
- 8 new tests (164 in total).

### Evaluation batches saved on pause (2026-11-24)
- Each finished evaluation batch goes into the checkpoint; a resumed run asks only the
  batches not done (with the same goal; a new goal redoes them all). Real run on a copy
  (Lecture 1, technical goal): paused at 165 s, after batch 1 of 2 (19 sections);
  the resumed run evaluated only batch 2, in 24 s (189 s of work in all, 25 sections,
  no warnings). The same pause before this change redid batch 1 (67 s).
- 1 new test (165 in total).

### Outline windows saved on pause (2026-11-24)
- A long transcript's outline saves its state after each window. A resumed run reads
  only the windows left, or goes straight to the overview. The last evaluation batch is
  now saved too: before, a pause at "Re-evaluating skipped sections" lost it. A pause
  now loses no finished model call (pauses are checked only between calls).
- Real run on a copy (Lecture 1, 70 min, technical goal, outline window lowered to
  6,000 tokens so it took 3 windows): paused at 52 s after window 2 (14 sections so
  far, next window from block 114). The resumed run read only window 3, then the
  overview, boundaries and evaluations: 141 s, 192.9 s of work in all, 24 sections
  tiling the video from 0:06, no warnings, checkpoint removed at the end.
- 1 new test (166 in total).

### README as a user guide (2026-11-24)
The README now explains how to use Curio (getting started, analyzing a video, the
video page, review, the queue, courses, LAN use, CLI, settings, things to know)
instead of tracking what was built; build history and measurements stay here and the
design in ARCHITECTURE. Podman internals moved from the README to ARCHITECTURE §8.

### Threshold calibration on 19 videos (2026-11-24)
Re-scored every saved analysis offline with the current code (the tool reproduced all
saved tiers exactly), plus 5 new analyses chosen for what was missing, on a copy of
the database:

| Video | Kind | Goal | Sections | Playlist |
|---|---|---|---|---|
| `aircAruvnKk` 3Blue1Brown, neural networks (19 min) | short, dense explainer | beginner | 10 | 10 of 19 min |
| `nm1TxQj9IsQ` Huberman Lab, sleep (82 min) | solo podcast, sponsor reads | executive + "just the practical protocols" | 53 | 17 of 82 min |
| `vg01Q1BI4WM` Yale, This Is Your Brain (53 min) | non-technical lecture | general | 19 | 17 of 53 min |
| `zjkBMFhNj_g` Karpathy, Intro to LLMs (60 min) | talk, auto-captions | deep dive | 20 | 29 of 60 min |
| `ZA-tUyM_y7s` MIT 6.006 lecture 1 (46 min) | CS lecture | beginner | 12 | 14 of 46 min |

With the 11 CS329Z lectures and the earlier podcast, Kubernetes and PostgreSQL runs:
19 analyses, 6 kinds of video, all 5 goals. 86–303 s each.

**Findings, judged by reading the sections against each goal:**
1. **"Essential" didn't match the playlist.** 58 of 145 essential sections (40%) were
   not in their playlist: essentials added up to 1.1–2.6× the budget in every video.
   Raising the threshold doesn't fix it (at 82, 21% still out, up to 2× the budget),
   because the budget changes per video and with the watch time chosen. Fixed with
   `fit_labels`: an essential section that doesn't fit is labeled recommended, with
   "didn't fit in your N-minute playlist" in the scoring table. Now 90 essential
   sections, all in their playlists; essential minutes ≈ the budget.
2. **The thresholds themselves hold.** Skip below 40, and the content floor at 50,
   matched my reading: skipped content is Q&A digressions, early history, and basics
   the learner knows. Borderline cases (AI rating 48–54; the model's "middling" 49.5
   is common) are judgment calls either way. Raising the essential threshold would
   not have improved agreement: the sections I'd call essential don't score higher
   than the ones I wouldn't (Lecture 1 for an experienced learner: the debugging
   framework 98, reliability 71; textbook ReAct vs. CoT 99). Thresholds stay
   70 / 55 / 40, floor 50.
3. **Boundary refinement failed on the Huberman podcast**: 53 sections needed a 36k-token
   prompt in one call (Ollama context 32k), so every section kept block-level starts.
   Boundaries now go in batches that fit.
4. **A false validation warning** ("X04 boundaries don't match caption times", 3Blue1Brown)
   next to a cut-out sponsor read: content ends where the ad's first cue starts. Both
   cue edges now count as caption times.

**Not threshold problems, left as they are:**
- Ranking: for an expert's own goal the model sometimes rates textbook material above
  "what to watch out for"; Karpathy's "LLM as an operating system" was rated 35 (skip).
- Within-video rank (half of each score) pulls solid sections down a tier in videos
  where everything is strong (Kubernetes for beginners: "What is Kubernetes" 82 → 65,
  recommended).
- Greedy selection by score lets one long section crowd out several: MIT lecture 1's
  10-minute induction proof (100) left out efficiency and complexity classes (87, 93)
  from a 14-minute beginner playlist.
- Saved analyses were relabeled with the new `curio relabel` (labels only, no model
  calls; playlists, part ids, custom playlists and watched parts unchanged). The live
  database (backed up first to `data/curio-live-backup-2026-11-24.db`): 48 sections in
  11 analyses, now 77 essential sections, all in their playlists; the CS329Z course
  playlist unchanged. The pipeline version isn't bumped, so nothing is re-analyzed.
- 4 new tests (170 in total).

### Ranking misses fixed (2026-11-24)
The three misses from the calibration, checked offline on all 19 analyses (scoring and
selection) and with re-run evaluations (prompts):
1. **All-strong videos dragged down.** Rank calibration now ranks only teaching sections
   (core, example, demo, Q&A) against each other, on the range of ratings the model gave
   them instead of 0–100. Kubernetes for beginners: "What is Kubernetes" 65 → 86,
   architecture 61 → 84, Ingress and Volumes up to recommended. Across 19 analyses,
   skips fell 110 → 91, mostly low-rated Q&A sections now optional.
2. **One long section crowding out several.** Video playlists are now a knapsack over
   (score/100)³ × √minutes, plus prerequisites that fit. MIT lecture 1 now covers
   definitions, an example algorithm and efficiency instead of a 10-minute proof;
   Lecture 1 (experienced learner) now includes reliability, failure cases and privacy;
   the tools lecture swaps 9 minutes of Toolformer for lost-in-the-middle, AST
   evaluation, JSON function calling and pass^k. Playlists hold 142 sections instead of
   133 in the same time. A prerequisite of a better section wins ties.
3. **The model's ratings for the goal.** Two prompt changes kept, each tested over two
   evaluation runs against two with the old prompt (runs vary 10–20 points per section):
   - Deep dive: a speaker's own framework counts as novel insight. Karpathy's "LLM as
     an operating system" 35 / 29 → 71 / 78; jailbreaks and data poisoning up 8–10.
   - Own goal: what the learner says they know is low novelty and relevance, what they
     ask for is high relevance. Lecture 1: LLM-era basics 73 / 54 → 47 / 31, agent
     anatomy 84 / 76 → 68 / 66, reliability up; but the debugging-framework section
     also fell (88 / 68 → 59 / 55).
   - Dropped: a beginner sentence ("what it is and how its parts fit come first") moved
     nothing for Kubernetes (82 / 79 → 77 / 76) and lifted MIT's course logistics.
- 4 new tests (173 in total). Saved analyses keep their scores; new analyses get all of
  this (the pipeline version isn't bumped).

### Beginner foundations (2026-11-24)
For the beginner goal, a content section gets up to +12 for the later teaching sections
that build on it (`builds_on`, followed transitively), in proportion to the share of the
video that depends on it, shown as "N later section(s) build on this". Offline on the
three beginner analyses (other goals unchanged):
- Kubernetes: "What is Kubernetes" 86 → 97 and architecture 84 → 95, both now in the
  22-minute playlist (13 of 14 teaching sections build on the first). The trade: within
  the same time, Deployments and the YAML configuration section (both already at the
  100 cap) left the playlist.
- MIT 6.006 and 3Blue1Brown: same picks; foundations score 2–10 higher.
- 2 new tests (175 in total).

### Course playlists use the knapsack (2026-11-24)
Course playlists now use the same selection as video playlists, with repeats: if the
best set holds both halves of a repeat, the half worth less is set aside and the set is
chosen again. The greedy selector is gone. Offline on the CS329Z course (10 lectures,
189 sections, 2 repeat pairs, from the saved plan's verdicts; no model calls):

| Length | Greedy | Knapsack |
|---|---|---|
| 90 min | 29 sections, 97 min | 38 sections, 96 min |
| 180 min | 55 sections, 195 min | 64 sections, 194 min |

No repeat has both halves in either; 0.01–0.02 s. At 90 minutes it adds privacy and
collusion, observation masking, MCP tool discovery, memory maintenance and
MemoryArena, handoffs vs. agent-as-tool, and choosing context vs. weight optimization;
it drops longer sections, including the course's top-scored one (DSPy optimizers with
GEPA, 100, 9.3 min), bi-encoders vs. cross-encoders (95, 6 min) and agent anatomy.
Parts get shorter (about 2.5 instead of 3.3 minutes). The CS329Z playlist changes the
next time it's built. 2 new tests (177 in total).

### Known gaps / next steps
- The Anthropic provider has not been run against the live API (no credentials
  here); `curio check-claude` checks it with one tiny request.
- Scores are capped at 100, so with the beginner foundation bonus several top sections
  tie at 100 and the bonus can't separate them (Kubernetes).
- With the learner's own goal, sections the goal asks for can be marked down along with
  what they know (Lecture 1's debugging framework).
- The knapsack values a section by √minutes, so a long top section loses to several
  shorter very good ones (CS329Z's 9.3-minute DSPy optimizers section, scored 100).
  That's the intent, but whether √ is the right curve hasn't been tested with a learner.
- The lexical redundancy threshold (0.45) wasn't examined in this calibration.
- Repeat vs overlap verdicts vary run to run.
- The made-up-names check covers names, not plain-word claims or numbers.
