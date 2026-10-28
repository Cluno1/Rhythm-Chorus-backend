# issue 127: internal lyrics provider (Meting). Bound to 127.0.0.1 on the host network.
# Pinned to the tag already present on JDC; the registry defaults to npmmirror because JDC reaches
# Chinese hosts directly while its overseas proxy may be down (2026-10-28).
FROM node:22.20.0-bookworm-slim
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund --registry="$NPM_REGISTRY"
COPY server.mjs ./
USER node
ENV LYRICS_PROVIDER_HOST=127.0.0.1 \
    LYRICS_PROVIDER_PORT=8787
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.LYRICS_PROVIDER_PORT+'/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"
CMD ["node", "server.mjs"]
