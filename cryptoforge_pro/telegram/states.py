"""FSM states for CryptoForge Pro."""

from aiogram.fsm.state import State, StatesGroup


class NavStates(StatesGroup):
    main_menu = State()
    wait_symbol = State()
    wait_search_query = State()
    settings = State()
    settings_confidence = State()
    wait_alert_symbol = State()
    wait_alert_above = State()
    wait_alert_below = State()
    wait_risk_size = State()
    wait_risk_stop = State()
