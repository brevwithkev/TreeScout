"""Live Overpass integration check: python tests/test_trails.py."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.tools.trails import get_trails


def test_live_trails():
    trails = get_trails(42.2411, -83.6130, 15)
    print(f"Found {len(trails)} named trails")
    for trail in trails[:5]:
        print(f"{trail['name']}: {trail['distance_km']:.3f} km, {len(trail['coords'])} points")
    assert len(trails) >= 5, "Expected at least five named trails near Ypsilanti"
    assert all(trail["name"] and trail["distance_km"] >= 1 and len(trail["coords"]) >= 2 for trail in trails)


if __name__ == "__main__":
    test_live_trails()
