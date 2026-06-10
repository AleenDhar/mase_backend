"""Tests for _clean_first_name() in lemlist_mcp_server.py."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lemlist_mcp_server import _clean_first_name


class TestCleanFirstNameWithLastName:
    """When last_name is provided, only strip on a confirmed initial match."""

    def test_confirmed_match_strips(self):
        assert _clean_first_name("VittorioF", "Franzoni") == "Vittorio"

    def test_confirmed_match_strips_carmen(self):
        assert _clean_first_name("CarmenF", "Fernandez") == "Carmen"

    def test_confirmed_match_strips_john(self):
        assert _clean_first_name("JohnD", "Doe") == "John"

    def test_confirmed_match_strips_maria_n(self):
        assert _clean_first_name("MariaN", "Nunez") == "Maria"

    def test_no_match_preserves(self):
        assert _clean_first_name("MariaB", "Santos") == "MariaB"

    def test_no_match_preserves_different_initial(self):
        assert _clean_first_name("LeaS", "Torres") == "LeaS"

    def test_no_match_preserves_john_m(self):
        assert _clean_first_name("JohnM", "Davis") == "JohnM"

    def test_ends_lowercase_not_triggered(self):
        assert _clean_first_name("Carol", "Chen") == "Carol"

    def test_ends_lowercase_jeff(self):
        assert _clean_first_name("Jeff", "Johnson") == "Jeff"

    def test_leann_ends_lowercase_n(self):
        assert _clean_first_name("LeAnn", "Brown") == "LeAnn"

    def test_none_last_name_coerced_to_fallback(self):
        assert _clean_first_name("VittorioF", None) == "Vittorio"


class TestCleanFirstNameFallback:
    """When last_name is absent, use the generic [lower][UPPER] heuristic."""

    def test_fallback_strips_vittorio(self):
        assert _clean_first_name("VittorioF") == "Vittorio"

    def test_fallback_strips_carmen(self):
        assert _clean_first_name("CarmenF") == "Carmen"

    def test_fallback_leann_unchanged(self):
        assert _clean_first_name("LeAnn") == "LeAnn"

    def test_fallback_jeff_unchanged(self):
        assert _clean_first_name("Jeff") == "Jeff"

    def test_fallback_empty_last_name_string(self):
        assert _clean_first_name("VittorioF", "") == "Vittorio"


class TestCleanFirstNameEdgeCases:
    def test_empty_string(self):
        assert _clean_first_name("") == ""

    def test_single_letter(self):
        assert _clean_first_name("F", "Franzoni") == "F"

    def test_single_letter_no_last(self):
        assert _clean_first_name("F") == "F"

    def test_accented_name_unchanged(self):
        assert _clean_first_name("María", "Santos") == "María"

    def test_all_caps_unchanged(self):
        assert _clean_first_name("JOHN", "Doe") == "JOHN"
