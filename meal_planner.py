#!/usr/bin/env python3
"""
Weekly Meal Plan Emailer — configurable template.

Generates a dynamic, seasonal weekly dinner plan and emails it on a schedule.
Everything personal — recipe blogs, dietary rules, budget, stores, schedule,
equipment, and the AI model — lives in `config.json`, so you can fork this and
make it your own without touching the code. Credentials stay in environment
variables / GitHub Secrets.

How it works:
- Each run fetches the post sitemaps of your configured recipe blogs, filters
  them down to a dinner-friendly catalog, and hands Claude a rotating weekly
  sample to pick from — so every meal card links to a real blog post.
- A rolling history (meal_history.json, committed back by the workflow) is fed
  into the prompt so recent dinners aren't repeated.
- Claude is primary; if it errors (e.g. no API credit) and an OpenRouter key is
  set, the same prompt is retried on your configured fallback models.
- If the whole run fails, an alert email is sent so outages aren't silent.
"""

import anthropic
import smtplib
import json
import os
import re
import sys
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date
from html import escape
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote_plus
from xml.etree import ElementTree

# ─────────────────────────────────────────────
# CONFIG  (everything personal — see config.json + README.md)
# ─────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        print("❌ config.json not found. Copy and edit it — see README.md.")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ config.json is not valid JSON: {e}")
        sys.exit(1)


CONFIG = load_config()

HOUSEHOLD = CONFIG["household"]
DIET      = CONFIG["diet"]
SCHEDULE  = CONFIG["schedule"]
BRANDING  = CONFIG.get("branding", {})
TUNING    = CONFIG.get("tuning", {})

PRIMARY_SITES   = CONFIG["sources"]["primary"]
SECONDARY_SITES = CONFIG["sources"].get("secondary", {})
RECIPE_SITES    = {**PRIMARY_SITES, **SECONDARY_SITES}

STORES    = CONFIG["stores"]
EQUIPMENT = CONFIG["equipment"]

DINNER_DAYS   = SCHEDULE["dinner_days"]
N_DINNERS     = len(DINNER_DAYS)
TAKEOUT_NIGHT = SCHEDULE.get("takeout_night")  # may be None
SPECIAL_RULES = SCHEDULE.get("special_rules", [])

BUDGET       = CONFIG["budget"]["weekly_total"]
BUDGET_NOTES = CONFIG["budget"].get("notes", "")

# Dietary/allergy catalog exclusions (e.g. no seafood). Substrings match anywhere
# in a recipe slug; tokens match whole hyphen-separated words (for short terms
# that would false-positive, like "cod" inside "code").
EXCLUDE_SUBSTRINGS = [s.lower() for s in DIET.get("exclude_keywords", [])]
EXCLUDE_TOKENS     = {t.lower() for t in DIET.get("exclude_tokens", [])}

# Tuning knobs (sensible defaults if omitted from config).
HISTORY_WEEKS         = TUNING.get("history_weeks", 6)
CATALOG_SAMPLE_SIZE   = TUNING.get("catalog_sample_size", 300)
SECONDARY_SAMPLE_EACH = TUNING.get("secondary_sample_each", 20)
MIN_CATALOG           = TUNING.get("min_catalog", 25)

# ─────────────────────────────────────────────
# CREDENTIALS  (environment / GitHub Secrets — never in config.json)
# ─────────────────────────────────────────────
GMAIL_SENDER      = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASS    = os.environ.get("GMAIL_APP_PASS", "")
EMAIL_RECIPIENT   = os.environ.get("EMAIL_RECIPIENT", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Failure alerts go here (defaults to the sending account so the admin is
# notified without spamming everyone). Comma-separated list to override.
ALERT_RECIPIENT   = os.environ.get("ALERT_RECIPIENT", "")

# Primary model from config; an env var can still override it per-run.
PRIMARY_MODEL = os.environ.get("PRIMARY_MODEL", "").strip() or CONFIG["model"]["primary"]
# Secondary provider: Claude stays primary; only if it errors does the planner
# retry via OpenRouter. Leave OPENROUTER_API_KEY unset to disable the fallback.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# Env var overrides config; `or` so a present-but-empty env var still falls back.
OPENROUTER_MODELS = (
    os.environ.get("OPENROUTER_MODELS", "").strip()
    or ",".join(CONFIG["model"].get("openrouter_fallbacks", []))
)

# Many recipe blogs return 403 to Python's default urllib User-Agent, so every
# request must identify as a regular browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HISTORY_FILE       = os.path.join(os.path.dirname(__file__), "meal_history.json")
CATALOG_CACHE_FILE = os.path.join(os.path.dirname(__file__), "recipe_catalog.json")
FEEDBACK_FILE      = os.path.join(os.path.dirname(__file__), "feedback.txt")


def require_credentials():
    """Exit with a helpful message if any required env var is missing."""
    missing = [k for k, v in {
        "GMAIL_SENDER": GMAIL_SENDER,
        "GMAIL_APP_PASS": GMAIL_APP_PASS,
        "EMAIL_RECIPIENT": EMAIL_RECIPIENT,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("   See README.md for setup instructions.")
        sys.exit(1)


def get_season() -> str:
    """Northern-hemisphere season from the current month. The model fills in
    locally-seasonal produce from the household location in the prompt."""
    month = date.today().month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "fall"


def load_feedback() -> str:
    """Read optional household feedback notes from feedback.txt if present."""
    if os.path.exists(FEEDBACK_FILE):
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            print("📝 Loaded feedback notes.")
            return content
    print("📝 No feedback file found — generating fresh plan.")
    return ""


# ─────────────────────────────────────────────
# RECIPE CATALOG
# ─────────────────────────────────────────────

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _fetch_sitemap_locs(url: str) -> list:
    """Fetch one sitemap XML and return its <loc> entries."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        tree = ElementTree.parse(resp)
    return [loc.text.strip() for loc in tree.findall(".//sm:loc", _SITEMAP_NS) if loc.text]


def fetch_recipe_urls() -> list:
    """Fetch all post URLs from every configured recipe site. Post sitemaps are
    discovered from each site's sitemap index; falls back to the conventional
    /post-sitemap.xml if the index can't be read."""
    urls = []
    for domain in RECIPE_SITES:
        try:
            index_locs = _fetch_sitemap_locs(f"https://{domain}/sitemap_index.xml")
            post_maps = [loc for loc in index_locs if "post-sitemap" in loc]
        except Exception:
            post_maps = []
        if not post_maps:
            post_maps = [f"https://{domain}/post-sitemap.xml"]

        site_count = 0
        for sitemap in post_maps:
            try:
                found = _fetch_sitemap_locs(sitemap)
                urls.extend(found)
                site_count += len(found)
            except Exception as e:
                print(f"⚠️  Could not load sitemap {sitemap}: {e}")
        print(f"✅ {domain}: {site_count} posts")
    return urls


def get_recipe_urls() -> list:
    """Fetch live post URLs, falling back to the committed cache when the blogs
    are unreachable. A healthy fetch refreshes the cache; a degraded one leaves
    the cache intact so next week's outage still has the full list."""
    fresh = fetch_recipe_urls()
    cached = []
    if os.path.exists(CATALOG_CACHE_FILE):
        try:
            with open(CATALOG_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Could not read catalog cache: {e}")

    if len(fresh) >= max(MIN_CATALOG, len(cached) // 2):
        if fresh != cached:
            with open(CATALOG_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(fresh, f, indent=1)
                f.write("\n")
            print(f"💾 Catalog cache refreshed ({len(fresh)} posts).")
        return fresh

    if cached:
        print(f"⚠️  Live fetch degraded ({len(fresh)} posts) — using cached catalog ({len(cached)} posts).")
        return cached
    return fresh


# Posts whose slug matches any of these are never dinner recipes (universal).
NON_DINNER_SUBSTRINGS = [
    # desserts & baked goods
    "smoothie", "muffin", "cookie", "brownie", "cake", "waffle", "granola",
    "oatmeal", "overnight-oats", "baked-oats", "scone", "donut", "doughnut",
    "pudding", "cobbler", "crumble", "ice-cream", "popsicle", "frosting",
    "icing", "candy", "fudge", "blondie", "biscotti", "banana-bread",
    "zucchini-bread", "pumpkin-bread", "french-toast", "parfait", "dessert",
    "butter-cups", "sorbet", "macaroon", "cinnamon-roll", "babka", "strudel",
    "energy-bites", "energy-balls", "trail-mix", "bars", "truffles",
    "caramel", "chocolate",
    # drinks
    "latte", "matcha", "espresso", "cocktail", "mocktail", "margarita",
    "sangria", "lemonade", "hot-chocolate", "juice", "acai",
    # not dinner
    "breakfast", "brunch", "hummus", "salsa", "guacamole", "dressing",
    "broth", "snack", "shake",
    # lifestyle / roundup posts
    "guide", "round-up", "roundup", "recap", "travel", "what-i", "what-we",
    "gift", "favorite", "faves", "amazon", "nordstrom", "podcast",
    "interview", "review", "haul", "outfit", "beauty", "skincare", "workout",
    "fitness", "pregnancy", "baby", "nursery", "home-tour", "restaurant",
    "wellness", "anxiety", "supplement", "video", "blog", "recent-posts",
    "week-in", "weekly", "monthly", "meal-plan", "meal-prep", "kitchen",
    "pantry", "how-to", "tips", "things", "hack", "collab", "giveaway",
    "announcement", "reveal", "resolution", "essentials", "must-have",
    "postpartum", "reeses", "questions",
    "recipes", "game-day", "super-bowl", "labor-day", "memorial-day",
    "thanksgiving", "christmas", "easter", "halloween", "valentines",
]
NON_DINNER_TOKENS = {"milk", "crisp", "city", "pie", "why", "bites", "dip", "sale", "qa"}


def source_for_url(url: str) -> str:
    for domain, label in RECIPE_SITES.items():
        if domain in url:
            return label
    return "View Recipe"


def source_list_phrase() -> str:
    labels = list(RECIPE_SITES.values())
    if not labels:
        return "your recipe blogs"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def build_recipe_catalog(urls: list) -> list:
    """Filter sitemap URLs down to plausible dinner recipes. The filter is
    heuristic — Claude makes the final pick, so a few stray titles are harmless,
    but anything in the configured dietary exclusions must never get through."""
    catalog = []
    seen_slugs = set()
    for url in urls:
        slug = url.rstrip("/").split("/")[-1].lower()
        tokens = slug.split("-")
        if len(tokens) < 2 or slug in seen_slugs:
            continue
        if any(s in slug for s in EXCLUDE_SUBSTRINGS) or any(t in EXCLUDE_TOKENS for t in tokens):
            continue
        if any(s in slug for s in NON_DINNER_SUBSTRINGS) or any(t in NON_DINNER_TOKENS for t in tokens):
            continue
        seen_slugs.add(slug)
        catalog.append({
            "title": slug.replace("-", " ").title(),
            "url": url,
            "source": source_for_url(url),
        })
    return catalog


def weekly_recipe_sample(catalog: list) -> list:
    """Draw this week's catalog sample, seeded by ISO year+week so re-runs within
    the same week see the same candidates while each new week rotates in a fresh
    selection. Primary blogs split most of the sample; backups get a fixed number
    each, with primary slots filled from backups if the primaries come up short."""
    iso = date.today().isocalendar()
    rng = random.Random(f"{iso[0]}-W{iso[1]}")

    primary_labels = set(PRIMARY_SITES.values())
    by_source = {}
    for recipe in catalog:
        by_source.setdefault(recipe["source"], []).append(recipe)

    primary_sources = sorted(s for s in by_source if s in primary_labels)
    secondary_sources = sorted(s for s in by_source if s not in primary_labels)

    picks, leftovers = [], []

    def take(sources, quota):
        for source in sources:
            pool = by_source[source]
            chosen = rng.sample(pool, min(quota, len(pool)))
            picks.extend(chosen)
            chosen_urls = {r["url"] for r in chosen}
            leftovers.extend(r for r in pool if r["url"] not in chosen_urls)

    if primary_sources:
        primary_budget = CATALOG_SAMPLE_SIZE - SECONDARY_SAMPLE_EACH * len(secondary_sources)
        take(primary_sources, max(primary_budget // len(primary_sources), 0))
        take(secondary_sources, SECONDARY_SAMPLE_EACH)
    else:
        take(secondary_sources, CATALOG_SAMPLE_SIZE // max(len(secondary_sources), 1))

    shortfall = CATALOG_SAMPLE_SIZE - len(picks)
    if shortfall > 0:
        primary_left = [r for r in leftovers if r["source"] in primary_labels]
        fill = rng.sample(primary_left, min(shortfall, len(primary_left)))
        picks.extend(fill)
        shortfall -= len(fill)
    if shortfall > 0:
        secondary_left = [r for r in leftovers if r["source"] not in primary_labels]
        picks.extend(rng.sample(secondary_left, min(shortfall, len(secondary_left))))

    rng.shuffle(picks)
    return [{**recipe, "id": i + 1} for i, recipe in enumerate(picks)]


# ─────────────────────────────────────────────
# MEAL HISTORY (rotation memory)
# ─────────────────────────────────────────────

def load_meal_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            if isinstance(history, list):
                print(f"📚 Loaded meal history ({len(history)} past weeks).")
                return history
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Could not read meal history: {e}")
    print("📚 No meal history yet — first run.")
    return []


def save_meal_history(plan: dict, history: list):
    """Append this week's meals to the history file (workflow commits it)."""
    history.append({
        "week_of": plan.get("week_of", ""),
        "meals": [
            {"day": m["day"], "name": m["name"], "recipe_url": m.get("recipe_url", "")}
            for m in plan["meals"]
        ],
    })
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"💾 Meal history updated ({len(history)} weeks recorded).")


# ─────────────────────────────────────────────
# RECIPE LINK FALLBACK (only for original, non-catalog meals)
# ─────────────────────────────────────────────

def find_recipe_url(meal_name: str, recipe_urls: list) -> tuple:
    """Fuzzy-match fallback for meals Claude invented rather than picked from the
    catalog. Requires at least two meaningful overlapping words before trusting a
    match; otherwise returns a Google site-search across the primary blogs."""
    core_name = re.split(r"\s+(with|over|and|topped|served)\s+", meal_name, flags=re.IGNORECASE)[0]
    core_name = re.sub(r"^(traeger|grill|grilled|smoked|slow cooker|sheet pan|one-skillet|one skillet)\s+",
                       "", core_name, flags=re.IGNORECASE)

    stop_words = {"with", "and", "the", "a", "an", "in", "on", "of", "for", "to", "over"}
    meal_words = set(
        w.lower() for w in re.split(r"[\s\-]+", core_name)
        if w.lower() not in stop_words and len(w) > 2
    )

    best_url, best_score = None, 0
    for url in recipe_urls:
        slug = url.rstrip("/").split("/")[-1]
        slug_words = set(slug.split("-"))
        overlap = meal_words & slug_words
        score = len(overlap)
        if slug_words:
            score += len(overlap) / len(slug_words)
        if score > best_score:
            best_score = score
            best_url = url

    if best_score >= 2.5 and best_url:
        return best_url, source_for_url(best_url)

    sites = "+OR+".join(f"site:{d}" for d in PRIMARY_SITES) or ""
    fallback = f"https://www.google.com/search?q={quote_plus(core_name)}"
    if sites:
        fallback += f"+{sites}"
    return fallback, "Search Recipes"


def attach_recipe_links(plan: dict, recipe_sample: list, recipe_urls: list):
    """Resolve each meal's recipe_id to a real catalog URL (or fall back)."""
    by_id = {r["id"]: r for r in recipe_sample}
    for meal in plan["meals"]:
        entry = by_id.get(meal.pop("recipe_id", None))
        if entry:
            meal["recipe_url"] = entry["url"]
            meal["recipe_source"] = entry["source"]
        else:
            url, source = find_recipe_url(meal["name"], recipe_urls)
            meal["recipe_url"] = url
            meal["recipe_source"] = source


# ─────────────────────────────────────────────
# MEAL PLAN GENERATION
# ─────────────────────────────────────────────

_GROCERY_ITEMS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "item": {"type": "string"},
            "est_cost": {"type": "string", "description": 'e.g. "$12.99"'},
            "bulk_tip": {"type": "string", "description": "Short note if buying larger saves money long-term; omit otherwise"},
            "swap": {"type": "string", "description": "Cheaper alternative if item is pricey; omit otherwise"},
            "likely_sale": {"type": "boolean", "description": "True if this item's category rotates on predictable weekly sale cycles at this store"},
        },
        "required": ["item", "est_cost", "likely_sale"],
        "additionalProperties": False,
    },
}


def build_meal_plan_schema() -> dict:
    """Build the structured-output schema from config (stores, days, budget)."""
    grocery_props = {store["key"]: _GROCERY_ITEMS_SCHEMA for store in STORES}
    methods_desc = "One of: " + ", ".join(EQUIPMENT)
    location = HOUSEHOLD.get("location", "your area")
    return {
        "type": "object",
        "properties": {
            "week_of": {"type": "string"},
            "season": {"type": "string"},
            "seasonal_note": {"type": "string", "description": f"One sentence about what's fresh near {location} right now"},
            "meals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string", "enum": DINNER_DAYS},
                        "name": {"type": "string"},
                        "method": {"type": "string", "description": methods_desc},
                        "prep_note": {"type": "string", "description": 'Timing note (slow cooker start time, marinade note); "" if none'},
                        "description": {"type": "string", "description": "1-2 sentences; mention kid/eater appeal if relevant"},
                        "cook_time": {"type": "string", "description": 'Active time, e.g. "30 min"'},
                        "recipe_steps": {"type": "array", "items": {"type": "string"}, "description": "4-6 clear steps for your adapted version"},
                        "recipe_id": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "description": "Catalog id this dinner is based on, or null for an original creation",
                        },
                    },
                    "required": ["day", "name", "method", "prep_note", "description", "cook_time", "recipe_steps", "recipe_id"],
                    "additionalProperties": False,
                },
            },
            "grocery": {
                "type": "object",
                "properties": grocery_props,
                "required": [store["key"] for store in STORES],
                "additionalProperties": False,
            },
            "grocery_total": {"type": "string", "description": f'Sum of all items, e.g. "$148" — must be at or under {BUDGET}'},
            "budget_tips": {"type": "array", "items": {"type": "string"}},
            "pantry_note": {"type": "string"},
        },
        "required": ["week_of", "season", "seasonal_note", "meals", "grocery", "grocery_total", "budget_tips", "pantry_note"],
        "additionalProperties": False,
    }


MEAL_PLAN_SCHEMA = build_meal_plan_schema()


def _bullets(items) -> str:
    return "\n".join(f"- {it}" for it in items)


def build_catalog_section(recipe_sample: list) -> str:
    from_catalog = max(1, N_DINNERS - 2)
    if len(recipe_sample) < MIN_CATALOG:
        return f"""RECIPE SOURCES:
The live recipe catalog could not be loaded this week. Create all {N_DINNERS} dinners
yourself (set every "recipe_id" to null), in the style of {source_list_phrase()}.

"""
    primary_labels = set(PRIMARY_SITES.values())
    n_primary = sum(1 for r in recipe_sample if r["source"] in primary_labels)
    primary_names = " and ".join(PRIMARY_SITES.values())
    backup_names = " and ".join(SECONDARY_SITES.values()) if SECONDARY_SITES else ""

    if n_primary >= MIN_CATALOG and backup_names:
        priority_rule = f"""- SOURCE PRIORITY: the go-to blogs are {primary_names} — pick from them by default.
  Recipes from {backup_names} are backups for extra variety: use AT MOST 1 backup-blog
  dinner this week (zero is fine), and only when it fits the week clearly better."""
    elif n_primary >= MIN_CATALOG:
        priority_rule = f"- SOURCE PRIORITY: pick from the catalog below ({primary_names})."
    else:
        priority_rule = f"""- SOURCE PRIORITY: the go-to blogs ({primary_names}) could not be loaded this week,
  so the catalog below is mostly backups — choose freely from it."""

    lines = "\n".join(f"{r['id']}. {r['title']} — {r['source']}" for r in recipe_sample)
    return f"""RECIPE CATALOG — THIS WEEK'S SELECTION:
Below is a rotating sample of real recipes from {source_list_phrase()}
(a fresh sample is drawn each week). Each line is "id. Recipe Title — Source".

RULES FOR CHOOSING DINNERS:
- At least {from_catalog} of the {N_DINNERS} dinners must come from this catalog. Set that meal's
  "recipe_id" to the catalog id, and keep the meal name recognizably tied to the title.
{priority_rule}
- Up to 2 dinners may be your own creation ("recipe_id": null) when that better serves the
  budget, the leftover strategy, or equipment needs.
- Only pick recipes that fit EVERY constraint listed above. Skip anything that isn't a dinner.
- If a recipe relies on equipment you don't have, adapt the steps to your equipment and name
  the meal accordingly. Adapt carb-heavy sides toward the dietary preferences above.
- Prefer recipes the household has NOT seen in the recently-served list.

{lines}

"""


def build_rotation_section(history: list) -> str:
    must_be_new = max(1, N_DINNERS - 2)
    proteins = min(3, N_DINNERS)
    section = ""
    recent = history[-HISTORY_WEEKS:]
    if recent:
        weeks = "\n".join(
            f"- Week of {w.get('week_of', '?')}: " + "; ".join(m["name"] for m in w.get("meals", []))
            for w in reversed(recent)
        )
        section += f"""RECENTLY SERVED — DO NOT REPEAT:
These dinners were served in recent weeks. Do not serve them again this week, and avoid close
variants (same protein + same preparation/cuisine counts as a repeat). At least {must_be_new} of this
week's {N_DINNERS} dinners must be completely new vs this list. A deliberate repeat is allowed only
when the feedback notes explicitly ask for that meal again.

{weeks}

"""
    section += f"""VARIETY WITHIN THE WEEK:
- Use at least {proteins} different primary proteins across the {N_DINNERS} dinners.
- A bulk anchor protein may appear in 2 meals only if the preparations are clearly different
  cuisines/styles (e.g., Greek chicken bowls one night, chicken lettuce-wrap tacos another).
- Never two dishes of the same style in one week (no two taco nights, two stir-fries, etc.).

"""
    return section


def _build_meal_plan_prompt(recipe_sample: list, history: list) -> str:
    """Assemble the full generation prompt from config + this week's data."""
    season = get_season()
    week_of = date.today().strftime("%B %d, %Y")
    feedback = load_feedback()
    location = HOUSEHOLD.get("location", "your area")
    who = HOUSEHOLD.get("who", "the household")

    takeout_clause = f" {TAKEOUT_NIGHT} is takeout/leftover night." if TAKEOUT_NIGHT else ""
    days_phrase = f"{DINNER_DAYS[0]} through {DINNER_DAYS[-1]}" if N_DINNERS > 2 else ", ".join(DINNER_DAYS)

    rules = _bullets(DIET.get("hard_rules", []))
    prefs = _bullets(DIET.get("preferences", []))
    specials = _bullets(SPECIAL_RULES)
    store_strategy = _bullets(
        f"At {s['label']}: {s['strategy']}" for s in STORES if s.get("strategy")
    )
    store_buys = "\n".join(f"  {s['label']}: {s.get('buys', 'as appropriate')}" for s in STORES)

    if feedback:
        feedback_section = f"""FEEDBACK & PREFERENCES (from past weeks):
The household has left notes about past meals. Use this to repeat hits, avoid flops, note
ingredients eaters refused, and let it meaningfully shape this week's plan. Treat it as real
memory accumulated over time.

--- FEEDBACK NOTES START ---
{feedback}
--- FEEDBACK NOTES END ---

"""
    else:
        feedback_section = ""

    return f"""You are a meal planning expert creating a weekly dinner plan for {who} in {location}.

Today is {week_of}. The current season is {season}. Favor ingredients that are in season
near {location} right now.

CONSTRAINTS:
- {N_DINNERS} dinners ({days_phrase}).{takeout_clause}
- Budget: {BUDGET} total groceries for the week — a firm limit. {BUDGET_NOTES}
{rules}
{prefs}
- Available equipment: {", ".join(EQUIPMENT)}. Only use these cooking methods.
{specials}
- Use seasonal ingredients where possible.

BUDGET-SMART PURCHASING STRATEGY:
{store_strategy}
- Avoid specialty/boutique ingredients that spike cost without nutritional payoff.
- Where a recipe calls for an expensive ingredient, suggest a budget swap in the item's "swap" field.
- Plan at least one meal that intentionally reuses leftovers or a second use of a bulk protein
  bought earlier in the week — call this out explicitly.
- Flag items where buying a larger size now saves money next week in the "bulk_tip" field.

{build_catalog_section(recipe_sample)}{build_rotation_section(history)}{feedback_section}OUTPUT NOTES (the response is validated against a JSON schema automatically):
- meals: exactly {N_DINNERS} dinners, in order: {", ".join(DINNER_DAYS)}.
- method: one of {", ".join(EQUIPMENT)}.
- prep_note: timing note ("Start slow cooker at 8am on LOW", "Marinate the night before"); "" if none.
- recipe_steps: 4-6 clear steps for YOUR adapted version, written for a home cook.
- grocery: every ingredient needed, organized by store:
{store_buys}
  Assume a typical pantry (salt, pepper, basic dried spices, cooking oil) is already on hand.
- grocery_total: MUST be at or under {BUDGET}.
- budget_tips: 2 short tips — this week's best value anchor protein, and a bulk buy that pays
  off over multiple weeks.
- week_of: "{week_of}". season: "{season}"."""


def generate_meal_plan(recipe_sample: list, history: list) -> dict:
    """Generate the weekly plan. Claude is primary; if it errors and an
    OpenRouter key is set, the same prompt is retried on the fallback models."""
    prompt = _build_meal_plan_prompt(recipe_sample, history)
    try:
        return _generate_with_anthropic(prompt)
    except Exception as primary_error:
        if not OPENROUTER_API_KEY:
            raise
        print(f"⚠️  Primary model failed: {type(primary_error).__name__}: {primary_error}")
        print(f"   Falling back to OpenRouter ({OPENROUTER_MODELS})...")
        try:
            return _generate_with_openrouter(prompt)
        except Exception as fallback_error:
            raise RuntimeError(
                f"Both providers failed — primary: {type(primary_error).__name__}: {primary_error}; "
                f"OpenRouter: {type(fallback_error).__name__}: {fallback_error}"
            ) from fallback_error


def _generate_with_anthropic(prompt: str) -> dict:
    """Primary path: the configured Claude model with structured outputs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    last_error = None
    for attempt in range(2):
        try:
            message = client.messages.create(
                model=PRIMARY_MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": MEAL_PLAN_SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
            if message.stop_reason == "max_tokens":
                raise ValueError("Response truncated at max_tokens")
            text = next(b.text for b in message.content if b.type == "text")
            plan = json.loads(text)
            if len(plan["meals"]) != N_DINNERS:
                print(f"⚠️  Expected {N_DINNERS} meals, got {len(plan['meals'])} — continuing anyway.")
            return plan
        except (json.JSONDecodeError, ValueError, StopIteration) as e:
            last_error = e
            print(f"⚠️ Generation attempt {attempt + 1} failed: {e}. Retrying...")
    raise ValueError(f"Failed to generate meal plan after 2 attempts. Last error: {last_error}")


def _generate_with_openrouter(prompt: str) -> dict:
    """Fallback path: retry the same prompt across OPENROUTER_MODELS in order.

    Uses json_object mode rather than strict json_schema: the grocery schema has
    optional fields, which OpenAI-style strict validation rejects. The full
    schema is appended to the prompt so the model still returns the right shape."""
    or_prompt = (
        prompt
        + "\n\nReturn ONLY a single JSON object — no markdown fences, no commentary "
        + "— that conforms to this JSON schema:\n"
        + json.dumps(MEAL_PLAN_SCHEMA)
    )
    models = [m.strip() for m in OPENROUTER_MODELS.split(",") if m.strip()]
    last_error = None
    for model in models:
        for attempt in range(2):
            try:
                plan = _openrouter_request(model, or_prompt)
                if len(plan["meals"]) != N_DINNERS:
                    print(f"⚠️  OpenRouter {model} returned {len(plan['meals'])} meals, expected {N_DINNERS} — continuing.")
                print(f"✅ Plan generated via OpenRouter fallback ({model}).")
                return plan
            except Exception as e:
                last_error = e
                print(f"⚠️  OpenRouter {model} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"All OpenRouter fallback models failed. Last error: {last_error}")


def _openrouter_request(model: str, prompt: str) -> dict:
    """One OpenRouter chat-completions call; returns the parsed plan dict."""
    body = json.dumps({
        "model": model,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": BRANDING.get("openrouter_app_title", "Weekly Meal Plan"),
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as he:
        detail = he.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {he.code}: {detail}") from he

    if data.get("error"):
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    choice = data["choices"][0]
    content = choice["message"].get("content")
    if not content:
        raise ValueError(f"empty content (finish_reason={choice.get('finish_reason')})")
    return json.loads(_strip_code_fence(content))


def _strip_code_fence(text: str) -> str:
    """Strip a leading ```json / trailing ``` fence if a model wraps its output."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

DAY_COLORS = {
    "Sunday": "#8B7355", "Monday": "#6B8E6B", "Tuesday": "#7B8E9B",
    "Wednesday": "#9B8B6B", "Thursday": "#7B6B8E", "Friday": "#8E6B7B",
    "Saturday": "#999999",
}
METHOD_ICONS = {
    "Traeger": "🔥", "Grill": "🔥", "Grilled": "🔥", "Smoker": "🔥",
    "Slow Cooker": "🫕", "Crockpot": "🫕", "Oven": "♨️", "Stove": "🍳",
    "Stovetop": "🍳", "Blender": "🌀", "Air Fryer": "🍤",
    "Instant Pot": "⚡", "Pressure Cooker": "⚡",
}


def build_html_email(plan: dict) -> str:
    """Render the meal plan as an HTML email."""
    meals_html = ""
    for meal in plan["meals"]:
        color = DAY_COLORS.get(meal["day"], "#888")
        icon = METHOD_ICONS.get(meal["method"], "🍽️")
        prep_html = f'<div class="prep-note">⏰ {escape(meal["prep_note"])}</div>' if meal.get("prep_note") else ""

        if meal.get("recipe_steps"):
            steps_items = "".join(f"<li>{escape(s)}</li>" for s in meal["recipe_steps"])
            steps_html = f'<div class="recipe-steps"><div class="steps-label">How to make it</div><ol class="steps-list">{steps_items}</ol></div>'
        else:
            steps_html = ""

        link_html = (
            f'<a class="recipe-link" href="{escape(meal["recipe_url"], quote=True)}" '
            f'target="_blank">📖 {escape(meal["recipe_source"])} →</a>'
        )

        meals_html += f"""
        <div class="meal-card">
          <div class="meal-day" style="color:{color}">{escape(meal["day"])}</div>
          <div class="meal-header">
            <span class="method-badge">{icon} {escape(meal["method"])}</span>
            <span class="cook-time">⏱ {escape(meal["cook_time"])}</span>
          </div>
          <div class="meal-name">{escape(meal["name"])}</div>
          <div class="meal-desc">{escape(meal["description"])}</div>
          {prep_html}
          {steps_html}
          {link_html}
        </div>"""

    if TAKEOUT_NIGHT:
        meals_html += f"""
        <div class="meal-card takeout">
          <div class="meal-day" style="color:#aaa">{escape(TAKEOUT_NIGHT)}</div>
          <div class="meal-name">🥡 Takeout / Leftover Night</div>
          <div class="meal-desc">You've earned it. Enjoy a night off!</div>
        </div>"""

    grocery_html = ""
    for store in STORES:
        items = plan["grocery"].get(store["key"], [])
        if not items:
            continue
        rows = ""
        for i in items:
            extra = ""
            if i.get("likely_sale"):
                extra += '<span class="tag tag-sale">likely on sale</span> '
            if i.get("bulk_tip"):
                extra += f'<span class="tag tag-bulk">📦 {escape(i["bulk_tip"])}</span> '
            if i.get("swap"):
                extra += f'<span class="tag tag-swap">💡 {escape(i["swap"])}</span>'
            extra_html = f'<div class="item-tags">{extra}</div>' if extra else ""
            rows += f'<tr><td class="g-item">{escape(i["item"])}{extra_html}</td><td class="g-cost">{escape(i["est_cost"])}</td></tr>'
        grocery_html += f"""
        <div class="store-section">
          <div class="store-header" style="background:{escape(store.get('color', '#444'), quote=True)}">{escape(store['label'].upper())}</div>
          <table class="grocery-table">
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    pantry_note = escape(plan.get("pantry_note", ""))
    seasonal_note = escape(plan.get("seasonal_note", ""))
    headline = escape(HOUSEHOLD.get("headline", HOUSEHOLD.get("name", "")))
    title_html = BRANDING.get("title_html", "This Week's<br><em>Dinner Plan</em>")
    footer = escape(BRANDING.get("footer", "Happy cooking 🍽️"))

    budget_tips_html = ""
    if plan.get("budget_tips"):
        tips = "".join(f'<div class="budget-tip-item">{escape(tip)}</div>' for tip in plan["budget_tips"])
        budget_tips_html = f'<div class="budget-tips"><div class="budget-tips-label">💰 This Week\'s Budget Tips</div>{tips}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekly Meal Plan – {escape(plan['week_of'])}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;1,400&family=Source+Sans+3:wght@300;400;600&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #F7F4EF; font-family: 'Source Sans 3', sans-serif; color: #2C2C2C; }}
  .wrapper {{ max-width: 640px; margin: 0 auto; background: #fff; }}
  .header {{ background: #2C2215; padding: 40px 36px 32px; text-align: center; }}
  .header-eyebrow {{ font-size: 11px; letter-spacing: 3px; text-transform: uppercase; color: #B8A882; margin-bottom: 10px; }}
  .header h1 {{ font-family: 'Playfair Display', serif; font-size: 36px; color: #F5EDD8; line-height: 1.1; margin-bottom: 6px; }}
  .header-sub {{ font-size: 13px; color: #8A7A5A; margin-bottom: 20px; }}
  .season-badge {{ display: inline-block; background: #8B7355; color: #F5EDD8; font-size: 11px; letter-spacing: 2px; text-transform: uppercase; padding: 5px 14px; border-radius: 20px; }}
  .seasonal-note {{ background: #F0EAD8; border-left: 3px solid #8B7355; padding: 14px 20px; font-size: 13px; color: #5A4A2A; font-style: italic; }}
  .section-label {{ font-family: 'Playfair Display', serif; font-size: 22px; color: #2C2215; padding: 28px 36px 12px; border-bottom: 1px solid #E8E0D0; }}
  .meals-grid {{ padding: 20px 24px; display: grid; gap: 14px; }}
  .meal-card {{ background: #FDFAF5; border: 1px solid #EAE4D8; border-radius: 8px; padding: 16px 18px; }}
  .meal-card.takeout {{ background: #F5F5F5; border-color: #DEDEDE; opacity: 0.7; }}
  .meal-day {{ font-size: 11px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 6px; }}
  .meal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 6px; }}
  .method-badge {{ font-size: 11px; background: #2C2215; color: #F5EDD8; padding: 3px 10px; border-radius: 12px; }}
  .cook-time {{ font-size: 11px; color: #999; }}
  .meal-name {{ font-family: 'Playfair Display', serif; font-size: 18px; color: #2C2215; margin-bottom: 4px; line-height: 1.3; }}
  .meal-desc {{ font-size: 13px; color: #6A5A4A; line-height: 1.5; }}
  .prep-note {{ margin-top: 8px; font-size: 12px; color: #8B6020; background: #FFF8EC; border: 1px solid #F0D89A; border-radius: 4px; padding: 5px 10px; }}
  .recipe-steps {{ margin-top: 10px; background: #F7F4EC; border: 1px solid #E8E0D0; border-radius: 6px; padding: 10px 14px; }}
  .steps-label {{ font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: #8B7355; font-weight: 600; margin-bottom: 6px; }}
  .steps-list {{ margin: 0; padding-left: 18px; }}
  .steps-list li {{ font-size: 12px; color: #5A4A3A; line-height: 1.5; padding: 2px 0; }}
  .recipe-link {{ display: inline-block; margin-top: 10px; font-size: 12px; font-weight: 600; color: #8B7355; text-decoration: none; border-bottom: 1px solid #D8C8A8; padding-bottom: 1px; }}
  .grocery-wrap {{ padding: 16px 24px 28px; display: grid; gap: 16px; }}
  .store-section {{ border-radius: 8px; overflow: hidden; border: 1px solid #E0D8CC; }}
  .store-header {{ color: white; font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase; font-weight: 600; padding: 8px 14px; }}
  .grocery-table {{ width: 100%; border-collapse: collapse; }}
  .grocery-table tr:nth-child(even) {{ background: #FDFAF5; }}
  .g-item {{ font-size: 13px; padding: 7px 14px; color: #2C2C2C; }}
  .g-cost {{ font-size: 13px; padding: 7px 14px; color: #6A8A6A; font-weight: 600; text-align: right; white-space: nowrap; }}
  .budget-row {{ margin: 0 24px 8px; background: #2C2215; color: #F5EDD8; padding: 12px 18px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; }}
  .budget-label {{ letter-spacing: 1px; text-transform: uppercase; font-size: 11px; }}
  .budget-amount {{ font-family: 'Playfair Display', serif; font-size: 22px; }}
  .pantry-note {{ margin: 0 24px 28px; font-size: 11px; color: #999; font-style: italic; line-height: 1.5; }}
  .budget-tips {{ margin: 0 24px 20px; background: #F0FAF0; border: 1px solid #C8E6C8; border-radius: 8px; padding: 14px 16px; }}
  .budget-tips-label {{ font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: #4A8A4A; font-weight: 600; margin-bottom: 8px; }}
  .budget-tip-item {{ font-size: 12px; color: #3A6A3A; padding: 3px 0; line-height: 1.4; }}
  .budget-tip-item::before {{ content: "✓  "; }}
  .item-tags {{ margin-top: 3px; }}
  .tag {{ display: inline-block; font-size: 10px; padding: 2px 7px; border-radius: 10px; margin-right: 4px; margin-top: 2px; font-weight: 600; }}
  .tag-sale {{ background: #FFF0C0; color: #9A6A00; border: 1px solid #F0D060; }}
  .tag-bulk {{ background: #E8F0FF; color: #2A4A9A; border: 1px solid #B0C8FF; }}
  .tag-swap {{ background: #F0F0F0; color: #555; border: 1px solid #CCC; font-weight: 400; }}
  .footer {{ background: #2C2215; text-align: center; padding: 20px; font-size: 11px; color: #9A8A6A; letter-spacing: 1px; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="header-eyebrow">{headline}</div>
    <h1>{title_html}</h1>
    <div class="header-sub">Week of {escape(plan['week_of'])}</div>
    <div class="season-badge">🌿 {escape(plan['season'].title())} Season</div>
  </div>
  <div class="seasonal-note">🌱 {seasonal_note}</div>
  <div class="section-label">Dinners This Week</div>
  <div class="meals-grid">{meals_html}</div>
  <div class="section-label">Grocery List by Store</div>
  <div class="grocery-wrap">{grocery_html}</div>
  {budget_tips_html}
  <div class="budget-row">
    <div><div class="budget-label">Estimated Weekly Total</div></div>
    <div class="budget-amount">{escape(plan['grocery_total'])}</div>
  </div>
  <div class="pantry-note">* {pantry_note}</div>
  <div class="footer">{footer}</div>
</div>
</body>
</html>"""


def _send_mail(subject: str, html_content: str, recipients: list):
    """Low-level Gmail SMTP send to an explicit recipient list."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    sender_clean  = "".join(c for c in GMAIL_SENDER   if ord(c) < 128).strip()
    apppass_clean = "".join(c for c in GMAIL_APP_PASS if ord(c) < 128).strip()

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_clean, apppass_clean)
        server.sendmail(sender_clean, recipients, msg.as_bytes())


def send_email(html_content: str, week_of: str):
    """Send the weekly meal-plan email to all recipients."""
    recipients = [r.strip() for r in EMAIL_RECIPIENT.split(",") if r.strip()]
    subject = BRANDING.get("email_subject", "🍽️ Your Weekly Meal Plan — Week of {week_of}")
    subject = subject.replace("{week_of}", week_of)
    _send_mail(subject, html_content, recipients)
    print(f"✅ Meal plan sent to {len(recipients)} recipient(s) for week of {week_of}")


def send_failure_alert(error: Exception):
    """Email an admin alert when a run fails, so outages aren't silent.

    Goes only to ALERT_RECIPIENT (default: the sending account), never all
    recipients. Best-effort: if SMTP itself is broken this also fails, logged."""
    week_of = date.today().strftime("%B %d, %Y")
    alert_to = ALERT_RECIPIENT or GMAIL_SENDER
    recipients = [r.strip() for r in alert_to.split(",") if r.strip()]
    if not recipients:
        print("⚠️  No alert recipient configured; skipping failure email.")
        return

    err_type, err_msg = type(error).__name__, str(error)
    hint = ""
    if "credit balance is too low" in err_msg:
        hint = ("Your Anthropic API account is out of credits. Add a balance at "
                "console.anthropic.com → Settings → Billing (and consider OpenRouter "
                "fallback models in config.json so a single provider outage isn't fatal).")
    hint_html = (f'<p style="background:#fdecea;border-left:4px solid #c0392b;'
                 f'padding:12px 16px;margin:0 0 16px">{escape(hint)}</p>' if hint else "")
    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1a1a1a">
  <h2 style="color:#c0392b;margin:0 0 12px">⚠️ Meal plan did NOT send</h2>
  <p style="margin:0 0 16px">The weekly meal-plan job failed on {escape(week_of)}, so no plan went out this week.</p>
  {hint_html}
  <p style="margin:0 0 4px"><strong>Error</strong></p>
  <pre style="background:#f4f4f4;border-radius:6px;padding:12px 16px;white-space:pre-wrap;font-size:13px;margin:0 0 16px">{escape(err_type)}: {escape(err_msg)}</pre>
  <p style="font-size:13px;color:#666;margin:0">Open the GitHub Actions run for the full traceback, then re-run from the Actions tab.</p>
</div>"""
    _send_mail(f"⚠️ Meal plan FAILED — week of {week_of}", body, recipients)
    print(f"📧 Failure-alert email sent to {len(recipients)} recipient(s).")


def main():
    require_credentials()
    try:
        print("🔍 Fetching recipe sitemaps...")
        recipe_urls = get_recipe_urls()
        catalog = build_recipe_catalog(recipe_urls)
        recipe_sample = weekly_recipe_sample(catalog)
        print(f"📚 {len(catalog)} dinner recipes in catalog, {len(recipe_sample)} offered this week")

        history = load_meal_history()

        print("🥦 Generating weekly meal plan...")
        plan = generate_meal_plan(recipe_sample, history)
        print(f"✅ Plan generated for week of {plan['week_of']}")

        attach_recipe_links(plan, recipe_sample, recipe_urls)
        linked = sum(1 for m in plan["meals"] if m["recipe_source"] != "Search Recipes")
        print(f"🔗 {linked}/{len(plan['meals'])} meals link directly to a blog recipe")

        backup_labels = set(SECONDARY_SITES.values())
        n_backup = sum(1 for m in plan["meals"] if m["recipe_source"] in backup_labels)
        if n_backup > 1:
            print(f"⚠️  {n_backup} meals came from backup blogs (limit is 1) — model ignored the source priority rule this week.")

        print("🎨 Building HTML email...")
        email_html = build_html_email(plan)

        print("📧 Sending email...")
        send_email(email_html, plan["week_of"])

        save_meal_history(plan, history)
    except Exception as exc:
        # A run can die mid-way (no API credit, blogs unreachable, SMTP hiccup).
        # Email an alert, then re-raise so the Actions run still shows red and the
        # traceback is preserved in the logs.
        print(f"❌ Meal plan run failed: {type(exc).__name__}: {exc}")
        try:
            send_failure_alert(exc)
        except Exception as alert_exc:
            print(f"❌ Could not send failure-alert email either: {alert_exc}")
        raise


if __name__ == "__main__":
    main()
