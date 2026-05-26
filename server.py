"""MindReader AI — Backend server."""
from __future__ import annotations

import json
import os
import re
import time
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


def build_domain_context(domain: str) -> str:
    """Build domain-specific expert context to inject into system prompt."""
    expert = DOMAIN_EXPERTS.get(domain)
    if not expert or domain == "general":
        general = DOMAIN_EXPERTS.get("general", {})
        if general:
            return f"\n\n## DETECTED DOMAIN: 通用\n{general.get('expert_role', '')}"
        return ""

    ctx = f"\n\n## DETECTED DOMAIN: {expert['display_name']}\n"
    ctx += f"{expert['expert_role']}\n\n"

    # Must-ask dimensions
    ctx += "### 你必须深入挖掘的维度（每个维度至少问到）：\n"
    for dim in expert.get("must_ask_dimensions", []):
        ctx += f"\n**{dim['dimension']}**"
        if "why" in dim:
            ctx += f" — {dim['why']}"
        ctx += "\n"
        if "good_question" in dim:
            ctx += f"  好的问题示例: {dim['good_question']}\n"
        if "bad_question" in dim:
            ctx += f"  ❌ 差的问题: {dim['bad_question']}\n"
        if "example_questions" in dim:
            for eq in dim["example_questions"][:2]:
                ctx += f"  - {eq}\n"

    # Output structure
    output = expert.get("output_structure", {})
    if output.get("sections"):
        ctx += "\n### 最终prompt必须包含这些章节：\n"
        for sec in output["sections"]:
            ctx += f"- **{sec['name']}**: {sec['description']}\n"

    # Good example
    if output.get("good_example_summary"):
        ctx += f"\n### 专业级prompt示例（这是你要达到的质量标准）：\n{output['good_example_summary'][:2000]}\n"

    return ctx


def build_round_context(domain: str, round_num: int) -> str:
    """Build round-specific guidance from domain expert."""
    expert = DOMAIN_EXPERTS.get(domain, DOMAIN_EXPERTS.get("general", {}))
    guidance = expert.get("round_guidance", {})

    round_key = f"round{min(round_num, 4)}"
    round_text = guidance.get(round_key, "")

    if round_num == 1:
        return f"\n\n[SYSTEM: This is Round 1. {round_text}]"
    elif round_num == 2:
        return f"\n\n[SYSTEM: This is Round 2. {round_text}]"
    elif round_num >= 3:
        # Inject the good_example as quality benchmark for final prompt
        output_struct = expert.get("output_structure", {})
        good_example = output_struct.get("good_example_summary", "")
        sections = output_struct.get("sections", [])
        # Use domain-specific density rules if available
        domain_density = output_struct.get("round4_density_rules", "")

        section_names = [s["name"] for s in sections]
        section_list = "、".join(section_names)

        # Calculate target length from good_example
        target_len = max(len(good_example), 1500)

        # Build density section — domain-specific or fallback
        if domain_density:
            density_block = domain_density
        else:
            density_block = """## 密度硬性要求：
- 每个章节至少80字，要有具体细节不是笼统描述
- Negative：至少分3类，每类至少4项，共至少15个禁止项
- 总长度：至少1200字"""

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
- 你的final_prompt必须至少{target_len}字
- 质量标杆有{len(good_example)}字——你的输出不能比它短
- 如果你发现自己写完了但总字数不够，立刻给每个章节补充更多具体细节——加形容词、加材质描写、加参考对标、加具体数值
- 宁可写2500字的专业prompt也不要写1000字的敷衍prompt
- 每写完一个章节都检查一下它的密度——如果整个章节只有两三句话，说明你太敷衍了

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
- label: ≤6个字，清晰分类
- description: 不是字典定义！是一个让用户脑海浮现画面的微场景。
- ❌ 差：{"label": "产品展示", "description": "展示某个产品的功能与特点"}
- ✅ 好：{"label": "产品展示", "description": "像Apple发布会那样——一个产品从黑暗中缓缓旋转出现，每个细节特写，配合精准的文字节奏"}
- ❌ 差：{"label": "感动", "description": "让观众感受到情感的共鸣"}
- ✅ 好：{"label": "感动", "description": "像Pixar《UP》开头——不说一句话，用画面让人安静流泪"}

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


def _build_initial_system_message(domain: str, first_message: str) -> str:
    domain_context = build_domain_context(domain)
    relevant = find_relevant_prompts(first_message, limit=3)
    kb_context = ""
    if relevant:
        kb_context = "\n\n## Reference Prompts from Knowledge Base\nUse these as INSPIRATION only:\n\n"
        for i, p in enumerate(relevant, 1):
            kb_context += f"### Reference {i}: {p['act']}\n{p['prompt'][:500]}\n\n"
    return SYSTEM_PROMPT + domain_context + kb_context


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

    sessions[session_id] = {
        "messages": restored,
        "created_at": session.get("created_at", time.time()),
        "domain": domain,
        "model": session.get("model", "hybrid"),
    }
    print(f"Restored session {session_id[:8]}... | Domain: {domain} | Messages: {len(db_messages)}")
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

  <div class="flex gap-3 mt-4">
    <button onclick="copyPrompt()" id="copyBtn" class="bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-semibold transition">Copy Prompt</button>
    <a href="/" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded-lg px-4 py-2 text-sm font-semibold transition inline-block">Create Your Own</a>
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
        # Detect domain from user's first message
        detected_domain = detect_domain(user_message)

        sessions[session_id] = {
            "messages": [{"role": "system", "content": _build_initial_system_message(detected_domain, user_message)}],
            "created_at": time.time(),
            "domain": detected_domain,
        }
        print(f"New session {session_id[:8]}... | Domain: {detected_domain}")
        database.save_session(session_id, detected_domain, use_model, user_message, user_id=user_id)

    msgs = sessions[session_id]["messages"]
    domain = sessions[session_id].get("domain", "general")

    # Count user turns to determine round
    user_turn_count = len([m for m in msgs if m["role"] == "user"]) + 1

    # Inject domain-specific round guidance
    round_guidance = build_round_context(domain, user_turn_count)

    # For Round 1, add input-depth-adaptive guidance
    if user_turn_count == 1:
        msg_len = len(user_message.strip())
        expert = DOMAIN_EXPERTS.get(domain, DOMAIN_EXPERTS.get("general", {}))

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
            "video": "ask about character appearance details (materials, colors, textures, wear/damage), environment specifics (objects, time of day, weather), and the precise nature of the mood.",
            "design": "ask about the core visual concept, specific colors (not just 'blue' but what shade), layout preferences, typography personality, and references they like or dislike.",
            "writing": "ask about the specific message, target reader's current state vs desired state, tone (give a reference person/brand voice), and what cliches to avoid.",
            "code": "ask about the core user flow step by step, data model, tech stack preferences, scale/performance requirements, and what they explicitly DON'T want.",
            "general": "ask about specific details they haven't mentioned yet.",
        }
        domain_detail = detail_hints.get(domain, detail_hints["general"])

        if msg_len < 30:
            # Check if domain has preset questions for vague inputs
            preset = expert.get("round1_preset_vague")
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
                }

            round_guidance += f"\n[INPUT_DEPTH: VAGUE — user said very little. Ask: 1) What specifically is this about? ({domain_opts}), 2) Do you have any specific picture/idea in mind? (open text with good placeholder), 3) What's the core goal/feeling? Do NOT ask about expert-level details yet — the user hasn't told you enough.]"
        else:
            round_guidance += f"\n[INPUT_DEPTH: DETAILED — user already told you a lot. Do NOT repeat their information back as questions — that's insulting. They already told you the topic and mood. Instead {domain_detail} NEVER ask 'what is this about' or 'what feeling' when they already told you.]"

    # Inject round guidance as a separate system message for cleaner context
    if round_guidance:
        msgs.append({"role": "system", "content": round_guidance})
    msgs.append({"role": "user", "content": user_message})
    database.save_message(session_id, "user", user_message, user_turn_count)
    database.update_session(session_id)

    # Model selection logic
    if use_model == "hybrid":
        # Smart mode: mini for round 1 only, 4o for round 2+ (better questions + generation)
        active_model = "gpt-4o" if user_turn_count >= 2 else "gpt-4o-mini"
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
    expand_attempts = 0
    while fp and data.get("prompt_ready") and len(fp) < 1500 and user_turn_count >= 3 and expand_attempts < 2:
        expand_attempts += 1
        print(f"  ⚠️ Final prompt too short ({len(fp)} chars). Expansion attempt {expand_attempts}...")
        expand_msg = f"""你的final_prompt只有{len(fp)}字，严重不足！目标是至少1500字。请重新生成，这次必须：
1. 每个章节至少写100-250字的详细描述，不是两三句话就结束
2. 加入更多具体的形容词、材质描写、颜色描写、空间关系、品牌参考
3. Negative部分至少列18个具体禁止项，分3类
4. 总长度必须超过1500字——这是硬性要求
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
    }


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
