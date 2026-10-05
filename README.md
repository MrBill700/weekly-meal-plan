# 🍽️ Weekly Meal Plan Emailer

A self-running weekly dinner planner. Every week, GitHub Actions picks real
recipes from **your** favorite food blogs, builds a budget-aware plan tailored to
**your** household's tastes, and emails it to you — with a grocery list grouped by
store and a direct link to each recipe.

It's powered by Claude (with an optional cheaper fallback model), and **everything
personal lives in one file: `config.json`.** No coding required to make it yours.

> This is a template. Click **“Use this template”** (or fork it), edit
> `config.json`, add a few secrets, and you're done.

---

## What you get

- 📅 A fresh plan emailed on your schedule (default: Saturday morning)
- 🔗 Every dinner links to a real recipe on a blog you trust
- 🛒 Grocery list split by store, with budget tips, likely-on-sale flags, and swaps
- 🧾 Every dinner comes with an ingredient list (amounts for your household) and steps
- 🔁 Never repeats recent dinners -- and learns from your 1-5 ratings: favorites
  come back, flops never do
- 🌿 Uses what's in season near you
- 🛟 Falls back to a cheaper AI model if your primary one is down, and emails you
  an alert if a run ever fails

---

## Quick start (≈10 minutes)

### 1. Copy the template
Click **“Use this template” → Create a new repository** (or fork it). Make it
private if you like — GitHub Actions works either way.

### 2. Edit `config.json`
This is where you make it yours. See **[Configuration](#configuration)** below.
At minimum, change the recipe blogs, dietary rules, stores, and household info.

### 3. Get the credentials you'll need
- **Gmail App Password** — turn on 2-Step Verification, then create an
  [App Password](https://myaccount.google.com/apppasswords). (Your normal Gmail
  password won't work for SMTP.)
- **Anthropic API key** — from [console.anthropic.com](https://console.anthropic.com)
  → API Keys. Add a few dollars of credit under Billing.
- *(Optional)* **OpenRouter API key** — from
  [openrouter.ai](https://openrouter.ai) → Keys. This powers the cheaper fallback
  model so a single provider outage never kills your plan.

### 4. Add them as repository secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Required? | What it is |
|---|---|---|
| `GMAIL_SENDER` | ✅ | The Gmail address that sends the email |
| `GMAIL_APP_PASS` | ✅ | The 16-character Gmail App Password |
| `EMAIL_RECIPIENT` | ✅ | Who receives the plan (comma-separate for several) |
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key |
| `OPENROUTER_API_KEY` | optional | Enables the fallback model |
| `OPENROUTER_MODELS` | optional | Comma-separated fallback models (else uses `config.json`) |
| `ALERT_RECIPIENT` | optional | Where failure alerts go (defaults to `GMAIL_SENDER`) |
| `PRIMARY_MODEL` | optional | Override the model in `config.json` |

> 🔐 **Secrets are separate from your code.** Editing or pushing files never
> touches them. They live in repo Settings and persist until you change them.

### 5. Turn on Actions and test it
Open the **Actions** tab and enable workflows if prompted. Then run
**“Weekly Meal Plan Email” → Run workflow** to test immediately — you don't have
to wait for Saturday. Watch the log; you should get an email in a couple minutes.

---

## Configuration

Everything personal is in **`config.json`**. Edit it right on GitHub (pencil icon)
or locally. Here's what each section controls:

| Section | Controls |
|---|---|
| `household` | Your name/city (used in the email header), who you're cooking for, and your location (for seasonal produce) |
| `sources.primary` | Your go-to blogs — most dinners come from these. `"domain": "Display Name"` |
| `sources.secondary` | Backup blogs — at most one dinner/week unless your primaries are down |
| `diet.hard_rules` | Non-negotiables (allergies, "no fish", spice level) |
| `diet.preferences` | Softer steers (lower-carb, quick weeknights, whole foods) |
| `diet.exclude_keywords` / `exclude_tokens` | Recipes whose title contains these are filtered out *before* the AI sees them — use for hard dietary lines like seafood |
| `budget` | Your weekly grocery cap and a note |
| `equipment` | Your appliances — these become the allowed cooking methods |
| `schedule` | Which days get dinners, your takeout/night-off, and special rules (e.g. "grill on Sunday") |
| `stores` | The stores you shop, their brand color, what you buy there, and a buying strategy. Add/remove freely |
| `model` | Your primary Claude model + ordered OpenRouter fallbacks |
| `branding` | Email subject, header title, footer |
| `tuning` | Advanced knobs (how many weeks count as "recent", catalog sample size) |

### Adding your own blogs
Any WordPress-style food blog with a sitemap usually works — paste its domain
under `sources.primary` or `sources.secondary`:

```json
"sources": {
  "primary": {
    "mygofavoriteblog.com": "My Favorite Blog",
    "anotherblog.com": "Another Great One"
  }
}
```
The planner reads each site's post sitemap automatically. If a blog returns no
recipes, it may not expose a standard sitemap — try another.

### Changing the AI model
`config.json` → `model.primary`. `claude-opus-4-8` is a strong default;
`claude-fable-5` is the most capable (and most expensive). The
`openrouter_fallbacks` list (e.g. `deepseek/deepseek-v4-flash`) only kicks in if
your primary errors — set `OPENROUTER_API_KEY` to enable it. Browse model IDs at
[openrouter.ai/models](https://openrouter.ai/models).

### Changing the schedule
Edit the `cron` line in `.github/workflows/weekly_meal_plan.yml`. The default
`0 14 * * 6` is Saturday 14:00 UTC (7am US Pacific). Use
[crontab.guru](https://crontab.guru) to build your own.

---

## Rate your dinners
Every run records the week's dinners in `meal_history.json`, each with empty
`rating` and `note` slots. Fill them in (the email links straight to the file's
editor on GitHub):

```json
{"day": "Monday", "name": "Greek Chicken Bowls", "recipe_url": "...",
 "rating": 5, "note": "kids loved it -- double the tzatziki"}
```

The planner reads every rating ever given:

| Rating | What happens |
|---|---|
| **4-5** (favorite) | May come back once it's been off the menu 2 weeks -- at most 2 favorites a week. Its note is applied ("double the tzatziki"). |
| **3** (neutral) | Normal rotation; won't repeat within the recent-history window. |
| **1-2** (flop) | Never served again, nor close variants. |

Notes work without a rating too ("kids refused the mushrooms"). For lasting
rules ("no mushrooms ever"), use `diet` in `config.json` instead.

> ⚠️ **Keep the JSON valid.** If a hand edit breaks the file (usually a missing
> or extra comma), the run stops and emails you the line and column to fix --
> it never overwrites your ratings.
>
> 🔒 Ratings and notes are committed to the repo, so anyone who can see the
> repo can read them. Make the repo private if that matters.

---

## How it works (for the curious)

1. Fetches every post URL from your configured blogs' sitemaps.
2. Filters them down to plausible dinners (drops desserts, drinks, roundups, and
   anything in your dietary exclusions).
3. Draws a rotating weekly sample (seeded by the week, so re-runs match).
4. Hands Claude that sample + your rules + recent history + your ratings, and
   gets back a structured plan: each dinner with ingredient amounts and steps,
   plus a grocery list built from those ingredients. Scraped titles and your
   notes are fenced off as data, so text on a blog can't rewrite the
   instructions. If Claude errors, it retries on your OpenRouter fallbacks.
5. Renders an HTML email and sends it via Gmail.
6. Commits the week's meals to `meal_history.json` (with blank rating slots)
   so they aren't repeated.

`recipe_catalog.json` is a committed cache of blog URLs used as a safety net if
the blogs are unreachable on run day.

---

## Preview and test locally
No API key or email needed:

```bash
pip install -r requirements.txt pytest
python tools/render_sample.py     # writes out/sample-email.html from a sample plan
python -m pytest -q               # offline tests (also run on every push)
```

Re-render the sample after changing `config.json` (stores, branding, takeout
night) to see how the email will look.

---

## Troubleshooting

- **No email arrived** — check the Actions run log. Most failures are a missing
  secret or an empty API balance; the log says which.
- **“credit balance is too low”** — top up Anthropic, or add an `OPENROUTER_API_KEY`
  so the fallback covers you.
- **Recipes don't link / few catalog matches** — that blog may not expose a
  standard sitemap. Swap it for another.
- **"meal_history.json is not valid JSON"** -- a hand edit broke the file. The
  alert names the line and column; fix it on GitHub and re-run from Actions.
- **Run failed but you didn't notice** — you should get a failure-alert email; set
  `ALERT_RECIPIENT` if you want it to go somewhere specific.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, share it. 🍴
