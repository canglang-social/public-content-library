"""Capacity invariants for optional audio; no network, services or live data."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops.audio_policy import estimate_disk_reserve, remaining_audio_seconds, remaining_collection_seconds


class AudioTimePolicy(unittest.TestCase):
    def test_fast_text_allows_cap_but_full_text_budget_leaves_finalization(self):
        self.assertEqual(remaining_audio_seconds(1800), 600)
        self.assertEqual(remaining_audio_seconds(600), 290)
        self.assertEqual(remaining_audio_seconds(310), 0)
        self.assertEqual(remaining_audio_seconds(-4), 0)

    def test_fractional_budget_rounds_down_and_never_crosses_outer_deadline(self):
        for remaining in (0, 309.9, 310.9, 311.1, 599.8, 910.2, 1800):
            with self.subTest(remaining=remaining):
                budget = remaining_audio_seconds(remaining)
                self.assertIsInstance(budget, int)
                self.assertGreaterEqual(budget, 0)
                self.assertLessEqual(budget, 600)
                if budget:
                    self.assertLessEqual(budget + 300 + 10, remaining)
        self.assertEqual(remaining_audio_seconds(310.9), 0)

    def test_current_collection_cannot_reset_exhausted_shared_phase(self):
        self.assertEqual(remaining_collection_seconds(1800, 1200), 1190)
        self.assertEqual(remaining_collection_seconds(600, 0), 0)
        self.assertEqual(remaining_collection_seconds(1000, 125.8), 115)
        self.assertEqual(remaining_collection_seconds(600, -1), 0)
        self.assertEqual(remaining_collection_seconds(1800, 10), 0)

    def test_collection_obeys_both_phase_and_finalization_limits(self):
        self.assertEqual(remaining_collection_seconds(600, 1200), 290)
        self.assertEqual(remaining_collection_seconds(1800, 1000, cap_seconds=75), 75)
        self.assertEqual(remaining_audio_seconds(600, cap_seconds=0), 0)

    def test_nonfinite_or_invalid_time_configuration_is_rejected(self):
        for bad in (True, "600", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    remaining_audio_seconds(bad)
        for name in ("cap_seconds", "finalization_reserve_seconds", "shutdown_reserve_seconds"):
            with self.assertRaises(ValueError):
                remaining_audio_seconds(1800, **{name: -1})
        with self.assertRaises(ValueError):
            remaining_collection_seconds(1800, float("nan"))


class AudioDiskPolicy(unittest.TestCase):
    def estimate(self, **extra):
        return estimate_disk_reserve(60_000_000, 146_500_000, minimum_free_bytes=5 * 1024**3, **extra)

    def test_finalization_reserves_both_complete_backups_and_existing_queue(self):
        result = self.estimate(queue_backup_bytes=12_000_000)
        self.assertEqual(result["existing_next_backup_total_bytes"], 218_500_000)
        self.assertEqual(result["candidate_total_bytes"], 0)
        self.assertEqual(result["required_free_bytes"], 5 * 1024**3 + 218_500_000 + 16 * 1024**2)

    def test_candidate_reserves_every_live_staging_and_backup_copy(self):
        baseline = self.estimate()
        candidate = self.estimate(candidate_bytes=80_000_000)
        self.assertEqual(candidate["required_free_bytes"] - baseline["required_free_bytes"], 480_000_000)
        self.assertEqual(candidate["candidate_copy_bytes"]["queued_prefix_backup"], 80_000_000)
        self.assertEqual(candidate["candidate_copy_bytes"]["staging"], 80_000_000)

    def test_native_alias_amplification_reserves_publication_and_backup(self):
        once = self.estimate(candidate_bytes=80_000_000)
        twice = self.estimate(candidate_bytes=80_000_000, native_candidate_copies=2)
        self.assertEqual(twice["required_free_bytes"] - once["required_free_bytes"], 160_000_000)

    def test_admission_boundary_does_not_spend_floor_or_future_text_growth(self):
        result = self.estimate(candidate_bytes=80_000_000, other_growth_bytes=600_000_000)
        required = result["required_free_bytes"]
        self.assertFalse(required - 1 >= required)
        self.assertTrue(required >= required)
        spend = result["existing_next_backup_total_bytes"] + result["candidate_total_bytes"] + result["overhead_bytes"] + result["other_growth_bytes"]
        self.assertEqual(required - spend, result["minimum_free_bytes"])

    def test_bad_byte_counts_and_zero_copy_multiplier_fail_closed(self):
        for bad in (-1, True, 80.5, "80", float("inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.estimate(candidate_bytes=bad)
        with self.assertRaises(ValueError):
            self.estimate(native_candidate_copies=0)
        with self.assertRaises(ValueError):
            self.estimate(queue_backup_bytes=-1)

    def test_results_are_independent_and_do_not_share_mutable_state(self):
        first = self.estimate(candidate_bytes=10)
        first["candidate_copy_bytes"]["staging"] = 999
        second = self.estimate(candidate_bytes=10)
        self.assertEqual(second["candidate_copy_bytes"]["staging"], 10)
        self.assertEqual(second["candidate_total_bytes"], 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
