FROM golang:1.26-bookworm

LABEL org.opencontainers.image.title="PatchPilot Go Sandbox"
LABEL org.opencontainers.image.description="Networkless non-root Go test runtime for PatchPilot"

RUN ["groupadd", "--gid", "10001", "patchpilot"]
RUN ["useradd", "--uid", "10001", "--gid", "10001", "--create-home", "patchpilot"]
RUN ["mkdir", "-p", "/workspace"]
RUN ["chown", "10001:10001", "/workspace"]

ENV HOME=/tmp \
    TMPDIR=/tmp \
    GOCACHE=/tmp/go-cache \
    GOMODCACHE=/tmp/go-mod-cache \
    GONOSUMDB=* \
    GOPROXY=off \
    GOSUMDB=off \
    GOTOOLCHAIN=local

WORKDIR /workspace
USER 10001:10001

CMD ["go", "version"]
