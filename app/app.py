from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
import time, os, requests, datetime, re, math, threading, hashlib
from openai import OpenAI

from db import get_db, hash_password
from sqlalchemy.orm import joinedload
from models import (
    SystemConfig, ConnectionSetting, Prompt, SavedQuery, ReportPromptCase, PromptReport, User,
    LLMProvider, LLMModel, LLMFallback
)
from utils import (
    REDASH_URL, REDASH_API_KEY, REDASH_DATA_SOURCE_ID,
    DOWNLOADS_DIR, LLM_MODEL, LLM_TOKEN, get_openai_client,
    db_description, get_connection, get_schema,
    build_prompt, build_messages_from_prompt_key,
    validate_and_fix_sql, analyze_sql_error, explain_sql,
    clean_sql, fix_limit, validate_sql, run_sql_to_df, sql_to_one_line,
    render_markdown, save_excel, build_charts, build_chart_body_html,
    build_chart_body_only, save_chart_outputs, create_jpg_collage, check_sql,
    generate_report, log_llm_request, log_message, log_request_response,
    _add_formatted_text, modify_sql_for_business_terms,
    get_dated_filename, load_prompt_template_from_db,
    get_default_llm_config, get_default_api_key,
)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "text2bi-secret-key-change-me")
LLM_TIMEOUT = 120
USERS = {"alex": "secret", "max": "secret"}


# ── Context processor: user_theme + default_model ─────────────────
@app.context_processor
def inject_globals():
    theme = "dark"
    model_name = "по умолчанию"
    try:
        with get_db() as db:
            u = current_user()
            if u:
                theme = (u.theme or "dark") if getattr(u, 'theme', None) else "dark"
            default_model = db.query(LLMModel).filter(
                LLMModel.is_default == True,
                LLMModel.enabled == True
            ).first()
            if default_model:
                model_name = default_model.display_name or default_model.model_name
    except Exception:
        pass
    return {"user_theme": theme, "default_model_name": model_name}


# ── Auth helpers ─────────────────────────────────────────────────
def auth_user(username, password):
    with get_db() as db:
        u = db.query(User).filter(User.username == username).first()
        if u and u.password_hash == hash_password(password):
            return u
    return None


def current_user():
    uid = session.get("user_id")
    if uid:
        with get_db() as db:
            return db.query(User).filter(User.id == uid).first()
    return None


def user_id():
    u = current_user()
    return u.id if u else 0


def login_required():
    if not current_user():
        return redirect(url_for("login_page"))
    return None


def menu_items():
    items = [
        {"href": "/", "icon": "bi-search", "label": "Запрос"},
        {"href": "/chat", "icon": "bi-chat-dots", "label": "Чат-бот"},
        {"href": "/graf", "icon": "bi-bar-chart", "label": "График"},
        {"href": "/table", "icon": "bi-table", "label": "Таблица"},
    ]
    if current_user():
        items.extend([
            {"href": "/connections", "icon": "bi-plug", "label": "Подключения"},
            {"href": "/prompts", "icon": "bi-file-text", "label": "Промпты"},
            {"href": "/prompt_reports", "icon": "bi-file-earmark-text", "label": "Промпты-отчёты"},
            {"href": "/providers", "icon": "bi-cloud", "label": "Провайдеры"},
            {"href": "/models", "icon": "bi-cpu", "label": "Модели"},
            {"href": "/fallbacks", "icon": "bi-arrow-repeat", "label": "Фолбэки"},
        ])
    return items


def llm_complete(messages, timeout=LLM_TIMEOUT):
    cfg = get_default_llm_config()

    result, exc = [], []

    def worker():
        try:
            client = OpenAI(
                api_key=cfg.get('api_key') or "placeholder",
                base_url=cfg.get('base_url') or "https://bothub.chat/api/v2/openai/v1"
            )
            resp = client.chat.completions.create(
                model=cfg.get('model_name') or LLM_MODEL,
                messages=messages,
                temperature=cfg.get('temperature') or 0,
                timeout=timeout
            )
            result.append(resp)
        except Exception as e:
            exc.append(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 10)
    if t.is_alive():
        raise TimeoutError(f"LLM timeout {timeout}s")
    if exc:
        raise exc[0]
    return result[0]
# ── Serialize ────────────────────────────────────────────────────
def _sv(val):
    if val is None: return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)): return None
    if isinstance(val, (datetime.datetime, datetime.date)): return val.isoformat()
    return val if isinstance(val, (int, float)) else str(val)


# ═══════════════════════════════════════════════════════════════════
#  LOGIN / LOGOUT
# ═══════════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        p = request.form.get("password", "").strip()
        user = auth_user(u, p)
        if user:
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))
        return render_template("login.html", error="Неверный логин или пароль")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ═══════════════════════════════════════════════════════════════════
#  INDEX — main SQL generation
# ═══════════════════════════════════════════════════════════════════
@app.route("/", methods=["GET", "POST"])
def index():
    r = login_required()
    if r:
        return r

    result = None
    sql_query = ""
    elapsed_time = None
    model_time = None
    sql_time = None
    question = ""
    analysis = None
    error_msg = None

    if request.method == "POST":
        question = request.form.get("question", "").strip()
        sql_query = request.form.get("sql_query", "").strip()
        start_time = time.time()
        try:
            schema_text = get_schema()
            if not sql_query:
                t0 = time.time()
                prompt_data = build_prompt(question, schema_text, db_description)
                llm_messages = [{"role": "system", "content": prompt_data["system_role"]},
                                {"role": "user", "content": prompt_data["user_content"]}]
                log_llm_request(llm_messages)
                resp = llm_complete(llm_messages)
                sql_query = clean_sql(resp.choices[0].message.content)
                if check_sql:
                    sql_query = validate_and_fix_sql(question, sql_query, schema_text, db_description)
                model_time = int(time.time() - t0)

            if sql_query:
                t1 = time.time()
                sql_query = fix_limit(clean_sql(sql_query))
                validate_sql(sql_query)
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(sql_query)
                colnames = [d[0] for d in cur.description]
                rows = cur.fetchall()
                cur.close()
                conn.close()
                result = {"columns": colnames, "rows": rows}
                sql_time = int(time.time() - t1)
            elapsed_time = int(time.time() - start_time)
        except Exception as e:
            error_msg = str(e)
            try:
                analysis = analyze_sql_error(sql_query, error_msg, get_schema(), db_description)
            except Exception:
                pass
            result = {"error": error_msg}

    u = current_user()
    return render_template("index.html",
                           result=result, sql_query=sql_query, elapsed_time=elapsed_time,
                           model_time=model_time, sql_time=sql_time,
                           question=question, analysis=analysis, error=error_msg,
                           llm_model=LLM_MODEL, menu=menu_items(), user=u)


# ═══════════════════════════════════════════════════════════════════
#  CHAT
# ═══════════════════════════════════════════════════════════════════
@app.route("/chat")
def chat_page():
    r = login_required()
    if r:
        return r
    sql_query = request.args.get("sql_query", "").strip()
    return render_template("chat.html", sql_query=sql_query, llm_model=LLM_MODEL, menu=menu_items(), user=current_user())


@app.route("/chat_ask", methods=["POST"])
def chat_ask():
    try:
        data = request.get_json()
        question = data.get("question", "").strip()
        sql_query = data.get("sql_query", "").strip()
        sql_data = data.get("sql_data", "")
        history = data.get("history", [])

        if not question:
            return jsonify({"status": "error", "message": "Пустой вопрос"})

        schema_text = get_schema()
        msgs = build_messages_from_prompt_key("prompt_chat_ask", {
            "SCHEMA_TEXT": schema_text, "DB_DESC": db_description,
            "SQL_QUERY": sql_query, "SQL_DATA": sql_data, "QUESTION": question
        })
        insert_pos = len(msgs) - 1
        for i, h in enumerate(history):
            msgs.insert(insert_pos + i, {"role": h["role"], "content": h["content"]})

        log_llm_request(msgs)
        resp = llm_complete(msgs)
        answer = resp.choices[0].message.content.strip()
        return jsonify({"status": "ok", "answer": render_markdown(answer), "answer_raw": answer})
    except TimeoutError:
        return jsonify({"status": "error", "message": "LLM timeout"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  SAVE CHAT OUTPUT (Word / Text / Excel)
# ═══════════════════════════════════════════════════════════════════
@app.route("/save_chat_output", methods=["POST"])
def save_chat_output():
    try:
        data = request.get_json()
        content = data.get("content", "").strip()
        fmt = data.get("format", "docx").strip()
        if not content:
            return jsonify({"status": "error", "message": "Пустое содержимое"})

        date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        if fmt == "docx":
            from docx import Document
            doc = Document()
            from docx.shared import Pt
            style = doc.styles['Normal']
            style.font.size = Pt(11)
            doc.add_paragraph(content)
            filename = f"chat_output_{date_str}.docx"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            doc.save(filepath)
        elif fmt == "xlsx":
            import pandas as pd
            lines = [line for line in content.split('\n') if line.strip()]
            df = pd.DataFrame({"Ответ": lines})
            filename = f"chat_output_{date_str}.xlsx"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            df.to_excel(filepath, index=False)
        else:
            filename = f"chat_output_{date_str}.txt"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

        return jsonify({"status": "ok", "filename": filename,
                        "url": url_for('download_file', filename=filename)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  EXPLAIN SQL
# ═══════════════════════════════════════════════════════════════════
@app.route("/explain_sql", methods=["POST"])
def explain_sql_route():
    try:
        data = request.get_json()
        sql_query = clean_sql(data.get("sql_query", ""))
        if not sql_query:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        raw, html = explain_sql(sql_query, get_schema(), db_description)
        return jsonify({"status": "ok", "explanation": html, "explanation_raw": raw})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  BUSINESS TERMS
# ═══════════════════════════════════════════════════════════════════
@app.route("/modify_sql_business_terms", methods=["POST"])
def modify_sql_business_terms_route():
    try:
        data = request.get_json()
        sql = clean_sql(data.get("sql_query", ""))
        if not sql:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        modified = modify_sql_for_business_terms(sql, get_schema(), db_description)
        modified = fix_limit(clean_sql(modified))
        validate_sql(modified)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(modified)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "sql_query": modified, "result": {"columns": cols, "rows": rows}})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  SAVE PROMPT (qa pairs)
# ═══════════════════════════════════════════════════════════════════
@app.route("/save_prompt", methods=["POST"])
def save_prompt_route():
    data = request.get_json()
    q = data.get("question", "").strip()
    s = data.get("sql_query", "").strip()
    if not q or not s:
        return jsonify({"status": "error"}), 400
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  GENERATE REPORT (Word .docx)
# ═══════════════════════════════════════════════════════════════════
@app.route("/generate_report", methods=["POST"])
def generate_report_route():
    try:
        data = request.get_json()
        sql = clean_sql(data.get("sql_query", ""))
        question = data.get("question", "").strip()
        charts = data.get("include_charts", False)
        prompt_id = data.get("prompt_id")
        if not sql:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        sql = fix_limit(sql)
        validate_sql(sql)
        df = run_sql_to_df(sql)
        chart_paths = []
        if charts:
            for i, item in enumerate(build_charts(df)):
                cp = os.path.join(DOWNLOADS_DIR, f"_tmp_chart_{i}.png")
                item["fig"].write_image(cp, format="png", width=1400, height=900, scale=2)
                chart_paths.append(cp)
        fname = get_dated_filename("report", "docx", question or "report")
        path = os.path.join(DOWNLOADS_DIR, fname)
        generate_report(get_schema(), db_description, sql, df, path, chart_paths=chart_paths, prompt_id=prompt_id)
        for cp in chart_paths:
            try: os.remove(cp)
            except: pass
        return jsonify({"status": "ok", "message": "Готово", "filename": fname})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  GENERATE CHART
# ═══════════════════════════════════════════════════════════════════
@app.route("/generate_chart", methods=["POST"])
def generate_chart_route():
    try:
        data = request.get_json()
        sql = fix_limit(clean_sql(data.get("sql_query", "")))
        validate_sql(sql)
        df = run_sql_to_df(sql)
        figs = build_charts(df)
        if not figs:
            return jsonify({"status": "error", "message": "Нет данных"})
        r = build_chart_body_only(figs)
        return jsonify({"status": "ok", "chart_html": r["full_html"], "chart_body": r["chart_body"]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  GRAF PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/graf")
def graf_page():
    r = login_required()
    if r: return r
    sql = request.args.get("sql_query", "").strip()
    table_mode = request.args.get("table_mode", "") == "1"
    columns, rows = [], []
    if sql and not table_mode:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(fix_limit(clean_sql(sql)))
            columns = [d[0] for d in cur.description]
            rows = [[_sv(v) for v in r] for r in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception:
            pass
    return render_template("graf.html", sql_query=sql, columns=columns, rows=rows,
                           table_mode=table_mode, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  TABLE PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/table")
def table_page():
    r = login_required()
    if r: return r
    return render_template("table.html", columns=[], rows=[], menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  TABLE CHAT
# ═══════════════════════════════════════════════════════════════════
@app.route("/table_chat_ask", methods=["POST"])
def table_chat_ask():
    try:
        data = request.get_json()
        question = data.get("question", "").strip()
        cols = data.get("table_columns", [])
        rows = data.get("table_rows", [])
        history = data.get("history", [])
        if not question:
            return jsonify({"status": "error", "message": "Пустой вопрос"})
        tbl = "\n".join([" | ".join(str(c) for c in cols)] +
                        [" | ".join(str(v) for v in r) for r in rows[:50]])
        msgs = build_messages_from_prompt_key("prompt_chat_ask_all", {
            "SCHEMA_TEXT": get_schema(), "DB_DESC": db_description,
            "QUESTION": question, "TABLES": tbl
        })
        log_llm_request(msgs)
        resp = llm_complete(msgs)
        answer = resp.choices[0].message.content.strip()
        return jsonify({"status": "ok", "answer": render_markdown(answer), "answer_raw": answer})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  SEND TO REDASH
# ═══════════════════════════════════════════════════════════════════
@app.route("/send_to_redash", methods=["POST"])
def send_to_redash():
    try:
        data = request.get_json()
        q = data.get("question", "").strip()
        s = sql_to_one_line(data.get("sql_query", ""))
        resp = requests.post(REDASH_URL,
                             headers={"Authorization": f"Key {REDASH_API_KEY}", "Content-Type": "application/json"},
                             json={"name": q, "query": s, "data_source_id": REDASH_DATA_SOURCE_ID}, timeout=30)
        return jsonify({"status": "ok" if resp.status_code in [200, 201] else "error", "message": resp.text})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  EXPORT CHART
# ═══════════════════════════════════════════════════════════════════
@app.route("/export_chart/<fmt>")
def export_chart(fmt):
    try:
        sql = fix_limit(clean_sql(request.args.get("sql_query", "")))
        validate_sql(sql)
        df = run_sql_to_df(sql)
        figs = build_charts(df)
        if not figs:
            return "No data", 400
        if fmt == "html":
            fpath = os.path.join(DOWNLOADS_DIR, "chart.html")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Chart</title></head>"
                        f"<body style='background:#1e1e1e;color:#fff'>{build_chart_body_html(figs)}</body></html>")
        elif fmt == "jpg":
            paths = []
            for i, it in enumerate(figs):
                p = os.path.join(DOWNLOADS_DIR, f"c{i}.jpg")
                it["fig"].write_image(p, format="jpg", width=1400, height=900, scale=2)
                paths.append(p)
            fpath = os.path.join(DOWNLOADS_DIR, "chart.jpg")
            create_jpg_collage(paths, fpath)
        else:
            return "Bad format", 400
        return send_file(fpath, as_attachment=False)
    except Exception as e:
        return str(e), 500


# ═══════════════════════════════════════════════════════════════════
#  DOWNLOADS
# ═══════════════════════════════════════════════════════════════════
@app.route("/download_file/<path:filename>")
def download_file(filename):
    filepath = os.path.abspath(os.path.join(DOWNLOADS_DIR, os.path.basename(filename)))
    if not filepath.startswith(os.path.abspath(DOWNLOADS_DIR)):
        return "Forbidden", 403
    if not os.path.exists(filepath):
        return "File not found", 404
    return send_file(filepath, as_attachment=True)


@app.route("/download_chart/<file_type>")
def download_chart_file(file_type):
    ext_map = {"html": ("chart.html", "text/html"), "jpg": ("chart.jpg", "image/jpeg")}
    if file_type not in ext_map:
        return "Bad type", 400
    fn, mt = ext_map[file_type]
    p = os.path.join(DOWNLOADS_DIR, fn)
    return send_file(p, mimetype=mt, as_attachment=True)


@app.route("/download_chat/docx", methods=["POST"])
def download_chat_docx():
    try:
        data = request.get_json()
        content = data.get("content", "")
        tcols = data.get("table_columns")
        trows = data.get("table_rows")
        from docx import Document
        from docx.shared import Pt, RGBColor
        doc = Document()
        doc.add_heading("Чат — ответ ассистента", level=1)
        if tcols and trows:
            doc.add_heading("Результат SQL", level=2)
            tbl = doc.add_table(rows=1, cols=len(tcols))
            tbl.style = "Light Grid Accent 1"
            for i, c in enumerate(tcols):
                tbl.rows[0].cells[i].text = str(c)
            for row in trows[:100]:
                rc = tbl.add_row().cells
                for i, v in enumerate(row):
                    if i < len(rc): rc[i].text = str(v) if v else "NULL"
        for line in content.split("\n"):
            ls = line.strip()
            if not ls: continue
            p = doc.add_paragraph(style="List Bullet" if ls.startswith("- ") else None)
            _add_formatted_text(p, ls[2:] if ls.startswith("- ") else ls)
        fn = get_dated_filename("chat", "docx", "chat")
        path = os.path.join(DOWNLOADS_DIR, fn)
        doc.save(path)
        return send_file(path, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         as_attachment=True, download_name=fn)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
#  EXECUTE SQL
# ═══════════════════════════════════════════════════════════════════
@app.route("/execute_sql_ajax", methods=["POST"])
def execute_sql_ajax():
    try:
        data = request.get_json()
        sql = fix_limit(clean_sql(data.get("sql_query", "")))
        if not sql:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        validate_sql(sql)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [[_sv(v) for v in r] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "columns": cols, "rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/execute_sql_tab")
def execute_sql_tab():
    sql = request.args.get("sql_query", "").strip()
    result, analysis, error = None, None, None
    if sql:
        try:
            s = fix_limit(clean_sql(sql))
            validate_sql(s)
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(s)
            result = {"columns": [d[0] for d in cur.description], "rows": cur.fetchall()}
            cur.close()
            conn.close()
        except Exception as e:
            error = str(e)
            try:
                analysis = analyze_sql_error(sql, error, get_schema(), db_description)
            except: pass
    return render_template("execute_sql.html", sql_query=sql, result=result, analysis=analysis,
                           error=error, llm_model=LLM_MODEL, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  SPEECH
# ═══════════════════════════════════════════════════════════════════
@app.route("/speech", methods=["POST"])
def speech_route():
    data = request.get_json()
    text = data.get("text", "")
    print(f"[VOICE] {text}", flush=True)
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  COLORS THEME PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/colors")
def colors_page():
    r = login_required()
    if r: return r
    themes = [
        {"id": "default", "name": "По умолчанию"},
        {"id": "dark", "name": "Тёмная"},
        {"id": "light", "name": "Светлая"},
        {"id": "nord", "name": "Nord"},
        {"id": "solarized", "name": "Solarized"},
    ]
    return render_template("colors.html", themes=themes, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  CONNECTIONS PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/connections")
def connections_page():
    r = login_required()
    if r: return r
    with get_db() as db:
        conns = db.query(ConnectionSetting).all()
    return render_template("connections_list.html", connections=conns, menu=menu_items(), user=current_user())


@app.route("/connections/edit/<int:cid>", methods=["GET", "POST"])
def connections_edit(cid):
    r = login_required()
    if r: return r
    with get_db() as db:
        c = db.query(ConnectionSetting).filter(ConnectionSetting.id == cid).first()
        if not c:
            flash('Подключение не найдено', 'danger')
            return redirect(url_for('connections_page'))
        if request.method == "POST":
            c.host = request.form['host']
            c.port = int(request.form['port']) if request.form.get('port') else 5432
            c.database_name = request.form['database_name']
            c.username = request.form['username']
            new_password = request.form.get('password', '')
            if new_password:
                c.password = new_password
            c.is_default = request.form.get('is_default') == 'on'
            if c.is_default:
                for other in db.query(ConnectionSetting).filter(ConnectionSetting.id != c.id).all():
                    other.is_default = False
            db.commit()
            flash('Подключение обновлено', 'success')
            return redirect(url_for('connections_page'))
    return render_template("connections_form.html", connection=c, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  PROMPTS PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/prompts")
def prompts_page():
    r = login_required()
    if r: return r
    with get_db() as db:
        prompts = db.query(Prompt).all()
    return render_template("prompts_list.html", prompts=prompts, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  LLM PROVIDERS PAGES
# ═══════════════════════════════════════════════════════════════════
@app.route("/providers")
def providers_list():
    r = login_required()
    if r: return r
    with get_db() as db:
        providers = db.query(LLMProvider).order_by(LLMProvider.provider_number).all()
    return render_template("providers_list.html", providers=providers, menu=menu_items(), user=current_user())


@app.route("/providers/create", methods=["GET", "POST"])
def providers_create():
    r = login_required()
    if r: return r
    if request.method == "POST":
        with get_db() as db:
            max_n = db.query(LLMProvider.provider_number).order_by(LLMProvider.provider_number.desc()).first()
            next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
            p = LLMProvider(
                provider_number=next_n,
                name=request.form['name'],
                provider_type=request.form['provider_type'],
                base_url=request.form['base_url'],
                api_key=request.form.get('api_key'),
                enabled=request.form.get('enabled') == 'on'
            )
            db.add(p)
            db.commit()
        flash('Провайдер создан', 'success')
        return redirect(url_for('providers_list'))
    return render_template("providers_form.html", provider=None, menu=menu_items(), user=current_user())


@app.route("/providers/edit/<pid>", methods=["GET", "POST"])
def providers_edit(pid):
    r = login_required()
    if r: return r
    with get_db() as db:
        p = db.query(LLMProvider).filter(LLMProvider.id == pid).first()
        if not p:
            flash('Провайдер не найден', 'danger')
            return redirect(url_for('providers_list'))
        if request.method == "POST":
            p.name = request.form['name']
            p.provider_type = request.form['provider_type']
            p.base_url = request.form['base_url']
            new_key = request.form.get('api_key', '')
            if new_key:
                p.api_key = new_key
            p.enabled = request.form.get('enabled') == 'on'
            db.commit()
            flash('Провайдер обновлён', 'success')
            return redirect(url_for('providers_list'))
    return render_template("providers_form.html", provider=p, menu=menu_items(), user=current_user())


@app.route("/providers/delete/<pid>", methods=["POST"])
def providers_delete(pid):
    r = login_required()
    if r: return r
    with get_db() as db:
        p = db.query(LLMProvider).filter(LLMProvider.id == pid).first()
        if p:
            db.delete(p)
            db.commit()
            flash('Провайдер удалён', 'success')
        else:
            flash('Провайдер не найден', 'danger')
    return redirect(url_for('providers_list'))


# ═══════════════════════════════════════════════════════════════════
#  LLM MODELS PAGES
# ═══════════════════════════════════════════════════════════════════
@app.route("/models")
def models_list():
    r = login_required()
    if r: return r
    with get_db() as db:
        models = db.query(LLMModel).options(joinedload(LLMModel.provider)).order_by(LLMModel.model_number).all()
    return render_template("models_list.html", models=models, menu=menu_items(), user=current_user())


@app.route("/models/create", methods=["GET", "POST"])
def models_create():
    r = login_required()
    if r: return r
    with get_db() as db:
        providers = db.query(LLMProvider).order_by(LLMProvider.name).all()
    if request.method == "POST":
        with get_db() as db:
            max_n = db.query(LLMModel.model_number).order_by(LLMModel.model_number.desc()).first()
            next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
            m = LLMModel(
                model_number=next_n,
                provider_id=request.form['provider_id'],
                model_name=request.form['model_name'],
                display_name=request.form.get('display_name'),
                context_size=int(request.form['context_size']) if request.form.get('context_size') else None,
                max_tokens=int(request.form['max_tokens']) if request.form.get('max_tokens') else None,
                temperature=float(request.form['temperature']) if request.form.get('temperature') else None,
                enabled=request.form.get('enabled') == 'on',
                is_default=request.form.get('is_default') == 'on',
                timeout=int(request.form['timeout']) if request.form.get('timeout') else 180,
            )
            db.add(m)
            if m.is_default:
                for other in db.query(LLMModel).filter(LLMModel.id != m.id).all():
                    other.is_default = False
            db.commit()
        flash('Модель создана', 'success')
        return redirect(url_for('models_list'))
    return render_template("models_form.html", model=None, providers=providers, menu=menu_items(), user=current_user())


@app.route("/models/edit/<mid>", methods=["GET", "POST"])
def models_edit(mid):
    r = login_required()
    if r: return r
    with get_db() as db:
        m = db.query(LLMModel).filter(LLMModel.id == mid).first()
        if not m:
            flash('Модель не найдена', 'danger')
            return redirect(url_for('models_list'))
        providers = db.query(LLMProvider).order_by(LLMProvider.name).all()
        if request.method == "POST":
            m.provider_id = request.form['provider_id']
            m.model_name = request.form['model_name']
            m.display_name = request.form.get('display_name')
            m.context_size = int(request.form['context_size']) if request.form.get('context_size') else None
            m.max_tokens = int(request.form['max_tokens']) if request.form.get('max_tokens') else None
            m.temperature = float(request.form['temperature']) if request.form.get('temperature') else None
            m.enabled = request.form.get('enabled') == 'on'
            m.is_default = request.form.get('is_default') == 'on'
            if m.is_default:
                for other in db.query(LLMModel).filter(LLMModel.id != m.id).all():
                    other.is_default = False
            m.timeout = int(request.form['timeout']) if request.form.get('timeout') else 180
            db.commit()
            flash('Модель обновлена', 'success')
            return redirect(url_for('models_list'))
    return render_template("models_form.html", model=m, providers=providers, menu=menu_items(), user=current_user())


@app.route("/models/delete/<mid>", methods=["POST"])
def models_delete(mid):
    r = login_required()
    if r: return r
    with get_db() as db:
        m = db.query(LLMModel).filter(LLMModel.id == mid).first()
        if m:
            db.delete(m)
            db.commit()
            flash('Модель удалена', 'success')
        else:
            flash('Модель не найдена', 'danger')
    return redirect(url_for('models_list'))


# ═══════════════════════════════════════════════════════════════════
#  LLM FALLBACKS PAGES
# ═══════════════════════════════════════════════════════════════════
@app.route("/fallbacks")
def fallbacks_list():
    r = login_required()
    if r: return r
    with get_db() as db:
        relations = db.query(LLMFallback).order_by(LLMFallback.relation_number).all()
        models = db.query(LLMModel).options(joinedload(LLMModel.provider)).order_by(LLMModel.model_number).all()
        model_map = {str(m.id): m for m in models}
    return render_template("fallbacks_list.html", relations=relations, model_map=model_map, menu=menu_items(), user=current_user())


@app.route("/fallbacks/create", methods=["GET", "POST"])
def fallbacks_create():
    r = login_required()
    if r: return r
    with get_db() as db:
        models = db.query(LLMModel).options(joinedload(LLMModel.provider)).order_by(LLMModel.display_name).all()
    if request.method == "POST":
        with get_db() as db:
            max_n = db.query(LLMFallback.relation_number).order_by(LLMFallback.relation_number.desc()).first()
            next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
            f = LLMFallback(
                relation_number=next_n,
                model_id=request.form['model_id'],
                fallback_model_id=request.form['fallback_model_id'],
                priority=int(request.form.get('priority', 1))
            )
            db.add(f)
            try:
                db.commit()
                flash('Фолбэк создан', 'success')
            except Exception:
                db.rollback()
                flash('Такой фолбэк уже существует', 'danger')
        return redirect(url_for('fallbacks_list'))
    return render_template("fallbacks_form.html", relation=None, models=models, menu=menu_items(), user=current_user())


@app.route("/fallbacks/delete/<fid>", methods=["POST"])
def fallbacks_delete(fid):
    r = login_required()
    if r: return r
    with get_db() as db:
        f = db.query(LLMFallback).filter(LLMFallback.id == fid).first()
        if f:
            db.delete(f)
            db.commit()
            flash('Фолбэк удалён', 'success')
        else:
            flash('Фолбэк не найден', 'danger')
    return redirect(url_for('fallbacks_list'))


# ═══════════════════════════════════════════════════════════════════
#  API: Connections CRUD
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/connections", methods=["GET", "POST"])
@app.route("/api/connections/<int:cid>", methods=["GET", "PUT", "DELETE"])
def api_connections(cid=None):
    with get_db() as db:
        if request.method == "GET" and cid is None:
            return jsonify([{"id": c.id, "host": c.host, "port": c.port,
                             "database_name": c.database_name, "username": c.username,
                             "is_default": c.is_default} for c in db.query(ConnectionSetting).all()])
        if request.method == "GET" and cid is not None:
            c = db.query(ConnectionSetting).filter(ConnectionSetting.id == cid).first()
            return jsonify({"id": c.id, "host": c.host, "port": c.port,
                            "database_name": c.database_name, "username": c.username,
                            "password": c.password, "is_default": c.is_default}) if c else ("", 404)
        if request.method == "POST":
            data = request.get_json()
            c = ConnectionSetting(host=data["host"], port=data.get("port", 5432),
                                  database_name=data["database_name"], username=data["username"],
                                  password=data["password"], is_default=data.get("is_default", False))
            db.add(c)
            db.flush()
            if c.is_default:
                for other in db.query(ConnectionSetting).filter(ConnectionSetting.id != c.id).all():
                    other.is_default = False
            db.commit()
            return jsonify({"status": "ok", "id": c.id})
        if request.method == "PUT":
            c = db.query(ConnectionSetting).filter(ConnectionSetting.id == cid).first()
            if not c: return "", 404
            data = request.get_json()
            for f in ["host", "port", "database_name", "username", "password", "is_default"]:
                if f in data: setattr(c, f, data[f])
            if c.is_default:
                for other in db.query(ConnectionSetting).filter(ConnectionSetting.id != c.id).all():
                    other.is_default = False
            db.commit()
            return jsonify({"status": "ok"})
        if request.method == "DELETE":
            db.query(ConnectionSetting).filter(ConnectionSetting.id == cid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: Prompts CRUD
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/prompts", methods=["GET", "POST"])
@app.route("/api/prompts/<int:pid>", methods=["GET", "PUT", "DELETE"])
def api_prompts(pid=None):
    with get_db() as db:
        if request.method == "GET" and pid is None:
            return jsonify([{"id": p.id, "name": p.name, "prompt_key": p.prompt_key,
                             "category": p.category, "content": p.content}
                            for p in db.query(Prompt).all()])
        if request.method == "GET" and pid is not None:
            p = db.query(Prompt).filter(Prompt.id == pid).first()
            return jsonify({"id": p.id, "name": p.name, "prompt_key": p.prompt_key,
                            "category": p.category, "content": p.content}) if p else ("", 404)
        if request.method == "POST":
            data = request.get_json()
            p = Prompt(name=data["name"], prompt_key=data["prompt_key"],
                       category=data.get("category", "general"), content=data["content"])
            db.add(p)
            db.commit()
            return jsonify({"status": "ok", "id": p.id})
        if request.method == "PUT":
            p = db.query(Prompt).filter(Prompt.id == pid).first()
            if not p: return "", 404
            data = request.get_json()
            for f in ["name", "prompt_key", "category", "content"]:
                if f in data: setattr(p, f, data[f])
            db.commit()
            return jsonify({"status": "ok"})
        if request.method == "DELETE":
            db.query(Prompt).filter(Prompt.id == pid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  PROMPT REPORT — page listing
# ═══════════════════════════════════════════════════════════════════
@app.route("/prompt_reports")
def prompt_reports_page():
    r = login_required()
    if r: return r
    uid = user_id()
    with get_db() as db:
        reports = db.query(PromptReport).filter(PromptReport.user_id == uid).all()
    return render_template("prompt_reports_list.html", reports=reports, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  API: PromptReport CRUD
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/prompt_reports", methods=["GET", "POST"])
@app.route("/api/prompt_reports/<int:rid>", methods=["GET", "PUT", "DELETE"])
def api_prompt_reports(rid=None):
    uid = user_id()
    with get_db() as db:
        if request.method == "GET" and rid is None:
            return jsonify([{"id": r.id, "name": r.name,
                             "content": r.content, "is_active": r.is_active,
                             "is_default": r.is_default}
                            for r in db.query(PromptReport).filter(PromptReport.user_id == uid).all()])
        if request.method == "GET" and rid is not None:
            r = db.query(PromptReport).filter(PromptReport.id == rid, PromptReport.user_id == uid).first()
            return jsonify({"id": r.id, "name": r.name,
                            "content": r.content, "is_active": r.is_active,
                            "is_default": r.is_default}) if r else ("", 404)
        if request.method == "POST":
            data = request.get_json()
            is_default = data.get("is_default", False)
            if is_default:
                db.query(PromptReport).filter(PromptReport.user_id == uid).update({PromptReport.is_default: False})
                db.flush()
            r = PromptReport(user_id=uid, name=data["name"],
                            content=data.get("content", ""),
                            is_active=data.get("is_active", True),
                            is_default=is_default)
            db.add(r)
            db.commit()
            return jsonify({"status": "ok", "id": r.id})
        if request.method == "PUT":
            r = db.query(PromptReport).filter(PromptReport.id == rid, PromptReport.user_id == uid).first()
            if not r: return "", 404
            data = request.get_json()
            for f in ["name", "content", "is_active", "is_default"]:
                if f in data: setattr(r, f, data[f])
            if data.get("is_default"):
                db.query(PromptReport).filter(PromptReport.id != rid, PromptReport.user_id == uid).update({PromptReport.is_default: False})
            db.commit()
            return jsonify({"status": "ok"})
        if request.method == "DELETE":
            db.query(PromptReport).filter(PromptReport.id == rid, PromptReport.user_id == uid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: SavedQueries CRUD
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/saved_queries", methods=["GET", "POST"])
@app.route("/api/saved_queries/<int:qid>", methods=["GET", "PUT", "DELETE"])
def api_saved_queries(qid=None):
    uid = user_id()
    with get_db() as db:
        if request.method == "GET" and qid is None:
            qs = db.query(SavedQuery).filter(SavedQuery.user_id == uid).all()
            return jsonify([{"id": q.id, "title": q.title, "query_text": q.query_text} for q in qs])
        if request.method == "GET" and qid is not None:
            q = db.query(SavedQuery).filter(SavedQuery.id == qid, SavedQuery.user_id == uid).first()
            return jsonify({"id": q.id, "title": q.title, "query_text": q.query_text}) if q else ("", 404)
        if request.method == "POST":
            data = request.get_json()
            q = SavedQuery(user_id=uid, title=data["title"], query_text=data["query_text"])
            db.add(q)
            db.commit()
            return jsonify({"status": "ok", "id": q.id})
        if request.method == "DELETE":
            db.query(SavedQuery).filter(SavedQuery.id == qid, SavedQuery.user_id == uid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: Report Prompt Cases
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/report_cases", methods=["GET", "POST"])
@app.route("/api/report_cases/<int:rcid>", methods=["GET", "PUT", "DELETE"])
def api_report_cases(rcid=None):
    uid = user_id()
    with get_db() as db:
        if request.method == "GET" and rcid is None:
            cs = db.query(ReportPromptCase).filter(ReportPromptCase.user_id == uid).all()
            return jsonify([{"id": c.id, "case_name": c.case_name, "description": c.description,
                             "prompt_id": c.prompt_id, "is_default": c.is_default} for c in cs])
        if request.method == "POST":
            data = request.get_json()
            c = ReportPromptCase(user_id=uid, case_name=data["case_name"],
                                 description=data.get("description", ""),
                                 prompt_id=data.get("prompt_id"),
                                 connection_id=data.get("connection_id"),
                                 is_default=data.get("is_default", False))
            db.add(c)
            db.commit()
            return jsonify({"status": "ok", "id": c.id})
        if request.method == "DELETE":
            db.query(ReportPromptCase).filter(ReportPromptCase.id == rcid, ReportPromptCase.user_id == uid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: LLM Providers
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/llm_providers", methods=["GET", "POST"])
def api_llm_providers():
    with get_db() as db:
        if request.method == "GET":
            return jsonify([{
                "id": str(p.id), "provider_number": p.provider_number,
                "name": p.name, "provider_type": p.provider_type,
                "base_url": p.base_url, "enabled": p.enabled,
                "created_at": p.created_at.isoformat() if p.created_at else None
            } for p in db.query(LLMProvider).order_by(LLMProvider.provider_number).all()])
        data = request.get_json()
        max_n = db.query(LLMProvider.provider_number).order_by(LLMProvider.provider_number.desc()).first()
        next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
        p = LLMProvider(
            provider_number=next_n,
            name=data["name"],
            provider_type=data["provider_type"],
            base_url=data["base_url"],
            api_key=data.get("api_key"),
            enabled=data.get("enabled", True)
        )
        db.add(p)
        db.commit()
    return jsonify({"status": "ok", "id": str(p.id)})


@app.route("/api/llm_providers/<pid>", methods=["GET", "PUT", "DELETE"])
def api_llm_provider(pid):
    with get_db() as db:
        p = db.query(LLMProvider).filter(LLMProvider.id == pid).first()
        if not p:
            return jsonify({"status": "error", "message": "Not found"}), 404
        if request.method == "GET":
            return jsonify({
                "id": str(p.id), "provider_number": p.provider_number,
                "name": p.name, "provider_type": p.provider_type,
                "base_url": p.base_url, "api_key": p.api_key or "",
                "enabled": p.enabled
            })
        if request.method == "PUT":
            data = request.get_json()
            for f in ["name", "provider_type", "base_url", "enabled"]:
                if f in data: setattr(p, f, data[f])
            if "api_key" in data and data["api_key"]:
                p.api_key = data["api_key"]
            db.commit()
            return jsonify({"status": "ok"})
        if request.method == "DELETE":
            db.delete(p)
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: LLM Models
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/llm_models", methods=["GET", "POST"])
def api_llm_models():
    with get_db() as db:
        if request.method == "GET":
            result = []
            for m in db.query(LLMModel).order_by(LLMModel.model_number).all():
                provider = db.query(LLMProvider).filter(LLMProvider.id == m.provider_id).first()
                result.append({
                    "id": str(m.id), "model_number": m.model_number,
                    "provider_id": str(m.provider_id),
                    "provider_name": provider.name if provider else "-",
                    "model_name": m.model_name,
                    "display_name": m.display_name or "",
                    "context_size": m.context_size,
                    "max_tokens": m.max_tokens,
                    "temperature": float(m.temperature) if m.temperature else None,
                    "enabled": m.enabled,
                    "timeout": m.timeout
                })
            return jsonify(result)
        data = request.get_json()
        max_n = db.query(LLMModel.model_number).order_by(LLMModel.model_number.desc()).first()
        next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
        m = LLMModel(
            model_number=next_n,
            provider_id=data["provider_id"],
            model_name=data["model_name"],
            display_name=data.get("display_name"),
            context_size=data.get("context_size"),
            max_tokens=data.get("max_tokens"),
            temperature=data.get("temperature"),
            enabled=data.get("enabled", True),
            timeout=data.get("timeout", 180)
        )
        db.add(m)
        db.commit()
    return jsonify({"status": "ok", "id": str(m.id)})


@app.route("/api/llm_models/<mid>", methods=["GET", "PUT", "DELETE"])
def api_llm_model(mid):
    with get_db() as db:
        m = db.query(LLMModel).filter(LLMModel.id == mid).first()
        if not m:
            return jsonify({"status": "error", "message": "Not found"}), 404
        if request.method == "GET":
            return jsonify({
                "id": str(m.id), "model_number": m.model_number,
                "provider_id": str(m.provider_id),
                "model_name": m.model_name,
                "display_name": m.display_name or "",
                "context_size": m.context_size,
                "max_tokens": m.max_tokens,
                "temperature": float(m.temperature) if m.temperature else None,
                "enabled": m.enabled,
                "timeout": m.timeout
            })
        if request.method == "PUT":
            data = request.get_json()
            for f in ["provider_id", "model_name", "display_name", "context_size",
                       "max_tokens", "temperature", "enabled", "timeout"]:
                if f in data: setattr(m, f, data[f])
            db.commit()
            return jsonify({"status": "ok"})
        if request.method == "DELETE":
            db.delete(m)
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: LLM Fallbacks
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/llm_fallbacks", methods=["GET", "POST"])
def api_llm_fallbacks():
    with get_db() as db:
        if request.method == "GET":
            result = []
            for f in db.query(LLMFallback).order_by(LLMFallback.relation_number).all():
                model = db.query(LLMModel).filter(LLMModel.id == f.model_id).first()
                fb_model = db.query(LLMModel).filter(LLMModel.id == f.fallback_model_id).first()
                m_prov = db.query(LLMProvider).filter(LLMProvider.id == model.provider_id).first() if model else None
                fb_prov = db.query(LLMProvider).filter(LLMProvider.id == fb_model.provider_id).first() if fb_model else None
                result.append({
                    "id": str(f.id), "relation_number": f.relation_number,
                    "model_id": str(f.model_id),
                    "model_label": f"{m_prov.name if m_prov else '-'} - {model.display_name or model.model_name}" if model else str(f.model_id),
                    "fallback_model_id": str(f.fallback_model_id),
                    "fallback_label": f"{fb_prov.name if fb_prov else '-'} - {fb_model.display_name or fb_model.model_name}" if fb_model else str(f.fallback_model_id),
                    "priority": f.priority
                })
            return jsonify(result)
        data = request.get_json()
        max_n = db.query(LLMFallback.relation_number).order_by(LLMFallback.relation_number.desc()).first()
        next_n = (max_n[0] + 1) if max_n and max_n[0] else 1
        f = LLMFallback(
            relation_number=next_n,
            model_id=data["model_id"],
            fallback_model_id=data["fallback_model_id"],
            priority=data.get("priority", 1)
        )
        db.add(f)
        db.commit()
    return jsonify({"status": "ok", "id": str(f.id)})


@app.route("/api/llm_fallbacks/<fid>", methods=["GET", "DELETE"])
def api_llm_fallback(fid):
    with get_db() as db:
        f = db.query(LLMFallback).filter(LLMFallback.id == fid).first()
        if not f:
            return jsonify({"status": "error", "message": "Not found"}), 404
        if request.method == "DELETE":
            db.delete(f)
            db.commit()
            return jsonify({"status": "ok"})
        return jsonify({"id": str(f.id), "model_id": str(f.model_id),
                        "fallback_model_id": str(f.fallback_model_id), "priority": f.priority})


# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
