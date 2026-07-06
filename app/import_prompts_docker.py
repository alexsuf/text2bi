#!/usr/bin/env python3
"""Import prompt content from text files into PostgreSQL dash-config."""
import psycopg2, os, glob

conn = psycopg2.connect(host="host.docker.internal", port=1111, user="postgres", password="secret", dbname="dash-config")
conn.autocommit = True
cur = conn.cursor()

prompt_map = {
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

for key, fname in prompt_map.items():
    if os.path.exists(f"/tmp/{fname}"):
        with open(f"/tmp/{fname}", "r", encoding="utf-8") as f:
            content = f.read()
        cur.execute("UPDATE app.prompts SET content = %s WHERE prompt_key = %s", (content, key))
        print(f"  updated: {key} ({len(content)} bytes)")
    else:
        print(f"  SKIP: {fname} not found in /tmp")

cur.close()
conn.close()
print("Done.")
