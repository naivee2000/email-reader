"""Recognize passive raster mail images even when a provider omits MIME type."""

from pathlib import Path

_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
_VECTOR_MIME = {"image/svg+xml", "image/svg", "image/x-svg"}


def raster_mime(content: bytes) -> str:
    """Return a known raster MIME from a short signature, never from sender text."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM"):
        return "image/bmp"
    if content.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def is_mail_image(mime_type: str, filename: str) -> bool:
    """Permit a raster extension only when MIME is generic, not contradictory."""
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return (mime.startswith("image/") and mime not in _VECTOR_MIME) or (
        mime in _GENERIC_MIME and Path(filename or "").suffix.lower() in _RASTER_SUFFIXES
    )


def mail_image_sql(alias: str) -> str:
    """SQLite equivalent of ``is_mail_image`` for trusted table aliases."""
    if not alias.isidentifier():
        raise ValueError("Invalid attachment table alias")
    raw_mime = f"coalesce({alias}.mime_type,'')"
    mime = f"lower(trim(substr({raw_mime},1,instr({raw_mime}||';',';')-1)))"
    filename = f"lower(coalesce({alias}.filename,''))"
    extensions = " OR ".join(f"{filename} LIKE '%{suffix}'" for suffix in sorted(_RASTER_SUFFIXES))
    vector = ",".join(f"'{item}'" for item in sorted(_VECTOR_MIME))
    generic = ",".join(f"'{item}'" for item in sorted(_GENERIC_MIME))
    return (
        f"(({mime} LIKE 'image/%' AND {mime} NOT IN ({vector})) OR "
        f"({mime} IN ({generic}) AND ({extensions})))"
    )


def visible_mail_file_sql(alias: str) -> str:
    """Keep non-image inline documents in file search and answer cards."""
    if not alias.isidentifier():
        raise ValueError("Invalid attachment table alias")
    return f"NOT ({alias}.is_inline=1 AND {mail_image_sql(alias)})"
