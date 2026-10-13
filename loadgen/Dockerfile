FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
RUN pip install --no-cache-dir playwright==1.48.0
WORKDIR /app
COPY run.py .
ENTRYPOINT ["python", "run.py"]
CMD ["demo"]
