# GradeMate — Offline-First AI Marking Assistant

**Gemma 4 Good Hackathon Submission | Future of Education Track**

GradeMate is an AI-powered marking assistant that helps Cambridge IGCSE ICT teachers mark student work in seconds — completely offline. Built on Gemma 4 running locally via Ollama, it provides rubric-aligned marking, structured feedback, and class analytics without needing internet connectivity.

## The Problem

Secondary school ICT teachers in Cambridge-affiliated schools spend 10-15 hours per week manually marking student practical work and written answers. In many schools across Africa and Asia, unreliable internet makes cloud-based tools impractical.

## The Solution

GradeMate runs entirely on the teacher's laptop:
- **Text marking**: Paste a student's written answer, get marks + feedback per Cambridge criteria
- **Image marking**: Upload a screenshot of a spreadsheet/database — Gemma 4 sees and marks it visually (multimodal)
- **Batch marking**: Mark an entire class in one click
- **Smart rubrics**: Pre-loaded Cambridge IGCSE ICT marking schemes
- **Class analytics**: See grade distribution, identify weak topics across the class
- **CSV export**: Download results for school records

## Why Gemma 4?

1. **Offline-first**: Runs locally via Ollama — no cloud, no internet needed
2. **Multimodal**: Gemma 4's vision capabilities mark screenshots of spreadsheets, databases, and web pages
3. **Edge deployment**: Works on a teacher's laptop (16GB RAM for 27B model)
4. **Privacy**: Student data never leaves the teacher's machine

## Tech Stack

| Layer | Technology |
|-------|-----------|
| AI Model | Gemma 4 27B via Ollama |
| Backend | Python + FastAPI |
| Frontend | HTML/CSS/JS (single file, no build step) |
| Database | SQLite (zero setup) |
| Deployment | Teacher's laptop — no server needed |

## Quick Start

### 1. Install Ollama & Pull Gemma

```bash
# Download from https://ollama.com
# Then:
ollama pull gemma3:27b
ollama serve
```

> Use `gemma3:12b` if your laptop has less than 16GB RAM.

### 2. Start the Backend

```bash
cd backend
pip install -r requirements.txt
python main.py
```

The API runs on `http://localhost:8000`.

### 3. Open the Frontend

Open `frontend/index.html` in any browser. The green status indicator confirms Gemma is connected.

### 4. Load Default Rubrics

Click **Rubrics** → **Load Cambridge ICT Defaults** to get pre-built marking schemes for spreadsheet, database, web development, and theory tasks.

## Project Structure

```
grademate/
├── backend/
│   ├── main.py              # FastAPI server + all endpoints
│   └── requirements.txt     # Python dependencies
├── frontend/
│   └── index.html           # Complete single-page application
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Check Ollama + Gemma status |
| `/api/rubrics` | GET/POST | List/create rubrics |
| `/api/rubrics/seed-defaults` | POST | Load Cambridge ICT defaults |
| `/api/mark/single` | POST | Mark one student's text answer |
| `/api/mark/batch` | POST | Mark multiple students at once |
| `/api/mark/image` | POST | Mark a screenshot (multimodal) |
| `/api/analytics/{class}` | GET | Class performance analytics |
| `/api/export/{class}` | GET | Download CSV report |

## Real-World Impact

Built by an ICT teacher at a Cambridge-affiliated school in Accra, Ghana. Tested with real student work in a real classroom. This tool directly addresses:

- **Time**: Reduces marking time from hours to minutes
- **Consistency**: Same rubric applied fairly to every student
- **Feedback quality**: Every student gets detailed, criterion-level feedback
- **Offline access**: Works in schools with unreliable internet
- **Scalability**: Any Cambridge ICT teacher worldwide can use this

## License

MIT — built for the Gemma 4 Good Hackathon 2026.
