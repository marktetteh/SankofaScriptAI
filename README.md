# SankofahScriptAI — AI Marking Assistant for Cambridge IGCSE Mathematics

> *Sankofa* — a Twi word meaning "go back and fetch what was forgotten."  
> SankofahScriptAI gives teachers back the hours lost to manual marking.

AI-powered marking assistant for Cambridge IGCSE Mathematics 0580. Upload a photo or PDF of a student's handwritten answer paper — the app reads both the printed questions and handwritten answers, marks each question using Cambridge M/A/B conventions, and returns per-question feedback, worked solutions, and a final grade.

Built on **Gemma 4** (`gemma-4-31b-it`) via Google AI Studio. Runs on any teacher's laptop — no cloud subscription, no per-mark fee.

---

## Quick Start

### Prerequisites
- [Python 3.9+](https://www.python.org/downloads/) — tick **"Add Python to PATH"** during install
- A free Google API key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

### Clone & Run (Windows)

```bash
git clone https://github.com/marktetteh/SankofaScriptAI
cd SankofaScriptAI
```

Then double-click **`launch.bat`**.

On first run it will ask for your Google API key, save it, install dependencies, and open the app in your browser automatically.

### Clone & Run (Mac / Linux)

```bash
git clone https://github.com/marktetteh/SankofaScriptAI
cd SankofaScriptAI
chmod +x launch.sh
./launch.sh
```

Same thing — enter your API key once, and the app opens at `http://localhost:8000`.

> On every run after the first, just double-click `launch.bat` (or `./launch.sh`) — no setup needed.

---

## What It Does

- **Upload** a photo or PDF of a student's handwritten answer paper
- **AI marks** each question using Cambridge M/A/B mark scheme conventions
- **Per-question feedback** — what the student did right, what went wrong
- **Worked solutions** — step-by-step correct working for every question
- **Grade** (A* → U) with percentage
- **Teacher amendment** — override any AI mark with one click
- **Class history** — all submissions stored, grouped by exercise
- **CSV export** — download results for school records
- **Mobile-friendly** — teachers on the same WiFi can use it from their phone

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| AI Model | Gemma 4 (`gemma-4-31b-it`) via Google AI Studio |
| Backend | Python + FastAPI + SQLite |
| Frontend | Single-file HTML/CSS/JS — no build step |
| Deployment | Teacher's laptop — no server or Docker needed |

---

## How the Marking Engine Works

Forcing a multimodal LLM to return structured JSON from a complex handwritten image hits a known failure: when `responseSchema` is active, the model treats it as a completeness constraint and stops after 2–3 questions.

SankofahScriptAI uses a **two-step chain**:

1. **Step 1 — Free analysis**: send the image with no JSON schema. The model freely analyses the whole paper in natural language, identifying every question and every student answer.
2. **Step 2 — JSON conversion**: feed that analysis back as text only (no image) and ask the model to format it as structured JSON. Smaller context → model completes the full array.

Result: all questions marked every time, with no truncation.

---

## Project Structure

```
SankofaScriptAI/
├── backend/
│   ├── main.py              # FastAPI server — all endpoints + marking engine
│   └── requirements.txt     # Python dependencies
├── frontend/
│   └── index.html           # Complete single-page application
├── launch.bat               # Windows one-click launcher
├── launch.sh                # Mac/Linux one-click launcher
├── sample_paper.pdf         # Example student paper for testing
└── README.md
```

---

## Impact

A mathematics teacher in Ghana with 200 students across 5 classes, marking 8 exercises per term:

| | Manual | SankofahScriptAI |
|--|--|--|
| Time per paper | 8–15 min | ~45 seconds |
| Time per class (40 students) | 5–10 hours | ~30 minutes |
| Feedback quality | Tick/cross + total | Per-question feedback + worked solutions |
| Term marking total | ~320 hours | ~27 hours |

**293 hours returned to teaching every term.**

---

## License

MIT — built for the Gemma 4 Good Hackathon 2026.
