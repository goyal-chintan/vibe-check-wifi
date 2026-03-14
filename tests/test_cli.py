from pathlib import Path
from unittest.mock import patch

import vibe_check_wifi
from vibe_check_wifi import load_settings, run_menu, save_settings


def test_run_menu_exit_directly():
    inputs = iter(["9"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Exit" in line for line in lines)
    assert any("5) Choose Theme" in line for line in lines)
    assert any("9) Exit" in line for line in lines)


@patch("vibe_check_wifi.run_check")
def test_menu_quick_check_bypasses_prompts(mock_run_check):
    inputs = iter(["1", "9"])
    lines = []
    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers=None,
    )
    mock_run_check.assert_called_once()
    args, kwargs = mock_run_check.call_args
    assert args[0] in ["video", "audio", "video_share"] # profile
    assert kwargs["minutes"] == 1
    assert kwargs["include_speed_test"] is True


def test_run_menu_retries_on_invalid_option():
    inputs = iter(["999", "9"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Invalid option" in line for line in lines)


def test_run_menu_can_change_theme():
    inputs = iter(["5", "3", "9"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Theme updated to: Vibe Arcade" in line for line in lines)


def test_run_menu_can_change_theme_to_studio_board():
    inputs = iter(["5", "1", "9"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Theme updated to: Studio Board" in line for line in lines)


def test_settings_round_trip_theme(tmp_path):
    settings_path = Path(tmp_path) / "settings.json"
    save_settings({"theme": "studio_board"}, path=settings_path)
    loaded = load_settings(path=settings_path)
    assert loaded["theme"] == "studio_board"


def test_invalid_saved_theme_falls_back_to_default(tmp_path):
    settings_path = Path(tmp_path) / "settings.json"
    settings_path.write_text('{"theme":"unknown"}\n', encoding="utf-8")
    loaded = load_settings(path=settings_path)
    assert loaded["theme"] == "studio_board"
