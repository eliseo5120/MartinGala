# MartinGala
bot con calculadora del gafas

Bot de Telegram para gestionar una estrategia LONG/SHORT en Binance USDⓈ-M Futures. El proyecto usa un solo script (bot.py) y lee la configuración desde .env. La calculadora está integrada; no hace falta instalar un archivo aparte.
Importante: empieza en Binance Futures Demo. Las órdenes reales solo se habilitan configurando BINANCE_TESTNET=false y ENABLE_LIVE_TRADING=true. No hagas ese cambio hasta haber probado todo en Demo. El bot opera con futuros y puedes perder dinero; el stop calculado no garantiza una pérdida máxima exacta en caso de deslizamiento, gaps, liquidación, comisiones o fallos de conexión.

Archivos
- bot.py: script del bot.
- .env: token de Telegram, ID autorizado, claves API y ajustes.
- requirements.txt: dependencias de Python.
- elgafas_bot_state.json: archivo de estado que el bot crea automáticamente.
- elgafas_bot_state.json.bak: copia de seguridad del estado que el bot crea automáticamente.
- bot.log: puedes redirigir aquí la salida al iniciar el bot; el propio script no necesita este archivo para funcionar.
No borres los archivos JSON de estado mientras haya una operación activa, porque contienen los datos que usa el bot para intentar reanudarla tras un reinicio. No compartas tu .env ni las claves de API.
1. Preparar Termux
Instala Termux desde una fuente confiable y abre la aplicación. Ejecuta:
pkg update && pkg upgrade
pkg install python nano
termux-setup-storage
Crea una carpeta de trabajo y coloca dentro bot.py, .env, README.md y requirements.txt. Por ejemplo, si los archivos están en Descargas:
mkdir -p ~/ElGafas
cp ~/storage/downloads/bot.py ~/ElGafas/
cp ~/storage/downloads/.env ~/ElGafas/
cp ~/storage/downloads/requirements.txt ~/ElGafas/
cp ~/storage/downloads/README.md ~/ElGafas/
cd ~/ElGafas
Si tus archivos están en otra carpeta, ajusta la ruta de los comandos cp. Si ya los tienes en ~/ElGafas, no hace falta copiarlos.
2. Instalar dependencias
Desde la carpeta donde está bot.py, ejecuta:
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
3. Configurar .env
Abre el archivo:
nano .env
Completa estas variables:
- TELEGRAM_BOT_TOKEN: token creado mediante @BotFather.
- ALLOWED_TELEGRAM_USER_ID: tu ID numérico de Telegram.
- BINANCE_API_KEY y BINANCE_API_SECRET: claves de Binance Futures Demo.
- BINANCE_TESTNET=true y ENABLE_LIVE_TRADING=false: mantén estos valores para probar sin operar con fondos reales.
Guarda en nano con Ctrl+O, pulsa Enter y sal con Ctrl+X.

Para obtener tu ID, puedes poner un valor temporal en ALLOWED_TELEGRAM_USER_ID, iniciar el bot y enviar /id en el chat. Copia el ID que responde el bot en .env, detén el proceso con Ctrl+C y vuelve a iniciarlo. El bot está restringido al ID configurado.
4. Iniciar el bot
cd ~/ElGafas
python bot.py
Mantén Termux abierto mientras pruebas. En Telegram envía /start, configura los parámetros y prepara una operación Demo. Revisa todos los precios y cantidades antes de confirmar la orden.

Comandos disponibles:
- /start: abre el menú.
- /id: muestra tu ID numérico de Telegram.
- /status: consulta el estado de la operación administrada por el bot.
- /cancelar: cancela un borrador que todavía no se ha enviado; no cierra una operación que ya esté activa.
5. Persistencia y reconexión
El bot guarda los parámetros y el estado de la estrategia en elgafas_bot_state.json y mantiene una copia de respaldo .bak junto a bot.py. Si Termux o el teléfono se reinicia, inicia el script otra vez desde la misma carpeta: intentará reconciliar el estado guardado con Binance y no debería volver a enviar automáticamente la entrada inicial.

La persistencia local no puede garantizar recuperación perfecta ante todo corte. Después de cualquier interrupción, comprueba /status y verifica la posición y las órdenes directamente en Binance antes de tomar nuevas decisiones. Si el bot informa de un fallo crítico, revisa Binance inmediatamente.

Para mantener el proceso más estable mientras pruebas, puedes usar:
termux-wake-lock
Android puede suspender o cerrar aplicaciones por ahorro de batería. Excluye Termux de la optimización de batería si tu dispositivo lo permite. Para arrancar automáticamente después de reiniciar Android puedes instalar Termux:Boot desde la misma fuente que Termux y configurar un script de inicio; el dispositivo debe arrancar y Termux:Boot debe poder ejecutarse.
Restricciones y seguridad
- Esta versión administra una operación de estrategia a la vez y requiere el modo One-way de Binance Futures; no uses Hedge Mode.
- No concede permisos de retiro a las claves API. Restringe la clave por IP cuando sea posible.
- La entrada límite al precio indicado puede quedar esperando. La modalidad de límite rápido usa el desplazamiento definido por FAST_LIMIT_OFFSET; la modalidad mercado puede ejecutarse a un precio distinto del solicitado.
- Binance aplica tickSize, stepSize y otros filtros del contrato; puede haber diferencias entre el cálculo teórico y las órdenes aceptadas.
- El trailing depende de que el bot esté activo para detectar la activación y coordinar las órdenes. No apagues el bot durante una operación sin comprobar las protecciones en Binance.
- Prueba primero en Demo con operaciones pequeñas y supervisadas. No se garantiza rentabilidad ni ejecución exacta del riesgo configurado.
