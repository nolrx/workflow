# ---- Build stage: compile the Vite/React app ----
FROM node:22-alpine AS build

WORKDIR /app

# Install deps first for better layer caching.
COPY package.json package-lock.json ./
RUN npm ci || npm install

# Build the production bundle (tsc -b && vite build).
COPY . .
RUN npm run build

# ---- Serve stage: nginx hosts the static SPA and proxies /api ----
FROM nginx:1.27-alpine

# Replace the stock server block with our SPA + API-proxy config.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/default.conf /etc/nginx/conf.d/default.conf

# Static build output.
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
