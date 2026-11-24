#!/usr/bin/env bash
# Build, start, stop and run Curio in Podman.
#
#   ./curio.sh build              build the image (web app + API)
#   ./curio.sh start              start in the background, then wait until it's healthy
#   ./curio.sh stop               stop and remove the container (data is kept)
#   ./curio.sh restart            stop, then start
#   ./curio.sh run                run in the foreground (Ctrl+C stops it)
#   ./curio.sh status             show whether it's running and healthy
#   ./curio.sh logs [-f]          show (or follow) the server log
#   ./curio.sh analyze <url> ...  analyze a video with the CLI inside the image
#   ./curio.sh queue [<url> ...]  queue a video for the overnight queue, or list the queue
#   ./curio.sh shell              open a shell in the running container
#
# Settings (environment variables):
#   CURIO_PORT          host port for the app                       (default 8470)
#   CURIO_BIND          host address to publish on                  (default 0.0.0.0: every
#                       device on your network can open it; 127.0.0.1 keeps it to this machine)
#   CURIO_IMAGE         image name                                  (default localhost/curio:latest)
#   CURIO_CONTAINER     container name                              (default curio)
#   CURIO_VOLUME        named volume holding the SQLite database    (default curio-data)
#   CURIO_OLLAMA_URL    Ollama as seen from the container
#                       (default http://host.containers.internal:11434)
#   CURIO_GPU_CHECK     1 = let the container read GPU utilization (nvidia-smi through
#                       CDI), so the overnight queue waits while the GPU is in use;
#                       0 = don't (the queue then checks only Ollama). Default: 1 when
#                       an NVIDIA CDI spec exists (nvidia-ctk cdi generate).
# Values in ./.env (API keys, models, weights) are passed to the container.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${CURIO_PORT:-8470}"
BIND="${CURIO_BIND:-0.0.0.0}"
# Where this machine reaches the app (0.0.0.0 is a bind address, not one to connect to).
LOCAL="$([[ "$BIND" == "0.0.0.0" ]] && echo 127.0.0.1 || echo "$BIND")"
IMAGE="${CURIO_IMAGE:-localhost/curio:latest}"
NAME="${CURIO_CONTAINER:-curio}"
VOLUME="${CURIO_VOLUME:-curio-data}"
OLLAMA_URL="${CURIO_OLLAMA_URL:-http://host.containers.internal:11434}"

# Rootless Podman's pasta network resolves host.containers.internal to the host's LAN
# address, which pasta treats as the container's own, so Ollama on the host is
# unreachable. Map a fixed address to the host instead and point the name at it.
HOST_ALIAS_IP="169.254.1.2"
NET_ARGS=(--network "pasta:--map-guest-addr,${HOST_ALIAS_IP}" --add-host "host.containers.internal:${HOST_ALIAS_IP}")

die() { echo "curio.sh: $*" >&2; exit 1; }

command -v podman >/dev/null || die "podman is not installed."

env_args() {
  local args=()
  [[ -f .env ]] && args+=(--env-file .env)
  # These must point inside the container, whatever .env says for local runs.
  args+=(-e "CURIO_OLLAMA_HOST=${OLLAMA_URL}" -e "CURIO_DATABASE_URL=sqlite:////app/data/curio.db")
  printf '%s\n' "${args[@]}"
}

image_exists() { podman image exists "$IMAGE"; }

ensure_port_free() {
  if (exec 3<>"/dev/tcp/${LOCAL}/${PORT}") 2>/dev/null; then
    die "port ${PORT} is already in use on ${BIND} by another program. Choose another: CURIO_PORT=8471 ./curio.sh $1"
  fi
}
# The addresses to open Curio at, one per line: this machine, then the LAN.
urls() {
  echo "http://${LOCAL}:${PORT}"
  if [[ "$BIND" == "0.0.0.0" ]]; then
    local ip
    for ip in $(hostname -I 2>/dev/null); do
      # IPv4 LAN addresses only; skip container and VPN bridges.
      [[ "$ip" =~ ^(10|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\. ]] && [[ ! "$ip" =~ ^10\.88\. ]] && echo "http://${ip}:${PORT}"
    done
  fi
}

print_urls() {
  local first=1 u
  while read -r u; do
    if ((first)); then echo "Curio is running at $u"; first=0; else echo "  on your network: $u"; fi
  done < <(urls)
}

running() { [[ "$(podman container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" == "true" ]]; }

build() {
  echo "Building $IMAGE…"
  # Docker format keeps the HEALTHCHECK (the OCI format drops it).
  podman build --format docker -t "$IMAGE" -f Containerfile .
}

ensure_image() { image_exists || build; }

check_ollama() {
  # Only a warning: with ANTHROPIC_API_KEY set, Ollama isn't needed.
  if ! curl -sf --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    echo "Warning: Ollama isn't answering on this machine (port 11434). Analyses will fail" >&2
    echo "unless ANTHROPIC_API_KEY is set in .env. Start it with: ollama serve" >&2
  fi
}

check_container_ollama() {
  if ! podman exec "$NAME" python -c "import urllib.request; urllib.request.urlopen('${OLLAMA_URL}/api/version', timeout=3)" >/dev/null 2>&1; then
    echo "Warning: the container can't reach Ollama at ${OLLAMA_URL}. Analyses with Ollama will" >&2
    echo "fail. Make sure Ollama listens on all interfaces (OLLAMA_HOST=0.0.0.0), or set CURIO_OLLAMA_URL." >&2
  fi
}

# The container reads utilization only; Ollama on the host does the GPU work.
gpu_args() {
  local want="${CURIO_GPU_CHECK:-}"
  if [[ -z "$want" ]]; then
    want=0
    [[ -f /etc/cdi/nvidia.yaml || -f /var/run/cdi/nvidia.yaml ]] && want=1
  fi
  [[ "$want" == 1 ]] && printf '%s\n' --device nvidia.com/gpu=all
  return 0
}

container_args() {
  local -a envs gpu
  mapfile -t envs < <(env_args)
  mapfile -t gpu < <(gpu_args)
  # The host's clock zone, so the queue hours are local time.
  local -a tz=()
  [[ -e /etc/localtime ]] && tz=(-v /etc/localtime:/etc/localtime:ro)
  printf '%s\n' --name "$NAME" -p "${BIND}:${PORT}:8000" -v "${VOLUME}:/app/data" "${NET_ARGS[@]}" "${envs[@]}" \
    "${gpu[@]}" "${tz[@]}"
}

start() {
  ensure_image
  if running; then
    echo "Curio is already running."
    print_urls
    return
  fi
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  ensure_port_free start
  check_ollama
  local -a args
  mapfile -t args < <(container_args)
  podman run -d --replace "${args[@]}" "$IMAGE" >/dev/null
  echo -n "Starting Curio"
  for _ in $(seq 60); do
    if curl -sf --max-time 2 "http://${LOCAL}:${PORT}/api/v1/health" >/dev/null 2>&1; then
      echo
      print_urls
      check_container_ollama
      return
    fi
    running || { echo; podman logs --tail 30 "$NAME" >&2; die "the container exited during startup."; }
    echo -n "."
    sleep 1
  done
  echo
  die "Curio didn't become healthy within 60 s. See: ./curio.sh logs"
}

stop() {
  if podman container exists "$NAME"; then
    # A running analysis is marked as interrupted the next time Curio starts.
    podman stop -t 10 "$NAME" >/dev/null && podman rm "$NAME" >/dev/null
    echo "Curio stopped. Your analyses are kept in the '$VOLUME' volume."
  else
    echo "Curio isn't running."
  fi
}

run_foreground() {
  ensure_image
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  ensure_port_free run
  check_ollama
  local -a args
  mapfile -t args < <(container_args)
  urls | sed '1s/^/Curio will be at /; 2,$s/^/  on your network: /'
  echo "(Ctrl+C to stop)"
  exec podman run --rm -it "${args[@]}" "$IMAGE"
}

status() {
  if running; then
    local health
    health="$(curl -sf --max-time 2 "http://${LOCAL}:${PORT}/api/v1/health" 2>/dev/null || true)"
    print_urls
    [[ -n "$health" ]] && echo "Health: $health" || echo "Health check isn't answering yet."
  else
    echo "Curio isn't running."
    return 1
  fi
}

analyze() {
  [[ $# -ge 1 ]] || die "usage: ./curio.sh analyze <youtube url> [--profile technical] [--minutes 20] …"
  ensure_image
  local -a envs
  mapfile -t envs < <(env_args)
  podman run --rm -v "${VOLUME}:/app/data" "${NET_ARGS[@]}" "${envs[@]}" "$IMAGE" curio analyze "$@"
}

case "${1:-}" in
  build) build ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  run) run_foreground ;;
  status) status ;;
  logs) shift; podman logs "$@" "$NAME" ;;
  analyze) shift; analyze "$@" ;;
  queue) shift; running || die "Curio isn't running. Start it first: ./curio.sh start"; podman exec "$NAME" curio queue "$@" ;;
  shell) running || die "Curio isn't running."; podman exec -it "$NAME" bash ;;
  *)
    sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    [[ -z "${1:-}" ]] || exit 1
    ;;
esac
