import sqlite3
import json

db = sqlite3.connect('kubeclaw_audit.db')
cursor = db.cursor()

print("--- RECENT ACTIONS ---")
cursor.execute("SELECT * FROM actions ORDER BY timestamp DESC LIMIT 5")
for row in cursor.fetchall():
    print(row)

print("\n--- RECENT EVENTS ---")
cursor.execute("SELECT * FROM events ORDER BY timestamp DESC LIMIT 5")
for row in cursor.fetchall():
    print(row)

db.close()
