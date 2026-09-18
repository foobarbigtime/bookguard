from __future__ import annotations

import zipfile


DEFAULT_XML_MEMBER_LIMIT = 4 * 1024 * 1024


def read_zip_member_bounded(
    archive: zipfile.ZipFile,
    name: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read one ZIP member without allowing unbounded metadata expansion."""
    limit = DEFAULT_XML_MEMBER_LIMIT if max_bytes is None else int(max_bytes)
    if limit < 1:
        raise ValueError("ZIP member read limit must be positive.")

    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"ZIP member is missing: {name}") from exc

    if info.is_dir():
        raise ValueError(f"ZIP member {name!r} is a directory, not a file.")
    if info.file_size > limit:
        raise ValueError(
            f"ZIP member {name!r} exceeds the {limit}-byte metadata limit."
        )

    with archive.open(info, "r") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(
            f"ZIP member {name!r} exceeds the {limit}-byte metadata limit."
        )
    return payload
