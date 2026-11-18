"""Javtiful URL parsing, channel scanning and selection helpers."""

from .channel import ChannelInfo, JavtifulChannelScraper, normalize_channel_url, page_url, parse_listing
from .client import JavtifulProvider
from .provider import JavtifulURLParser, VideoSource
from .selection import VideoCandidate, VideoSelection, parse_number_selection, select_videos
from .stream_detector import JavtifulStreamDetector, StreamDetectionError

__all__ = [
    "ChannelInfo",
    "JavtifulChannelScraper",
    "JavtifulProvider",
    "JavtifulURLParser",
    "JavtifulStreamDetector",
    "StreamDetectionError",
    "VideoCandidate",
    "VideoSelection",
    "VideoSource",
    "normalize_channel_url",
    "page_url",
    "parse_listing",
    "parse_number_selection",
    "select_videos",
]
