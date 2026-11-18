from tmjdlc.providers.javtiful.provider import JavtifulURLParser


def test_parser_extracts_code_from_video_slug():
    source = JavtifulURLParser().parse(
        "https://javtiful.com/video/116000/sdde-774"
    )
    assert source.code == "SDDE-774"
    assert source.provider == "javtiful"


def test_parser_rejects_non_javtiful_host():
    import pytest

    with pytest.raises(ValueError, match="not on the Javtiful domain"):
        JavtifulURLParser().parse("https://example.com/video/sdde-774")
