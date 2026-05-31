# 📈 CriptoCartera

App web (PWA) para llevar tu **cartera de cripto** y ver la **tendencia del mercado**
desde el iPhone (o cualquier móvil/ordenador). Funciona a pantalla completa como una app
y **no necesita claves API ni accede a tu cuenta de ningún exchange**.

## Qué hace

- 💼 **Resumen de cartera**: añades tus monedas y cantidades (ej. `0,5 BTC`) y ves el
  **valor total** y el de cada moneda, con su **% de cambio en 24 h**.
- 📈 **Gráficas históricas** por moneda (7 días / 30 días / 1 año).
- 😨🤑 **Índice Miedo/Codicia** del mercado (Fear & Greed) con indicador visual.
- 💱 Cambio entre **USD** y **EUR**.
- 📲 **Instalable** en la pantalla de inicio y con soporte **offline** (el esqueleto de
  la app se cachea; los precios siempre se piden frescos).

## De dónde salen los datos

Todo son **endpoints públicos de solo lectura**, sin autenticación:

- Precios y % 24 h, e histórico → **API pública de Binance** (`/api/v3/ticker/24hr`, `/api/v3/klines`).
- Índice Miedo/Codicia → **alternative.me** (`/fng`).

Los precios de mercado son prácticamente iguales en todos los exchanges, así que sirven
aunque tus monedas estén en **CoinEx** u otro. Solo tienes que escribir tú las cantidades.

## Privacidad y seguridad

- **No se usan claves API** ni contraseñas. La app **no** se conecta a tu cuenta de ningún
  exchange, así que no hay nada que pueda filtrarse.
- Tus monedas y cantidades se guardan **solo en tu dispositivo** (`localStorage`).

> ⚠️ Nunca compartas tu *API Secret* de un exchange con nadie (ni lo pegues en chats,
> capturas o código). Si alguna vez lo expones, **revócalo de inmediato** y crea uno nuevo.

## Cómo usarla en el iPhone

1. Publica/abre la app en una URL (ver más abajo).
2. Ábrela en **Safari**.
3. Pulsa **Compartir** → **«Añadir a pantalla de inicio»**.
4. Ábrela desde el icono: se verá a pantalla completa, como una app.

## Cómo publicarla (GitHub Pages)

Esta carpeta es 100% estática. Una opción sencilla:

1. En GitHub: **Settings → Pages** y publica desde la rama principal (`master`).
2. La app quedará en:
   `https://<tu-usuario>.github.io/upptime/crypto-tracker/`

O para probar en tu ordenador (con el repo descargado):

```bash
cd crypto-tracker
python3 -m http.server 8080
# abre http://localhost:8080
```

## Ampliación futura: sincronizar saldos desde CoinEx

CoinEx no permite leer el saldo directamente desde el navegador (por *CORS*). Para
sincronizar saldos automáticamente haría falta un **mini-servidor** que guarde una clave
de **solo lectura** y le hable a la app. No está incluido aquí por seguridad y simplicidad;
se puede añadir si se necesita.

## Archivos

| Archivo | Función |
|---|---|
| `index.html` | Estructura de la interfaz |
| `styles.css` | Estilos (tema oscuro, móvil) |
| `app.js` | Lógica: cartera, precios, gráficas, índice |
| `sw.js` | Service worker (offline / instalable) |
| `manifest.webmanifest` | Metadatos PWA |
| `icons/` | Iconos + generador (`generate_icons.py`) |
