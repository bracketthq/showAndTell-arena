# ShowAndTell ONLYOFFICE storage connector.
#
# Document Server is an editor, not a file store. This serves the small
# integration surface it needs (signed document URLs, editor config, save
# callback) plus an admin API so a remote fixture host can register and clear
# workbooks. It runs beside Document Server so both are reachable from the
# operator's browser and from each other.
#
# Build context is the repository root, so this folder's own modules and the
# workbook writer they use can both be copied in.
FROM python:3.13-slim

# The connector itself needs only these; the workbook writer beside it is
# standard-library only.
RUN pip install --no-cache-dir \
      "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27"

WORKDIR /app
# Copy only the canonical package spine and this connector's modules.
COPY showAndTell/__init__.py /app/showAndTell/__init__.py
COPY showAndTell/applications/__init__.py /app/showAndTell/applications/__init__.py
COPY showAndTell/applications/onlyoffice/__init__.py /app/showAndTell/applications/onlyoffice/__init__.py
COPY showAndTell/applications/onlyoffice/connector.py /app/showAndTell/applications/onlyoffice/connector.py
COPY showAndTell/applications/onlyoffice/xlsx.py /app/showAndTell/applications/onlyoffice/xlsx.py
COPY showAndTell/applications/onlyoffice/connector_main.py /app/showAndTell/applications/onlyoffice/connector_main.py

ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN find /app -name __pycache__ -type d -prune -exec rm -rf {} +

EXPOSE 8000
CMD ["python", "-m", "showAndTell.applications.onlyoffice.connector_main"]
