FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Be resilient to flaky / rate-limited connections (this box scrapes over cellular).
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries

# System deps for a headless Chrome + Xvfb virtual display.
# NOTE: do NOT add libgconf-2-4 here - it was DROPPED in Debian 12 (bookworm), which the
# python:3.11-slim tag now resolves to. Referencing it makes apt-get exit 100 ("Unable to locate
# package"). Chrome's own .deb (installed below) pulls every runtime lib it needs (libnss3, etc.).
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    xvfb \
    xauth \
    x11-utils \
    libtcl8.6 \
    libtk8.6 \
    scrot \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome Stable from the official .deb. Installing the local .deb *via apt* lets it
# resolve Chrome's own dependencies from the standard Debian repos - no deprecated `apt-key` and no
# custom sources.list (that legacy approach also breaks on bookworm).
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project.
COPY . .

EXPOSE 8501

# Xvfb gives SeleniumBase a virtual display so it can physically move the mouse to clear the
# Cloudflare Turnstile checkbox. Chrome runs as root here (docker-compose sets user: root) together
# with --no-sandbox (passed in sources.py), which is required for root Chrome.
# CORS/XSRF are disabled because Streamlit sits behind the Tailscale Serve HTTPS proxy, whose origin
# (https://<name>.tail9128d0.ts.net) differs from the container's localhost. Without this the
# Streamlit websocket is rejected and the UI hangs on "Connecting..." (safe: tailnet-private).
CMD xvfb-run -a streamlit run app.py --server.address=0.0.0.0 --server.enableCORS=false --server.enableXsrfProtection=false
