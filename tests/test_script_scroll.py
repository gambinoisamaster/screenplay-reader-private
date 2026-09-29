from screenplay_reader.ui.main_window import _centered_scroll_value


def test_centered_scroll_value_places_passage_in_viewport_center():
    assert _centered_scroll_value(120, 240, 280, 400, 1000) == 180


def test_centered_scroll_value_clamps_at_document_start_and_end():
    assert _centered_scroll_value(0, 10, 30, 400, 1000) == 0
    assert _centered_scroll_value(950, 500, 540, 400, 1000) == 1000
