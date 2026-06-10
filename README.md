# llama-save-wrapper

A proxy wrapper around [`llama-server`](https://github.com/ggml-org/llama.cpp) that automatically saves and restores slot KV caches across restarts.

Starts `llama-server` on an internal port behind an aiohttp reverse proxy. On startup, slot caches are restored from disk before the health endpoint returns 200. On shutdown (Ctrl+C), all slot caches are saved before the backend is terminated.

## How It Works

```
Client → Proxy (public port) → llama-server (internal port)
```

1. **Startup**: Launches `llama-server` on a random ephemeral port, starts the proxy on your chosen public port, waits for the backend to become healthy, then restores KV caches for each slot from `{slot_id}.bin` files.
2. **Running**: All requests (including streaming completions) are proxied to the backend. The `/health` endpoint returns 503 until restoration is complete, so external tools (e.g. `llama-swap`) know when the server is truly ready.
3. **Shutdown**: On Ctrl+C (SIGINT/SIGTERM/SIGHUP), all slot KV caches are saved to disk, then the backend is terminated. A second interrupt forces an immediate quit without saving.

## Prerequisites

- Python 3.13+
- [`uv`](https://github.com/astral-sh/uv) for dependency management
- A `llama-server` binary (from [llama.cpp](https://github.com/ggml-org/llama.cpp))

## Setup

```bash
# Install dependencies
uv sync

# Configure the path to your llama-server binary
echo 'LLAMA_BINARY=/path/to/llama-server' > .env
```

## Usage

```bash
./.venv/bin/python -u main.py \
  --port 12872 \
  -m /path/to/model.gguf \
  --slot-save-path /path/to/cache/dir \
  -c 32768
```

### Required Arguments

| Argument | Description |
|---|---|
| `--port <port>` | Public-facing port for the proxy |
| `--slot-save-path <path>` | Path to save the slot information to |

### Passed-Through Arguments

All other arguments are forwarded to `llama-server`. Key ones for this wrapper:

| Argument | Description |
|---|---|
| `-m <model>` | Path to the model file |
| `--slot-save-path <dir>` | Directory where `{slot_id}.bin` cache files are stored (created if missing) |
| `-c <n>` | Context length |
| `-np <n>` | Number of parallel instances (maps to slot count) |

Any valid `llama-server` flag can be used.

### Slots

The slot count is derived from the `-np` / `--parallel` flag passed to `llama-server`, defaulting to 4 if neither is provided. Each slot's KV cache is saved to `{slot_id}.bin` in the `--slot-save-path` directory. Only slots with existing cache files are restored on startup.

## Shutdown Behavior

| Action | Result |
|---|---|
| **Ctrl+C (once)** | Saves all slot caches, then terminates the backend |
| **Ctrl+C (twice)** | Force quits immediately without saving |

## Configuration

### `.env`

| Variable | Description | Default |
|---|---|---|
| `LLAMA_BINARY` | Path to the `llama-server` binary | `/usr/local/bin/llama-server` |
