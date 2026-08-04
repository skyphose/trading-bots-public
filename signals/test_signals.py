#!/usr/bin/env python3
"""Tests for the pricing and sizing primitives.

These are the pieces where a sign error or an off-by-one in a probability is
expensive and silent: nothing crashes, the bot simply sizes wrong for weeks. The
tests below pin the properties that must hold rather than golden numbers, so they
still mean something after a refactor.
"""
import math
import unittest

import devig
import lmsr
import sizing


class LMSRTests(unittest.TestCase):
    def test_prices_are_a_probability_distribution(self):
        for q in ([0.0, 0.0], [50.0, 10.0], [-30.0, 120.0]):
            for b in (5.0, 50.0, 500.0):
                prices = lmsr.lmsr_prices(q, b)
                self.assertAlmostEqual(
                    sum(prices), 1.0, places=9,
                    msg=f"LMSR prices must sum to 1 (q={q}, b={b}) — a market maker "
                        f"whose prices do not is quoting free money")
                for p in prices:
                    self.assertGreater(p, 0.0)
                    self.assertLess(p, 1.0)

    def test_equal_inventory_is_a_coin_flip(self):
        self.assertAlmostEqual(lmsr.lmsr_prices([0.0, 0.0], 10.0)[0], 0.5, places=9)

    def test_buying_an_outcome_raises_its_price(self):
        before = lmsr.lmsr_prices([0.0, 0.0], 10.0)[0]
        after = lmsr.lmsr_prices([5.0, 0.0], 10.0)[0]
        self.assertGreater(after, before,
                           "buying YES must move YES up; if this inverts, every "
                           "impact estimate has the wrong sign")

    def test_larger_b_means_less_price_impact(self):
        shallow = lmsr.lmsr_prices([5.0, 0.0], 5.0)[0]
        deep = lmsr.lmsr_prices([5.0, 0.0], 500.0)[0]
        self.assertGreater(shallow, deep,
                           "b is the liquidity parameter — a deeper book must absorb "
                           "the same trade with less movement")

    def test_cost_is_monotonic_in_size(self):
        costs = [lmsr.lmsr_cost([n, 0.0], 10.0) for n in (0.0, 1.0, 5.0, 20.0)]
        self.assertEqual(costs, sorted(costs))

    def test_ev_is_estimate_minus_price(self):
        self.assertAlmostEqual(lmsr.compute_ev(0.60, 0.45), 0.15, places=9)
        self.assertAlmostEqual(lmsr.compute_ev(0.30, 0.45), -0.15, places=9)


class DevigTests(unittest.TestCase):
    def test_american_odds_convert_to_probabilities(self):
        self.assertAlmostEqual(devig.american_to_prob(-150), 150 / 250, places=9)
        self.assertAlmostEqual(devig.american_to_prob(+130), 100 / 230, places=9)

    def test_removing_vig_normalises_to_one(self):
        # A real two-way market overrounds: implied probabilities sum to >1, and the
        # excess is the book's margin. Every method must return a true distribution.
        a, b = devig.american_to_prob(-150), devig.american_to_prob(+130)
        self.assertGreater(a + b, 1.0, "fixture should overround, or it tests nothing")
        for method in ("proportional", "power", "shin"):
            p_a, p_b = devig.devig_two_way(a, b, method=method)
            self.assertAlmostEqual(p_a + p_b, 1.0, places=6, msg=f"method={method}")
            self.assertGreater(p_a, 0.0)
            self.assertGreater(p_b, 0.0)

    def test_devig_preserves_the_favourite(self):
        a, b = devig.american_to_prob(-150), devig.american_to_prob(+130)
        for method in ("proportional", "power", "shin"):
            p_a, p_b = devig.devig_two_way(a, b, method=method)
            self.assertGreater(p_a, p_b,
                               f"{method} must not flip which side is favoured")

    def test_a_fair_market_is_left_alone(self):
        for method in ("proportional", "power", "shin"):
            p_a, p_b = devig.devig_two_way(0.5, 0.5, method=method)
            self.assertAlmostEqual(p_a, 0.5, places=6, msg=f"method={method}")


class KellyTests(unittest.TestCase):
    def test_no_edge_means_no_bet(self):
        self.assertEqual(sizing.kelly_fraction_yes(0.50, 0.50), 0.0)

    def test_negative_edge_is_clamped_to_zero(self):
        # Must clamp rather than return a negative fraction: a negative size that
        # reaches an executor is a bet on the other side, not a skipped trade.
        self.assertEqual(sizing.kelly_fraction_yes(0.30, 0.60), 0.0)

    def test_fraction_grows_with_edge(self):
        small = sizing.kelly_fraction_yes(0.55, 0.50)
        large = sizing.kelly_fraction_yes(0.75, 0.50)
        self.assertGreater(large, small)

    def test_matches_the_closed_form(self):
        self.assertAlmostEqual(
            sizing.kelly_fraction_yes(0.60, 0.40), (0.60 - 0.40) / (1 - 0.40), places=9)

    def test_degenerate_prices_do_not_divide_by_zero(self):
        for price in (0.0, 1.0, -0.1, 1.5):
            self.assertEqual(sizing.kelly_fraction_yes(0.9, price), 0.0,
                             f"price={price} must be rejected, not propagated")

    def test_certainty_sizes_at_full_bankroll_fraction(self):
        self.assertAlmostEqual(sizing.kelly_fraction_yes(1.0, 0.40), 1.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
