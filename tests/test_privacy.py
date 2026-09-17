import re
import unittest
from pathlib import Path

try:
    from papercuts.privacy import PrivacyError, validate_caller_field
except ModuleNotFoundError:
    PrivacyError = None
    validate_caller_field = None


HOME = str(Path.home().resolve())
SYNTHETIC_HOME = "/" + "home/alice/x"


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        if validate_caller_field is None:
            self.fail("privacy implementation missing")

    def assert_rejected(self, field, value, rule):
        with self.assertRaises(PrivacyError) as raised:
            validate_caller_field(value, field)
        error = raised.exception
        self.assertEqual(error.rule_id, rule)
        self.assertEqual(error.field, field)
        diagnostic = str(error)
        self.assertIn(rule, diagnostic)
        self.assertIn(field, diagnostic)
        if value:
            self.assertNotIn(repr(value), diagnostic)
            self.assertNotIn(str(value), diagnostic)
        self.assertNotIn("hash", diagnostic.lower())
        self.assertNotIn("preview", diagnostic.lower())
        self.assertNotIn("length", diagnostic.lower())
        self.assertNotIn("path=", diagnostic.lower())
        self.assertNotIn(HOME, diagnostic)

    def test_accepted_scalar_is_returned_unchanged(self):
        value = "keep this exact text"
        self.assertIs(validate_caller_field(value, "summary"), value)
        self.assertIs(validate_caller_field("same", "expected"), validate_caller_field("same", "expected"))
        self.assertIsNone(validate_caller_field(None, "recurrence_key"))

    def test_scalar_type_utf8_categories_bounds_and_outer_whitespace(self):
        self.assert_rejected("summary", 7, "E_PRIVACY_TYPE")
        self.assert_rejected("summary", "\ud800", "E_PRIVACY_UTF8")
        for value in ("a\x00b", "a\u200db", "a\u2028b", "a\u2029b"):
            self.assert_rejected("summary", value, "E_PRIVACY_CATEGORY")
        self.assert_rejected("summary", "", "E_PRIVACY_BOUNDS")
        self.assert_rejected("summary", "x" * 121, "E_PRIVACY_BOUNDS")
        self.assert_rejected("expected", "x" * 501, "E_PRIVACY_BOUNDS")
        self.assert_rejected("observed", " x", "E_PRIVACY_WHITESPACE")
        self.assert_rejected("resolution", "x ", "E_PRIVACY_WHITESPACE")
        self.assert_rejected("summary", "\u00a0x", "E_PRIVACY_WHITESPACE")

    def test_recurrence_key_contract(self):
        self.assertEqual(validate_caller_field("a0._-z", "recurrence_key"), "a0._-z")
        self.assertIsNone(validate_caller_field(None, "recurrence_key"))
        self.assert_rejected("recurrence_key", 1, "E_PRIVACY_TYPE")
        self.assert_rejected("recurrence_key", "", "E_PRIVACY_RECURRENCE_KEY")
        self.assert_rejected("recurrence_key", "Bad-Key", "E_PRIVACY_RECURRENCE_KEY")
        self.assert_rejected("recurrence_key", "é", "E_PRIVACY_RECURRENCE_KEY")
        self.assert_rejected("recurrence_key", "a" * 81, "E_PRIVACY_BOUNDS")

    def test_invalid_field_name_is_not_disclosed(self):
        invalid_field = "field=attacker\nsecret"
        with self.assertRaises(PrivacyError) as raised:
            validate_caller_field("value", invalid_field)
        error = raised.exception
        self.assertIsNone(error.field)
        self.assertNotIn(invalid_field, str(error))
        self.assertEqual(str(error), "papercuts: E_PRIVACY_FIELD")

    def test_home_path_and_prefix_boundaries(self):
        self.assert_rejected("observed", HOME + "/x", "E_PRIVACY_HOME_PATH")
        self.assert_rejected("observed", SYNTHETIC_HOME, "E_PRIVACY_HOME_PATH")
        self.assert_rejected("observed", "label " + SYNTHETIC_HOME, "E_PRIVACY_HOME_PATH")
        self.assert_rejected("observed", "(" + SYNTHETIC_HOME, "E_PRIVACY_HOME_PATH")
        for near in ("/homes/alice/x", "label" + SYNTHETIC_HOME):
            self.assertEqual(validate_caller_field(near, "observed"), near)

    def test_drive_path_boundaries(self):
        self.assert_rejected("expected", r"C:\x", "E_PRIVACY_DRIVE_PATH")
        self.assert_rejected("expected", "C:/x", "E_PRIVACY_DRIVE_PATH")
        self.assert_rejected("expected", r"label C:\x", "E_PRIVACY_DRIVE_PATH")
        self.assertEqual(validate_caller_field("C:relative", "expected"), "C:relative")
        self.assertEqual(validate_caller_field("labelC:\\x", "expected"), "labelC:\\x")

    def test_unc_path_boundaries(self):
        self.assert_rejected("observed", r"\\server\share", "E_PRIVACY_UNC_PATH")
        self.assert_rejected("observed", "//server/share", "E_PRIVACY_UNC_PATH")
        self.assertEqual(validate_caller_field(r"\server\share", "observed"), r"\server\share")

    def test_uri_userinfo_is_selected_and_uri_like_safe_text_is_accepted(self):
        self.assert_rejected("expected", "https://u:p@example.test/x", "E_PRIVACY_URI_USERINFO")
        self.assert_rejected("expected", "HTTPS://u:p@example.test/x", "E_PRIVACY_URI_USERINFO")
        accepted = "https://example.test/@u"
        self.assertEqual(validate_caller_field(accepted, "expected"), accepted)
        self.assert_rejected("expected", "https://u:p@example.test/\\\\server\\share", "E_PRIVACY_URI_USERINFO")

    def test_pem_header(self):
        for kind in ("", "RSA ", "EC ", "OPENSSH "):
            with self.subTest(kind=kind):
                header = "-----" + "BEGIN " + kind + "PRIVATE KEY" + "-----"
                self.assert_rejected("resolution", header, "E_PRIVACY_PEM_HEADER")
        near = "-----" + "BEGIN PRIVATE KEYS" + "-----"
        self.assertEqual(validate_caller_field(near, "resolution"), near)

    def test_github_token_boundaries(self):
        token = "ghp_" + "A" * 20
        self.assert_rejected("observed", token, "E_PRIVACY_GITHUB_TOKEN")
        self.assert_rejected("observed", "label " + token, "E_PRIVACY_GITHUB_TOKEN")
        self.assertEqual(validate_caller_field("x" + token, "observed"), "x" + token)
        self.assertEqual(validate_caller_field(token[:-1], "observed"), token[:-1])

    def test_sk_token_boundaries(self):
        token = "sk-" + "a" * 20
        self.assert_rejected("observed", token, "E_PRIVACY_SK_TOKEN")
        self.assertEqual(validate_caller_field("x" + token, "observed"), "x" + token)
        self.assertEqual(validate_caller_field(token[:-1], "observed"), token[:-1])

    def test_aws_key_boundaries(self):
        token = "AKIA" + "A" * 16
        self.assert_rejected("observed", token, "E_PRIVACY_AWS_KEY")
        self.assertEqual(validate_caller_field("X" + token, "observed"), "X" + token)
        self.assertEqual(validate_caller_field(token[:-1], "observed"), token[:-1])

    def test_slack_token_boundaries(self):
        token = "xoxb-" + "a" * 10
        self.assert_rejected("observed", token, "E_PRIVACY_SLACK_TOKEN")
        self.assertEqual(validate_caller_field("X" + token, "observed"), "X" + token)
        self.assertEqual(validate_caller_field(token[:-1], "observed"), token[:-1])

    def test_label_adjacent_paths_need_ascii_whitespace_or_listed_opener(self):
        import papercuts.privacy as privacy

        for prefix in (" ", "\t", '"', "'", "`", "(", "<", "[", "{", "="):
            value = "a" + prefix + SYNTHETIC_HOME
            self.assertEqual(privacy._choose_sensitive_rule(value), "HOME_PATH")
        for prefix in ("x", ")", "]", ":"):
            value = prefix + SYNTHETIC_HOME
            self.assertIsNone(privacy._choose_sensitive_rule(value))

    def test_sensitive_rule_tie_order_is_explicit(self):
        import papercuts.privacy as privacy

        self.assertEqual(
            [name for name, _ in privacy._SENSITIVE_RULES],
            [
                "URI_USERINFO",
                "PEM_HEADER",
                "GITHUB_TOKEN",
                "SK_TOKEN",
                "AWS_KEY",
                "SLACK_TOKEN",
                "HOME_PATH",
                "DRIVE_PATH",
                "UNC_PATH",
            ],
        )
        first = re.compile(r"(?P<value>x)")
        second = re.compile(r"(?P<value>x)")
        self.assertEqual(privacy._choose_sensitive_rule("x", (("FIRST", first), ("SECOND", second))), "FIRST")


if __name__ == "__main__":
    unittest.main()
