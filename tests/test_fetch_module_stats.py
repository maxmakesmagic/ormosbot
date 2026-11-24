"""Tests for recording Scryfall lookups in the raw shard files."""

import json
import sys
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
from requests import Response
from requests.exceptions import Timeout
from tenacity import Retrying

from ormosbot import fetch_module_stats as fetch
from ormosbot.colors import COLOR_ORDER


def _response(status: int, data: object) -> Response:
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    return response


@pytest.fixture
def request_scryfall(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Mock Scryfall responses and remove the wait between test retries."""
    request = Mock()
    monkeypatch.setattr(fetch, "_request_scryfall", request)
    monkeypatch.setattr(fetch, "Retrying", partial(Retrying, sleep=lambda _: None))
    return request


@pytest.mark.parametrize(
    ("status", "data", "expected"),
    [
        (200, {"total_cards": 12}, 12),
        (200, {"total_cards": 0}, 0),
        (404, {"object": "error", "code": "not_found"}, 0),
        (404, {"message": "not found"}, None),
        (429, {"object": "error", "code": "too_many_requests"}, None),
    ],
)
def test_raw_distinguishes_confirmed_counts_from_failures(
    request_scryfall: Mock, status: int, data: object, expected: int | None
) -> None:
    """Only a valid count or a confirmed no-match response supplies a count."""
    request_scryfall.return_value = _response(status, data)

    assert fetch._fetch_raw_count(Mock(), "t:cat", "w") == expected
    request_scryfall.assert_called_once()


def test_raw_retries_an_invalid_success_without_reusing_it(
    request_scryfall: Mock,
) -> None:
    """Bypass cached responses when retrying invalid successful results."""
    request_scryfall.side_effect = [
        _response(200, {"data": []}),
        _response(200, {"total_cards": True}),
        _response(200, {"total_cards": 15}),
    ]

    assert fetch._fetch_raw_count(Mock(), "t:cat", "w") == 15
    assert [
        call.kwargs["force_refresh"] for call in request_scryfall.call_args_list
    ] == [False, True, True]


@pytest.mark.parametrize("failure", [Timeout("timed out"), _response(503, {})])
def test_raw_retries_transient_failures_three_times(
    request_scryfall: Mock, failure: Exception | Response
) -> None:
    """Return no count when a transport or server error persists."""
    if isinstance(failure, Exception):
        request_scryfall.side_effect = failure
    else:
        request_scryfall.return_value = failure

    assert fetch._fetch_raw_count(Mock(), "t:cat", "w") is None
    assert request_scryfall.call_count == 3


def test_raw_retries_display_options_without_brackets(request_scryfall: Mock) -> None:
    """Keep the unbracketed query if the retry also needs a retry."""
    request_scryfall.side_effect = [
        _response(400, {"details": "Display options must come after the search terms"}),
        _response(503, {}),
        _response(200, {"total_cards": 12}),
    ]

    assert fetch._fetch_raw_count(Mock(), "t:cat unique:prints", "w") == 12
    assert [call.args[1] for call in request_scryfall.call_args_list] == [
        "(t:cat unique:prints) id=w",
        "t:cat unique:prints id=w",
        "t:cat unique:prints id=w",
    ]


def test_raw_cli_writes_integer_and_null_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_scryfall: Mock
) -> None:
    """Keep the shard format and skip wiki reads when gathering raw counts."""
    queries = tmp_path / "queries.json"
    output = tmp_path / "stats.json"
    queries.write_text('["t:cat"]', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MATRIX_CHUNK_INDEX", "0")
    monkeypatch.setenv("MATRIX_QUERIES_FILE", str(queries))
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch-module-stats", "--raw-results", "--output-file", str(output)],
    )
    monkeypatch.setattr(fetch, "get_session", Mock())
    legacy_read = Mock()
    monkeypatch.setattr(fetch, "load_existing_stats", legacy_read)
    request_scryfall.side_effect = [
        _response(429, {}),
        *[_response(200, {"total_cards": 1})] * (len(COLOR_ORDER) - 1),
    ]

    fetch.main()

    legacy_read.assert_not_called()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result == {
        "chunk_index": 0,
        "query_count": 1,
        "stats": {
            "t:cat": {color: None if color == "c" else 1 for color in COLOR_ORDER}
        },
    }
