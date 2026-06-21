import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.web_queries import load_group_rows

sf = create_session_factory(r'C:\Users\dgtan\Documents\telegram_kol\telegram-kol-analyzer\data\research.db')
rows = load_group_rows(sf, group_labels_by_title={}, configured_groups=[])
print('=== load_group_rows() top 5 ===')
for i, r in enumerate(rows[:5]):
    print(f'{i+1}. chat_id={r["chat_id"]} title={r["title"]} last={r["last_posted_at"]} count={r["message_count"]}')
