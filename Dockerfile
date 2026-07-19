FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY llmarr ./llmarr
RUN pip install --no-cache-dir .

# Persist config + library state on a mounted volume.
ENV LLMARR_CONFIG=/config/config.yaml \
    LLMARR_DB=/config/llmarr.db \
    LLMARR_TRANSPORT=streamable-http \
    LLMARR_HOST=0.0.0.0 \
    LLMARR_PORT=8000
VOLUME ["/config"]
EXPOSE 8000

CMD ["llmarr"]
