# Project Bootstrap Super Prompt

> Paste this entire prompt into Claude Code at the start of any new project.
> Fill in the [PLACEHOLDERS] before pasting, or leave them and Claude will ask.

---

## PROMPT START — copy everything below this line

---

I want to build: **[DESCRIBE YOUR PROJECT IN 1-3 SENTENCES]**

I am learning as we go, so explain every concept you introduce. I want a production-quality workflow with full tracking of what we build and why.

**Before writing any code**, set up the following project structure and files. Use the exact file names and formats described. Ask me clarifying questions if you need to before creating anything.

---

## STEP 1 — Understand the project

Ask me these questions if I haven't answered them already:

1. What does this app do? (one sentence)
2. Who uses it?
3. What is the core technical challenge? (e.g. real-time, AI, high volume data)
4. What language/runtime? (Python, Node, Go, etc.)
5. Do you have a preferred stack, or should I propose one?
6. What's the rough scope? (weekend project / months-long / production SaaS)

Once you have answers, propose:
- The minimal stack (fastest to build, covers the requirements)
- The expert stack (production-grade, scalable, what a senior engineer would use)

Let me choose before proceeding.

---

## STEP 2 — Create the memory-bank

Create `memory-bank/` with these five files. Fill them based on what I told you.

### `memory-bank/projectbrief.md`
```
# Project Brief

**App:** [Name] — [Tagline]

**One-liner:** [What it does]

**Problem it solves:** [The pain point]

**Users:** [Who uses it]

**Core value:** [The killer feature / key insight]
```

### `memory-bank/techContext.md`
```
# Tech Context

## Versions & Packages

| Package | Version | Role |
|---|---|---|
[fill with chosen stack]

## Infrastructure
[databases, queues, services — local or cloud]

## Environment Variables
[list all .env keys with blank values]
```

### `memory-bank/systemPatterns.md`
```
# System Patterns

## Architecture Overview
[ASCII diagram of data flow from user action to response]

## Key Patterns
[List the non-obvious design decisions with one-line explanations]
```

### `memory-bank/activeContext.md`
```
# Active Context

> Update this file at the start and end of every session.

## Current Phase
**Phase 0 — Project Setup** (in progress)

## What Was Completed
- Nothing yet

## Next Steps (Phase 0)
1. [list the first 3-5 concrete tasks]

## Key Decisions (locked in)
[list the stack choices we locked in]

## User Context
- User is learning as we build — explain every new concept in chat AND save to learn.md
- Update log.md with every terminal command and code change
- Mark PLAN.md steps [x] as completed
```

### `memory-bank/progress.md`
```
# Progress

## Done
- [x] Problem statement understood
- [x] Stack chosen
- [x] memory-bank/ populated

## In Progress
- Phase 0: Project Setup

## Not Started
[list all phases for this project]

## Known Issues / Risks
[list anything we already know will be tricky]
```

---

## STEP 3 — Create CLAUDE.md

Create `CLAUDE.md` in the project root. This is your permanent rulebook. Adapt the content to the chosen stack, but always include these sections:

```markdown
# CLAUDE.md — [Project Name] Development Rules

> This file defines the rules Claude must follow throughout this project.
> Reference this before every code change.

## Stack Rules (Approved Libraries Only)

### [Layer 1, e.g. Backend]
- [library]: [role]. No [forbidden alternatives].
[repeat for each layer]

### [Layer 2, e.g. Frontend]
[same format]

### [Layer 3, e.g. Database]
[same format]

## Code Rules
- All [async context] must be async.
- No [anti-pattern].
- [Domain logic] lives exclusively in [file]. Nowhere else.
- No hardcoded strings — use constants or config.
- Environment variables via .env + python-dotenv (or equivalent). Never commit secrets.

## Development Rules
- Explain every concept as it is introduced (user is learning).
- Every concept explained in chat must also be saved to `learn.md` under the correct phase section.
- Log every terminal command in `log.md`.
- Log every code change phase by phase in `log.md`.
- Check `PLAN.md` before starting any task and mark steps complete.
- Check `memory-bank/activeContext.md` to know what's in progress. Update it at end of each phase.
- Never add a library not in the approved list without asking.
- Write minimum code needed. No premature abstractions.
- No test files unless explicitly asked.

## Folder Structure
[draw the folder structure for this specific project]
```

---

## STEP 4 — Create PLAN.md

Create `PLAN.md` in the project root. This is the master development checklist. Break the project into phases. Each phase is a coherent deliverable (not just "write some code"). Format:

```markdown
# PLAN.md — [Project Name] Development Checklist

> Check this before every session. Mark steps [x] as done.

## Phase 0: Project Setup
- [ ] Create folder structure
- [ ] Write requirements.txt / package.json
- [ ] Write .env.example
- [ ] Write docker-compose.yml (if applicable)
- [ ] Initialize git

## Phase 1: [First real phase]
- [ ] [concrete task]
- [ ] [concrete task]

[continue for all phases]

## Done When
- [ ] [End-to-end user journey works]
- [ ] [Core feature works]
- [ ] [Deployed or runnable locally]
```

---

## STEP 5 — Create tracking files

### `log.md`
```markdown
# Dev Log

> Record every terminal command and every code change here, phase by phase.
> Format: date, phase, what was done, command or file changed.

## [Today's date] — Phase 0

### Commands Run
(none yet)

### Files Created
(none yet)

### Files Changed
(none yet)
```

### `learn.md`
```markdown
# What I'm Learning

> Every concept explained during this build is saved here, under the phase it was introduced.
> Use this as your personal reference — written in plain English, not jargon.

## Phase 0 — Project Setup

(concepts will be added here as we build)
```

### `why.md`
```markdown
# Why We Chose These Tools

> Every non-obvious tool choice is justified here.
> So we never forget why we made a decision.

[Fill one entry per non-obvious choice:]

## [Tool name]
**Chosen over:** [alternatives]
**Why:** [1-2 sentence reason]
```

---

## STEP 6 — Begin Phase 0

After creating all files above:

1. Show me the complete folder structure you just created.
2. Explain the first 3 concepts I need to understand to start building (keep it short, plain English).
3. Save those 3 concept explanations to `learn.md` under Phase 0.
4. Log the file creation in `log.md`.
5. Ask: "Ready to start Phase 0 tasks?"

---

## Ongoing Rules (apply for the entire project)

- **Before every coding session:** read `memory-bank/activeContext.md` and `PLAN.md` to orient yourself.
- **After every phase:** update `memory-bank/activeContext.md`, mark `PLAN.md` steps done, add entries to `log.md`.
- **Every new concept introduced:** add it to `learn.md` under the current phase section.
- **Every terminal command:** log it in `log.md` with a one-line explanation of what it does.
- **Every file created or changed:** log it in `log.md`.
- **Never skip the logs.** They are the learning trail.

---

## PROMPT END

