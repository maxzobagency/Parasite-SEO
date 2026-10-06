FROM python:3.12-slim
WORKDIR /app
COPY parasite_tracker ./parasite_tracker
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "parasite_tracker.webapp"]
