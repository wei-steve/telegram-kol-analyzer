# Phase-Scoped Production Authorization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace repeated per-action authorization gates with one phase-scoped execution rule while preserving audit facts and technical safety checks.

**Architecture:** Define the policy once in `AGENTS.md`, mirror it once in the current Deepcoin status, and delete superseded forward-looking authorization lists. Keep historical records of actions that did or did not occur.

**Tech Stack:** Markdown, Git

---

### Task 1: Define the project-wide phase scope

**Files:**
- Modify: `AGENTS.md`

1. Add the phase-scoped authorization rule under Project Workflow.
2. Replace separate push/stage/activation wording with exact-SHA execution wording.
3. Make L3 exchange semantics require inclusion in the approved phase, not a separate message.

### Task 2: Remove repeated Deepcoin authorization lists

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

1. Add one current phase-scoped execution-policy section.
2. Remove or compress repeated future-action authorization lists.
3. Preserve historical no-action evidence and unknown/freshness rules.

### Task 3: Verify and commit

**Files:**
- Verify: `AGENTS.md`
- Verify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

1. Search for remaining separate/per-action authorization language.
2. Run `git diff --check`.
3. Inspect the exact diff and stage only the four documentation paths.
4. Commit the documentation cleanup without push, deployment, or production action.
