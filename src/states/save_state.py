from aiogram.fsm.state import State, StatesGroup


class SaveStates(StatesGroup):
    waiting_for_tag = State()
    waiting_for_folder = State()