# MindReader AI — Long-term Roadmap

## Vision
Make MindReader AI the best free prompt generator on the internet.
Target: 1000 daily active users within 3 months.

---

## Phase 1: Core Polish (Current — Week 1-2)
**Goal: Make the product feel "complete" to a first-time user**

- [x] 3-round conversation flow (Understand > Deep Dive > Generate)
- [x] 6 domain experts (video, image, design, writing, code, general)
- [x] Smart model selection (GPT-4o for quality, mini for speed)
- [x] Rate limiting (per-minute, per-day, global)
- [x] Anonymous user system (localStorage UUID)
- [x] Prompt export (Midjourney, DALL-E, ChatGPT, Stable Diffusion)
- [x] Shareable prompt pages (/prompt/{id})
- [x] Security headers + CORS
- [x] Mobile responsive
- [x] SEO basics (JSON-LD, OG tags)
- [x] Custom 404 page
- [x] Smart input depth detection (CJK-aware)
- [x] English language support (i18n)
- [x] Better onboarding — first-time user tutorial overlay
- [x] Prompt rating system (thumbs up/down)
- [x] Session naming (auto-generate from first message)

## Phase 2: Growth Features (Week 2-4)
**Goal: Give users reasons to come back and share**

- [ ] Custom domain (mindreader.ai or similar)
- [ ] Prompt gallery — public feed of best prompts
- [ ] User accounts (Google/GitHub OAuth)
- [ ] Prompt collections / folders
- [ ] Prompt remix — "use this as starting point"
- [ ] Social sharing cards with preview
- [ ] Analytics dashboard (Plausible/Umami)
- [ ] Email collection for updates

## Phase 3: Monetization (Week 4-8)
**Goal: Generate revenue to cover API costs**

- [ ] Pro tier: unlimited prompts, priority model, saved history
- [ ] Payment integration (Stripe)
- [ ] API access for developers
- [ ] Team/workspace features
- [ ] Custom domain experts (user-created)

## Phase 4: Scale (Week 8-12)
**Goal: Handle real traffic and iterate on data**

- [ ] Redis for rate limiting + session cache
- [ ] CDN for static assets
- [ ] Prompt quality scoring (automated)
- [ ] A/B testing framework for system prompts
- [ ] User feedback loop into domain expert tuning
- [ ] Multi-language system prompts (EN/JP/KR)

---

## Key Metrics to Track
- Daily active users (DAU)
- Prompts generated per day
- Average prompt length (target: 2000+ chars)
- Session completion rate (% who reach Round 3)
- Share rate (% of prompts shared)
- Return rate (% of users who come back)

## Current Status
- **Live URL**: https://web-production-3e4e9.up.railway.app
- **GitHub**: https://github.com/Pineappkeicecream/mindreader-ai
- **Railway**: https://railway.com/project/00630c89-1976-4d26-819c-ed8a44cf612c
- **Sessions**: 6 | **Prompts**: 6 | **KB**: 1,633 expert prompts
