SCRIPTS_SRC := RawLoadrr/src/trackers
WORK_TRACKERS := work_data/trackers

.PHONY: build up down restart logs attach shell install prep check

# --- 1. Orquestación ---
# No hay `pull`: Singularity Lite no publica imagen. Se construye en tu máquina,
# que es justo lo que permite que funcione en ARM, en un NAS o en una tostadora.
build:
	docker compose build

up: prep
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

# --- 2. Persistencia ---
# Este bloque es el que evita el fallo más común del stack: docker-compose.yml
# monta ARCHIVOS sueltos, y si el archivo no existe en el host, Docker crea un
# DIRECTORIO en su lugar. El módulo revienta luego con IsADirectoryError, lejos
# de la causa. Crear los archivos antes del primer `up` lo evita del todo.
prep:
	@echo "🔧 Verificando integridad de archivos de persistencia..."
	@mkdir -p work_data/mass_editor
	@mkdir -p work_data/logs/csi_log
	@mkdir -p work_data/logs/rawloadrr
	@mkdir -p work_data/logs/mkverything
	@mkdir -p work_data/reports
	@mkdir -p work_data/cookies
	@mkdir -p work_data/qbit_backup
	@mkdir -p $(WORK_TRACKERS)
	@mkdir -p work_data/tmp/qbit_backup
	@if [ -z "$$(ls -A $(WORK_TRACKERS) 2>/dev/null)" ]; then \
		if [ -d "$(SCRIPTS_SRC)" ] && [ -n "$$(ls -A $(SCRIPTS_SRC) 2>/dev/null)" ]; then \
			echo "🧬 Infundiendo trackers desde el source local..."; \
			cp -rn $(SCRIPTS_SRC)/* $(WORK_TRACKERS)/; \
		else \
			echo "❌ No encuentro $(SCRIPTS_SRC). ¿Repo incompleto?"; \
			exit 1; \
		fi; \
	fi
	@mkdir -p config
	@if [ ! -f .env ] && [ -f .env.example ]; then cp .env.example .env; echo "🧩 Creado ./.env (compose) desde plantilla — EDÍTALO: MEDIA_ROOT"; fi
	@if [ ! -f config/.env ] && [ -f config/.env.example ]; then cp config/.env.example config/.env; echo "🧩 Creado config/.env (aplicación) desde plantilla"; fi
	@if [ ! -f config/singularity_config.py ] && [ -f config/singularity_config.py.example ]; then cp config/singularity_config.py.example config/singularity_config.py; echo "🧩 Creado config/singularity_config.py desde plantilla"; fi
	@if [ ! -f config/config.py ] && [ -f config/config.py.example ]; then cp config/config.py.example config/config.py; echo "🧩 Creado config/config.py desde plantilla"; fi
	@if [ ! -f config/mass_config.py ] && [ -f config/mass_config.py.example ]; then cp config/mass_config.py.example config/mass_config.py; echo "🧩 Creado config/mass_config.py desde plantilla"; fi
	@touch work_data/mass_editor/completados.txt
	@touch work_data/mass_editor/completados_img.txt
	@touch work_data/mass_editor/ids.txt
	@# Los JSON necesitan {} — un archivo de 0 bytes revienta json.load con
	@# JSONDecodeError, que es un fallo distinto y más confuso que "no existe".
	@for j in mapeo_maestro titulos_mapa mapeo_por_titulo; do \
		[ -s work_data/mass_editor/$$j.json ] || echo '{}' > work_data/mass_editor/$$j.json; \
	done
	@if [ ! -f config/.env ] || [ ! -f config/singularity_config.py ] || [ ! -f config/config.py ] || [ ! -f config/mass_config.py ]; then \
		echo "❌ Faltan archivos de config obligatorios en ./config"; \
		echo "   Esperados: .env, singularity_config.py, config.py, mass_config.py"; \
		echo "   Ejecuta: make install"; \
		exit 1; \
	fi
	@chmod -R 755 $(WORK_TRACKERS) 2>/dev/null || true
	@chmod -R 775 work_data/tmp 2>/dev/null || true
	@echo "✅ Estructura, plantillas y permisos listos."

# --- 3. Diagnóstico ---
# Comprueba que ningún archivo montado se haya convertido en directorio.
# Es el mismo chequeo que hace core/preflight.py dentro del contenedor.
check:
	@echo "🔍 Buscando archivos que Docker haya convertido en directorios..."
	@found=$$(find config work_data -type d \( -name '*.py' -o -name '*.txt' -o -name '*.json' -o -name '.env' \) 2>/dev/null); \
	if [ -n "$$found" ]; then \
		echo "❌ Estos deberían ser ARCHIVOS, no directorios:"; \
		echo "$$found" | sed 's/^/   /'; \
		echo ""; \
		echo "   Arréglalo con:  make down && rmdir $$(echo $$found | tr '\n' ' ') && make install && make up"; \
		exit 1; \
	else \
		echo "✅ Todo correcto."; \
	fi

# --- 4. Acceso ---
attach:
	docker exec -it singularity_core python3 singularity.py

shell:
	docker exec -it singularity_core /bin/bash

# --- 5. Instalación ---
install:
	@chmod +x final-user-install.sh
	@./final-user-install.sh
	@echo "Singularity Lite: estructura lista. Edita ./.env y config/.env, luego 'make up'."
