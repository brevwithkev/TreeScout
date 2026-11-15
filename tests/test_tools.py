"""Live check of every data tool for Ypsilanti, tomorrow: python tests/test_tools.py"""

from datetime import date, timedelta
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend.tools.climate import get_climate  # noqa: E402
from backend.tools.elevation import get_elevation_gain  # noqa: E402
from backend.tools.hazards import get_hazards  # noqa: E402
from backend.tools.space_weather import get_space_weather  # noqa: E402
from backend.tools.timing import compute_timing  # noqa: E402
from backend.tools.weather import get_weather  # noqa: E402

LAT, LON = 42.2411, -83.6130
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
# A short real segment of the Border-to-Border Trail along the Huron River in Ypsilanti.
SAMPLE_COORDS = [[42.2448, -83.6205], [42.2430, -83.6180], [42.2412, -83.6150], [42.2395, -83.6122],
                 [42.2380, -83.6098], [42.2362, -83.6071]]


def show(name, fn, *args):
    started = time.perf_counter()
    result = fn(*args)
    ms = round((time.perf_counter() - started) * 1000)
    if isinstance(result, dict) and isinstance(result.get("items"), list) and len(result["items"]) > 3:
        result = {**result, "items": result["items"][:3] + [f"... {len(result['items']) - 3} more"]}
    status = "UNAVAILABLE" if isinstance(result, dict) and result.get("available") is False else "ok"
    print(f"\n== {name}  [{status}, {ms} ms]\n{json.dumps(result, indent=1)}")
    return result


def test_tools_live():
    print(f"Ypsilanti ({LAT}, {LON}), date {TOMORROW}")
    results = {
        "weather": show("get_weather", get_weather, LAT, LON, TOMORROW),
        "hazards": show("get_hazards", get_hazards, LAT, LON, 100),
        "climate": show("get_climate", get_climate, LAT, LON, TOMORROW),
        "elevation": show("get_elevation_gain", get_elevation_gain, SAMPLE_COORDS),
        "space_weather": show("get_space_weather", get_space_weather),
    }
    gain = results["elevation"].get("elevation_gain_m")
    results["timing"] = show("compute_timing", compute_timing, 8.0, gain, LAT, LON, TOMORROW, "10:00")
    show("compute_timing (late start)", compute_timing, 8.0, gain, LAT, LON, TOMORROW, "16:00")

    for name, result in results.items():
        assert isinstance(result, dict) and "source" in result and "checked_at" in result, name
    return results


if __name__ == "__main__":
    test_tools_live()
