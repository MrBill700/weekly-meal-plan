# HANDOFF

Session-close ledger for maintainers of this template. Newest entry first.
Setup and code rules live in AGENTS.md; field reference in README.md.

## [2026-10-05] session close
- Done: batch 1 (02f4018) -- 1-5 ratings + notes in meal_history.json replace
  feedback.txt, per-meal serves + ingredients, prompt data fencing, corrupt-history
  hard error, first offline tests + Tests workflow. Podcast port (3978df0, 3527dd6)
  -- optional weekly podcast, off until the PODCAST_SITE_REPO repo variable is set;
  seasonal_produce; week_of pinned in code; AGENTS.md setup guide.
- Verified: GitHub Tests 54 passed, 0 skipped on 3527dd6 (includes the podcast
  test-mode end-to-end run). No live run here by design: the workflows skip in this
  template repo. The same changes run live in a private instance.
- Open: nothing in flight.
- Next: batch 2 -- structured cook / leftovers / flex nights with explicit leftover
  references, a copy-paste plain-text grocery list, and one-off requests as a
  structured constraint that expires after its target week (not a free-text file).
  Then batch 3: night-before prep / thaw reminders, pantry + freezer inventory.
- Watch: before any change, re-run `python -m pytest -q`; keep the public repo free
  of household names, repo names, and URLs (scan every tracked file before pushing).
