from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


_CODE_PATTERN = re.compile(r"(?i)\b([a-z]{2,8})[-_ ]?(\d{2,6})\b")


@dataclass(frozen=True, slots=True)
class VideoSource:
    url: str
    code: str | None
    provider: str = "javtiful"


class JavtifulURLParser:
    """Validate a Javtiful post URL and extract its catalogue code."""

    HOSTS = {"javtiful.com", "www.javtiful.com"}

    def parse(self, url: str) -> VideoSource:
        value = url.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provide a valid http(s) Javtiful URL")
        host = parsed.hostname.lower().rstrip(".")
        if host not in self.HOSTS and not host.endswith(".javtiful.com"):
            raise ValueError("URL is not on the Javtiful domain")
        if parsed.username or parsed.password:
            raise ValueError("URLs containing embedded credentials are not supported")
        code = self.extract_code(parsed.path) or self.extract_code(parsed.query)
        return VideoSource(url=value, code=code)

    @staticmethod
    def extract_code(value: str) -> str | None:
        match = _CODE_PATTERN.search(value)
        if match:
            return f"{match.group(1).upper()}-{match.group(2)}"
        return None
