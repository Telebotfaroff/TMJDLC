from pathlib import Path


def test_core_packages_exist():
    root = Path(__file__).parents[1] / "tmjdlc"
    expected = {"bot", "providers", "downloader", "drive", "queue", "storage", "workers", "utils"}
    assert expected.issubset({path.name for path in root.iterdir() if path.is_dir()})
