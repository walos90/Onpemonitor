# Dashboard Electoral ONPE - versión final

Incluye:
- interfaz renovada con título, tarjetas y diseño más amigable;
- hora en Perú usando America/Lima;
- resumen total corregido con la fila Nivel general / Ámbito general;
- lectura rápida de cada actualización;
- cambios importantes separados del detalle técnico;
- historial descargable en Excel .xlsx con filtros y columnas ordenadas;
- mantiene CSV como descarga técnica.

Subir a GitHub solo estos archivos: app.py, requirements.txt, packages.txt y README.md.


## v40
Correcciones:
- Descarga principal en Excel real .xlsx.
- Sección Descargas más clara.
- Respaldo de base actual en Excel.
- Respaldo JSON para restaurar la base si Streamlit la pierde al actualizar.
- Restauración de base desde JSON.


## v41
- Se eliminó la sección nueva de Descargas/Respaldo.
- El botón de descarga existente ahora descarga directamente Excel .xlsx.
- El CSV ya no aparece como descarga principal.


## v42
- Interfaz minimalista.
- Título más simple.
- Tarjetas y recuadros más limpios.
- Menos texto visual y colores más sobrios.


## v43
- La app ya no se detiene si ONPE devuelve HTML al listar departamentos o extranjero.
- Si falla esa lista, continúa mostrando el total general y lo disponible.


## v44
- Mantiene la consulta de departamentos.
- Hace más robusta la consulta API: URL absoluta, sesión ONPE, reintentos y recarga si ONPE devuelve HTML.
- Solo usa respaldo si ONPE falla después de varios intentos.


## v45
- Inicializa la sesión en /main/resumen, que es la ruta real del portal ONPE.
- Reintenta las llamadas API después de cargar correctamente el frontend.
- Mantiene interfaz minimalista y descarga Excel.


## v46
- Corrige inicialización de sesión ONPE para evitar "SessionInfo before it was initialized".
- Abre primero el dominio principal y luego el resumen.
- Usa domcontentloaded en vez de networkidle para no quedar atrapado en errores internos del frontend ONPE.
