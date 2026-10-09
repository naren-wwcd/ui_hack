#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [[ ! -x "$VENV_PY" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PIP" install -r requirements.txt

mkdir -p data logs screenshots
"$VENV_PY" -m playwright install chromium
"$VENV_PY" -m scripts.seed

if [[ -f frontend/package.json ]]; then
  npm --prefix frontend install
fi

pids=()

start_process() {
  local label="$1"
  shift
  "$@" &
  local pid=$!
  pids+=("$pid")
  printf 'Started %s (pid %s)\n' "$label" "$pid"
}

stop_all() {
  local pid
  trap - INT TERM EXIT
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap stop_all INT TERM EXIT

start_process sandbox "$VENV_PY" -m uvicorn sandbox_app.main:app --host 127.0.0.1 --port 8001
start_process backend "$VENV_PY" -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
start_process portal "$VENV_PY" -m uvicorn itsm_portal.app:app --host 127.0.0.1 --port 8002
start_process rpa_worker "$VENV_PY" -m rpa.worker

if [[ -f frontend/package.json ]]; then
  start_process frontend npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
else
  printf 'frontend/package.json is missing; frontend was not started.\n' >&2
fi

printf 'AutoHeal SANDBOX is running. Press Ctrl+C to stop the started processes.\n'
wait