# Ralph for Claude Code - Docker Image
FROM node:20-bookworm

# Install dependencies
RUN apt-get update && apt-get install -y \
    bash \
    jq \
    git \
    curl \
    python3 \
    python3-pip \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user (Claude Code requires non-root for --dangerously-skip-permissions)
RUN useradd -m -s /bin/bash ralph && \
    echo "ralph ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create working directory and set ownership
WORKDIR /app
RUN chown -R ralph:ralph /app

# Switch to non-root user
USER ralph

# Set home directory
ENV HOME=/home/ralph

# Set entrypoint
ENTRYPOINT ["/bin/bash"]
