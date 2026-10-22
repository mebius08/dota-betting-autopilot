import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
PAGE = Path(__file__).resolve().parents[1] / "app/web/index.html"


def inline_function(name):
    pattern = r"    function " + re.escape(name) + r"\([^)]*\) \{.*?\n    \}"
    match = re.search(pattern, PAGE.read_text(encoding="utf-8"), re.S)
    assert match is not None, f"Missing inline function: {name}"
    return match.group()


def run_javascript(code):
    result = subprocess.run(["node", "-e", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("times", "selected", "expected"), [
    ([], 0, -1),
    ([0.0002, 60.000004], 0, -1),
    ([0, 60.000004, 120.00001], 60, 0),
    ([0, 60.000004, 120.00001], 60.00000399999999, 0),
    ([0, 60.000004, 120.00001], 60.000004, 1),
    ([50.0334, 60.0334], 60.000004, 0),
    ([0, 3840.0002], 3860.0337, 1),
    ([0, 60], None, -1),
])
def test_as_of_uses_actual_time_without_future_tolerance(times, selected, expected):
    run_javascript(
        "const assert = require('node:assert/strict');\n"
        + inline_function("asOfIndex")
        + f"\nconst times = {json.dumps(times)};"
        + "\nconst history = times.map(time => ({actual_game_time_seconds: time}));"
        + f"\nconst selected = {json.dumps(selected)};"
        + "\nconst index = asOfIndex(history, selected);"
        + f"\nassert.equal(index, {expected});"
        + "\nif (index >= 0) assert.ok(times[index] <= selected);"
        + "\nif (selected !== null && index + 1 < times.length) assert.ok(times[index + 1] > selected);"
    )


def test_bidirectional_selection_uses_independent_as_of_indices():
    run_javascript(
        "const assert = require('node:assert/strict');\n"
        + inline_function("asOfIndex") + "\n" + inline_function("selectGameTime")
        + """
        let selectedGameTime = null;
        const economyHistory = [0.0002, 60.000004, 120.00001].map(
          actual_game_time_seconds => ({actual_game_time_seconds}));
        const povFrames = [0, 50.0334, 60.0334, 70.0334, 130.0334].map(
          (actual_game_time_seconds, replay_tick) => ({actual_game_time_seconds, replay_tick}));
        const rendered = [];
        function renderEconomyPoint(index) { rendered.push(['economy', index]); }
        function renderPovPoint(index) { rendered.push(['pov', index]); }

        selectGameTime(povFrames[0].actual_game_time_seconds);
        assert.deepEqual(rendered.splice(0), [['economy', -1], ['pov', 0]]);
        selectGameTime(economyHistory[1].actual_game_time_seconds);
        assert.equal(selectedGameTime, 60.000004);
        assert.deepEqual(rendered.splice(0), [['economy', 1], ['pov', 1]]);
        selectGameTime(povFrames[2].actual_game_time_seconds);
        assert.equal(selectedGameTime, 60.0334);
        assert.deepEqual(rendered.splice(0), [['economy', 1], ['pov', 2]]);
        selectGameTime(povFrames[4].actual_game_time_seconds);
        assert.deepEqual(rendered.splice(0), [['economy', 2], ['pov', 4]]);
        """
    )
