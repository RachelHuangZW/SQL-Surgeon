# SQL-Surgeon 🩺

**Agentic SQL Tuning Surgeon: Leveraging LLMs and Execution Plan Analysis to automate PostgreSQL Performance Optimization.**

---

## 🌟 Overview

**SQL-Surgeon** is an AI-native database tuning agent designed to bridge the gap between "slow SQL" and "expert-level optimization." Unlike generic Text-to-SQL tools, SQL-Surgeon acts as a **Digital Database Consultant** that doesn't just write queries—it performs "surgery" on them.

Powered by **LangGraph**, it iteratively analyzes PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` outputs, identifies scan/join bottlenecks, and suggests optimized indices or query rewrites based on real execution telemetry.

## 🧠 The Agentic Workflow (Powered by LangGraph)

SQL-Surgeon operates via a sophisticated state machine:

1.  **Diagnosis Node**: Parses `EXPLAIN` plans to find high-cost operators (e.g., Seq Scans, Hash Joins).
2.  **Heuristic Analysis**: Applies expert DBA rules to identify missing indices or sub-optimal join orders.
3.  **Prescription Node**: Generates optimized SQL or DDL (Index creation).
4.  **Verification Node (Sandbox)**: Executes the suggestion in a temporary Docker/Neon container to verify performance gain before reporting.

## 🛠️ Tech Stack

- **Orchestration**: LangGraph / LangChain
- **LLM**: Anthropic Claude 3.5 Sonnet (Optimized for code/logic)
- **Database**: PostgreSQL
- **Infrastructure**: Docker (Local Sandboxing) / Neon.tech (Cloud)
- **Evaluation**: RAGAS (for tuning suggestion faithfulness)

## 📈 Roadmap

- [ ] Core Agentic Loop for Index Suggestions
- [ ] Support for JSON-based EXPLAIN plan parsing
- [ ] Sandbox execution environment using Testcontainers
- [ ] Multi-agent collaboration (Planner + Executor + Evaluator)