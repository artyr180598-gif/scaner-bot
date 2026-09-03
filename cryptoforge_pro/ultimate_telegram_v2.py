from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptoforge_pro.ultimate_bot import Scanner, Store, price

router = Router()
scanner: Scanner | None = None
store: Store | None = None
allowed: set[int] = set()

class Form(StatesGroup):
    symbol = State()
    risk = State()

def setup(sc, st, ids):
    global scanner, store, allowed
    scanner, store, allowed = sc, st, ids

def permitted(uid: int) -> bool:
    return not allowed or uid in allowed

def menu_kb():
    b = InlineKeyboardBuilder()
    for text, data in [("🔎 Сканер", "scan"), ("📊 Монета", "analyze"), ("📈 LONG", "long"), ("📉 SHORT", "short"), ("🌐 Рынок", "market"), ("🎯 Риск", "risk"), ("🕘 История", "history"), ("⚙️ Настройки", "settings"), ("ℹ️ Методика", "help")]:
        b.button(text=text, callback_data=data)
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()

def back_kb():
    b = InlineKeyboardBuilder(); b.button(text="🏠 Главное меню", callback_data="menu"); return b.as_markup()

def render(s):
    if s.side == "NO TRADE":
        return (f"🟡 <b>{s.symbol} — NO TRADE</b>\n📊 Score: <b>{s.score:.0f}%</b>\n💰 Цена: <code>{price(s.price)}</code>\n\n" +
                "<b>Причина:</b>\n" + "\n".join("• " + x for x in s.risks) +
                "\n\nСигнал намеренно отклонён: качество недостаточное.")
    return (f"{'🟢' if s.side == 'LONG' else '🔴'} <b>{s.symbol} — {s.side}</b>\n"
            f"🎯 Score: <b>{s.score:.0f}%</b> | TF <b>{s.timeframe}</b>\n\n"
            f"💰 Цена <code>{price(s.price)}</code>\n🎯 Вход <code>{price(s.entry_low)} — {price(s.entry_high)}</code>\n"
            f"🛑 SL <code>{price(s.sl)}</code>\n🥇 TP1 <code>{price(s.tp1)}</code>\n🥈 TP2 <code>{price(s.tp2)}</code>\n🥉 TP3 <code>{price(s.tp3)}</code>\n"
            f"⚖️ R:R <code>1:{s.rr1:g} / 1:{s.rr2:g} / 1:{s.rr3:g}</code>\n\n"
            f"<b>Подтверждения</b>\n" + "\n".join("• " + x for x in s.reasons) + "\n\n" +
            f"<b>Риски</b>\n" + "\n".join("• " + x for x in s.risks) + "\n\n" +
            f"📐 RSI {s.metrics['rsi']} | ADX {s.metrics['adx']} | ATR {s.metrics['atr_pct']}% | Vol {s.metrics['volume_ratio']}x\n"
            f"📊 Funding {s.metrics['funding_pct']:+.4f}% | OI Δ {s.metrics['oi_change_pct']:+.2f}% | Spread {s.metrics['spread_pct']:.3f}%\n"
            f"🌐 24h {s.metrics['change24h_pct']:+.2f}% | Turnover ${s.metrics['turnover24h']:,.0f}\n\n"
            "⚠️ <i>Score — оценка качества модели, не вероятность прибыли.</i>")

async def run_scan(m, side=None):
    if scanner is None: return
    await m.answer("⏳ Сканирую реальные USDT perpetual Bybit: liquidity → multi-TF → technicals → derivatives...")
    try: results = await scanner.scan(side, 5)
    except Exception as e: return await m.answer(f"❌ Bybit недоступен: {str(e)[:220]}", reply_markup=back_kb())
    if not results: return await m.answer("🟡 <b>NO TRADE</b>\nНи один кандидат не прошёл фильтр качества.", reply_markup=back_kb())
    for s in results:
        if store: await store.save(s)
        await m.answer(render(s), reply_markup=back_kb())

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if not permitted(m.from_user.id): return await m.answer("⛔ Доступ ограничен.")
    await state.clear()
    await m.answer("<b>🚀 CryptoForge Ultimate</b>\n\nРеальный Bybit futures research bot. Без автоторговли. Сигнал проходит мульти-ТФ и derivatives-фильтры; слабый рынок = NO TRADE.", reply_markup=menu_kb())

@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=menu_kb()); await c.answer()

@router.callback_query(F.data.in_({"scan", "long", "short"}))
async def scan_callback(c: CallbackQuery):
    await c.answer(); await run_scan(c.message, None if c.data == "scan" else c.data.upper())

@router.callback_query(F.data == "analyze")
async def analyze(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.symbol); await c.message.edit_text("📊 Введи BTC, ETH, SOL или полный USDT perpetual символ.", reply_markup=back_kb()); await c.answer()

@router.message(Form.symbol)
async def symbol(m: Message, state: FSMContext):
    if scanner is None: return
    sym = (m.text or "").upper().replace("/", "").strip(); sym = sym if sym.endswith("USDT") else sym + "USDT"
    await state.clear(); await m.answer(f"⏳ Анализ {sym}: 15m / 1h / 4h / 1d + derivatives...")
    try: s = await scanner.analyze(sym)
    except Exception as e: return await m.answer(f"❌ {str(e)[:240]}", reply_markup=back_kb())
    if store: await store.save(s)
    await m.answer(render(s), reply_markup=back_kb())

@router.callback_query(F.data == "market")
async def market(c: CallbackQuery):
    if scanner is None: return
    await c.answer()
    try:
        rows = list((await scanner.universe()).values()); rows.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        up = sum(float(x.get("price24hPcnt", 0)) > 0 for x in rows)
        text = f"🌐 <b>Bybit USDT Perpetual</b>\n\nUniverse: <b>{len(rows)}</b>\n🟢 {up} растут | 🔴 {len(rows)-up} падают\n\n" + "\n".join(f"{x['symbol']} {float(x.get('price24hPcnt',0))*100:+.2f}% | ${float(x.get('turnover24h',0)):,.0f}" for x in rows[:8])
        await c.message.edit_text(text, reply_markup=back_kb())
    except Exception as e: await c.message.edit_text(f"❌ {str(e)[:200]}", reply_markup=back_kb())

@router.callback_query(F.data == "history")
async def history(c: CallbackQuery):
    if store is None: return
    rows = await store.recent(10); text = "🕘 <b>Последние идеи</b>\n\n"
    text += "\n".join(f"{r[1]} {r[2]} — {r[3]:.0f}% | {price(r[4])}" for r in rows) if rows else "История пока пуста."
    await c.message.edit_text(text, reply_markup=back_kb()); await c.answer()

@router.callback_query(F.data == "risk")
async def risk(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.risk); await c.message.edit_text("🎯 Риск-калькулятор\nФормат: <code>1000 1 60000 59000</code> = депозит, риск%, вход, SL.", reply_markup=back_kb()); await c.answer()

@router.message(Form.risk)
async def risk_calc(m: Message, state: FSMContext):
    try:
        dep, pct, entry, sl = map(float, (m.text or "").split()[:4]); risk_usd = dep * pct / 100; distance = abs(entry - sl); qty = risk_usd / distance if distance else 0
        await m.answer(f"🎯 Риск: <b>${risk_usd:,.2f}</b>\nSL distance: <b>{distance:,.6f}</b>\nРазмер: <b>{qty:,.6f}</b> единиц\nНоминал: <b>${qty*entry:,.2f}</b>\n\n⚠️ Без комиссий, funding и slippage.", reply_markup=back_kb())
    except (ValueError, IndexError): await m.answer("Формат: <code>1000 1 60000 59000</code>", reply_markup=back_kb())
    await state.clear()

@router.callback_query(F.data == "settings")
async def settings(c: CallbackQuery):
    await c.message.edit_text("⚙️ <b>Настройки</b>\n\nBybit USDT linear perpetual\nДо 120 кандидатов\n15m / 1h / 4h / 1d\nTechnical + derivatives\nМинимальный score: 60\nАвтоторговля: выключена", reply_markup=back_kb()); await c.answer()

@router.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await c.message.edit_text("ℹ️ <b>Методика</b>\n\nUniverse → liquidity → multi-TF trend → momentum → structure → volatility → volume → funding/OI/spread → score → entry/SL/TP.\n\nNO TRADE используется при слабом или конфликтном сетапе. Score не является математической вероятностью выигрыша.", reply_markup=back_kb()); await c.answer()
