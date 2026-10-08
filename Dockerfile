FROM node:24-alpine AS frontend
WORKDIR /build
COPY VERSION ./VERSION
COPY frontend/package.json frontend/pnpm-lock.yaml ./frontend/
RUN npm install --global pnpm@11.19.0
WORKDIR /build/frontend
ENV NODE_OPTIONS=--max-old-space-size=384
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY frontend/ ./
RUN pnpm run build

FROM mcr.microsoft.com/playwright/python:v1.62.0-noble
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && groupadd --gid 10001 assistant && useradd --uid 10001 --gid 10001 --no-create-home assistant
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/
COPY docs/ ./docs/
COPY frontend/ ./frontend/
COPY Dockerfile compose.yml README.md CHANGELOG.md .dockerignore .gitignore .gitattributes .project-id 使用说明.txt 部署教程.txt ./
COPY VERSION ./VERSION
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
