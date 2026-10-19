from aiogram.fsm.state import State, StatesGroup


class OCRStates(StatesGroup):
    waiting_for_media = State()
    reviewing_text = State()
    editing_text = State()
