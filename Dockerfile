FROM python:3.11-slim

# Install system dependencies
RUN sed -i 's/deb.debian.org/ftp.us.debian.org/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    xvfb \
    x11-utils \
    libgconf-2-4 \
    libnss3 \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome Stable
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user specifically for Chrome
RUN useradd -m appuser

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Change ownership of the app directory so the non-root user can write to SQLite and Cache
RUN chown -R appuser:appuser /app

# Switch to the non-root user!
USER appuser

EXPOSE 8501

CMD xvfb-run -a streamlit run app.py --server.address=0.0.0.0

