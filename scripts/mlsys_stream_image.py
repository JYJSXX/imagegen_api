#!/usr/bin/env python3
"""Generate images through api.mlsys.online using config.toml, auth.json, and SSE."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

try:
    import tomllib
except ImportError as exc:  # pragma: no cover - Python < 3.11
    raise SystemExit("Python 3.11 or newer is required (missing tomllib).") from exc

ALLOWED_HOST = "api.mlsys.online"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "medium"
DEFAULT_FORMAT = "png"
DEFAULT_OUTPUT = "output/imagegen/output.png"
BASE_KEYS = {"baseurl", "openaibaseurl", "apibaseurl"}
API_KEY_KEYS = {"apikey", "openaiapikey"}
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_FORMATS = {"png", "jpeg", "webp"}
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
MAX_RATIO = 3.0


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def walk_tables(
    node: Mapping[str, Any], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Mapping[str, Any]]]:
    yield path, node
    for key, value in node.items():
        if isinstance(value, Mapping):
            yield from walk_tables(value, path + (str(key),))


def normalized_entries(table: Mapping[str, Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for key, value in table.items():
        if isinstance(value, Mapping):
            continue
        result.setdefault(canonical_key(str(key)), []).append(value)
    return result


def values_for(entries: Mapping[str, list[Any]], allowed: set[str]) -> list[str]:
    found: list[str] = []
    for key in allowed:
        for value in entries.get(key, []):
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
    return found


def normalize_base_url(raw: str) -> tuple[str, str]:
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https":
        die("BASE_URL must use HTTPS.")
    if parsed.username or parsed.password:
        die("BASE_URL must not contain user information.")
    if (parsed.hostname or "").lower() != ALLOWED_HOST:
        die(f"BASE_URL hostname must be exactly {ALLOWED_HOST}.")
    try:
        port = parsed.port
    except ValueError:
        die("BASE_URL contains an invalid port.")
    if port not in (None, 443):
        die("BASE_URL may only use the default HTTPS port 443.")
    if parsed.query or parsed.fragment:
        die("BASE_URL must not contain a query string or fragment.")

    path = parsed.path.rstrip("/")
    clean_base = urlunparse(("https", ALLOWED_HOST, path, "", "", ""))
    if path.endswith("/v1"):
        endpoint = clean_base + "/images/generations"
    else:
        endpoint = clean_base + "/v1/images/generations"
    return clean_base, endpoint


def is_allowed_base_url(raw: str) -> bool:
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and not parsed.username
        and not parsed.password
        and (parsed.hostname or "").lower() == ALLOWED_HOST
        and port in (None, 443)
        and not parsed.query
        and not parsed.fragment
    )


def extract_base_url(document: Mapping[str, Any]) -> str:
    bases: list[str] = []
    for _, table in walk_tables(document):
        entries = normalized_entries(table)
        bases.extend(values_for(entries, BASE_KEYS))
    valid = list(dict.fromkeys(base for base in bases if is_allowed_base_url(base)))
    if not valid:
        raise ValueError(f"No BASE_URL for {ALLOWED_HOST} was found in config.toml.")
    if len(valid) > 1:
        raise ValueError("Multiple api.mlsys.online BASE_URL values were found in config.toml.")
    return valid[0]


def extract_api_key(document: Mapping[str, Any]) -> str:
    keys: list[str] = []
    for _, table in walk_tables(document):
        entries = normalized_entries(table)
        keys.extend(values_for(entries, API_KEY_KEYS))
    unique = list(dict.fromkeys(keys))
    if not unique:
        raise ValueError(
            "No OPENAI_API_KEY/api_key/apikey value was found in auth.json. "
            "OAuth access_token values are intentionally not used."
        )
    if len(unique) > 1:
        raise ValueError("Multiple API key values were found in auth.json.")
    return unique[0]


def dedupe_paths(candidates: Sequence[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(path)
    return deduped


def config_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]

    candidates: list[Path] = []
    env_path = os.environ.get("MLSYS_CONFIG_TOML")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [Path.cwd() / "config.toml", Path.cwd() / ".codex" / "config.toml"]
    )
    codex_home = Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser()
    candidates.append(codex_home / "config.toml")
    return dedupe_paths(candidates)


def auth_candidates(explicit: str | None, config_path: Path) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]

    candidates: list[Path] = []
    env_path = os.environ.get("MLSYS_AUTH_JSON")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            config_path.with_name("auth.json"),
            Path.cwd() / "auth.json",
            Path.cwd() / ".codex" / "auth.json",
        ]
    )
    codex_home = Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser()
    candidates.append(codex_home / "auth.json")
    return dedupe_paths(candidates)


def warn_if_permissions_are_broad(path: Path, label: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        warn(f"{label} is readable by group/others; consider chmod 600 {path}")


def load_config(explicit: str | None) -> tuple[Path, str, str]:
    existing = [path for path in config_candidates(explicit) if path.is_file()]
    if not existing:
        if explicit:
            die(f"Config file not found: {Path(explicit).expanduser()}")
        die("No config.toml found. Use --config or set MLSYS_CONFIG_TOML.")

    failures: list[str] = []
    for path in existing:
        try:
            warn_if_permissions_are_broad(path, "Config file")
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            raw_base = extract_base_url(document)
            base_url, endpoint = normalize_base_url(raw_base)
            return path, base_url, endpoint
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            failures.append(f"{path}: {exc}")
            if explicit:
                die(str(exc))

    die(
        "No usable MLSys BASE_URL found in discovered config files ("
        + "; ".join(failures)
        + ")."
    )
    raise AssertionError("unreachable")


def load_auth(explicit: str | None, config_path: Path) -> tuple[Path, str]:
    existing = [
        path for path in auth_candidates(explicit, config_path) if path.is_file()
    ]
    if not existing:
        if explicit:
            die(f"Auth file not found: {Path(explicit).expanduser()}")
        die("No auth.json found. Use --auth or set MLSYS_AUTH_JSON.")

    failures: list[str] = []
    for path in existing:
        try:
            warn_if_permissions_are_broad(path, "Auth file")
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, Mapping):
                raise ValueError("auth.json root must be a JSON object.")
            return path, extract_api_key(document)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            failures.append(f"{path}: {exc}")
            if explicit:
                die(str(exc))

    die(
        "No usable API key found in discovered auth.json files ("
        + "; ".join(failures)
        + ")."
    )
    raise AssertionError("unreachable")


def validate_size(size: str) -> None:
    if size == "auto":
        return
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if not match:
        die("size must be auto or WIDTHxHEIGHT, for example 1024x1024.")
    width, height = int(match.group(1)), int(match.group(2))
    if max(width, height) > MAX_EDGE:
        die("gpt-image-2 size maximum edge is 3840px.")
    if width % 16 or height % 16:
        die("gpt-image-2 width and height must be multiples of 16px.")
    pixels = width * height
    if not MIN_PIXELS <= pixels <= MAX_PIXELS:
        die("gpt-image-2 total pixels must be between 655,360 and 8,294,400.")
    if max(width, height) / min(width, height) > MAX_RATIO:
        die("gpt-image-2 long-to-short edge ratio must not exceed 3:1.")


def read_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if bool(prompt) == bool(prompt_file):
        die("Use exactly one of --prompt or --prompt-file.")
    if prompt_file:
        try:
            result = Path(prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            die(f"Cannot read prompt file: {exc}")
    else:
        result = (prompt or "").strip()
    if not result:
        die("Prompt must not be empty.")
    return result


def output_paths(out: str, output_format: str, count: int) -> list[Path]:
    path = Path(out)
    suffix = ".jpg" if output_format == "jpeg" else "." + output_format
    valid_suffixes = {".jpg", ".jpeg"} if output_format == "jpeg" else {suffix}
    if not path.suffix:
        path = path.with_suffix(suffix)
    elif path.suffix.lower() not in valid_suffixes:
        die(f"Output extension does not match output format {output_format}.")
    if count == 1:
        return [path]
    return [
        path.with_name(f"{path.stem}-{index}{path.suffix}")
        for index in range(1, count + 1)
    ]


def ensure_outputs_available(paths: Sequence[Path], force: bool) -> None:
    if force:
        return
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        die(
            "Output already exists: "
            + ", ".join(existing)
            + " (use --force only with user approval)."
        )


def atomic_write_image(path: Path, encoded: str, force: bool) -> None:
    if path.exists() and not force:
        die(f"Output already exists: {path}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        die(f"Server returned invalid base64 image data: {exc}")
    if not raw:
        die("Server returned an empty image.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def iter_sse(stream: BinaryIO) -> Iterator[tuple[str | None, str]]:
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines or event_name:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines or event_name:
        yield event_name, "\n".join(data_lines)


def event_images(payload: Mapping[str, Any]) -> list[str]:
    images: list[str] = []
    for field in ("b64_json", "partial_image_b64"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            images.append(value)
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, Mapping):
                value = item.get("b64_json")
                if isinstance(value, str) and value:
                    images.append(value)
    return images


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def sanitize(message: str, api_key: str) -> str:
    return message.replace(api_key, "<redacted>") if api_key else message


def api_error_message(body: bytes, api_key: str) -> str:
    text = body.decode("utf-8", errors="replace")[:2048]
    try:
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(error, str):
                text = error
    except json.JSONDecodeError:
        pass
    return sanitize(text.strip() or "no response body", api_key)


def stream_generate(
    endpoint: str, api_key: str, payload: Mapping[str, Any], timeout: float
) -> list[str]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "mlsys-stream-imagegen/1.0",
        },
        method="POST",
    )
    context = ssl.create_default_context()
    opener = build_opener(NoRedirects(), HTTPSHandler(context=context))
    completed: list[str] = []
    partial_count = 0
    connected_reported = False
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = urlparse(response.geturl())
            if (
                final_url.scheme != "https"
                or (final_url.hostname or "").lower() != ALLOWED_HOST
            ):
                die("The response URL left the allowed api.mlsys.online HTTPS origin.")
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                body = response.read(2048)
                die(
                    f"Expected text/event-stream, received {content_type}: "
                    f"{api_error_message(body, api_key)}"
                )
            print("SSE stream connected; waiting for image events...", file=sys.stderr)
            connected_reported = True
            for event_name, data in iter_sse(response):
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    warn("Ignoring a non-JSON SSE data event.")
                    continue
                if not isinstance(event, Mapping):
                    continue
                event_type = str(event.get("type") or event_name or "")
                if (
                    event_type in {"error", "image_generation.failed"}
                    or event_type.endswith(".failed")
                ):
                    detail = (
                        event.get("error")
                        or event.get("message")
                        or "image generation failed"
                    )
                    die(sanitize(str(detail), api_key))
                images = event_images(event)
                if event_type.endswith("partial_image"):
                    partial_count += len(images) or 1
                    print(
                        f"Received partial image event {partial_count}.",
                        file=sys.stderr,
                    )
                elif (
                    event_type == "image_generation.completed"
                    or event_type.endswith(".completed")
                ):
                    completed.extend(images)
                    print(
                        f"Received completed image event ({len(completed)} image(s)).",
                        file=sys.stderr,
                    )
    except HTTPError as exc:
        body = exc.read(2048)
        if 300 <= exc.code < 400:
            die(
                f"Redirect refused (HTTP {exc.code}); endpoint must remain on "
                f"{ALLOWED_HOST}."
            )
        die(
            f"API request failed with HTTP {exc.code}: "
            f"{api_error_message(body, api_key)}"
        )
    except URLError as exc:
        die("API connection failed: " + sanitize(str(exc.reason), api_key))
    except TimeoutError:
        die(f"The SSE connection had no data for {timeout:g} seconds.")
    except OSError as exc:
        die("API connection failed: " + sanitize(str(exc), api_key))
    if not connected_reported:
        die("The SSE connection was not established.")
    if not completed:
        die("The SSE stream ended without image_generation.completed output.")
    return completed


def generate(args: argparse.Namespace) -> None:
    prompt = read_prompt(args.prompt, args.prompt_file)
    validate_size(args.size)
    if args.quality not in ALLOWED_QUALITIES:
        die("quality must be low, medium, high, or auto.")
    if args.output_format not in ALLOWED_FORMATS:
        die("output-format must be png, jpeg, or webp.")
    if not 1 <= args.n <= 10:
        die("n must be between 1 and 10.")
    if not 0 <= args.partial_images <= 3:
        die("partial-images must be between 0 and 3.")
    if args.timeout <= 0:
        die("timeout must be greater than zero.")

    config_path, base_url, endpoint = load_config(args.config)
    auth_path, api_key = load_auth(args.auth, config_path)
    expected_outputs = output_paths(args.out, args.output_format, args.n)
    ensure_outputs_available(expected_outputs, args.force)
    payload = {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
        "stream": True,
        "partial_images": args.partial_images,
    }

    if args.dry_run:
        print(
            json.dumps(
                {
                    "config": str(config_path),
                    "auth": str(auth_path),
                    "base_url": base_url,
                    "endpoint": endpoint,
                    "api_key": "<redacted>",
                    "outputs": [str(path) for path in expected_outputs],
                    "payload": payload,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    images = stream_generate(endpoint, api_key, payload, args.timeout)
    actual_outputs = output_paths(args.out, args.output_format, len(images))
    ensure_outputs_available(actual_outputs, args.force)
    for path, encoded in zip(actual_outputs, images):
        atomic_write_image(path, encoded, args.force)
        print(f"Wrote {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate images through api.mlsys.online using BASE_URL from "
            "config.toml, API key from auth.json, and SSE"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser(
        "generate", help="Generate a new image with SSE streaming"
    )
    generate_parser.add_argument("--config", help="Explicit config.toml path")
    generate_parser.add_argument("--auth", help="Explicit auth.json path")
    prompt_group = generate_parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    generate_parser.add_argument("--size", default=DEFAULT_SIZE)
    generate_parser.add_argument("--quality", default=DEFAULT_QUALITY)
    generate_parser.add_argument("--output-format", default=DEFAULT_FORMAT)
    generate_parser.add_argument("--n", type=int, default=1)
    generate_parser.add_argument("--partial-images", type=int, default=1)
    generate_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Socket inactivity timeout, not total duration",
    )
    generate_parser.add_argument("--out", default=DEFAULT_OUTPUT)
    generate_parser.add_argument("--force", action="store_true")
    generate_parser.add_argument("--dry-run", action="store_true")
    generate_parser.set_defaults(func=generate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
