import logging
import uuid
import hashlib
import json
import urllib.parse
from decimal import Decimal
from urllib.parse import urlencode

from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import (
    get_user, get_plan_by_id, get_setting, create_pending_transaction,
    update_transaction_status, update_user_balance,
    get_promo_code, use_promo_code, create_user_key, get_user_keys,
    get_transaction_by_payment_id, get_host_by_name, get_key_by_id, update_key_expiry
)
from shop_bot.bot import keyboards
from shop_bot.modules import xui_api

logger = logging.getLogger(__name__)
user_router = Router()

PAYMENT_METHODS = {}

class PaymentProcess(StatesGroup):
    waiting_for_payment_method = State()

class TopUpProcess(StatesGroup):
    waiting_for_topup_amount = State()
    waiting_for_topup_method = State()

# --- Successful Payment Processor ---
async def process_successful_payment(bot: Bot, metadata: dict):
    """
    Обработка успешного платежа.
    metadata: словарь с данными платежа (user_id, action, amount, payment_id, etc.)
    """
    try:
        payment_id = metadata.get('payment_id')
        user_id = int(metadata.get('user_id'))
        action = metadata.get('action')
        amount = float(metadata.get('price', 0))
        
        logger.info(f"Processing payment {payment_id} for user {user_id}, action: {action}, amount: {amount}")
        
        # Обновляем статус транзакции
        update_transaction_status(payment_id, 'paid')
        
        if action == 'top_up':
            # Пополнение баланса
            new_balance = update_user_balance(user_id, amount)
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Баланс успешно пополнен на {amount} RUB.\nТекущий баланс: {new_balance} RUB"
            )
            
        else:
            # Покупка или продление ключа
            plan_id = metadata.get('plan_id')
            months = int(metadata.get('months', 1))
            host_name = metadata.get('host_name')
            email = metadata.get('customer_email')
            key_id = metadata.get('key_id')
            
            if key_id:
                # Продление существующего ключа
                key_data = get_key_by_id(key_id)
                if key_data:
                    # Используем create_or_update_key_on_host для продления
                    # days_to_add = months * 30 (примерно)
                    days = months * 30
                    result = await xui_api.create_or_update_key_on_host(
                        key_data['host_name'], 
                        key_data['key_email'], 
                        days_to_add=days
                    )
                    
                    if result:
                        update_key_expiry(key_id, result['expiry_timestamp_ms'])
                        await bot.send_message(
                            chat_id=user_id, 
                            text=f"✅ Ключ успешно продлен на {months} мес.\nНовая дата окончания: {datetime.fromtimestamp(result['expiry_timestamp_ms']/1000).strftime('%Y-%m-%d %H:%M')}"
                        )
                    else:
                        await bot.send_message(chat_id=user_id, text="❌ Ошибка при продлении ключа на сервере. Обратитесь в поддержку.")
                else:
                    await bot.send_message(chat_id=user_id, text="❌ Ключ не найден в базе данных.")
            else:
                # Создание нового ключа
                # Генерируем email если нет
                if not email:
                    import random
                    import string
                    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
                    email = f"user_{user_id}_{suffix}"
                
                # Создаем ключ в панели
                # Получаем данные хоста
                # host = get_host_by_name(host_name) # Предполагаем наличие такой функции или берем из settings
                # Для создания ключа используем xui_api
                client = await xui_api.create_client(host_name, email, months=months)
                
                if client:
                    # Сохраняем в БД
                    create_user_key(user_id, host_name, client['client_uuid'], email, client['expiry_timestamp_ms'])
                    
                    # Отправляем ключ пользователю
                    msg = (
                        f"✅ Оплата прошла успешно!\n\n"
                        f"Ваш ключ доступа:\n`{client['connection_string']}`\n\n"
                        f"Инструкции по настройке доступны в главном меню."
                    )
                    await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                else:
                    await bot.send_message(chat_id=user_id, text="✅ Оплата прошла, но возникла ошибка при создании ключа. Обратитесь в поддержку.")
                    logger.error(f"Failed to create client for payment {payment_id}")

            # Применяем промокод если был
            promo_code = metadata.get('promo_code')
            if promo_code:
                use_promo_code(promo_code, user_id)

    except Exception as e:
        logger.error(f"Error processing payment {metadata}: {e}", exc_info=True)


# --- YooMoney Handlers ---
@user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yoomoney")
async def create_yoomoney_payment_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Создаю ссылку YooMoney...")
    data = await state.get_data()
    user_data = get_user(callback.from_user.id)
    plan = get_plan_by_id(data.get('plan_id'))
    if not plan:
        await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
        await state.clear()
        return
    
    base_price = Decimal(str(plan['price']))
    price_rub = base_price
    if user_data and user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
        try:
            discount_percentage = Decimal(get_setting("referral_discount") or "0")
        except Exception:
            discount_percentage = Decimal("0")
        if discount_percentage > 0:
            price_rub = base_price - (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
    
    final_price_decimal = price_rub
    try:
        final_price_from_state = data.get('final_price')
        if final_price_from_state is not None:
            final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
    except Exception:
        pass
    if final_price_decimal < Decimal('0'):
        final_price_decimal = Decimal('0.00')
        
    final_price_float = float(final_price_decimal)
    
    wallet = (get_setting("yoomoney_wallet") or "").strip()
    if not wallet:
        await callback.message.edit_text("❌ Оплата через YooMoney временно недоступна (не настроен кошелек).")
        await state.clear()
        return
        
    months = int(plan['months'])
    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "months": months,
        "price": final_price_float,
        "action": data.get('action'),
        "key_id": data.get('key_id'),
        "host_name": data.get('host_name'),
        "plan_id": data.get('plan_id'),
        "customer_email": data.get('customer_email'),
        "payment_method": "YooMoney",
        "promo_code": data.get('promo_code'),
        "promo_discount_percent": data.get('promo_discount_percent'),
        "promo_discount_amount": data.get('promo_discount_amount'),
    }
    
    try:
        create_pending_transaction(payment_id, user_id, final_price_float, metadata)
    except Exception as e:
        logger.warning(f"YooMoney: не удалось создать ожидающую транзакцию: {e}")
        
    desc = f"Оплата {months} мес. (User {user_id})"
    # label в YooMoney используется как идентификатор платежа
    pay_url = _build_yoomoney_url(wallet, final_price_float, payment_id, desc)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты (YooMoney):",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

@user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_yoomoney")
async def topup_pay_yoomoney(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Готовлю YooMoney...")
    data = await state.get_data()
    amount = Decimal(str(data.get('topup_amount', 0)))
    if amount <= 0:
        await callback.message.edit_text("❌ Некорректная сумма пополнения.")
        await state.clear()
        return
        
    wallet = (get_setting("yoomoney_wallet") or "").strip()
    if not wallet:
        await callback.message.edit_text("❌ Оплата через YooMoney временно недоступна.")
        await state.clear()
        return

    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "price": float(amount),
        "action": "top_up",
        "payment_method": "YooMoney",
    }
    try:
        create_pending_transaction(payment_id, user_id, float(amount), metadata)
    except Exception as e:
        logger.warning(f"YooMoney topup: не удалось создать ожидающую транзакцию: {e}")
        
    desc = f"Пополнение баланса (User {user_id})"
    pay_url = _build_yoomoney_url(wallet, float(amount), payment_id, desc)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты (YooMoney):",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

def _build_yoomoney_url(wallet: str, amount: float, label: str, desc: str) -> str:
    # https://yoomoney.ru/quickpay/confirm.xml
    # receiver, quickpay-form, targets, paymentType, sum, label
    qs = urlencode({
        "receiver": wallet,
        "quickpay-form": "shop",
        "targets": desc,
        "paymentType": "PC", # PC = ЮMoney кошелек, AC = карта
        "sum": f"{amount:.2f}",
        "label": label
    })
    return f"https://yoomoney.ru/quickpay/confirm.xml?{qs}"


# --- Unitpay Handlers ---
@user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_unitpay")
async def create_unitpay_payment_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Создаю ссылку Unitpay...")
    data = await state.get_data()
    user_data = get_user(callback.from_user.id)
    plan = get_plan_by_id(data.get('plan_id'))
    if not plan:
        await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
        await state.clear()
        return
    
    base_price = Decimal(str(plan['price']))
    price_rub = base_price
    if user_data and user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
        try:
            discount_percentage = Decimal(get_setting("referral_discount") or "0")
        except Exception:
            discount_percentage = Decimal("0")
        if discount_percentage > 0:
            price_rub = base_price - (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
    
    final_price_decimal = price_rub
    try:
        final_price_from_state = data.get('final_price')
        if final_price_from_state is not None:
            final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
    except Exception:
        pass
    if final_price_decimal < Decimal('0'):
        final_price_decimal = Decimal('0.00')
        
    final_price_float = float(final_price_decimal)
    
    public_key = (get_setting("unitpay_public_key") or "").strip()
    secret_key = (get_setting("unitpay_secret_key") or "").strip()
    domain = (get_setting("unitpay_domain") or "unitpay.money").strip()
    
    if not public_key or not secret_key:
        await callback.message.edit_text("❌ Оплата через Unitpay временно недоступна.")
        await state.clear()
        return
        
    months = int(plan['months'])
    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "months": months,
        "price": final_price_float,
        "action": data.get('action'),
        "key_id": data.get('key_id'),
        "host_name": data.get('host_name'),
        "plan_id": data.get('plan_id'),
        "customer_email": data.get('customer_email'),
        "payment_method": "Unitpay",
        "promo_code": data.get('promo_code'),
        "promo_discount_percent": data.get('promo_discount_percent'),
        "promo_discount_amount": data.get('promo_discount_amount'),
    }
    
    try:
        create_pending_transaction(payment_id, user_id, final_price_float, metadata)
    except Exception as e:
        logger.warning(f"Unitpay: не удалось создать ожидающую транзакцию: {e}")
        
    desc = f"Оплата {months} мес."
    pay_url = _build_unitpay_url(domain, public_key, secret_key, final_price_float, payment_id, desc)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

@user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_unitpay")
async def topup_pay_unitpay(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Готовлю Unitpay...")
    data = await state.get_data()
    amount = Decimal(str(data.get('topup_amount', 0)))
    if amount <= 0:
        await callback.message.edit_text("❌ Некорректная сумма пополнения.")
        await state.clear()
        return
        
    public_key = (get_setting("unitpay_public_key") or "").strip()
    secret_key = (get_setting("unitpay_secret_key") or "").strip()
    domain = (get_setting("unitpay_domain") or "unitpay.money").strip()
    
    if not public_key or not secret_key:
        await callback.message.edit_text("❌ Оплата через Unitpay временно недоступна.")
        await state.clear()
        return

    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "price": float(amount),
        "action": "top_up",
        "payment_method": "Unitpay",
    }
    try:
        create_pending_transaction(payment_id, user_id, float(amount), metadata)
    except Exception as e:
        logger.warning(f"Unitpay topup: не удалось создать ожидающую транзакцию: {e}")
        
    desc = f"Пополнение на {amount:.2f} RUB"
    pay_url = _build_unitpay_url(domain, public_key, secret_key, float(amount), payment_id, desc)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

def _build_unitpay_url(domain: str, public_key: str, secret_key: str, amount: float, account: str, desc: str) -> str:
    # Unitpay signature: sha256(params + secret) where params are sorted alphabetically
    # Required params for signature: account, desc, sum
    # sum should be string, e.g. "10.00"
    sum_str = f"{amount:.2f}"
    
    # params dict for signature
    params = {
        "account": account,
        "desc": desc,
        "sum": sum_str
    }
    
    # Sort keys
    sorted_keys = sorted(params.keys())
    # Join values
    vals = [params[k] for k in sorted_keys]
    vals.append(secret_key)
    joined = "{up}".join(vals)
    
    import hashlib
    signature = hashlib.sha256(joined.encode('utf-8')).hexdigest()
    
    # Build URL
    # https://{domain}/pay/{public_key}?sum={sum}&account={account}&desc={desc}&signature={signature}
    qs = urlencode({
        "sum": sum_str,
        "account": account,
        "desc": desc,
        "signature": signature
    })
    return f"https://{domain}/pay/{public_key}?{qs}"

# --- Freekassa Handlers ---
@user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_freekassa")
async def create_freekassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Создаю ссылку Freekassa...")
    data = await state.get_data()
    user_data = get_user(callback.from_user.id)
    plan = get_plan_by_id(data.get('plan_id'))
    if not plan:
        await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
        await state.clear()
        return
    
    base_price = Decimal(str(plan['price']))
    price_rub = base_price
    if user_data and user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
        try:
            discount_percentage = Decimal(get_setting("referral_discount") or "0")
        except Exception:
            discount_percentage = Decimal("0")
        if discount_percentage > 0:
            price_rub = base_price - (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
    
    final_price_decimal = price_rub
    try:
        final_price_from_state = data.get('final_price')
        if final_price_from_state is not None:
            final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
    except Exception:
        pass
    if final_price_decimal < Decimal('0'):
        final_price_decimal = Decimal('0.00')
        
    final_price_float = float(final_price_decimal)
    
    shop_id = (get_setting("freekassa_shop_id") or "").strip()
    secret_key = (get_setting("freekassa_api_key") or "").strip() # secret_key_1 usually used for signature form
    
    if not shop_id or not secret_key:
        await callback.message.edit_text("❌ Оплата через Freekassa временно недоступна.")
        await state.clear()
        return
        
    months = int(plan['months'])
    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "months": months,
        "price": final_price_float,
        "action": data.get('action'),
        "key_id": data.get('key_id'),
        "host_name": data.get('host_name'),
        "plan_id": data.get('plan_id'),
        "customer_email": data.get('customer_email'),
        "payment_method": "Freekassa",
        "promo_code": data.get('promo_code'),
        "promo_discount_percent": data.get('promo_discount_percent'),
        "promo_discount_amount": data.get('promo_discount_amount'),
    }
    
    try:
        create_pending_transaction(payment_id, user_id, final_price_float, metadata)
    except Exception as e:
        logger.warning(f"Freekassa: не удалось создать ожидающую транзакцию: {e}")
        
    pay_url = _build_freekassa_url(shop_id, secret_key, final_price_float, payment_id)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

@user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_freekassa")
async def topup_pay_freekassa(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Готовлю Freekassa...")
    data = await state.get_data()
    amount = Decimal(str(data.get('topup_amount', 0)))
    if amount <= 0:
        await callback.message.edit_text("❌ Некорректная сумма пополнения.")
        await state.clear()
        return
        
    shop_id = (get_setting("freekassa_shop_id") or "").strip()
    secret_key = (get_setting("freekassa_api_key") or "").strip()
    
    if not shop_id or not secret_key:
        await callback.message.edit_text("❌ Оплата через Freekassa временно недоступна.")
        await state.clear()
        return

    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "price": float(amount),
        "action": "top_up",
        "payment_method": "Freekassa",
    }
    try:
        create_pending_transaction(payment_id, user_id, float(amount), metadata)
    except Exception as e:
        logger.warning(f"Freekassa topup: не удалось создать ожидающую транзакцию: {e}")
        
    pay_url = _build_freekassa_url(shop_id, secret_key, float(amount), payment_id)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

def _build_freekassa_url(shop_id: str, secret_key: str, amount: float, order_id: str) -> str:
    # Signature: md5(shop_id:amount:secret_key:currency:order_id)
    currency = "RUB"
    amount_str = f"{amount:.2f}" # Freekassa expects amount as is, usually dot separated
    
    raw = f"{shop_id}:{amount_str}:{secret_key}:{currency}:{order_id}"
    import hashlib
    sign = hashlib.md5(raw.encode('utf-8')).hexdigest()
    
    qs = urlencode({
        "m": shop_id,
        "oa": amount_str,
        "o": order_id,
        "s": sign,
        "currency": currency
    })
    return f"https://pay.freekassa.ru/?{qs}"

# --- Enot.io Handlers ---
@user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_enot")
async def create_enot_payment_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Создаю ссылку Enot.io...")
    data = await state.get_data()
    user_data = get_user(callback.from_user.id)
    plan = get_plan_by_id(data.get('plan_id'))
    if not plan:
        await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
        await state.clear()
        return
    
    base_price = Decimal(str(plan['price']))
    price_rub = base_price
    if user_data and user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
        try:
            discount_percentage = Decimal(get_setting("referral_discount") or "0")
        except Exception:
            discount_percentage = Decimal("0")
        if discount_percentage > 0:
            price_rub = base_price - (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
    
    final_price_decimal = price_rub
    try:
        final_price_from_state = data.get('final_price')
        if final_price_from_state is not None:
            final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
    except Exception:
        pass
    if final_price_decimal < Decimal('0'):
        final_price_decimal = Decimal('0.00')
        
    final_price_float = float(final_price_decimal)
    
    shop_id = (get_setting("enot_shop_id") or "").strip()
    secret_key = (get_setting("enot_secret_key") or "").strip()
    
    if not shop_id or not secret_key:
        await callback.message.edit_text("❌ Оплата через Enot.io временно недоступна.")
        await state.clear()
        return
        
    months = int(plan['months'])
    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "months": months,
        "price": final_price_float,
        "action": data.get('action'),
        "key_id": data.get('key_id'),
        "host_name": data.get('host_name'),
        "plan_id": data.get('plan_id'),
        "customer_email": data.get('customer_email'),
        "payment_method": "Enot.io",
        "promo_code": data.get('promo_code'),
        "promo_discount_percent": data.get('promo_discount_percent'),
        "promo_discount_amount": data.get('promo_discount_amount'),
    }
    
    try:
        create_pending_transaction(payment_id, user_id, final_price_float, metadata)
    except Exception as e:
        logger.warning(f"Enot: не удалось создать ожидающую транзакцию: {e}")
        
    pay_url = _build_enot_url(shop_id, secret_key, final_price_float, payment_id)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

@user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_enot")
async def topup_pay_enot(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Готовлю Enot.io...")
    data = await state.get_data()
    amount = Decimal(str(data.get('topup_amount', 0)))
    if amount <= 0:
        await callback.message.edit_text("❌ Некорректная сумма пополнения.")
        await state.clear()
        return
        
    shop_id = (get_setting("enot_shop_id") or "").strip()
    secret_key = (get_setting("enot_secret_key") or "").strip()
    
    if not shop_id or not secret_key:
        await callback.message.edit_text("❌ Оплата через Enot.io временно недоступна.")
        await state.clear()
        return

    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    metadata = {
        "payment_id": payment_id,
        "user_id": user_id,
        "price": float(amount),
        "action": "top_up",
        "payment_method": "Enot.io",
    }
    try:
        create_pending_transaction(payment_id, user_id, float(amount), metadata)
    except Exception as e:
        logger.warning(f"Enot topup: не удалось создать ожидающую транзакцию: {e}")
        
    pay_url = _build_enot_url(shop_id, secret_key, float(amount), payment_id)
    
    await state.clear()
    await callback.message.edit_text(
        "Нажмите на кнопку ниже для оплаты:",
        reply_markup=keyboards.create_payment_keyboard(pay_url)
    )

def _build_enot_url(shop_id: str, secret_key: str, amount: float, order_id: str) -> str:
    # Enot signature: md5(merchant_id:payment_amount:secret_word:order_id)
    amount_str = f"{amount:.2f}"
    
    raw = f"{shop_id}:{amount_str}:{secret_key}:{order_id}"
    import hashlib
    sign = hashlib.md5(raw.encode('utf-8')).hexdigest()
    
    # https://enot.io/pay/{shop_id}?oa={amount}&o={order_id}&s={sign}
    qs = urlencode({
        "oa": amount_str,
        "o": order_id,
        "s": sign
    })
    return f"https://enot.io/pay/{shop_id}?{qs}"
