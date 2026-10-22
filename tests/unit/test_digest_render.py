from pathlib import Path

import pytest

from papersteward.delivery.contracts import Digest
from papersteward.delivery.render import render_html, render_markdown, safe_url

FIXTURES = Path("tests/fixtures/digest")


@pytest.mark.parametrize("extension,render", [("md", render_markdown), ("html", render_html)])
def test_digest_render_golden(extension, render):
    digest = Digest.model_validate_json((FIXTURES / "basic.json").read_text())
    assert render(digest) == (FIXTURES / f"basic.{extension}").read_text()


def test_untrusted_content_and_unsafe_urls():
    digest = Digest.model_validate_json((FIXTURES / "basic.json").read_text())
    digest.items[0].title = "<script>alert(1)</script>\n# Inject [x](javascript:x)"
    digest.items[0].summary_100 = '</pre><img src=x onerror="bad()">'
    digest.items[0].url = "javascript:alert(1)"
    digest.items[0].pdf_url = "https://host/x\nInjected"
    for render in (render_html, render_markdown):
        result = render(digest)
        assert "<script>" not in result and "<img" not in result
        assert 'href="javascript:' not in result and "[原文](javascript:" not in result
    for url in (
        "//host/path",
        "data:text/html,x",
        "https://u:pw@host",
        "https://:pw@host",
        "https://user:@host",
        "https://[broken",
        "https://x/\x00",
    ):
        assert safe_url(url) is None
    assert safe_url('https://host/x")') == "https://host/x%22%29"
