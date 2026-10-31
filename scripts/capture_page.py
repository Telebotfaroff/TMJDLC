from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "key",
    "api_key",
    "apikey",
    "signature",
    "sig",
    "expires",
    "policy",
    "jwt",
    "session",
}


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        safe_value = "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else value
        query.append((key, safe_value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def assess_html(html: str, status_code: int) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body or soup
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    scripts = len(soup.find_all("script"))
    links = len(soup.find_all("a", href=True))
    meaningful = len(text) >= 200 and (len(text) >= scripts * 20 or scripts == 0)
    blocked_terms = ("just a moment", "access denied", "verify you are human")
    blocked = any(term in text.lower() for term in blocked_terms)
    ok_status = 200 <= status_code < 400
    useful = ok_status and len(html) >= 500 and meaningful and not blocked
    return {
        "status_code": status_code,
        "title": title,
        "visible_text_characters": len(text),
        "script_count": scripts,
        "link_count": links,
        "looks_blocked_or_challenged": blocked,
        "http_html_looks_useful": useful,
    }


def capture_http(url: str, output: Path, timeout: int) -> tuple[str, dict]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    response = session.get(url, timeout=timeout, allow_redirects=True)
    html = response.text
    (output / "http-response.html").write_text(html, encoding="utf-8")
    assessment = assess_html(html, response.status_code)
    report = {
        "requested_url": redact_url(url),
        "final_url": redact_url(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "content_length": len(response.content),
        "redirect_count": len(response.history),
        "redirects": [redact_url(item.url) for item in response.history],
        "assessment": assessment,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output / "http-report.json", report)
    return html, assessment


def capture_playwright(url: str, output: Path, timeout: int) -> None:
    from playwright.sync_api import sync_playwright

    network_events: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        def on_request(request) -> None:
            network_events.append(
                {
                    "event": "request",
                    "url": redact_url(request.url),
                    "method": request.method,
                    "resource_type": request.resource_type,
                }
            )

        def on_response(response) -> None:
            try:
                content_type = response.headers.get("content-type")
            except Exception:
                content_type = None
            network_events.append(
                {
                    "event": "response",
                    "url": redact_url(response.url),
                    "status": response.status,
                    "content_type": content_type,
                }
            )

        page.on("request", on_request)
        page.on("response", on_response)

        navigation_error = None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            html = page.content()
            (output / "rendered-page.html").write_text(html, encoding="utf-8")
            write_json(
                output / "playwright-report.json",
                {
                    "requested_url": redact_url(url),
                    "final_url": redact_url(page.url),
                    "title": page.title(),
                    "assessment": assess_html(html, 200),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            navigation_error = f"{type(exc).__name__}: {exc}"
            write_json(
                output / "playwright-report.json",
                {
                    "requested_url": redact_url(url),
                    "navigation_error": navigation_error,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            write_json(output / "network-events.json", {"events": network_events})
            context.close()
            browser.close()

        if navigation_error:
            print(f"Playwright navigation issue: {navigation_error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a page using HTTP first.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", default="artifacts/page-capture")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    parts = urlsplit(args.url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        parser.error("--url must be an absolute HTTP or HTTPS URL")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    try:
        _, assessment = capture_http(args.url, output, args.timeout)
    except requests.RequestException as exc:
        assessment = {"http_error": f"{type(exc).__name__}: {exc}", "http_html_looks_useful": False}
        write_json(output / "http-report.json", assessment)

    if assessment.get("http_html_looks_useful"):
        print("HTTP extraction returned a usable-looking HTML document; Playwright skipped.")
    else:
        print("HTTP extraction looks incomplete or failed; falling back to Playwright.")
        capture_playwright(args.url, output, args.timeout)

    write_json(
        output / "capture-summary.json",
        {
            "requested_url": redact_url(args.url),
            "http_assessment": assessment,
            "playwright_used": not assessment.get("http_html_looks_useful", False),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
