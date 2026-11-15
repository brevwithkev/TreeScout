"""Golden-prompt check of POST /plan against a running server.

    python tests/golden.py [--url http://localhost:8000]

Sends every non-empty line of tests/prompts.txt as {"request": line, "lat": 42.2411, "lon": -83.6130}
and checks the response against the mock_plan.json contract and per-prompt expectations.
Prints PASS/FAIL per prompt with its time and failure reasons, then a total. Exit code 1 if any fail.
"""

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import argparse
import json
import re
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "tests" / "prompts.txt"
CONTRACT = ROOT / "mock_plan.json"
LAT, LON = 42.2411, -83.6130
MAX_SECONDS = 20
MIN_COORDS = 10
DISTANCE_TOLERANCE = 0.5
KM_PER_MI = 1.609344
FORBIDDEN = ("Acorn Nature Trail", "River Bend Path", "Example Loop", "Found 14 named trails")
FAR_WARNING = "No trail near"
# (words that must all appear in the prompt, regex a warning must match, description)
EXPECTED_WARNINGS = [
    (("4 miles", "3:30 pm"), r"sunset", "a sunset-related warning"),
    (("10 miles", "2 pm"), rf"^{FAR_WARNING} ", f"a '{FAR_WARNING} ...' distance warning"),
    (("antarctica",), r"coverage", "a coverage warning"),
]
SECTIONS_WITH_SOURCE = ("weather", "climate", "space_weather")


def asked_distance_km(prompt):
    """Distance asked for in the prompt, in km (e.g. '3 mile' -> 4.83, 'under 5 km' -> 5), or None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*(miles?|mi\b|kilometers?|kilometres?|km\b)", prompt, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    return value * KM_PER_MI if m.group(2).lower().startswith("mi") else value


def post_plan(base_url, prompt, timeout):
    body = json.dumps({"request": prompt, "lat": LAT, "lon": LON}).encode("utf-8")
    request = Request(f"{base_url}/plan", data=body, method="POST", headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urlopen(request, timeout=timeout) as response:
        status, text = response.status, response.read().decode("utf-8")
    return status, text, time.perf_counter() - started


def has_source(section):
    return isinstance(section, dict) and ("source" in section or section.get("available") is False)


def check(prompt, plan, raw_text, seconds, contract_keys):
    """Return a list of failure reasons (empty means PASS)."""
    problems = []
    lowered = prompt.lower()

    missing = [k for k in contract_keys if k not in plan]
    if missing:
        problems.append(f"missing top-level keys: {', '.join(missing)}")

    leaked = [s for s in FORBIDDEN if s in raw_text]
    if leaked:
        problems.append(f"mock values in response: {', '.join(leaked)}")

    trail = plan.get("trail")
    if trail is not None:
        if not isinstance(trail, dict) or trail.get("available") is False:
            error = trail.get("error") if isinstance(trail, dict) else trail
            problems.append(f"trail unavailable: {str(error)[:160]}")
        else:
            coords = trail.get("coords") or []
            if len(coords) < MIN_COORDS:
                problems.append(f"trail.coords has {len(coords)} points (need >= {MIN_COORDS})")
            alternatives = plan.get("alternatives")
            if not isinstance(alternatives, list) or len(alternatives) != 2:
                count = len(alternatives) if isinstance(alternatives, list) else alternatives
                problems.append(f"alternatives has {count} items (need 2)")
            asked = asked_distance_km(prompt)
            got = trail.get("distance_km")
            told_far = any(str(w).startswith(FAR_WARNING) for w in plan.get("warnings") or [])
            if asked is not None:
                if not isinstance(got, (int, float)):
                    problems.append(f"trail.distance_km is {got!r}")
                elif abs(got - asked) > DISTANCE_TOLERANCE * asked and not told_far:
                    problems.append(f"trail is {got:.1f} km; asked {asked:.1f} km "
                                    f"(allowed {asked * (1 - DISTANCE_TOLERANCE):.1f}-{asked * (1 + DISTANCE_TOLERANCE):.1f}) "
                                    f"and there is no '{FAR_WARNING}' warning")

    for name in SECTIONS_WITH_SOURCE:
        if not has_source(plan.get(name)):
            problems.append(f"{name} has no source (and is not available:false)")
    hazards = plan.get("hazards")
    if isinstance(hazards, list):
        bad = [i for i, h in enumerate(hazards) if not isinstance(h, dict) or "source" not in h]
        if bad:
            problems.append(f"hazard items without source: {bad}")
    elif not has_source(hazards):
        problems.append("hazards is neither a list nor available:false with a source")

    constraints = plan.get("constraints")
    if not isinstance(constraints, dict) or not constraints.get("parsed_by"):
        problems.append("constraints.parsed_by is not set")
    if not plan.get("agent_trace"):
        problems.append("agent_trace is empty")
    checker = plan.get("checker")
    if not isinstance(checker, dict) or "approved" not in checker or "rounds" not in checker:
        problems.append(f"checker lacks approved/rounds: {checker!r}")

    warnings = [str(w) for w in plan.get("warnings") or []]
    for words, pattern, description in EXPECTED_WARNINGS:
        if all(word in lowered for word in words) and not any(re.search(pattern, w, re.IGNORECASE) for w in warnings):
            problems.append(f"expected {description}; warnings = {warnings}")
    if "antarctica" in lowered and plan.get("trail") is not None:
        problems.append("expected trail to be null")

    if seconds >= MAX_SECONDS:
        problems.append(f"took {seconds:.1f} s (limit {MAX_SECONDS} s)")
    return problems


def main():
    parser = argparse.ArgumentParser(description="Run the golden prompts against POST /plan.")
    parser.add_argument("--url", default="http://localhost:8000", help="server base URL (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=90, help="per-request HTTP timeout in seconds")
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    contract_keys = list(json.loads(CONTRACT.read_text(encoding="utf-8")))
    prompts = [line.strip() for line in PROMPTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    passed = 0
    for prompt in prompts:
        try:
            status, raw_text, seconds = post_plan(base_url, prompt, args.timeout)
            plan = json.loads(raw_text)
            problems = [] if status == 200 else [f"HTTP {status}"]
            problems += check(prompt, plan, raw_text, seconds, contract_keys) if isinstance(plan, dict) \
                else ["response is not a JSON object"]
        except HTTPError as exc:
            seconds, problems = 0.0, [f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"]
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            seconds, problems = 0.0, [f"request failed: {exc}"]

        verdict = "PASS" if not problems else "FAIL"
        passed += not problems
        print(f"{verdict}  {seconds:5.1f} s  {prompt}")
        for problem in problems:
            print(f"        - {problem}")

    print(f"\n{passed}/{len(prompts)} passed")
    return 0 if passed == len(prompts) else 1


if __name__ == "__main__":
    sys.exit(main())
