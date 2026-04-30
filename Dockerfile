FROM python:3.11-slim

# System dependencies: Tesseract + Hebrew pack, Poppler, OpenCV runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-heb \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the files needed for installation
COPY pyproject.toml ./
COPY src/ ./src/

# Trust PyPI hosts (needed in environments with SSL-intercepting proxies)
RUN pip config set global.trusted-host "pypi.org files.pythonhosted.org pypi.python.org"

# Install the Python package and all dependencies
RUN pip install --no-cache-dir .

ENTRYPOINT ["pdf-vary"]
