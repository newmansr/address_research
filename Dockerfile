FROM python:3.11-slim

# Install system dependencies, Chromium, and Xvfb (Virtual Display for Turnstile bypassing)
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    xvfb \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Expose the Streamlit port
EXPOSE 8501

# Run Xvfb (fake display server) and Streamlit simultaneously
CMD xvfb-run -a streamlit run app.py --server.address=0.0.0.0

