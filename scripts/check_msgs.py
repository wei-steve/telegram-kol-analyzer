import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage

sf = create_session_factory(r'C:\Users\dgtan\Documents\telegram_kol\telegram-kol-analyzer\data\research.db')
with sf() as s:
    rows = s.query(RawMessage).filter(RawMessage.chat_id == -1002344190971).order_by(RawMessage.posted_at.desc()).limit(5).all()
    print('=== ROSE (-1002344190971) last 5 ===')
    for r in rows:
        txt = (r.text or '')[:80]
        print(f'  id={r.id} msg_id={r.message_id} posted={r.posted_at} text={txt}')

    rows2 = s.query(RawMessage).filter(RawMessage.chat_id == -1002282384698).order_by(RawMessage.posted_at.desc()).limit(5).all()
    print('\n=== 比特币军长 (-1002282384698) last 5 ===')
    for r in rows2:
        txt = (r.text or '')[:80]
        print(f'  id={r.id} msg_id={r.message_id} posted={r.posted_at} text={txt}')
