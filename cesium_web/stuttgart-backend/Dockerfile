FROM node:20-slim

WORKDIR /app

# Install dependencies first (cached layer unless package.json changes)
COPY package*.json ./
RUN npm ci --omit=dev

COPY . .

# pg2b3dm requires .NET runtime.
# NOTE: server.mjs currently hard-codes the Windows path to pg2b3dm.exe.
# Set PG2B3DM_PATH env var to /root/.dotnet/tools/pg2b3dm after installing:
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates \
    && wget -qO /tmp/dotnet-install.sh https://dot.net/v1/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 8.0 --runtime dotnet \
    && /root/.dotnet/dotnet tool install -g pg2b3dm \
    && apt-get purge -y wget && rm -rf /var/lib/apt/lists/* /tmp/dotnet-install.sh

ENV PATH="$PATH:/root/.dotnet/tools"
ENV DOTNET_ROOT="/root/.dotnet"

EXPOSE 5000

CMD ["node", "server.mjs"]
