#!/usr/bin/env python3
"""Render the sample plan to out/sample-email.html -- no API key, network, or email.

Use it to preview the email after editing config.json (stores, branding,
takeout night) or the email template in meal_planner.py.

Usage:  python tools/render_sample.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import meal_planner as mp  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def render_sample() -> str:
    """The sample plan, run through link resolution and the email renderer."""
    plan = load_fixture("sample_plan.json")
    catalog = load_fixture("sample_catalog.json")
    mp.attach_recipe_links(plan, catalog, [r["url"] for r in catalog])
    return mp.build_html_email(plan)


if __name__ == "__main__":
    out_dir = os.path.join(ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "sample-email.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_sample())
    print(f"Wrote {os.path.relpath(out, ROOT)} -- open it in a browser.")
