import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).with_name("feedback_manager.py")
SPEC = importlib.util.spec_from_file_location("feedback_manager_module", MODULE_PATH)
feedback_manager = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(feedback_manager)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b'{"ok":true}'


class FeedbackPrivacyTests(unittest.TestCase):
    def test_feedback_payload_does_not_send_local_hostname(self):
        captured = {}

        def fake_urlopen(request, **_kwargs):
            captured.update(json.loads(request.data.decode("utf-8")))
            return _Response()

        with (
            patch.object(
                feedback_manager,
                "current_package_info",
                return_value={"feedback_endpoint": "https://example.com/feedback"},
            ),
            patch.object(feedback_manager, "urlopen", side_effect=fake_urlopen),
        ):
            result = feedback_manager.send_feedback("/tmp/app", message="test")

        self.assertTrue(result["ok"])
        self.assertNotIn("host", captured)


if __name__ == "__main__":
    unittest.main()
