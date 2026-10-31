from scripts.capture_page import assess_html, redact_url


def test_redact_url_hides_sensitive_query_values():
    result = redact_url("https://example.test/video?token=secret&id=42")
    assert "secret" not in result
    assert "token=%5BREDACTED%5D" in result
    assert "id=42" in result


def test_assess_html_accepts_meaningful_static_page():
    html = (
        "<html><head><title>Example</title></head><body>"
        "<h1>Example page</h1><p>" + ("Useful page content " * 30) + "</p>"
        "<a href='/one'>One</a><a href='/two'>Two</a>"
        "</body></html>"
    )
    result = assess_html(html, 200)
    assert result["http_html_looks_useful"] is True
    assert result["title"] == "Example"


def test_assess_html_flags_challenge_page():
    html = (
        "<html><head><title>Just a moment</title></head><body>"
        "Just a moment. Verify you are human."
        "</body></html>"
    )
    result = assess_html(html, 403)
    assert result["looks_blocked_or_challenged"] is True
    assert result["http_html_looks_useful"] is False
