
# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from aiogram import Dispatcher
from plugins import Admin, PayBus, callbacks, inform, Crypto


def register_all(dp: Dispatcher) -> None:
    dp.include_router(Admin.router)
    dp.include_router(PayBus.router)
    dp.include_router(Crypto.router)
    dp.include_router(callbacks.router)
    dp.include_router(inform.router)
