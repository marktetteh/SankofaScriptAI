"""
SankofahScriptAI — Cambridge IGCSE Mathematics 0580
Upload a student's answer paper (photo or PDF).
Gemma reads the printed questions AND the student's handwritten answers,
separates them, and marks each question using Cambridge conventions.
"""

import os, json, sqlite3, base64, uuid, csv, io
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_OK = True
except ImportError:
    PYPDF_OK = False

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_BASE    = os.getenv("OLLAMA_BASE",    "http://localhost:11434")
GEMMA_MODEL    = os.getenv("GEMMA_MODEL",    "gemma:2b")
DB_PATH        = os.getenv("DB_PATH",        "grademate.db")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_MODEL   = os.getenv("GOOGLE_MODEL",   "gemma-4-31b-it")
GOOGLE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
USE_GOOGLE     = bool(GOOGLE_API_KEY)

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    conn = get_db()
    # Create tables if they don't exist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            class_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id                TEXT PRIMARY KEY,
            student_id        TEXT,
            class_name        TEXT NOT NULL,
            paper_type        TEXT,
            assessment_type   TEXT DEFAULT 'Class Exercise',
            exercise_number   INTEGER DEFAULT 1,
            marks_awarded     REAL DEFAULT 0,
            max_marks         REAL DEFAULT 0,
            percentage        REAL DEFAULT 0,
            grade             TEXT DEFAULT 'U',
            questions_data    TEXT DEFAULT '[]',
            overall_feedback  TEXT DEFAULT '',
            teacher_notes     TEXT DEFAULT '',
            marked_at         TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS classes (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
    """)
    # Migrate: add any missing columns to existing submissions table
    existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
    migrations = {
        "paper_type":       "TEXT DEFAULT ''",
        "assessment_type":  "TEXT DEFAULT 'Class Exercise'",
        "exercise_number":  "INTEGER DEFAULT 1",
        "questions_data":   "TEXT DEFAULT '[]'",
        "overall_feedback": "TEXT DEFAULT ''",
        "teacher_notes":    "TEXT DEFAULT ''",
        "max_marks":        "REAL DEFAULT 0",
        "marks_awarded":    "REAL DEFAULT 0",
        "percentage":       "REAL DEFAULT 0",
        "grade":            "TEXT DEFAULT 'U'",
    }
    for col, definition in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {definition}")
            print(f"[DB] Migrated: added column '{col}' to submissions")
    conn.commit()
    conn.close()

def save_submission(conn, student_name, class_name, result,
                    assessment_type="Class Exercise", exercise_number=1):
    sid     = str(uuid.uuid4())[:8]
    stud_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT OR IGNORE INTO students (id, name, class_name) VALUES (?,?,?)",
        (stud_id, student_name, class_name))
    conn.execute(
        "INSERT OR IGNORE INTO classes (id, name) VALUES (?,?)",
        (str(uuid.uuid4())[:8], class_name))
    conn.execute("""
        INSERT INTO submissions
          (id, student_id, class_name, paper_type, assessment_type, exercise_number,
           marks_awarded, max_marks, percentage, grade,
           questions_data, overall_feedback, teacher_notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, stud_id, class_name,
         result.get("paper_type", "Unknown"),
         assessment_type,
         exercise_number,
         result.get("total_marks_awarded", 0),
         result.get("total_marks_available", 0),
         result.get("percentage", 0),
         result.get("grade", "U"),
         json.dumps(result.get("questions", [])),
         result.get("overall_feedback", ""),
         result.get("teacher_notes", "")))
    return sid

# ── AI Callers ────────────────────────────────────────────────────────────────

QUESTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "question_number":  {"type": "STRING"},
        "question_text":    {"type": "STRING"},
        "student_answer":   {"type": "STRING"},
        "marks_available":  {"type": "NUMBER"},
        "marks_awarded":    {"type": "NUMBER"},
        "mark_breakdown":   {"type": "STRING"},
        "correct":          {"type": "BOOLEAN"},
        "feedback":         {"type": "STRING"},
        "correct_answer":   {"type": "STRING"},   # final correct answer
        "worked_solution":  {"type": "STRING"},   # step-by-step working
    },
    "required": ["question_number","question_text","student_answer",
                 "marks_available","marks_awarded","feedback",
                 "correct_answer","worked_solution"]
}

MARKING_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "paper_type":             {"type": "STRING"},
        "paper_code":             {"type": "STRING"},
        "total_marks_available":  {"type": "NUMBER"},
        "total_marks_awarded":    {"type": "NUMBER"},
        "percentage":             {"type": "NUMBER"},
        "grade":                  {"type": "STRING"},
        "questions":              {"type": "ARRAY", "items": QUESTION_SCHEMA},
        "overall_feedback":       {"type": "STRING"},
        "teacher_notes":          {"type": "STRING"},
    },
    "required": ["paper_type","total_marks_available","questions","overall_feedback"]
}

# Lighter schema used for each PDF chunk (no grade/percentage — those are computed after merging)
CHUNK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "paper_type":            {"type": "STRING"},
        "paper_code":            {"type": "STRING"},
        "total_marks_available": {"type": "NUMBER"},
        "questions":             {"type": "ARRAY", "items": QUESTION_SCHEMA},
        "chunk_feedback":        {"type": "STRING"},
        "teacher_notes":         {"type": "STRING"},
    },
    "required": ["paper_type", "questions"]
}


async def _google_request(parts: list, gen_config: dict) -> str:
    """Internal: send one request to the Google AI API, return text content."""
    payload = {
        "system_instruction": {
            "parts": [{"text": (
                "You are an experienced Cambridge IGCSE Mathematics 0580 examiner. "
                "Your response MUST be a single valid JSON object with no text before or after it. "
                "Follow the provided response schema exactly. "
                "Never include explanations, markdown, or prose outside the JSON structure."
            )}]
        },
        "contents": [{"parts": parts}],
        "generationConfig": gen_config,
    }
    url = f"{GOOGLE_API_URL}/{GOOGLE_MODEL}:generateContent?key={GOOGLE_API_KEY}"
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(url, json=payload)
        except httpx.ConnectError:
            raise HTTPException(503, "Cannot reach Google AI API — check your internet connection and try again.")
        except httpx.TimeoutException:
            raise HTTPException(504, "Google AI API timed out — the image may be too large or the network is slow.")
        except httpx.RequestError as e:
            raise HTTPException(503, f"Network error contacting Google AI: {type(e).__name__}")
        if resp.status_code != 200:
            raise HTTPException(resp.status_code,
                                f"Google AI error ({resp.status_code}): {resp.text[:600]}")
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise HTTPException(500,
                                f"AI returned no candidates: {json.dumps(data)[:400]}")
        finish_reason = candidates[0].get("finishReason", "unknown")
        out_parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in out_parts).strip()
        print(f"[AI] finishReason={finish_reason}  response_len={len(text)}")
        if not text:
            print(f"[AI] Empty text. Candidate: {json.dumps(candidates[0])[:400]}")
        return text


async def call_google(prompt: str, file_b64: str, mime_type: str, schema: dict = None) -> str:
    """Single-call Google AI request (used by batch marking and PDF chunks)."""
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": mime_type, "data": file_b64}}
    ]
    effective_schema = schema if schema is not None else MARKING_SCHEMA
    schema_label = "CHUNK" if schema is CHUNK_SCHEMA else "FULL"

    # Try with schema first for batch/PDF (these are already well-structured calls)
    gen_config = {
        "temperature": 0.1,
        "maxOutputTokens": 65536,
        "topP": 0.9,
        "responseMimeType": "application/json",
        "responseSchema": effective_schema,
    }
    print(f"[AI] {GOOGLE_MODEL} | {schema_label} schema")
    text = await _google_request(parts, gen_config)
    print(f"[AI] Response preview: {text[:300]}")
    return text

async def call_ollama(prompt: str, file_b64: str) -> str:
    payload = {
        "model":   GEMMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "images":  [file_b64],
        "options": {"temperature": 0.1, "top_p": 0.9, "num_predict": 4096}
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(f"{OLLAMA_BASE}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")

async def call_ai(prompt: str, file_b64: str, mime_type: str = "image/jpeg", schema: dict = None) -> str:
    if USE_GOOGLE:
        return await call_google(prompt, file_b64, mime_type, schema)
    return await call_ollama(prompt, file_b64)

# ── Prompts ───────────────────────────────────────────────────────────────────

def paper_prompt(paper_type_hint: str = "Auto-detect") -> str:
    hint = (
        f"This is a Cambridge IGCSE Mathematics 0580 {paper_type_hint} paper."
        if paper_type_hint != "Auto-detect"
        else "This is a Cambridge IGCSE Mathematics 0580 paper (Core or Extended — detect from the paper)."
    )
    return f"""You are an experienced Cambridge IGCSE Mathematics 0580 examiner marking a student's completed answer paper.

{hint}

HOW THIS PAPER IS LAID OUT:
- QUESTIONS are TYPESET/PRINTED — uniform, clean machine font
- Mark allocations are in brackets, e.g. [2] or [3] — these are the authoritative marks per question
- STUDENT ANSWERS are HANDWRITTEN — irregular human strokes in pen or pencil (any colour)
- Answers are on dotted lines, blank spaces, or drawn directly on diagrams
- Even when pen colour matches printed text, handwriting is identifiable by irregular letterforms

YOUR TASK — follow these steps in order:

STEP 1 — SCAN FIRST: Before writing anything, scan the entire paper from top to bottom. Note EVERY question number you can see (e.g. 1(a), 1(b)(i), 1(b)(ii), 1(c), 2(a)…). Count them all. Do not start generating until you have identified every question on the page.

STEP 2 — MARK EACH ONE: For every question you identified in Step 1, add an entry to the "questions" array. Do not skip any. If you identified 5 questions in Step 1, the array must have exactly 5 entries.

For EACH question and sub-question identify:
   - The printed question text and its bracketed mark allocation
   - What the student wrote or drew as their answer/working
3. Mark each answer using Cambridge IGCSE M/A/B mark conventions:
   - M mark: method mark — award for correct method even if final answer is wrong
   - A mark: accuracy mark — only if dependent M mark was earned
   - B mark: independent mark — regardless of method
   - FT: follow-through — award if the answer follows correctly from a previous wrong answer
   - Do NOT penalise the same error twice across question parts
   - Blank answer space → marks_awarded = 0, note "No attempt"
4. For diagram/graph questions, describe what the student drew and assess correctness

ACCURACY RULES — follow these strictly:
- marks_awarded MUST be a whole number between 0 and marks_available (never exceed marks_available)
- marks_available MUST match the integer in the brackets on the paper, e.g. [2] → 2
- If you cannot clearly read the student's handwriting, describe what you can see and give benefit of the doubt for partially correct working
- Do not award marks for a blank space even if the question is easy
- Always check: does marks_awarded + (remaining marks) = marks_available for each question?

FOR EACH QUESTION populate these fields:
- question_number: e.g. "1(a)" or "3(b)(ii)"
- question_text: the full printed question
- student_answer: exactly what the student wrote/drew, or "No attempt"
- marks_available: the integer from the brackets on the paper
- marks_awarded: integer you are awarding
- mark_breakdown: e.g. "M1 awarded – correct method; A0 – wrong final answer"
- correct: true if fully correct, false otherwise
- feedback: 1–2 sentences of specific helpful feedback addressed to the student
- correct_answer: the final correct answer only (e.g. "x = 4.5" or "-180")
- worked_solution: full step-by-step Cambridge working showing exactly how to reach the correct answer (show all method steps, e.g. "Step 1: 5 × -3 = -15. Step 2: -3 × -4 = 12. Step 3: -15 × 12 = -180.")

FOR THE OVERALL RESULT populate:
- paper_type: "Core" or "Extended"
- paper_code: e.g. "0580/33" if visible on the cover, otherwise empty string
- total_marks_available: SUM of all question marks_available values
- total_marks_awarded: SUM of all question marks_awarded values
- overall_feedback: 2–3 encouraging sentences addressed directly to the student
- teacher_notes: 1–2 sentences for the teacher highlighting patterns"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def chunk_prompt(page_start: int, page_end: int, total_pages: int,
                 paper_type_hint: str, is_first: bool) -> str:
    """Prompt for a single page-chunk of the paper."""
    if is_first:
        context = (
            f"These are pages {page_start}–{page_end} of {total_pages} of a Cambridge IGCSE "
            f"Mathematics 0580 answer paper. The cover page is included — read it for the "
            f"paper code and paper type (Core or Extended)."
        )
    else:
        context = (
            f"These are pages {page_start}–{page_end} of {total_pages} of a Cambridge IGCSE "
            f"Mathematics 0580 {paper_type_hint} answer paper. "
            f"This is a continuation — extract only the questions that appear on these pages."
        )

    return f"""You are an experienced Cambridge IGCSE Mathematics 0580 examiner.

{context}

HOW THIS PAPER IS LAID OUT:
- QUESTIONS are TYPESET/PRINTED — uniform machine font
- Mark allocations are in brackets, e.g. [2] or [3] — use these as marks_available
- STUDENT ANSWERS are HANDWRITTEN — irregular human strokes (any pen colour or pencil)
- Answers are on dotted lines, blank spaces, or drawn directly on diagrams

TASK — SCAN FIRST, then mark. Before writing the JSON, scan all pages and note every question number visible (1(a), 1(b)(i), 2(a), etc.). Then include every one of them in the "questions" array.
1. Read the printed question text and its bracketed mark allocation
2. Find the student's handwritten answer/working
3. Mark using Cambridge conventions (M/A/B marks, follow-through, no double penalty)
4. If blank → marks_awarded = 0, student_answer = "No attempt"

FOR EACH QUESTION populate:
- question_number: e.g. "1(a)" or "3(b)(ii)"
- question_text: the full printed question
- student_answer: exactly what the student wrote/drew, or "No attempt"
- marks_available: the integer from the brackets
- marks_awarded: integer you are awarding
- mark_breakdown: e.g. "M1 awarded – correct method; A0 – wrong answer"
- correct: true if fully correct, false otherwise
- feedback: 1–2 sentences of specific helpful feedback

Also populate:
- paper_type: "Core" or "Extended"
- paper_code: paper code from cover page (e.g. "0580/33") if visible, else empty string
- total_marks_available: total marks for the WHOLE paper from cover page if visible (0 if not on these pages)
- chunk_feedback: one sentence observation about student performance on these pages"""


async def mark_pdf_chunked(pdf_bytes: bytes, paper_type_hint: str) -> dict:
    """Split a multi-page PDF into 4-page chunks, mark each, then combine."""
    if not PYPDF_OK:
        # No pypdf — send the whole PDF in one shot
        b64 = base64.b64encode(pdf_bytes).decode()
        raw = await call_ai(paper_prompt(paper_type_hint), b64, "application/pdf")
        return finalise(parse_json(raw))

    reader     = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    CHUNK      = 4   # pages per chunk — keeps output well within token limits
    print(f"[PDF] {total_pages} pages → {((total_pages-1)//CHUNK)+1} chunks of {CHUNK} pages")

    all_questions    = []
    paper_type       = paper_type_hint
    total_marks_avail = 0
    feedback_parts   = []
    teacher_parts    = []

    for start in range(0, total_pages, CHUNK):
        end      = min(start + CHUNK, total_pages)
        is_first = (start == 0)

        # Build chunk PDF
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunk_b64 = base64.b64encode(buf.getvalue()).decode()

        prompt = chunk_prompt(start + 1, end, total_pages, paper_type, is_first)
        print(f"[PDF] Marking pages {start+1}–{end}…")

        try:
            raw    = await call_ai(prompt, chunk_b64, "application/pdf", schema=CHUNK_SCHEMA)
            result = parse_json(raw)
            chunk_qs = result.get("questions", [])
            q_nums   = [q.get("question_number", "?") for q in chunk_qs]
            print(f"[PDF] Chunk {start+1}-{end}: response_len={len(raw)}  questions={len(chunk_qs)} — {q_nums}")
            # Clamp marks per question
            for q in chunk_qs:
                avail   = max(0, int(q.get("marks_available", 0)))
                awarded = max(0, int(q.get("marks_awarded",  0)))
                q["marks_available"] = avail
                q["marks_awarded"]   = min(awarded, avail)
        except Exception as ex:
            print(f"[PDF] Chunk {start+1}-{end} error: {ex}")
            result = {}

        # Grab paper metadata from first chunk
        if is_first:
            paper_type        = result.get("paper_type", paper_type_hint)
            total_marks_avail = result.get("total_marks_available", 0)

        qs = result.get("questions", [])
        all_questions.extend(qs)
        if result.get("chunk_feedback"):
            feedback_parts.append(result["chunk_feedback"])
        if result.get("teacher_notes"):
            teacher_parts.append(result["teacher_notes"])

    # Compute totals strictly from individual question marks.
    # Each question carries its own mark allocation read from the brackets on the paper,
    # so the sum of those is the correct denominator for the percentage.
    # We do NOT use the cover-page paper total — that represents the whole exam paper,
    # but a student may only be submitting one exercise/section of it.
    awarded   = sum(q.get("marks_awarded",  0) for q in all_questions)
    available = sum(q.get("marks_available", 0) for q in all_questions)

    combined = {
        "paper_type":            paper_type,
        "total_marks_available": available,
        "total_marks_awarded":   awarded,
        "percentage":            0,
        "grade":                 "U",
        "questions":             all_questions,
        "overall_feedback":      " ".join(feedback_parts),
        "teacher_notes":         " ".join(teacher_parts),
    }
    return finalise(combined)


def parse_json(text: str) -> dict:
    text = text.strip()

    # Strip markdown code fences
    if "```" in text:
        import re
        m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
        if m:
            text = m.group(1).strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find outermost { } — handles preamble text before the JSON
    s = text.find("{")
    if s != -1:
        # Walk from the end to find the matching closing brace
        depth = 0
        for i, ch in enumerate(text[s:], start=s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:i+1])
                    except json.JSONDecodeError:
                        break

    # Last resort — strip everything before first { and after last }
    s, e = text.find("{"), text.rfind("}") + 1
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e])
        except json.JSONDecodeError:
            pass

    print(f"[PARSE] Failed to extract JSON from response (len={len(text)}): {text[:300]}")
    # Fallback
    return {
        "paper_type": "Unknown",
        "total_marks_available": 0,
        "total_marks_awarded": 0,
        "percentage": 0,
        "grade": "U",
        "questions": [],
        "overall_feedback": text[:500] if text else "Could not parse AI response.",
        "teacher_notes": "Structured response not returned — check terminal for raw output."
    }

def grade_from_pct(pct: float) -> str:
    if pct >= 90: return "A*"
    if pct >= 80: return "A"
    if pct >= 70: return "B"
    if pct >= 60: return "C"
    if pct >= 50: return "D"
    if pct >= 40: return "E"
    return "U"

def mime_for(filename: str) -> str:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):   return "application/pdf"
    if fn.endswith(".png"):   return "image/png"
    if fn.endswith(".webp"):  return "image/webp"
    return "image/jpeg"

def finalise(result: dict) -> dict:
    avail = result.get("total_marks_available", 0)
    awarded = result.get("total_marks_awarded", 0)
    if avail > 0:
        result["percentage"] = round((awarded / avail) * 100, 1)
    result["grade"] = grade_from_pct(result.get("percentage", 0))
    return result

# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    provider = f"Google AI Studio ({GOOGLE_MODEL})" if USE_GOOGLE else f"Ollama ({GEMMA_MODEL})"
    print(f"\n{'='*55}")
    print(f"  SankofahScriptAI — Cambridge IGCSE 0580")
    print(f"  Provider : {provider}")
    print(f"  API      : http://localhost:8000")
    print(f"{'='*55}\n")
    yield

app = FastAPI(title="SankofahScriptAI", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Endpoints ─────────────────────────────────────────────────────────────────

def _clamp_marks(parsed: dict) -> dict:
    """Ensure marks_awarded never exceeds marks_available on any question."""
    for q in parsed.get("questions", []):
        avail   = max(0, int(q.get("marks_available", 0)))
        awarded = max(0, int(q.get("marks_awarded",  0)))
        q["marks_available"] = avail
        q["marks_awarded"]   = min(awarded, avail)
    qs = parsed.get("questions", [])
    parsed["total_marks_awarded"]   = sum(q.get("marks_awarded",  0) for q in qs)
    parsed["total_marks_available"] = sum(q.get("marks_available", 0) for q in qs)
    return parsed


async def _mark_image_with_retry(prompt: str, file_b64: str, mime: str) -> dict:
    """
    Two-step marking strategy:
      Step 1 — Send image with NO JSON enforcement so the model freely analyses
               the whole paper and identifies every question in natural language.
      Step 2 — Feed that analysis back as text-only (no image) and ask the model
               to format it as JSON using responseSchema.
               This avoids the schema-induced early stopping that happens when the
               image + schema are combined in one call.
    Fallback — if both steps fail, send image + schema directly (old approach).
    """
    image_parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": mime, "data": file_b64}}
    ]

    # ── Step 1: free-form analysis (no JSON, no schema) ──────────────────────
    gen_free = {
        "temperature": 0.1,
        "maxOutputTokens": 65536,
        "topP": 0.9,
    }
    print(f"[MARK] Step 1: free-form image analysis…")
    analysis = ""
    try:
        analysis = await _google_request(image_parts, gen_free)
        print(f"[MARK] Step 1: analysis_len={len(analysis)}")

        # If step 1 already returned valid JSON with enough questions, use it
        if analysis.strip().startswith("{") and '"questions"' in analysis:
            parsed = parse_json(analysis)
            qs = parsed.get("questions", [])
            if len(qs) >= 3:
                print(f"[MARK] Step 1 returned complete JSON ({len(qs)} questions) — done")
                return _clamp_marks(parsed)
    except HTTPException:
        pass   # step 1 failed — still try step 2 with whatever we have

    # ── Step 2: text-only JSON conversion using step 1's analysis ────────────
    if analysis:
        conversion_prompt = (
            "You just analysed a Cambridge IGCSE Mathematics 0580 answer paper and produced "
            "the following detailed marking analysis. Now convert your ENTIRE analysis into "
            "the required JSON format. You MUST include every question you identified — "
            "do not skip or merge any questions.\n\n"
            f"YOUR ANALYSIS:\n{analysis[:10000]}\n\n"
            "Convert all questions above to JSON. The 'questions' array must have one entry "
            "for every question and sub-question you identified in your analysis."
        )
        gen_schema = {
            "temperature": 0.1,
            "maxOutputTokens": 65536,
            "topP": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": MARKING_SCHEMA,
        }
        print(f"[MARK] Step 2: converting analysis to JSON (text-only)…")
        try:
            text_parts = [{"text": conversion_prompt}]
            json_text  = await _google_request(text_parts, gen_schema)
            parsed     = parse_json(json_text)
            qs         = parsed.get("questions", [])
            q_nums     = [q.get("question_number", "?") for q in qs]
            print(f"[MARK] Step 2: questions={len(qs)} — {q_nums}")
            if len(qs) >= 2:
                return _clamp_marks(parsed)
        except Exception as ex:
            print(f"[MARK] Step 2 failed: {ex}")

    # ── Fallback: image + schema in one shot ─────────────────────────────────
    print(f"[MARK] Fallback: image + schema…")
    gen_schema_img = {
        "temperature": 0.1,
        "maxOutputTokens": 65536,
        "topP": 0.9,
        "responseMimeType": "application/json",
        "responseSchema": MARKING_SCHEMA,
    }
    raw    = await _google_request(image_parts, gen_schema_img)
    parsed = parse_json(raw)
    qs     = parsed.get("questions", [])
    print(f"[MARK] Fallback: questions={len(qs)} — {[q.get('question_number','?') for q in qs]}")
    return _clamp_marks(parsed)


@app.get("/api/health")
async def health():
    provider_info = {"provider": "Google AI Studio", "model": GOOGLE_MODEL} if USE_GOOGLE else {"provider": "Ollama", "model": GEMMA_MODEL}
    if not USE_GOOGLE:
        try:
            async with httpx.AsyncClient(timeout=4.0) as c:
                r = await c.get(f"{OLLAMA_BASE}/api/tags")
                provider_info["models"] = [m["name"] for m in r.json().get("models", [])]
                provider_info["ollama_running"] = True
        except Exception:
            provider_info["ollama_running"] = False
    return {"status": "ok", **provider_info}


@app.post("/api/mark/paper")
async def mark_paper(
    student_name:     str        = Form(...),
    class_name:       str        = Form(...),
    paper_type:       str        = Form("Auto-detect"),
    assessment_type:  str        = Form("Class Exercise"),
    exercise_number:  int        = Form(1),
    file:             UploadFile = File(...),
):
    """Mark a single student's answer paper (photo or PDF)."""
    raw_bytes = await file.read()
    mime      = mime_for(file.filename)
    file_b64  = base64.b64encode(raw_bytes).decode()
    prompt    = paper_prompt(paper_type)

    size_kb = len(raw_bytes) // 1024
    print(f"\n[MARK] {student_name} | {class_name} | {assessment_type} #{exercise_number} | {file.filename} ({mime}) | {size_kb}KB")
    if size_kb < 150 and mime != "application/pdf":
        print(f"[MARK] WARNING: image is only {size_kb}KB — low resolution may reduce accuracy. Recommend photos >300KB.")

    if mime == "application/pdf":
        result = await mark_pdf_chunked(raw_bytes, paper_type)
    else:
        parsed = await _mark_image_with_retry(prompt, file_b64, mime)
        result = finalise(parsed)

    # Attach assessment metadata to result so it's returned to frontend
    result["assessment_type"]  = assessment_type
    result["exercise_number"]  = exercise_number
    result["marked_at"]        = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db()
    try:
        sub_id = save_submission(conn, student_name, class_name, result,
                                 assessment_type, exercise_number)
        conn.commit()
    finally:
        conn.close()

    return {"submission_id": sub_id, "student_name": student_name, **result}


@app.post("/api/mark/batch")
async def mark_batch(
    class_name:       str              = Form(...),
    paper_type:       str              = Form("Auto-detect"),
    assessment_type:  str              = Form("Class Exercise"),
    exercise_number:  int              = Form(1),
    student_names:    str              = Form(...),
    files:            List[UploadFile] = File(...),
):
    """Mark multiple students' papers in one request."""
    try:
        names = json.loads(student_names)
    except Exception:
        names = []

    results = []
    prompt  = paper_prompt(paper_type)

    for i, file in enumerate(files):
        student_name = names[i] if i < len(names) else f"Student {i + 1}"
        raw_bytes    = await file.read()
        mime         = mime_for(file.filename)
        file_b64     = base64.b64encode(raw_bytes).decode()

        print(f"\n[BATCH {i+1}/{len(files)}] {student_name} | {file.filename}")
        try:
            raw    = await call_ai(prompt, file_b64, mime)
            result = parse_json(raw)
            result = finalise(result)
        except Exception as ex:
            print(f"[BATCH] Error for {student_name}: {ex}")
            result = {
                "paper_type": "Unknown", "total_marks_available": 0,
                "total_marks_awarded": 0, "percentage": 0, "grade": "U",
                "questions": [], "overall_feedback": f"Error: {ex}",
                "teacher_notes": "Could not process this paper."
            }

        result["assessment_type"] = assessment_type
        result["exercise_number"] = exercise_number
        result["marked_at"]       = datetime.now().strftime("%Y-%m-%d %H:%M")

        conn = get_db()
        try:
            sub_id = save_submission(conn, student_name, class_name, result,
                                     assessment_type, exercise_number)
            conn.commit()
        finally:
            conn.close()

        results.append({"submission_id": sub_id, "student_name": student_name, **result})

    awarded_list = [r["total_marks_awarded"] for r in results]
    avg = round(sum(awarded_list) / len(awarded_list), 1) if awarded_list else 0
    return {
        "class_name":    class_name,
        "total_marked":  len(results),
        "class_average": avg,
        "results":       results
    }


@app.patch("/api/submissions/{submission_id}/amend")
async def amend_submission(submission_id: str, payload: dict):
    """
    Teacher amendment — update marks for one or more questions and recompute totals.
    Payload: { "questions": [ { "question_number": "1(a)", "marks_awarded": 2 }, ... ] }
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT questions_data, max_marks FROM submissions WHERE id=?",
            (submission_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Submission not found")

        questions = json.loads(row["questions_data"] or "[]")
        amendments = {q["question_number"]: q["marks_awarded"]
                      for q in payload.get("questions", [])}

        # Apply amendments
        for q in questions:
            qn = q.get("question_number", "")
            if qn in amendments:
                new_val = int(amendments[qn])
                available = q.get("marks_available", 0)
                # Clamp to valid range
                q["marks_awarded"] = max(0, min(new_val, available))
                q["amended"] = True   # flag so UI can show it was teacher-edited

        # Recompute totals
        awarded   = sum(q.get("marks_awarded",  0) for q in questions)
        available = sum(q.get("marks_available", 0) for q in questions)
        pct       = round((awarded / available) * 100, 1) if available > 0 else 0
        grade     = grade_from_pct(pct)

        conn.execute("""
            UPDATE submissions
            SET questions_data=?, marks_awarded=?, percentage=?, grade=?
            WHERE id=?""",
            (json.dumps(questions), awarded, pct, grade, submission_id))
        conn.commit()
    finally:
        conn.close()

    return {
        "submission_id":       submission_id,
        "total_marks_awarded": awarded,
        "total_marks_available": available,
        "percentage":          pct,
        "grade":               grade,
        "questions":           questions,
    }


@app.get("/api/submissions/{class_name}")
async def list_submissions(class_name: str):
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*, st.name as student_name
        FROM submissions s
        LEFT JOIN students st ON s.student_id = st.id
        WHERE s.class_name = ?
        ORDER BY s.marked_at DESC""", (class_name,)).fetchall()
    conn.close()
    return [
        {**dict(r), "questions": json.loads(r["questions_data"] or "[]")}
        for r in rows
    ]


@app.delete("/api/submissions/{submission_id}")
async def delete_submission(submission_id: str):
    """Delete a single student submission by ID."""
    conn = get_db()
    try:
        result = conn.execute(
            "DELETE FROM submissions WHERE id = ?", (submission_id,))
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Submission not found")
    finally:
        conn.close()
    return {"deleted": submission_id}


@app.delete("/api/submissions/group/{assessment_type}/{exercise_number}/{class_name}")
async def delete_group(assessment_type: str, exercise_number: int, class_name: str):
    """Delete all submissions for a specific assessment group."""
    conn = get_db()
    try:
        result = conn.execute(
            "DELETE FROM submissions WHERE assessment_type=? AND exercise_number=? AND class_name=?",
            (assessment_type, exercise_number, class_name))
        conn.commit()
        deleted = result.rowcount
    finally:
        conn.close()
    return {"deleted_count": deleted}


@app.get("/api/classes")
async def list_classes():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.name,
               COUNT(s.id)          AS submission_count,
               ROUND(AVG(s.percentage), 1) AS avg_percentage
        FROM classes c
        LEFT JOIN submissions s ON c.name = s.class_name
        GROUP BY c.name
        ORDER BY c.name""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/analytics/{class_name}")
async def class_analytics(class_name: str):
    conn = get_db()
    subs = conn.execute(
        "SELECT * FROM submissions WHERE class_name=?", (class_name,)).fetchall()
    conn.close()
    if not subs:
        return {"class_name": class_name, "total_submissions": 0}
    n = len(subs)
    grades: dict = {}
    total_pct = 0.0
    for s in subs:
        g = s["grade"] or "U"
        grades[g] = grades.get(g, 0) + 1
        total_pct += s["percentage"] or 0
    return {
        "class_name":         class_name,
        "total_submissions":  n,
        "class_average":      round(total_pct / n, 1),
        "grade_distribution": grades,
    }


@app.get("/api/export/{class_name}")
async def export_csv(class_name: str):
    conn = get_db()
    rows = conn.execute("""
        SELECT st.name, s.marks_awarded, s.max_marks, s.percentage,
               s.grade, s.paper_type, s.overall_feedback, s.marked_at
        FROM submissions s
        LEFT JOIN students st ON s.student_id = st.id
        WHERE s.class_name = ?
        ORDER BY st.name""", (class_name,)).fetchall()
    conn.close()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["Student", "Marks Awarded", "Max Marks", "Percentage",
                "Grade", "Paper Type", "Feedback", "Date Marked"])
    for r in rows:
        w.writerow(list(r))
    out.seek(0)
    fname = f"SankofahScriptAI_{class_name}_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@app.get("/api/export-exercise")
async def export_exercise_csv(
    assessment_type:  str = "Class Exercise",
    exercise_number:  int = 1,
    class_name:       str = "",
):
    """Export a CSV for one specific exercise group (assessment type + number + class)."""
    conn = get_db()
    if class_name:
        rows = conn.execute("""
            SELECT st.name, s.class_name, s.marks_awarded, s.max_marks, s.percentage,
                   s.grade, s.paper_type, s.assessment_type, s.exercise_number,
                   s.overall_feedback, s.teacher_notes, s.marked_at
            FROM submissions s
            LEFT JOIN students st ON s.student_id = st.id
            WHERE s.assessment_type = ? AND s.exercise_number = ? AND s.class_name = ?
            ORDER BY s.percentage DESC""",
            (assessment_type, exercise_number, class_name)).fetchall()
    else:
        rows = conn.execute("""
            SELECT st.name, s.class_name, s.marks_awarded, s.max_marks, s.percentage,
                   s.grade, s.paper_type, s.assessment_type, s.exercise_number,
                   s.overall_feedback, s.teacher_notes, s.marked_at
            FROM submissions s
            LEFT JOIN students st ON s.student_id = st.id
            WHERE s.assessment_type = ? AND s.exercise_number = ?
            ORDER BY s.class_name, s.percentage DESC""",
            (assessment_type, exercise_number)).fetchall()
    conn.close()

    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["Student", "Class", "Marks Awarded", "Max Marks", "Percentage",
                "Grade", "Paper Type", "Assessment Type", "Exercise No.",
                "Student Feedback", "Teacher Notes", "Date Marked"])
    for r in rows:
        w.writerow(list(r))
    out.seek(0)

    safe_type = assessment_type.replace(" ", "_")
    cls_part  = f"_{class_name.replace(' ', '_')}" if class_name else ""
    fname     = f"SankofahScriptAI_{safe_type}_{exercise_number}{cls_part}_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


# ── Serve frontend ────────────────────────────────────────────────────────────

FRONTEND = Path(__file__).parent.parent / "frontend" / "index.html"

@app.get("/")
async def serve_frontend():
    if FRONTEND.exists():
        return FileResponse(FRONTEND, media_type="text/html")
    return {"error": "Frontend not found", "expected_path": str(FRONTEND)}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
