import sqlite3
import json
from collections import Counter

def analyze_history():
    conn = sqlite3.connect("kubeclaw_audit.db")
    cursor = conn.cursor()
    
    # 1. Most frequent incident reasons
    print("### Incident Frequency ###")
    cursor.execute("SELECT reason, count(*) FROM events GROUP BY reason ORDER BY count(*) DESC")
    for r in cursor.fetchall():
        print(f"{r[0]}: {r[1]}")
    print("\n")

    # 2. Most frequent successful tool calls by the Soul
    print("### Successful Soul Interventions ###")
    cursor.execute("""
        SELECT tool_name, tool_args, reasoning 
        FROM actions 
        WHERE source = 'soul' AND status = 'executed'
        LIMIT 20
    """)
    for r in cursor.fetchall():
        print(f"Tool: {r[0]}")
        print(f"Reasoning: {r[2][:200]}...")
        print(f"Args: {r[1]}")
        print("-" * 20)
    
    conn.close()

if __name__ == "__main__":
    analyze_history()
