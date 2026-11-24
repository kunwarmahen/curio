<img src="web/public/favicon.svg" width="56" height="56" alt="">

# Curio

**Watch the parts of long videos that teach you the most.**

Paste a YouTube lecture, podcast or tutorial, and Curio reads its full transcript,
works out what each part teaches, and gives you a playlist of the parts worth your
time for *your* goal: what each one teaches, why it's worth watching, and what you
can safely skip. Click a part and the original video plays from that moment.

Curio also writes study notes, answers questions about the video with timestamps,
quizzes you, and keeps flashcards for review. Give it a whole YouTube playlist and it
does the same across every lecture of a course, leaving out what one lecture repeats
from another.

It runs on your own computer, with a local model through Ollama (your GPU) or with
Claude (an Anthropic API key). It never downloads video or audio.

- [Getting started](#getting-started)
- [Analyze a video](#analyze-a-video)
- [The video page](#the-video-page)
- [Flashcard review](#flashcard-review)
- [The overnight queue](#the-overnight-queue)
- [Courses](#courses)
- [Using it from your phone or another computer](#using-it-from-your-phone-or-another-computer)
- [Command line](#command-line)
- [Settings](#settings)
- [Good to know](#good-to-know)

## Getting started

### What you need

- **A model**, one of:
  - [Ollama](https://ollama.com) with a long-context model (default `qwen3.8-64k`;
    a 90-minute transcript is about 20k tokens), and ideally `nomic-embed-text`
    for searching long videos and courses.
    About 2.5–4 minutes per hour-long video on a 24 GB GPU.
  - An Anthropic API key (`claude-opus-5-5` by default). Faster; costs API credit.
- **Either** Podman 4.4+ (easiest: everything runs in one container) **or**
  Python 3.12+ with [uv](https://docs.astral.sh/uv/) and Node.js 20+.
- Internet access to YouTube. Videos need captions (manual or auto-generated).

### Start it with Podman (recommended)

```bash
cp .env.example .env    # optional: add ANTHROPIC_API_KEY, change models or settings
./curio.sh start        # builds the image the first time, then prints the addresses
```

Open **http://127.0.0.1:8470**. Ollama keeps running on your machine; the container
talks to it. It must listen on all interfaces (`OLLAMA_HOST=0.0.0.0` for the Ollama
service); `start` warns if the container can't reach it.

Day to day:

```bash
./curio.sh status       # is it running and healthy?
./curio.sh logs -f      # follow the log
./curio.sh stop         # stop it; your analyses are kept
./curio.sh build && ./curio.sh restart   # after updating Curio (wait until the queue is idle)
```

Your data lives in the Podman volume `curio-data`, so it survives restarts and
updates.

### Or run it directly

```bash
uv sync                                    # install
(cd web && npm install && npm run build)   # build the web app (again after updating)
uv run curio api                           # → http://127.0.0.1:8470
```

This keeps its data in `data/curio.db`, separate from the container's.

## Analyze a video

On the first page, paste a YouTube link and choose **what you're watching for**:

| Goal | Picks |
|---|---|
| General | The most important ideas, balanced between foundations and examples |
| Beginner | Clear foundations and definitions; intros and recaps can help |
| Technical | Implementation, architecture, code and demos |
| Executive | Strategy, trade-offs, decisions and conclusions, in less time |
| Deep dive | Advanced ideas and expert detail; a longer playlist |

Under **Describe your own goal or set a watch time** you can say what you want in
your own words ("I want to understand how GEPA works; skip the course logistics")
and set how many minutes you have. Otherwise Curio picks about a third of the video.
Your choice is remembered for next time, on every device.

Then either:

- **Find the best parts**: analyze now (a few minutes with a local model), or
- **Queue for later**: analyze it tonight, when your GPU is free (see
  [The overnight queue](#the-overnight-queue)).

A video you've already analyzed with the same goal opens straight away.

## The video page

**The watch map** shows the whole video as one line: strong yellow is essential,
fainter is recommended or optional, hatched is safe to skip. Essential parts are
always in your playlist; a part that scored as essential but didn't fit your watch
time is shown as recommended, and "How it was scored" says so. Your playlist is
underlined and a red line shows where playback is. Click any stretch to play it.

**Your playlist** sits beside the player. Each part says what you'll learn and why
it's worth watching; parts play one after another and stop at their end. Tick a
part as watched, or let it be ticked when it finishes.

**Edit the playlist** lists every part of the video. Tick the ones you want,
including parts Curio left out, and save. A switch flips between **Yours** and
**Curio's picks**.

Below the player:

- **Notes**: study notes on your playlist, the whole video or parts you choose:
  summary, key takeaways, notes by section, definitions and what to do next. Every
  point links to the moment it's said. Download them as Markdown. About a minute per
  20 minutes of video with a local model.
- **Quiz and flashcards**: a multiple-choice quiz and a deck of flashcards. After you
  check your answers, each question shows the right answer, why, and the moment that
  explains it.
- **Ask the video**: ask anything ("Explain the method at 23 minutes", "Where is X
  explained?", "Compare A and B"). Answers link to the moments they come from.
  Anything from general knowledge rather than the video is shown separately, marked
  "Not from the video". Follow-up questions know what you asked before. The first
  question takes about 40 s with a local model, follow-ups 5–10 s.
- **More to watch**: good parts that didn't fit your time.
- **Safe to skip**: what was left out, and why (intros, housekeeping, sponsor reads,
  repeats).
- **How it was scored**: every part's ratings and each adjustment, with its reason.

## Flashcard review

The first page shows how many cards are due; **Review now** (or **Review flashcards**
in the menu) goes through them from all your videos and courses. Grade each card
**Forgot** (back in 10 minutes), **Remembered** or **Easy** (keys 1, 2, 3). Cards you
know come back after longer and longer gaps: 1, 3, 7, 16, 35, then 90 days.

## The overnight queue

**Queue for later** adds a video to a queue that Curio works through at night, so
analysis never competes with your own GPU work:

- It runs between **1 AM and 7 AM** local time, one video at a time, oldest first.
- It starts a video only while the GPU is idle: under 20% busy and no other
  program's model loaded in Ollama.
- If you start using the GPU while a video is being analyzed, Curio pauses it within
  about a minute and puts it back in the queue. When it runs again, it carries on where
  it stopped; finished work isn't lost.
- After each video it also writes the notes and a quiz with flashcards for your
  playlist, so they're ready in the morning.

The **Queue** page (in the menu) shows what's waiting and why it's waiting. You can
start a video now, remove it, or give every waiting video your current goal. The
container (or `curio api`) has to be running at night for the queue to work.

## Courses

Paste a **playlist** link (`youtube.com/playlist?list=…`) on the first page, or use
**Add the whole playlist as a course** when a video link belongs to one. Curio lists
the lectures, puts them in lecture order, and queues the ones not analyzed yet with
your goal, so a course is usually ready the next morning. You can also make a course
from videos you've already analyzed (**Make a course from videos you've analyzed**
on the Courses page).

On the course page, **Build the course playlist** picks the best parts across all
the analyzed lectures for one length (about 15 minutes per lecture by default). It
leaves out sections that another lecture teaches again, and plays everything in
lecture order in one player. When the last queued lecture of a course finishes
overnight, the playlist is rebuilt by itself.

- **The course map** shows every lecture as one line, with the playlist's parts
  highlighted and left-out repeats hatched.
- **Edit the playlist** lists every section of every lecture, with Curio's picks
  marked. Tick what you want and save; switch between **Yours** and **Curio's picks**,
  or go back to Curio's. Parts you keep stay marked as watched.
- **Easy to miss**: 8–12 points from the course that matter for your goal, each
  linked to the lecture moment that teaches it.
- **Quiz and flashcards**: a quiz on every part of the course playlist (yours, if
  you made one) and flashcards, including one for each "easy to miss" point. About a
  minute per lecture with a local model.
- **Ask the course**: questions answered from every lecture at once, saying which
  lecture says what. Name a lecture ("in lecture 6…", "the guest lecture") to look
  there first.
- **Repeats**: every pair of similar sections Curio compared across lectures, and
  whether it judged them a repeat (watch one), an overlap (each adds something) or
  different, with why.
- **Lectures**: each lecture's state (analyzed, queued, failed…), arrows to put them
  in your own order, the playlist length, and **Build again**. Reordering changes the
  playlist straight away; building again also redoes which of two repeated sections
  is kept.

**Learning paths.** With two or more courses, **Combine courses into a learning path**
on the Courses page joins them, in the order you pick, into one course, and
everything above works across them.

## Using it from your phone or another computer

Curio listens on your whole local network and prints the address when it starts
(for example `http://192.168.1.44:8470`). Open that on your phone; everything,
including your goal and playlists, is shared between devices.

- If a firewall is on, allow the port from your network only, for example
  `sudo ufw allow from 192.168.1.0/24 to any port 8470 proto tcp`.
- **There is no login.** Anyone who can reach the port can use Curio (your GPU or
  your API credit) and see everything in it. Use it only on networks you trust and
  never forward the port to the internet. To keep it on this computer only, set
  `CURIO_BIND=127.0.0.1` (Podman) or run `uv run curio api --host 127.0.0.1`.

## Command line

Everything is also available from the terminal (with Podman, `./curio.sh analyze` and
`./curio.sh queue` run these inside the container):

```bash
uv run curio analyze "<youtube url>" --profile technical --objective "how GEPA works" --minutes 20
uv run curio notes <analysis id> --scope playlist --out notes.md
uv run curio ask <analysis id> "Where does the speaker explain GRPO?"
uv run curio quiz <analysis id> --scope video
uv run curio queue "<youtube url>"           # add to tonight's queue
uv run curio queue                           # show the queue (--remove <id> to take one out)
uv run curio course "<playlist url>"         # add a course
uv run curio course <course id> --build --minutes 120
uv run curio check-claude                    # check an Anthropic key with one tiny request
uv run curio relabel                         # after an update that changes labels: relabel saved analyses (no model calls)
```

`analyze` uses your saved goal unless you pass `--profile` or `--objective`, prints the
playlist, and `--json out.json` saves the full analysis. `--force` analyzes again.
With Podman, run any other command inside the container, for example
`podman exec curio curio relabel`.

## Settings

Settings go in `.env` (see `.env.example`); the container picks them up on restart.

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | use Claude instead of the local model |
| `CURIO_LLM_PROVIDER` | `auto` | `auto` picks Claude when a key is set, otherwise Ollama |
| `CURIO_OLLAMA_DEFAULT_MODEL` | `qwen3.8-64k` | local model |
| `CURIO_ANTHROPIC_DEFAULT_MODEL` | `claude-opus-5-5` | Claude model |
| `CURIO_LLM_MODEL_OUTLINE` / `_EVALUATE` / `_NOTES` / `_ASK` / `_STUDY` / `_COURSE` | — | a different model for one task |
| `CURIO_EMBED_MODEL` | `nomic-embed-text` | embedding model for searching long transcripts and courses; empty = keyword search only |
| `CURIO_QUEUE_HOURS` | `1-7` | when the queue runs (`22:30-6` crosses midnight; empty = whenever the GPU is idle) |
| `CURIO_QUEUE_GPU_BUSY_PERCENT` | `20` | GPU use at which the queue waits |
| `CURIO_QUEUE_PREPARE` | `true` | also write notes and a quiz after each queued video |
| `CURIO_BUDGET_FRACTION` | `0.30` | default playlist length as a share of the video (10–35 min) |
| `CURIO_TRANSCRIPT_LANGUAGES` | `en,en-US,en-GB` | caption languages to look for |
| `CURIO_OLLAMA_NUM_CTX` | `32768` | context size for the local model |
| `CURIO_WEIGHT_*` | 30/20/20/15/15 | how much educational value, novelty, practical value, depth and relevance count |

For the container: `CURIO_PORT` (`8470`), `CURIO_BIND` (`0.0.0.0`), `CURIO_VOLUME`
(`curio-data`) and `CURIO_OLLAMA_URL` (Ollama as the container sees it).

## Good to know

- **Captions are required.** Videos without captions can't be analyzed. Curio reads
  captions through unofficial YouTube endpoints, and YouTube may block a computer
  that makes very many requests.
- **Some videos won't play inside Curio** because their owner turned off embedding.
  They're still analyzed; the player shows a link to that moment on YouTube instead.
- **Picks vary a little** between two analyses of the same video, and judgments like
  "this repeats that lecture" are the model's opinion. Every pick shows its reasons,
  and you can always edit the playlist.
- **Section starts are exact to a caption line**, so on auto-captions a part can
  start a few words early.
- **The GPU check needs an NVIDIA GPU.** Elsewhere the queue goes by the hours and
  Ollama's loaded models only. In Podman it needs the NVIDIA CDI spec
  (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`).
- **A lecture analyzed for a different goal** can be used in a course meanwhile; the
  course page says so and queues it again for the course's goal.

## For developers

How Curio works inside, the API and the data model are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); what's been built, measured and what's
next is in [docs/PROGRESS.md](docs/PROGRESS.md).

```bash
uv run pytest                  # offline: the model, YouTube and metadata are faked
uv run ruff check src tests
uv run curio api               # then, in another terminal:
cd web && npm run dev          # http://localhost:5173, proxies /api to port 8470
```

The Python package is `curio_learn` because a top-level `curio` module collides with
the [curio](https://github.com/dabeaz/curio) async library; the app and CLI are still
called `curio`.
