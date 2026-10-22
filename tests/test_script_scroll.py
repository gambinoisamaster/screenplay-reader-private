from screenplay_reader.ui.main_window import _centered_scroll_value


def test_centered_scroll_value_places_passage_in_viewport_center():
    assert _centered_scroll_value(120, 240, 280, 400, 1000) == 180


def test_centered_scroll_value_clamps_at_document_start_and_end():
    assert _centered_scroll_value(0, 10, 30, 400, 1000) == 0
    assert _centered_scroll_value(950, 500, 540, 400, 1000) == 1000


def test_char_map_follows_wrapped_lines_and_indentation():
    from screenplay_reader.ui.main_window import _char_map

    text = "So there's the scene of the crime as they say. He re-ties it."
    lines = ["So there's the scene of the crime", "as they say. He re-", "ties it."]
    pad = " " * 10
    rendered = "\n".join(pad + line for line in lines)
    positions = _char_map(text, rendered, base=100, skip=len(pad))
    assert len(positions) == len(text) + 1
    for i, ch in enumerate(text):
        if not ch.isspace():
            assert rendered[positions[i] - 100] == ch
    # "as" starts the second rendered line, after its indentation.
    assert positions[text.index("as they")] == 100 + rendered.index("as they")
