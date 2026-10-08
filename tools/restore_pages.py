"""Restore a verified Pages site from small Git-compatible Web export parts."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

SITE_FILES = {
    "index.html", "index.js", "index.wasm", "index.pck", "client-config.js",
    "index.apple-touch-icon.png", "index.icon.png", "index.png",
    "index.audio.position.worklet.js", "index.audio.worklet.js",
    "licenses/MPLUSRounded1c-OFL.txt",
}


def websocket_url(value: str, allow_local: bool = False) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
        local = parsed.hostname == "localhost"
        if parsed.hostname:
            try:
                local = local or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        allowed = parsed.scheme == "wss" or (allow_local and local and parsed.scheme == "ws")
        if (allowed and parsed.hostname and parsed.username is None and parsed.password is None
                and not parsed.fragment and (port is None or 1 <= port <= 65535)
                and not any(character.isspace() for character in value)):
            return value
    except ValueError:
        pass
    raise ValueError("Set PUBLIC_WEBSOCKET_URL to wss://your-render-service.onrender.com/ws. "
                     "Local loopback ws:// is accepted only with --allow-local-websocket.")


def restore(source: Path, output: Path, url: str, allow_local: bool = False) -> dict:
    url = websocket_url(url, allow_local)
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Source and site output must be separate directories.")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("renderer") != "gl_compatibility" or manifest.get("threads") is not False:
        raise ValueError("Pages requires the existing Compatibility single-thread Web export.")
    entries = manifest["files"]
    if {entry["name"] for entry in entries} != SITE_FILES or len(entries) != len(SITE_FILES):
        raise ValueError("The release must contain exactly the game runtime and font license.")
    if output.exists():
        unexpected = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()} - SITE_FILES - {".nojekyll"}
        if unexpected:
            raise ValueError(f"Output contains unrelated files; use another directory: {sorted(unexpected)}")
    output.mkdir(parents=True, exist_ok=True)
    restored = []
    for entry in entries:
        name = entry["name"]
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, length = hashlib.sha256(), 0
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            for part in entry["parts"]:
                relative = Path(part["path"])
                if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2 or relative.parts[0] != "chunks":
                    raise ValueError("Invalid chunk path in release manifest.")
                chunk_path = (source / relative).resolve()
                if source not in chunk_path.parents or chunk_path.is_symlink():
                    raise ValueError("Chunk leaves the release directory.")
                chunk = chunk_path.read_bytes()
                if len(chunk) != part["bytes"] or hashlib.sha256(chunk).hexdigest() != part["sha256"]:
                    raise ValueError(f"Missing or damaged upload part: {relative}")
                stream.write(chunk)
                digest.update(chunk)
                length += len(chunk)
        if length != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise ValueError(f"Reconstructed runtime differs from the original export: {name}")
        temporary.replace(target)
        restored.append(name)
    html = (output / "index.html").read_text(encoding="utf-8")
    config_tag, engine_tag = '<script src="client-config.js"></script>', '<script src="index.js"></script>'
    if html.count(config_tag) != 1 or html.index(config_tag) > html.index(engine_tag):
        raise ValueError("Runtime connection settings must load before the Web engine.")
    if 'src="/' in html or 'href="/' in html:
        raise ValueError("Root-relative resources would break a Pages project URL.")
    (output / "client-config.js").write_text(
        "window.BLUE_CIRCUIT_CONFIG = " + json.dumps({"websocket_url": url}, ensure_ascii=True) + ";\n", encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return {"files": restored + [".nojekyll"], "websocket_url": url, "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("publish/web"))
    parser.add_argument("--output", type=Path, default=Path("build/pages_site"))
    parser.add_argument("--websocket-url", default=os.environ.get("PUBLIC_WEBSOCKET_URL", ""))
    parser.add_argument("--allow-local-websocket", action="store_true", help="For local regression only; never used by the Pages workflow")
    args = parser.parse_args()
    result = restore(args.source, args.output, args.websocket_url, args.allow_local_websocket)
    print(f"Restored {len(result['files'])} Pages files; WebSocket: {result['websocket_url']}")


if __name__ == "__main__":
    main()
