#!/usr/bin/env bash
set -euo pipefail

REPO_SSH="git@github.com:v0jt4s13/learning_platform.git"
APP_USER="learningcenter"

APP_DIR="/opt/apps/app_learning_center"
REPO_DIR="${APP_DIR}/app"
VENV_DIR="${APP_DIR}/venv"

PYTHON_BIN="python3.11"
WSGI_APP="app:create_app()"
SERVICE_NAME="learningcenter"
GUNICORN_BIND="127.0.0.1:8003"
DOMAIN="ops02.jdblayer.com"
URL_PREFIX="/learning-center"

SSH_DIR="${APP_DIR}/.ssh"
DEPLOY_KEY="${SSH_DIR}/id_ed25519"
LOG_DIR="/var/log/app_learning_center"
NGINX_SITE="/etc/nginx/sites-available/moderacja.conf"

### === Sprawdzenie uprawnień ===
if [[ $EUID -ne 0 ]]; then
  echo "Uruchom skrypt jako root (sudo)." >&2
  exit 1
fi

### === Pakiety systemowe ===
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  ${PYTHON_BIN} python3-venv python3-pip \
  git nginx ca-certificates openssh-client

echo "=== Użytkownik i katalogi ==="
if id -u "${APP_USER}" >/dev/null 2>&1; then
  CUR_HOME="$(getent passwd "${APP_USER}" | cut -d: -f6 || true)"
  if [[ "${CUR_HOME}" != "${APP_DIR}" ]]; then
    if [[ ! -e "${APP_DIR}" || -z "$(ls -A "${APP_DIR}" 2>/dev/null)" ]]; then
      usermod -d "${APP_DIR}" -m "${APP_USER}"
    else
      usermod -d "${APP_DIR}" "${APP_USER}"
    fi
  fi
else
  adduser --system --group --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_USER}" "${APP_DIR}" "${REPO_DIR}"
install -d -o "${APP_USER}" -g www-data "${LOG_DIR}"
chmod 750 "${LOG_DIR}"

echo "=== SSH ==="
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "${SSH_DIR}"
if [[ ! -f "${DEPLOY_KEY}" ]]; then
  sudo -u "${APP_USER}" ssh-keygen -t ed25519 -C "deploy-${SERVICE_NAME}@$(hostname -f)" -f "${DEPLOY_KEY}" -N ""
  echo ">>> Dodaj do GitHuba (Deploy key, read-only):"
  cat "${DEPLOY_KEY}.pub"
fi
chmod 600 "${DEPLOY_KEY}"; chown "${APP_USER}:${APP_USER}" "${DEPLOY_KEY}"
chmod 644 "${DEPLOY_KEY}.pub"; chown "${APP_USER}:${APP_USER}" "${DEPLOY_KEY}.pub"

ssh-keyscan -H github.com >> "${SSH_DIR}/known_hosts" 2>/dev/null || true
chown "${APP_USER}:${APP_USER}" "${SSH_DIR}/known_hosts" 2>/dev/null || true
chmod 644 "${SSH_DIR}/known_hosts" 2>/dev/null || true
mkdir -p ~root/.ssh && chmod 700 ~root/.ssh
ssh-keyscan -H github.com >> ~root/.ssh/known_hosts 2>/dev/null || true
chmod 644 ~root/.ssh/known_hosts

if [[ ! -f "${SSH_DIR}/config" ]] || ! grep -q "IdentityFile ${DEPLOY_KEY}" "${SSH_DIR}/config"; then
  cat > "${SSH_DIR}/config" <<EOF
Host github.com
  HostName github.com
  User git
  IdentityFile ${DEPLOY_KEY}
  IdentitiesOnly yes
EOF
  chown "${APP_USER}:${APP_USER}" "${SSH_DIR}/config"
  chmod 600 "${SSH_DIR}/config"
fi

echo "=== Repozytorium ==="
if [[ -d "${REPO_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" -H git -C "${REPO_DIR}" fetch --all --prune
  BRANCH="$(sudo -u "${APP_USER}" -H git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD || echo main)"
  sudo -u "${APP_USER}" -H git -C "${REPO_DIR}" reset --hard "origin/${BRANCH}" || sudo -u "${APP_USER}" -H git -C "${REPO_DIR}" checkout -B main origin/main
elif [[ -z "$(ls -A "${REPO_DIR}")" ]]; then
  sudo -u "${APP_USER}" -H git clone --depth 1 "${REPO_SSH}" "${REPO_DIR}"
else
  echo "Katalog ${REPO_DIR} istnieje i nie jest repozytorium git. Przenieś jego zawartość lub wyczyść i uruchom ponownie." >&2
  exit 1
fi

echo "=== Venv i zależności ==="
if [[ ! -d "${VENV_DIR}" ]]; then
  sudo -u "${APP_USER}" ${PYTHON_BIN} -m venv "${VENV_DIR}"
fi
sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
  sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -m pip install -r "${REPO_DIR}/requirements.txt"
else
  sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -m pip install flask gunicorn sqlalchemy alembic python-dotenv boto3 azure-cognitiveservices-speech
fi

echo "=== systemd ==="
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Gunicorn - Flask app (${SERVICE_NAME})
After=network.target

[Service]
User=${APP_USER}
Group=www-data
WorkingDirectory=${REPO_DIR}
Environment=PATH=${VENV_DIR}/bin
Environment=VIRTUAL_ENV=${VENV_DIR}
Environment=SCRIPT_NAME=${URL_PREFIX}
Environment=FORWARDED_ALLOW_IPS=*

ExecStart=${VENV_DIR}/bin/python -m gunicorn --workers 3 --bind ${GUNICORN_BIND} ${WSGI_APP}

Restart=always
RestartSec=5
TimeoutStartSec=300

StandardOutput=append:${LOG_DIR}/stdout.log
StandardError=append:${LOG_DIR}/stderr.log

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "=== Nginx (${URL_PREFIX}) ==="
[[ -f "${NGINX_SITE}" ]] || { echo "Brak ${NGINX_SITE}"; exit 1; }
[[ ! -f "${NGINX_SITE}.bak" ]] && cp "${NGINX_SITE}" "${NGINX_SITE}.bak"

if ! grep -q "location ${URL_PREFIX}/" "${NGINX_SITE}"; then
  awk -v bind="${GUNICORN_BIND}" -v prefix="${URL_PREFIX}" '
    BEGIN{ins=0}
    {print}
    /server_name/ && /ops02\.jdblayer\.com/ && ins==0 {
      print "    # --- learning center path prefix ---";
      print "    location = " prefix " { return 301 " prefix "/; }";
      print "    location ^~ " prefix "/ {";
      print "        proxy_pass http://" bind "/;";
      print "        proxy_set_header Host $host;";
      print "        proxy_set_header X-Real-IP $remote_addr;";
      print "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;";
      print "        proxy_set_header X-Forwarded-Proto $scheme;";
      print "        proxy_set_header X-Script-Name " prefix ";";
      print "        proxy_set_header X-Forwarded-Prefix " prefix ";";
      print "        proxy_redirect off;";
      print "        proxy_read_timeout 300; proxy_connect_timeout 60; proxy_send_timeout 300;";
      print "    }";
      print "    # --- end learning center ---";
      ins=1
    }
  ' "${NGINX_SITE}" > "${NGINX_SITE}.tmp" && mv "${NGINX_SITE}.tmp" "${NGINX_SITE}"
fi

nginx -t
systemctl reload nginx || systemctl restart nginx

echo
echo "=== STATUS ==="
systemctl --no-pager --full status "${SERVICE_NAME}" || true
systemctl --no-pager --full status nginx || true

cat <<INFO

Gotowe!

URL:            http://${DOMAIN}${URL_PREFIX}/
Repo:           ${REPO_DIR}
Venv:           ${VENV_DIR}
SSH:            ${SSH_DIR}   (użytkownik ${APP_USER})
Logi:           ${LOG_DIR}
Unit:           ${SERVICE_PATH}
Nginx:          ${NGINX_SITE} (backup: ${NGINX_SITE}.bak)

INFO
