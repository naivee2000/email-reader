"""Contrast contract for the implementation's shared warm palette."""

import re
from pathlib import Path

import pytest

THEME = dict(re.findall(
    r'var\s+(\w+)\s*=\s*"(#[\da-fA-F]{6})"',
    (Path(__file__).resolve().parents[1] / "src/email_reader/qml/Theme.js").read_text(),
))


def luminance(value):
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722), strict=True))


def contrast(foreground, background):
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + .05) / (dark + .05)


@pytest.mark.parametrize("background", ["surface", "background", "selection", "hover"])
@pytest.mark.parametrize("foreground", ["text", "secondary", "accent", "danger"])
def test_text_including_placeholder_disabled_and_selected_meets_45(foreground, background):
    ratio = contrast(THEME[foreground], THEME[background])
    assert ratio >= 4.5, f"{foreground} on {background}: {ratio:.2f}:1"


@pytest.mark.parametrize("background", ["surface", "background", "selection", "hover"])
@pytest.mark.parametrize("foreground", ["controlBorder", "accent"])
def test_control_boundaries_and_focus_indicators_meet_3(foreground, background):
    ratio = contrast(THEME[foreground], THEME[background])
    assert ratio >= 3, f"{foreground} on {background}: {ratio:.2f}:1"


def test_primary_button_text_meets_45():
    assert contrast(THEME["surface"], THEME["accent"]) >= 4.5


def test_outline_icons_meet_control_contrast_in_all_navigation_states():
    for foreground in ("text", "secondary", "accent"):
        for background in ("surface", "background", "selection", "hover"):
            assert contrast(THEME[foreground], THEME[background]) >= 3


def test_incomplete_evidence_warning_is_readable():
    assert contrast(THEME["danger"], THEME["surface"]) >= 4.5
