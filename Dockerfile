FROM golang:1.27.4-bookworm AS native
RUN git init /opt/agy-go && cd /opt/agy-go && \
    git fetch --depth=1 https://github.com/golang/go.git 298f53e483ef15096a05b1d3017cb9d4207cb327 && \
    git checkout --detach FETCH_HEAD && cd src && GOROOT_BOOTSTRAP=/usr/local/go ./make.bash
ENV GOROOT=/opt/agy-go
ENV PATH=/opt/agy-go/bin:${PATH}
ENV GOTOOLCHAIN=local
WORKDIR /build
COPY native/go.mod native/go.sum ./
RUN go mod download
COPY native/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /gcli2api .

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai
WORKDIR /app
COPY --from=frontend /dist ./dist
COPY --from=native /gcli2api ./gcli2api
COPY version.txt ./version.txt
EXPOSE 7861
CMD ["/app/gcli2api"]
