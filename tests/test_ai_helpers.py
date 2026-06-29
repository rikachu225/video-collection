import pytest
from ai_agent import parse_time_to_seconds, resolve_refs


@pytest.mark.parametrize("value,expected", [
    ("1:20", 80),
    ("0:05", 5),
    ("1:02:03", 3723),
    ("80", 80),
    (80, 80),
    (80.5, 80.5),
    ("1m20s", 80),
    ("90s", 90),
    ("2m", 120),
    ("", None),
    (None, None),
    ("abc", None),
    ("-5", None),
])
def test_parse_time_to_seconds(value, expected):
    assert parse_time_to_seconds(value) == expected


CLIPS = [
    {"name": "Pink Sunset", "path": "Fallen/a.mp4"},
    {"name": "Blue Dawn", "path": "Fallen/b.mp4"},
    {"name": "Pink Morning", "path": "Favorites/c.mp4"},
]


def test_resolve_by_index_1based():
    assert resolve_refs(3, CLIPS) == [(2, CLIPS[2])]
    assert resolve_refs("1", CLIPS) == [(0, CLIPS[0])]


def test_resolve_index_out_of_range():
    assert resolve_refs(9, CLIPS) == []


def test_resolve_all():
    assert resolve_refs("all", CLIPS) == list(enumerate(CLIPS))
    assert resolve_refs("everything", CLIPS) == list(enumerate(CLIPS))


def test_resolve_by_name_substring_case_insensitive():
    matches = resolve_refs("pink", CLIPS)
    assert [i for i, _ in matches] == [0, 2]


def test_resolve_no_match():
    assert resolve_refs("nonexistent", CLIPS) == []


def test_resolve_empty_list():
    assert resolve_refs("all", []) == []
