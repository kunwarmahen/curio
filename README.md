# Curio

**Watch the parts of long videos that teach you the most.**

Curio reads the full transcript of a long YouTube video (a lecture, podcast or
tutorial), maps its topics, and judges every section against the whole video and
your learning objective. It then builds a curated playlist: which segments to
watch, what each one teaches, why it's worth your time, and what you can skip.
Clicking a segment plays the original video from that moment.

Curio is not a summarizer. Its goal is to maximize knowledge acquired per minute watched.

## Status

Phase 1 (functional MVP) is implemented: URL → metadata and transcript →
topic segmentation → contextual evaluation → explainable scoring → time-budgeted
playlist → validation → web app (React) with a watch map and an embedded player. Phase 2 is
complete: learning profiles, custom objectives, redundancy detection, skip
recommendations and **learning notes** (key points, explanations, definitions,
takeaways and recommendations, each linked to the moment it's said). From Phase 3,
**Ask the Video** is in: questions answered from the transcript with clickable
timestamps, keeping anything that isn't from the video apart. See
[docs/PROGRESS.md](docs/PROGRESS.md) for details and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 20+ (to build the web app in `web/`), **or** Podman 4.4+ to run everything
  in a container (see "Run in Podman")
- **One** LLM backend:
  - [Ollama](https://ollama.com) with a long-context model (default
    `qwen3.8-64k`; a 90-minute transcript is about 20k tokens), **or**
  - an Anthropic API key (`claude-opus-5-5` by default).
- Network access to YouTube. Curio never downloads video or audio.

## Install

```bash
uv sync                 # creates .venv and installs dependencies
cp .env.example .env    # optional: adjust settings / add ANTHROPIC_API_KEY
```

## Run

```bash
(cd web && npm install && npm run build)   # once, and after frontend changes
uv run curio api        # app → http://127.0.0.1:8470 and http://<this machine's LAN IP>:8470
uv run curio analyze "https://www.youtube.com/watch?v=e_m99aR7_3Q" \
    --profile technical --objective "how GEPA optimizes prompts" --minutes 20
uv run curio notes <analysis id> --scope playlist --out notes.md   # learning notes
uv run curio ask <analysis id> "Where does the speaker explain GRPO?"
```

`curio api` listens on all interfaces, so phones and other computers on your
network can open it at `http://<LAN IP>:8470`. Use `--host 127.0.0.1` to keep it
on this machine. See "Access from your network" below.

For frontend development, run `uv run curio api` and, in another terminal,
`cd web && npm run dev` (http://localhost:5173, also on the LAN; proxies `/api` to port 8470).

The video page shows a **watch map**: the whole video as one line, each section
highlighted by how much it's worth (strong yellow: essential, fainter:
recommended and optional, hatched: skip), with your playlist underlined and a
red playhead. Click any stretch to play it. Playlist parts stop at their end
and continue to the next part automatically.

The **Notes** tab writes study notes on your playlist, the whole video, or parts
you pick: a summary, key takeaways, notes by section (key points and an
explanation of any method or mechanism), definitions, and what to do next. Every
item has a timestamp; select it to play the video from that moment. Notes are
saved, and can be downloaded as Markdown. With the local model, notes take about a
minute per 20 minutes of video (68 s for a 26-minute playlist, 1 min 51 s for a
72-minute video).

The **Ask the video** tab answers questions about the video ("Explain the method
at 23 minutes", "Compare GEPA and MIPROv2", "Where is X explained?"). Each part of an
answer has the timestamps it's based on; general knowledge the video doesn't contain
is shown in a separate box labeled "Not from the video", and questions the video
doesn't cover say so. The conversation is saved, and follow-ups know what was
asked before. With the local model, the first question about a video takes about
40 s (the model reads the whole transcript) and follow-ups 5–10 s.

`curio analyze` prints the playlist and, with `--json out.json`, writes the full
analysis. Results are cached in `data/curio.db`. Pass `--force` to re-analyze.

## Run in Podman

`curio.sh` builds one image (web app + API) and runs it with rootless Podman.
Ollama stays on the host, so the GPU is used directly and no model is copied into
the image.

```bash
./curio.sh build              # build the image (also done by start/run if missing)
./curio.sh start              # start in the background; prints the local and LAN addresses
./curio.sh status             # running? healthy? (exit code 1 when stopped)
./curio.sh logs -f            # follow the server log
./curio.sh stop               # stop and remove the container; analyses are kept
./curio.sh restart
./curio.sh run                # run in the foreground; Ctrl+C stops it
./curio.sh analyze "<youtube url>" --profile technical   # one-off CLI analysis
./curio.sh shell              # shell inside the running container
```

| Variable | Default | Meaning |
|---|---|---|
| `CURIO_PORT` | `8470` | host port (8000 is often taken; on this machine by another app) |
| `CURIO_BIND` | `0.0.0.0` | host address the port is published on: all interfaces, so the LAN can reach it; `127.0.0.1` keeps it to this machine |
| `CURIO_VOLUME` | `curio-data` | named volume holding the SQLite database |
| `CURIO_OLLAMA_URL` | `http://host.containers.internal:11434` | Ollama as seen from the container |
| `CURIO_IMAGE` / `CURIO_CONTAINER` | `localhost/curio:latest` / `curio` | image and container names |

Notes:
- Settings in `.env` (API key, models, weights) are passed to the container. The
  database location and Ollama address are always set for the container, whatever
  `.env` says for local runs.
- Ollama must listen on all interfaces (it shows `*:11434` in `ss -ltn`; set
  `OLLAMA_HOST=0.0.0.0` for the Ollama service if not). `start` checks that the
  container can reach it and warns if not.
- Rootless Podman's default network (`pasta`) resolves `host.containers.internal`
  to the host's LAN address, which pasta treats as the container's own, so
  connections are refused. The script maps `169.254.1.2` to the host
  (`--map-guest-addr`) and points `host.containers.internal` at it.
- The container runs as a non-root user. The image has a health check, built in
  Docker format because the OCI format drops it.
- The container database (volume) is separate from `data/curio.db` used by local
  `uv run` commands.

## Access from your network

Both `uv run curio api` and `./curio.sh start` listen on all interfaces and print
the LAN address (for example `http://192.168.1.44:8470`). Two things to know:

- **Firewall.** If ufw is active, allow the port, ideally only from your network:
  `sudo ufw allow from 192.168.1.0/24 to any port 8470 proto tcp`.
- **No login.** Anyone who can reach the port can analyze videos (which uses your
  GPU, or your Anthropic credit) and see every analysis and set of notes. Only
  expose it on networks you trust; never forward the port to the internet. Set
  `CURIO_BIND=127.0.0.1` (Podman) or `--host 127.0.0.1` to turn LAN access off.

## Configuration

All settings are environment variables (or `.env`); see `.env.example`. The
most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `CURIO_LLM_PROVIDER` | `auto` | `auto` uses Anthropic if `ANTHROPIC_API_KEY` is set, otherwise Ollama |
| `CURIO_LLM_MODEL` | provider default | model for all tasks; `CURIO_LLM_MODEL_OUTLINE` / `_EVALUATE` / `_NOTES` / `_ASK` override per task |
| `CURIO_ASK_CONTEXT_TOKENS` | `0` (auto) | transcript tokens Ask may show the model; longer transcripts are searched |
| `CURIO_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model for searching long transcripts; empty = keyword search only |
| `CURIO_OLLAMA_DEFAULT_MODEL` | `qwen3.8-64k` | local model |
| `CURIO_OLLAMA_NUM_CTX` | `32768` | context size for every Ollama call (fixed, so the model loads once) |
| `CURIO_ANTHROPIC_DEFAULT_MODEL` | `claude-opus-5-5` | cloud model |
| `CURIO_WEIGHT_*` | 30/20/20/15/15 | rubric weights (educational value, novelty, practical, depth, relevance) |
| `CURIO_BUDGET_FRACTION` | `0.30` | default playlist length as a fraction of the video (clamped to 10–35 min) |
| `CURIO_TRANSCRIPT_LANGUAGES` | `en,en-US,en-GB` | preferred caption languages |
| `CURIO_REFINE_BOUNDARIES` | `true` | place section starts at the exact sentence (one extra LLM call) |
| `CURIO_DETECT_ADS` | `true` | cut sponsor reads out of content sections (one extra LLM call, only when ad-like text exists) |
| `CURIO_DATABASE_URL` | `sqlite:///data/curio.db` | any SQLAlchemy URL |

## How it works

1. **Ingest:** yt-dlp reads metadata (no download). `youtube-transcript-api`
   reads captions, preferring manual tracks over auto-generated ones.
2. **Normalize:** cleans the caption cues (annotations, repeated auto-caption
   lines) and merges them into 20–45 s *blocks*. Timestamps come from the original cues.
3. **Outline (LLM):** the whole transcript in one prompt (or in overlapping
   windows for very long videos). The model returns sections as block numbers, so
   it can't invent a timestamp. Code repairs gaps and overlaps.
4. **Exact starts (LLM):** for each boundary the model picks the first sentence of
   the new section from the sentences around it, so a section starts where the
   speaker changes topic. Auto-captions without reliable punctuation use single
   caption cues (about 2.5 s) instead of sentences.
5. **Ads (LLM):** sentences that look like ad copy are checked; confirmed sponsor
   reads, including their segue, are cut out into their own always-skipped sections.
6. **Evaluate (LLM):** each section is scored 1–10 on educational value,
   novelty, practical value, depth and relevance, with the outline of the whole
   video in view, plus what you'll learn, why it's worth watching,
   dependencies and redundancy.
7. **Score:** weighted rubric, half of it from the section's rank within the video,
   plus rule-based adjustments (section kind, length, lexical and LLM-flagged
   redundancy, new concepts, standalone, and a floor that keeps solid content
   optional instead of "skip"). Every adjustment is shown in the UI with its reason.
8. **Playlist:** essentials first, then by score within the time budget, pulling
   in direct prerequisites when they fit. Adjacent picks are merged and the result is
   chronological. Everything else is listed as "more to watch" or "safe to skip, because…".
9. **Validate:** checks that timestamps are real caption times, that sections
   tile the transcript without dropping speech, transcript coverage and budget,
   repairing what it safely can.
10. **Notes (LLM, on request):** the chosen sections' transcripts are shown as
    numbered sentences; each point and definition names the sentence it comes from,
    and code turns that into the sentence's cue time. A second, short call writes
    the summary, takeaways and recommendations, each tied to section ids that are
    checked against the notes.
11. **Ask (LLM, on request):** the outline plus the whole transcript when it fits
    the context (about 95 minutes of speech with Ollama's 32k, always with Claude);
    otherwise the passages found by keyword (BM25) and embedding search, plus the
    stretch the question names by time. Answer parts cite passage numbers, which code
    turns into the start of the best-matching sentence in each passage.

## Tests

```bash
uv run pytest           # offline: LLM, YouTube and metadata are faked
uv run ruff check src tests
```

## Limitations

- Transcript access uses unofficial YouTube endpoints. YouTube may block IPs that
  make many requests, and videos without captions can't be analyzed.
- Local models are slower: about 2.5–4 minutes per 60–90-minute video on a 24 GB GPU
  (qwen3.8 27B), more if another program shares the GPU.
- Section starts are exact to a caption cue, not a word: captions carry no word
  timings, so when a topic changes mid-cue (common in auto-captions) the start is a
  few words early.
- Picks vary somewhat between runs of the same video (the model's outline and
  ratings aren't deterministic).
- The coverage estimate is an AI judgment, not an objective measurement.
- The Python package is named `curio_learn` because a top-level `curio` module
  collides with the [curio](https://github.com/dabeaz/curio) async library, which
  `sniffio` (used by httpx/Starlette) probes for. The product and CLI are still `curio`.
