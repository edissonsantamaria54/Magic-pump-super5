# Magic Pump 1.3.1 — solo modo normal

Esta variante conserva únicamente las compras normales. Se retiraron el detector, los parámetros, las etiquetas y la gestión de salidas dinámicas del modo explosivo.

- La señal normal y sus filtros se conservan.
- Se mantienen SL/TP configurados, monto, filtros, compras normales, seguimiento de posiciones, salidas por tendencia/tiempo y reconciliación de residuos.
- Después de 120 minutos, la salida normal por objetivo pasa a +0,75%.
- Posiciones ya abiertas se recuperan con sus SL/TP porcentuales guardados y se gestionan con la lógica normal; no se cierran al actualizar.
- El SL sigue siendo vigilancia local, no un stop alojado en Binance.
- Fuente no compilada ni probada en APK/teléfono.


## Recuperación segura en 1.3.2

La recuperación reconstruye el costo promedio a partir de hasta 1000 fills Spot disponibles, incluyendo comisiones cuando afectan la moneda base o USDT, y comprueba que la cantidad neta coincide con el saldo libre. Si el historial no cuadra (por ejemplo, por historial insuficiente, transferencias o depósitos), omite la posición en vez de inventar un costo. El saldo omitido sigue en Binance; no se vende ni se mueve.


## Recuperación de la última compra en 1.3.3

La recuperación usa solo el pedido BUY más reciente por moneda y agrupa sus fills por orderId. No promedia ni suma pedidos de compra anteriores. El registro recuperado se limita a la cantidad y al costo de ese pedido; cualquier saldo adicional queda sin incluir. Si el saldo libre actual no cubre ese lote o el pedido no aparece en el historial disponible, la posición se omite y no se inventa una cantidad/costo.


## Recuperación vinculada a sesión (1.3.4)

Las compras nuevas guardan el orderId de Binance en la posición local. RECUPERAR DESDE BINANCE verifica únicamente esos orderId; no deduce el costo a partir de compras genéricas recientes. Las posiciones heredadas sin orderId local se omiten por seguridad y deben mantenerse desde el registro local existente o verificarse manualmente en Binance.
