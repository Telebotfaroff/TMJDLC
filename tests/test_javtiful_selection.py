import pytest

from tmjdlc.providers.javtiful.selection import (
    parse_number_selection,
    select_videos,
)


def test_parse_all_and_ranges():
    assert parse_number_selection("all", maximum=6) == [1, 2, 3, 4, 5, 6]
    assert parse_number_selection("1, 3,5-6,3", maximum=6) == [1, 3, 5, 6]


@pytest.mark.parametrize("value", ["", "0", "1-", "4-2", "1,abc", "1-7"])
def test_reject_invalid_selections(value):
    with pytest.raises(ValueError):
        parse_number_selection(value, maximum=6)


def test_select_videos_filters_completed_codes_and_duplicate_ids():
    urls = [
        "https://javtiful.com/video/1001/ABP-123",
        "https://javtiful.com/video/1002/XYZ-987",
        "https://javtiful.com/video/1003/ABC-0042",
        "https://javtiful.com/video/1001/another-slug",
    ]
    result = select_videos(urls, "all", completed_codes={"XYZ-987"})
    assert [item.code for item in result.selected] == ["ABP-123", "ABC-0042"]
    assert [item.index for item in result.skipped_duplicates] == [2, 4]
    assert result.discovered_count == 4


def test_select_specific_positions():
    urls = [
        "https://javtiful.com/video/1001/ABP-123",
        "https://javtiful.com/video/1002/XYZ-987",
        "https://javtiful.com/video/1003/ABC-0042",
    ]
    result = select_videos(urls, "2-3")
    assert [item.index for item in result.selected] == [2, 3]


def test_empty_discovery_is_rejected():
    with pytest.raises(ValueError):
        select_videos([], "all")
