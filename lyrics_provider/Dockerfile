# issue 127: internal lyrics provider (Meting). Bound to 127.0.0.1 on the host network.
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY server.mjs ./
USER node
ENV LYRICS_PROVIDER_HOST=127.0.0.1 \
    LYRICS_PROVIDER_PORT=8787
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.LYRICS_PROVIDER_PORT+'/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"
CMD ["node", "server.mjs"]
