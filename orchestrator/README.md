# Orchestrator (The Brain) 🧠

The **Orchestrator** is the central intelligence hub of the Krastix ecosystem. It is responsible for understanding user intent, managing context (memory), and coordinating specialized agents to execute complex tasks.

## 🏗 Architecture

The Orchestrator is built upon **LangGraph**, a stateful graph-based library for building reliable actor frameworks. It uses **FastAPI** to expose endpoints and **Google Gemini** as its cognitive engine.

### Core Components

1.  **The Graph Brain (`src/graph.py`)**:
    -   Implements a State Machine using `LangGraph`.
    -   **Planner Node**: The decision-maker. It analyzes the user request, retrieves context from memory, and decides whether to respond directly or delegate a task.
    -   **Dispatcher Node**: Handles the routing of tasks to downstream agents via RabbitMQ (Celery).
    -   **Architecture Diagram**:
        ```mermaid
        graph TD
            User[User Request] --> API[FastAPI Endpoint]
            API --> Planner(Planner Node)
            Planner -- RAG Lookup --> Memory[Vector DB]
            Planner -- "Reasoning" --> Decision{Need Tools?}
            Decision -- No --> End[Respond to User]
            Decision -- Yes --> Dispatcher(Dispatcher Node)
            Dispatcher --> Queue[RabbitMQ]
            Queue --> Agents[Sub-Agents: CRM, Research, etc.]
        ```

2.  **Memory Service (`src/services/memory.py`)**:
    -   Provides **Long-term Semantic Memory** using **PostgreSQL + pgvector**.
    -   Uses **Google Gemini (`text-embedding-004`)** to generate high-dimensional vectors (768d).
    -   Supports metadata filtering (e.g., filtering by `user_id` or `domain`).
    -   **LangGraph Checkpointing**: Uses `AsyncPostgresSaver` to persist conversation state, allowing for long-running, interruptible workflows.

3.  **API Layer (`src/main.py`)**:
    -   Manages the lifecycle of the application (DB connections, Brain initialization).
    -   Provides the primary `/chat` interface for front-end clients.

## 🛠 Technical Specifications

### Tech Stack
-   **Framework**: FastAPI
-   **LLM Orchestration**: LangGraph + LangChain
-   **LLM Provider**: Google Gemini (`gemini-2.0-flash`)
-   **Database**: PostgreSQL (with `pgvector` extension)
-   **Message Broker**: RabbitMQ (via Celery)
-   **Vector Embeddings**: `text-embedding-004`

### Key Files
-   `src/main.py`: Entry point. Sets up the FastAPI app, CORS, and dependency injection.
-   `src/graph.py`: Defines the `OrchestratorGraph` class, the `AgentState` schema, and the workflow nodes.
-   `src/schemas.py`: Pydantic models for API requests (`ChatRequest`) and LLM Tools (`DelegateTask`, `QueueBatch`).
-   `services/memory.py`: Encapsulates logic for embedding generation, vector storage, and semantic search.

## 🚀 Getting Started

### Prerequisites
-   **Python 3.11+**
-   **PostgreSQL** with `vector` extension enabled.
-   **RabbitMQ** instance.
-   **Google Gemini API Key**.

### Environment Variables
The Orchestrator requires the following variables in a `.env` file:

```ini
# Core
GEMINI_API_KEY=AI...
DATABASE_URL=postgresql://user:pass@localhost:5432/krastix_db
REDIS_URL=redis://localhost:6379/0  # Used for Celery broker

# System
PYTHONPATH=/app
```

### Installation

#### Local Development
```bash
# Navigate to the folder (from root)
cd orchestrator

# Install dependencies
pip install -r requirements.txt

# Run the server (ensure you have the 'shared' module in your path)
# Best run from the project root:
uvicorn orchestrator.src.main:app --host 0.0.0.0 --port 8000 --reload
```

#### Docker
```bash
# Build
docker build -f orchestrator/Dockerfile -t krastix-orchestrator .

# Run
docker run -p 8000:8000 --env-file .env krastix-orchestrator
```

## 📡 API Reference

### Health Check
`GET /health`
-   Returns the connectivity status of the Database, Celery Broker, and the Brain itself.

### Chat / Instruction
`POST /chat` (Hypothetical, based on standard patterns) or similar interaction endpoints defined in main.
**(Note: Based on code analysis, check `src/main.py` for exact route definitions, typically used to feed the Graph)**

### Graph Tools
The LLM has access to structured tools defined in `src/schemas.py`:
-   **`DelegateTask`**: Pushes a task to a specific queue (e.g., `research_queue`).
-   **`QueueBatch`**: Handles bulk operations logic.

## 🧠 Memory Logic
1.  **Ingestion**: When agents complete tasks, they push content to the memory endpoint.
2.  **Embedding**: Text is converted to vectors.
3.  **Retrieval**: `Planner` node queries the DB using cosine similarity (`<=>` operator) to find relevant context before answering.
