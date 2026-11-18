import pytest

from tmjdlc.providers.javtiful import JavtifulProvider


@pytest.mark.parametrize(("url", "code"), [
    ("https://javtiful.com/video/ABP-123", "ABP-123"),
    ("https://www.javtiful.com/watch/xyz-987?x=1", "XYZ-987"),
    ("https://sub.javtiful.com/title/ABC_0042", "ABC-0042"),
])
def test_parse_javtiful_url(url, code):
    source = JavtifulProvider().parse(url)
    assert source.code == code
    assert source.url == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "https://example.com/ABP-123",
    "not a url",
    "https://user:pass@javtiful.com/ABP-123",
])
def test_reject_invalid_or_untrusted_url(url):
    with pytest.raises(ValueError):
        JavtifulProvider().parse(url)
