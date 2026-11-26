from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from curio_learn.api.app import create_app
from curio_learn.gpu import ProcReader, app_from_cgroup, gpu_report, parse_cards, parse_pmon, tidy_command
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

CARDS = "0, NVIDIA GeForce RTX 3090, 47, 22864, 24576, 54, 194.01, 390.00, 59\n"
PMON = """# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command
# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name
    0       8756   C+G      -      -      -      -      -      -    260      0    gnome-remote-de
    0      26463     G      3      2      -      -      -      -    209      0    Code --g
    0    2471020     C     15     10      -      -      -      -  20986      0    llama-server
"""
EMPTY_PMON = """# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command
# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name
    0          -     -      -      -      -      -      -      -      -      -    -
"""


def test_parse_cards_and_pmon():
    [card] = parse_cards(CARDS + "garbage\n")
    assert card.name == "NVIDIA GeForce RTX 3090" and card.utilization == 47 and card.memory_total_mb == 24576
    assert parse_cards("0, Tesla, [N/A], 1, 2, [N/A], [N/A], [N/A], [N/A]")[0].power_w is None
    rows = parse_pmon(PMON)
    assert [r["pid"] for r in rows] == [8756, 26463, 2471020]
    assert rows[0]["kind"] == "both" and rows[0]["utilization"] is None and rows[0]["memory_mb"] == 260
    assert rows[1]["kind"] == "graphics" and rows[1]["program"] == "Code --g"
    assert rows[2]["kind"] == "compute" and rows[2]["utilization"] == 15
    assert parse_pmon(EMPTY_PMON) == []  # the container can't see the host's processes


def test_commands_hide_secrets_and_shorten():
    assert tidy_command(["/usr/bin/python", "-m", "train", "--api-key", "sk-123", "--lr", "3e-4"]) == (
        "python -m train --api-key … --lr 3e-4"
    )
    hidden = tidy_command(["app", "--token=abc", "HF_TOKEN=xyz", "--auth-file=/x"])
    assert hidden == "app --token=… HF_TOKEN=… --auth-file=…"
    one = tidy_command(["brave --type=gpu-process --gpu-preferences=" + "A" * 80 + " --shared-files"])
    assert one == "brave --type=gpu-process --gpu-preferences=… --shared-files"
    assert tidy_command(["x", "/very/long/" + "d" * 60 + "/model.gguf"]) == "x …/model.gguf"


def test_app_from_cgroup():
    assert app_from_cgroup("0::/../../../../../../system.slice/ollama.service") == ("ollama service", False)
    assert app_from_cgroup("0::/../app.slice/snap.code.code-21389081-b128.scope") == ("VS Code", False)
    assert app_from_cgroup("0::/user.slice/app.slice/app-gnome-firefox-4242.scope") == ("Firefox", False)
    assert app_from_cgroup("0::/user.slice/user@1000.service/session.slice/org.gnome.Shell@ubuntu.service") == (
        "GNOME desktop",
        False,
    )
    cid = "d4b2b625bcf9003b1ee9582b441b610559f8f68bdb30f413d8080dd34c5544b0"
    path = f"0::/user.slice/user@1000.service/user.slice/libpod-{cid}.scope/container"
    assert app_from_cgroup(path, "d4b2b625bcf9") == ("Curio (this app's container)", True)
    assert app_from_cgroup(path, "aaaaaaaaaaaa") == ("Container d4b2b625bcf9", False)
    assert app_from_cgroup("") == ("", False)


def _proc(tmp_path, pid, argv, cgroup, start_ticks=100):
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "cgroup").write_text(cgroup)
    fields = ["S"] + ["0"] * 18 + [str(start_ticks)]
    (d / "stat").write_text(f"{pid} (x) " + " ".join(fields))


def test_gpu_report(tmp_path):
    (tmp_path / "uptime").write_text("1000.0 5000.0\n")
    _proc(tmp_path, 2471020, ["/usr/local/bin/llama-server", "--model", "m"], "0::/system.slice/ollama.service")
    _proc(tmp_path, 8756, ["gnome-remote-desktop-daemon"], "0::/app.slice/gnome-remote-desktop.service")
    _proc(tmp_path, 26463, [".venv/bin/python", "-m", "aksharallm.train"], "0::/app.slice/snap.code.code-1.scope")
    outputs = {"--query": CARDS, "pmon": PMON.replace(" 26463 ", " 26463 ")}
    run = lambda args: next(v for k, v in outputs.items() if any(k in a for a in args))  # noqa: E731
    expires = datetime(2026, 11, 26, 7, 38).astimezone()
    models = [SimpleNamespace(model="qwen3.8-64k:latest", size=2**30, size_vram=2**29, expires_at=expires)]
    client = SimpleNamespace(ps=lambda: SimpleNamespace(models=models))
    r = gpu_report(
        own_models={"qwen3.8-64k"},
        ollama_client=client,
        nvidia_smi="nvidia-smi",
        run=run,
        proc=ProcReader(str(tmp_path)),
        own_container="",
    )
    assert r.cards[0].memory_used_mb == 22864 and r.processes_visible
    first, second, third = r.processes  # most GPU memory first
    assert (first.pid, first.program, first.app) == (2471020, "llama-server", "ollama service")
    assert first.command == "llama-server --model m" and first.running_s is not None
    assert third.program == "python" and third.app == "Started from VS Code"
    assert r.ollama[0].curio_model and r.ollama[0].vram_mb == 512 and r.ollama[0].expires_at == expires

    # Inside a container without the host's processes: the cards, but no programs.
    outputs["pmon"] = EMPTY_PMON
    r = gpu_report(own_models=set(), nvidia_smi="nvidia-smi", run=run, proc=ProcReader(str(tmp_path)))
    assert r.processes == [] and not r.processes_visible and not r.ollama_reachable
    # No nvidia-smi at all.
    assert not gpu_report(own_models=set(), nvidia_smi="").nvidia_smi


def test_api_gpu(settings):
    service = CurioService(settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())
    with TestClient(create_app(service)) as client:
        body = client.get("/api/v1/gpu").json()
        assert body["curio_busy"] is False and body["curio_doing"] == []
        assert "cards" in body and "processes_visible" in body and body["queue_message"]
