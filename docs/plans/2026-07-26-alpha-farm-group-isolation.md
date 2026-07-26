# Alpha 牧场群组隔离表达修订 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 消除 KOL 介绍段的跨群管理歧义，并明确每个 KOL 群独立解析、独立风控、独立执行。

**Architecture:** 保持 180 秒视频结构与现有视觉身份不变，只修改 KOL 阵容子场景、对应字幕、旁白和分镜文档。重新生成现有晓晓女声轨后，用 HyperFrames 完成静态检查、布局检查与高质量渲染。

**Tech Stack:** HyperFrames 0.7.72、HTML/CSS/GSAP、Edge TTS、FFmpeg

---

### Task 1: 修正 KOL 场景表达

**Files:**
- Modify: `videos/alpha-farm/compositions/04-kol-roster.html`
- Modify: `videos/alpha-farm/content/shot-list.md`
- Modify: `docs/plans/2026-07-26-alpha-farm-video-design.md`

**Step 1: 修改智哥风格标签**

将 `左侧挂单 · 后续管理` 改为 `限价挂单 · 后续更新`，只描述该 KOL 群内的消息特点。

**Step 2: 增加群组隔离提示**

在 KOL 阵容场景增加可读的隔离标签：

```text
群组隔离
每个 KOL 群独立解析 · 独立风控 · 独立执行
```

**Step 3: 更新分镜与设计文档**

写明所有 KOL 群策略、订单和仓位互不干扰。

### Task 2: 同步旁白和字幕

**Files:**
- Modify: `videos/alpha-farm/content/narration.zh-CN.txt`
- Modify: `videos/alpha-farm/content/narration-spoken.zh-CN.json`
- Modify: `videos/alpha-farm/index.html`

**Step 1: 更新旁白**

使用以下核心表述，并控制在现有 21 秒场景内：

```text
智哥常发布限价挂单和后续更新。所有群组分别运行，策略、订单和仓位互不干扰。
```

**Step 2: 更新时间轴字幕**

将原“智哥管理挂单”字幕替换为“智哥常发限价挂单”，并增加“各群策略独立运行，互不干扰”。

**Step 3: 重新生成女声**

Run:

```bash
.venv/bin/python videos/alpha-farm/scripts/generate_edge_narration.py
```

Expected: `kol-roster` 音频短于 20.2 秒，最终语音轨为 180 秒。

### Task 3: 检查并渲染

**Files:**
- Output: `videos/alpha-farm/renders/alpha-farm-final-v4.mp4`

**Step 1: 运行 HyperFrames 检查**

Run:

```bash
cd videos/alpha-farm
npx --yes hyperframes@0.7.72 lint
npx --yes hyperframes@0.7.72 check --samples 15
```

Expected: 0 errors、0 contrast issues、0 layout issues。

**Step 2: 高质量渲染**

Run:

```bash
npx --yes hyperframes@0.7.72 render --output renders/alpha-farm-final-v4.mp4 --quality high
```

Expected: 1920×1080、30fps、180 秒，包含 H.264 视频和 AAC 音频。

**Step 3: 抽帧复核**

抽取约 60 秒处画面，确认：

- 不再出现“负责管理挂单”；
- 显示群组隔离提示；
- 智哥仅被描述为发布限价挂单和后续更新。

**Step 4: 保存修订**

仅提交本计划列出的源文件，不包含生成的音频和 MP4。
