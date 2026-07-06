"""Импорт содержимого промптов из txt-файлов в таблицу app.prompts."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_db
from models import Prompt

prompts_map = {
    "prompt_generate_sql": "prompt_generate_sql.txt",
    "prompt_report": "prompt_report.txt",
    "prompt_validate_sql": "prompt_validate_sql.txt",
    "prompt_explain_sql": "prompt_explain_sql.txt",
    "prompt_chat_ask": "prompt_chat_ask.txt",
    "prompt_modify_sql": "prompt_modify_sql.txt",
    "prompt_analyze_sql": "prompt_analyze_sql.txt",
    "prompt_chat_ask_all": "prompt_chat_ask_all.txt",
    "prompt_chat_ask_only": "prompt_chat_ask_only.txt",
}

with get_db() as db:
    for key, filename in prompts_map.items():
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                content = f.read()
            prompt = db.query(Prompt).filter(Prompt.prompt_key == key).first()
            if prompt:
                prompt.content = content
                print(f"  updated: {key} ({len(content)} bytes)")
            else:
                print(f"  SKIP: {key} - not found in DB")
    db.commit()
print("Done.")
