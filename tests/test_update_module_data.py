"""Tests for ormosbot.update_module_data."""

import json
from pathlib import Path

import pytest

from ormosbot.update_module_data import (
    escape_switch_case_value,
    load_stats_file,
    lua_from_mapping,
    parse_expected_total,
    should_update_wiki,
    switch_from_mapping,
)


class TestEscapeSwitchCaseValue:
    """Tests for wiki #switch case escaping."""

    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            ("x=y", "x{{=}}y"),
            ("a|b", "a{{!}}b"),
            ("a|b=c", "a{{!}}b{{=}}c"),
            ("x{{=}}y", "x{{=}}y"),
            ("a{{!}}b", "a{{!}}b"),
            ("a{{!}}b|c x={{=}}y=z", "a{{!}}b{{!}}c x{{=}}{{=}}y{{=}}z"),
            (
                "o:/(target|that|each) (opponent|player) sacrifices/",
                "o:/(target{{!}}that{{!}}each) (opponent{{!}}player) sacrifices/",
            ),
            ("", ""),
            ("hello world", "hello world"),
            ("a||b", "a{{!}}{{!}}b"),
            ("a==b", "a{{=}}{{=}}b"),
        ],
        ids=[
            "raw_equals",
            "raw_pipe",
            "both_equals_and_pipe",
            "already_escaped_equals",
            "already_escaped_pipe",
            "mixed_raw_and_escaped",
            "real_world_regex",
            "empty_string",
            "no_special_chars",
            "consecutive_pipes",
            "consecutive_equals",
        ],
    )
    def test_escape(self, input_value: str, expected: str) -> None:
        """Verify escaping of parser-significant characters."""
        assert escape_switch_case_value(input_value) == expected


class TestLuaFromMapping:
    """Tests for Lua source generation."""

    def test_single_query(self) -> None:
        """Single query produces correct Lua table entry."""
        data = {"t:creature": {"c": "10", "w": "5"}}
        result = lua_from_mapping(data)
        assert "return {" in result
        assert "['t:creature']" in result
        assert "c = 10" in result
        assert "w = 5" in result
        assert "u = 0" in result

    def test_empty_mapping(self) -> None:
        """Empty mapping produces a valid empty Lua table."""
        result = lua_from_mapping({})
        assert result == ("-- Auto-generated data. Edit carefully.\nreturn {\n}\n")


class TestSwitchFromMapping:
    """Tests for wikitext #switch generation."""

    def test_single_query_csv(self) -> None:
        """CSV values include all colors and a correct total."""
        data = {"T:Creature": {"c": "1", "w": "2", "u": "3"}}
        result = switch_from_mapping(data)
        assert "{{#switch:" in result
        assert " | t:creature = " in result
        line = [x for x in result.splitlines() if "t:creature" in x][0]
        csv = line.split("= ", 1)[1]
        values = list(map(int, csv.split(",")))
        assert values[-1] == sum(values[:-1])

    def test_escaping_applied(self) -> None:
        """Special characters in query keys are escaped in switch output."""
        data = {"o:x=y|z": {"c": "1"}}
        result = switch_from_mapping(data)
        assert "o:x{{=}}y{{!}}z" in result

    def test_default_case_present(self) -> None:
        """Output always includes the default fallback case."""
        result = switch_from_mapping({})
        assert " | default = " in result
        assert "}}" in result


class TestLoadStatsFile:
    """Tests for JSON stats file loading."""

    def test_bare_dict(self, tmp_path: Path) -> None:
        """Bare dict format is loaded and values are stringified."""
        p = tmp_path / "stats.json"
        p.write_text(json.dumps({"q1": {"c": 5}}))
        result = load_stats_file(p)
        assert result == {"q1": {"c": "5"}}

    def test_wrapped_dict(self, tmp_path: Path) -> None:
        """Dict wrapped in a 'stats' key is unwrapped."""
        p = tmp_path / "stats.json"
        p.write_text(json.dumps({"stats": {"q1": {"w": 3}}}))
        result = load_stats_file(p)
        assert result == {"q1": {"w": "3"}}

    def test_non_dict_raises(self, tmp_path: Path) -> None:
        """Non-dict top-level JSON raises RuntimeError."""
        p = tmp_path / "stats.json"
        p.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(RuntimeError, match="does not contain a mapping"):
            load_stats_file(p)

    def test_non_mapping_entry_raises(self, tmp_path: Path) -> None:
        """Non-dict value for a query key raises RuntimeError."""
        p = tmp_path / "stats.json"
        p.write_text(json.dumps({"q1": "not_a_dict"}))
        with pytest.raises(RuntimeError, match="non-mapping entry"):
            load_stats_file(p)


class TestShouldUpdateWiki:
    """Tests for environment-based wiki update flag."""

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", " true "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Recognized truthy strings return True."""
        monkeypatch.setenv("UPDATE_WIKI", value)
        assert should_update_wiki() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Non-truthy strings return False."""
        monkeypatch.setenv("UPDATE_WIKI", value)
        assert should_update_wiki() is False

    def test_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env var returns False."""
        monkeypatch.delenv("UPDATE_WIKI", raising=False)
        assert should_update_wiki() is False


class TestParseExpectedTotal:
    """Tests for environment-based expected total parsing."""

    def test_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid integer string is parsed correctly."""
        monkeypatch.setenv("EXPECTED_QUERY_TOTAL", "42")
        assert parse_expected_total() == 42

    def test_with_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Surrounding whitespace is stripped before parsing."""
        monkeypatch.setenv("EXPECTED_QUERY_TOTAL", " 100 ")
        assert parse_expected_total() == 100

    def test_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env var returns None."""
        monkeypatch.delenv("EXPECTED_QUERY_TOTAL", raising=False)
        assert parse_expected_total() is None

    def test_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only env var returns None."""
        monkeypatch.setenv("EXPECTED_QUERY_TOTAL", "  ")
        assert parse_expected_total() is None

    def test_non_integer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-integer string raises RuntimeError."""
        monkeypatch.setenv("EXPECTED_QUERY_TOTAL", "abc")
        with pytest.raises(RuntimeError, match="must be an integer"):
            parse_expected_total()
