from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import zipfile
import xml.etree.ElementTree as ET


AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".mp4"
}


def audio_files(path: str, limit: int) -> list[str]:
    p = Path(path)
    if p.is_file():
        return [str(p)] if p.suffix.lower() in AUDIO_EXTENSIONS else []
    if not p.is_dir():
        return []
    found: list[str] = []
    for root, _, names in os.walk(p):
        for name in sorted(names):
            candidate = Path(root) / name
            if candidate.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(str(candidate))
                if len(found) >= limit:
                    return found
    return found


def ffprobe_metadata(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=duration:format_tags=artist,album_artist,author,composer,album,title,genre",
        "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        return {"probe_error": proc.stderr.strip()[:500], "path": path}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"probe_error": "ffprobe returned invalid JSON", "path": path}
    fmt = payload.get("format") or {}
    tags = fmt.get("tags") or {}
    normalized = {str(k).lower(): v for k, v in tags.items()}
    return {
        "path": path,
        "artist": normalized.get("artist", ""),
        "album_artist": normalized.get("album_artist", ""),
        "author": normalized.get("author", ""),
        "composer": normalized.get("composer", ""),
        "album": normalized.get("album", ""),
        "title": normalized.get("title", ""),
        "genre": normalized.get("genre", ""),
        "duration": fmt.get("duration", ""),
    }


def epub_metadata(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            rootfile = None
            for elem in container.iter():
                if elem.tag.endswith("rootfile"):
                    rootfile = elem.attrib.get("full-path")
                    if rootfile:
                        break
            if not rootfile:
                return {"error": "EPUB container does not declare an OPF package."}
            package = ET.fromstring(zf.read(rootfile))
            title = ""
            creators: list[str] = []
            for elem in package.iter():
                local = elem.tag.rsplit("}", 1)[-1].lower()
                if local == "title" and not title and elem.text:
                    title = elem.text.strip()
                elif local == "creator" and elem.text:
                    creators.append(elem.text.strip())
            return {"title": title, "author": "; ".join(creators)}
    except Exception as exc:
        return {"error": str(exc)[:500]}


def pdf_metadata(path: str) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        md = reader.metadata or {}
        return {"title": str(md.get("/Title") or ""), "author": str(md.get("/Author") or "")}
    except Exception as exc:
        return {"error": str(exc)[:500]}


def ebook_metadata(path: str) -> dict:
    suffix = Path(path).suffix.lower()
    if suffix == ".epub":
        return epub_metadata(path)
    if suffix == ".pdf":
        return pdf_metadata(path)
    return {"unsupported": suffix or "unknown"}
