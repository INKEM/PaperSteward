"""人工审核入口已移除，历史表仍保留。"""

import pytest

from papersteward.cli.main import main


@pytest.mark.parametrize(
    "path",
    ["/review", "/review/p/approve", "/review/p/relabel", "/ui/review", "/ui/review/approve"],
)
def test_review_routes_are_removed(client, path):
    response = (
        client.post(path, json={}) if path.endswith(("approve", "relabel")) else client.get(path)
    )
    assert response.status_code == 404
    assert "/review" not in client.get("/openapi.json").json()["paths"]


def test_review_cli_is_removed():
    with pytest.raises(SystemExit) as exc:
        main(["review", "list"])
    assert exc.value.code == 2
