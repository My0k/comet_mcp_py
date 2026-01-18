# Comet Auto (Python, Windows 10)

App simple en PyQt5 que:

1) pide un prompt,
2) lo escribe en Perplexity dentro del navegador Comet usando el puerto de *remote debugging* (CDP),
3) espera la respuesta y la imprime/visualiza.

## Requisitos

- Windows 10
- Python 3.10+ recomendado
- Perplexity Comet instalado

## Ejecutar

1) Doble click o ejecuta `run_comet_auto.bat`
2) En el primer inicio se pide configuración (ruta de `comet.exe`, puerto, etc.)
3) Se abre Comet y navega a `https://www.perplexity.ai/` (si hace falta)

Si Perplexity requiere login, inicia sesión manualmente en esa ventana de Comet y luego vuelve a la app.

## Notas de implementación

- La lógica reutiliza el patrón de `example_mcp_comet`: conexión por CDP, `Runtime.evaluate` para escribir/enviar y *smart completion* por estabilidad de respuesta y presencia/ausencia de botón “Stop”.

