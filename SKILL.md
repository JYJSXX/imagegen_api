---
name: mlsys-stream-imagegen
description: Generate raster images through the OpenAI-compatible API at api.mlsys.online using BASE_URL from config.toml, API key from auth.json, and server-sent-event streaming. Use when the user asks to generate an image with MLSys, api.mlsys.online, their configured endpoint/auth files, or specifically wants streamed GPT Image generation through that gateway. Supports new image generation only, not image editing.
---

# MLSys Streaming ImageGen

Generate images with the bundled `scripts/mlsys_stream_image.py` CLI. The CLI reads `BASE_URL` from `config.toml`, reads `OPENAI_API_KEY`/`api_key`/`apikey` from `auth.json`, requires the exact HTTPS host `api.mlsys.online`, sends `stream: true`, parses SSE, and writes the completed image locally.

## Workflow

1. Treat the request as new-image generation. If the user wants to edit an existing image, explain that this skill does not implement edits and use an appropriate image-editing path instead.
2. Shape the user's prompt into a concise production spec. Preserve exact requested text and constraints. Do not add unrequested subjects, brands, or narrative elements.
3. Choose a non-existing output path:
   - Project asset: place the final under the project's requested asset directory.
   - No requested destination: use `output/imagegen/<descriptive-name>.png` in the current workspace.
4. Run the bundled CLI from the skill directory:

```bash
python3 <skill-dir>/scripts/mlsys_stream_image.py generate \
  --prompt "<final prompt>" \
  --size 1024x1024 \
  --quality medium \
  --out output/imagegen/<descriptive-name>.png
```

5. Add `--config /path/to/config.toml` or `--auth /path/to/auth.json` when discovery is ambiguous.
   - Config order: `--config`, `$MLSYS_CONFIG_TOML`, `./config.toml`, `./.codex/config.toml`, `${CODEX_HOME:-$HOME/.codex}/config.toml`.
   - Auth order: `--auth`, `$MLSYS_AUTH_JSON`, `auth.json` next to the selected config, `./auth.json`, `./.codex/auth.json`, `${CODEX_HOME:-$HOME/.codex}/auth.json`.
6. If network access is denied by the sandbox, rerun the same command with scoped network approval. Do not change endpoints or copy the key into an environment variable as a workaround.
7. Inspect the generated file with the available image-viewing tool. Validate subject, composition, requested text, and constraints. Iterate with one targeted prompt change if necessary, using a new versioned filename unless the user explicitly permits overwrite.
8. Report the saved path and final prompt. Do not mention or expose credential values.

## Credential and endpoint rules

- Never print, quote, log, return, or ask the user to paste the API key.
- Never inspect `auth.json` with commands that display secret-bearing values. Let the bundled CLI read it.
- Read only the endpoint from `config.toml`; do not use API key values found there.
- Read only explicit `OPENAI_API_KEY`, `api_key`, or `apikey` fields from `auth.json`. Do not substitute OAuth `tokens.access_token` or other session tokens.
- Accept only an HTTPS URL whose hostname is exactly `api.mlsys.online` and whose port is absent or `443`.
- Do not follow redirects. Stop if the configured host differs, the API key is missing, or multiple key candidates are ambiguous.
- Expected files:

```toml
# config.toml
BASE_URL = "https://api.mlsys.online"
```

```json
{
  "OPENAI_API_KEY": "<secret>"
}
```

- The config aliases `base_url`, `OPENAI_BASE_URL`, and `API_BASE_URL` are accepted. The auth aliases `api_key` and `apikey` are accepted.

## Streaming behavior

- Always use the CLI's streaming path. Do not replace it with a one-shot SDK call or ordinary JSON request.
- Keep `partial_images=1` by default so progress images can arrive; the CLI saves only `image_generation.completed` output.
- The client ignores SSE comment heartbeats, recognizes partial-image events, and waits for the completed event.
- A timeout is an inactivity timeout, not a total generation deadline. Do not add a 120-second total timeout.
- Do not treat receipt of a partial image as successful completion.

## Useful options

```bash
# Validate discovery and request shape without making a paid API call
python3 <skill-dir>/scripts/mlsys_stream_image.py generate \
  --prompt "Test prompt" --dry-run

# Use explicit config and auth files
python3 <skill-dir>/scripts/mlsys_stream_image.py generate \
  --config /path/to/config.toml --auth /path/to/auth.json \
  --prompt "<prompt>" --out output/imagegen/result.png

# Generate a high-quality landscape image
python3 <skill-dir>/scripts/mlsys_stream_image.py generate \
  --prompt "<prompt>" --size 1536x1024 --quality high \
  --out output/imagegen/result.png
```

Use `--force` only when the user explicitly asked to replace an existing file.
