from __future__ import annotations
import asyncio, time
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptoforge_pro.ultimate_bot import Scanner, Store, price

router=Router(); scanner:Scanner|None=None; store:Store|None=None; allowed:set[int]=set()
class S(StatesGroup): symbol=State(); risk=State()
def setup(sc,st,ids):
    global scanner,store,allowed; scanner,store,allowed=sc,st,ids

def ok(uid): return not allowed or uid in allowed

def kb():
    b=InlineKeyboardBuilder();
    for text,data in [("🔎 Сканер рынка","scan"),("📊 Анализ монеты","analyze"),("📈 LONG сетапы","long"),("📉 SHORT сетапы","short"),("🌐 Рынок","market"),("🎯 Риск-калькулятор","risk"),("🕘 История","history"),("⚙️ Настройки","settings"),("ℹ️ Как это работает","help")]: b.button(text=text,callback_data=data)
    b.adjust(2,2,2,2,1); return b.as_markup()

def back():
    b=InlineKeyboardBuilder(); b.button(text="🏠 Главное меню",callback_data="menu"); return b.as_markup()

def fmt(s):
    rr=f"1:{s.rr1:g} / 1:{s.rr2:g} / 1:{s.rr3:g}"
    reasons='\n'.join('• '+x for x in s.reasons) or '• Недостаточно подтверждений'
    risks='\n'.join('• '+x for x in s.risks)
    return (f"{'🟢' if s.side=='LONG' else '🔴'} <b>{s.symbol} — {s.side}</b>\n"
            f"🎯 Оценка сетапа: <b>{s.score:.0f}%</b>\n"
            f"⏱ Таймфрейм: <b>{s.timeframe}</b>\n\n"
            f"💰 Текущая цена: <code>{price(s.price)}</code>\n"
            f"🎯 Зона входа: <code>{price(s.entry_low)} — {price(s.entry_high)}</code>\n"
            f"🛑 Stop Loss: <code>{price(s.sl)}</code>\n"
            f"🥇 TP1: <code>{price(s.tp1)}</code>\n"
            f"🥈 TP2: <code>{price(s.tp2)}</code>\n"
            f"🥉 TP3: <code>{price(s.tp3)}</code>\n"
            f"⚖️ R:R: <code>{rr}</code>\n\n"
            f"<b>Почему:</b>\n{reasons}\n\n<b>Риски:</b>\n{risks}\n\n"
            f"📐 RSI {s.metrics['rsi']} | ADX {s.metrics['adx']} | ATR {s.metrics['atr_pct']}% | Vol {s.metrics['volume_ratio']}x\n\n"
            f"⚠️ <i>{'Оценка модели, а не гарантированная вероятность прибыли. Решение остаётся за вами.'}</i>")

async def send_scan(m,side=None):
    if scanner is None:return
    await m.answer("⏳ Сканирую ликвидные USDT perpetual на Bybit и проверяю до 120 кандидатов...\nЭто может занять немного времени.")
    try: sigs=await scanner.scan(side,5)
    except Exception as e: await m.answer(f"❌ Не удалось получить данные Bybit: {str(e)[:180]}",reply_markup=back()); return
    if not sigs: await m.answer("🟡 Сейчас нет сетапов, которые проходят фильтр качества. Это нормальный результат: бот не будет придумывать сигнал.",reply_markup=back()); return
    for s in sigs:
        if store: await store.save(s)
        await m.answer(fmt(s),reply_markup=back())

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    if not ok(m.from_user.id): return await m.answer("⛔ Доступ ограничен.")
    await state.clear(); await m.answer("<b>🚀 CryptoForge Ultimate</b>\n\nЯ не выбираю монету по одному индикатору. Сначала просматриваю ликвидные USDT perpetual, затем проверяю структуру, тренд, momentum, RSI, MACD, ADX, Bollinger, ATR, объём и уровни.\n\n<b>Главное правило:</b> если качественного сетапа нет — я скажу <b>NO TRADE</b>.",reply_markup=kb())

@router.callback_query(F.data=="menu")
async def menu(c:CallbackQuery,state:FSMContext): await state.clear(); await c.message.edit_text("🏠 <b>Главное меню</b>",reply_markup=kb()); await c.answer()
@router.callback_query(F.data=="scan")
async def scan(c:CallbackQuery): await c.answer(); await send_scan(c.message)
@router.callback_query(F.data=="long")
async def long(c:CallbackQuery): await c.answer(); await send_scan(c.message,"LONG")
@router.callback_query(F.data=="short")
async def short(c:CallbackQuery): await c.answer(); await send_scan(c.message,"SHORT")
@router.callback_query(F.data=="analyze")
async def analyze(c:CallbackQuery,state:FSMContext): await state.set_state(S.symbol); await c.message.edit_text("📊 Введи тикер: <code>BTC</code>, <code>ETH</code>, <code>SOL</code> или полный символ.",reply_markup=back()); await c.answer()
@router.message(S.symbol)
async def symbol(m:Message,state:FSMContext):
    if scanner is None:return
    sym=(m.text or '').upper().replace('/','').strip(); sym=sym if sym.endswith('USDT') else sym+'USDT'
    await state.clear(); await m.answer(f"⏳ Глубоко анализирую <b>{sym}</b> по нескольким таймфреймам...")
    try:s=await scanner.analyze(sym)
    except Exception as e:return await m.answer(f"❌ {str(e)[:220]}",reply_markup=back())
    if store: await store.save(s)
    await m.answer(fmt(s),reply_markup=back())
@router.callback_query(F.data=="market")
async def market(c:CallbackQuery):
    if scanner is None:return
    await c.answer();
    try:
        t=await scanner.universe(); rows=list(t.values()); rows.sort(key=lambda x:float(x.get('turnover24h',0)),reverse=True)
        up=sum(float(x.get('price24hPcnt',0))>0 for x in rows); dn=len(rows)-up; top=rows[:5]
        text=f"🌐 <b>Рынок Bybit USDT Perpetual</b>\n\nКонтрактов в сканере: <b>{len(rows)}</b>\n🟢 Растут: <b>{up}</b>\n🔴 Падают: <b>{dn}</b>\n\n<b>Топ по обороту:</b>\n"+'\n'.join(f"{x['symbol']}  {float(x.get('price24hPcnt',0))*100:+.2f}%" for x in top)
        await c.message.edit_text(text,reply_markup=back())
    except Exception as e: await c.message.edit_text(f"❌ Bybit: {str(e)[:180]}",reply_markup=back())
@router.callback_query(F.data=="history")
async def history(c:CallbackQuery):
    if store is None:return
    rows=await store.recent(10); text="🕘 <b>Последние идеи</b>\n\n" or ''
    text+='\n'.join(f"{r[1]} {r[2]} — {r[3]:.0f}% | вход {price(r[4])}" for r in rows) if rows else 'История пока пуста.'
    await c.message.edit_text(text,reply_markup=back()); await c.answer()
@router.callback_query(F.data=="risk")
async def risk(c:CallbackQuery,state:FSMContext): await state.set_state(S.risk); await c.message.edit_text("🎯 <b>Риск-калькулятор</b>\nВведи размер депозита и риск через пробел. Например: <code>1000 1</code> = $1000 депозит, риск 1%.",reply_markup=back()); await c.answer()
@router.message(S.risk)
async def risk_calc(m:Message,state:FSMContext):
    try:
        dep,risk=map(float,(m.text or '').split()[:2]); risk_usd=dep*risk/100; await m.answer(f"🎯 Депозит: <b>${dep:,.2f}</b>\nРиск: <b>{risk:.2f}%</b>\nМаксимальный убыток до комиссий: <b>${risk_usd:,.2f}</b>\n\nДля расчёта размера позиции пришли также цену входа и SL.",reply_markup=back())
    except: await m.answer("Формат: <code>1000 1</code>",reply_markup=back())
    await state.clear()
@router.callback_query(F.data=="settings")
async def settings(c:CallbackQuery): await c.message.edit_text("⚙️ <b>Настройки</b>\n\nСканер: USDT perpetual\nИсточник рынка: Bybit V5\nРежим: сигнал без автоторговли\nКандидаты на проход: до 120\nМинимальная оценка: 60%\nТаймфреймы: 5m / 15m / 1h / 4h / 1d",reply_markup=back()); await c.answer()
@router.callback_query(F.data=="help")
async def help_(c:CallbackQuery): await c.message.edit_text("ℹ️ <b>Как формируется сигнал</b>\n\n1. Отбирается ликвидный рынок perpetual.\n2. Проверяются несколько таймфреймов.\n3. Считаются EMA20/50/200, RSI, MACD, ADX, Bollinger, ATR, объём и структура.\n4. Факторы объединяются в score.\n5. Строятся зона входа, SL и 3 TP от реального ATR/структуры.\n6. При слабом сетапе бот выдаёт NO TRADE.\n\nПроцент — <b>оценка качества/уверенности модели</b>, а не математически доказанная вероятность выигрыша.",reply_markup=back()); await c.answer()
