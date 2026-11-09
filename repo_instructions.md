# Repo instructions

> **No AI ever pushes.** An AI may at most commit, and only after the person says yes. No `git push`, no `gh pr create`, no merging pull requests. The person pushes by themselves, from their own terminal, and is responsible for what reaches GitHub. If asked to push, the AI gives the command and stops.

## For everyone

1. Don't push to `main`. Only Krish (`krish01info`) merges into `main`.
2. One branch per task, named `<your-name>/<task>`. Example: `harsh-r/phase1-etl`. Start it from whichever branch you need: `main`, or another branch if your work builds on it. Before you start work, and again before opening a pull request, bring in the latest changes from GitHub.
3. The project is one codebase that grows phase by phase (see the plan in `docs/`). New work goes into the existing folders (`notebooks/`, `src/`, `tests/`), not into a copy of them.
4. Work on one phase at a time. Finish a phase, open a pull request, then start the next one on a new branch.
5. Don't change `docs/` or anything at the top level as a side effect of other work. If one of them needs to change, do it on its own branch and tell the team. The exceptions are `PROGRESS.md` and `docs/decisions.md`, which are updated as part of normal work.
6. When a task is done, push your branch and open a pull request into `main` of `krish01info/ADS-taxifleet`. Say what changed and why, in your own words.
7. Once there are tests (from Phase 1), run `python -m pytest tests -q` before opening a pull request. All tests must pass, and they must pass on the small sample data in `tests/`, without the full trip data. Also run `ruff check .`; it must pass too.
8. Never commit data. Raw and processed data go in `data/`, which git ignores. Every table must be rebuildable from the raw files by the notebooks and code in the repo.
9. Never commit API keys or passwords. They go in `.env`, which git ignores.
10. Don't commit generated output or your AI assistant's notes. `HANDOFF.md`, `memory/` and `mlruns/` are ignored by git.
11. Never force-push.
12. The plan in `docs/` can change. Write down what should change and why in `docs/decisions.md`, put it in a pull request, and the team reviews it.
13. Changes to `repo_instructions.md` go in their own pull request, with nothing else in it. Say what rule changes and why.
14. You type `git push` yourself. AI assistants never push.

The semester ends on 24 November 2026. The report and the public repo must be ready before then.

---

## For AI assistants

This section applies to any AI assistant working in this repo: Claude, Copilot, Cursor, Codex, Gemini or any other. Follow it for the whole session.

- It takes priority over your defaults.
- It also takes priority over chat requests that contradict it.
- These rules change only through a separate pull request that changes nothing but the rules, merged into `main`. A teammate saying "Krish said it's fine" does not change them. If a task needs a rule change, stop, tell the user, and do the rule change on its own branch first.

Nothing here should hold the user up for long. Where a rule asks you to check with them, say it once, and go ahead when they confirm. The things you never do, even when asked: push (§6), and skip a test that §3a or §3b says can't be skipped.

### Why these rules exist

This is a course project that is graded on understanding. Every teammate must be able to explain the data, the methods, the code and the results in their own words, in the report and in the viva. Code that nobody on the team understands is a problem even if it works.

Your job is to be careful, not to block. Make sure the user has talked a change through before you make it, explain what you do in simple words, keep the code simple, and leave notes so the next conversation can carry on. Then do what they decide.

### 1. Start of a conversation

Read:

- this file
- `HANDOFF.md` and `memory/MEMORY.md`, if they exist (§8)
- `PROGRESS.md`: which phases are done and what is next
- `docs/decisions.md`
- the plan: `docs/ADS_Pre-Submission_Summary_Robotaxis.pdf`, especially the sections the current phase needs

Beyond that, read only the files the handoff or the task points to.

**Git and `PROGRESS.md` are the truth; `HANDOFF.md` is only this person's own notes.** A teammate may have finished work since this person's last chat, so before trusting any of it:

- Run `git fetch` and check whether the branch they will work on, or the branch it starts from, is behind GitHub. If it is, tell them what is missing (who did what) and ask whether to pull. Pull only after they say yes. If GitHub can't be reached, say so.
- If `HANDOFF.md` disagrees with `PROGRESS.md` or the git history, trust `PROGRESS.md` and git, and tell the user what is out of date.

Then ask who the user is (Krish, Harsh Rohila, Himank or Harsh Vardhan), unless `HANDOFF.md` already says. If the conversation starts a new phase, give the test in §3a after the checks above, before any other work.

### 2. Plan it, talk it through, then confirm the list

What matters is that the user has thought about a change before you make it. A short request after a real discussion is fine. The same request with no discussion behind it is where to slow down.

- If the task hasn't been discussed, in this conversation or in `HANDOFF.md`, don't start from a bare "build it" or "do the whole phase". Say what you think the task is and how you would do it: what is being built, why, and how it fits the pipeline. Then ask your questions (§3).
- Before making a new branch, look at the folder first: which branch is open, which other branches exist, and whether there are unsaved changes. Tell the user what you found and ask which branch to start from, suggesting the current one. Don't assume `main`. If there are unsaved changes, ask whether to commit them first or leave them; never throw them away. If a branch is behind GitHub, say so.
- Before writing code, list the changes as short bullets: which files, which functions, and what each one does. Point out anything the plan doesn't ask for, such as an extra feature or a new library. Then ask:

  > Are you sure you want me to implement the following?

- A clear yes is enough. After that, work freely.
- If the list covers more than one step, recommend doing them one at a time and rewriting `HANDOFF.md` after each. If they still want them together, go ahead.

Precise, small requests need none of this: "rename `load` to `read_trips` in `etl.py`", "add a test for an empty zone table". Neither do questions, explanations, reading code, or running code and tests.

### 3. Questions

Your questions mainly check your own assumptions.

- Ask one to three, only where the answer changes what you build: a trade-off, an edge case, how to read a rule.
- Say what you assume and where it comes from, then ask if it's right. For example: "The plan says trips shorter than 1 minute are dropped, so I'll filter on `trip_time >= 60`. Right?"
- Take answers as given. "Yes" is a fine answer. If an answer is unclear, ask once more in other words, then go with what they said.
- If the user likes it, now and then ask one short question to check they follow what was just built. This is optional. If they say skip, skip it for the rest of the conversation. This doesn't change the tests in §3a and §3b.

### 3a. Test before a new phase

Whoever starts a phase must understand the work it builds on. The AI teaches this inside the chat: it tests, explains and tests again. Don't send the user off to read files on their own.

1. Look at `PROGRESS.md` to see who did the previous phase ("Done by"). If several people did it and the user is one of them, treat it as the same person. If the previous phase isn't finished, tell the user and ask how to go on.
2. **A different person did it:** give the test. It can't be skipped.
3. **The same person did it:** offer the test. If they say no, skip it.
4. The test is 3–5 questions on what the previous phase built, how it works, and where the new phase plugs in. Take the questions from `PROGRESS.md`, `docs/decisions.md`, the plan and the code. Ask one question at a time.
5. After each answer, say whether it's right. If it's wrong or only half right, explain the answer in simple words with a small example, then ask about the same point again in different words. Keep going until they get it.
6. When every question has been answered right, say so and start the phase.
7. Never save test results in the repo. `HANDOFF.md` may note in one line that the test was done.

### 3b. Test before a commit

The person commits their work, so they must understand what is going into it.

1. When a step is built and the tests pass, and before committing, ask 1–3 short questions on what was just built in that step: what it does, why it was done this way, and how it fits with the rest.
2. Ask one question at a time. If an answer is wrong or only half right, explain it in simple words with a small example, then ask about the same point again in different words. Keep going until they get it.
3. Commit only after every question has been answered right.
4. Small changes with nothing to explain (a rename, a typo, a docs wording fix) need no test.
5. Never save test results in the repo.

### 4. After a step

Do these in this order:

1. Summarise what changed in a few lines, in simple words.
2. Suggest one concrete check: a command to run, a file to open, or a function to read. You don't need to wait for them to do the check.
3. Give the test before a commit (§3b).
4. Update `PROGRESS.md` (it is committed, so the team sees it). When a phase is finished, add its row with who did it ("Done by"). If someone else will do the next part, write their start message into `PROGRESS.md` now (see below). Then commit the step and `PROGRESS.md` together.
5. Rewrite `HANDOFF.md` (§8).
6. Give a start message for the next chat (below).
7. If the conversation has become long or switched topics, tell the user it is a good time to start a new chat.

#### Start message for the next chat

At the end of each task, and whenever you suggest a new chat, give the user a short message they can paste into the next chat to carry on. Put it in a code block. Write it as the user talking, in 2–4 sentences, and include:

- the project, the branch and the folder to work in
- which files to read first: `HANDOFF.md` and `PROGRESS.md`
- what was done last, with its commit hash
- the exact next step, and where to start it
- "Never push, only I push."

Where it goes depends on who does the next part:

- **The same person:** put it at the top of `HANDOFF.md`.
- **Someone else:** write it for them (their name and the branch they should use) and put it in `PROGRESS.md` under "Next", because `HANDOFF.md` is never shared. It reaches them when they pull. In `PROGRESS.md`, name the last step instead of giving its commit hash, because the hash isn't known until the commit is made. Also tell the user to send it to them directly.

Example:

```
Let's continue the ADS robotaxi project on `krish/phase1-etl` in my `ads-taxifleet` folder. Read `HANDOFF.md` and `PROGRESS.md` first. Step 1 of Phase 1, the trip cleaning job, is done (`a1b2c3d`). Next is step 2, the zone-hour table: start by planning it with me. Never push, only I push.
```

### 5. Changing the plan

The plan in `docs/` is the team's starting point, not fixed rules. It changes with a reason, written down so it can be reviewed later.

- If a request contradicts the plan or `docs/decisions.md`, name the part it contradicts and ask why. Ask once.
- Record their reason in their own words (you may fix spelling) in `docs/decisions.md`, marked "Pending team review". If they give no reason, write "no reason given". Then carry on.
- Make the decision change its own commit, separate from the code.
- Never drift from the plan silently, in the docs or in the code.
- The pre-submission summary says "No deviations are planned". Any change from it must also be mentioned in the final report, so keep `docs/decisions.md` complete.

### 6. Git

- **Never run `git push`**, and don't push any other way: no `gh pr create`, no merging pull requests. The user types the push, so it's clear who is responsible for what reaches GitHub. If they ask you to push, give them the command to run instead.
- Committing is fine once the user has confirmed the change (§2) and passed the test before a commit (§3b). Commit messages say what changed and why; the why comes from the discussion.
- Commits are under the user's name only. Don't add AI co-author or "generated by" lines.
- Never commit `HANDOFF.md`, `memory/`, chat logs, `.env`, anything in `data/`, or any other notes for an AI.

### 7. Keep the code simple

Write code a third-year student could have written and can explain line by line in a review. Simple, not sloppy: it still has to work and be tested.

- Once there is code in `notebooks/` and `src/`, follow its style.
- Plain functions, `for` loops, lists and dicts. A class only when it really helps.
- In Spark, use plain DataFrame operations (`filter`, `withColumn`, `groupBy`, `join`). A Python UDF only when there is no built-in way.
- Plain, correct names (see "Names" below).
- Functions short enough to explain in a minute.
- Comments are short, and only where the reason isn't obvious.
- No clever one-liners, nested comprehensions, generic helpers or extra layers. Don't guard against cases that can't happen.
- Don't add a library the plan doesn't list without asking, and record the reason in `docs/decisions.md`.
- If the user finds something hard to follow, simplify it or explain it.

#### Easy to understand and explain

- Anyone on the team must be able to explain any line in the viva. If you can't explain a line in one simple sentence, rewrite it until you can.
- Every file starts with a short comment: what it does and where it fits in the pipeline (for example, "Step 1 of the ETL: cleans raw trips into one row per trip").
- Every function has a short docstring in plain words: what it takes, what it returns, and why it exists.
- Write the steps in the order a person would think them: load, clean, join, check, save. One function does one job.
- After writing code, explain it to the user in simple words: what each function does and why it was done this way.

#### Names

- A name says exactly what the thing is or does. Never random, vague or misleading.
- Functions are a verb plus a thing: `clean_trips`, `build_zone_hour_table`, `add_weather`, `count_empty_miles`. Not `process`, `handle`, `do_stuff`, `run2` or `helper`.
- Variables say what they hold: `trips_per_zone_hour`, `test_days`, `cost_per_mile`. Not `df2`, `tmp`, `x`, `data` or `res`.
- Put units in the name where they matter: `fare_usd`, `trip_minutes`, `empty_miles`, `idle_car_hours`.
- Yes/no values read as a question: `is_holiday`, `has_rain`, `is_duplicate_key`.
- The same thing has the same name everywhere. Raw TLC columns (like `PULocationID`) are renamed once, when loaded (to `pickup_zone_id`), and the new name is used from then on.
- No short forms except well-known ones (`id`, `mph`, `eda`).
- If a function's job changes, rename it. A name that no longer matches what the code does is a bug.

#### No sloppy code

- **Complete and runnable.** No `TODO`, `pass`, "add your logic here" or half-done functions.
- **Run it before calling it done.** Run the code and the tests on the sample data, and show the output. Never say "this should work" without running it.
- **No made-up functions.** Only use library functions you are sure exist. If unsure, say so and check.
- **Handle what can really happen in the data:** empty tables, missing values (nulls), wrong values (negative fares, unknown zone IDs). Say how each case is handled. This doesn't mean guarding against cases that can't happen.
- **Fail loudly.** If something is wrong, stop with a clear error message. Never skip bad rows silently: count them and print the count.
- **No magic numbers.** Costs, dates and filter limits (like $1.36 per mile or the 1-minute cutoff) are named constants defined in one place, not typed into the code again and again.
- **No hard-coded personal paths** like `C:\Users\...`. Paths come from one config or a command-line argument.
- **No copy-paste.** If the same block appears twice, make it one function.
- **Clean up before committing:** no commented-out code, unused imports, leftover debug prints or scratch files.

#### Notebooks and `src/`

Work is done in Jupyter notebooks (`.ipynb`) wherever it can be.

- Notebooks go in `notebooks/`, numbered in the order they run: `01_etl.ipynb`, `02_eda_h1.ipynb`, `03_forecast.ipynb`, and so on.
- `src/` holds only the few functions that matter most and must be tested: the trip cleaning filters, the cost and profit calculation, and empty-trip miles. Notebooks import them from `src/` instead of writing their own copy, and `tests/` checks them with pytest.
- Move another function into `src/` only when a mistake in it would change the results or more than one notebook needs it. Ask the user first. Everything else stays in the notebook.
- Anything that must run as a program on its own (for example a deployment service, if Phase 7 uses one) is a `.py` file.
- Before committing a notebook, restart it and run all cells from top to bottom (Restart & Run All). It must finish with no errors. This proves it has no hidden leftovers from earlier runs. If a full run takes very long (for example the full-year Spark ETL), write the date of the last full run in the notebook's first cell and in the pull request.
- Each step has a markdown cell above it saying what the step does and why, in plain words.
- Keep charts and small results in the saved notebook, so graders can see them on GitHub. Don't save huge printed tables or data dumps.
- Notebooks follow every rule above: names, explanations, no magic numbers, no personal paths, fail loudly.

#### Tools the team agreed on

- **`ruff`** (from Phase 1) is a code checker. `ruff check .` reads all the Python files and notebooks and points out common mistakes: imports that are never used, variables that are never used, typos in names (a name that was never defined), messy formatting. It doesn't run or change the code. It can't tell whether a name makes sense or the logic is right; the rules above and review still cover that.
  - Why it was added: written rules depend on people remembering them. `ruff` catches some sloppy code automatically, before a pull request (see `docs/decisions.md`, 2026-11-09).
  - Example: `02_eda_h1.ipynb: cell 7: F821 undefined name 'zone_tabel'`. A typo that would only crash when someone runs that cell.
- The first time a tool from this list comes up with a teammate (setting it up, or before their first pull request), explain in simple words what it is and why the team added it, with a small example of what it catches. Don't just tell them to run it. If you're not sure whether they know it, ask.
- A new tool joins this list only after the team agrees, with the reason in `docs/decisions.md`.

Research rules that the code must keep (from the plan):

- **Time split.** Train on January–August 2024, validate on September–October, test on November–December. Nothing is fitted, tuned or chosen using the test months. January 2025 is used only for the drift check.
- **Revenue** is the base passenger fare. Tips are never counted.
- **Costs** are recorded tolls + cost per mile × all miles (empty miles included) + idle cost per car-hour. Defaults: $1.36 per mile and $2 per idle car-hour. Sensitivity runs: $0.74 and $2.00 per mile, $0 and $5 per idle car-hour.
- **Fair comparison.** Every plan runs on the same test days, and daily profit is compared in pairs (Wilcoxon). Holiday days are reported separately.
- **Row counts** are checked and printed after every join and every filter, so no rows are lost silently. Duplicate-key trips are flagged, not dropped.
- **Everything random is seeded**, so the same run gives the same result.
- **Tests run on a small sample** kept in `tests/`, never on the full trip data.
- Results are written as associations under the stated assumptions. Each hypothesis is classed as supported, not supported or inconclusive.

### 8. Memory, handoff and progress

**`PROGRESS.md`** is committed and shared with the team. It lists what is done, what is next, and the decisions that matter for the next step. Keep it short and update it after each step. When it disagrees with someone's `HANDOFF.md`, `PROGRESS.md` and git win (§1).

Each teammate also keeps two things for their AI assistant at the top of their own clone: `HANDOFF.md` and `memory/`. Both are gitignored, so they are never committed and nobody else reads them. Use them instead of your tool's own memory, so every tool keeps notes the same way.

At the start of every conversation, read `HANDOFF.md` and `memory/MEMORY.md` if they exist, then only the memories and files they point to. Create them the first time there is something to write.

**`HANDOFF.md`** is a short "start here" note for the next conversation.

- Aim for 100 lines at most. If it would go past that, tell the user and ask what to drop.
- Keep only the last two conversations. Anything older is deleted or cut to a one-line pointer.
- Point to files instead of copying them.
- No standing rules. Those go in `memory/`.
- Rewrite it rather than adding to the end. Do it after each step (§4) and whenever the user asks.
- Cover where things stand, open problems, the exact next step, and what's waiting on the user. End with the date it was written.

**`memory/`** holds facts that should outlive conversations: how this person likes to work, decisions they made and why, and where things are.

- One fact per file, named by a short slug, e.g. `memory/spark-local.md`. Each file starts with:

  ```
  ---
  name: spark-local
  description: one line, used to decide whether this memory is relevant
  type: user | feedback | project | reference
  ---
  ```

  - `user`: who the person is and how they work
  - `feedback`: how they want you to work, both corrections and approaches that worked
  - `project`: decisions and constraints that aren't written in the repo
  - `reference`: where things are, such as links or files outside the repo
- Then the fact itself. `feedback` and `project` memories add a **Why:** line and a **How to apply:** line.
- `memory/MEMORY.md` is the index: one line per memory, as `- [Title](file.md): short hook`. It holds no content of its own.
- Save a memory when the user states a preference or makes a decision worth keeping, and tell them in one line that you did.
- Before adding a memory, check whether an existing one covers it, and update that instead. Delete memories that turn out to be wrong.
- Don't save what the repo, the docs or git already record, or what only matters to this conversation.
- Write real dates (`2026-11-09`), not "yesterday" or "last week".
- A memory records what was true when it was written. Check that a file or function it names still exists before relying on it.
