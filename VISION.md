# MindReader AI — Product Vision

> **让 AI 做出 99% 和用户心里想的东西一样的结果。**

---

## The Problem

Every day, millions of people type vague prompts into AI tools and get disappointing results. Not because the AI isn't powerful enough — but because the user can only express 10% of what's in their head.

The gap between **what you imagine** and **what you can describe** is the #1 bottleneck in AI-powered creation.

- "帮我画一张图" → AI doesn't know what picture you see in your mind
- "帮我写个文案" → AI doesn't know your brand voice, your audience, your taste
- "帮我设计个logo" → AI doesn't know you hate gradients and love Helvetica

**The problem isn't AI. The problem is the prompt.**

---

## The Solution

MindReader AI is a **prompt architect** — it reads your mind through expert-level questioning, then generates prompts so precise that AI tools produce exactly what you imagined.

### How It Works

```
You say something vague
       ↓
MindReader detects your domain (video / image / design / writing / code)
       ↓
It becomes that domain's top expert and asks surgical questions
       ↓
3 rounds: Understand → Deep Dive → Generate
       ↓
A 1500-3000 word professional prompt, ready to copy into any AI tool
```

### The Magic

- **Domain Experts**: 6 specialized expert personas (video, image, design, writing, code, general), each with the knowledge of a 15-year industry veteran
- **Smart Questions**: Not "what style do you want?" but "Is the line quality sharp like Nike's swoosh or rounded like Airbnb's curves?"
- **Reference Anchoring**: Every question uses real brands and works as anchors — Apple, Pixar, Aesop, Notion — so you don't have to describe from scratch
- **Negative Prompting**: Always asks "what do you absolutely NOT want?" — knowing what to avoid is often more precise than knowing what you want
- **Quality Floor**: Every generated prompt is at least 1500 characters with enforced density rules per domain

---

## Core Philosophy

### 1. Users Have a 100% Clear Picture — They Just Can't Express It

When someone says "I want a cool video", they already see colors, mood, pacing, characters in their mind. They just don't know which details matter or how to articulate them. MindReader's job is to pull out that other 90%.

### 2. Amateur vs Professional Prompts

| Amateur | Professional |
|---------|-------------|
| "一个机器人牛仔在西部冒险，幽默风格" | "末日后的洛杉矶空城。主角：半机械牛仔，破旧宽檐帽、黑色战损皮夹克..." |
| Gets you 30% of what you imagined | Gets you 99% of what you imagined |

### 3. Every Noun Needs Adjectives

- ❌ "a button" → ✅ "a floating circular blue primary action button in the bottom-right corner with a subtle shadow"
- ❌ "简约风格" → ✅ "大面积留白（60%以上）、Inter字体14px、#F5F5F5灰白底色、1px线性图标"

### 4. One Reference Beats a Thousand Adjectives

"像 Apple 官网的克制但不要那么冷淡" tells more than 200 words of description.

---

## Product Architecture

```
┌─────────────────────────────────────┐
│            Frontend (SPA)           │
│  Welcome → Questions → Prompt View  │
│  Prompt Library · Export · Mobile    │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│         FastAPI Backend             │
│  Domain Detection · Round Router    │
│  Auto-Expand · Format Converter     │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│        Intelligence Layer           │
│  6 Domain Expert JSONs              │
│  1,633 Reference Prompts (KB)       │
│  GPT-4o / GPT-4o-mini (Hybrid)     │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│        Persistence (SQLite)         │
│  Sessions · Messages · Prompts      │
│  Soft-delete · Stats                │
└─────────────────────────────────────┘
```

---

## Domain Experts

| Domain | Persona | Key Dimensions |
|--------|---------|----------------|
| **Video** | Pixar-level director + cinematographer | Shot composition, sound design, pacing, color grading |
| **Image** | Senior art director at top studio | Composition, lighting, material texture, atmosphere |
| **Design** | Pentagram-level brand design director | Brand DNA, color systems, typography, spatial relationships |
| **Writing** | Ogilvy/W+K creative copy director | Reader psychology, platform DNA, emotional arc, voice |
| **Code** | Senior architect at top tech company | User flow, data model, tech stack, error handling |
| **General** | Versatile creative strategist | Cross-domain pattern recognition, goal clarity |

---

## Key Metrics

- **Prompt Quality**: Average 1,700+ characters, 6+ sections per prompt
- **Efficiency**: 3 rounds (down from 4), ~3 minutes per session
- **Coverage**: 6 domains covering 90%+ of AI creation use cases
- **Knowledge Base**: 1,633 expert reference prompts

---

## Roadmap

### ✅ Done
- [x] 3-round flow (Understand → Deep Dive → Generate)
- [x] 6 domain expert systems with professional-grade examples
- [x] Hybrid model (4o-mini for speed + 4o for quality)
- [x] Auto-expand retry for short prompts
- [x] SQLite persistence (sessions, messages, prompts)
- [x] Prompt Library with saved prompts
- [x] Markdown rendering for prompt output
- [x] Export buttons (Raw, Midjourney, ChatGPT, DALL-E, SD)
- [x] Mobile responsive design
- [x] Error handling with retry

### 🔲 To Ship
- [ ] Format conversion API (smart export per tool)
- [ ] Session recovery on page reload
- [ ] Full end-to-end testing
- [ ] Performance optimization
- [ ] Landing page / onboarding
- [ ] Deployment (Vercel / Railway / VPS)
- [ ] Custom domain

---

## The Vision

**MindReader AI makes the gap between imagination and creation disappear.**

Today, only prompt engineers and AI power users can get great results from AI tools. MindReader democratizes that skill — anyone who can answer a few smart questions can get professional-grade output.

> "You don't need to learn prompt engineering. You just need to answer my questions."

---

*Built by Charles · 2025-2026*
