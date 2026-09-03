"""FSM states for CryptoForge Pro."""

from aiogram.fsm.state import State, StatesGroup


class NavStates(StatesGroup):
    main_menu = State()
    wait_symbol = State()
    wait_search_query = State()
    settings = State()
    settings_confidence = State()
