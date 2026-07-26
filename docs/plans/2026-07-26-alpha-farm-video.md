# Alpha Farm Project Video Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a polished three-minute 1920×1080 Chinese explainer video titled《Alpha 牧场》that shows how paid KOL signals flow through AI recognition, risk-based sizing, two-leg entry, Deepcoin execution, and safety controls.

**Architecture:** Build the video as a modular HyperFrames project under `videos/alpha-farm/`, with one external HTML composition per narrative scene and a root composition that owns timing, narration, captions, music, and transitions. Real KOL assets and message excerpts remain in ignored local-only directories; committed source contains the visual system, animation code, redacted data manifest, scripts, and documentation needed to reproduce the video.

**Tech Stack:** HyperFrames 0.7.72, HTML, CSS, GSAP, FFmpeg, Kokoro TTS, Python 3, pytest

---

## Required Skills

- `@hyperframes:hyperframes`
- `@hyperframes:hyperframes-cli`
- `@hyperframes:gsap`
- `@systematic-debugging` if lint, inspection, preview, or render fails
- `@requesting-code-review` before final delivery

## Task 1: Scaffold the Secure Video Workspace

**Files:**

- Create: `videos/alpha-farm/index.html`
- Create: `videos/alpha-farm/DESIGN.md`
- Create: `videos/alpha-farm/README.md`
- Create: `videos/alpha-farm/compositions/`
- Create: `videos/alpha-farm/assets/placeholders/`
- Create: `videos/alpha-farm/data/kol-showcase.example.json`
- Modify: `.gitignore`

**Step 1: Add privacy-safe ignore rules**

Append these exact rules to `.gitignore`:

```gitignore
# Alpha Farm local-only video inputs and outputs
videos/alpha-farm/assets/private/
videos/alpha-farm/audio/generated/
videos/alpha-farm/data/private/
videos/alpha-farm/renders/
videos/alpha-farm/.hyperframes/
```

**Step 2: Scaffold the HyperFrames project**

Run:

```bash
npx hyperframes init videos/alpha-farm --non-interactive
```

Expected: `videos/alpha-farm/index.html` exists and HyperFrames reports a created composition.

**Step 3: Write the approved visual identity**

Create `videos/alpha-farm/DESIGN.md` with:

```markdown
# Alpha Farm Visual Identity

## Style Prompt

A dark, precise AI trading control room with restrained futuristic motion. KOL avatars and Telegram messages behave like trusted data sources flowing into a central reasoning engine. The tone is technically credible with brief dry humor, never casino-like or hyperbolic.

## Colors

- Canvas: #070A12
- Electric blue: #35C2FF
- AI violet: #8B5CF6
- Safety green: #2FE6A6
- Risk red: #FF4D67
- Primary text: #F4F7FF

## Typography

- Chinese display/body: PingFang SC, Source Han Sans SC fallback
- Latin display: Space Grotesk
- Data: a legible monospace with tabular numerals

## Motion

- Medium-energy data pushes as the primary transition
- Scanning, field locking, line tracing, and controlled number rolls
- One distinctive chromatic/data pulse at the opening
- Gentle color dip to black at the ending

## What NOT to Do

- No cash rain, sports cars, chips, or guaranteed-profit imagery
- No dense meaningless candlestick wallpaper
- No cheap full-screen neon gradients
- No cartoon livestock as the main visual language
- No small terminal text that cannot be read at 1080p
```

**Step 4: Create the safe manifest template**

Create `videos/alpha-farm/data/kol-showcase.example.json`:

```json
{
  "kol_count": 12,
  "membership_usd_per_six_months": 1000,
  "featured": [
    {
      "display_name": "KOL 示例",
      "style_label": "低频趋势",
      "avatar_file": "assets/private/avatars/example.png",
      "message_excerpt": "BTC 62000-62400 做多，止损 61500，目标 63500 / 64800",
      "entry_range": [62000, 62400],
      "stop_loss": 61500,
      "take_profit": [63500, 64800]
    }
  ]
}
```

**Step 5: Verify the scaffold**

Run:

```bash
npx hyperframes lint videos/alpha-farm
git check-ignore videos/alpha-farm/assets/private/test.png
git check-ignore videos/alpha-farm/renders/test.mp4
```

Expected: HyperFrames finds the project; both test paths are ignored.

**Step 6: Commit**

```bash
git add .gitignore videos/alpha-farm
git commit -m "feat: scaffold Alpha Farm video"
```

## Task 2: Produce the KOL Candidate Report

**Files:**

- Create locally: `videos/alpha-farm/data/private/kol-leaderboard.json`
- Create locally: `videos/alpha-farm/data/private/candidate-notes.md`
- Reference: `src/telegram_kol_research/reporting.py`
- Reference: `src/telegram_kol_research/analytics.py`
- Reference: `config/groups.yaml`

**Step 1: Confirm the production database is available**

Run:

```bash
find data -maxdepth 2 -type f \( -name '*.db' -o -name '*.sqlite' \) -print
```

Expected: at least one local research database is listed. If none is present, use the server update/runbook workflow to obtain a read-only report instead of copying the production database into Git.

**Step 2: Generate the existing project report**

Run:

```bash
mkdir -p videos/alpha-farm/data/private
PYTHONPATH=src python -m telegram_kol_research.cli report \
  --output-path videos/alpha-farm/data/private/kol-leaderboard.json
```

Expected: a JSON report with per-KOL samples and outcome statistics.

**Step 3: Build a 3–4 KOL shortlist**

In `videos/alpha-farm/data/private/candidate-notes.md`, record for each candidate:

- display name;
- group name;
- usable strategy sample count;
- evidence of restrained posting;
- whether entry, stop, and targets are explicit;
- a representative message ID;
- visual role in the video.

Select candidates for sample quality and style diversity, not win rate alone.

**Step 4: Verify no secrets entered the committed tree**

Run:

```bash
git status --short videos/alpha-farm/data
git check-ignore videos/alpha-farm/data/private/kol-leaderboard.json
```

Expected: private report and notes are ignored.

**Step 5: User checkpoint**

Present the 3–4 proposed KOL names and sanitized representative messages to the user. Do not continue with final real assets until the shortlist is approved.

## Task 3: Collect and Sanitize Real KOL Assets

**Files:**

- Create locally: `videos/alpha-farm/assets/private/avatars/`
- Create locally: `videos/alpha-farm/assets/private/messages/`
- Create locally: `videos/alpha-farm/data/private/kol-showcase.json`
- Create: `videos/alpha-farm/scripts/check_private_content.py`
- Test: `videos/alpha-farm/tests/test_check_private_content.py`

**Step 1: Write the failing privacy-check tests**

Test these cases:

```python
def test_rejects_phone_number():
    assert find_sensitive_tokens("联系 +86 13800138000") == ["phone"]


def test_rejects_telegram_invite_link():
    assert find_sensitive_tokens("https://t.me/+secretinvite") == ["telegram_invite"]


def test_accepts_strategy_prices():
    assert find_sensitive_tokens("BTC 62000-62400 SL 61500") == []
```

**Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest videos/alpha-farm/tests/test_check_private_content.py -v
```

Expected: FAIL because `find_sensitive_tokens` does not exist.

**Step 3: Implement the minimum checker**

Implement `find_sensitive_tokens(text: str) -> list[str]` with explicit patterns for:

- international and mainland Chinese phone numbers;
- `t.me/+...` and `joinchat` links;
- strings containing API key, secret, passphrase, or authorization labels;
- email addresses;
- long exchange order IDs when not allowlisted as display fixtures.

Add a CLI that scans JSON, Markdown, HTML, and text files under a supplied path and exits non-zero on a match.

**Step 4: Run tests**

Run:

```bash
python -m pytest videos/alpha-farm/tests/test_check_private_content.py -v
```

Expected: PASS.

**Step 5: Export or capture approved assets**

Prefer the local database and Web workbench. Use Telegram only for missing avatars or source messages, with the user logged in and the approved shortlist in hand.

For every selected message:

- crop unrelated members and navigation;
- remove usernames, phone numbers, invite links, balances, API data, and order IDs;
- retain KOL avatar, public display name, message wording, entry, stop, and targets;
- store raw captures only under ignored `assets/private/`.

**Step 6: Run the privacy checker**

Run:

```bash
python videos/alpha-farm/scripts/check_private_content.py \
  videos/alpha-farm/data/private/kol-showcase.json
```

Expected: `No sensitive tokens found`.

**Step 7: Commit only the checker**

```bash
git add videos/alpha-farm/scripts/check_private_content.py \
  videos/alpha-farm/tests/test_check_private_content.py
git commit -m "test: add Alpha Farm privacy guard"
```

## Task 4: Lock the Narration and Shot Timing

**Files:**

- Create: `videos/alpha-farm/content/narration.zh-CN.txt`
- Create: `videos/alpha-farm/content/shot-list.md`
- Create: `videos/alpha-farm/content/captions.json`

**Step 1: Write the narration**

Write approximately 650–750 Chinese characters, targeting 170–180 seconds at a calm AI voice pace.

The script must include these approved lines:

```text
我没有雇交易员，但我有十几个 KOL，全天替我寻找机会。
这些优质 KOL 群，每一个半年的费用都接近一千美元。
没有止损，就没有仓位；没有止损，就不执行交易。
贵的是信息，难的是执行。让 KOL 提供 Alpha，让 AI 负责执行。
```

Avoid:

- guaranteed-profit claims;
- implying every KOL trade wins;
- implying the system removes trading risk;
- disclosing real account size.

**Step 2: Create the shot list**

Map all nine scenes to exact intended time ranges:

```text
00.0–15.0  Opening
15.0–35.0  Cost wall
35.0–55.0  System pipeline
55.0–80.0  KOL roster
80.0–105.0 AI parsing
105.0–135.0 Risk sizing
135.0–160.0 Two-leg entry
160.0–172.0 Safety gate
172.0–180.0 Outro
```

For each range, specify narration, hero frame, on-screen text, assets, entrance choreography, and transition.

**Step 3: Review reading density**

Run:

```bash
wc -m videos/alpha-farm/content/narration.zh-CN.txt
```

Expected: roughly 650–750 Chinese characters.

**Step 4: Commit**

```bash
git add videos/alpha-farm/content
git commit -m "docs: script Alpha Farm narration"
```

## Task 5: Install and Generate Chinese AI Voice

**Files:**

- Create locally: `videos/alpha-farm/audio/generated/narration.wav`
- Create locally: `videos/alpha-farm/audio/generated/voice-samples/`

**Step 1: Install optional TTS dependencies**

Run:

```bash
brew list espeak-ng >/dev/null 2>&1 || brew install espeak-ng
python3 -m pip install kokoro-onnx soundfile
npx hyperframes doctor
```

Expected: TTS and Mandarin phonemization checks are available. Missing MusicGen is acceptable because AI-generated background music is not required.

**Step 2: List Mandarin voices**

Run:

```bash
npx hyperframes tts --list | rg '(^|\\s)z[a-z]?_'
```

Expected: one or more Mandarin `z...` voices are listed.

**Step 3: Generate short voice auditions**

Generate the opening sentence with the best 2–3 Mandarin female voices at speeds 0.90, 0.95, and 1.00. Save them under `audio/generated/voice-samples/`.

**Step 4: User checkpoint**

Play the best two samples for the user and obtain a voice choice.

**Step 5: Generate full narration**

Run with the approved voice:

```bash
npx hyperframes tts videos/alpha-farm/content/narration.zh-CN.txt \
  --voice <approved-z-voice> \
  --output videos/alpha-farm/audio/generated/narration.wav
```

**Step 6: Measure duration**

Run:

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 \
  videos/alpha-farm/audio/generated/narration.wav
```

Expected: 170–180 seconds. Adjust TTS speed or shorten copy if outside that range.

## Task 6: Build the Root Timeline and Scene Shells

**Files:**

- Modify: `videos/alpha-farm/index.html`
- Create: `videos/alpha-farm/styles/base.css`
- Create: `videos/alpha-farm/compositions/01-opening.html`
- Create: `videos/alpha-farm/compositions/02-cost-wall.html`
- Create: `videos/alpha-farm/compositions/03-pipeline.html`
- Create: `videos/alpha-farm/compositions/04-kol-roster.html`
- Create: `videos/alpha-farm/compositions/05-ai-parsing.html`
- Create: `videos/alpha-farm/compositions/06-risk-sizing.html`
- Create: `videos/alpha-farm/compositions/07-two-leg-entry.html`
- Create: `videos/alpha-farm/compositions/08-safety-gate.html`
- Create: `videos/alpha-farm/compositions/09-outro.html`

**Step 1: Build static hero frames**

For every scene:

- create a full-canvas flex `.scene-content`;
- use scene padding rather than absolute positioning for main content;
- give every scene an explicit `background-color: #070A12`;
- use 60px+ headlines, 20px+ body text, and 16px+ data labels;
- use tabular numerals for cost and risk calculations.

Do not add GSAP yet.

**Step 2: Wire the root composition**

The root must:

- use `data-composition-id="alpha-farm-root"`;
- be 1920×1080;
- load all nine external compositions;
- set non-overlapping scene start and duration values from the shot list;
- mount narration on its own audio track;
- reserve a separate track for background music.

**Step 3: Validate static structure**

Run:

```bash
npx hyperframes lint videos/alpha-farm
npx hyperframes validate videos/alpha-farm --no-contrast
```

Expected: no composition, timing, or track-overlap errors.

**Step 4: Inspect hero frames**

Run:

```bash
npx hyperframes inspect videos/alpha-farm \
  --at 7,25,45,67,92,120,147,166,176
```

Expected: no unintended text or card overflow.

**Step 5: Commit**

```bash
git add videos/alpha-farm/index.html videos/alpha-farm/styles \
  videos/alpha-farm/compositions
git commit -m "feat: lay out Alpha Farm scenes"
```

## Task 7: Animate the Cost, KOL, and AI Pipeline Scenes

**Files:**

- Modify: `videos/alpha-farm/compositions/01-opening.html`
- Modify: `videos/alpha-farm/compositions/02-cost-wall.html`
- Modify: `videos/alpha-farm/compositions/03-pipeline.html`
- Modify: `videos/alpha-farm/compositions/04-kol-roster.html`
- Modify: `videos/alpha-farm/compositions/05-ai-parsing.html`

**Step 1: Animate opening entrances**

Animate every visible element with distinct `gsap.from()` entrances. Offset the first motion by 0.1–0.3 seconds.

Opening sequence:

- KOL avatars wake in a controlled stagger;
- message particles travel toward the AI core;
- title locks into place;
- subtitle appears after the title.

**Step 2: Animate the cost wall**

Show:

```text
$1,000 / 6个月
× 10+ KOL
= $10,000+ / 6个月
```

Use deterministic number rolls and avatar activation. No `Math.random()`.

**Step 3: Animate the pipeline**

Reveal nodes in this order:

```text
Telegram → AI 理解 → 风控计算 → Deepcoin 执行 → 持仓保护
```

Use safety green only after validation nodes pass.

**Step 4: Animate the KOL roster**

Use real local avatar paths from `data/private/kol-showcase.json`. All avatars must have a placeholder fallback. Feature 3–4 approved KOLs without displaying private group links or unrelated members.

**Step 5: Animate AI field extraction**

Scan a sanitized message, then lock fields one by one:

```text
SYMBOL  BTC
SIDE    LONG
ENTRY   62000–62400
STOP    61500
TP      63500 / 64800
```

**Step 6: Run animation checks**

Run:

```bash
npx hyperframes lint videos/alpha-farm
npx hyperframes inspect videos/alpha-farm --samples 15
node /Users/steven/.codex/plugins/cache/openai-curated-remote/hyperframes/0.1.2/skills/hyperframes/scripts/animation-map.mjs \
  videos/alpha-farm \
  --out videos/alpha-farm/.hyperframes/anim-map
```

Expected: no missing entrances, collisions, invisible hero elements, or unexplained dead zones.

**Step 7: Commit**

```bash
git add videos/alpha-farm/compositions
git commit -m "feat: animate Alpha Farm signal pipeline"
```

## Task 8: Animate Risk Sizing, Two-Leg Entry, and Safety Gate

**Files:**

- Modify: `videos/alpha-farm/compositions/06-risk-sizing.html`
- Modify: `videos/alpha-farm/compositions/07-two-leg-entry.html`
- Modify: `videos/alpha-farm/compositions/08-safety-gate.html`

**Step 1: Animate maximum-loss-first sizing**

Display calculations in this exact order:

```text
最大亏损预算 = 100 USDT
单腿风险预算 = 100 × 50% = 50 USDT
```

Then show the greedy leg:

```text
50 ÷ (62,400 − 61,500) ≈ 0.0556 BTC
```

Then show the conservative leg:

```text
50 ÷ (62,000 − 61,500) = 0.1 BTC
```

Finish with:

```text
50 + 50 = 100 USDT 最大预设亏损
```

Add a small label stating that exchange contract steps and rounding apply in real execution.

**Step 2: Animate two parallel entry paths**

Do not call them first and second in the visible copy.

- Greedy: better price, lower fill probability;
- Conservative: easier fill, joins the move sooner.

Show both sharing the same fixed stop while receiving different quantities from the risk formula.

**Step 3: Animate the no-stop gate**

Feed a message without a stop into the parser. Keep the outgoing content visible until the transition. The safety scene entrance must land on:

```text
NO STOP, NO TRADE
没有止损，就没有仓位
```

Use risk red only for the blocked path.

**Step 4: Validate formulas and layout**

Run:

```bash
rg -n '62000|62400|61500|0\\.1|0\\.0556|100 USDT|NO STOP' \
  videos/alpha-farm/compositions
npx hyperframes inspect videos/alpha-farm --at 112,120,130,143,152,166
```

Expected: all approved values are present and all hero frames fit the canvas.

**Step 5: Commit**

```bash
git add videos/alpha-farm/compositions/06-risk-sizing.html \
  videos/alpha-farm/compositions/07-two-leg-entry.html \
  videos/alpha-farm/compositions/08-safety-gate.html
git commit -m "feat: explain Alpha Farm risk controls"
```

## Task 9: Add Narration, Captions, Music, and Transitions

**Files:**

- Modify: `videos/alpha-farm/index.html`
- Modify: `videos/alpha-farm/content/captions.json`
- Modify: `videos/alpha-farm/compositions/*.html`
- Create locally: `videos/alpha-farm/assets/private/audio/background.*`

**Step 1: Transcribe generated narration**

Install Whisper only if word-level caption timing cannot be obtained from the TTS workflow:

```bash
brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
npx hyperframes transcribe videos/alpha-farm/audio/generated/narration.wav
```

Expected: a transcript with time-aligned words or caption groups.

**Step 2: Build readable captions**

Group words into short Chinese phrases. Ensure each caption:

- can be read before it disappears;
- does not cover formulas or KOL names;
- highlights only key values and safety terms;
- exits before the next caption begins.

**Step 3: Add background music**

Use licensed or user-owned instrumental audio. Duck it beneath narration. Do not use AI MusicGen unless explicitly requested later.

**Step 4: Add transitions**

Use one primary medium-energy push/data transition for 60–70% of scene changes. Use at most two accents:

- chromatic/data pulse for the opening or AI reveal;
- gentle color dip to black for the outro.

Every scene must have entrance animation. Do not animate scenes out before transitions; only the final scene may fade out.

**Step 5: Validate audio and transitions**

Run:

```bash
npx hyperframes lint videos/alpha-farm
npx hyperframes validate videos/alpha-farm
npx hyperframes inspect videos/alpha-farm --samples 18
```

Expected: zero errors; resolve contrast warnings and any unintentional overflow.

**Step 6: Commit**

```bash
git add videos/alpha-farm/index.html videos/alpha-farm/content \
  videos/alpha-farm/compositions
git commit -m "feat: finish Alpha Farm audio and transitions"
```

## Task 10: Render and Review the Draft

**Files:**

- Create locally: `videos/alpha-farm/renders/alpha-farm-draft.mp4`
- Create locally: `videos/alpha-farm/renders/alpha-farm-draft-contact-sheet.png`

**Step 1: Run complete QA**

Run:

```bash
npx hyperframes lint videos/alpha-farm
npx hyperframes validate videos/alpha-farm
npx hyperframes inspect videos/alpha-farm --samples 20 --strict
python videos/alpha-farm/scripts/check_private_content.py videos/alpha-farm
```

Expected: all commands pass.

**Step 2: Render the draft**

Run:

```bash
cd videos/alpha-farm
npx hyperframes render \
  --output renders/alpha-farm-draft.mp4 \
  --quality draft \
  --fps 30 \
  --strict
```

Expected: a playable 1920×1080 MP4 approximately 180 seconds long.

**Step 3: Verify media properties**

Run:

```bash
ffprobe -v error \
  -show_entries format=duration:stream=codec_name,width,height,r_frame_rate \
  -of json videos/alpha-farm/renders/alpha-farm-draft.mp4
```

Expected:

- width 1920;
- height 1080;
- frame rate 30;
- duration close to 180 seconds;
- both video and audio streams present.

**Step 4: Create a visual review sheet**

Extract one frame near the hero moment of each scene and combine them into a 3×3 contact sheet. Inspect it for visual consistency, repeated layouts, tiny text, and accidental private content.

**Step 5: User checkpoint**

Show the draft video and contact sheet to the user. Collect timestamped feedback before final rendering.

## Task 11: Apply Review Notes and Render the Final Video

**Files:**

- Modify: relevant `videos/alpha-farm/` source files
- Create locally: `videos/alpha-farm/renders/alpha-farm-final.mp4`

**Step 1: Convert feedback into a timestamp checklist**

Record each requested change with:

- timestamp;
- current issue;
- exact requested outcome;
- affected composition.

**Step 2: Apply only approved changes**

Preserve timing and content outside affected scenes.

**Step 3: Re-run complete QA**

Run:

```bash
npx hyperframes lint videos/alpha-farm
npx hyperframes validate videos/alpha-farm
npx hyperframes inspect videos/alpha-farm --samples 20 --strict
python videos/alpha-farm/scripts/check_private_content.py videos/alpha-farm
```

Expected: all checks pass.

**Step 4: Render final quality**

Run:

```bash
cd videos/alpha-farm
npx hyperframes render \
  --output renders/alpha-farm-final.mp4 \
  --quality high \
  --fps 30 \
  --strict
```

Expected: final high-quality MP4 with synchronized narration, captions, music, and sound effects.

**Step 5: Verify final playback**

Watch the entire exported file from start to finish. Confirm:

- no black or frozen frames;
- no clipped text;
- correct KOL names and avatars;
- accurate `$1,000 / 半年 × 10+` statement;
- accurate risk calculation;
- readable `NO STOP, NO TRADE` gate;
- narration and captions remain synchronized;
- no sensitive information appears.

**Step 6: Request code review**

Use `@requesting-code-review` to review the committed video source, privacy guard, and QA evidence.

**Step 7: Commit final source changes**

```bash
git add videos/alpha-farm
git commit -m "feat: complete Alpha Farm project video"
```

The rendered MP4 remains ignored unless the user explicitly requests it be stored through a release or external artifact channel.
