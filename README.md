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


## v47
- Se verificó que no haya botones de descarga CSV.
- El botón de descarga genera únicamente Excel .xlsx.


## v48
- Oculta el botón automático de descarga CSV de las tablas de Streamlit.
- Agrega botón propio para descargar la tabla principal en Excel .xlsx.


## v49
- Agrega botón “Descargar todo en Excel”.
- El Excel incluye hojas: Tabla principal, Resumen total, Cambios actuales, Historial, Actas originales, Candidatos originales y Meta.
- Mantiene botón opcional para descargar solo la tabla principal.


## v50
- Quita el botón “Descargar todo en Excel”.
- Cada sección tiene su propio botón de descarga Excel:
  tabla principal, resumen total, cambios actuales, historial, actas originales y candidatos originales.


## v51
- Restaura las secciones/cuadros visibles.
- Mantiene resumen total, actualizaciones detectadas, cambios importantes, historial, tabla principal y campos originales.
- Mantiene botones Excel por sección.
- Quita duplicado del resumen debajo de la tabla.


## v52 depurada
- Revisión general y checklist de requisitos.
- Autoactualización más robusta: mantiene estado activo tras errores, muestra última consulta exitosa y último error.
- Si falla una consulta automática, no borra la base ni detiene el monitor; reintenta en el siguiente intervalo.
- Oculta el botón CSV automático de Streamlit.
- Mantiene botones Excel por sección y todas las secciones visibles.


## v53
- Agrega tarjetas de hora:
  - Última actualización.
  - Último cambio detectado.
- Ambas se muestran en hora Perú.


## v54
- Corrige error: name 'display' is not defined.
- Revisión de compilación del archivo app.py.
