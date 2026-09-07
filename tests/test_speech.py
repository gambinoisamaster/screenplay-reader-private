from screenplay_reader import speech


def test_int_ext_always_expanded():
    assert speech.scene_heading("INT. HOTEL BEL-AIR - GRACE KELLY SUITE - SITTING ROOM - DAY") == (
        "Interior. Hotel Bel-Air, Grace Kelly Suite, Sitting Room. Day."
    )
    assert speech.scene_heading("EXT. PARK - CONTINUOUS") == "Exterior. Park. Continuous."
    assert speech.scene_heading("INT./EXT. CAR - NIGHT") == "Interior, Exterior. Car. Night."
    assert speech.scene_heading("I/E TAXI - LATER") == "Interior, Exterior. Taxi. Later."
    assert speech.scene_heading("INT. KITCHEN") == "Interior. Kitchen."


def test_scene_numbers_dropped():
    assert speech.scene_heading("12 INT. CAR - NIGHT 12") == "Interior. Car. Night."


def test_no_raw_int_ext_survives():
    for heading in ["INT. A - DAY", "EXT. B - NIGHT", "INT/EXT. C - DAY", "int. d - day"]:
        spoken = speech.scene_heading(heading)
        assert "INT" not in spoken and "EXT" not in spoken


def test_character_cue_is_a_readable_name():
    assert speech.character_cue("ETHAN") == "Ethan."
    assert speech.character_cue("MRS. DALLOWAY") == "Mrs. Dalloway."
    assert speech.character_cue("O'BRIEN") == "O'Brien."
    assert speech.character_cue("KYLE'S MOM") == "Kyle's Mom."
    assert speech.character_cue("COP #2") == "Cop number 2."


def test_action_line_reads_names_but_keeps_acronyms():
    names = {"KYLE", "AL"}
    spoken = speech.action_line("KYLE, 30s, nods. AL turns on the TV. The FBI arrives.", names)
    assert spoken == "Kyle, 30s, nods. Al turns on the TV. The FBI arrives."


def test_wrapped_lines_collapse_to_one_utterance():
    wrapped = "Excellent work, guys. This is\nexactly what we're going for. It's\nbold."
    assert speech.dialogue(wrapped) == "Excellent work, guys. This is exactly what we're going for. It's bold."
    assert "\n" not in speech.dialogue(wrapped)


def test_curly_apostrophes_become_ascii():
    # Final Draft exports U+2019; Pocket-TTS's tokenizer has no token for it.
    assert speech.dialogue("Can\u2019t have my baby looking musty") == "Can't have my baby looking musty"
    assert speech.dialogue("\u2018quoted\u2019 and \u201cdouble\u201d\u2026") == "'quoted' and \"double\"..."
    assert speech.character_cue("O\u2019BRIEN") == "O'Brien."
    assert speech.scene_heading("INT. O\u2019BRIEN\u2019S PUB - NIGHT") == "Interior. O'Brien's Pub. Night."
