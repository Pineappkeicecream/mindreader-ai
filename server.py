"""MindReader AI — Backend server."""
from __future__ import annotations

import json
import os
import re
import time
import hashlib
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None

import db as database

load_dotenv()


# --- Rate Limiter ---
class RateLimiter:
    """Simple in-memory IP-based rate limiter. No extra dependencies."""

    def __init__(
        self,
        per_minute: int = 5,
        per_day: int = 50,
        global_per_day: int = 500,
    ):
        self.per_minute = per_minute
        self.per_day = per_day
        self.global_per_day = global_per_day
        self._minute_hits: dict[str, list[float]] = defaultdict(list)
        self._day_hits: dict[str, list[float]] = defaultdict(list)
        self._global_hits: list[float] = []

    def _cleanup(self, bucket: list[float], window: float) -> list[float]:
        now = time.time()
        return [t for t in bucket if now - t < window]

    def check(self, ip: str) -> str | None:
        """Return an error message if rate-limited, else None."""
        now = time.time()

        # Per-minute check
        self._minute_hits[ip] = self._cleanup(self._minute_hits[ip], 60)
        if len(self._minute_hits[ip]) >= self.per_minute:
            return "Too many requests. Please wait a minute before trying again."

        # Per-day check
        self._day_hits[ip] = self._cleanup(self._day_hits[ip], 86400)
        if len(self._day_hits[ip]) >= self.per_day:
            return "Daily usage limit reached. Please come back tomorrow!"

        # Global daily check
        self._global_hits = self._cleanup(self._global_hits, 86400)
        if len(self._global_hits) >= self.global_per_day:
            return "Service is at capacity for today. Please try again tomorrow."

        return None

    def record(self, ip: str) -> None:
        now = time.time()
        self._minute_hits[ip].append(now)
        self._day_hits[ip].append(now)
        self._global_hits.append(now)


rate_limiter = RateLimiter(
    per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "5")),
    per_day=int(os.getenv("RATE_LIMIT_PER_DAY", "50")),
    global_per_day=int(os.getenv("RATE_LIMIT_GLOBAL_DAY", "500")),
)


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting reverse proxy headers."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _access_code() -> str:
    return os.getenv("ACCESS_CODE", "").strip()


def _access_cookie_value() -> str:
    code = _access_code()
    if not code:
        return ""
    return hashlib.sha256(("mindreader-access:" + code).encode()).hexdigest()


def _has_access(request: Request) -> bool:
    code = _access_code()
    if not code:
        return True
    if request.cookies.get("mr_access") == _access_cookie_value():
        return True
    return request.query_params.get("access_code", "") == code


def _admin_token() -> str:
    return os.getenv("ADMIN_TOKEN", "").strip()


def _is_admin_request(request: Request) -> bool:
    token = _admin_token()
    if not token:
        return False
    supplied = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    return supplied == token


ACCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MindReader AI — Access</title>
<meta name="robots" content="noindex">
<style>
*{box-sizing:border-box;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;background:#09090b;color:#fafafa;display:grid;place-items:center;padding:24px}
.panel{width:min(420px,100%);border:1px solid #27272a;background:#111113;border-radius:16px;padding:24px}
.logo{width:40px;height:40px;border-radius:10px;background:#4f46e5;display:grid;place-items:center;margin-bottom:16px}
h1{font-size:22px;margin:0 0 8px}
p{color:#a1a1aa;font-size:14px;line-height:1.6;margin:0 0 20px}
input{width:100%;background:#09090b;border:1px solid #3f3f46;border-radius:10px;color:#fff;padding:12px 14px;font-size:14px;outline:none}
input:focus{border-color:#6366f1;box-shadow:0 0 0 2px rgba(99,102,241,.25)}
button{width:100%;margin-top:12px;border:0;border-radius:10px;background:#4f46e5;color:#fff;padding:12px 14px;font-weight:700;cursor:pointer}
button:hover{background:#6366f1}
.err{display:none;color:#fca5a5;font-size:13px;margin-top:12px}
</style>
</head>
<body>
<main class="panel">
  <div class="logo">●</div>
  <h1>MindReader AI</h1>
  <p>This beta is private. Enter the access code from Charles to continue.</p>
  <form id="form">
    <input id="code" type="password" placeholder="Access code" autocomplete="current-password" autofocus>
    <button type="submit">Enter</button>
    <div class="err" id="err">Wrong access code. Please try again.</div>
  </form>
</main>
<script>
document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const code = document.getElementById('code').value.trim();
  const res = await fetch('/api/access', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code})
  });
  if (res.ok) location.href = '/';
  else document.getElementById('err').style.display = 'block';
});
</script>
</body>
</html>"""


app = FastAPI(title="MindReader AI")

# CORS — allow the Railway domain and localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.middleware("http")
async def access_gate(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    public_paths = {"/health", "/api/access"}
    if path in public_paths or path.startswith("/favicon"):
        return await call_next(request)
    if _has_access(request):
        return await call_next(request)
    accepts_html = "text/html" in request.headers.get("accept", "")
    if request.method == "GET" and accepts_html:
        return HTMLResponse(ACCESS_HTML, status_code=401)
    return JSONResponse(status_code=401, content={"error": "Access code required"})


@app.post("/api/access")
async def enter_access(request: Request):
    code = _access_code()
    if not code:
        return {"ok": True}
    body = await request.json()
    if (body.get("code") or "").strip() != code:
        return JSONResponse(status_code=401, content={"error": "Invalid access code"})
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "mr_access",
        _access_cookie_value(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Initialize database
database.init_db()
print("Database initialized")

# --- Load knowledge base ---
KB_PATH = Path(__file__).parent / "knowledge_base.json"
KNOWLEDGE_BASE: list[dict] = []
if KB_PATH.exists():
    KNOWLEDGE_BASE = json.loads(KB_PATH.read_text()).get("prompts", [])
    print(f"Loaded {len(KNOWLEDGE_BASE)} prompts into knowledge base")

# --- Load domain experts ---
DOMAIN_EXPERTS_DIR = Path(__file__).parent / "domain_experts"
DOMAIN_EXPERTS: dict[str, dict] = {}
if DOMAIN_EXPERTS_DIR.exists():
    for f in DOMAIN_EXPERTS_DIR.glob("*.json"):
        expert = json.loads(f.read_text())
        DOMAIN_EXPERTS[expert["domain"]] = expert
    print(f"Loaded {len(DOMAIN_EXPERTS)} domain experts: {list(DOMAIN_EXPERTS.keys())}")


def detect_language(text: str) -> str:
    """Detect language: 'zh' if CJK-dominant, 'en' otherwise."""
    if not text.strip():
        return "en"
    cjk_count = sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿'
                    or '　' <= c <= '〿' or '＀' <= c <= '￯')
    return "zh" if cjk_count / max(len(text.strip()), 1) > 0.15 else "en"


def detect_domain(text: str) -> str:
    """Detect which domain the user's request belongs to."""
    text_lower = text.lower()
    best_domain = "general"
    best_score = 0
    for domain, expert in DOMAIN_EXPERTS.items():
        if domain == "general":
            continue
        score = 0
        for kw in expert.get("keywords", []):
            if kw.lower() in text_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_domain = domain
    return best_domain


def build_domain_context(domain: str, lang: str = "zh") -> str:
    """Build domain-specific expert context to inject into system prompt."""
    expert = DOMAIN_EXPERTS.get(domain)
    is_en = lang == "en"

    if not expert or domain == "general":
        general = DOMAIN_EXPERTS.get("general", {})
        if general:
            label = "General" if is_en else "通用"
            return f"\n\n## DETECTED DOMAIN: {label}\n{general.get('expert_role', '')}"
        return ""

    ctx = f"\n\n## DETECTED DOMAIN: {expert['display_name']}\n"
    ctx += f"{expert['expert_role']}\n\n"

    # Must-ask dimensions
    header = "### Dimensions you MUST explore (ask about each one):" if is_en else "### 你必须深入挖掘的维度（每个维度至少问到）："
    ctx += header + "\n"
    for dim in expert.get("must_ask_dimensions", []):
        ctx += f"\n**{dim['dimension']}**"
        if "why" in dim:
            ctx += f" — {dim['why']}"
        ctx += "\n"
        if "good_question" in dim:
            gq_label = "  Good question example" if is_en else "  好的问题示例"
            ctx += f"{gq_label}: {dim['good_question']}\n"
        if "bad_question" in dim:
            bq_label = "  ❌ Bad question" if is_en else "  ❌ 差的问题"
            ctx += f"{bq_label}: {dim['bad_question']}\n"
        if "example_questions" in dim:
            for eq in dim["example_questions"][:2]:
                ctx += f"  - {eq}\n"

    # Output structure
    output = expert.get("output_structure", {})
    if output.get("sections"):
        sec_header = "\n### Final prompt MUST include these sections:" if is_en else "\n### 最终prompt必须包含这些章节："
        ctx += sec_header + "\n"
        for sec in output["sections"]:
            ctx += f"- **{sec['name']}**: {sec['description']}\n"

    # Good example
    if output.get("good_example_summary"):
        ex_header = "\n### Professional-grade prompt example (this is the quality bar you must reach):" if is_en else "\n### 专业级prompt示例（这是你要达到的质量标准）："
        ctx += f"{ex_header}\n{output['good_example_summary'][:2000]}\n"

    return ctx


def build_round_context(domain: str, round_num: int, lang: str = "zh") -> str:
    """Build round-specific guidance from domain expert."""
    expert = DOMAIN_EXPERTS.get(domain, DOMAIN_EXPERTS.get("general", {}))
    guidance = expert.get("round_guidance", {})
    is_en = lang == "en"

    round_key = f"round{min(round_num, 4)}"
    round_text = guidance.get(round_key, "")

    if round_num == 1:
        return f"\n\n[SYSTEM: This is Round 1. {round_text}]"
    elif round_num == 2:
        if is_en:
            return f"""\n\n[SYSTEM: This is Round 2 — EXPERT DEEP DIVE. {round_text}

## Round 2 QUALITY RULES:
1. Use the must_ask_dimensions from the domain expert. Ask 3-5 questions covering different dimensions.
2. Each question MUST have 3-4 options. Two options is too few — it feels like a yes/no.
3. Each option description must be a vivid micro-scene with a specific reference (brand, movie, artwork).
4. You MUST ask "What do you absolutely NOT want?" — this powers the Negative Prompt section.
5. NEVER repeat what the user told you in previous rounds. Read the conversation history carefully.
6. Questions should use specific brand/work anchors. ❌"What lighting do you want?" ✅"Is the lighting closer to Vermeer's soft window light, or Blade Runner's neon split lighting?"
7. This is the LAST round of questions. Cover ALL remaining professional dimensions. Don't hold anything back.
8. IMPORTANT: Respond in English since the user is writing in English.]"""
        else:
            return f"""\n\n[SYSTEM: This is Round 2 — EXPERT DEEP DIVE. {round_text}

## Round 2 QUALITY RULES:
1. Use the must_ask_dimensions from the domain expert. Ask 3-5 questions covering different dimensions.
2. Each question MUST have 3-4 options. Two options is too few — it feels like a yes/no.
3. Each option description must be a vivid micro-scene with a specific reference (brand, movie, artwork).
4. You MUST ask "什么是你绝对不想要的？" — this powers the Negative Prompt section.
5. NEVER repeat what the user told you in previous rounds. Read the conversation history carefully.
6. Questions should use specific brand/work anchors. ❌"你想要什么光线？" ✅"光线更接近Vermeer的柔和窗光，还是《银翼杀手》的霓虹split lighting？"
7. This is the LAST round of questions. Cover ALL remaining professional dimensions. Don't hold anything back.]"""
    elif round_num >= 3:
        # Inject the good_example as quality benchmark for final prompt
        output_struct = expert.get("output_structure", {})
        good_example = output_struct.get("good_example_summary", "")
        sections = output_struct.get("sections", [])
        # Use domain-specific density rules if available
        domain_density = output_struct.get("round4_density_rules", "")

        section_names = [s["name"] for s in sections]
        section_list = ", ".join(section_names) if is_en else "、".join(section_names)

        # Calculate target length from good_example
        target_len = max(len(good_example), 1500)

        # Build density section — domain-specific or fallback
        if domain_density:
            density_block = domain_density
        elif is_en:
            density_block = """## Density Requirements:
- Each section must be at least 120 words with specific details, not vague descriptions
- Negative: at least 3 categories, each with at least 5 items, totaling at least 18 prohibitions
- Total length: at least 2000 characters. Don't stop before 2000!"""
        else:
            density_block = """## 密度硬性要求：
- 每个章节至少120字，要有具体细节不是笼统描述
- Negative：至少分3类，每类至少5项，共至少18个禁止项
- 总长度：至少2000字。写到2000字之前不要停！"""

        if is_en:
            density_rules = f"""
[SYSTEM: This is the FINAL round. You MUST set prompt_ready=true and generate final_prompt NOW. No more questions.
IMPORTANT: Respond entirely in English since the user is writing in English.

## SUMMARY (Required):
Before generating final_prompt, write a 200-400 word professional summary in the summary field that "paints" the user's vision.
- NOT a bullet-point recap! Write a cohesive, vivid description
- Use specific brand/work names as anchors
- The summary will be shown to the user so they know what you understood

## CRITICAL OUTPUT RULES:
1. final_prompt is a STRING (markdown format), use ## headers to separate sections
2. Must include these sections: {section_list}
3. Each section's description density must match the level of this example — not shorter, equally detailed or longer:

=== Quality Benchmark (your output must match this density and professionalism) ===
{good_example[:3000]}
=== End Quality Benchmark ===

{density_block}

## General Quality Rules:
- Every noun needs 2-3 modifiers. ❌"a button" ✅"a floating circular blue primary action button in the bottom-right corner with a subtle drop shadow"
- Use specific descriptions instead of abstract summaries. ❌"minimalist style" ✅"generous whitespace (60%+), Inter font 14px, #F5F5F5 off-white background, 1px line icons"
- References must be specific to a brand or work. ❌"modern feel" ✅"like Linear's interface — high information density without feeling crowded, using grayscale layers rather than color to differentiate priority"
- Prohibitions must be specific and actionable. ❌"don't look ugly" ✅"no gradients, no 3D skeuomorphic effects, no border-radius greater than 12px"

## ⚠️ Anti-Copying Rule:
- The quality benchmark is a "density reference" — learn from its level of detail, but NEVER copy its specific content
- The benchmark's characters/scenes/colors/artists are from a completely different project, not yours
- Your final_prompt must be 100% original, created from scratch for the user's specific vision
- Negative Prompt must be custom-tailored to the current project, not a generic template

## ⚠️ Length Requirements (one of the most important rules):
- Your final_prompt must be at least {max(target_len, 2000)} characters
- The quality benchmark has {len(good_example)} characters — your output cannot be shorter
- If you finish writing but the total is not long enough, immediately add more specific details to each section — more adjectives, material descriptions, reference comparisons, specific values
- Better to write a 3000-char professional prompt than a 1500-char lazy one
- After writing each section, check its density — if a section is only 2-3 sentences, you're being too lazy
- 2000 characters is the MINIMUM! Each section at least 150 words!

{round_text}]"""
        else:
            density_rules = f"""
[SYSTEM: This is the FINAL round. You MUST set prompt_ready=true and generate final_prompt NOW. No more questions.

## SUMMARY（必填）：
在生成final_prompt之前，先在summary字段中用200-400字的专业语言"画出"用户脑海中的画面。
- 不是列表复述！是连贯的、有画面感的描述
- 用具体品牌/作品名做锚点
- summary会展示给用户看，让他知道你理解了什么

## CRITICAL OUTPUT RULES:
1. final_prompt是STRING（markdown格式），用 ## 标题分段
2. 必须包含这些章节：{section_list}
3. 每个章节的描述密度必须达到下面这个示例的水平——不是更短，是同等或更长：

=== 质量标杆（你的输出必须达到这个密度和专业度）===
{good_example[:3000]}
=== 质量标杆结束 ===

{density_block}

## 通用质量规则：
- 每个名词要有2-3个修饰词。❌"一个按钮" ✅"右下角浮动的圆形蓝色主操作按钮，带有微妙的投影效果"
- 用具体描写代替抽象概括。❌"简约风格" ✅"大面积留白（60%以上）、Inter字体14px、#F5F5F5灰白底色、1px线性图标"
- 参考/对标必须具体到品牌或作品名。❌"现代感" ✅"像Linear的界面——信息密度高但不拥挤，用灰度层次而非颜色区分优先级"
- 禁止项必须具体到可执行。❌"不要难看" ✅"不要渐变色、不要3D拟物效果、不要圆角大于12px"

## ⚠️ 反照搬规则：
- 质量标杆是"密度参考"——学习它的详细程度，但绝对不要照搬里面的具体内容
- 质量标杆里的角色/场景/颜色/参考艺术家是另一个完全不同的作品，不是你的
- 你的final_prompt必须100%针对用户描述的场景从零创作
- Negative Prompt必须针对当前场景定制，不是通用模板

## ⚠️ 长度硬性要求（这是最重要的规则之一）：
- 你的final_prompt必须至少{max(target_len, 2000)}字
- 质量标杆有{len(good_example)}字——你的输出不能比它短
- 如果你发现自己写完了但总字数不够，立刻给每个章节补充更多具体细节——加形容词、加材质描写、加参考对标、加具体数值
- 宁可写3000字的专业prompt也不要写1500字的敷衍prompt
- 每写完一个章节都检查一下它的密度——如果整个章节只有两三句话，说明你太敷衍了
- 2000字是最低要求！每个章节至少150字！

{round_text}]"""
        return density_rules
    return ""


def find_relevant_prompts(query: str, limit: int = 5) -> list[dict]:
    """Find relevant prompts from knowledge base using keyword matching."""
    query_lower = query.lower()
    scored = []
    for p in KNOWLEDGE_BASE:
        score = 0
        text = f"{p['act']} {' '.join(p['categories'])}".lower()
        for word in query_lower.split():
            if len(word) > 2 and word in text:
                score += 2
            if len(word) > 2 and word in p['prompt'].lower()[:500]:
                score += 1
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored[:limit]]


SYSTEM_PROMPT = """你是MindReader AI——顶级prompt架构师。你不是客服，你是导演/设计总监/创意总监/首席架构师。用户说一句模糊的话，你能从中读出他脑海里那个完整的画面，然后用专家级提问把它全部拉出来。

## 核心哲学
用户脑海里有一个100%清晰的画面，但他们只能表达10%。你的工作是用专业提问拉出另外90%。

amateur prompt vs professional prompt的差距：
- Amateur: "一个机器人牛仔在西部冒险，幽默风格"
- Professional: "末日后的洛杉矶空城。主角：半机械牛仔，破旧宽檐帽、黑色战损皮夹克、红褐色旧围巾、白色磨损机械身体。黑色电子屏脸，轻松时淡蓝微笑，警觉时变冷静横线。分镜：0-3s 低机位广角前推，从地上的高尔夫球裂缝杂草抬起... 声音：风声、鸟鸣、击球声..."

## CRITICAL: JSON格式

{
  "intro": "一句话，≤40字。展示专业洞察，不要废话。",
  "questions": [
    {
      "question": "专家级问题——像domain_expert模板中good_question那样的质量",
      "multi_select": true,
      "allow_text": true,
      "text_placeholder": "具体的、有启发性的提示...",
      "options": [
        {"label": "简短标签", "description": "一个生动的场景描述，不是功能定义"},
        {"label": "简短标签", "description": "让用户脑海里立刻浮现画面"}
      ]
    }
  ],
  "summary": "",
  "prompt_ready": false,
  "final_prompt": "",
  "prompt_explanation": ""
}

## ⚠️ INTRO规则（最重要）：
- ≤40个字。一句话。不是段落。
- 绝对禁止："非常有趣"、"充满创意"、"很棒的想法"、"我注意到你提到了..."、"为了帮助你更好地..."、"这个过程..."
- 正确的intro像专家说话：
  ✅ "短视频——先确定核心：你要讲一个故事，还是展示一个产品？"
  ✅ "Logo设计。最关键的第一步：这个品牌的性格是什么？"
  ✅ "小红书文案，这个平台的核心是真实感和好奇心缺口。"
  ✅ "全栈应用——先画清楚核心用户流，技术选型才有意义。"
- 如果用户给了详细描述，intro应该展示你读懂了："机器人牛仔+末日洛杉矶——很有Pixar短片的气质。几个关键细节决定成败。"

## ⚠️ 问题质量规则：
- 你的域专家模板里有good_question示例。你必须用那个质量水准的问题，或直接用那些问题。
- 绝对禁止平庸问题："你想要什么风格？"→ 应该是"线条是圆润像Airbnb还是锋利像Nike？"
- 每个问题必须包含具体的参考/对比/二选一，让用户不需要从零想。
- 问题中用真实品牌/作品做锚点：Apple、Nike、Pixar、苹果官网、杜蕾斯文案、Notion、Stripe。

## ⚠️ 选项质量规则：
- 每个问题必须有3-5个选项。2个选项太少！用户需要足够的选择空间。
- label: ≤6个字，清晰分类
- description: 不是字典定义！是一个让用户脑海浮现画面的微场景，用具体品牌/作品做锚点。
- ❌ 差：{"label": "产品展示", "description": "展示某个产品的功能与特点"}
- ✅ 好：{"label": "产品展示", "description": "像Apple发布会那样——一个产品从黑暗中缓缓旋转出现，每个细节特写，配合精准的文字节奏"}
- ❌ 差：{"label": "感动", "description": "让观众感受到情感的共鸣"}
- ✅ 好：{"label": "感动", "description": "像Pixar《UP》开头——不说一句话，用画面让人安静流泪"}
- 最后一个选项可以是"其他 / 让我自己描述"让用户自由输入

## 流程 — 2轮提问 + 1轮生成（共3轮，不要拖到4轮！）：

### Round 1 — 锁定方向（3-4个问题）
根据用户输入深度自适应：
- 模糊输入（<30字）：先确定"做什么"→ 有没有画面 → 核心感受
- 详细输入（>30字）：不要重复他说的！直接深挖他没提到的专业细节。

### Round 2 — 专家级深挖（3-5个问题）
使用域专家模板中的must_ask_dimensions。问法必须像模板中的good_question那样，带具体参考和对比。
必须问"什么是你绝对不想要的？"——这决定Negative Prompt。
⚠️ 这是最后一轮提问！问完所有关键维度，不要留到下一轮。

### Round 3 — 直接生成最终prompt（不要再问问题！）
1. 先填summary字段：用专业语言"画出"用户脑海中的画面（200-400字）
2. 设prompt_ready=true
3. 生成final_prompt（markdown string格式，1200-3000字）
final_prompt的章节结构必须严格按照域专家模板中output_structure.sections定义的来。
final_prompt必须是STRING不是JSON。用markdown ## headers来分段。
如果有round4_density_rules，你必须严格遵守每个章节的字数最低要求。
⚠️ 绝对不要在Round 3再问问题！用户已经给了足够信息，直接生成。

## 描述密度规则：
❌ "父亲穿着深色外套，神态温和"
✅ "父亲穿着有些起球的深蓝色呢子大衣，领口竖起挡风，左手撑着一把黑色折叠伞，伞面上滚动着雨珠。表情沉稳但眼角有温柔的笑纹。"

❌ "声音：雨声"
✅ "声音：前景——雨滴打在伞面上的密集嗒嗒声；中景——父子脚步踩水的轻响；远景——城市低沉的白噪音。"

规则：每个名词要有形容词。每个动作要有方式。每个声音要有层次。闭上眼睛看不到画面=不够详细。

## 铁律：
- intro ≤40字，绝不说废话（"非常有趣""充满创意"=废话）
- 问题必须达到域专家模板good_question的水准
- 选项description是微场景，不是字典定义
- 绝不问"目标观众是谁""什么风格"这种平庸问题
- Round 3必须直接生成final_prompt，不要再问确认问题
- 绝不跳过Negative部分
- 绝不复述用户已说的内容
"""

SYSTEM_PROMPT_EN = """You are MindReader AI — a world-class prompt architect. You're not a chatbot; you're a director / design lead / creative director / chief architect. When a user says something vague, you can read the complete picture in their mind, then use expert-level questions to extract it all.

## Core Philosophy
The user has a 100% clear picture in their mind, but can only express 10%. Your job is to use professional questions to pull out the other 90%.

Amateur prompt vs professional prompt gap:
- Amateur: "A robot cowboy in the wild west, humorous style"
- Professional: "Post-apocalyptic empty LA. Hero: half-mechanical cowboy, worn wide-brim hat, black battle-damaged leather jacket, rust-red old scarf, white weathered mechanical body. Black digital screen face, relaxed = faint blue smile, alert = cold horizontal line. Shots: 0-3s low-angle wide push-in from golf ball cracks and weeds... Sound: wind, birdsong, golf swing..."

## CRITICAL: JSON Format

{
  "intro": "One sentence, ≤40 words. Show professional insight, no filler.",
  "questions": [
    {
      "question": "Expert-level question — match the quality of good_question in the domain expert template",
      "multi_select": true,
      "allow_text": true,
      "text_placeholder": "Specific, inspiring hint...",
      "options": [
        {"label": "Short label", "description": "A vivid micro-scene, not a dictionary definition"},
        {"label": "Short label", "description": "Makes the user instantly picture something"}
      ]
    }
  ],
  "summary": "",
  "prompt_ready": false,
  "final_prompt": "",
  "prompt_explanation": ""
}

## ⚠️ INTRO Rules (Most Important):
- ≤40 words. One sentence. Not a paragraph.
- NEVER say: "That's interesting", "Great idea", "What a creative concept", "I notice you mentioned...", "To help you better..."
- Correct intros sound like an expert:
  ✅ "Short-form video — first question: are you telling a story, or showcasing a product?"
  ✅ "Logo design. Critical first step: what's this brand's personality?"
  ✅ "Full-stack app — let's map the core user flow first. Tech stack follows from that."
- For detailed inputs, show you understood: "Robot cowboy + post-apocalyptic LA — very Pixar short film energy. A few key details will make or break it."

## ⚠️ Question Quality Rules:
- Your domain expert template has good_question examples. Your questions MUST match that quality level, or use those questions directly.
- NEVER ask bland questions: "What style do you want?" → Should be "Should the lines be rounded like Airbnb or sharp like Nike?"
- Every question must include specific references/comparisons/choices so users don't have to think from scratch.
- Use real brands/works as anchors: Apple, Nike, Pixar, Stripe, Notion, etc.

## ⚠️ Option Quality Rules:
- Each question MUST have 3-5 options. 2 options is too few! Users need enough choice space.
- label: ≤6 words, clear category
- description: NOT a dictionary definition! A vivid micro-scene using specific brand/work anchors.
- ❌ Bad: {"label": "Product showcase", "description": "Showing a product's features and highlights"}
- ✅ Good: {"label": "Product showcase", "description": "Like an Apple keynote — product slowly rotating from darkness, every detail in close-up, perfectly timed text beats"}
- ❌ Bad: {"label": "Emotional", "description": "Making the audience feel emotional resonance"}
- ✅ Good: {"label": "Emotional", "description": "Like the opening of Pixar's UP — no words needed, just visuals that make you cry quietly"}
- Last option can be "Other / let me describe" for free input

## Flow — 2 rounds of questions + 1 generation round (3 total, don't drag to 4!):

### Round 1 — Lock Direction (3-4 questions)
Adapt based on input depth:
- Vague input (<30 words): Establish "what is this" → any specific vision → core feeling
- Detailed input (>30 words): Don't repeat what they said! Go straight to expert-level details they haven't mentioned.

### Round 2 — Expert Deep Dive (3-5 questions)
Use must_ask_dimensions from domain expert template. Questions must be like the template's good_question — with specific references and comparisons.
MUST ask "What do you absolutely NOT want?" — this powers the Negative Prompt.
⚠️ This is the LAST round of questions! Cover ALL key dimensions, don't save anything for later.

### Round 3 — Generate Final Prompt (no more questions!)
1. Fill the summary field: "paint" the user's vision in professional language (200-400 words)
2. Set prompt_ready=true
3. Generate final_prompt (markdown string format, 1200-3000 words)
final_prompt sections MUST follow the output_structure.sections defined in the domain expert template.
final_prompt must be a STRING not JSON. Use markdown ## headers to separate sections.
If there are round4_density_rules, you MUST strictly follow each section's minimum word count.
⚠️ NEVER ask more questions in Round 3! The user has given enough information, generate directly.

## Description Density Rules:
❌ "Father wearing a dark coat, gentle expression"
✅ "Father in a slightly pilled navy wool overcoat, collar turned up against the wind, left hand holding a black folding umbrella with raindrops rolling off. Expression calm but with warm laugh lines at the corners of his eyes."

❌ "Sound: rain"
✅ "Sound: foreground — dense tapping of raindrops on umbrella surface; midground — gentle splashing of father-son footsteps in puddles; background — low urban white noise."

Rule: Every noun needs adjectives. Every action needs a manner. Every sound needs layers. If you close your eyes and can't see/hear it = not detailed enough.

## Iron Rules:
- intro ≤40 words, never waste words ("so interesting" "such a creative" = wasted words)
- Questions must match domain expert template good_question quality
- Option descriptions are micro-scenes, not dictionary definitions
- NEVER ask "who is the target audience" "what style" — these are bland questions
- Round 3 must generate final_prompt directly, no confirmation questions
- NEVER skip the Negative section
- NEVER restate what the user already said
"""


def _format_structured_prompt(obj: dict) -> str:
    """Convert a structured prompt dict into a well-formatted string."""
    lines = []
    for key, value in obj.items():
        if isinstance(value, str):
            lines.append(f"## {key}\n{value}")
        elif isinstance(value, list):
            lines.append(f"## {key}")
            for item in value:
                if isinstance(item, dict):
                    # Shot breakdown item
                    parts = []
                    for k, v in item.items():
                        parts.append(f"{k}: {v}")
                    lines.append("  ".join(parts))
                else:
                    lines.append(f"- {item}")
        elif isinstance(value, dict):
            lines.append(f"## {key}")
            for k, v in value.items():
                lines.append(f"- {k}: {v}")
        else:
            lines.append(f"## {key}\n{value}")
    return "\n\n".join(lines)



def _plain_prompt_text(prompt: str) -> str:
    """Flatten markdown into copy-friendly plain text for tool exporters."""
    text = re.sub(r"```.*?```", " ", prompt, flags=re.S)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^[\s>*-]+", "", text, flags=re.M)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _guess_aspect_ratio(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in ["9:16", "vertical", "portrait", "竖屏", "手机屏", "小红书", "tiktok", "reels"]):
        return "9:16"
    if any(k in lower for k in ["1:1", "square", "正方形", "头像", "logo", "icon"]):
        return "1:1"
    if any(k in lower for k in ["4:5", "poster", "海报"]):
        return "4:5"
    return "16:9"


def _split_negative_prompt(prompt: str) -> tuple[str, str]:
    match = re.search(r"(?is)(negative prompt|negative|禁止|不要|避免|反向提示词)[:：\s]*(.+)$", prompt)
    if not match:
        return prompt, ""
    positive = prompt[:match.start()].strip()
    negative = match.group(2).strip()
    negative = re.sub(r"^#{1,6}\s*", "", negative, flags=re.M)
    negative = re.sub(r"\s+", " ", negative)
    return positive, negative


def format_prompt_for_tool(prompt: str, target_format: str) -> str:
    """Convert the generated master prompt into a tool-specific clipboard format."""
    target = (target_format or "raw").lower().strip()
    aspect = _guess_aspect_ratio(prompt)
    positive, negative = _split_negative_prompt(prompt)
    plain_positive = _plain_prompt_text(positive)
    plain_negative = _plain_prompt_text(negative)

    if target in {"raw", "master"}:
        return prompt.strip()

    if target == "midjourney":
        base = plain_positive[:3500]
        no_block = f" --no {plain_negative[:700]}" if plain_negative else ""
        return f"{base}{no_block} --ar {aspect} --style raw --v 6.1".strip()

    if target in {"dalle", "dall-e", "dall_e"}:
        parts = [
            "Create an image using the following creative direction.",
            "Preserve the concrete visual details, composition, lighting, materials, mood, and references.",
            plain_positive,
        ]
        if plain_negative:
            parts.append(f"Avoid: {plain_negative}")
        parts.append(f"Aspect ratio: {aspect}.")
        return "\n\n".join(parts).strip()

    if target in {"sd", "stable-diffusion", "stable_diffusion"}:
        positive_line = plain_positive[:3000]
        negative_line = plain_negative or "low quality, blurry, distorted anatomy, extra limbs, bad composition, watermark, text artifacts"
        return f"Positive prompt:\n{positive_line}\n\nNegative prompt:\n{negative_line}\n\nSuggested settings:\nAspect ratio {aspect}, CFG 6-8, 30-40 steps."

    if target == "chatgpt":
        return (
            "Use this as a master instruction. Follow every concrete requirement, preserve the structure, "
            "ask only if a required input is missing, and produce the requested final artifact directly.\n\n"
            f"{prompt.strip()}"
        )

    return prompt.strip()


# In-memory session store
sessions: dict[str, dict] = {}


def _build_initial_system_message(domain: str, first_message: str, lang: str = "zh") -> str:
    base_prompt = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT
    domain_context = build_domain_context(domain, lang)
    relevant = find_relevant_prompts(first_message, limit=3)
    kb_context = ""
    if relevant:
        kb_context = "\n\n## Reference Prompts from Knowledge Base\nUse these as INSPIRATION only:\n\n"
        for i, p in enumerate(relevant, 1):
            kb_context += f"### Reference {i}: {p['act']}\n{p['prompt'][:500]}\n\n"

    # Add language instruction
    lang_instruction = ""
    if lang == "en":
        lang_instruction = "\n\n## LANGUAGE: The user is writing in English. You MUST respond entirely in English — all intros, questions, options, descriptions, summaries, and final prompts must be in English.\n"

    return base_prompt + lang_instruction + domain_context + kb_context


def _restore_session_from_db(session_id: str) -> bool:
    session = database.get_session(session_id)
    if not session:
        return False

    db_messages = database.get_session_messages(session_id)
    if not db_messages:
        return False

    domain = session.get("domain", "general")
    first_message = session.get("first_message", "")
    restored = [{"role": "system", "content": _build_initial_system_message(domain, first_message)}]
    for msg in db_messages:
        if msg["role"] in {"user", "assistant"}:
            restored.append({"role": msg["role"], "content": msg["content"]})

    # Detect language from first user message
    first_user_msg = ""
    for msg in db_messages:
        if msg["role"] == "user":
            first_user_msg = msg["content"]
            break
    restored_lang = detect_language(first_user_msg)

    sessions[session_id] = {
        "messages": restored,
        "created_at": session.get("created_at", time.time()),
        "domain": domain,
        "model": session.get("model", "hybrid"),
        "lang": restored_lang,
    }
    print(f"Restored session {session_id[:8]}... | Domain: {domain} | Lang: {restored_lang} | Messages: {len(db_messages)}")
    return True


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/prompt/{prompt_id}", response_class=HTMLResponse)
async def shared_prompt_page(prompt_id: int):
    """Shareable prompt page — standalone HTML with the prompt content."""
    prompt = database.get_prompt(prompt_id)
    if not prompt:
        return HTMLResponse("<h1>Prompt not found</h1>", status_code=404)

    # Escape for safe HTML embedding
    import html as html_mod
    safe_summary = html_mod.escape(prompt.get("summary", "") or "")
    safe_domain = html_mod.escape(prompt.get("domain", "general"))

    # Convert markdown to basic HTML for the prompt
    fp = prompt.get("final_prompt", "")
    safe_fp = html_mod.escape(fp)
    # Basic markdown rendering in the template via JS

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MindReader AI Prompt #{prompt_id}</title>
<meta name="description" content="{safe_summary[:160]}">
<meta property="og:title" content="MindReader AI Prompt #{prompt_id} — {safe_domain}">
<meta property="og:description" content="{safe_summary[:200]}">
<meta property="og:url" content="https://web-production-3e4e9.up.railway.app/prompt/{prompt_id}">
<meta name="theme-color" content="#4F46E5">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234F46E5'/%3E%3Ccircle cx='16' cy='16' r='6' fill='none' stroke='%23C7D2FE' stroke-width='1.5'/%3E%3Ccircle cx='16' cy='16' r='2.5' fill='%23E0E7FF'/%3E%3C/svg%3E">
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{font-family:'Inter',sans-serif}}
body{{margin:0;background:#09090b;color:#fafafa;min-height:100vh}}
.prompt-rendered h2{{font-size:16px;font-weight:700;color:#a5b4fc;margin:24px 0 10px 0;padding-bottom:6px;border-bottom:1px solid #1e2d4a}}
.prompt-rendered h2:first-child{{margin-top:0}}
.prompt-rendered h3{{font-size:14px;font-weight:600;color:#c7d2fe;margin:16px 0 8px 0}}
.prompt-rendered p{{margin:8px 0;color:#d4d4d8}}
.prompt-rendered ul{{margin:8px 0;padding-left:20px;color:#d4d4d8}}
.prompt-rendered li{{margin:4px 0;line-height:1.6}}
.prompt-rendered strong{{color:#e0e7ff;font-weight:600}}
.prompt-rendered em{{color:#a5b4fc;font-style:italic}}
</style>
</head>
<body>
<div class="max-w-3xl mx-auto px-4 py-8">
  <div class="flex items-center gap-3 mb-6">
    <a href="/" class="flex items-center gap-2 text-sm text-indigo-400 hover:text-indigo-300">
      <svg width="24" height="24" viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="8" fill="#4F46E5"/>
        <circle cx="16" cy="16" r="2.5" fill="#E0E7FF"/>
      </svg>
      MindReader AI
    </a>
    <span class="text-zinc-600">·</span>
    <span class="text-xs font-semibold text-zinc-500 uppercase">{safe_domain}</span>
    <span class="text-zinc-600">·</span>
    <span class="text-xs text-zinc-600">{prompt.get('char_count', 0):,} chars</span>
  </div>

  {"<div class='bg-[#0f0d1a] border border-[#312e81] rounded-xl p-4 mb-4'><div class='text-xs font-semibold text-indigo-400 mb-2'>What I understood</div><div class='text-sm text-[#c7d2fe] leading-relaxed'>" + safe_summary + "</div></div>" if safe_summary else ""}

  <div class="bg-[#080f1a] border border-[#1a3a5c] rounded-xl p-5">
    <div class="prompt-rendered" id="promptContent"></div>
  </div>

  <div class="flex flex-wrap gap-3 mt-4">
    <button onclick="copyPrompt()" id="copyBtn" class="bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-semibold transition">Copy Prompt</button>
    <button onclick="copyFor('midjourney',this)" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg px-4 py-2 text-sm font-semibold transition">&#127912; Midjourney</button>
    <button onclick="copyFor('chatgpt',this)" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg px-4 py-2 text-sm font-semibold transition">&#128172; ChatGPT</button>
    <a href="/?remix={prompt_id}" class="bg-purple-600 hover:bg-purple-500 text-white rounded-lg px-4 py-2 text-sm font-semibold transition inline-block">&#128260; Remix</a>
    <a href="/" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg px-4 py-2 text-sm font-semibold transition inline-block">&#10024; Create Your Own</a>
    <a href="/gallery" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg px-4 py-2 text-sm font-semibold transition inline-block">&#128218; Gallery</a>
  </div>
</div>
<script>
const rawPrompt = {json.dumps(fp)};
function renderMd(t) {{
  let h = t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h = h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h = h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h = h.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>');
  h = h.replace(/^- (.+)$/gm,'<li>$1</li>');
  h = h.replace(/(<li>.*<\\/li>\\n?)+/g,'<ul>$&</ul>');
  h = h.replace(/\\n\\n/g,'</p><p>');
  return '<p>'+h+'</p>';
}}
document.getElementById('promptContent').innerHTML = renderMd(rawPrompt);
function copyPrompt() {{
  navigator.clipboard.writeText(rawPrompt).then(()=>{{
    const b = document.getElementById('copyBtn');
    b.textContent = 'Copied!';
    b.classList.replace('bg-indigo-600','bg-emerald-600');
    setTimeout(()=>{{ b.textContent='Copy Prompt'; b.classList.replace('bg-emerald-600','bg-indigo-600'); }}, 2000);
  }});
}}
async function copyFor(tool, btn) {{
  const orig = btn.innerHTML;
  btn.textContent = '...';
  try {{
    const res = await fetch('/api/format', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ prompt: rawPrompt, format: tool }}),
    }});
    const data = await res.json();
    await navigator.clipboard.writeText(data.formatted || rawPrompt);
    btn.textContent = '✓ Copied!';
    btn.classList.replace('bg-zinc-800','bg-emerald-600');
    setTimeout(()=>{{ btn.innerHTML = orig; btn.classList.replace('bg-emerald-600','bg-zinc-800'); }}, 2000);
  }} catch {{
    await navigator.clipboard.writeText(rawPrompt);
    btn.textContent = '✓ Raw copied';
    setTimeout(()=>{{ btn.innerHTML = orig; }}, 2000);
  }}
}}
</script>
</body></html>""")



@app.get("/health")
async def health():
    """Health check endpoint for monitoring."""
    return {
        "status": "ok",
        "domains": len(DOMAIN_EXPERTS),
        "kb_prompts": len(KNOWLEDGE_BASE),
    }


@app.post("/api/chat")
async def chat(request: Request):
    # --- Rate limiting ---
    client_ip = _get_client_ip(request)
    limit_msg = rate_limiter.check(client_ip)
    if limit_msg:
        return JSONResponse(status_code=429, content={"error": limit_msg})
    rate_limiter.record(client_ip)

    body = await request.json()
    session_id = body.get("session_id", "default")
    user_id = body.get("user_id", "")
    user_message = body.get("message", "").strip()
    use_model = body.get("model", "gpt-4o-mini")  # "gpt-4o-mini" or "gpt-4o"

    if not user_message:
        return {"error": "Empty message"}

    if session_id not in sessions and not _restore_session_from_db(session_id):
        # Detect domain and language from user's first message
        detected_domain = detect_domain(user_message)
        detected_lang = detect_language(user_message)

        sessions[session_id] = {
            "messages": [{"role": "system", "content": _build_initial_system_message(detected_domain, user_message, detected_lang)}],
            "created_at": time.time(),
            "domain": detected_domain,
            "lang": detected_lang,
        }
        print(f"New session {session_id[:8]}... | Domain: {detected_domain} | Lang: {detected_lang}")
        database.save_session(session_id, detected_domain, use_model, user_message, user_id=user_id)

    msgs = sessions[session_id]["messages"]
    domain = sessions[session_id].get("domain", "general")
    lang = sessions[session_id].get("lang", "zh")

    # Count user turns to determine round
    user_turn_count = len([m for m in msgs if m["role"] == "user"]) + 1

    # Inject domain-specific round guidance
    round_guidance = build_round_context(domain, user_turn_count, lang)

    # For Round 1, add input-depth-adaptive guidance
    is_detailed = True  # default for Round 2+
    if user_turn_count == 1:
        expert = DOMAIN_EXPERTS.get(domain, DOMAIN_EXPERTS.get("general", {}))

        # Smarter input depth detection:
        # Chinese characters carry ~3x the information density of English chars.
        # Count "information units": CJK chars count as 2.5, others as 1.
        stripped = user_message.strip()
        info_units = sum(2.5 if '一' <= c <= '鿿' else 1 for c in stripped)
        # Also check for specificity signals: proper nouns, style keywords, scene descriptions
        specificity_keywords = [
            "风格", "赛博", "cyberpunk", "像", "类似", "参考", "色调", "氛围",
            "场景", "角色", "背景", "颜色", "材质", "品牌", "产品",
            "故事", "剧情", "分镜", "镜头", "构图", "光线", "光影",
        ]
        has_specifics = any(kw in stripped.lower() for kw in specificity_keywords)
        # Input is "detailed" if high info density OR contains specific creative keywords
        is_detailed = info_units >= 40 or has_specifics

        # Build domain-specific vague options
        vague_options = {
            "video": "给出具体选项引导用户，如：产品展示、故事叙事、知识科普、Vlog记录、艺术短片、搞笑段子",
            "design": "给出具体选项引导用户，如：品牌Logo、App界面、海报、名片、社交媒体封面、产品包装",
            "writing": "给出具体选项引导用户，如：产品推广文案、社交媒体帖子、品牌故事、邮件营销、自媒体文章、广告脚本",
            "code": "给出具体选项引导用户，如：SaaS产品、电商平台、社交应用、管理后台、API服务、个人项目/工具",
            "general": "给出具体选项引导用户发现自己的需求",
        }
        domain_opts = vague_options.get(domain, vague_options["general"])

        # Build domain-specific detail prompts
        detail_hints = {
            "video": "直接深入专业细节：镜头语言（机位、运镜、景深）、声音设计（环境音层次、音乐风格）、剪辑节奏（快切还是长镜头）、色彩调性（冷暖、饱和度）。每个问题必须有3-4个选项，每个选项用具体品牌/作品做锚点。",
            "image": "直接深入专业细节：构图视角（镜头焦距感、仰视/俯视/平视）、光线方向和色温（侧光/逆光/顶光，暖调/冷调）、风格媒介（照片级/油画级/插画级，具体参考艺术家）、色彩调色盘（具体hex色值或电影色调参考）。每个问题必须有3-4个选项。",
            "design": "直接深入专业细节：视觉概念（具体色值不是'蓝色'）、字体性格（参考品牌字体）、布局偏好（参考网站/App）。每个问题必须有3-4个选项。",
            "writing": "直接深入专业细节：目标读者的现状vs理想状态、语气人设（参考具体人/品牌的说话方式）、要避免的套路和禁区。每个问题必须有3-4个选项。",
            "code": "直接深入专业细节：核心用户流step by step、数据模型、技术栈偏好、规模/性能要求、明确不想要什么。每个问题必须有3-4个选项。",
            "general": "直接深入用户没提到的具体细节。每个问题必须有3-4个选项。",
        }
        domain_detail = detail_hints.get(domain, detail_hints["general"])

        is_en = lang == "en"

        if not is_detailed:
            # Check if domain has preset questions for vague inputs (only for Chinese)
            preset = expert.get("round1_preset_vague") if not is_en else None
            if preset:
                # Use preset questions directly — skip API call for much better quality
                preset_data = {
                    "intro": preset["intro"],
                    "questions": preset["questions"],
                    "summary": "",
                    "prompt_ready": False,
                    "final_prompt": "",
                    "prompt_explanation": "",
                }
                # Store the preset response as assistant message so conversation history is correct
                msgs.append({"role": "user", "content": user_message})
                msgs.append({"role": "assistant", "content": json.dumps(preset_data, ensure_ascii=False)})

                return {
                    **preset_data,
                    "turn": len([m for m in msgs if m["role"] == "user"]),
                    "model_used": "preset",
                    "domain": domain,
                    "lang": lang,
                }

            if is_en:
                round_guidance += f"\n[INPUT_DEPTH: VAGUE — user said very little. Ask: 1) What specifically is this about? ({domain_opts}), 2) Do you have any specific picture/idea in mind? (open text with good placeholder), 3) What's the core goal/feeling? Do NOT ask about expert-level details yet — the user hasn't told you enough. IMPORTANT: Respond in English.]"
            else:
                round_guidance += f"\n[INPUT_DEPTH: VAGUE — user said very little. Ask: 1) What specifically is this about? ({domain_opts}), 2) Do you have any specific picture/idea in mind? (open text with good placeholder), 3) What's the core goal/feeling? Do NOT ask about expert-level details yet — the user hasn't told you enough.]"
        else:
            # Detailed input: user already gave specifics, jump straight to expert-level questions
            en_suffix = "\n9. IMPORTANT: Respond entirely in English since the user is writing in English." if is_en else ""
            round_guidance += f"""\n[INPUT_DEPTH: DETAILED — user already gave specific information. CRITICAL RULES:
1. NEVER repeat or rephrase what the user already told you. If they said 'cyberpunk Tokyo rainy streets', do NOT ask 'what style' or 'what scene' or 'what weather' — they already told you.
2. Jump to the NEXT LEVEL of detail — the expert dimensions they HAVEN'T covered yet.
3. {domain_detail}
4. Each question MUST have 3-4 options minimum, each option with vivid description using specific brand/work references.
5. The user's message is: "{user_message[:200]}". Extract what they already decided, then ask about everything ELSE.
6. Your intro should prove you understood: reference their specific idea, then pivot to what's missing.
7. EXAMPLE of what NOT to do: User says 'cyberpunk Tokyo' → you ask 'What style?' ← This is TERRIBLE. They already said the style.
8. EXAMPLE of what TO do: User says 'cyberpunk Tokyo' → you ask 'Is the lighting for this cyberpunk Tokyo more like Blade Runner's orange-blue split lighting, or Ghost in the Shell's cold green holographic projection feel?' ← This proves you understood AND goes deeper.{en_suffix}]"""

    # Inject round guidance as a separate system message for cleaner context
    if round_guidance:
        msgs.append({"role": "system", "content": round_guidance})
    msgs.append({"role": "user", "content": user_message})
    database.save_message(session_id, "user", user_message, user_turn_count)
    database.update_session(session_id)

    # Model selection logic
    if use_model == "hybrid":
        # Smart mode: use 4o for detailed Round 1 (quality questions matter), mini for vague Round 1
        if user_turn_count == 1 and not is_detailed:
            active_model = "gpt-4o-mini"
        else:
            active_model = "gpt-4o"  # 4o for detailed R1, all R2, and all R3
    elif use_model == "gpt-4o-mini" and user_turn_count >= 3:
        # Even in Fast mode, use 4o for final prompt generation — quality matters most here
        active_model = "gpt-4o"
    else:
        active_model = use_model

    sessions[session_id]["model"] = use_model  # store user's choice

    # More tokens for final prompt generation (Round 3+)
    if user_turn_count >= 3:
        max_tok = 10000
    else:
        max_tok = 4000

    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=msgs,
            max_tokens=max_tok,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"OpenAI request failed: {e}")
        return {
            "error": "AI request failed. Check OPENAI_API_KEY and network access, then try again.",
            "details": str(e),
        }

    raw = response.choices[0].message.content
    msgs.append({"role": "assistant", "content": raw})
    database.save_message(session_id, "assistant", raw or "", user_turn_count)
    database.update_session(session_id)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try cleaning control characters and retry
        cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', raw or '')
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            data = {
                "intro": raw,
                "questions": [],
                "prompt_ready": False,
                "final_prompt": "",
                "prompt_explanation": "",
            }

    # If final_prompt is a dict/list (structured), convert to formatted string
    fp = data.get("final_prompt", "")
    if isinstance(fp, dict):
        fp = _format_structured_prompt(fp)
    elif isinstance(fp, list):
        fp = "\n\n".join(str(item) for item in fp)

    # Auto-expand if final_prompt is too short, retry up to 2 times
    # Auto-expand if too short: 2000 chars minimum for quality prompts
    expand_attempts = 0
    while fp and data.get("prompt_ready") and len(fp) < 2000 and user_turn_count >= 3 and expand_attempts < 2:
        expand_attempts += 1
        print(f"  ⚠️ Final prompt too short ({len(fp)} chars). Expansion attempt {expand_attempts}...")
        if lang == "en":
            expand_msg = f"""Your final_prompt is only {len(fp)} characters — far too short! Target is at least 2000 characters. Please regenerate, this time you MUST:
1. Write at least 150-300 words of detailed description per section, not just 2-3 sentences
2. Add more specific adjectives, material descriptions, color details, spatial relationships, brand references
3. Negative section must list at least 18 specific prohibitions across 3 categories
4. Total length MUST exceed 2000 characters — this is a hard requirement
5. Review the quality benchmark again — your density per section must at least match it
Please output the complete JSON again in the same format."""
        else:
            expand_msg = f"""你的final_prompt只有{len(fp)}字，严重不足！目标是至少2000字。请重新生成，这次必须：
1. 每个章节至少写150-300字的详细描述，不是两三句话就结束
2. 加入更多具体的形容词、材质描写、颜色描写、空间关系、品牌参考
3. Negative部分至少列18个具体禁止项，分3类
4. 总长度必须超过2000字——这是硬性要求
5. 重新看一遍质量标杆，你的每个章节密度必须至少达到标杆的水平
请重新输出完整的JSON，保持同样的格式。"""
        msgs.append({"role": "user", "content": expand_msg})

        try:
            expand_response = client.chat.completions.create(
                model=active_model,
                messages=msgs,
                max_tokens=10000,
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            expand_raw = expand_response.choices[0].message.content
            msgs.append({"role": "assistant", "content": expand_raw})

            expand_data = json.loads(re.sub(r'[\x00-\x1f\x7f]', ' ', expand_raw or ''))
            new_fp = expand_data.get("final_prompt", "")
            if isinstance(new_fp, dict):
                new_fp = _format_structured_prompt(new_fp)
            elif isinstance(new_fp, list):
                new_fp = "\n\n".join(str(item) for item in new_fp)

            if len(new_fp) > len(fp):
                fp = new_fp
                data = expand_data
                print(f"  ✅ Expanded to {len(fp)} chars")
            else:
                print(f"  ⚠️ Expansion didn't help ({len(new_fp)} chars)")
                break
        except Exception as e:
            print(f"  ❌ Expansion failed: {e}")
            break

    # Persist the generated prompt
    prompt_id = None
    if fp and data.get("prompt_ready"):
        first_user_msg = ""
        for m in msgs:
            if m["role"] == "user":
                first_user_msg = m["content"][:100]
                break
        prompt_id = database.save_prompt(
            session_id, fp, data.get("summary", ""), domain, first_user_msg
        )
        database.update_session(session_id)
        # Track prompt generation event
        database.track_event("prompt_generated", f"/prompt/{prompt_id}", "", user_id, _hash_ip(_get_client_ip(request)))

    return {
        "intro": data.get("intro", ""),
        "questions": data.get("questions", []),
        "summary": data.get("summary", ""),
        "prompt_ready": data.get("prompt_ready", False),
        "final_prompt": fp,
        "prompt_explanation": data.get("prompt_explanation", ""),
        "prompt_id": prompt_id,
        "turn": len([m for m in msgs if m["role"] == "user"]),
        "model_used": active_model,
        "domain": domain,
        "lang": lang,
    }


@app.post("/api/reset")
async def reset(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "default")
    sessions.pop(session_id, None)
    return {"ok": True}


@app.get("/api/sessions")
async def list_sessions(limit: int = 30, offset: int = 0, user_id: str = ""):
    """Return recent sessions, optionally filtered by user_id."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    return database.get_sessions(limit=limit, offset=offset, user_id=user_id)


@app.post("/api/format")
async def format_prompt(request: Request):
    """Return a tool-specific version of a generated master prompt."""
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    target_format = (body.get("format") or "raw").strip()
    if not prompt:
        return {"error": "Empty prompt", "formatted": ""}
    return {"format": target_format, "formatted": format_prompt_for_tool(prompt, target_format)}


@app.get("/api/history")
async def history():
    """Return recent prompts for sidebar (DB-backed)."""
    prompts = database.get_prompts(limit=20)
    items = []
    for p in prompts:
        items.append({
            "id": p["id"],
            "session_id": p["session_id"],
            "preview": p["preview_text"] or "Prompt",
            "domain": p["domain"],
            "char_count": p["char_count"],
            "created_at": p["created_at"],
        })
    return items


@app.get("/api/prompts")
async def list_prompts(limit: int = 20, offset: int = 0, domain: str | None = None):
    """Return saved prompts with pagination."""
    prompts = database.get_prompts(limit=limit, offset=offset, domain=domain)
    return prompts


@app.get("/api/prompts/{prompt_id}")
async def get_prompt(prompt_id: int):
    """Get a specific prompt by ID."""
    prompt = database.get_prompt(prompt_id)
    if not prompt:
        return {"error": "Prompt not found"}
    return prompt


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt(prompt_id: int):
    """Soft-delete a prompt."""
    ok = database.delete_prompt(prompt_id)
    return {"ok": ok}


@app.post("/api/prompts/{prompt_id}/rate")
async def rate_prompt(prompt_id: int, request: Request):
    """Rate a prompt: 1 = thumbs up, -1 = thumbs down, 0 = clear."""
    body = await request.json()
    rating = body.get("rating", 0)
    ok = database.rate_prompt(prompt_id, rating)
    return {"ok": ok, "rating": rating}


@app.post("/api/prompts/{prompt_id}/publish")
async def publish_prompt(prompt_id: int, request: Request):
    """Publish or unpublish a prompt from the public gallery."""
    body = await request.json()
    is_public = bool(body.get("is_public", True))
    ok = database.set_prompt_public(prompt_id, is_public)
    return {"ok": ok, "is_public": is_public}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    """Replay a session's messages from DB."""
    db_msgs = database.get_session_messages(session_id)
    if not db_msgs:
        # Fallback to in-memory
        if session_id not in sessions:
            return {"error": "Session not found"}
        msgs = sessions[session_id]["messages"]
        db_msgs = [{"role": m["role"], "content": m["content"]} for m in msgs if m["role"] != "system"]

    replay = []
    for m in db_msgs:
        if m["role"] == "system":
            continue
        if m["role"] == "user":
            replay.append({"role": "user", "content": m["content"]})
        else:
            try:
                data = json.loads(m["content"])
                replay.append({"role": "ai", "data": data})
            except Exception:
                replay.append({"role": "ai", "data": {"intro": m["content"], "questions": []}})
    return {"messages": replay}


@app.get("/api/stats")
async def stats():
    """Return app stats."""
    db_stats = database.get_stats()
    return {
        "kb_prompts": len(KNOWLEDGE_BASE),
        "saved_prompts": db_stats["prompt_count"],
        "sessions": db_stats["session_count"],
        "domains": len(DOMAIN_EXPERTS),
        "thumbs_up": db_stats.get("thumbs_up", 0),
        "thumbs_down": db_stats.get("thumbs_down", 0),
        "subscribers": database.get_subscriber_count(),
    }


def _hash_ip(ip: str) -> str:
    """Hash IP for privacy-preserving analytics."""
    return hashlib.sha256((ip + "mindreader-salt").encode()).hexdigest()[:16]


@app.post("/api/track")
async def track_event(request: Request):
    """Track a page view or event for analytics."""
    body = await request.json()
    event = body.get("event", "pageview")
    path = body.get("path", "/")
    referrer = body.get("referrer", "")
    user_id = body.get("user_id", "")
    ip = _get_client_ip(request)
    database.track_event(event, path, referrer, user_id, _hash_ip(ip))
    return {"ok": True}


@app.get("/api/analytics")
async def analytics_api(request: Request, days: int = 7):
    """Return analytics summary. Simple dashboard data."""
    if not _is_admin_request(request):
        return JSONResponse(status_code=403, content={"error": "Admin token required"})
    days = max(1, min(days, 90))
    return database.get_analytics(days=days)


@app.post("/api/subscribe")
async def subscribe(request: Request):
    """Subscribe to email updates."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email:
        return JSONResponse(status_code=400, content={"error": "Invalid email"})
    is_new = database.add_subscriber(email)
    return {"ok": True, "new": is_new}


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    """Simple analytics dashboard."""
    if not _is_admin_request(request):
        return HTMLResponse(
            "<h1 style='font-family:system-ui;background:#09090b;color:#fafafa;min-height:100vh;margin:0;display:grid;place-items:center'>Analytics locked</h1>",
            status_code=403,
        )
    return HTMLResponse(ANALYTICS_HTML)


ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Analytics — MindReader AI</title>
<meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234F46E5'/%3E%3Ccircle cx='16' cy='16' r='2.5' fill='%23E0E7FF'/%3E%3C/svg%3E">
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{font-family:'Inter',sans-serif;box-sizing:border-box}
body{margin:0;background:#09090b;color:#fafafa;min-height:100vh}
.stat-card{background:#111113;border:1px solid #1e1e22;border-radius:16px;padding:20px;text-align:center}
.stat-value{font-size:32px;font-weight:800;background:linear-gradient(135deg,#818cf8,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-label{font-size:12px;color:#71717a;margin-top:4px;font-weight:500}
.bar{height:24px;border-radius:6px;background:#1e1b4b;transition:width .5s ease}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#6366f1,#818cf8)}
.period-btn{background:#18181b;border:1px solid #27272a;border-radius:8px;padding:5px 14px;font-size:12px;color:#a1a1aa;cursor:pointer;transition:all .2s}
.period-btn:hover{border-color:#6366f1;color:#e0e7ff}
.period-btn.active{background:#1e1b4b;color:#c7d2fe;border-color:#6366f1}
</style>
</head>
<body>
<header class="border-b border-zinc-800/50 px-4 sm:px-6 py-4">
  <div class="max-w-5xl mx-auto flex items-center justify-between">
    <a href="/" class="flex items-center gap-2.5 hover:opacity-80 transition">
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="8" fill="#4F46E5"/>
        <circle cx="16" cy="16" r="2.5" fill="#E0E7FF"/>
      </svg>
      <span class="text-sm font-bold text-white">MindReader AI</span>
      <span class="text-xs text-zinc-600 ml-1">Analytics</span>
    </a>
    <div class="flex gap-2" id="periodBtns">
      <button class="period-btn" onclick="loadData(1,this)">Today</button>
      <button class="period-btn active" onclick="loadData(7,this)">7 Days</button>
      <button class="period-btn" onclick="loadData(30,this)">30 Days</button>
    </div>
  </div>
</header>

<main class="max-w-5xl mx-auto px-4 sm:px-6 py-8">
  <!-- Top stats -->
  <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8" id="topStats">
    <div class="stat-card"><div class="stat-value" id="pvCount">-</div><div class="stat-label">Page Views</div></div>
    <div class="stat-card"><div class="stat-value" id="uvCount">-</div><div class="stat-label">Unique Visitors</div></div>
    <div class="stat-card"><div class="stat-value" id="pgCount">-</div><div class="stat-label">Prompts Generated</div></div>
    <div class="stat-card"><div class="stat-value" id="evCount">-</div><div class="stat-label">Total Events</div></div>
  </div>

  <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
    <!-- Daily chart -->
    <div class="bg-[#111113] border border-[#1e1e22] rounded-2xl p-5">
      <h3 class="text-sm font-semibold text-zinc-300 mb-4">Daily Views</h3>
      <div id="dailyChart" class="space-y-2"></div>
    </div>

    <!-- Events breakdown -->
    <div class="bg-[#111113] border border-[#1e1e22] rounded-2xl p-5">
      <h3 class="text-sm font-semibold text-zinc-300 mb-4">Events</h3>
      <div id="eventsBreakdown" class="space-y-2"></div>
    </div>

    <!-- Top pages -->
    <div class="bg-[#111113] border border-[#1e1e22] rounded-2xl p-5">
      <h3 class="text-sm font-semibold text-zinc-300 mb-4">Top Pages</h3>
      <div id="topPages" class="space-y-2"></div>
    </div>

    <!-- Referrers -->
    <div class="bg-[#111113] border border-[#1e1e22] rounded-2xl p-5">
      <h3 class="text-sm font-semibold text-zinc-300 mb-4">Referrers</h3>
      <div id="referrers" class="space-y-2"></div>
    </div>
  </div>
</main>

<script>
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function loadData(days, btn) {
  if (btn) {
    document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  try {
    const token = new URLSearchParams(location.search).get('token') || '';
    const res = await fetch('/api/analytics?days=' + days + '&token=' + encodeURIComponent(token));
    const d = await res.json();
    render(d);
  } catch { console.error('Failed to load analytics'); }
}

function render(d) {
  const totalEvents = d.events.reduce((s, e) => s + e.cnt, 0);
  document.getElementById('pvCount').textContent = d.pageviews.toLocaleString();
  document.getElementById('uvCount').textContent = d.unique_visitors.toLocaleString();
  document.getElementById('pgCount').textContent = d.prompts_generated.toLocaleString();
  document.getElementById('evCount').textContent = totalEvents.toLocaleString();

  // Daily chart
  const maxViews = Math.max(...d.daily.map(r => r.views), 1);
  const dailyEl = document.getElementById('dailyChart');
  if (d.daily.length === 0) {
    dailyEl.innerHTML = '<div class="text-xs text-zinc-600 text-center py-4">No data yet</div>';
  } else {
    dailyEl.innerHTML = d.daily.map(r => {
      const pct = (r.views / maxViews * 100).toFixed(0);
      const dayLabel = 'Day ' + (r.day_offset + 1);
      return `<div class="flex items-center gap-3">
        <span class="text-[10px] text-zinc-600 w-12 flex-shrink-0">${dayLabel}</span>
        <div class="bar flex-1"><div class="bar-fill" style="width:${pct}%"></div></div>
        <span class="text-xs text-zinc-400 w-12 text-right">${r.views}</span>
      </div>`;
    }).join('');
  }

  // Events
  const maxEvt = Math.max(...d.events.map(e => e.cnt), 1);
  const evEl = document.getElementById('eventsBreakdown');
  evEl.innerHTML = d.events.length ? d.events.map(e => `<div class="flex items-center gap-3">
    <span class="text-xs text-zinc-400 w-32 truncate flex-shrink-0">${esc(e.event)}</span>
    <div class="bar flex-1"><div class="bar-fill" style="width:${(e.cnt/maxEvt*100).toFixed(0)}%"></div></div>
    <span class="text-xs text-zinc-400 w-10 text-right">${e.cnt}</span>
  </div>`).join('') : '<div class="text-xs text-zinc-600 text-center py-4">No events yet</div>';

  // Top pages
  const maxPg = Math.max(...d.top_pages.map(p => p.cnt), 1);
  const pgEl = document.getElementById('topPages');
  pgEl.innerHTML = d.top_pages.length ? d.top_pages.map(p => `<div class="flex items-center gap-3">
    <span class="text-xs text-zinc-400 w-32 truncate flex-shrink-0">${esc(p.path || '/')}</span>
    <div class="bar flex-1"><div class="bar-fill" style="width:${(p.cnt/maxPg*100).toFixed(0)}%"></div></div>
    <span class="text-xs text-zinc-400 w-10 text-right">${p.cnt}</span>
  </div>`).join('') : '<div class="text-xs text-zinc-600 text-center py-4">No data yet</div>';

  // Referrers
  const maxRef = Math.max(...(d.top_referrers || []).map(r => r.cnt), 1);
  const refEl = document.getElementById('referrers');
  refEl.innerHTML = (d.top_referrers || []).length ? d.top_referrers.map(r => `<div class="flex items-center gap-3">
    <span class="text-xs text-zinc-400 w-40 truncate flex-shrink-0">${esc(r.referrer)}</span>
    <div class="bar flex-1"><div class="bar-fill" style="width:${(r.cnt/maxRef*100).toFixed(0)}%"></div></div>
    <span class="text-xs text-zinc-400 w-10 text-right">${r.cnt}</span>
  </div>`).join('') : '<div class="text-xs text-zinc-600 text-center py-4">No referrer data yet</div>';
}

loadData(7);
</script>
</body>
</html>"""


@app.get("/api/gallery")
async def gallery_api(limit: int = 30, offset: int = 0, domain: str | None = None):
    """Return prompts for the public gallery."""
    limit = max(1, min(limit, 50))
    return database.get_gallery_prompts(limit=limit, offset=offset, domain=domain)


@app.get("/gallery", response_class=HTMLResponse)
async def gallery_page():
    """Public prompt gallery page."""
    return HTMLResponse(GALLERY_HTML)


GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prompt Gallery — MindReader AI</title>
<meta name="description" content="Browse professionally crafted AI prompts for Midjourney, DALL-E, ChatGPT, and more. Free prompt gallery by MindReader AI.">
<meta property="og:title" content="Prompt Gallery — MindReader AI">
<meta property="og:description" content="Browse professionally crafted AI prompts for Midjourney, DALL-E, ChatGPT, and more.">
<meta name="theme-color" content="#4F46E5">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234F46E5'/%3E%3Ccircle cx='16' cy='16' r='6' fill='none' stroke='%23C7D2FE' stroke-width='1.5'/%3E%3Ccircle cx='16' cy='16' r='2.5' fill='%23E0E7FF'/%3E%3C/svg%3E">
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{font-family:'Inter',sans-serif;box-sizing:border-box}
body{margin:0;background:#09090b;color:#fafafa;min-height:100vh}
.domain-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;text-transform:uppercase;letter-spacing:.3px}
.domain-badge.video{background:#1a1a2e;color:#818cf8;border:1px solid #312e81}
.domain-badge.image{background:#1a2e1a;color:#86efac;border:1px solid #22543d}
.domain-badge.design{background:#2e1a2e;color:#f0abfc;border:1px solid #581c87}
.domain-badge.writing{background:#2e2a1a;color:#fcd34d;border:1px solid #854d0e}
.domain-badge.code{background:#1a2e2e;color:#67e8f9;border:1px solid #155e75}
.domain-badge.general{background:#1e1e22;color:#a1a1aa;border:1px solid #3f3f46}
.gallery-card{background:#111113;border:1px solid #1e1e22;border-radius:16px;padding:20px;transition:all .2s;cursor:pointer}
.gallery-card:hover{border-color:#6366f1;transform:translateY(-2px);box-shadow:0 8px 30px rgba(99,102,241,.1)}
.filter-btn{background:#18181b;border:1px solid #27272a;border-radius:8px;padding:6px 14px;font-size:12px;color:#a1a1aa;cursor:pointer;transition:all .2s;font-weight:500}
.filter-btn:hover{border-color:#6366f1;color:#e0e7ff}
.filter-btn.active{background:#1e1b4b;color:#c7d2fe;border-color:#6366f1}
.fade-in{animation:fadeIn .4s ease-out}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<!-- Header -->
<header class="border-b border-zinc-800/50 px-4 sm:px-6 py-4">
  <div class="max-w-6xl mx-auto flex items-center justify-between">
    <a href="/" class="flex items-center gap-2.5 hover:opacity-80 transition">
      <svg width="28" height="28" viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="8" fill="#4F46E5"/>
        <circle cx="16" cy="16" r="6" fill="none" stroke="#C7D2FE" stroke-width="1.5"/>
        <circle cx="16" cy="16" r="2.5" fill="#E0E7FF"/>
      </svg>
      <span class="text-sm font-bold text-white">MindReader AI</span>
    </a>
    <a href="/" class="text-sm text-indigo-400 hover:text-indigo-300 font-medium transition">&#10024; Create Your Own</a>
  </div>
</header>

<!-- Gallery -->
<main class="max-w-6xl mx-auto px-4 sm:px-6 py-8">
  <div class="text-center mb-8">
    <h1 class="text-3xl font-bold text-white mb-2">Prompt Gallery</h1>
    <p class="text-zinc-500 text-sm">Browse professionally crafted prompts generated by MindReader AI</p>
  </div>

  <!-- Domain filters -->
  <div class="flex flex-wrap justify-center gap-2 mb-8" id="filters">
    <button class="filter-btn active" onclick="filterDomain(null,this)">All</button>
    <button class="filter-btn" onclick="filterDomain('video',this)">&#127916; Video</button>
    <button class="filter-btn" onclick="filterDomain('image',this)">&#127912; Image</button>
    <button class="filter-btn" onclick="filterDomain('design',this)">&#127912; Design</button>
    <button class="filter-btn" onclick="filterDomain('writing',this)">&#9997; Writing</button>
    <button class="filter-btn" onclick="filterDomain('code',this)">&#128187; Code</button>
  </div>

  <!-- Grid -->
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" id="galleryGrid">
    <div class="text-center text-zinc-600 py-12 col-span-full">Loading prompts...</div>
  </div>

  <!-- Load more -->
  <div class="text-center mt-8" id="loadMoreWrap" style="display:none">
    <button onclick="loadMore()" class="filter-btn" id="loadMoreBtn">Load More</button>
  </div>
</main>

<footer class="text-center text-xs text-zinc-700 py-6 border-t border-zinc-800/50 mt-8">
  <a href="/" class="text-indigo-500 hover:text-indigo-400">MindReader AI</a> — AI Prompt Generator
</footer>

<script>
let currentDomain = null;
let currentOffset = 0;
const PAGE_SIZE = 18;

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function loadGallery(append = false) {
  const grid = document.getElementById('galleryGrid');
  if (!append) {
    grid.innerHTML = '<div class="text-center text-zinc-600 py-12 col-span-full">Loading prompts...</div>';
    currentOffset = 0;
  }

  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: currentOffset });
  if (currentDomain) params.set('domain', currentDomain);

  try {
    const res = await fetch('/api/gallery?' + params);
    const prompts = await res.json();

    if (!append) grid.innerHTML = '';

    if (prompts.length === 0 && currentOffset === 0) {
      grid.innerHTML = '<div class="text-center text-zinc-600 py-12 col-span-full">No prompts yet. Be the first to create one!</div>';
      document.getElementById('loadMoreWrap').style.display = 'none';
      return;
    }

    prompts.forEach(p => {
      const date = new Date(p.created_at * 1000);
      const timeStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
      const summary = p.summary || p.preview_text || 'Prompt';
      const ratingIcon = p.rating > 0 ? '&#128077;' : '';

      const card = document.createElement('div');
      card.className = 'gallery-card fade-in';
      card.innerHTML = `
        <div class="flex items-center gap-2 mb-3">
          <span class="domain-badge ${p.domain}">${p.domain}</span>
          <span class="text-[10px] text-zinc-600">#${p.id}</span>
          ${ratingIcon ? '<span class="text-xs ml-auto">' + ratingIcon + '</span>' : ''}
        </div>
        <div class="text-sm text-zinc-300 leading-relaxed mb-3 line-clamp-3" style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer" onclick="window.location.href='/prompt/${p.id}'">${esc(summary)}</div>
        <div class="flex items-center gap-3 text-[10px] text-zinc-600">
          <span>${(p.char_count/1000).toFixed(1)}k chars</span>
          <span>${p.section_count || 0} sections</span>
          <a href="/?remix=${p.id}" class="ml-auto text-indigo-400 hover:text-indigo-300 font-medium text-[11px]" onclick="event.stopPropagation()">&#128260; Remix</a>
        </div>
      `;
      grid.appendChild(card);
    });

    document.getElementById('loadMoreWrap').style.display = prompts.length >= PAGE_SIZE ? '' : 'none';
    currentOffset += prompts.length;
  } catch {
    if (!append) grid.innerHTML = '<div class="text-center text-red-400 py-12 col-span-full">Failed to load prompts</div>';
  }
}

function filterDomain(domain, btn) {
  currentDomain = domain;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadGallery();
}

function loadMore() {
  loadGallery(true);
}

loadGallery();

// Track gallery page view
fetch('/api/track', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ event: 'pageview', path: '/gallery', referrer: document.referrer || '' }),
}).catch(() => {});
</script>
</body>
</html>"""


@app.exception_handler(404)
async def not_found(request: Request, exc):
    """Custom 404 page."""
    return HTMLResponse(
        """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>404 — MindReader AI</title>
<style>*{font-family:Inter,system-ui,sans-serif}body{margin:0;background:#09090b;color:#fafafa;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}
a{color:#818cf8;text-decoration:none}a:hover{text-decoration:underline}</style></head>
<body><div><h1 style="font-size:72px;margin:0;color:#27272a">404</h1>
<p style="color:#71717a;margin:16px 0">This page doesn't exist.</p>
<a href="/">Go to MindReader AI &rarr;</a></div></body></html>""",
        status_code=404,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7777)
