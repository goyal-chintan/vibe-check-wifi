from wifi_call_health import run_menu


def test_run_menu_exit_directly():
    inputs = iter(["7"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Exit" in line for line in lines)


def test_run_menu_retries_on_invalid_option():
    inputs = iter(["999", "7"])
    lines = []

    run_menu(
        input_fn=lambda _: next(inputs),
        output_fn=lines.append,
        handlers={},
    )

    assert any("Invalid option" in line for line in lines)
