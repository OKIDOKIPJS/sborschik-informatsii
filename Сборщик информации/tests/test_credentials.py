from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.credentials import get_max_credentials


class CredentialResolutionTests(unittest.TestCase):
    def test_environment_credentials_take_precedence(self) -> None:
        with patch.dict(os.environ, {
            "MAX_BOT_TOKEN": "env-token",
            "MAX_CHANNEL_ID": "env-channel",
        }, clear=False), patch("app.credentials._read_stored_credentials") as read_stored:
            self.assertEqual(get_max_credentials(), ("env-token", "env-channel"))
            read_stored.assert_not_called()

    def test_missing_environment_values_fall_back_to_windows_store(self) -> None:
        with patch.dict(os.environ, {
            "MAX_BOT_TOKEN": "",
            "MAX_CHANNEL_ID": "",
        }, clear=False), patch("app.credentials._read_stored_credentials", return_value={
            "token": "stored-token",
            "channel_id": "stored-channel",
        }):
            self.assertEqual(get_max_credentials(), ("stored-token", "stored-channel"))

    def test_environment_values_override_individual_stored_values(self) -> None:
        with patch.dict(os.environ, {
            "MAX_BOT_TOKEN": "env-token",
            "MAX_CHANNEL_ID": "",
        }, clear=False), patch("app.credentials._read_stored_credentials", return_value={
            "token": "stored-token",
            "channel_id": "stored-channel",
        }):
            self.assertEqual(get_max_credentials(), ("env-token", "stored-channel"))


if __name__ == "__main__":
    unittest.main()
