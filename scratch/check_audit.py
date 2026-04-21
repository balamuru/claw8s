import sqlite3
import json

def check_audit():
    conn = sqlite3.connect("kubeclaw_audit.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tool_name, status, source, reasoning, tool_args FROM actions ORDER BY id DESC LIMIT 10")
    rows = cursor.fetchall()
    for r in rows:
        print(f"Tool: {r[0]} | Status: {r[1]} | Source: {r[2]}")
        print(f"Reasoning: {r[3]}")
        print(f"Args: {r[4]}")
        print("-" * 40)
    conn.close()

if __name__ == "__main__":
    check_audit()
