FROM python:3.11-slim

WORKDIR /app

# تثبيت المتطلبات النظامية المطلوبة
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

RUN bash -c "$(curl -fsSL https://railway.com/install.sh)"
ENV PATH="/root/.railway/bin:${PATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]