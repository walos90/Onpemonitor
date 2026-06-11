# Monitor ONPE Desktop v35

Mejoras:
- Sistematiza las variaciones por bloques:
  1. Diferencia total entre candidatos
  2. Cambios de votos
  3. Cambios de porcentaje de votos
  4. Cambios de actas
  5. Otros cambios
- Agrega en la tabla principal:
  - Va adelante
  - Diferencia de votos
  - Diferencia en puntos %
- Guarda esos cambios en el historial acumulado.

Mantiene:
- Porcentajes de votos de agrupaciones políticas.
- Sin porcentajes de actas.
- Actas siempre como enteros.
- Historial acumulado de cambios.
- Formato uniforme de miles con coma.

## Ejecutar

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
streamlit run app.py
```
