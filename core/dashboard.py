import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import json

from .status_manager import get_status, clear_stale_statuses, clear_all_statuses

BASE_DIR = Path(__file__).parent.parent
templates_dir = BASE_DIR / "core" / "templates"
os.makedirs(templates_dir, exist_ok=True)

templates = Jinja2Templates(directory=str(templates_dir))

TEMPLATE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Radar de Operaciones Singularity</title>
    <meta http-equiv="refresh" content="5">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
        .card { background: #1a1a1a; border-radius: 12px; padding: 20px; border-top: 4px solid #444; box-shadow: 0 4px 15px rgba(0,0,0,0.5); transition: transform 0.2s; cursor: pointer; user-select: none; }
        .card:hover { transform: translateY(-3px); }

        /* Module Colors */
        .card-core { border-top-color: #00bcd4; }
        .card-mkverything { border-top-color: #4caf50; }
        .card-rawloadrr { border-top-color: #e91e63; }
        .card-unit3d { border-top-color: #ffc107; }

        .module-name { font-size: 1.1em; font-weight: 800; margin-bottom: 15px; display: flex; align-items: center; justify-content: space-between; }
        .expand-hint { font-size: 0.6em; color: #555; font-weight: normal; margin-left: 8px; }
        .card.expanded .expand-hint::after { content: '▲ colapsar'; }
        .card:not(.expanded) .expand-hint::after { content: '▼ detalle'; }

        .label { color: #888; font-size: 0.85em; text-transform: uppercase; letter-spacing: 1px; margin-top: 10px; }
        .value { color: #fff; font-weight: 500; margin-bottom: 8px; }

        .progress-container { width: 100%; background-color: #333; border-radius: 10px; margin: 10px 0; height: 12px; overflow: hidden; }
        .progress-bar { height: 100%; background: linear-gradient(90deg, #00bcd4, #00acc1); border-radius: 10px; transition: width 0.5s ease-in-out; }

        .last-update { font-size: 0.7em; color: #666; text-align: right; margin-top: 15px; }
        .last-update.duration { color: #4caf50; }
        .last-update.active { color: #00bcd4; }

        h1 { color: #fff; text-align: center; font-weight: 900; letter-spacing: 4px; margin-bottom: 40px; text-shadow: 0 0 10px rgba(0,188,212,0.3); }
        .status-badge { font-size: 0.7em; padding: 3px 8px; border-radius: 4px; font-weight: bold; background: #333; text-transform: uppercase; }
        .status-online { color: #4caf50; border: 1px solid #4caf50; }
        .status-processing { color: #00bcd4; border: 1px solid #00bcd4; }
        .status-scanning { color: #00bcd4; border: 1px solid #00bcd4; }
        .status-error { color: #f44336; border: 1px solid #f44336; }
        .status-completed { color: #4caf50; border: 1px solid #4caf50; }
        .status-finished { color: #4caf50; border: 1px solid #4caf50; }
        .status-offline { color: #666; border: 1px solid #444; }

        /* Telemetry accordion */
        .telemetry-panel { display: none; margin-top: 15px; border-top: 1px dashed #555; padding-top: 10px; }
        .card.expanded .telemetry-panel { display: block; }

        .console-log { background: #050505; color: #00ff00; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.8em; border-left: 3px solid #00ff00; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; }
        .console-log.empty { color: #333; font-style: italic; }

        .metrics-grid { display: flex; justify-content: space-around; font-size: 0.8em; color: #aaa; margin-top: 12px; background: #111; border-radius: 8px; padding: 10px 0; }
        .metric-item { text-align: center; }
        .metric-label { color: #555; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; display: block; }
        .metric-value { color: #00bcd4; font-weight: bold; font-size: 1.1em; display: block; margin-top: 3px; font-family: monospace; }
        .metric-value.null { color: #444; }
        .telemetry-header { font-size: 0.7em; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
    </style>
</head>
<body>
    <h1>&#9889; RADAR DE OPERACIONES &#9889;</h1>
    <div class="grid">
        {% for key, data in status.items() %}
        {% set mod = (data.module if data.module else key) | lower %}
        <div class="card card-{{ mod }}" onclick="toggleCard(this)">
            <div class="module-name">
                <span>
                    {{ (data.module if data.module else key) | upper }}
                    {% if data.pid %}<span style="font-size: 0.6em; color: #888;">(PID: {{ data.pid }})</span>{% endif %}
                    <span class="expand-hint"></span>
                </span>
                <span class="status-badge status-{{ data.status | lower }}">{{ data.status }}</span>
            </div>

            <div class="label">Curro</div>
            <div class="value">{{ data.task }}</div>

            {% if data.progress is not none %}
            <div class="label">Avance</div>
            <div class="progress-container">
                <div class="progress-bar" style="width: {{ data.progress }}%;"></div>
            </div>
            <div style="font-weight: bold; color: #00bcd4; font-size: 0.9em;">{{ data.progress }}%</div>
            {% endif %}

            <div class="last-update {% if data.status in ['FINISHED', 'COMPLETED', 'ERROR'] and data.duration is defined %}duration{% elif data.start_time is defined %}active{% endif %}">
                {% if data.status in ['FINISHED', 'COMPLETED', 'ERROR'] and data.duration is defined %}
                    {% set total = data.duration | int %}
                    {% set h = total // 3600 %}
                    {% set m = (total % 3600) // 60 %}
                    {% set s = total % 60 %}
                    &#10003; Finiquitado en: {{ '%02d' | format(h) }}:{{ '%02d' | format(m) }}:{{ '%02d' | format(s) }}
                {% elif data.start_time is defined %}
                    &#9679; Dando caña: {{ (now - data.start_time) | int }}s
                {% else %}
                    Último reporte hace {{ (now - data.last_update) | int }}s
                {% endif %}
            </div>

            <!-- Telemetry accordion panel -->
            <div class="telemetry-panel">
                <div class="telemetry-header">&#128196; Bitácora de a Bordo</div>
                {% if data.details %}
                <div class="console-log">{{ data.details }}</div>
                {% else %}
                <div class="console-log empty">Sin novedades en el frente...</div>
                {% endif %}

                <div class="metrics-grid">
                    <div class="metric-item">
                        <span class="metric-label">CPU</span>
                        {% if data.metrics %}
                        <span class="metric-value">{{ data.metrics.cpu }}</span>
                        {% else %}
                        <span class="metric-value null">&#8212;</span>
                        {% endif %}
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">RAM</span>
                        {% if data.metrics %}
                        <span class="metric-value">{{ data.metrics.ram }}</span>
                        {% else %}
                        <span class="metric-value null">&#8212;</span>
                        {% endif %}
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">PID</span>
                        <span class="metric-value" style="color: #888;">{{ data.pid if data.pid else '&#8212;' }}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Últ. Check</span>
                        <span class="metric-value" style="color: #555; font-size: 0.85em;">hace {{ (now - data.last_update) | int }}s</span>
                    </div>
                </div>
            </div>
        </div>
        {% endfor %}

        {% if not status %}
        <div style="grid-column: 1/-1; text-align: center; color: #333; padding: 60px 0;">
            <div style="font-size: 3em;">&#9881;</div>
            <div style="font-size: 1.2em; margin-top: 10px; letter-spacing: 2px;">Aquí no hay ni Dios currando</div>
        </div>
        {% endif %}
    </div>

    <script>
    function toggleCard(card) {
        card.classList.toggle('expanded');
    }
    </script>
</body>
</html>
"""

index_path = templates_dir / "index.html"
with open(index_path, "w", encoding="utf-8") as f:
    f.write(TEMPLATE_HTML)


@asynccontextmanager
async def lifespan(app: FastAPI):
    clear_all_statuses()
    yield


app = FastAPI(title="Singularity Core Dashboard", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    status_data = get_status()
    return templates.TemplateResponse(request, "index.html", {
        "status": status_data,
        "now": time.time()
    })


@app.get("/api/status")
async def api_status():
    return get_status()


def run_dashboard():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="warning")


if __name__ == "__main__":
    # This is the container's command (`python3 -m core.dashboard`), so it is
    # the first thing that runs. Warn rather than exit: aborting here would put
    # the container into a restart loop and bury the diagnostic.
    from .preflight import warn as _preflight_warn
    _preflight_warn()
    run_dashboard()
