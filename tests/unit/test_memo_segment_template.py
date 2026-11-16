"""The answer memo templates a subject that is part of a path segment (Wikipedia list pages)."""

from atlas.core.memo import answer_template, bind

URL = "https://en.wikipedia.org/wiki/List_of_awards_and_nominations_received_by_Hrithik_Roshan"


def test_value_inside_a_segment_becomes_a_slot():
    template = answer_template(URL, {"person": "Hrithik Roshan"})
    assert template == "https://en.wikipedia.org/wiki/List_of_awards_and_nominations_received_by_{person}"
    assert bind(template, {"person": "Shah Rukh Khan"}) == (
        "https://en.wikipedia.org/wiki/List_of_awards_and_nominations_received_by_Shah_Rukh_Khan"
    )


def test_whole_segment_still_works():
    url = "https://en.wikipedia.org/wiki/Kajol"
    assert answer_template(url, {"topic": "Kajol"}) == "https://en.wikipedia.org/wiki/{topic}"


def test_partial_word_does_not_match():
    url = "https://shop.test/items/lamps_and_lampshades"
    assert answer_template(url, {"q": "lamp"}) is None


def test_short_single_word_is_not_templated_inside_a_segment():
    url = "https://shop.test/items/red_box"
    assert answer_template(url, {"c": "red"}) is None
