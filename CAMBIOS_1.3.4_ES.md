# Magic Pump 1.3.4 — recuperación vinculada a compra de sesión

- Cada compra manual/automática en primer plano y servicio guarda el orderId de Binance en su posición local.
- RECUPERAR DESDE BINANCE usa exclusivamente esos IDs locales para verificar órdenes BUY de Magic Pump; no consulta la compra más reciente genérica de Binance ni suma operaciones anteriores/externas.
- Recupera solo la cantidad ejecutada y el costo de esa orden guardada; compras anteriores o saldo adicional quedan fuera.
- Si no hay ID guardado, Binance no confirma la orden o el saldo libre no respalda la cantidad ejecutada, la moneda se omite en lugar de inventar datos.
- Posiciones compradas en versiones anteriores no tienen necesariamente el ID local; por seguridad no se pueden recuperar con atribución exacta por esta función.
- Fuente no compilada; instalar requiere APK nueva.
