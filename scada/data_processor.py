import requests
import pandas as pd
import json
import re
from dateutil.relativedelta import relativedelta
import plotly.express as px
import pvlib

BASE_URL = "https://api.licor.cloud"

def process_scada_data(token, overall_start="2025-01-01 00:00:00", overall_end="2025-12-31 23:59:59", targets=["solar radiation", "temperature", "wind speed"]):
    """
    Process SCADA data from API, filter variables, apply quality control, and generate plots.

    Returns:
        df_plot: DataFrame with processed data
        plot_htmls: dict with HTML strings for plots
    """
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    # 1. OBTENER SERIAL
    devices = session.get(f"{BASE_URL}/v2/devices").json()

    def find_serial(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ["deviceserialnumber", "serial", "sn"]:
                    return v
                r = find_serial(v)
                if r: return r
        elif isinstance(obj, list):
            for v in obj:
                r = find_serial(v)
                if r: return r
        return None

    SERIAL = find_serial(devices)
    if not SERIAL:
        raise ValueError("No se pudo encontrar el SERIAL del dispositivo")

    # 2. FUNCIONES CLAVE
    def normalize(text):
        """Normaliza texto para matching robusto"""
        if not isinstance(text, str):
            return ""
        text = text.lower()
        text = re.sub(r'[^a-z0-9 ]', ' ', text)
        return text.strip()

    def find_variable_column(df):
        """Detecta automáticamente la columna donde está el nombre de la variable"""
        for col in df.columns:
            sample = df[col].astype(str).head(20).str.lower()
            if any(any(t in v for t in targets) for v in sample):
                return col
        return None

    def filter_variables(df):
        """Filtra solo las variables objetivo"""
        col = find_variable_column(df)

        if col is None:
            print("⚠️ No se encontró columna de variables automáticamente")
            return pd.DataFrame()

        df["_var_norm"] = df[col].apply(normalize)

        mask = df["_var_norm"].apply(
            lambda x: any(t in x for t in targets)
        )

        df_filtered = df[mask].copy()

        # Clasificación limpia
        def classify(x):
            for t in targets:
                if t in x:
                    return t.title()
            return "Other"

        df_filtered["variable"] = df_filtered["_var_norm"].apply(classify)

        return df_filtered

    # 3. DESCARGA POR SEMANA
    overall_start_dt = pd.to_datetime(overall_start)
    overall_end_dt = pd.to_datetime(overall_end)

    df_all = []
    current_start = overall_start_dt

    while current_start <= overall_end_dt:
        current_end = current_start + relativedelta(weeks=1) - relativedelta(seconds=1)

        if current_end > overall_end_dt:
            current_end = overall_end_dt

        params = {
            "loggers": SERIAL,
            "start_date_time": current_start.strftime('%Y-%m-%d %H:%M:%S'),
            "end_date_time": current_end.strftime('%Y-%m-%d %H:%M:%S')
        }

        try:
            r = session.get(f"{BASE_URL}/v1/data", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Error descargando {current_start} - {current_end}: {e}")
            current_start += relativedelta(weeks=1)
            continue

        rows = data if isinstance(data, list) else data.get("data", [])

        if rows:
            df_week = pd.json_normalize(rows)
            df_all.append(df_week)

        current_start += relativedelta(weeks=1)

    if not df_all:
        raise ValueError("No se encontraron datos en todo el período")

    df = pd.concat(df_all, ignore_index=True)

    # 4. FILTRAR VARIABLES OBJETIVO
    df_filtered = filter_variables(df)

    if df_filtered.empty:
        raise ValueError("No se encontraron variables objetivo")

    # Pivot
    df_filtered['timestamp'] = pd.to_datetime(df_filtered['timestamp'])
    df_selected_variables = df_filtered.pivot_table(
        index='timestamp',
        columns='variable',
        values='value',
        aggfunc='mean'
    ).reset_index()
    df_selected_variables.columns.name = None

    # Quality control
    col_time = 'timestamp'
    col_ghi = 'Solar Radiation'
    col_temp = 'Temperature'
    col_wind = 'Wind Speed'

    df_pivot = df_selected_variables.copy()

    if col_time in df_pivot.columns:
        df_pivot[col_time] = pd.to_datetime(df_pivot[col_time])
        df_pivot = df_pivot.set_index(col_time)
    else:
        df_pivot.index = pd.to_datetime(df_pivot.index)

    # FLAGS DE CALIDAD
    df_pivot["flag_ghi_neg"] = df_pivot[col_ghi] < 0
    df_pivot["flag_ghi_excesivo"] = df_pivot[col_ghi] > 1367
    df_pivot["flag_temp_extrema"] = (df_pivot[col_temp] < -20) | (df_pivot[col_temp] > 60)
    df_pivot["flag_wind_neg"] = df_pivot[col_wind] < 0

    # ELIMINAR DATOS INVALIDOS
    df_pivot = df_pivot[~df_pivot["flag_ghi_neg"]]
    df_pivot = df_pivot[~df_pivot["flag_wind_neg"]]

    # ALTURA SOLAR
    solpos = pvlib.solarposition.get_solarposition(
        time=df_pivot.index,
        latitude=-33.34684265811497,
        longitude=-56.52111191366177
    )

    df_pivot["solar_elevation"] = solpos["apparent_elevation"]

    # CORRECCIÓN GHI NOCTURNO
    mask_noche = (df_pivot["solar_elevation"] <= 0) & (df_pivot[col_ghi] > 0)
    df_pivot.loc[mask_noche, col_ghi] = 0
    df_pivot["flag_noche_ghi"] = mask_noche

    # GHI MUY BAJO → 0
    df_pivot.loc[df_pivot[col_ghi] < 1, col_ghi] = 0

    # volver a columna
    df_pivot = df_pivot.reset_index()

    # Generar tabla final
    df_plot = df_pivot[[col_time, col_ghi, col_wind, col_temp]].copy()
    df_plot[col_time] = pd.to_datetime(df_plot[col_time])
    df_plot = df_plot.sort_values(col_time)
    df_plot.columns = ['Tiempo (5 min)', 'GHI (W/m²)', 'Viento (m/s)', 'Temperatura (°C)']

    # Generar plots
    plot_htmls = {}

    # GHI
    fig_ghi = px.line(
        df_pivot,
        x=col_time,
        y=col_ghi,
        title="GHI (Irradiancia)",
        labels={col_time: "Tiempo", col_ghi: "W/m²"}
    )
    plot_htmls['ghi'] = fig_ghi.to_html(full_html=False)

    # Viento
    fig_wind = px.line(
        df_pivot,
        x=col_time,
        y=col_wind,
        title="Velocidad del viento (2,2m)",
        labels={col_time: "Tiempo", col_wind: "m/s"}
    )
    plot_htmls['wind'] = fig_wind.to_html(full_html=False)

    # Temperatura
    fig_temp = px.line(
        df_pivot,
        x=col_time,
        y=col_temp,
        title="Temperatura ambiente",
        labels={col_time: "Tiempo", col_temp: "°C"}
    )
    plot_htmls['temp'] = fig_temp.to_html(full_html=False)

    return df_plot, plot_htmls