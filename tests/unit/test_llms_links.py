"""Every same-site link in llms.txt and llms-full.txt must resolve to a file in web/."""

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
HOST = "atlas.yashdynamo.fun"
URL = re.compile(r"https://" + re.escape(HOST) + r"[^\s)>\]`'\"]*")


def _links(name: str) -> list[str]:
    return sorted({u.rstrip(".,;:") for u in URL.findall((WEB / name).read_text(encoding="utf-8"))})


@pytest.mark.parametrize("name", ["llms.txt", "llms-full.txt"])
def test_same_site_links_resolve(name: str) -> None:
    links = _links(name)
    assert links
    for link in links:
        parts = urlparse(link)
        path = parts.path
        if path.startswith(("/mcp", "/v1")):
            continue  # API endpoints, not files
        target = WEB / ("index.html" if path in ("", "/") else path.lstrip("/"))
        assert target.is_file(), f"{link} has no file {target.name}"
        if parts.fragment and target.name == "docs.html":
            html = target.read_text(encoding="utf-8")
            assert f'id="{parts.fragment}"' in html, f"{link}: no anchor"
