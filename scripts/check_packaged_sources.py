"""Read-only check that a frozen candidate contains the current application code."""
from __future__ import annotations

import argparse
import marshal
from pathlib import Path
from types import CodeType

from PyInstaller.archive.readers import CArchiveReader


def normalize(code):
    if not isinstance(code, CodeType):
        return code
    return code.replace(co_filename="source", co_consts=tuple(normalize(value) for value in code.co_consts))


def verify(candidate: Path) -> None:
    project = Path(__file__).resolve().parent.parent
    archive = CArchiveReader(str(candidate / "Contents" / "MacOS" / "Email Reader"))
    embedded = next(name for name in archive.toc if name.endswith(".pyz"))
    modules = archive.open_embedded_archive(embedded)
    checked = 0
    for source in (project / "src" / "email_reader").rglob("*.py"):
        name = ".".join(source.relative_to(project / "src").with_suffix("").parts)
        if name.endswith(".__init__"):
            name = name.removesuffix(".__init__")
        if name == "email_reader.__main__":
            continue
        packed = (marshal.loads(archive.extract("app")) if name == "email_reader.app"
                  else modules.extract(name))
        compiled = compile(source.read_bytes(), str(source), "exec", optimize=0, dont_inherit=True)
        if normalize(packed) != normalize(compiled):
            raise RuntimeError(f"Packaged source differs: {name}")
        checked += 1
    for source in (project / "src" / "email_reader" / "qml").rglob("*"):
        if source.is_file():
            packed = candidate / "Contents" / "Resources" / "email_reader" / "qml" / source.relative_to(project / "src" / "email_reader" / "qml")
            if packed.read_bytes() != source.read_bytes():
                raise RuntimeError("Packaged QML differs from source")
    print(f"Verified {checked} Python modules and all QML assets match current source")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    verify(parser.parse_args().candidate)
