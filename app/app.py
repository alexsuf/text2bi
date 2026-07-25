from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
import time, os, requests, datetime, re, math, threading, hashlib
from openai import OpenAI

from db import get_db, hash_password
from sqlalchemy.orm import joinedload
from uuid import UUID
from models import (
    SystemConfig, ConnectionSetting, Prompt, SavedQuery, QueryHistory, ReportPromptCase, PromptReport, User,
    LLMProvider, LLMModel, LLMFallback, QueryResult
)
from utils import (
    REDASH_URL, REDASH_API_KEY, REDASH_DATA_SOURCE_ID,
    LLM_MODEL, LLM_TOKEN, get_openai_client,
    db_description, get_connection, get_schema, _get_db_config, get_system_config,
    refresh_system_config,
    build_prompt, build_messages_from_prompt_key,
    validate_and_fix_sql, analyze_sql_error, explain_sql,
    clean_sql, fix_limit, validate_sql, run_sql_to_df, sql_to_one_line,
    render_markdown, save_excel, build_charts, build_chart_body_html,
    build_chart_body_only, save_chart_outputs, create_jpg_collage,
    generate_report, log_llm_request, log_message, log_request_response,
    _add_formatted_text, modify_sql_for_business_terms,
    get_dated_filename, load_prompt_template_from_db,
    get_default_llm_config, get_default_api_key, llm_complete_with_config, get_fallback_configs,
    log_user_llm_request,
)

# Определяем DOWNLOADS_DIR из переменной окружения, как в docker-compose.yaml
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "app/downloads")

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
            {"href": "/config", "icon": "bi-gear", "label": "Конфигурация"},
            {"href": "/providers", "icon": "bi-cloud", "label": "Провайдеры"},
            {"href": "/models", "icon": "bi-robot", "label": "Модели"},
            {"href": "/fallbacks", "icon": "bi-arrow-repeat", "label": "Фолбэки"},
        ])
    return items


def llm_complete(messages, timeout=LLM_TIMEOUT):
    cfg = get_default_llm_config()
    model_id = cfg.get("model_id")
    fallback_configs = get_fallback_configs(model_id) if model_id else []
    total_attempts = 1 + len(fallback_configs)
    hard_timeout = timeout * total_attempts + 30

    result, exc = [], []

    def worker():
        try:
            resp = llm_complete_with_config(messages, timeout)
            result.append(resp)
        except Exception as e:
            exc.append(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(hard_timeout)
    if t.is_alive():
        raise TimeoutError(f"LLM timeout {hard_timeout}s")
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

    u = current_user()

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
                resp = llm_complete(llm_messages)
                sql_query = clean_sql(resp.choices[0].message.content)
                llm_answer = resp.choices[0].message.content
                if question:
                    username = u.username if u else "anonymous"
                    log_user_llm_request(username, question, llm_answer)
                if get_system_config().get("check", "yes").strip().lower() in ("yes", "true", "1"):
                    sql_query = validate_and_fix_sql(question, sql_query, schema_text, db_description)
                model_time = int(time.time() - t0)

            if sql_query:
                t1 = time.time()
                sql_query = fix_limit(clean_sql(sql_query))
                validate_sql(sql_query)
                cfg = _get_db_config()
                db_info = (
                    ">>>>>>>>>>>>>>>>>>>>>>>>>\n"
                    f"host: {cfg['host']}\n"
                    f"port: {cfg['port']}\n"
                    f"database: {cfg['database']}\n"
                    "<<<<<<<<<<<<<<<<<<<<<<<<<<"
                )
                log_message("DB_CONNECT", db_info)
                print(db_info, flush=True)
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
        u = current_user()
        username = u.username if u else "anonymous"
        log_user_llm_request(username, question, answer)
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
#  EXECUTE SQL AJAX
# ═══════════════════════════════════════════════════════════════════
@app.route("/execute_sql_ajax", methods=["POST"])
def execute_sql_ajax():
    try:
        data = request.get_json()
        sql_query = clean_sql(data.get("sql_query", ""))
        if not sql_query:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        sql_query = fix_limit(sql_query)
        validate_sql(sql_query)
        df = run_sql_to_df(sql_query)
        columns = df.columns.tolist()
        rows = df.values.tolist()
        count = len(rows)
        return jsonify({
            "status": "ok",
            "columns": columns,
            "rows": rows,
            "count": count
        })
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
        prompt_id = data.get("prompt_id")
        chart_image_data = data.get("chart_image")
        if not sql:
            return jsonify({"status": "error", "message": "Пустой SQL"})
        sql = fix_limit(sql)
        validate_sql(sql)
        df = run_sql_to_df(sql)
        chart_paths = []
        if chart_image_data:
            import base64
            img_data = base64.b64decode(chart_image_data.split(",")[1] if "," in chart_image_data else chart_image_data)
            cp = os.path.join(DOWNLOADS_DIR, f"_tmp_chart_0.png")
            with open(cp, "wb") as f:
                f.write(img_data)
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
#  SAVE/RESULT QUERY RESULT (cross-page sharing via DB)
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/save_result", methods=["POST"])
def api_save_result():
    u = current_user()
    if not u:
        return jsonify({"status": "error"}), 401
    uid = user_id()
    data = request.get_json()
    with get_db() as db:
        db.query(QueryResult).filter(QueryResult.user_id == uid).delete()
        db.add(QueryResult(
            user_id=uid,
            sql_query=data.get("sql", ""),
            columns=data.get("columns", []),
            data=data.get("rows", [])
        ))
        db.commit()
    return jsonify({"status": "ok"})


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
    uid = user_id()
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
    if not columns and uid:
        with get_db() as db:
            res = db.query(QueryResult).filter(QueryResult.user_id == uid).order_by(QueryResult.created_at.desc()).first()
            if res:
                columns = res.columns or []
                rows = res.data or []
                sql = res.sql_query or sql


    return render_template("graf.html", sql_query=sql, columns=columns, rows=rows,
                           table_mode=table_mode, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  TABLE PAGE
# ═══════════════════════════════════════════════════════════════════
@app.route("/table")
def table_page():
    r = login_required()
    if r: return r
    uid = user_id()
    columns, rows = [], []
    if uid:
        with get_db() as db:
            res = db.query(QueryResult).filter(QueryResult.user_id == uid).order_by(QueryResult.created_at.desc()).first()
            if res:
                columns = res.columns or []
                rows = res.data or []
    return render_template("table.html", columns=columns, rows=rows, sql_query="", menu=menu_items(), user=current_user())


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
        msgs = build_messages_from_prompt_key("prompt_table", {
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
#  MODELS (LLM Models)
# ═══════════════════════════════════════════════════════════════════
@app.route("/models")
def models_list():
    r = login_required()
    if r:
        return r

    with get_db() as db:
        models = db.query(LLMModel).options(joinedload(LLMModel.provider)).all()
        providers = db.query(LLMProvider).all()
    return render_template("models_list.html", models=models, providers=providers, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
@app.route("/config")
def config_page():
    r = login_required()
    if r: return r
    with get_db() as db:
        configs = db.query(SystemConfig).all()
    return render_template("config.html", configs=configs, menu=menu_items(), user=current_user())


# ═══════════════════════════════════════════════════════════════════
#  API: System Config
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/system_config/<key>", methods=["GET", "PUT"])
def api_system_config(key):
    with get_db() as db:
        if request.method == "GET":
            c = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            return jsonify({"key": c.key, "value": c.value}) if c else ("", 404)
        if request.method == "PUT":
            data = request.get_json()
            c = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if not c:
                c = SystemConfig(key=key, value=data.get("value", ""))
                db.add(c)
            else:
                c.value = data.get("value", "")
            db.commit()
            refresh_system_config()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  FALLBACKS
# ═══════════════════════════════════════════════════════════════════
@app.route("/fallbacks")
def fallbacks_page():
    r = login_required()
    if r:
        return r

    with get_db() as db:
        relations = db.query(LLMFallback).order_by(LLMFallback.relation_number).all()
        models = db.query(LLMModel).options(joinedload(LLMModel.provider)).order_by(LLMModel.model_name).all()
        model_map = {str(m.id): m for m in models}
    return render_template("fallbacks_list.html", relations=relations, model_map=model_map, menu=menu_items(), user=current_user())


@app.route("/providers")


@app.route("/connections")
def connections_list():
    r = login_required()
    if r:
        return r

    with get_db() as db:
        connections = db.query(ConnectionSetting).all()
        return render_template("connections_list.html", connections=connections, menu=menu_items(), user=current_user())


@app.route("/prompts")
def prompts_list():
    r = login_required()
    if r:
        return r

    with get_db() as db:
        prompts = db.query(Prompt).all()
        return render_template("prompts_list.html", prompts=prompts, menu=menu_items(), user=current_user())


@app.route("/prompt_reports")
def prompt_reports_list():
    r = login_required()
    if r:
        return r

    with get_db() as db:
        reports = db.query(PromptReport).all()
        return render_template("prompt_reports_list.html", reports=reports, menu=menu_items(), user=current_user())


@app.route("/api/prompt_reports")
def api_prompt_reports():
    with get_db() as db:
        reports = db.query(PromptReport).all()
        return jsonify([{
            "id": r.id,
            "name": r.name,
            "content": r.content,
            "is_active": r.is_active,
            "is_default": r.is_default
        } for r in reports])


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
#  API: Query History
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/query_history", methods=["GET", "POST"])
@app.route("/api/query_history/<int:hid>", methods=["DELETE"])
def api_query_history(hid=None):
    uid = user_id()
    if not uid:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    with get_db() as db:
        if request.method == "GET":
            history = (db.query(QueryHistory)
                       .filter(QueryHistory.user_id == uid)
                       .order_by(QueryHistory.created_at.desc())
                       .all())
            return jsonify([{
                "id": h.id,
                "question": h.question,
                "sql_query": h.generated_sql,
                "created_at": h.created_at.isoformat() if h.created_at else None
            } for h in history])

        if request.method == "POST":
            data = request.get_json()
            if not data or not data.get("sql_query"):
                return jsonify({"status": "error", "message": "sql_query required"}), 400
            h = QueryHistory(
                user_id=uid,
                question=data.get("question", ""),
                generated_sql=data["sql_query"]
            )
            db.add(h)
            db.commit()
            return jsonify({"status": "ok", "id": h.id})

        if request.method == "DELETE" and hid is None:
            db.query(QueryHistory).filter(QueryHistory.user_id == uid).delete()
            db.commit()
            return jsonify({"status": "ok"})

        if request.method == "DELETE" and hid is not None:
            db.query(QueryHistory).filter(QueryHistory.id == hid, QueryHistory.user_id == uid).delete()
            db.commit()
            return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
#  API: Generate SQL (JSON endpoint, no page reload)
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/generate_sql", methods=["POST"])
def api_generate_sql():
    r = login_required()
    if r:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    u = current_user()
    data = request.get_json()
    question = (data.get("question") or "").strip()
    sql_query = (data.get("sql_query") or "").strip()

    result = None
    elapsed_time = None
    model_time = None
    sql_time = None
    error_msg = None
    analysis = None
    start_time = time.time()

    try:
        schema_text = get_schema()
        if not sql_query and question:
            t0 = time.time()
            prompt_data = build_prompt(question, schema_text, db_description)
            llm_messages = [{"role": "system", "content": prompt_data["system_role"]},
                            {"role": "user", "content": prompt_data["user_content"]}]
            resp = llm_complete(llm_messages)
            sql_query = clean_sql(resp.choices[0].message.content)
            llm_answer = resp.choices[0].message.content
            if question and u:
                log_user_llm_request(u.username, question, llm_answer)
            if get_system_config().get("check", "yes").strip().lower() in ("yes", "true", "1"):
                sql_query = validate_and_fix_sql(question, sql_query, schema_text, db_description)
            model_time = int(time.time() - t0)

        if sql_query:
            t1 = time.time()
            sql_query = fix_limit(clean_sql(sql_query))
            validate_sql(sql_query)
            cfg = _get_db_config()
            db_info = (
                ">>>>>>>>>>>>>>>>>>>>>>>>>\n"
                f"host: {cfg['host']}\n"
                f"port: {cfg['port']}\n"
                f"database: {cfg['database']}\n"
                "<<<<<<<<<<<<<<<<<<<<<<<<<<"
            )
            log_message("DB_CONNECT", db_info)
            print(db_info, flush=True)
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

    return jsonify({
        "status": "ok" if not error_msg else "error",
        "sql_query": sql_query,
        "question": question,
        "result": result,
        "error": error_msg,
        "analysis": analysis,
        "elapsed_time": elapsed_time,
        "model_time": model_time,
        "sql_time": sql_time
    })


@app.route("/models/edit/<uuid:model_id>", methods=["GET", "POST"])
def models_edit(model_id):
    r = login_required()
    if r:
        return r

    with get_db() as db:
        model = db.query(LLMModel).options(joinedload(LLMModel.provider)).filter(LLMModel.id == model_id).first()
        if not model:
            flash("Модель не найдена", "error")
            return redirect(url_for("models_list"))

        providers = db.query(LLMProvider).all()

        if request.method == "POST":
            model.model_name = request.form.get("model_name", "").strip()
            model.display_name = request.form.get("display_name", "").strip()
            model.provider_id = request.form.get("provider_id")
            model.context_size = int(request.form.get("context_size", 0)) or None
            model.max_tokens = int(request.form.get("max_tokens", 0)) or None
            model.temperature = float(request.form.get("temperature", 0.7)) or None
            model.enabled = bool(request.form.get("enabled"))
            model.is_default = bool(request.form.get("is_default"))
            model.timeout = int(request.form.get("timeout", 180))

            db.commit()
            flash("Модель обновлена", "success")
            return redirect(url_for("models_list"))

        return render_template("models_form.html", model=model, providers=providers, action="edit")


@app.route("/models/new", methods=["POST"])
def create_model():
    r = login_required()
    if r:
        return r

    provider_id = request.form.get("provider_id")
    model_name = request.form.get("model_name")
    display_name = request.form.get("display_name", "")
    context_size = request.form.get("context_size")
    max_tokens = request.form.get("max_tokens")
    temperature = request.form.get("temperature")
    enabled = request.form.get("enabled") == "on"
    is_default = request.form.get("is_default") == "on"

    if not provider_id or not model_name:
        flash("Провайдер и название модели обязательны", "error")
        return redirect(url_for("models_list"))

    try:
        context_size = int(context_size) if context_size else None
        max_tokens = int(max_tokens) if max_tokens else None
        temperature = float(temperature) if temperature else None
    except (ValueError, TypeError):
        flash("Некорректные значения параметров", "error")
        return redirect(url_for("models_list"))

    with get_db() as db:
        new_model = LLMModel(
            provider_id=provider_id,
            model_name=model_name,
            display_name=display_name,
            context_size=context_size,
            max_tokens=max_tokens,
            temperature=temperature,
            enabled=enabled,
            is_default=is_default
        )
        db.add(new_model)
        db.flush()  # чтобы получить model.id

        if is_default:
            # Сбросить is_default у всех других моделей
            db.query(LLMModel).filter(LLMModel.id != new_model.id).update({LLMModel.is_default: False})

        db.commit()
        flash("Модель успешно создана", "success")

    return redirect(url_for("models_list"))


@app.route("/models/delete/<uuid:model_id>", methods=["POST"])
def models_delete(model_id):
    r = login_required()
    if r:
        return r

    with get_db() as db:
        model = db.query(LLMModel).filter(LLMModel.id == model_id).first()
        if not model:
            flash("Модель не найдена", "error")
            return redirect(url_for("models_list"))

        db.query(LLMFallback).filter(
            (LLMFallback.model_id == model_id) | (LLMFallback.fallback_model_id == model_id)
        ).delete(synchronize_session=False)
        db.delete(model)
        db.commit()
        flash(f"Модель «{model.display_name or model.model_name}» удалена вместе со связанными фолбэками", "success")

    return redirect(url_for("models_list"))


@app.route("/models/update/<model_id>", methods=["POST"])
def update_model(model_id):
    r = login_required()
    if r:
        return r

    model_name = request.form.get("model_name")
    display_name = request.form.get("display_name", "")
    context_size = request.form.get("context_size")
    max_tokens = request.form.get("max_tokens")
    temperature = request.form.get("temperature")
    enabled = request.form.get("enabled") == "on"
    is_default = request.form.get("is_default") == "on"

    if not model_name:
        flash("Название модели обязательно", "error")
        return redirect(url_for("models_list"))

    try:
        context_size = int(context_size) if context_size else None
        max_tokens = int(max_tokens) if max_tokens else None
        temperature = float(temperature) if temperature else None
    except (ValueError, TypeError):
        flash("Некорректные значения параметров", "error")
        return redirect(url_for("models_list"))

    with get_db() as db:
        model = db.query(LLMModel).filter(LLMModel.id == model_id).first()
        if not model:
            flash("Модель не найдена", "error")
            return redirect(url_for("models_list"))

        model.model_name = model_name
        model.display_name = display_name
        model.context_size = context_size
        model.max_tokens = max_tokens
        model.temperature = temperature
        model.enabled = enabled

        if is_default:
            # Сбросить is_default у всех других моделей
            db.query(LLMModel).filter(LLMModel.id != model_id).update({LLMModel.is_default: False})
            model.is_default = True
        else:
            # Если снята галочка, просто обновляем остальные поля
            model.is_default = False

        db.commit()
        flash("Модель успешно обновлена", "success")

    return redirect(url_for("models_list"))


# ═══════════════════════════════════════════════════════════════════
#  SEND TO REDASH
# ═══════════════════════════════════════════════════════════════════
@app.route("/send_to_redash", methods=["POST"])
def send_to_redash():
    try:
        data = request.get_json()
        q = data.get("question", "").strip()
        if not q:
            return jsonify({"status": "error", "message": "Пустой вопрос"})
        # Здесь можно добавить логику отправки в Redash
        return jsonify({"status": "ok", "message": "Запрос отправлен в Redash"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════
#  DOWNLOAD FILE
# ═══════════════════════════════════════════════════════════════════
@app.route("/download_file/<path:filename>")
def download_file_route(filename):
    filepath = os.path.normpath(os.path.join(DOWNLOADS_DIR, filename))
    if not filepath.startswith(os.path.normpath(DOWNLOADS_DIR)):
        return "Forbidden", 403
    if not os.path.exists(filepath):
        return "File not found", 404
    return send_file(filepath, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
