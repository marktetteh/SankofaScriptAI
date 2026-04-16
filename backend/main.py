"""
GradeMate v3 — Cambridge IGCSE Mathematics 0580
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
GOOGLE_MODEL   = os.getenv("GOOGLE_MODEL",   "gemma-4-27b-it")
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

async def call_google(prompt: str, file_b64: str, mime_type: str) -> str:
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": mime_type, "data": file_b64}}
    ]
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192, "topP": 0.9}
    }
    url = f"{GOOGLE_API_URL}/{GOOGLE_MODEL}:generateContent?key={GOOGLE_API_KEY}"
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Google AI error: {resp.text[:400]}")
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            out_parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in out_parts)
    return ""

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

async def call_ai(prompt: str, file_b64: str, mime_type: str = "image/jpeg") -> str:
    if USE_GOOGLE:
        return await call_google(prompt, file_b64, mime_type)
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
- The QUESTIONS are TYPESET/PRINTED text — uniform, clean, machine-made font (black ink from a printer)
- The mark allocation for each question is shown in brackets at the end of the question, e.g. [2] or [3]
- The student's ANSWERS are HANDWRITTEN — you can tell because the strokes are irregular, human, and written in pen or pencil (any colour: black, blue, red, pencil — the key is it looks handwritten, not printed)
- Answers are written on dotted lines, in blank spaces beneath each question, or directly inside/beside diagrams
- Working/method steps may also be handwritten anywhere in the answer space
- For diagram questions (grids, graphs, geometric drawings), the student has drawn or labelled directly on the diagram in handwriting
- Even if the pen colour matches the printed text, you can distinguish handwriting from typeset text by the irregular letterforms and stroke variation

YOUR TASK — do all of the following:
1. Go through the paper page by page
2. For EACH question and sub-question (e.g. 1a, 1b(i), 1b(ii), 1c…), identify:
   - The printed question text
   - The mark allocation in brackets
   - What the student has written or drawn as their answer/working
3. Separate the printed question from the student's handwritten response
4. Mark each answer using Cambridge IGCSE conventions:
   - M mark = Method mark — award for a correct method even if the final answer is wrong
   - A mark = Accuracy mark — only award if the dependent M mark was also earned
   - B mark = Independent mark — award regardless of method shown
   - FT (Follow-through) — award if the answer follows correctly from a previous wrong answer
   - Do NOT penalise the same error twice across parts of a question
   - If the answer space is blank or shows no attempt, award 0
5. For diagram/graph questions, describe what the student drew and assess correctness

IMPORTANT: The total marks available is written on the cover page (e.g. "Total marks for this paper is 104"). Use this if visible.

Return ONLY valid JSON. No explanation before or after. Start with {{ and end with }}.

{{
  "paper_type": "Core or Extended",
  "paper_code": "<e.g. 0580/33 if visible on the paper>",
  "total_marks_available": <integer>,
  "total_marks_awarded": <integer>,
  "percentage": <number rounded to 1 decimal place>,
  "grade": "<A* | A | B | C | D | E | U>",
  "questions": [
    {{
      "question_number": "1(a)",
      "question_text": "<the full printed question text>",
      "student_answer": "<exactly what the student wrote or drew — 'No attempt' if blank>",
      "marks_available": <integer>,
      "marks_awarded": <integer>,
      "mark_breakdown": "<e.g. B1 awarded — correct answer 1302596>",
      "correct": <true if fully correct, false otherwise>,
      "feedback": "<1-2 sentences of specific, helpful feedback>"
    }}
  ],
  "overall_feedback": "<2-3 sentences of encouraging and specific feedback addressed directly to the student>",
  "teacher_notes": "<1-2 sentences for the teacher highlighting patterns, e.g. strong in algebra but losing marks on diagram questions>"
}}"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def chunk_prompt(page_start: int, page_end: int, total_pages: int,
                 paper_type_hint: str, is_first: bool) -> str:
    """Prompt for a single page-chunk of the paper."""
    if is_first:
        context = (
            f"These are pages {page_start}–{page_end} of {total_pages} of a Cambridge IGCSE "
            f"Mathematics 0580 answer paper. The cover page is included — read it for the "
            f"paper code, paper type (Core/Extended), and total marks for the whole paper."
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
- STUDENT ANSWERS are HANDWRITTEN — irregular human strokes (any pen colour or pencil)
- Mark allocations are in brackets, e.g. [2] or [3]
- Answers are on dotted lines, blank spaces, or drawn directly on diagrams

TASK: For every question and sub-question visible on these pages:
1. Read the printed question text and its mark allocation
2. Find the student's handwritten answer/working
3. Mark it using Cambridge conventions (M/A/B marks, follow-through, no double penalty)
4. If blank → 0 marks, note "No attempt"

Return ONLY valid JSON — start with {{ end with }}. No text before or after.

{{
  "paper_type": "Core or Extended",
  "paper_code": "<e.g. 0580/33 — only fill if visible on cover page, else omit>",
  "total_marks_available": <total marks for the whole paper from cover page, 0 if not visible>,
  "questions": [
    {{
      "question_number": "<e.g. 1(a) or 3(b)(ii)>",
      "question_text": "<printed question text>",
      "student_answer": "<handwritten answer/working, or 'No attempt'>",
      "marks_available": <integer>,
      "marks_awarded": <integer>,
      "mark_breakdown": "<e.g. M1 awarded – correct method; A0 – wrong answer>",
      "correct": <true or false>,
      "feedback": "<1–2 sentences specific feedback>"
    }}
  ],
  "chunk_feedback": "<brief observation about performance on these pages only>"
}}"""


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
            raw    = await call_ai(prompt, chunk_b64, "application/pdf")
            print(f"[PDF] Chunk {start+1}-{end} response preview: {raw[:200]}")
            result = parse_json(raw)
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
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find outermost { }
    s, e = text.find("{"), text.rfind("}") + 1
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e])
        except json.JSONDecodeError:
            pass
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
    print(f"  AssesslyAI — Cambridge IGCSE 0580")
    print(f"  Provider : {provider}")
    print(f"  API      : http://localhost:8000")
    print(f"{'='*55}\n")
    yield

app = FastAPI(title="GradeMate v3", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Endpoints ─────────────────────────────────────────────────────────────────

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

    print(f"\n[MARK] {student_name} | {class_name} | {assessment_type} #{exercise_number} | {file.filename} ({mime}) | {len(raw_bytes)//1024}KB")

    if mime == "application/pdf":
        result = await mark_pdf_chunked(raw_bytes, paper_type)
    else:
        raw    = await call_ai(prompt, file_b64, mime)
        print(f"[MARK] Raw response (first 300 chars):\n{raw[:300]}\n")
        parsed = parse_json(raw)
        # Recompute totals from individual question marks so the cover-page
        # paper total never inflates the denominator
        qs = parsed.get("questions", [])
        parsed["total_marks_awarded"]   = sum(q.get("marks_awarded",  0) for q in qs)
        parsed["total_marks_available"] = sum(q.get("marks_available", 0) for q in qs)
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
    fname = f"grademate_{class_name}_{datetime.now().strftime('%Y%m%d')}.csv"
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
    fname     = f"AssesslyAI_{safe_type}_{exercise_number}{cls_part}_{datetime.now().strftime('%Y%m%d')}.csv"
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
