from screenplay_reader.validate import normalize, word_error_rate


def test_normalize_ignores_case_punctuation_and_spells_numbers():
    assert normalize("Room 2, NOW!") == ["room", "two", "now"]
    assert normalize("It's bold.") == ["it's", "bold"]


def test_exact_match_is_zero():
    assert word_error_rate("For real? Not too out there?", "for real, not too out there") == 0.0


def test_dropped_word_counts():
    rate = word_error_rate("Excellent work, guys. This is bold.", "Excellent work. This is bold.")
    assert abs(rate - 1 / 6) < 1e-9


def test_garbage_is_high():
    assert word_error_rate("Hey, man. What you think?", "banana") > 0.5
