# MindReader AI

MindReader AI is a prompt architect app. It asks expert-level questions, then generates a dense, tool-ready prompt for image, video, design, writing, code, or general creative work.

## Run locally

```bash
cd "/Users/mr.nobody/Documents/New project/mindreader-ai"
./start.sh
```

Then open:

```text
http://127.0.0.1:7777
```

## Environment

Create a `.env` file with:

```bash
OPENAI_API_KEY=your_api_key_here
```

The app can load without `python-dotenv`, but installing the requirements is recommended.

## What Works

- Three-round MindReader flow: understand, deep dive, generate
- Domain detection for video, image, design, writing, code, and general prompts
- SQLite persistence for sessions, messages, and generated prompts
- Session replay after refresh or backend restart
- Prompt library
- Tool-specific export formats for Midjourney, ChatGPT, DALL-E, and Stable Diffusion

## Quick Checks

```bash
python3 -m py_compile server.py db.py
```

If dependencies are installed:

```bash
python3 -m uvicorn server:app --host 127.0.0.1 --port 7777
```
