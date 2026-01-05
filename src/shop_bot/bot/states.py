from aiogram.fsm.state import State, StatesGroup

class PaymentProcess(StatesGroup):
    waiting_for_payment_method = State()

class TopUpProcess(StatesGroup):
    waiting_for_topup_amount = State()
    waiting_for_topup_method = State()
