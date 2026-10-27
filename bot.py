        return
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await message.reply_text("Usa los botones del menú para comenzar.", reply_markup=main_keyboard())
        return

    if awaiting.startswith("setting:"):
        key = awaiting.split(":", 1)[1]
        try:
            value = validate_numeric_setting(key, text)
            set_setting(key, value)
            context.user_data["awaiting"] = None
            await message.reply_text(f"✅ Parámetro guardado: {key} = {value}", reply_markup=config_keyboard())
        except ValueError as exc:
            await message.reply_text(str(exc))
        return

    draft = context.user_data.get("draft")
    if not draft:
        context.user_data["awaiting"] = None
        await message.reply_text("No hay una operación en preparación. Vuelve al menú.", reply_markup=main_keyboard())
        return

    if awaiting == "symbol":
        symbol = re.sub(r"[^A-Za-z0-9]", "", text).upper()
        if not re.fullmatch(r"[A-Z0-9]{4,30}", symbol):
            await message.reply_text("Símbolo inválido. Envía algo como BTCUSDT, ETHUSDT o SOLUSDT.")
            return
        try:
            info = await asyncio.to_thread(BINANCE.symbol_info, symbol)
        except Exception as exc:
            await message.reply_text(f"No puedo usar ese símbolo: {exc}")
            return
        draft["symbol"] = symbol
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = "entry_price"
        await message.reply_text(f"Contrato validado: {symbol} (perpetuo USDT).\nEnvía el precio de entrada objetivo/de referencia.")
        return

    if awaiting == "entry_price":
        try:
            raw = text.replace(",", ".")
            value = D(raw)
            if not value.is_finite() or value <= 0:
                raise ValueError
        except Exception:
            await message.reply_text("Precio inválido. Envía un número positivo, por ejemplo 0.2072.")
            return
        draft["entry_price_text"] = raw
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = "qty"
        await message.reply_text("Ahora envía la cantidad inicial de monedas/contratos, por ejemplo 50 o 0.01.")
        return

    if awaiting == "qty":
        try:
            raw = text.replace(",", ".")
            value = D(raw)
            if not value.is_finite() or value <= 0:
                raise ValueError
        except Exception:
            await message.reply_text("Cantidad inválida. Envía un número positivo.")
            return
        draft["qty_text"] = raw
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 LÍMITE al precio escrito", callback_data="mode:limit")],
            [InlineKeyboardButton("⚡ LÍMITE RÁPIDO ±0.002 del mercado", callback_data="mode:quick")],
            [InlineKeyboardButton("📈 MERCADO", callback_data="mode:market")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_draft")],
        ])
        await message.reply_text(
            "Elige cómo entrar:\n\n"
            "• LÍMITE: deja la orden en el precio que escribiste; si está lejos puede esperar.\n"
            "• LÍMITE RÁPIDO: usa el precio de mercado ±0.002 USDT, no el precio de referencia escrito.\n"
            "• MERCADO: entra al precio disponible; la escalera se recalcula con el precio real ejecutado.",
            reply_markup=keyboard,
        )
        return


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth_ok(update):
        await update.effective_message.reply_text("No autorizado.")
        return
    context.user_data["awaiting"] = None
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("Borrador cancelado. Las operaciones ya enviadas a Binance no se cancelan con este comando.", reply_markup=main_keyboard())


async def post_init(application: Application) -> None:
    global TRADE_LOCK
    TRADE_LOCK = asyncio.Lock()
    application.job_queue.run_repeating(monitor_job, interval=MONITOR_INTERVAL, first=2, name="monitor-futures")
    try:
        await application.bot.set_my_commands([
            ("start", "Abrir menú del bot"),
            ("status", "Ver operación gestionada"),
            ("id", "Mostrar tu ID de Telegram"),
            ("cancelar", "Cancelar un borrador no enviado"),
        ])
    except Exception:
        log.exception("No pude registrar comandos de Telegram")
    log.info("Bot inicializado. Entorno Binance=%s; intervalo monitor=%.1fs", ENV_NAME, MONITOR_INTERVAL)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Crea un bot con BotFather y pon el token en .env.")
    if not BINANCE_TESTNET and ENABLE_LIVE_TRADING:
        log.warning("ATENCIÓN: el bot podrá enviar órdenes REALES. Asegúrate de estar usando una API key limitada y revisada.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
