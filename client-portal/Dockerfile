FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV NPM_CONFIG_FETCH_RETRIES=5 NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000 NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
COPY package.json ./
RUN --mount=type=cache,target=/root/.npm for i in 1 2 3 4 5; do npm install && break; [ "$i" = 5 ] && exit 1; echo "npm install failed (attempt $i), retrying"; sleep 10; done
COPY . .
RUN npm run build
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
