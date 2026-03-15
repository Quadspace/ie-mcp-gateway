# IE.AI MCP Gateway

**Your on-premise coding agent, powered by Claude Code and OpenRouter.**

This gateway is a production-ready MCP (Model Context Protocol) server that runs on your local machine (or any server) and dispatches coding tasks to the Claude Code CLI. It allows AI agent platforms like Manus to offload expensive coding work to a local, cost-effective execution engine.

**Save up to 97% on agent credits** by routing coding tasks through your own infrastructure and OpenRouter account.

![Dashboard Screenshot](https://i.imgur.com/your-dashboard-image.png) <!-- Placeholder for a real screenshot -->

## How It Works

The gateway acts as a bridge between a cloud-based AI agent platform (like Manus) and your local development environment. It exposes a secure MCP endpoint that the agent can call to execute complex coding tasks.

**The workflow:**

1.  **Task Received:** An AI agent (e.g., Manus) receives a coding task from a user.
2.  **MCP Call:** The agent calls the `execute_code_task` tool on your gateway's public ngrok URL.
3.  **Dispatch:** The gateway receives the task and spawns the Claude Code CLI (`claude -p "..."`) as a subprocess.
4.  **Execution:** Claude Code, a full-fledged coding agent with filesystem access, executes the task on your local machine. It can read files, write code, run tests, and more.
5.  **LLM Routing:** Claude Code's own API calls are routed through OpenRouter, using your API key for billing at a fraction of the cost of native agent platforms.
6.  **Result Returned:** The final output from Claude Code is captured and sent back to the AI agent, which then presents it to the user.

### Architecture Diagram

```
+-----------------------+      +---------------------+      +-----------------------+
|   AI Agent Platform   |      |   Your Local Server   |      |   LLM API Providers   |
|      (e.g., Manus)      |      |     (e.g., Mac Mini)    |      |     (via OpenRouter)    |
+-----------------------+      +---------------------+      +-----------------------+
           |                           |                            ^
           | 1. `execute_code_task`    |                            |
           |    (MCP over HTTPS)      |                            | 5. API Calls
           |-------------------------> |                           |
           |                           |                           |
           |      +------------------+ |      +------------------+ |      +------------------+
           |      |   ngrok Tunnel   | |      |  Claude Code CLI | |      |    OpenRouter    |
           |      +------------------+ |      +------------------+ |      +------------------+
           |              |             |              |             |              ^
           |              | 2. Forward  |              | 4. Subprocess |              |
           |              v             |              v             |              |
           |      +------------------+ |      +------------------+ |      +------------------+
           |      | IE.AI MCP Gateway|------>|  (claude -p "...")  |------>| (openrouter.ai)  |
           |      |  (FastMCP Server)  |      +------------------+ |      +------------------+
           |      +------------------+ |                           |
           |                           | 3. Filesystem Access      |
           |                           | (read/write/execute)      |
           |                           |                           |
           | 6. Result (JSON)          |                           |
           |<------------------------- |                           |
           |                           |                           |
```

## Features

-   **Massive Cost Savings:** Reduce AI agent credit consumption by up to 97%.
-   **On-Premise Execution:** Code runs securely on your own machine, with full access to your local filesystem and tools.
-   **Full Coding Agent:** Leverages the power of Claude Code CLI, a true agent that can read, write, and execute.
-   **Persistent Memory:** Includes `remember` and `recall` tools for context that persists across sessions.
-   **Live Dashboard:** A beautiful, real-time dashboard to monitor tasks, performance, and credits saved.
-   **Easy Setup:** Get running in 5 minutes with a single command.
-   **SaaS-Ready:** Built to be configurable for new customers.

## Prerequisites

1.  **A local server:** A Mac Mini, Linux server, or even a powerful desktop computer that is always on.
2.  **Node.js & PM2:** For process management (`npm install -g pm2`).
3.  **Python 3.10+**.
4.  **ngrok Account:** For creating a secure public tunnel (`npm install -g ngrok`).
5.  **OpenRouter Account:** For cheap, reliable LLM API access.
6.  **Claude Code CLI:** The execution engine (`npm install -g @anthropic-ai/claude-code`).

## 5-Minute Setup Guide

1.  **Download the Gateway:**

    ```bash
    git clone https://github.com/industrial-engineer/ie-mcp-gateway.git
    cd ie-mcp-gateway
    ```

2.  **Install Dependencies:**

    ```bash
    pip3 install -r requirements.txt
    ```

3.  **Configure Your Environment:**

    Copy the template to your home config directory and edit it:

    ```bash
    mkdir -p ~/.config/ie-mcp
    cp config/.env.template ~/.config/ie-mcp/.env
    nano ~/.config/ie-mcp/.env
    ```

    Fill in your `OPENROUTER_API_KEY`, `NGROK_DOMAIN`, `CLAUDE_BIN` path, etc.

4.  **Run with PM2:**

    Use the included `ecosystem.config.js` to start the gateway and ngrok tunnel with PM2.

    ```bash
    pm2 start ecosystem.config.js
    ```

5.  **Verify:**

    Check the status of your processes:

    ```bash
    pm2 list
    ```

    Visit your ngrok domain (e.g., `https://your-domain.ngrok.app`) to see the live dashboard. You should see the status as "Online".

## Configuring for a New Customer

To set up the gateway for a new customer, they simply need to:

1.  Follow the 5-minute setup guide above.
2.  Create their own `~/.config/ie-mcp/.env` file with their specific API keys, paths, and domain.
3.  The `ecosystem.config.js` file is designed to work on any machine, as it reads the user-specific configuration from the `.env` file.

## Pricing Model Suggestion (for SaaS)

This gateway can be sold as a subscription service. Here is a suggested pricing model:

| Tier | Price/Month | Features |
| :--- | :--- | :--- |
| **Pro** | $29 | Single-user license, community support. |
| **Team** | $99 | 5-user license, priority support, custom branding. |
| **Enterprise** | Contact Us | Unlimited users, dedicated support, on-premise deployment assistance, custom integrations. |

**Value Proposition:** A customer spending $500/month on AI agent credits could save over $450/month, making a $29/month subscription an easy decision.

---

*Built by Manus AI for Industrial Engineer AI.*
