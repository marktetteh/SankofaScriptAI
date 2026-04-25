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


_SYSTEM_JSON = (
    "You are an experienced Cambridge IGCSE Mathematics 0580 examiner. "
    "Your response MUST be a single valid JSON object with no text before or after it. "
    "Follow the provided response schema exactly. "
    "Never include explanations, markdown, or prose outside the JSON structure."
)
_SYSTEM_FREE = (
    "You are an experienced Cambridge IGCSE Mathematics 0580 examiner. "
    "Analyse the student's answer paper carefully and produce your marking notes "
    "in the exact plain-text format requested in the user message. "
    "Do NOT output JSON — write each question block using the --- Q<num> --- format shown. "
    "Follow the format in the prompt exactly."
)

async def _google_request(parts: list, gen_config: dict, free_form: bool = False) -> str:
    """Internal: send one request to the Google AI API, return text content.
    Use free_form=True for Step 1 analysis calls that expect plain text output.
    Automatically retries up to 3 times on transient 500 errors."""
    import asyncio
    payload = {
        "system_instruction": {
            "parts": [{"text": _SYSTEM_FREE if free_form else _SYSTEM_JSON}]
        },
        "contents": [{"parts": parts}],
        "generationConfig": gen_config,
    }
    url = f"{GOOGLE_API_URL}/{GOOGLE_MODEL}:generateContent?key={GOOGLE_API_KEY}"

    last_error = None
    for attempt in range(1, 5):   # up to 4 attempts
        try:
            async with httpx.AsyncClient(timeout=240.0) as client:
                resp = await client.post(url, json=payload)
        except httpx.ConnectError:
            raise HTTPException(503, "Cannot reach Google AI API — check your internet connection.")
        except httpx.TimeoutException:
            raise HTTPException(504, "Google AI API timed out — the file may be too large.")
        except httpx.RequestError as e:
            raise HTTPException(503, f"Network error: {type(e).__name__}")

        if resp.status_code == 200:
            break
        # Retry on 500 (transient Google internal error) with backoff
        if resp.status_code == 500 and attempt < 4:
            wait = attempt * 5
            print(f"[AI] 500 INTERNAL on attempt {attempt} — retrying in {wait}s…")
            await asyncio.sleep(wait)
            last_error = resp.text
            continue
        raise HTTPException(resp.status_code,
                            f"Google AI error ({resp.status_code}): {resp.text[:600]}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise HTTPException(500, f"AI returned no candidates: {json.dumps(data)[:400]}")
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

# Used as Step 1 of the two-step chain.  Replaces the long paper_prompt for the
# free-form analysis pass so the model produces a compact ~1–2 KB notes block
# instead of 10 KB of flowing prose.  Cutting Step 1 output from ~2500 tokens
# to ~300 tokens reduces Step 1 latency from ~25 s to ~3–5 s.
COMPACT_ANALYSIS_PROMPT = """Cambridge IGCSE 0580 Mathematics examiner. Mark the student's answer paper shown.

STEP 1 — SCAN ALL QUESTIONS FIRST (do not start marking yet):
Read the ENTIRE paper top to bottom. List EVERY question part you can see, including:
- Main questions: 1, 2, 3 …
- Sub-questions: 1(a), 1(b), 1(c) …
- Sub-sub-questions: 1(b)(i), 1(b)(ii), 2(a)(i) …
- Questions inside tables, diagrams, or grids

Write them as a single comma-separated line, e.g.:
Questions found: 1(a), 1(b)(i), 1(b)(ii), 1(c), 2(a), 2(b), 3(a), 3(b), 4

IMPORTANT: Cambridge IGCSE papers typically have 5–30 question parts. If you see fewer than 5, scan again — they are present. The question numbers and mark allocations in brackets [n] are printed on the paper.

STEP 2 — MARK EACH QUESTION: For EVERY question you listed in Step 1, write a compact block. Do NOT skip any.

--- Q<num> [<marks>M] ---
Student: <exact student answer, or "No attempt">
Award: <n>/<marks>
Answer: <correct final answer, e.g. "x=3" or "−180">
Solution: <concise step-by-step, e.g. "Step 1: 5×(−3)=−15. Step 2: (−3)×(−4)=12. Step 3: −15+12=−3.">
Feedback: <one sentence for the student>

RULE: The number of Q-blocks in Step 2 MUST exactly match the number of questions listed in Step 1.

After ALL questions, write:
Paper: <Core/Extended> | Code: <0580/XX if visible, else blank>
Overall: <2 sentences of encouragement for the student>
Teacher: <1 sentence about patterns noticed>

Keep every block SHORT. One line per field."""

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
- correct_answer: the final correct answer only (e.g. "x = 4.5" or "-180")
- worked_solution: full step-by-step Cambridge working (e.g. "Step 1: … Step 2: …")

Also populate:
- paper_type: "Core" or "Extended"
- paper_code: paper code from cover page (e.g. "0580/33") if visible, else empty string
- total_marks_available: total marks for the WHOLE paper from cover page if visible (0 if not on these pages)
- chunk_feedback: one sentence observation about student performance on these pages"""


async def _mark_chunk_two_step(prompt: str, chunk_b64: str, chunk_label: str, mime: str = "application/pdf") -> dict:
    """Two-step marking for a single PDF chunk — same approach as image marking.

    Step 1: Free-form analysis (no schema) → model reads ALL questions.
    Step 2: Text-only JSON conversion (with CHUNK_SCHEMA) → structured output.
    Step 3 (fallback): If < 5 questions returned, direct image+schema call.
    """
    import re as _re
    # Use compact prompt for Step 1 to minimise output length → faster
    compact_parts = [{"text": COMPACT_ANALYSIS_PROMPT}, {"inline_data": {"mime_type": mime, "data": chunk_b64}}]
    gen_free      = {"temperature": 0.1, "maxOutputTokens": 8192, "topP": 0.9}

    # ── Step 1 ───────────────────────────────────────────────────────────────
    print(f"[PDF] {chunk_label} Step 1: compact analysis…")
    analysis = await _google_request(compact_parts, gen_free, free_form=True)
    print(f"[PDF] {chunk_label} analysis_len={len(analysis)}")

    # If step 1 already returned complete JSON with ≥5 questions, use it directly
    if analysis.strip().startswith("{") and '"questions"' in analysis:
        parsed = parse_json(analysis)
        if len(parsed.get("questions", [])) >= 5:
            print(f"[PDF] {chunk_label} Step 1 returned complete JSON — skipping Step 2")
            return parsed

    # ── Step 2 ───────────────────────────────────────────────────────────────
    # Parse both the "Questions found:" list and the Q-block headers
    step1_q_list = _re.findall(r'Questions found:(.*?)(?:\n|$)', analysis)
    if step1_q_list:
        # Parse comma-separated list e.g. "1(a), 1(b)(i), 2(a)"
        step1_q_nums = [q.strip() for q in step1_q_list[0].split(',') if q.strip()]
    else:
        step1_q_nums = _re.findall(r'---\s*Q([\d()a-zA-Z]+)', analysis)

    expected_count = len(step1_q_nums) if step1_q_nums else "all"
    print(f"[PDF] {chunk_label} Step 2: JSON conversion (expected {expected_count} questions: {step1_q_nums})…")
    conversion_prompt = (
        "You just analysed pages of a Cambridge IGCSE Mathematics 0580 answer paper and produced "
        "the following marking notes. Convert your ENTIRE analysis into the required JSON format.\n\n"
        f"CRITICAL: Your analysis identified {expected_count} questions: {step1_q_nums}. "
        "You MUST include EVERY SINGLE ONE in the 'questions' array — do not skip, merge, or drop any. "
        f"The final 'questions' array MUST have exactly {expected_count} entries.\n"
        "If a question has sub-parts (e.g. 1(a), 1(b)), each sub-part is a SEPARATE entry.\n\n"
        f"YOUR ANALYSIS:\n{analysis}\n\n"
        "Convert ALL questions above to JSON now. Also populate: paper_type, paper_code, "
        "total_marks_available, chunk_feedback."
    )
    gen_schema = {
        "temperature": 0.1, "maxOutputTokens": 65536, "topP": 0.9,
        "responseMimeType": "application/json",
        "responseSchema": CHUNK_SCHEMA,
    }
    print(f"[AI] {GOOGLE_MODEL} | CHUNK schema (Step 2 text-only)")
    json_text = await _google_request([{"text": conversion_prompt}], gen_schema)
    parsed    = parse_json(json_text)
    q_nums    = [q.get("question_number", "?") for q in parsed.get("questions", [])]
    print(f"[PDF] {chunk_label} Step 2: questions={len(q_nums)} — {q_nums}")

    # ── Step 3: fallback if < 5 questions (or far below expected) ────────────
    expected_int = len(step1_q_nums) if step1_q_nums else 0
    got = len(q_nums)
    if got < 5 or (expected_int >= 5 and got < expected_int // 2):
        print(f"[PDF] {chunk_label} Only {got} questions — fallback to direct image+schema…")
        try:
            gen_direct = {
                "temperature": 0.2, "maxOutputTokens": 65536, "topP": 0.95,
                "responseMimeType": "application/json",
                "responseSchema": CHUNK_SCHEMA,
            }
            direct_parts = [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": chunk_b64}}]
            fb_text   = await _google_request(direct_parts, gen_direct)
            fb_parsed = parse_json(fb_text)
            fb_qs     = fb_parsed.get("questions", [])
            if len(fb_qs) > got:
                print(f"[PDF] {chunk_label} Fallback improved: {got} → {len(fb_qs)} questions")
                return fb_parsed
        except Exception as fb_ex:
            print(f"[PDF] {chunk_label} Fallback also failed: {fb_ex}")

    return parsed


def pdf_to_jpeg_pages(pdf_bytes: bytes) -> list:
    """Convert each PDF page to a JPEG image using pymupdf.
    Returns list of (jpeg_bytes, page_number) tuples.
    Falls back to raw PDF chunks if pymupdf is unavailable."""
    try:
        import fitz  # pymupdf
        doc    = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages  = []
        for i, page in enumerate(doc):
            mat = fitz.Matrix(1.5, 1.5)          # 1.5× zoom — good quality, smaller payload
            pix = page.get_pixmap(matrix=mat, alpha=False)
            # Compress JPEG at 82% quality — sharp enough for handwriting, smaller file
            jpeg_bytes = pix.tobytes("jpeg", jpg_quality=82)
            pages.append((jpeg_bytes, i + 1))
        doc.close()
        print(f"[PDF] Converted {len(pages)} pages to JPEG via pymupdf")
        return pages
    except ImportError:
        print("[PDF] pymupdf not available — falling back to PDF chunks")
        return []


async def mark_pdf_chunked(pdf_bytes: bytes, paper_type_hint: str) -> dict:
    """Convert PDF pages to JPEG images (one page at a time) then mark each.
    JPEG images are smaller and more reliable than raw PDF chunks with the AI."""

    # ── Try PDF→JPEG conversion first (most reliable) ─────────────────────────
    jpeg_pages = pdf_to_jpeg_pages(pdf_bytes)

    if jpeg_pages:
        total_pages   = len(jpeg_pages)
        all_questions = []
        paper_type    = paper_type_hint
        feedback_parts = []
        teacher_parts  = []

        print(f"[PDF] Marking {total_pages} pages as JPEG images…")
        for idx, (jpeg_bytes, page_num) in enumerate(jpeg_pages):
            import asyncio
            # Small pause between pages to avoid hitting API rate limits
            if idx > 0:
                await asyncio.sleep(2)

            is_first    = (page_num == 1)
            prompt      = chunk_prompt(page_num, page_num, total_pages, paper_type, is_first)
            chunk_label = f"Page {page_num}"
            print(f"[PDF] Marking {chunk_label} ({len(jpeg_bytes)//1024}KB JPEG)…")

            result = None
            # Attempt 1: normal quality
            try:
                img_b64 = base64.b64encode(jpeg_bytes).decode()
                result  = await _mark_chunk_two_step(prompt, img_b64, chunk_label, mime="image/jpeg")
            except Exception as ex:
                print(f"[PDF] {chunk_label} attempt 1 failed: {ex} — retrying at lower quality…")
                await asyncio.sleep(5)

            # Attempt 2: heavily compressed (smaller payload)
            if result is None:
                try:
                    import fitz
                    doc2 = fitz.open(stream=pdf_bytes, filetype="pdf")
                    mat2 = fitz.Matrix(1.0, 1.0)   # 1× zoom, smaller
                    pix2 = doc2[page_num - 1].get_pixmap(matrix=mat2, alpha=False)
                    small_jpeg = pix2.tobytes("jpeg", jpg_quality=65)
                    doc2.close()
                    print(f"[PDF] {chunk_label} attempt 2: {len(small_jpeg)//1024}KB compressed JPEG…")
                    img_b64 = base64.b64encode(small_jpeg).decode()
                    result  = await _mark_chunk_two_step(prompt, img_b64, chunk_label, mime="image/jpeg")
                except Exception as ex2:
                    print(f"[PDF] {chunk_label} attempt 2 also failed: {ex2} — skipping page")
                    continue   # skip this page, keep going with the rest

            chunk_qs = result.get("questions", [])
            for q in chunk_qs:
                avail   = max(0, int(q.get("marks_available", 0)))
                awarded = max(0, int(q.get("marks_awarded",  0)))
                q["marks_available"] = avail
                q["marks_awarded"]   = min(awarded, avail)

            if is_first:
                paper_type = result.get("paper_type", paper_type_hint)
            all_questions.extend(result.get("questions", []))
            if result.get("chunk_feedback"):
                feedback_parts.append(result["chunk_feedback"])
            if result.get("teacher_notes"):
                teacher_parts.append(result["teacher_notes"])

        awarded   = sum(q.get("marks_awarded",  0) for q in all_questions)
        available = sum(q.get("marks_available", 0) for q in all_questions)

        # ── Minimum question check ────────────────────────────────────────────
        # If the whole paper returned < 5 questions across all pages, something
        # went wrong.  Retry by sending the first page again with a "forced count"
        # prompt that explicitly demands ≥ 5 questions.
        if len(all_questions) < 5 and total_pages >= 1:
            import asyncio as _asyncio
            print(f"[PDF] Only {len(all_questions)} total questions — retrying page 1 with forced-count prompt…")
            await _asyncio.sleep(3)
            try:
                import fitz as _fitz
                doc_retry = _fitz.open(stream=pdf_bytes, filetype="pdf")
                mat_retry = _fitz.Matrix(2.0, 2.0)   # higher zoom for better detail
                pix_retry = doc_retry[0].get_pixmap(matrix=mat_retry, alpha=False)
                jpeg_retry = pix_retry.tobytes("jpeg", jpg_quality=88)
                doc_retry.close()
                b64_retry = base64.b64encode(jpeg_retry).decode()
                forced_prompt = (
                    "IMPORTANT: This Cambridge IGCSE paper contains MULTIPLE questions. You MUST find "
                    "and mark AT LEAST 5 question parts. Scan every line carefully — look for question "
                    "numbers printed in the left margin (1, 2, 3 …) and their sub-parts in brackets "
                    "(a), (b)(i), etc. Each one with a mark allocation [n] is a separate question.\n\n"
                    + chunk_prompt(1, 1, total_pages, paper_type, True)
                )
                retry_result = await _mark_chunk_two_step(forced_prompt, b64_retry, "Page 1 RETRY", mime="image/jpeg")
                retry_qs = retry_result.get("questions", [])
                if len(retry_qs) > len(all_questions):
                    print(f"[PDF] Retry improved: {len(all_questions)} → {len(retry_qs)} questions")
                    all_questions = retry_qs
                    if retry_result.get("chunk_feedback"):
                        feedback_parts = [retry_result["chunk_feedback"]]
                    if retry_result.get("teacher_notes"):
                        teacher_parts = [retry_result["teacher_notes"]]
                    awarded   = sum(q.get("marks_awarded",  0) for q in all_questions)
                    available = sum(q.get("marks_available", 0) for q in all_questions)
            except Exception as retry_ex:
                print(f"[PDF] Forced-count retry failed: {retry_ex}")

        return finalise({
            "paper_type": paper_type,
            "total_marks_available": available,
            "total_marks_awarded":   awarded,
            "questions":             all_questions,
            "overall_feedback":      " ".join(feedback_parts),
            "teacher_notes":         " ".join(teacher_parts),
        })

    # ── Fallback: raw PDF chunks (if pymupdf not installed) ───────────────────
    if not PYPDF_OK:
        b64 = base64.b64encode(pdf_bytes).decode()
        raw = await call_ai(paper_prompt(paper_type_hint), b64, "application/pdf")
        return finalise(parse_json(raw))

    reader      = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    print(f"[PDF] Fallback: {total_pages} pages as PDF chunks…")

    all_questions = []
    paper_type    = paper_type_hint
    feedback_parts = []
    teacher_parts  = []

    for start in range(0, total_pages, 1):
        end      = start + 1
        is_first = (start == 0)
        writer   = PdfWriter()
        writer.add_page(reader.pages[start])
        buf = io.BytesIO()
        writer.write(buf)
        chunk_b64   = base64.b64encode(buf.getvalue()).decode()
        prompt      = chunk_prompt(start + 1, end, total_pages, paper_type, is_first)
        chunk_label = f"Chunk {start+1}"
        print(f"[PDF] Marking page {start+1}…")

        try:
            result   = await _mark_chunk_two_step(prompt, chunk_b64, chunk_label)
            chunk_qs = result.get("questions", [])
            for q in chunk_qs:
                avail   = max(0, int(q.get("marks_available", 0)))
                awarded = max(0, int(q.get("marks_awarded",  0)))
                q["marks_available"] = avail
                q["marks_awarded"]   = min(awarded, avail)
        except HTTPException as ex:
            raise HTTPException(ex.status_code, f"AI error on {chunk_label}: {ex.detail}")
        except Exception as ex:
            print(f"[PDF] {chunk_label} error: {ex}")
            raise HTTPException(500, f"Unexpected error on {chunk_label}: {str(ex)}")

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

    # ── Step 1: compact analysis (no JSON, no schema) ────────────────────────
    # Use the compact prompt instead of the full paper_prompt so Step 1 produces
    # ~300-500 chars per question rather than ~2500 chars of prose.
    # This cuts Step 1 output from ~10,000 chars (~25 s) to ~1,500 chars (~3-5 s).
    compact_parts = [
        {"text": COMPACT_ANALYSIS_PROMPT},
        {"inline_data": {"mime_type": mime, "data": file_b64}}
    ]
    gen_free = {
        "temperature": 0.1,
        "maxOutputTokens": 8192,   # compact output doesn't need 65536 tokens
        "topP": 0.9,
    }
    print(f"[MARK] Step 1: compact analysis…")
    analysis = ""
    try:
        analysis = await _google_request(compact_parts, gen_free, free_form=True)
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
    step1_q_nums = []
    if analysis:
        import re as _re
        # Parse the "Questions found:" line first (new format)
        q_list_match = _re.findall(r'Questions found:(.*?)(?:\n|$)', analysis)
        if q_list_match:
            step1_q_nums = [q.strip() for q in q_list_match[0].split(',') if q.strip()]
        else:
            # Fall back to Q-block headers
            step1_q_nums = _re.findall(r'---\s*Q\s*([\d()a-zA-Z]+)', analysis)

        expected_count = len(step1_q_nums) if step1_q_nums else "all"
        conversion_prompt = (
            "You just analysed a Cambridge IGCSE Mathematics 0580 answer paper and produced "
            "the following marking notes. Convert your ENTIRE analysis into the required JSON format.\n\n"
            f"CRITICAL: Your analysis identified {expected_count} questions: {step1_q_nums}. "
            "You MUST include every single one of them in the 'questions' array — "
            "do not skip, merge, or drop any question. Each sub-part (e.g. 1(a), 1(b)) is a SEPARATE entry.\n"
            f"The final 'questions' array MUST have exactly {expected_count} entries.\n\n"
            f"YOUR ANALYSIS:\n{analysis}\n\n"
            "Convert ALL questions above to JSON now."
        )
        gen_schema = {
            "temperature": 0.1,
            "maxOutputTokens": 65536,
            "topP": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": MARKING_SCHEMA,
        }
        print(f"[MARK] Step 2: converting analysis to JSON (expected {expected_count} questions)…")
        try:
            text_parts = [{"text": conversion_prompt}]
            json_text  = await _google_request(text_parts, gen_schema)
            parsed     = parse_json(json_text)
            qs         = parsed.get("questions", [])
            q_nums     = [q.get("question_number", "?") for q in qs]
            print(f"[MARK] Step 2: questions={len(qs)} — {q_nums}")
            # Accept if we got at least 5 questions OR at least half of what was detected
            expected_int = len(step1_q_nums) if step1_q_nums else 0
            if len(qs) >= 5 or (expected_int and len(qs) >= max(2, expected_int // 2)):
                return _clamp_marks(parsed)
        except Exception as ex:
            print(f"[MARK] Step 2 failed: {ex}")

    # ── Fallback: image + schema in one shot ─────────────────────────────────
    print(f"[MARK] Fallback: direct image+schema (higher temperature for coverage)…")
    gen_schema_img = {
        "temperature": 0.2,   # slightly higher temperature to avoid lazy truncation
        "maxOutputTokens": 65536,
        "topP": 0.95,
        "responseMimeType": "application/json",
        "responseSchema": MARKING_SCHEMA,
    }
    raw    = await _google_request(image_parts, gen_schema_img)
    parsed = parse_json(raw)
    qs     = parsed.get("questions", [])
    print(f"[MARK] Fallback: questions={len(qs)} — {[q.get('question_number','?') for q in qs]}")

    # Last resort: if still < 5 questions, try once more with explicit count instruction
    if len(qs) < 5:
        print(f"[MARK] Still only {len(qs)} questions — final attempt with forced count prompt…")
        try:
            forced_prompt = (
                "IMPORTANT: This Cambridge IGCSE paper has multiple questions. You MUST identify and mark "
                "AT LEAST 5 question parts. Look carefully for all printed question numbers and their "
                "sub-parts (e.g. 1(a), 1(b)(i), 2(a), etc.). Do not stop after the first 2-3 questions.\n\n"
                + prompt
            )
            forced_parts = [
                {"text": forced_prompt},
                {"inline_data": {"mime_type": mime, "data": file_b64}}
            ]
            gen_forced = {
                "temperature": 0.3, "maxOutputTokens": 65536, "topP": 0.95,
                "responseMimeType": "application/json", "responseSchema": MARKING_SCHEMA,
            }
            raw2    = await _google_request(forced_parts, gen_forced)
            parsed2 = parse_json(raw2)
            qs2     = parsed2.get("questions", [])
            print(f"[MARK] Final attempt: questions={len(qs2)}")
            if len(qs2) > len(qs):
                parsed = parsed2
        except Exception as ex:
            print(f"[MARK] Final attempt failed: {ex}")

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


@app.get("/api/debug/test-ai")
async def test_ai():
    """Test the AI connection with a simple text-only prompt. Use this to diagnose issues."""
    if not GOOGLE_API_KEY:
        return {"ok": False, "error": "GOOGLE_API_KEY is not set in backend/.env"}
   