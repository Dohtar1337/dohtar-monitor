FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/package.json .
RUN npm install --production
COPY backend/server.js .
COPY backend/public/ ./public/

# Bundle agent files so /api/download-agent can serve them as a zip
COPY agent/ ./agent/

EXPOSE 9090

CMD ["node", "server.js"]
