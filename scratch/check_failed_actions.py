import sqlite3

def check_failed_actions():
    conn = sqlite3.connect("kubeclaw_audit.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tool_name, status, result, tool_args FROM actions WHERE status = 'failed' ORDER BY id DESC LIMIT 5")
    rows = cursor.fetchall()
    for r in rows:
        print(f"Tool: {r[0]} | Status: {r[1]}")
        print(f"Error Result: {r[2]}")
        print(f"Args: {r[3]}")
        print("-" * 40)
    conn.close()

if __name__ == "__main__":
    check_failed_actions()
