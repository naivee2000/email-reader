"""Guard the light-theme text and control colors used by the mail workflow."""

import re
from pathlib import Path

import pytest

QML = Path(__file__).resolve().parents[1] / "src" / "email_reader" / "qml"
THEME = dict(re.findall(r'var (\w+) = "(#[0-9A-Fa-f]{6})"', (QML / "Theme.js").read_text()))


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def _contrast(foreground: str, background: str) -> float:
    brighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    ("file", "foreground", "background", "minimum"),
    [
        ("Main.qml", "text", "surface", 4.5),
        ("Main.qml", "secondary", "surface", 4.5),
        ("Main.qml", "secondary", "background", 4.5),
        ("Main.qml", "secondary", "hover", 4.5),
        ("Main.qml", "secondary", "selection", 4.5),
        ("Main.qml", "accent", "selection", 4.5),
        ("DraftEditor.qml", "text", "surface", 4.5),
        ("DraftEditor.qml", "secondary", "surface", 4.5),
        ("DraftEditor.qml", "accent", "surface", 4.5),
        ("DraftEditor.qml", "secondary", "hover", 4.5),
        ("DraftEditor.qml", "secondary", "hover", 4.5),
        ("MailAssistant.qml", "text", "background", 4.5),
        ("MailAssistant.qml", "secondary", "background", 4.5),
        ("MailAssistant.qml", "danger", "surface", 4.5),
        ("DraftEditor.qml", "controlBorder", "background", 3.0),
        ("DraftEditor.qml", "controlBorder", "surface", 3.0),
    ],
)
def test_mail_interface_contrast(file, foreground, background, minimum):
    source = (QML / file).read_text()
    assert 'import "Theme.js" as Theme' in source
    assert _contrast(THEME[foreground], THEME[background]) >= minimum
