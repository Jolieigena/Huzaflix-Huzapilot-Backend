FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV NPM_CONFIG_FETCH_RETRIES=5 NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000 NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
COPY package.json ./
RUN --mount=type=cache,target=/root/.npm for i in 1 2 3 4 5; do npm install && break; [ "$i" = 5 ] && exit 1; echo "npm install failed (attempt $i), retrying"; sleep 10; done
COPY . .
RUN npm run build
FROM node:22-bookworm-slim
WORKDIR /app
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && apt-get update && apt-get install -y --no-install-recommends curl sqlite3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app .
CMD ["node","dist/main.js"]
