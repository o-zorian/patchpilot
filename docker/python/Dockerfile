FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="PatchPilot Python Sandbox"
LABEL org.opencontainers.image.description="Networkless non-root Python test runtime for PatchPilot"

RUN ["python", "-m", "pip", "install", "--no-cache-dir", "pytest==8.4.2"]
RUN ["groupadd", "--gid", "10001", "patchpilot"]
RUN ["useradd", "--uid", "10001", "--gid", "10001", "--create-home", "patchpilot"]
RUN ["mkdir", "-p", "/workspace"]
RUN ["chown", "10001:10001", "/workspace"]

ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
USER 10001:10001

CMD ["python", "--version"]
