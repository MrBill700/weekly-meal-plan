# AGENTS.md -- setting up and maintaining this meal planner

Instructions for an AI coding agent (or a person) setting this template up for a
household. Each step says what and why in plain words first; commands follow for
agents with a shell and the GitHub CLI (`gh`). Every step can also be done in
GitHub's web UI.

Ask the household before any step that spends money, sends email, or publishes
anything. Never put secrets in files; they go in repository secrets only.

## What this repo is

- `meal_planner.py` -- the weekly plan: picks dinners from the household's recipe
  blogs, asks Claude for a plan, emails it, and records it in `meal_history.json`.
  Runs from `.github/workflows/weekly_meal_plan.yml`.
- `podcast.py` -- an OPTIONAL weekly audio episode of the plan, published to a
  separate public website. Off until the household deliberately turns it on (part 2).
- `config.json` -- everything household-specific. `README.md` explains each field.
- `tests/` -- offline tests: `python -m pytest -q`. Also run on every push.

## Part 1 -- the weekly plan email

1. **Create the household's repo from this template.** Private is fine and
   usually best: ratings and notes in `meal_history.json` are visible to anyone
   who can see the repo.
   ```bash
   gh repo create <owner>/<name> --template MrBill700/weekly-meal-plan --private --clone
   ```
2. **Fill in `config.json` with the household.** Ask about: who's eating, location,
   recipe blogs they trust, hard dietary rules (allergies first), budget, equipment,
   which nights get dinners, takeout night, and stores. Keep the JSON valid.
3. **Preview the email without spending anything.**
   ```bash
   pip install -r requirements.txt pytest
   python tools/render_sample.py      # writes out/sample-email.html
   python -m pytest -q
   ```
4. **Add the secrets** (the household creates these accounts; you never see their
   passwords): a Gmail address plus a Gmail App Password, the recipient list, and
   an Anthropic API key. An OpenRouter key is optional (fallback model).
   ```bash
   gh secret set GMAIL_SENDER      --repo <owner>/<name>
   gh secret set GMAIL_APP_PASS    --repo <owner>/<name>
   gh secret set EMAIL_RECIPIENT   --repo <owner>/<name>
   gh secret set ANTHROPIC_API_KEY --repo <owner>/<name>
   gh secret set OPENROUTER_API_KEY --repo <owner>/<name>   # optional
   ```
   `gh secret set` prompts for the value -- let the household paste it.
5. **Run it once, with the household's OK** (it emails everyone on the list).
   ```bash
   gh workflow run "Weekly Meal Plan Email" --repo <owner>/<name>
   gh run watch --repo <owner>/<name>
   ```
   Check: the email arrived, and a "Record meal history" commit landed.

## Part 2 -- the optional podcast

> **Read this to the household first.** Turning the podcast on publishes the
> household's weekly dinners, as audio and text, on a **public website and RSS
> feed** that anyone can find. It can't be made private on a free GitHub plan. The
> episodes use AI-generated voices (the feed says so on every episode). It costs
> roughly $0.05-0.10 a week (OpenAI text-to-speech plus one script call). Only
> continue if they agree.

How it works: after each plan email, `.github/workflows/weekly_audio.yml` writes a
short script (one or two hosts, set in `config.json` under `"podcast"`), reads it
aloud with OpenAI TTS, and pushes the mp3 plus an RSS feed to a **second, public
repository** served by GitHub Pages. The household's main repo stays private.

1. **Create the public site repo, with one commit.** An empty repo can't be
   checked out by the workflow, so start it with a README.
   ```bash
   gh repo create <owner>/<name>-podcast --public --add-readme
   ```
2. **Turn on GitHub Pages for it**, serving the `main` branch, root folder.
   (Web UI: the site repo -> Settings -> Pages -> Deploy from a branch -> `main` / `/ (root)`.)
   ```bash
   gh api -X POST repos/<owner>/<name>-podcast/pages -f "source[branch]=main" -f "source[path]=/"
   ```
3. **Give the planner repo write access to the site repo, and nothing else.** A
   deploy key is an SSH key that works for exactly one repository. The public half
   goes on the site repo with write access; the private half becomes a secret in
   the planner repo. Delete the local key files afterwards.
   ```bash
   ssh-keygen -t ed25519 -N "" -C "podcast deploy" -f podcast_deploy_key
   gh repo deploy-key add podcast_deploy_key.pub --repo <owner>/<name>-podcast --allow-write --title "meal plan podcast"
   gh secret set PODCAST_DEPLOY_KEY --repo <owner>/<name> < podcast_deploy_key
   rm podcast_deploy_key podcast_deploy_key.pub
   ```
4. **Add the OpenAI key** (text-to-speech), from platform.openai.com.
   ```bash
   gh secret set OPENAI_API_KEY --repo <owner>/<name>
   ```
5. **Set up the show in `config.json` -> `"podcast"`**: title, description, and the
   one or two hosts. Each host has a `tag` (letters only, e.g. `HOST`), a spoken
   `name`, a `persona`, a TTS `voice`, and `delivery` notes. Use invented names;
   don't name real household members. Set `"tone_from_blogs": false` to stop
   reading the recipe blogs' intros as a tone reference.
6. **Try it in test mode -- nothing leaves the machine.** Needs ffmpeg. Test mode
   uses a canned script and silence, makes no API calls, and writes to a temp
   folder it prints.
   ```bash
   PODCAST_DRY_RUN=1 python podcast.py
   python -m pytest -q          # includes a test-mode end-to-end run
   ```
7. **Switch it on.** This repository variable is the podcast's only on/off
   switch: the workflows skip the podcast while it is unset, and the plan email
   shows "Listen" buttons only once it is set.
   ```bash
   gh variable set PODCAST_SITE_REPO --repo <owner>/<name> --body "<owner>/<name>-podcast"
   # Optional, only for a custom domain:
   # gh variable set PODCAST_BASE_URL --repo <owner>/<name> --body "https://pod.example.com/"
   ```
8. **First episode.** It follows the next plan email automatically. To make one now
   from the latest plan (with the household's OK -- this publishes):
   ```bash
   gh workflow run "Weekly Meal Plan Podcast" --repo <owner>/<name>
   gh run watch --repo <owner>/<name>
   ```
   Check: `https://<owner>.github.io/<name>-podcast/` lists the episode and
   `.../feed.xml` contains it. Subscribe in a podcast app with the feed URL.
9. **Optional: pick voices by ear.** The "Podcast Voice Samples" workflow
   publishes a page of every voice at `<site>/samples/` (a few cents). Put the
   chosen voices in `config.json`.

To turn the podcast off: delete the `PODCAST_SITE_REPO` variable
(`gh variable delete PODCAST_SITE_REPO --repo <owner>/<name>`). To remove what's
public, delete the site repo.

**Upkeep:** each episode adds about 1.5 MB to the site repo's history. After a year
or so, prune old episodes: delete old `episodes/*.mp3` files and their entries in
`episodes.json` (the feed lists the newest 52 anyway), or recreate the site repo.

## Rules for agents changing this code

- Run `python -m pytest -q` before committing. Tests are offline and use no secrets.
- Never run `python meal_planner.py` with real credentials just to test -- it emails
  the household and appends to the history. Use `tools/render_sample.py`.
- Never run `podcast.py` without `PODCAST_DRY_RUN=1` outside the workflow.
- Text that comes from websites or from `meal_history.json` goes into prompts only
  inside the `<<<BEGIN DATA ...>>>` markers (`_data_block`). Keep it that way: the
  podcast script becomes public audio.
- Keep `AI_DISCLOSURE` in the feed, the site page, and every episode.
- `meal_history.json` is hand-edited by the household (ratings). Never "repair" it by
  overwriting; a parse error must stop the run (it does).
