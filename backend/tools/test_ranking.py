"""Offline checks for rank_trails: python -m unittest backend.tools.test_ranking"""

import random
import unittest

from backend.tools.ranking import WEIGHTS, rank_trails

CENTER = (42.2411, -83.6130)


def trail(osm_id, km, loop=True, offset=0.0, gain=None):
    """A trail whose trailhead is `offset` degrees north of the center; loop = ends where it starts."""
    start = [CENTER[0] + offset, CENTER[1]]
    end = start if loop else [start[0] + 0.02, start[1]]
    return {"name": f"T{osm_id}", "osm_id": osm_id, "distance_km": km, "coords": [start, [start[0] + 0.01, start[1]], end],
            "elevation_gain_m": gain}


def ranked_ids(trails, constraints):
    return [t["osm_id"] for t in rank_trails(trails, constraints, *CENTER)]


class RankTrailsTests(unittest.TestCase):
    def test_weights_let_distance_dominate(self):
        self.assertEqual(sum(WEIGHTS.values()), 1.0)
        self.assertLess(WEIGHTS["loop"] + WEIGHTS["center"] + WEIGHTS["elevation"], WEIGHTS["distance"] / 3 + 1e-9)

    def test_closer_distance_beats_loop_bonus(self):
        """6.4 km request: Rolling Hills-like 6.8 km (not a loop, farther) must beat a 3.5 km loop, in every case."""
        for prefs in ([], ["hilly"], ["easy"]):
            for gains in ((None, None), (121, 40), (300, 0), (0, 300)):
                with self.subTest(prefs=prefs, gains=gains):
                    yellow = trail(1, 3.5, loop=True, offset=0.0, gain=gains[1])     # loop, at the center
                    rolling = trail(2, 6.8, loop=False, offset=0.05, gain=gains[0])  # one-way, ~5.5 km away
                    self.assertEqual(ranked_ids([yellow, rolling], {"distance_km": 6.4, "prefs": prefs})[0], 2)

    def test_much_closer_always_wins_randomized(self):
        rng = random.Random(7)
        for _ in range(2000):
            target = rng.uniform(1, 20)
            prefs = rng.choice([[], ["easy"], ["hilly"]])
            trails = [trail(i, rng.uniform(0.5, 30), loop=rng.random() < 0.5, offset=rng.uniform(0, 0.13),
                            gain=rng.choice([None, rng.uniform(0, 400)])) for i in range(2)]
            ranked = rank_trails(trails, {"distance_km": target, "prefs": prefs}, *CENTER)
            a, b = ranked[0], ranked[1]
            # If the second trail's distance part is more than 1/3 higher, it must not have been ranked lower.
            self.assertFalse(b["score_parts"]["distance"] - a["score_parts"]["distance"] > 1 / 3 + 1e-9,
                             (target, prefs, a, b))

    def test_without_target_distance_loop_and_center_decide(self):
        near_loop, far_line = trail(1, 3.0, loop=True, offset=0.0), trail(2, 6.0, loop=False, offset=0.1)
        ranked = rank_trails([far_line, near_loop], {"prefs": []}, *CENTER)
        self.assertEqual(ranked[0]["osm_id"], 1)
        self.assertNotIn("distance", ranked[0]["score_parts"])

    def test_unknown_elevation_is_neutral_and_every_trail_gets_the_same_parts(self):
        ranked = rank_trails([trail(1, 5, gain=None), trail(2, 5, gain=150)], {"distance_km": 5, "prefs": ["hilly"]},
                             *CENTER)
        parts = {t["osm_id"]: t["score_parts"] for t in ranked}
        self.assertEqual(parts[1]["elevation"], 0.5)
        self.assertEqual(set(parts[1]), set(parts[2]))


if __name__ == "__main__":
    unittest.main()
