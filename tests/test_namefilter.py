from ringleader import namefilter


def test_brackets_are_literal_not_a_character_class():
    # fnmatch would read "[cadet-city]" as a one-character class and match "c"
    assert namefilter.matches("[cadet-city] cadet/cadet.reviewer •", "[cadet-city]*")
    assert not namefilter.matches("c", "[cadet-city]")
    assert namefilter.matches("[cadet-city]", "[cadet-city]")


def test_star_and_question_are_wildcards():
    assert namefilter.matches("prefix anything", "prefix *")
    assert namefilter.matches("a.b", "a?b")
    assert not namefilter.matches("ab", "a?b")


def test_match_is_anchored_to_the_whole_name():
    assert not namefilter.matches("run [cadet-city] job", "[cadet-city]*")
    assert namefilter.matches("run [cadet-city] job", "*[cadet-city]*")


def test_regex_metacharacters_stay_literal():
    assert namefilter.matches("a.b", "a.b")
    assert not namefilter.matches("axb", "a.b")


def test_first_match_returns_the_matching_pattern():
    assert namefilter.first_match("[x] job", ["nope*", "[x]*"]) == "[x]*"
    assert namefilter.first_match("[x] job", ["nope*"]) is None
    assert namefilter.first_match(None, ["*"]) is None
    assert namefilter.first_match("", ["*"]) is None


def test_thread_match_falls_back_to_the_suggested_name():
    assert namefilter.thread_match({"title": "[x] a", "suggestedName": "other"}, ["[x]*"]) == "[x]*"
    assert namefilter.thread_match({"title": None, "suggestedName": "[x] a"}, ["[x]*"]) == "[x]*"
    assert namefilter.thread_match({"title": None, "suggestedName": None}, ["*"]) is None
