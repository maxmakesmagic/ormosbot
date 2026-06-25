"""Tests for ormosbot.wiki_stats."""

from ormosbot.update_module_data import switch_from_mapping
from ormosbot.wiki_stats import (
    parse_switch_template,
    unescape_switch_case_value,
)


class TestUnescapeSwitchCaseValue:
    """Tests for reversing #switch case escaping."""

    def test_equals(self) -> None:
        """Escaped equals is restored."""
        assert unescape_switch_case_value("x{{=}}y") == "x=y"

    def test_pipe(self) -> None:
        """Escaped pipe is restored."""
        assert unescape_switch_case_value("a{{!}}b") == "a|b"

    def test_both(self) -> None:
        """Escaped pipe and equals are both restored."""
        assert unescape_switch_case_value("a{{!}}b{{=}}c") == "a|b=c"

    def test_plain(self) -> None:
        """Plain text is unchanged."""
        assert unescape_switch_case_value("hello world") == "hello world"


class TestParseSwitchTemplate:
    """Tests for parsing the #switch stats template back into a mapping."""

    def test_single_query(self) -> None:
        """A single switch case parses into per-color counts."""
        text = (
            "<noinclude>{{Documentation}}</noinclude>\n"
            "{{#switch:{{lc:{{{query|}}}}}\n"
            " | t:creature = 1,2,3,4,5,6,7,28\n"
            " | default = \n"
            "}}"
        )
        result = parse_switch_template(text)
        assert result == {
            "t:creature": {
                "c": "1",
                "w": "2",
                "u": "3",
                "b": "4",
                "r": "5",
                "g": "6",
                "m": "7",
            }
        }

    def test_ignores_default_and_header(self) -> None:
        """The default case and switch header are not treated as queries."""
        text = (
            "{{#switch:{{lc:{{{query|}}}}}\n"
            " | a:b = 1,0,0,0,0,0,0,1\n"
            " | default = \n"
            "}}"
        )
        result = parse_switch_template(text)
        assert list(result) == ["a:b"]

    def test_unescapes_keys(self) -> None:
        """Escaped characters in query keys are unescaped."""
        text = " | o:x{{=}}y{{!}}z = 1,0,0,0,0,0,0,1\n"
        result = parse_switch_template(text)
        assert "o:x=y|z" in result

    def test_skips_malformed(self) -> None:
        """Lines with too few values are skipped."""
        text = " | broken = 1,2\n | good = 1,2,3,4,5,6,7,28\n"
        result = parse_switch_template(text)
        assert list(result) == ["good"]

    def test_roundtrip(self) -> None:
        """Output of switch_from_mapping parses back to the colors used."""
        data = {
            "art:Kavu include:extras": {
                "c": "3",
                "w": "0",
                "u": "0",
                "b": "0",
                "r": "5",
                "g": "9",
                "m": "1",
            }
        }
        text = switch_from_mapping(data)
        result = parse_switch_template(text)
        assert result == {"art:kavu include:extras": data["art:Kavu include:extras"]}
