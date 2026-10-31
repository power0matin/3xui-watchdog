"""Configuration path and persistent state defaults."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xui_watchdog.config import load_config  # noqa: E402


class TestStateFileConfig(unittest.TestCase):
    def test_relative_state_file_is_resolved_next_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                'state_file: "runtime/watchdog.json"\n',
                encoding="utf-8",
            )

            cfg = load_config(config_path)

            self.assertEqual(
                cfg.state_file,
                Path(temp_dir) / "runtime" / "watchdog.json",
            )

    def test_default_state_file_is_next_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("poll_interval_seconds: 10\n", encoding="utf-8")

            cfg = load_config(config_path)

            self.assertEqual(cfg.state_file, Path(temp_dir) / "state.json")

    def test_absolute_state_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(f'state_file: "{state_path}"\n', encoding="utf-8")

            cfg = load_config(config_path)

            self.assertEqual(cfg.state_file, state_path)


if __name__ == "__main__":
    unittest.main()
